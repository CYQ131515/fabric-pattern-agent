"""本地花型库：真实图片落盘 + AI 打标签 + 按客户要求检索。

跟 data/patterns.json 的区别：
    patterns.json 是内置演示数据，图案由前端用 SVG 程序画出来，**没有真实图片**；
    这里是真图库 —— 客户给的图、AI 生的图、描稿成品，全部存成图片文件，
    由 AI 打上标签（风格/元素/配色/场景 + 构图/密度/尺度 + 关键词），
    下次客户提要求时直接按标签把图捞出来。

目录（都在 agent/library/ 下，跟着项目走，不依赖任何外部服务）：
    orig/<id>.<ext>     原图，入库后不再改写
    thumb/<id>.jpg      缩略图，列表页用（sips 生成，省流量）
    index.json          索引：一条记录 = 一张图 + 它的标签

设计原则跟整套系统一致：
    **图片本身和"能不能入库"由本地程序定，标签由模型给，但归一到词表后才采信。**
    模型打标签失败 → 图照样入库（status='untagged'），只是没有标签，页面上可以重打。
    绝不因为模型抽风就把用户的图丢了。
"""
import base64
import hashlib
import json
import os
import shutil
import struct
import threading
import time
from contextlib import contextmanager

from . import sync
from .imagegen import sniff

# 参与「命中判定」的维度（跟 search_patterns 一致，客户提要求主要就这几维）
DIMS = ('style', 'element', 'color', 'scene')
# 生产维度：客户一般不直说，但设计师/织印师会用来筛
PROD_DIMS = ('composition', 'density', 'scale')
ALL_DIMS = DIMS + PROD_DIMS

# 来源标记，页面上要显示"这张图哪来的"
SOURCE_LABEL = {
    'import': '客户来图',
    'ai-gen': 'AI 生成',
    'ai-edit': 'AI 改图',
    'trace': '描稿仿稿',
    'seamless': '四方连续',
    'upload': '上传入库',
}

STATUS_LABEL = {
    'ready': '已入库',
    'untagged': '待打标签',
    'need_review': '待复核',
}

# 占号的有效期（秒）。入库中途挂掉的进程会把号留在占号表里，
# 超时后自动剔除，免得号一直往上跳。
RESERVE_TTL = 3600


def image_size(path):
    """读图片长宽（纯标准库，PNG / JPEG）。读不出来返回 (0, 0)。

    自己解的理由跟 imgio 一样：只为拿两个数字不值得引依赖，
    而 sips 要起进程、慢几十倍。
    """
    try:
        with open(path, 'rb') as f:
            head = f.read(24)
            if head[:8] == b'\x89PNG\r\n\x1a\n':
                return struct.unpack('>II', head[16:24])
            if head[:2] != b'\xff\xd8':
                return (0, 0)
            # JPEG：一路扫段，碰到 SOFn 就是它
            f.seek(2)
            while True:
                b = f.read(1)
                while b and b != b'\xff':
                    b = f.read(1)
                if not b:
                    return (0, 0)
                marker = f.read(1)
                while marker == b'\xff':            # 填充字节
                    marker = f.read(1)
                if not marker:
                    return (0, 0)
                code = marker[0]
                if code in (0xD8, 0xD9) or 0xD0 <= code <= 0xD7:
                    continue
                raw = f.read(2)
                if len(raw) < 2:
                    return (0, 0)
                seg = struct.unpack('>H', raw)[0]
                # SOF0~SOF15，跳过 DHT/JPG/DAC 这几个不是 SOF 的
                if 0xC0 <= code <= 0xCF and code not in (0xC4, 0xC8, 0xCC):
                    body = f.read(5)
                    if len(body) < 5:
                        return (0, 0)
                    h, w = struct.unpack('>HH', body[1:5])
                    return (w, h)
                f.seek(seg - 2, os.SEEK_CUR)
    except Exception:
        return (0, 0)


def has_alpha(path):
    """PNG 是否带透明通道（缩略图存 jpg 会把透明压成黑底，这种就用 png）。"""
    try:
        with open(path, 'rb') as f:
            head = f.read(26)
        return head[:8] == b'\x89PNG\r\n\x1a\n' and head[25] in (4, 6)
    except Exception:
        return False


