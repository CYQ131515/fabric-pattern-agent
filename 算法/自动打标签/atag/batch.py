"""批量打标：扫文件夹、并发调模型、断点续跑。

批量场景的四个真实麻烦，都在这里解决：

1. **跑一半断了不能重来**。几千张图跑两小时，中途关机是常事。
   所以每张打完就落盘，并且记下 md5；重跑时已经成功的直接跳过。
   不依赖"上一次跑到第几张"这种计数 —— 计数在并发下不可靠。

2. **进度要能看见**。本地模型一张图几秒，几百张就是十几分钟，
   用户得知道跑到哪了。进度原子写到 --progress-out，主程序边跑边读。

3. **一张坏图不能拖垮一批**。读到坏文件、模型这一次抽风、
   返回了没法解析的东西 —— 都只记这一张失败，继续下一张。

4. **并发不是越大越好**。本地模型只有一个 GPU，并发开太大只是让
   请求排队，还会把显存挤爆（大图吃显存）。默认 3 就够了。
"""
import concurrent.futures as futures
import json
import os
import time

from . import parse as P
from .image import ImageError, is_image, load_for_model, md5_file, probe
from .prompt import PROMPT_VERSION, build_prompt
from .taxonomy import DIMS

DEFAULT_WORKERS = 3
DEFAULT_MAX_SIDE = 1280


def collect(paths, recursive=True, limit=0):
    """把"文件夹 / 文件 / 两者混着"统一成图片文件列表。

    排序后再返回 —— 不然同一批图的处理顺序每次都不一样，
    重跑时日志对不上。
    """
    out = []
    for p in paths:
        p = os.path.abspath(os.path.expanduser(p))
        if os.path.isfile(p):
            if is_image(p):
                out.append(p)
        elif os.path.isdir(p):
            if recursive:
                for root, dirs, files in os.walk(p):
                    # 跳过自己上次的输出目录，免得把打过标的图再打一遍
                    dirs[:] = [d for d in dirs
                               if d not in ('images', 'tags', '__pycache__')]
                    for f in files:
                        if is_image(f) and not f.startswith('.'):
                            out.append(os.path.join(root, f))
            else:
                for f in os.listdir(p):
                    fp = os.path.join(p, f)
                    if os.path.isfile(fp) and is_image(f) and not f.startswith('.'):
                        out.append(fp)
        else:
            raise FileNotFoundError('这个路径不存在：%s' % p)
    # 去重（同一张图被两个路径指到）+ 排序
    out = sorted(set(out))
    if limit and limit > 0:
        out = out[:limit]
    return out


class Progress:
    """进度落盘。原子写，主程序可以随时来读，读到的永远是完整 JSON。"""

    def __init__(self, path='', total=0):
        self.path = path or ''
        self.total = total
        self.done = 0
        self.ok = 0
        self.not_pattern = 0
        self.empty = 0
        self.failed = 0
        self.skipped = 0
        self.duplicate = 0
        self.started = time.time()
        self.current = ''
        self.error = ''
        self._last_write = 0.0

    def bump(self, status, ident='', error=''):
        self.done += 1
        if status == 'ok':
            self.ok += 1
        elif status == 'not_pattern':
            self.not_pattern += 1
        elif status == 'empty':
            self.empty += 1
        else:
            self.failed += 1
        if ident:
            self.current = ident
        if error:
            self.error = error[:200]
        self.write()

    def skip(self, n=1):
        self.skipped += n
        self.write()

    def write(self, force=False):
        if not self.path:
            return
        now = time.time()
        # 节流：并发下每张都写会互相抢锁，间隔小于 0.3 秒就攒着
        if not force and (now - self._last_write) < 0.3:
            return
        self._last_write = now
        payload = {
            'phase': 'tagging',
            'total': self.total,
            'done': self.done,
            'ok': self.ok,
            'not_pattern': self.not_pattern,
            'empty': self.empty,
            'failed': self.failed,
            'skipped': self.skipped,
            'duplicate': self.duplicate,
            'current': self.current,
            'elapsed': round(now - self.started, 1),
            'error': self.error,
            'ts': now,
        }
        tmp = self.path + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            pass          # 进度写不进去不该影响打标


