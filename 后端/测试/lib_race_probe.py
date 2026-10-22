#!/usr/bin/env python3
"""图库并发入库：撞号竞态的复现 / 回归脚本（不参与主流程，手工跑）。

为什么留这个脚本
    2026-10-10 做四方连续接入时，两个任务几乎同时完成、各触发一次后台自动入库，
    真图库里出现了**两对撞号记录**（同 id 两条，指向同一个文件名）——
    后写的把先写的原图**覆盖**了，那张图事实上丢了。
    根因：占号（"号已发出、还没写进索引"）原来挂在图库**实例**上，
    而 `App.reload()` 会 new 一个新实例，新实例里那份占号是空的 → 同一个号发两次。
    已改成落盘的占号表（`library.py` 的 `_holds`），跨实例、跨进程都不再撞。

三个场景
    1 同一实例 3 线程并发     —— 应当通过
    2 入库中途 new 一个实例    —— 应当通过
    3 入库中途换实例 + 精确控窗口 —— 修之前稳定失败，就是线上那个症状

跑法
    $HOME/.workbuddy/binaries/python/versions/3.13.12/bin/python3 \
        后端/测试/lib_race_probe.py
    全部用临时目录，不碰真图库。三个场景全过才退出码 0。
"""
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.library import PatternLibrary  # noqa: E402


def tiny_png(seed):
    """造一张内容不同的小 PNG（图库只看字节，不校验图案）。"""
    w = h = 8
    raw = b''
    for y in range(h):
        raw += b'\x00' + bytes(((x * seed + y) % 256) for x in range(w * 3))

    def chunk(tag, data):
        c = tag + data
        return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)
    ihdr = struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr)
            + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


class SlowTagger:
    """打标签故意慢（真实情况是调一次视觉模型，几秒到十几秒）。"""

    def __init__(self, delay=0.6, gate=None):
        self.delay = delay
        self.gate = gate

    def tag(self, b64, mime):
        if self.gate is not None:
            self.gate.wait(timeout=10)       # 由外部精确放行，不靠 sleep 撞运气
        else:
            time.sleep(self.delay)
        return {"ok": True, "raw": {}}


def _report(label, root, out_ids):
    items = json.load(open(os.path.join(root, 'index.json'), encoding='utf-8'))['items']
    ids = [x['id'] for x in items]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    files = sorted(os.listdir(os.path.join(root, 'orig')))
    print('  ' + label)
    print('    编号 %s ／ 索引 %d 条 ／ 原图文件 %d 个'
          % (out_ids, len(ids), len(files)))
    if dup:
        print('    **撞号**：%s —— 索引 %d 条却只有 %d 个原图文件，少的那张已被覆盖。'
              % (', '.join(dup), len(ids), len(files)))
        return False
    print('    没复现')
    return True


# ---------------------------------------------------------------- 场景 1 / 2
def case_plain(label, n_threads=3, n_each=2, delay=0.6):
    root = tempfile.mkdtemp(prefix='librace_')
    try:
        lib = PatternLibrary(root=root, tagger=SlowTagger(delay))
        got, lock = [], threading.Lock()

        def worker(t):
            for i in range(n_each):
                r = lib.ingest_bytes(tiny_png(t * 10 + i), source='seamless',
                                     name='t%d-%d' % (t, i), tag=True)
                with lock:
                    got.append(r.get('id'))

        ts = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        return _report(label, root, got)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def case_new_instance_midflight(label, n_threads=3, n_each=2, delay=0.6):
    """入库进行中 new 一个实例（模拟 App.reload()）。"""
    root = tempfile.mkdtemp(prefix='librace_')
    try:
        holder = {'lib': PatternLibrary(root=root, tagger=SlowTagger(delay))}
        got, lock = [], threading.Lock()

        def worker(t):
            for i in range(n_each):
                r = holder['lib'].ingest_bytes(tiny_png(t * 10 + i), source='seamless',
                                               name='t%d-%d' % (t, i), tag=True)
                with lock:
                    got.append(r.get('id'))

        ts = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in ts:
            t.start()
        time.sleep(delay * 0.5)
        holder['lib'] = PatternLibrary(root=root, tagger=SlowTagger(delay))
        for t in ts:
            t.join()
        return _report(label, root, got)
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ------------------------------------------------------------------- 场景 3
def case_window_reload(label):
    """确定性复现：A 在实例1 占了号正在打标，此时 reload 换实例，B 再入一张。

    这个窗口不是理论风险 —— 打标签要调视觉模型、几秒钟；
    而且 `library.mutate()` 里 `sync.rev_bump('library')` 用的是硬编码 by，
    绕过了"自己改的不触发自己"的判据，所以**本进程写完图库后下一个请求必然整站 reload**。
    """
    root = tempfile.mkdtemp(prefix='librace3_')
    try:
        gate = threading.Event()
        inst1 = PatternLibrary(root=root, tagger=SlowTagger(gate=gate))
        out = {}

        def a():
            out['a'] = inst1.ingest_bytes(tiny_png(1), source='seamless', name='A', tag=True)

        ta = threading.Thread(target=a)
        ta.start()
        time.sleep(0.3)                       # 让 A 先占上号、卡在打标签里

        inst2 = PatternLibrary(root=root, tagger=SlowTagger(gate=gate))   # ← App.reload() 干的事
        out['b'] = inst2.ingest_bytes(tiny_png(2), source='seamless', name='B', tag=True)

        gate.set()
        ta.join()
        ok = _report(label, root, [out['a'].get('id'), out['b'].get('id')])
        print('      A 拿到 %s ／ B 拿到 %s' % (out['a'].get('id'), out['b'].get('id')))
        return ok
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == '__main__':
    print('=== 场景 1：同一实例，3 线程并发各入 2 张 ===')
    r1 = case_plain('同一实例并发')
    print()
    print('=== 场景 2：入库进行中 new 一个实例 ===')
    r2 = case_new_instance_midflight('中途换实例')
    print()
    print('=== 场景 3：入库进行中换实例 + 精确控窗口（当前失败点）===')
    r3 = case_window_reload('跨实例占号丢失')
    print()
    print('结果：场景1 %s ／ 场景2 %s ／ 场景3 %s'
          % ('通过' if r1 else '失败', '通过' if r2 else '失败', '通过' if r3 else '失败'))
    sys.exit(0 if (r1 and r2 and r3) else 1)
