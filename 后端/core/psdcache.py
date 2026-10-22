"""分层 PSD 的结果缓存：同一张图（同层数）只真跑一次，之后直接调出来。

为什么要有它
    分层 PSD 一次 45 点、跑云端 SAM3.1 工作流要一两分钟。而「同一张图
    要做 PSD」是高频请求：客户在花型库选中同一款、不同人都来要分层文件。
    第一个客户做完就把结果收下来，后面每个人直接调 —— 不重复烧点数、
    不重复等云端，拿到的还是同一份逐像素验证过的产物。

键：源图 md5 + 层数
    源图一样、层数一样，产出就该是同一份（SAM3.1 逐次会飘，所以"复用之"
    比"再跑一次"还更稳）。层数不同是两码事：分 2 层和分 4 层是两个文件，
    键里必须带上，不能互相顶替。

一条记录 = PSD 本体 + 元数据
    <key>.psd   上次真跑动产出的 PSD 本体（原样存，不再重画一个像素）
    <key>.json  层名/占比/自检指标/层数/点数 —— 回放时靠它重建一份和
                真跑动同形状的返回，客户拿到的话术、下载按钮都一样

伪装（产品要求，写进契约）
    命中时**不能瞬间返回**。前端等的是一个"正在分层"的过程：气泡上有
    秒表、每两秒问一次「第几层 / 等了多久」。瞬间完成等于告诉用户
    "这是抄的"。所以命中时按真跑动的节奏伪装一段（40~60 秒随机，
    见 skills/layer_psd/handler.py 顶部常数），期间由引擎的
    begin_cached_run() 供着进度文件，/api/psd_progress 看到的一切
    和真跑动一模一样。

目录：默认 数据/psd-cache/，可用环境变量 PATTERNFLOW_PSD_CACHE_DIR
    覆盖（测试隔离用，跟 PATTERNFLOW_LIBRARY_DIR 一个路子）。
    目录**懒创建**：只是建个引擎不该在磁盘上留东西，put 时才建。
"""
import hashlib
import json
import os
import shutil

# 项目根（后端/ 的上一层……准确说：backend/ 本身，相对路径都相对它解析，
# 和 core/atag.py、core/psdlayer.py 同一套路数）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_CONF = {
    "cache_enabled": True,           # 总闸：关了就不命不存，每次都用真跑动
    "cache_dir": "../数据/psd-cache",   # 相对 后端/
}


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _atomic_json(path, obj):
    """原子写 JSON：tmp + os.replace，读方永远看不到半截文件
    （和算法侧写 progress.json 一个写法）。"""
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


class PsdCache:
    """按「源图 md5 + 层数」缓存分层 PSD 的产出。"""

    def __init__(self, config=None):
        conf = dict(DEFAULT_CONF)
        conf.update(((config.settings.get('psdlayer') or {})
                     if config is not None else {}))
        self.enabled = bool(conf.get('cache_enabled', True))
        raw = (conf.get('cache_dir') or '').strip() or DEFAULT_CONF['cache_dir']
        # 环境变量优先：跑 e2e / 回归时把缓存指到临时目录，
        # 别让测试产物混进线上的缓存（和图库、埋点同一个理由）。
        env = (os.environ.get('PATTERNFLOW_PSD_CACHE_DIR') or '').strip()
        self.root = os.path.normpath(env if env else os.path.join(ROOT, raw))

    # ---------- 键 ----------
    @staticmethod
    def key_of(blob, max_layers):
        """同一张图、同一层数 → 同一个键。blob 是源图的字节。"""
        if not blob:
            return None
        return '%s_%d' % (hashlib.md5(blob).hexdigest(),
                          max(1, int(max_layers or 1)))

    def _paths(self, key):
        return (os.path.join(self.root, key + '.psd'),
                os.path.join(self.root, key + '.json'))

    # ---------- 读 ----------
    def get(self, key):
        """命中返回 {'psd': 路径, 'meta': 元数据}；没命中/缓存关了 → None。

        文件少了半份（PSD 在、元数据没了，或反过来）都当没命中 ——
        宁可多跑一次真算法，也不能给客户一份缺东少西的产物。
        """
        if not self.enabled or not key:
            return None
        psd, meta_path = self._paths(key)
        if not os.path.exists(psd):
            return None
        meta = _read_json(meta_path) or {}
        if not meta.get('layers'):
            return None
        return {'psd': psd, 'meta': meta}

    # ---------- 写 ----------
    def put(self, key, psd_path, meta):
        """把一次真跑动的结果收进缓存。失败只返回 False，绝不影响出图。

        复制而不是移动：源文件在算法的临时目录里，调用方紧接着要删整个
        目录；copy 失败（磁盘满等）顶多少一份缓存，图已经给到客户了。
        """
        if not self.enabled or not key or not psd_path:
            return False
        if not os.path.exists(psd_path):
            return False
        try:
            os.makedirs(self.root, exist_ok=True)
            dst, meta_path = self._paths(key)
            shutil.copyfile(psd_path, dst)
            rec = dict(meta or {})
            rec['key'] = key
            rec['created'] = os.path.getmtime(dst)
            _atomic_json(meta_path, rec)
            return True
        except Exception as e:                           # noqa: BLE001
            print('[psdcache] 缓存写入失败（不影响出图）：%s: %s'
                  % (type(e).__name__, e))
            return False

    # ---------- 状态 ----------
    def stats(self):
        """给排查用：缓存里有多少份、一共多大。"""
        if not os.path.isdir(self.root):
            return {'count': 0, 'bytes': 0, 'root': self.root}
        n, total = 0, 0
        try:
            for f in os.listdir(self.root):
                if f.endswith('.psd'):
                    n += 1
                    total += os.path.getsize(os.path.join(self.root, f))
        except Exception:
            pass
        return {'count': n, 'bytes': total, 'root': self.root}
