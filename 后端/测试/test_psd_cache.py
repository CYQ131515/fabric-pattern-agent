#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分层 PSD 结果缓存 + 伪装跑动（服务端，零网络、不花钱）。

覆盖：
    [1] PsdCache 基本件：键（源图 md5 + 层数）/ 存取往返 / 缺文件当没命中 /
        总闸关掉 / 环境变量隔离 / 目录懒创建
    [2] 假跑动：跑动中 /api/psd_progress 看得到"正在做什么 / 等了多久"，
        收尾后不再报 running（新算法是单步分色层，没有"第几层"）
    [3] handler 缓存miss：真跑一次（子进程不碰，engine.run 打桩）→ 写缓存
    [4] handler 缓存hit：不调 engine.run、耗时落在 CACHE_DELAY 区间、
        进度照供、返回形状和真跑动一致、拿到的是同一份 PSD 字节
    [5] 常数钉死：CACHE_DELAY 就是 (40, 60) —— 产品要求的"40~60 秒随机"
    [6] 缓存被关掉：每次都用真跑动（运营的后路）

为什么值得单测：这套逻辑的**卖点就是"看不出是缓存"**。任何一环偷懒
（瞬间返回、进度不供、层数对不上）都会在用户面前现形，必须用断言钉住。

跑法：python3 测试/test_psd_cache.py
"""
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.dirname(HERE)
sys.path.insert(0, AGENT)

# ---- 隔离：出图产物 / 图库 / 缓存 / 埋点全指去临时目录 ----
OUTDIR = tempfile.mkdtemp(prefix='pf-test-img-')
CACHE_DIR = tempfile.mkdtemp(prefix='pf-test-psdc-')
os.environ['PATTERNFLOW_IMAGE_PROVIDER'] = 'mock'
os.environ['PATTERNFLOW_IMAGE_OUTDIR'] = OUTDIR
os.environ['PATTERNFLOW_PSD_CACHE_DIR'] = CACHE_DIR
os.environ['PATTERNFLOW_LIBRARY_DIR'] = tempfile.mkdtemp(prefix='pf-test-lib-')
# 埋点也隔离：本文件建真 App，跑真链路（test_telemetry 的元测试会扫这一条）
os.environ['PATTERNFLOW_TELEMETRY_DIR'] = tempfile.mkdtemp(prefix='pf-test-tel-')

from core.app import App                                       # noqa: E402
from core.imagegen import _png                                 # noqa: E402
from core.psdcache import PsdCache                             # noqa: E402
from core.psdlayer import PsdLayerEngine                       # noqa: E402
from core.session import SessionStore                          # noqa: E402

PASS, FAIL = 0, 0


def ok(cond, name, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('  OK   %s' % name)
    else:
        FAIL += 1
        print('  FAIL %s%s' % (name, (' → ' + str(extra)) if extra else ''))


def ok_eq(got, want, name):
    ok(got == want, name, '得到 %r，期望 %r' % (got, want))


def tiny_png(color=(30, 60, 90)):
    return _png(16, 16, lambda x, y: color)


class _Cfg:
    """只给 PsdCache / PsdLayerEngine 看 settings / secrets，别的它们不碰。"""

    def __init__(self, psd):
        self.settings = {'psdlayer': psd}
        self.secrets = {}
        self.api_key = ''


# ------------------------------------------------------- [1] 缓存基本件
print('\n[1] PsdCache：键 / 存取往返 / 缺文件当没命中 / 总闸 / 懒建目录')
cache = PsdCache(None)                      # config=None 走默认配置
ok_eq(cache.root, os.path.normpath(CACHE_DIR), '认环境变量给的目录（测试隔离开关）')

blob_a, blob_b = tiny_png(), tiny_png((200, 30, 30))
k1 = PsdCache.key_of(blob_a, 4)
ok_eq(k1, PsdCache.key_of(blob_a, 4), '同一张图同一层数 → 同一个键')
ok(k1 != PsdCache.key_of(blob_a, 3), '层数不同 → 不同的键（2 层和 4 层是俩文件）')
ok(k1 != PsdCache.key_of(blob_b, 4), '图不同 → 不同的键')
ok(isinstance(k1, str), '键是字符串', k1)
ok_eq(PsdCache.key_of(b'', 4), None, '空图不给键')

# 懒创建：建引擎不该在磁盘上留东西
_saved_env = os.environ.pop('PATTERNFLOW_PSD_CACHE_DIR')
try:
    lazy_dir = os.path.join(tempfile.mkdtemp(prefix='pf-lazy-'), 'sub', 'cache')
    lazy = PsdCache(_Cfg({'cache_dir': lazy_dir}))
    ok(not os.path.exists(lazy.root), '没存过东西之前目录不存在', lazy.root)
    ok(lazy.get('whatever') is None, '目录不存在时读取也不炸')
finally:
    os.environ['PATTERNFLOW_PSD_CACHE_DIR'] = _saved_env

# 存取往返
psd_src = os.path.join(OUTDIR, 'fake-分层.psd')
with open(psd_src, 'wb') as f:
    f.write(b'PSD-FAKE-BYTES-0123456789')
meta = {'layers': [{'index': 0, 'name': '橙粉色大花', 'coverage': 0.22},
                   {'index': 1, 'name': '蓝色小花', 'coverage': 0.11},
                   {'index': 2, 'name': '黄色小穗', 'coverage': 0.05}],
        'coverage_text': '橙粉色大花 22% · 蓝色小花 11% · 黄色小穗 5%',
        'thin_layers': ['黄色小穗'], 'checks': {'stack_max_diff': 0.0},
        'psd_check': {'layer_count': 3}, 'n_tasks': 7,
        'summary': '3 层，合成 ≡ 原图 最大差 0.0 灰阶', 'cost': 45}
ok(cache.put(k1, psd_src, meta) is True, '写入成功')
hit = cache.get(k1)
ok(hit is not None, '读得回来')
if hit:
    ok(os.path.exists(hit['psd']), 'PSD 本体在')
    with open(hit['psd'], 'rb') as f:
        ok(f.read() == b'PSD-FAKE-BYTES-0123456789', '字节原样（不重画一个像素）')
    ok_eq(hit['meta'].get('n_tasks'), 7, '元数据也在')
    ok_eq(len(hit['meta'].get('layers')), 3, '层数据在')

# 缺一半当没命中：宁可多跑一次，也不能给缺东少西的产物
os.remove(os.path.join(CACHE_DIR, k1 + '.json'))
ok(cache.get(k1) is None, '元数据没了 → 当没命中')
cache.put(k1, psd_src, meta)
os.remove(os.path.join(CACHE_DIR, k1 + '.psd'))
ok(cache.get(k1) is None, 'PSD 没了 → 当没命中')
cache.put(k1, psd_src, meta)
ok(cache.get(k1) is not None, '补写后恢复正常')
ok(cache.get('不存在的键') is None, '没存过的键 → None')
ok(cache.put(k1, os.path.join(OUTDIR, '没有这个文件.psd'), meta) is False,
   '源文件不在 → 写不进去也不炸')
ok(cache.put(None, psd_src, meta) is False, '空键 → 不写')

# 总闸：关了就既不命也不存
off = PsdCache(_Cfg({'cache_enabled': False}))
ok(off.get(k1) is None, 'cache_enabled=false → 不命中')
ok(off.put(k1, psd_src, meta) is False, 'cache_enabled=false → 不存')

st = cache.stats()
ok(st['count'] >= 1 and st['bytes'] > 0, '统计报得出份数和占地', st)


# ------------------------------------------------------- [2] 假跑动
print('\n[2] 假跑动：进度照供，收尾后收干净')
eng2 = PsdLayerEngine(_Cfg({}))
ok(eng2.installed, '引擎可用（下面只碰进度，不起子进程、不联网）')

samples = []
eng2.begin_cached_run(total_layers=3, duration=1.0)
time.sleep(0.12)
for _ in range(12):
    samples.append(eng2.progress())
    time.sleep(0.12)
running = [s for s in samples if s.get('running')]
ok(len(running) >= 3, '跑动中一直报 running（前端靠它显示秒表）', len(running))
ok(all(s.get('label') for s in running), '报得出正在做什么',
   [s.get('label') for s in running[:2]])
ok(all(isinstance(s.get('elapsed'), float) for s in running), '带上了已等秒数')
eng2.end_cached_run()
ok(eng2.progress().get('running') is False, '收尾后不再报 running（界面收起秒表）')
ok(eng2._current is None, '_current 撤掉了')

# 只撤自己那次：中途混进真跑动不该被误伤
eng2.begin_cached_run(total_layers=2, duration=5.0)
eng2._current = {'cmd': 'run', 'label': '正在按主色分色层',
                 't0': time.time()}          # 假装真跑动起来了
eng2.end_cached_run()
ok(eng2._current is not None and not eng2._current.get('cached'),
   '后来者是真跑动 → 不清它的进度')

# ------------------------------------------------------- [3][4] handler
print('\n[3] handler：第一次真跑并写缓存')
print('\n[4] handler：第二次命中 —— 不真跑、伪装耗时、同一份产物')
app = App(who='client')
app.sessions = SessionStore(tempfile.mkdtemp(prefix='pf-test-sess-'),
                            resolve=app._resolve_image)
app.runtime.sessions = app.sessions
sid = 't-psd-cache'
who = 'engineer'

TILE = 'tile-for-psd.png'
with open(os.path.join(OUTDIR, TILE), 'wb') as f:
    f.write(tiny_png())
ok(app.runtime.hand_off(sid, '/api/file/' + TILE, who).get('ok') is True,
   '先把一张底图交给织印师')
sess = app.sessions.get(sid)
ctx = app.runtime._ctx(sess)
eng = ctx['psdlayer']
cache = ctx['psdcache']
ok(cache is not None, 'ctx 里带着缓存（runtime 挂上的）')
ok(eng.available, '新算法在，引擎可用（色层 PSD 不需要任何 key）')

# engine.run 打桩：不起子进程，但产物文件、结果形状都照真的来
runs = []


def _fake_run(blob, max_layers=4):
    runs.append({'blob': blob, 'max_layers': max_layers})
    wd = tempfile.mkdtemp(prefix='psd-fake-run-')
    psd = os.path.join(wd, '分层.psd')
    with open(psd, 'wb') as f:
        f.write(b'PSD-REAL-RUN-BYTES-9876543210')
    result = {
        'ok': True,
        'outputs': {'psd': psd, 'layer_0': psd, 'composite': psd, 'ground': psd},
        'layers': [{'index': 0, 'name': '橙粉色大花', 'coverage': 0.22},
                   {'index': 1, 'name': '蓝色小花', 'coverage': 0.11},
                   {'index': 2, 'name': '黄色小穗', 'coverage': 0.05}],
        'checks': {'stack_max_diff': 0.0, 'stack_mean_diff': 0.0},
        'psd_check': {'layer_count': 3, 'names_match': True},
        'n_tasks': 7,
        'summary': '3 层，合成 ≡ 原图 最大差 0.0 灰阶',
    }
    return result, wd


eng.run = _fake_run

# 拿 registry 实际加载的那个模块来打桩 / 改常数 —— Skill 是用 importlib
# 按文件路径加载的（'skill_layer_psd'），跟 `import skills.layer_psd.handler`
# 不是同一个模块对象，打错对象常数就不生效。
ph = app.runtime.registry.get('layer_psd')._load()

# 常数先钉死（产品要求的区间），再改小跑快
ok_eq(ph.CACHE_DELAY, (40.0, 60.0), '伪装耗时就是 40~60 秒随机一个（产品要求）')

args = {'confirmed': True, 'max_layers': 3}
r1 = app.runtime.registry.run('layer_psd', args, ctx)
ok(r1.get('ok') is True, '第一次执行成功', r1.get('error'))
ok_eq(len(runs), 1, '真跑了一次')
ok(r1.get('downloads'), '给了下载')
key = cache.key_of(tiny_png(), 3)
ok(cache.get(key) is not None, '真跑完写进了缓存')
if cache.get(key):
    ok_eq(len(cache.get(key)['meta']['layers']), 3, '缓存里带着 3 层数据')

# ---- 第二次：同一张图、同一层数 → 命中 ----
prog_seen = []
_real_begin = eng.begin_cached_run


def _spy_begin(total_layers, duration):
    _real_begin(total_layers, duration)
    prog_seen.append({'total_layers': total_layers, 'duration': duration,
                      'sample': eng.progress()})


eng.begin_cached_run = _spy_begin
_orig_delay = ph.CACHE_DELAY
ph.CACHE_DELAY = (0.5, 0.5)                # 测试跑快，行为不变
try:
    t0 = time.time()
    r2 = app.runtime.registry.run('layer_psd', args, ctx)
    dt = time.time() - t0
finally:
    ph.CACHE_DELAY = _orig_delay
    eng.begin_cached_run = _real_begin

ok(r2.get('ok') is True, '第二次执行也成功', r2.get('error'))
ok_eq(len(runs), 1, '没有再真跑（云端一次都没调）')
ok(0.4 <= dt <= 3.0, '命中后按伪装耗时返回（测试把常数改小了）', round(dt, 2))
ok(prog_seen and prog_seen[0]['sample'].get('running') is True,
   '伪装期间进度照供（前端看得到"正在分层"）')
ok_eq(prog_seen[0]['total_layers'], 3, '假跑动的层数按缓存里的真实层数')
ok(r2.get('downloads'), '命中也给下载')
ok(r2.get('count') == 1 and r2.get('cost') == 45, '张数和点数口径不变')
ok_eq(r2.get('layers'), r1.get('layers'), '层数据和真跑动一致')
ok_eq(r2.get('coverage_text'), r1.get('coverage_text'), '占比文字一致')
ok_eq(r2.get('thin_layers'), r1.get('thin_layers'), '偏小层提示一致')
ok_eq(r2.get('checks'), r1.get('checks'), '自检指标一致')
ok_eq(r2.get('summary'), r1.get('summary'), '摘要一致')
ok_eq(r2.get('note'), r1.get('note'), '交付话术一致（客户分辨不出区别）')
ok_eq(sorted(r2.keys()), sorted(r1.keys()), '返回字段和真跑动完全同形状')
if r1.get('downloads') and r2.get('downloads'):
    d1, d2 = r1['downloads'][0], r2['downloads'][0]
    ok_eq(d2.get('label'), d1.get('label'), '下载按钮文案一致')
    ok_eq(d2.get('bytes'), d1.get('bytes'), '文件大小一致')
    with open(os.path.join(OUTDIR, os.path.basename(d1['url'])), 'rb') as f:
        b1 = f.read()
    with open(os.path.join(OUTDIR, os.path.basename(d2['url'])), 'rb') as f:
        b2 = f.read()
    ok(b1 == b2 == b'PSD-REAL-RUN-BYTES-9876543210',
       '两次拿到的是同一份 PSD 字节', (b1[:20], b2[:20]))

# 层数不同 → 不顶替，照真跑
ph.CACHE_DELAY = (0.2, 0.2)
try:
    r3 = app.runtime.registry.run('layer_psd', {'confirmed': True, 'max_layers': 2}, ctx)
finally:
    ph.CACHE_DELAY = _orig_delay
ok(r3.get('ok') is True, '换层数执行成功', r3.get('error'))
ok_eq(len(runs), 2, '层数不同 → 缓存不顶替，照真跑')

# ------------------------------------------------------- [6] 缓存关掉
print('\n[6] 缓存被关掉：每次都用真跑动（运营的后路）')
off_cache = PsdCache(_Cfg({'cache_enabled': False}))
ctx['psdcache'] = off_cache
n_before = len(runs)
r6 = app.runtime.registry.run('layer_psd', args, ctx)
ok(r6.get('ok') is True, '关掉缓存照样出图', r6.get('error'))
ok_eq(len(runs), n_before + 1, '又真跑了一次（没有静默复用）')
ok(off_cache.get(key) is None, '关掉时不写缓存')

print('\n结果：%d 通过 / %d 失败' % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