def tag_one(client, prompt, path, vocab, max_side=DEFAULT_MAX_SIDE):
    """给一张图打标，返回一条完整记录（还没存）。

    任何一步出错都变成记录里的 status=failed，不往上抛 ——
    批量任务里单张失败是常态，不该中断整批。

    记录结构是**平铺**的：四维直接放在顶层（style/element/color/scene），
    跟主程序花型库 index.json 的写法一致，这样两边的索引能互相读。
    """
    t0 = time.time()
    rec = {
        'source': os.path.abspath(path),
        'model': client.model,
        'provider': client.base_url,
        'prompt_version': PROMPT_VERSION,
        'free': {d: [] for d in DIMS},
        'colors': [], 'keywords': [],
        'desc': '', 'reason': '', 'confidence': None,
        'is_pattern': None,
        'truncated': False, 'suspect': False,
        'status': 'failed', 'error': '',
    }
    for d in DIMS:
        rec[d] = []

    # 元信息（宽高）先拿，失败也有——出图记录里少了尺寸很难排查
    try:
        meta = probe(path)
        rec.update({'w': meta['w'], 'h': meta['h'],
                    'format': meta['format'], 'bytes': meta['bytes']})
    except Exception:                                     # noqa: BLE001
        pass

    try:
        rec['md5'] = md5_file(path)
    except OSError as e:
        rec['error'] = '读不了这个文件：%s' % e
        rec['elapsed_ms'] = int((time.time() - t0) * 1000)
        return rec

    try:
        b64, mime = load_for_model(path, max_side)
    except ImageError as e:
        rec['error'] = str(e)
        rec['elapsed_ms'] = int((time.time() - t0) * 1000)
        return rec

    try:
        text, usage = client.see(prompt, b64, mime)
    except Exception as e:                                # noqa: BLE001
        rec['error'] = str(e)[:300]
        rec['elapsed_ms'] = int((time.time() - t0) * 1000)
        return rec

    parsed, truncated = P.parse_json(text)
    if not parsed:
        rec['error'] = '模型没返回可解析的 JSON：%s' % (text or '')[:150]
        rec['elapsed_ms'] = int((time.time() - t0) * 1000)
        return rec

    st = P.structure(parsed, vocab, truncated)
    for d in DIMS:
        rec[d] = st['tags'][d]
    rec['free'] = st['free']
    rec['colors'] = st['colors']
    rec['keywords'] = st['keywords']
    rec['desc'] = st['desc']
    rec['reason'] = st['reason']
    rec['confidence'] = st['confidence']
    rec['is_pattern'] = st['is_pattern']
    rec['truncated'] = st['truncated']
    rec['suspect'] = st['suspect']
    rec['raw'] = st['raw']
    rec['usage'] = usage or {}

    if st['is_pattern'] is False:
        rec['status'] = 'not_pattern'
    elif not P.tag_any(st['tags']):
        # 是花型图，但四个维度一个词都没落 —— 记下来，值得看看是
        # 模型太保守还是图确实没特征
        rec['status'] = 'empty'
    else:
        rec['status'] = 'ok'

    rec['elapsed_ms'] = int((time.time() - t0) * 1000)
    return rec


def run(paths, store, client, vocab, max_side=DEFAULT_MAX_SIDE,
        workers=DEFAULT_WORKERS, force=False, progress=None,
        on_done=None, extra_note='', limit=0):
    """跑一批。

    返回 summary dict。on_done(记录, 序号) 用来给命令行实时打点。
    """
    files = collect(paths, limit=limit)
    total = len(files)
    prog = progress or Progress()
    prog.total = total

    done_md5 = set() if force else store.done_md5s()
    prompt = build_prompt(vocab, extra_note)

    # 两轮去重，缺一不可：
    #   跟"已经打过的"比 —— 断点续跑靠这个；
    #   跟"这一批里前面的"比 —— 客户目录里同一张图存两份太常见了，
    #   不去重就会对同一张图调两次模型（本地虽然不花钱，但白等一次），
    #   而且 tags/ 里会留下两条内容一样的记录、跟 index.json 对不上。
    todo, skipped, dup = [], 0, 0
    seen = {}
    for f in files:
        try:
            m = md5_file(f)
        except OSError:
            todo.append(f)          # 读不了会在 tag_one 里记失败
            continue
        if m in done_md5:
            skipped += 1
            continue
        if m in seen:
            dup += 1
            continue
        seen[m] = f
        todo.append(f)
    prog.duplicate = dup
    prog.skip(skipped)
    prog.write(force=True)

    results, n = [], 0
    if workers <= 1:
        for f in todo:
            rec = tag_one(client, prompt, f, vocab, max_side)
            results.append(rec)
            n += 1
            prog.bump(rec['status'], os.path.basename(f), rec.get('error') or '')
            if on_done:
                on_done(rec, n)
    else:
        with futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(tag_one, client, prompt, f, vocab, max_side): f
                    for f in todo}
            for fu in futures.as_completed(futs):
                f = futs[fu]
                try:
                    rec = fu.result()
                except Exception as e:                    # noqa: BLE001
                    # tag_one 自己吞异常了，走到这里说明是更外层的意外
                    rec = {'source': os.path.abspath(f), 'status': 'failed',
                           'error': '打标线程出错：%s' % str(e)[:200],
                           'free': {d: [] for d in DIMS}}
                    for d in DIMS:
                        rec[d] = []
                results.append(rec)
                n += 1
                prog.bump(rec['status'], os.path.basename(f), rec.get('error') or '')
                if on_done:
                    on_done(rec, n)

    # 统一落盘（也顺带写各自那条 tags/*.json）
    for rec in results:
        try:
            store.save(rec, rec['source'])
        except Exception as e:                            # noqa: BLE001
            rec['error'] = '存盘失败：%s' % str(e)[:200]

    prog.write(force=True)
    s = store.summary()
    s.update({'scanned': total, 'skipped': skipped, 'duplicate': dup,
              'elapsed': round(time.time() - prog.started, 1),
              'model': client.model, 'provider': client.base_url})
    return s
