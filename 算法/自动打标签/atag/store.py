"""存储：图片与标签一起存，按内容指纹判重。

目录结构（跑完一批之后长这样）：

    out/
      images/            原图的副本，保持原文件名
        花型A.jpg
      tags/              一张图一条记录
        花型A.json
      index.json         汇总索引，程序读这个

几条约定：

**按内容判重，不按文件名**。客户把同一张图改个名再传一遍是常态，
   所以用 md5 认图。已经打过标并且成功的，默认跳过（要重打加 --force）。
   这样批量任务被中断后重跑，不会把已经花掉的时间再花一遍。

**原图复制一份**。不是软链 —— 源目录随时可能被整理掉，标签留下但图没了
   是最糟的状态（搜到了却看不到）。复制的成本只是磁盘。

**写盘是原子的**。先写 .tmp 再 os.replace，读的一方永远看不到半截文件。
   跟主程序所有落盘的地方一致。

**落盘串行**。批量打标是并发调模型，但写文件用锁串起来 ——
   索引是"读-改-写"，两个线程同时改会把对方的记录吞掉。
"""
import json
import os
import shutil
import threading
import time

from .image import md5_file

INDEX_NAME = 'index.json'
IMAGES_DIR = 'images'
TAGS_DIR = 'tags'

# 记录状态。打标失败**不丢图** —— 跟主程序花型库"打标失败不丢图"同一条。
STATUS_LABEL = {
    'ok': '已打标',
    'empty': '是花型但四维都没给出标签',
    'not_pattern': '不是花型图',
    'failed': '打标失败',
}


class Store:
    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.images = os.path.join(self.root, IMAGES_DIR)
        self.tags = os.path.join(self.root, TAGS_DIR)
        self.index_path = os.path.join(self.root, INDEX_NAME)
        self._lock = threading.Lock()
        os.makedirs(self.images, exist_ok=True)
        os.makedirs(self.tags, exist_ok=True)

    # ---------- 索引 ----------
    def load_index(self):
        """读索引。坏了就当空的，不让它挡住新的一批。"""
        try:
            with open(self.index_path, encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {'version': 1, 'updated': 0, 'items': []}
        if not isinstance(data, dict):
            return {'version': 1, 'updated': 0, 'items': []}
        items = data.get('items')
        if not isinstance(items, list):
            items = []
        data['items'] = items
        return data

    def by_md5(self):
        """{md5: 记录}，判重用。"""
        return {r.get('md5'): r for r in self.load_index().get('items') or []
                if r.get('md5')}

    def done_md5s(self):
        """已经成功打过标的 md5 集合 —— 断点续跑跳过它们。"""
        return {r.get('md5') for r in self.load_index().get('items') or []
                if r.get('md5') and r.get('status') in ('ok', 'empty',
                                                        'not_pattern')}

    def save_index(self, items):
        """整表重写（原子）。items 就是我们自己的真源，这里只是把它序列化。"""
        payload = {'version': 1, 'updated': time.time(),
                   'count': len(items), 'items': items}
        tmp = self.index_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.index_path)
        return payload

    # ---------- 写入 ----------
    def _unique_image_path(self, src_path):
        """同名图加 -2 / -3，跟主程序花型库同一套约定。"""
        stem, ext = os.path.splitext(os.path.basename(src_path))
        cand = os.path.join(self.images, stem + ext)
        n = 2
        while os.path.exists(cand):
            # 已经是同一份内容就复用（重跑同一张图不该再复制一份）
            try:
                if md5_file(cand) == md5_file(src_path):
                    return cand, False
            except OSError:
                pass
            cand = os.path.join(self.images, '%s-%d%s' % (stem, n, ext))
            n += 1
        return cand, True

    def save(self, record, src_path):
        """存一条：复制原图 + 写记录 + 更新索引。返回补全后的记录。

        整段在锁里跑 —— 索引是读-改-写，并发下必须串行。
        """
        with self._lock:
            img_path, need_copy = self._unique_image_path(src_path)
            if need_copy:
                shutil.copy2(src_path, img_path)

            rec = dict(record)
            rec['file'] = os.path.relpath(img_path, self.root).replace(os.sep, '/')
            # id 直接用图片文件名（不含扩展名）—— 客户在文件夹里认得出
            rec['id'] = os.path.splitext(os.path.basename(img_path))[0]
            rec.setdefault('added', time.time())
            rec['updated'] = time.time()
            rec['status_label'] = STATUS_LABEL.get(rec.get('status'), '')

            tag_path = os.path.join(self.tags, rec['id'] + '.json')
            tmp = tag_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(rec, f, ensure_ascii=False, indent=1)
            os.replace(tmp, tag_path)
            rec['tag_file'] = os.path.relpath(tag_path, self.root).replace(os.sep, '/')

            data = self.load_index()
            items = [r for r in data['items']
                     if r.get('md5') != rec.get('md5') and r.get('id') != rec['id']]
            items.append(rec)
            # 最近打的排前面（跟会话列表、花型库一致的阅读习惯）
            items.sort(key=lambda r: r.get('updated') or 0, reverse=True)
            self.save_index(items)
            return rec

    def save_many(self, records):
        """一次写多条（批量任务结束时用），只重写一次索引。"""
        with self._lock:
            data = self.load_index()
            items = list(data['items'])
            for rec in records:
                items = [r for r in items
                         if r.get('md5') != rec.get('md5') and r.get('id') != rec.get('id')]
                items.append(rec)
            items.sort(key=lambda r: r.get('updated') or 0, reverse=True)
            self.save_index(items)
            return len(records)

    # ---------- 读取 ----------
    def one(self, ident):
        """按 id 读一条。"""
        p = os.path.join(self.tags, ident + '.json')
        try:
            with open(p, encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def all(self):
        return self.load_index().get('items') or []

    def summary(self):
        """给命令行和页面用的一句话统计。"""
        items = self.all()
        n = {}
        for r in items:
            s = r.get('status') or 'failed'
            n[s] = n.get(s, 0) + 1
        return {
            'total': len(items),
            'ok': n.get('ok', 0),
            'not_pattern': n.get('not_pattern', 0),
            'empty': n.get('empty', 0),
            'failed': n.get('failed', 0),
            'root': self.root,
        }