def make_thumb(src, dst, max_px=480, quality=72):
    """生成缩略图。macOS 自带 sips；没有就退回复制原图（宁可大一点也别丢图）。

    sips -Z 是"缩放到最长边"，对**小图会放大**，所以先比一下尺寸，
    本来就不大的图只做格式转换，不放大 —— 放大只会让缩略图更糊更大。
    """
    want_png = has_alpha(src)
    if not shutil.which('sips'):
        try:
            shutil.copyfile(src, dst)
            return True
        except Exception:
            return False
    w, h = image_size(src)
    args = ['sips']
    if max(w, h) > max_px:
        args += ['-Z', str(int(max_px))]
    args += ['-s', 'format', 'png' if want_png else 'jpeg']
    if not want_png:
        args += ['-s', 'formatOptions', str(int(quality))]
    args += [src, '--out', dst]
    try:
        import subprocess
        r = subprocess.run(args, capture_output=True, text=True, timeout=60)
        if r.returncode == 0 and os.path.exists(dst):
            return True
    except Exception:
        pass
    try:                                            # sips 不灵就原样复制
        shutil.copyfile(src, dst)
        return True
    except Exception:
        return False


class PatternLibrary:
    """真图库。线程安全（批量入库会并发打标签，落盘统一加锁）。"""

    def __init__(self, config=None, root=None, tagger=None, vocab=None):
        self.cfg = config
        if root:
            self.root = os.path.normpath(root)
        elif config is not None:
            conf = (config.settings.get('library') or {})
            # ⚠️ 跑实链路 e2e 时真会出图，出图又会自动入库 —— 不隔离的话
            # 测试产物会一批批混进客户来图那个真图库（2026-10-06 已经踩过一次，
            # 手工删了 21 条）。跑 e2e 前把图库目录指到临时目录即可。
            env = os.environ.get('PATTERNFLOW_LIBRARY_DIR')
            self.root = os.path.normpath(
                env or
                os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             conf.get('dir') or 'library'))
        else:
            raise ValueError('PatternLibrary 需要 config 或 root 之一')
        conf = (config.settings.get('library') or {}) if config is not None else {}
        self.enabled = bool(conf.get('enabled', True))
        self.thumb_px = int(conf.get('thumb_px') or 480)
        self.thumb_quality = int(conf.get('thumb_quality') or 72)
        self.auto_ingest = bool(conf.get('auto_ingest', True))
        self.orig_dir = os.path.join(self.root, 'orig')
        self.thumb_dir = os.path.join(self.root, 'thumb')
        self.index_path = os.path.join(self.root, 'index.json')
        self._lock = threading.RLock()
        self._vocab = vocab
        self._tagger = tagger
        os.makedirs(self.orig_dir, exist_ok=True)
        os.makedirs(self.thumb_dir, exist_ok=True)
        self.items = []
        self.load()

    # ---------- 索引读写 ----------
    @contextmanager
    def mutate(self):
        """跨进程安全的「读 → 改 → 写」。

        两个进程（C 端 / 后台）各自在内存里存了一份 items，谁先写完，
        另一个一保存就把对方的记录整份盖掉。所以每次修改都要：
        先跨进程加锁 → 重新读一遍磁盘（拿到别人刚加的）→ 改 → 写回 → 广播变更。

        **注意：所有 get(pid) 必须在 with 块里面做**。load() 会换掉 self.items
        里的对象，块外面先取到的引用是旧的，改了也写不回去。
        """
        with self._lock, sync.guard('library'):
            self.load()
            yield
            self.save()
            sync.rev_bump('library')

    def load(self):
        with self._lock:
            if not os.path.exists(self.index_path):
                self.items = []
                return self
            try:
                with open(self.index_path, encoding='utf-8') as f:
                    raw = json.load(f)
            except Exception as e:
                print('[library] 索引读不出来（%s），先当空库处理，原图不会动' % e)
                self.items = []
                return self
            self.items = raw.get('items') if isinstance(raw, dict) else raw
            if not isinstance(self.items, list):
                self.items = []
            # 原图不见了的记录直接剔掉，免得检索出一堆死链
            alive = [p for p in self.items if p.get('id')]
            dropped = len(self.items) - len(alive)
            self.items = alive
            if dropped:
                print('[library] 丢弃 %d 条无 id 的脏记录' % dropped)
            return self

    def save(self):
        with self._lock:
            tmp = self.index_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({"version": 1, "updated": time.time(),
                           "count": len(self.items), "items": self.items},
                          f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.index_path)     # 原子替换：写一半断电也不会毁库

    # ---------- 工具 ----------
    @property
    def tagger(self):
        if self._tagger is None:
            from .tagger import Tagger
            self._tagger = Tagger(self.cfg, vocab=self._vocab or self.load_vocab())
        return self._tagger

    def load_vocab(self):
        if self._vocab:
            return self._vocab
        if self.cfg is None:
            return {}
        p = os.path.join(self.cfg.path('data'), 'vocab.json')
        if os.path.exists(p):
            try:
                with open(p, encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def urls(self, pid, thumb_ext=None):
        """库里一张图的访问地址。缩略图优先（列表页别拉 2048 原图）。"""
        ext = thumb_ext or '.jpg'
        th = os.path.join(self.thumb_dir, pid + '.png')
        if os.path.exists(th):
            ext = '.png'
        return {'image': '/api/library/file/%s?thumb=1' % pid,
                'full': '/api/library/file/%s' % pid}

    def stats(self):
        by_source, by_status = {}, {}
        for p in self.items:
            by_source[p.get('source') or 'import'] = by_source.get(p.get('source') or 'import', 0) + 1
            by_status[p.get('status') or 'ready'] = by_status.get(p.get('status') or 'ready', 0) + 1
        return {"count": len(self.items), "by_source": by_source, "by_status": by_status,
                "root": self.root, "enabled": self.enabled}

    def get(self, pid):
        for p in self.items:
            if p.get('id') == pid:
                return p
        return None

    def path_of(self, item, thumb=False):
        if not item:
            return None
        if thumb:
            for ext in ('.jpg', '.png', '.jpeg'):
                p = os.path.join(self.thumb_dir, item['id'] + ext)
                if os.path.exists(p):
                    return p
            # 缩略图没生成成功就退回原图，页面不至于开天窗
            return self.path_of(item, False)
        return os.path.join(self.root, item.get('file') or '')

    # ---------- 占号（跨实例 + 跨进程）----------
    # 为什么必须落盘，而不是存在实例属性里
    #     "号已经发出去、但还没写进索引"这段时间是必须被保护的：否则第二个入库的人
    #     会把同一个号再发一次，两张图占同一个 id、同一个文件名 —— 后写的
    #     `open(...,'wb')` 直接把先写的原图**覆盖**掉，那张图就真没了。
    #
    #     放在实例属性上防不住两件事：
    #       · `App.reload()` 会 new 一个新的 PatternLibrary，新实例里那份占号是空的；
    #       · C 端和后台是两个进程，各自的内存互不可见。
    #     两者都会真的撞号（2026-10-10 实测踩到，丢过图）。
    #     所以占号写进库里一个文件，读改写都在 sync.guard('library') 里做。
    def _holds_path(self):
        return os.path.join(self.root, '.reserved.json')

    def _holds(self):
        """读占号表 {"ids": {pid: ts}, "md5": {md5: ts}}，顺手剔掉过期的。"""
        try:
            with open(self._holds_path(), encoding='utf-8') as f:
                d = json.load(f)
        except Exception:
            return {"ids": {}, "md5": {}}
        if not isinstance(d, dict):
            return {"ids": {}, "md5": {}}
        now = time.time()
        out = {}
        for key in ('ids', 'md5'):
            raw = d.get(key)
            raw = raw if isinstance(raw, dict) else {}
            out[key] = {k: v for k, v in raw.items()
                        if isinstance(v, (int, float)) and now - v < RESERVE_TTL}
        return out

    def _holds_write(self, d):
        p = self._holds_path()
        tmp = p + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(d, f)
            os.replace(tmp, p)
        except OSError as e:
            # 写不进去只是退回"仅本进程内防撞"，不该让入库本身失败
            print('[library] 占号表写不进去（%s），并发入库可能撞号' % e)

    def _by_md5(self, md5):
        for p in self.items:
            if p.get('md5') == md5:
                return p
        return None

    @staticmethod
    def _id_tail(pid):
        tail = (pid or '').rsplit('-', 1)[-1]
        return int(tail) if tail.isdigit() else 0

    def _next_id(self, prefix='PF-LIB'):
        year = time.strftime('%Y')
        top = 0
        for p in self.items:
            top = max(top, self._id_tail(p.get('id')))
        # 已经发出去、还没写进索引的号也要算上，否则两张图会撞同一个 id
        for pid in self._holds()['ids']:
            top = max(top, self._id_tail(pid))
        return '%s-%s-%04d' % (prefix, year, top + 1)

    # ---------- 入库 ----------
    def ingest_bytes(self, blob, source='import', src_name=None, name=None,
                     tag=True, check_pattern=True, force=False, extra=None,
                     preset_tags=None):
        """一张图入库：查重 → 落原图 → 生成缩略图 → AI 打标签 → 写索引。

        返回 {ok, id, item, dup?, not_pattern?, error?}。
        **任何一步失败都不丢图**：标签打不出来就 status='untagged' 照样入库。

        preset_tags：闸门（core/atag.py）已经打好的标签。给了它就不再问
        云端 tagger —— 一张图只该被一个引擎打一次，两边都跑又慢又可能
        给出互相冲突的标签。形状见 AtagEngine.to_library_tags()。
        """
        if not blob:
            return {"ok": False, "error": "空的图片数据"}
        md5 = hashlib.md5(blob).hexdigest()
        # 查重和"占号"必须在同一个锁里做完：并发入库时两张内容相同的图
        # 会各自查到"库里还没有"然后双双入库，同一张图进去两份。
        # 这把锁得是**跨进程**的：两个进程各自算号，会算出同一个 id。
        # 同理，占号也得写进文件（见 _holds 的说明）—— 光靠进程内变量挡不住 reload
        # 换实例和"两个进程同时入库"这两种情况。
        with self._lock, sync.guard('library'):
            self.load()
            dup = self._by_md5(md5)
            if dup and not force:
                return {"ok": True, "dup": True, "id": dup['id'], "item": dup,
                        "note": "这张图库里已经有了（内容完全相同），没有重复入库。"}
            holds = self._holds()
            if md5 in holds['md5']:
                return {"ok": True, "dup": True, "id": None, "pending": True,
                        "note": "同一张图正在入库中，跳过。"}
            pid = self._next_id()
            holds['ids'][pid] = time.time()
            holds['md5'][md5] = time.time()
            self._holds_write(holds)
        try:
            return self._store(pid, sniff(blob)[0], sniff(blob)[1], blob, md5, source,
                               src_name, name, tag, check_pattern, extra,
                               preset_tags=preset_tags)
        finally:
            with self._lock, sync.guard('library'):
                holds = self._holds()
                holds['ids'].pop(pid, None)
                holds['md5'].pop(md5, None)
                self._holds_write(holds)

    def _store(self, pid, ext, mime, blob, md5, source, src_name, name, tag,
               check_pattern, extra, preset_tags=None):
        rel = os.path.join('orig', pid + ext)
        dst = os.path.join(self.root, rel)
        try:
            with open(dst, 'wb') as f:
                f.write(blob)
        except Exception as e:
            return {"ok": False, "error": "原图落盘失败：%s" % e}

        w, h = image_size(dst)
        thumb_ext = '.png' if has_alpha(dst) else '.jpg'
        thumb_path = os.path.join(self.thumb_dir, pid + thumb_ext)
        make_thumb(dst, thumb_path, self.thumb_px, self.thumb_quality)
        # 另一种格式的残留清掉，避免 urls() 判错
        for other in ('.jpg', '.png'):
            if other != thumb_ext and os.path.exists(os.path.join(self.thumb_dir, pid + other)):
                os.remove(os.path.join(self.thumb_dir, pid + other))

        tags, status, tag_error, raw = {}, 'untagged', None, None
        if preset_tags:
            # 闸门（core/atag.py）已经打过标签了：直接采用，不再问云端 tagger。
            # 闸门保证 only ok 的预设才会走到这里（不是花型/没跑通都被拦下了）。
            tags = dict(preset_tags)
            raw = tags.get('raw')
            status = 'ready' if tags.get('ok') else 'need_review'
            if not tags.get('ok'):
                tag_error = tags.get('error') or '闸门没给出有效标签'
        elif tag:
            try:
                res = self.tagger.tag(base64.b64encode(blob).decode(), mime)
                if res.get('ok') is False and res.get('not_pattern'):
                    if check_pattern:
                        # 不是花型图（截图/人物/实景）就不进花型库，别把库搞脏。
                        # 原图删掉，缩略图删掉 —— 用户要的是花型库，不是相册。
                        try:
                            os.remove(dst)
                            if os.path.exists(thumb_path):
                                os.remove(thumb_path)
                        except Exception:
                            pass
                        return {"ok": False, "not_pattern": True,
                                "reason": res.get('reason'),
                                "error": "这张图看起来不是面料花型（%s），没有入库。"
                                         % (res.get('reason') or '画面与花型无关')}
                    status = 'need_review'    # 留着但标出来，别让人以为它是好图
                    tag_error = res.get('reason')
                else:
                    tags = res
                    status = 'ready' if res.get('ok') else 'need_review'
                    if not res.get('ok'):
                        tag_error = res.get('error')
                    raw = res.get('raw')
            except Exception as e:
                tag_error = '%s: %s' % (type(e).__name__, e)
                print('[library] 打标签失败（图仍会入库）：%s' % tag_error)

        item = self._build_item(pid, rel, w, h, md5, source, src_name, tags, status,
                                tag_error, raw, name, extra)
        with self.mutate():
            self.items.append(item)
        return {"ok": True, "id": pid, "item": item,
                "tagged": bool(tags.get('ok')) if isinstance(tags, dict) else False}

    def ingest_file(self, path, source='import', name=None, tag=True,
                    check_pattern=True, force=False, extra=None, move=False,
                    preset_tags=None):
        try:
            with open(path, 'rb') as f:
                blob = f.read()
        except Exception as e:
            return {"ok": False, "error": "读不了这个文件：%s" % e}
        res = self.ingest_bytes(blob, source=source, src_name=os.path.basename(path),
                                name=name, tag=tag, check_pattern=check_pattern,
                                force=force, extra=extra, preset_tags=preset_tags)
        if res.get('ok') and not res.get('dup') and move:
            try:
                os.remove(path)
            except Exception:
                pass
        return res

    def _build_item(self, pid, rel, w, h, md5, source, src_name, tags, status,
                    tag_error, raw, name, extra):
        tags = tags if isinstance(tags, dict) else {}
        item = {
            "id": pid,
            "lib": True,                          # 前端靠这个区分"真图"和"程序画的花型"
            "name": (name or tags.get('name') or '').strip() or ('未命名-' + pid[-4:]),
            "file": rel,
            "w": w, "h": h,
            "md5": md5,
            "source": source,
            "source_label": SOURCE_LABEL.get(source, source),
            "src_file": src_name,
            "status": status,
            "status_label": STATUS_LABEL.get(status, status),
            "added": time.time(),
            "updated": time.time(),
            "tag_mode": 'ai' if tags.get('ok') else 'none',
            "in_use": 0,
        }
        for k in ALL_DIMS:
            item[k] = tags.get(k) or None
        item['keywords'] = tags.get('keywords') or []
        item['colors'] = tags.get('colors') or []        # 自由色名（词表外的原话也留着）
        item['desc'] = tags.get('desc') or ''
        item['fabric'] = tags.get('fabric') or ''
        item['tag_reason'] = tags.get('reason') or ''
        item['tag_rejected'] = tags.get('rejected') or {}
        # 这标签是谁打的：atag = 本地算法闸门（后台导入/描稿/四方连续成品），
        # 空 = 云端 tagger（AI 生成/改图）。排查"这张图标签怎么来的"看它。
        item['tag_engine'] = tags.get('tag_engine') or ''
        item['raw'] = raw
        if tag_error:
            item['tag_error'] = tag_error
        if extra:
            item.update(extra)
        item.update(self.urls(pid))
        return item

    # ---------- 改 / 删 / 重打标签 ----------
    EDITABLE = ('name', 'style', 'element', 'color', 'scene', 'composition',
                'density', 'scale', 'desc', 'fabric', 'status')

    def update(self, pid, fields):
        """人工改标签。只认白名单里的字段，别让前端把 id/file 改坏。"""
        with self.mutate():
            item = self.get(pid)
            if not item:
                return {"ok": False, "error": "库里没这张图：%s" % pid}
            for k, v in (fields or {}).items():
                if k in self.EDITABLE:
                    item[k] = (v or None) if k != 'name' else ((v or '').strip() or item['name'])
                elif k in ('keywords', 'colors'):
                    if isinstance(v, list):
                        item[k] = [str(x).strip() for x in v if str(x).strip()]
                    elif isinstance(v, str):
                        item[k] = [x.strip() for x in v.replace('，', ',').split(',') if x.strip()]
            item['tag_mode'] = 'manual'
            if item.get('status') in (None, 'untagged', 'need_review'):
                item['status'] = 'ready'
                item['status_label'] = STATUS_LABEL['ready']
            item['updated'] = time.time()
            self.save()
            return {"ok": True, "item": item}

    def touch_use(self, pid):
        """记一次"被选用"（热度，检索排序用）。"""
        with self.mutate():
            item = self.get(pid)
            if item:
                item['in_use'] = int(item.get('in_use') or 0) + 1
            return item

    def retag(self, pid):
        """重打标签：AI 再读一遍，覆盖自动字段，不动人工改过的 name。"""
        item = self.get(pid)
        if not item:
            return {"ok": False, "error": "库里没这张图：%s" % pid}
        src = self.path_of(item)
        if not src or not os.path.exists(src):
            return {"ok": False, "error": "原图找不到，可能被移动或删除了"}
        try:
            with open(src, 'rb') as f:
                blob = f.read()
            res = self.tagger.tag(base64.b64encode(blob).decode(), sniff(blob)[1])
        except Exception as e:
            return {"ok": False, "error": "重打标签失败：%s: %s" % (type(e).__name__, e)}
        if not res.get('ok'):
            return {"ok": False, "error": "这次没读出有效标签，稍后再试"}
        with self.mutate():
            # 锁里重新取一次：mutate 会 load()，外面那个引用已经不是索引里的对象了
            item = self.get(pid)
            if not item:
                return {"ok": False, "error": "库里没这张图：%s" % pid}
            for k in ALL_DIMS:
                if res.get(k):
                    item[k] = res[k]
            item['keywords'] = res.get('keywords') or item.get('keywords') or []
            item['colors'] = res.get('colors') or item.get('colors') or []
            item['desc'] = res.get('desc') or item.get('desc') or ''
            item['fabric'] = res.get('fabric') or item.get('fabric') or ''
            if not item.get('name') or str(item.get('name')).startswith('未命名'):
                item['name'] = res.get('name') or item['name']
            item.update(self.urls(pid))
            item['status'] = 'ready'
            item['status_label'] = STATUS_LABEL['ready']
            item['tag_mode'] = 'ai'
            item['tag_error'] = None
            item['updated'] = time.time()
        return {"ok": True, "item": item}

    def remove(self, pid, purge=True):
        """从库里删掉。purge=True 连原图一起删（默认），False 只下架索引。"""
        with self.mutate():
            item = self.get(pid)
            if not item:
                return {"ok": False, "error": "库里没这张图：%s" % pid}
            self.items = [p for p in self.items if p.get('id') != pid]
        if purge:
            for p in (self.path_of(item), self.path_of(item, True)):
                try:
                    if p and os.path.exists(p) and os.path.commonpath(
                            [os.path.abspath(p), os.path.abspath(self.root)]) == \
                            os.path.abspath(self.root):
                        os.remove(p)
                except Exception as e:
                    print('[library] 删文件失败：%s' % e)
        return {"ok": True, "id": pid, "removed": item.get('name')}

    # ---------- 检索 ----------
    def search(self, brief=None, q=None, limit=6, offset=0, source=None,
               status=None, dims=None, strict=False):
        """按客户要求检索。

        两种模式，差别在"命中不了怎么办"：
            strict=False（机器人检索用）—— 打分排序，命中 0 的也带回来当"沾边款"，
                由「命中维度数 + 匹配度」排序，`matched` 说明库里到底有没有对得上的；
            strict=True（库页面筛选用）—— 严格过滤，选了"法式浪漫"就只给法式浪漫，
                不然用户勾了筛选还看到一堆不相干的图，会以为筛坏了。

        "库里有对得上的吗"由这里的确定性算法说了算，不交给模型自己感觉。
        """
        brief = {k: v for k, v in (brief or {}).items() if v}
        known = [k for k in (dims or DIMS) if brief.get(k)]
        q = (q or '').strip()
        ql = q.lower()

        rows = []
        for p in self.items:
            if source and p.get('source') != source:
                continue
            if status and p.get('status') != status:
                continue
            hay = ' '.join([
                str(p.get('name') or ''), str(p.get('desc') or ''),
                str(p.get('fabric') or ''), str(p.get('tag_reason') or ''),
                ' '.join(p.get('keywords') or []), ' '.join(p.get('colors') or []),
                ' '.join(str(p.get(k) or '') for k in ALL_DIMS),
            ]).lower()
            if strict:
                if any(p.get(k) != brief[k] for k in known):
                    continue
                if ql and ql not in hay:
                    continue

            hit = sum(1 for k in known if p.get(k) == brief.get(k))
            prod_hit = sum(1 for k in PROD_DIMS if brief.get(k) and p.get(k) == brief.get(k))

            if known:
                score = 52 + hit / len(known) * 42
            else:
                score = 62
            # 生产维度命中算轻量加分：客户不明说，但说到了就该往前排
            score += prod_hit * 4

            if ql and ql in hay:
                score += 12
                if ql in str(p.get('name') or '').lower():
                    score += 6
                if any(ql in str(k).lower() for k in (p.get('keywords') or [])):
                    score += 6
            # 热度：被别的客户选用过的款稍微往前排
            score += min(6, int(p.get('in_use') or 0) * 2)
            rows.append((hit, score, p))

        rows.sort(key=lambda x: (-x[0], -x[1], -(x[2].get('added') or 0)))
        total = len(rows)                       # 分页前的总数，前端要拿它显示"共 N 款"
        scored_items = []
        for hit, score, p in rows:
            scored_items.append({
                "id": p['id'], "name": p.get('name'),
                "style": p.get('style'), "element": p.get('element'),
                "color": p.get('color'), "scene": p.get('scene'),
                "composition": p.get('composition'), "density": p.get('density'),
                "scale": p.get('scale'),
                "colors": p.get('colors'), "keywords": p.get('keywords'),
                "desc": p.get('desc'), "fabric": p.get('fabric'),
                "tag_reason": p.get('tag_reason'),   # 详情弹层要显示"AI 凭什么这么判断"
                "source": p.get('source'), "source_label": p.get('source_label'),
                "src_file": p.get('src_file'),
                "status": p.get('status'),
                "image": p.get('image'), "full": p.get('full'),
                "lib": True,
                "match": int(round(score)),
                "hit": hit,
                "in_use": p.get('in_use') or 0,
            })
        items = scored_items[offset:offset + limit] if limit else scored_items[offset:]

        best_hit = max([r[0] for r in rows] + [0])
        need = 1 if len(known) <= 1 else 2
        matched = bool(known) and best_hit >= need
        # 没给维度、只输入了关键词：命中就算对上（客户原话检索）
        if not known and ql:
            matched = any(ql in ' '.join([str(p.get('name') or ''),
                                          str(p.get('desc') or ''),
                                          ' '.join(p.get('keywords') or [])]).lower()
                          for _, _, p in rows)
        return {
            "items": items,
            "count": len(items),
            "total": total,
            "matched": matched,
            "best_hit": best_hit,
            "known_dims": len(known),
            "need_hit": need,
        }

    def describe(self):
        d = self.stats()
        d.update({"auto_ingest": self.auto_ingest, "thumb_px": self.thumb_px,
                  "tagger": self.tagger.describe()})
        return d
