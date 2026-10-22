#!/usr/bin/env python3

import os
import sys

# 2026-10-20：描稿/四方连续/分层 PSD 已换成新算法（算法/描稿-四方连续-psd分层），
# 这条用例针对的是旧引擎（RunningHub SAM3.1、纯 Python 子进程、代理摘除、
# 逐层进度与逐层超时），这些内部实现已不存在。保留原文作历史参考；
# 需要对照旧行为时设置 PATTERNFLOW_LEGACY_TESTS=1 再跑。
if os.environ.get('PATTERNFLOW_LEGACY_TESTS') != '1':
    print('（旧引擎回归已归档，跳过。设置 PATTERNFLOW_LEGACY_TESTS=1 可强制运行）')
    sys.exit(0)

# -*- coding: utf-8 -*-
"""界面上「一跳两件事」的按钮 + 死代理体检（服务端，不连网、不花钱）。

    [1] 死代理体检：环境里挂着一个没在跑的本地代理时，启动就把变量摘掉
    [2] 界面直通车提案（propose）：只提案、不执行、不扣点，且只落进目标角色那一槽
    [3] 四方连续的成品图 → 「生成 PSD 文件」：交接给织印师之后，分层提案能读到它
    [4] 两个超时的不变式：进程级上限 > 单任务超时 × 单层候选数
    [5] 分层进度：跑动中查得到「第几层 / 等了多久」，跑完就不再报 running
    [6] 库图「交给织印师」：走读库那条路（不能按地址找），到的两个选项都能提案

跑法：python3 测试/test_v08_psd_btn.py
生图走 mock provider，看图那一步被打桩，子进程被替换掉，所以全程零网络、不花钱。

为什么 [2] 值得单独测：这个按钮不走模型，是**确定性**的。既然绕开了模型，
"不提方案就执行"这种事故就全靠 `propose` 自己守住闸门，不能只靠 AI 自觉。

为什么 [4] 值得单独测：这两个超时曾经都是 900。配置写错不会有任何报错，
只会表现为"用户盯着转圈满 15 分钟然后被 kill" —— 静默失效，必须用断言钉住。
"""
import json
import os
import socket
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.dirname(HERE)
sys.path.insert(0, AGENT)

OUTDIR = tempfile.mkdtemp(prefix='pf-test-img-')
os.environ['PATTERNFLOW_IMAGE_PROVIDER'] = 'mock'
# 出图产物挪去临时目录，别堆进项目的 generated/
os.environ['PATTERNFLOW_IMAGE_OUTDIR'] = OUTDIR
# 图库目录也挪走：出图会自动入库，别把测试产物混进真图库
os.environ['PATTERNFLOW_LIBRARY_DIR'] = tempfile.mkdtemp(prefix='pf-test-lib-')
# 埋点也挪走：跑一次回归会记下几十条出图/提案，混进真看板就成了假数据
os.environ['PATTERNFLOW_TELEMETRY_DIR'] = tempfile.mkdtemp(prefix='pf-test-tel-')

from core import netenv                                   # noqa: E402
from core.app import App                                  # noqa: E402
from core.session import SessionStore                      # noqa: E402

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


# 一张最小的合法 PNG（8x8 纯色），给 hand_off / 分层当底图用
def _tiny_png():
    import struct
    import zlib

    def chunk(tag, data):
        return (struct.pack('>I', len(data)) + tag + data
                + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff))

    ihdr = struct.pack('>IIBBBBB', 8, 8, 8, 2, 0, 0, 0)
    raw = b''.join(b'\x00' + b'\x30\x60\x90' * 8 for _ in range(8))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr)
            + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


TILE = 'tile-for-psd.png'
with open(os.path.join(OUTDIR, TILE), 'wb') as f:
    f.write(_tiny_png())


# ---------------------------------------------------------------- [1] 死代理
print('\n[1] 死代理体检')
_SAVED = {k: os.environ.get(k) for k in netenv.PROXY_VARS}


def _restore():
    for k, v in _SAVED.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# 一个"活着"的本地端口
srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.bind(('127.0.0.1', 0))
srv.listen(1)
ALIVE = 'http://127.0.0.1:%d/' % srv.getsockname()[1]

# 一个**确定没人监听**的本地端口：先绑住拿到号，再立刻关掉，端口就空着了。
# 别再写死 1082 —— 那台机器上代理软件真的会去听它，用例就随环境飘。
_probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
_probe.bind(('127.0.0.1', 0))
DEAD = 'http://127.0.0.1:%d/' % _probe.getsockname()[1]
_probe.close()

for k in netenv.PROXY_VARS:
    os.environ.pop(k, None)
os.environ['HTTP_PROXY'] = DEAD
os.environ['HTTPS_PROXY'] = DEAD
os.environ['no_proxy'] = 'localhost'
dropped = netenv.sanitize_proxies(verbose=False)
ok(set(dropped) == {'HTTP_PROXY', 'HTTPS_PROXY'}, '没在跑的本地代理被摘掉', dropped)
ok(os.environ.get('HTTP_PROXY') is None, '变量真的从环境里拿走了（后续请求才会直连）')
ok(os.environ.get('no_proxy') == 'localhost', 'NO_PROXY 这类不碰（不是代理地址）')

for k in netenv.PROXY_VARS:
    os.environ.pop(k, None)
os.environ['HTTPS_PROXY'] = ALIVE
ok_eq(netenv.sanitize_proxies(verbose=False), [], '活着的本地代理保留')
ok(os.environ.get('HTTPS_PROXY') == ALIVE, '没被动过')

for k in netenv.PROXY_VARS:
    os.environ.pop(k, None)
os.environ['HTTPS_PROXY'] = 'http://10.0.0.9:8080/'    # 远端
ok_eq(netenv.sanitize_proxies(verbose=False), [],
      '远端代理不猜（"连不上"和"有点慢"分不清，不替用户拍板）')
_restore()
srv.close()

# ------------------------------------------------------- [2] 直通车提案
print('\n[2] 界面直通车：propose 只提案，不执行、不扣点')
app = App(who='client')
# 会话也挪去临时目录：别把回归用的 session.jsonl 落进项目的数据目录
# （App 建的时候已经用真目录建了一个 store，这里整个换掉）
app.sessions = SessionStore(tempfile.mkdtemp(prefix='pf-test-sess-'),
                            resolve=app._resolve_image)
app.runtime.sessions = app.sessions
sid = 't-v08-btn'
who = 'engineer'
ok(app.runtime.hand_off(sid, '/api/file/' + TILE, who).get('ok') is True,
   '先把一张成品图交给织印师当底图')

sess = app.sessions.get(sid)
eng = app.runtime._ctx(sess).get('psdlayer')
ok(eng is not None and eng.available, 'PSD 分层引擎装上了')
# 看图那一步要联网调视觉模型，这里打桩 —— 这个用例验的是提案这一层
eng.plan = lambda blob, max_layers=4: {
    'plan': {'layers': [{'name': '橙粉色大花'}, {'name': '蓝色小花'},
                        {'name': '黄色小穗'}]}}

ran = []
_real_run = app.runtime.registry.run


def _spy(name, args, ctx):
    ran.append((name, bool(args.get('confirmed'))))
    return _real_run(name, args, ctx)


app.runtime.registry.run = _spy
credits_before = 0
r = app.runtime.propose(sid, 'layer_psd', {}, who)
app.runtime.registry.run = _real_run

ok(r.get('ok') is True, '提案成功', r.get('error'))
p = r.get('pending') or {}
ok_eq(p.get('action'), 'layer_psd', '待确认方案挂在 layer_psd 上')
ok_eq(p.get('cost'), 45, '报价是 45 点')
ok_eq(p.get('count'), 1, '就是一张（分层不会有第二张）')
ok('3 层' in (p.get('summary') or ''), '方案里写清了几层', p.get('summary'))
ok('橙粉色大花' in (p.get('summary') or ''), '也说清了是哪几层', p.get('summary'))
ok(ran and all(c is False for _, c in ran),
   '技能是**按未确认**调起来的（这就等于闸门：提案阶段绝不执行）',
   ran)
ok_eq(credits_before, 0, '提案不动点数')

acts = {k: (v or {}).get('action') for k, v in (sess.pending_actions or {}).items()}
ok_eq(acts.get('engineer'), 'layer_psd', '方案落进织印师那一槽')
ok(acts.get('designer') is None, '花型师那一槽没被污染（跨机器人记忆的口径）')

r2 = app.runtime.propose(sid, 'layer_psd', {}, 'designer')
ok(r2.get('ok') is False and '底图' in (r2.get('error') or ''),
   '换个没图的角色来点 → 说人话，让他先给一张', r2.get('error'))

bad = app.runtime.propose(sid, 'nope_skill', {}, who)
ok(bad.get('ok') is False, '不认识的技能直接拒掉', bad.get('error'))

# ------------------------------------------------- [3] 交接之后分层读得到
print('\n[3] 交接 → 分层提案读得到那张图')
sess.act_as('designer')
ok(not sess.last_image, '花型师那条线上没有这张图（记忆是按角色分槽的）')
sess.act_as('engineer')
ok(bool((sess.last_image or {}).get('b64')), '织印师那条线上有')
ok_eq((sess.last_image or {}).get('origin'), 'handoff', '来路记的是"用户交过来的"')

# --------------------------------------------- [4] 两个超时的不变式
print('\n[4] 超时不变式：进程级上限 > 单任务超时 × 单层候选数')
from core.psdlayer import PsdLayerEngine                     # noqa: E402


class _Cfg:
    """只给 PsdLayerEngine 看 settings / secrets / api_key，别的它不碰。"""

    def __init__(self, psd):
        self.settings = {'psdlayer': psd, 'llm': {}}
        self.secrets = {}
        self.api_key = ''


# 真机上的那份配置（settings.json + DEFAULT_CONF 合并的结果）
ok(eng.task_timeout == 180, '单任务超时是 180 秒（正常一个任务约 30 秒）',
   eng.task_timeout)
ok(eng.timeout_run > eng.task_timeout * (eng.max_retry + 1),
   '进程级上限大于"一层把候选提示词都试一遍"的最坏耗时',
   'timeout_run=%s 候选数=%s' % (eng.timeout_run, eng.max_retry + 1))
ok(eng.timeout_run_raised is False, '默认配置本身就合规，不需要兜底抬升')
_eng_desc = eng.describe()
ok_eq(_eng_desc.get('task_timeout'), eng.task_timeout, 'health 里报出了单任务超时')
ok_eq(_eng_desc.get('timeout_run'), eng.timeout_run, 'health 里也报出了进程级上限')

# 把配置写坏（两边都 900 —— 就是真踩过的那个值），必须被兜底抬到安全值
_bad = PsdLayerEngine(_Cfg({'task_timeout': 900, 'max_retry': 3, 'timeout_run': 900}))
ok(_bad.timeout_run_raised is True, '写坏的配置被识别出来', _bad.timeout_run_raised)
ok_eq(_bad.timeout_run, 900 * 4 + 60, '进程级上限被抬到安全值（单任务×候选数+60）')
ok(_bad.timeout_run > _bad.task_timeout * (_bad.max_retry + 1),
   '抬过之后不变式成立 —— 客户端自己的超时才有机会生效')

# --------------------------------------------- [5] 分层进度看得见
print('\n[5] 分层进度：跑动中查得到，跑完不再报 running')
if not eng.python:
    # 没装 venv 的机器上也别让用例白挂：只验参数与进度这条链路
    eng.python = sys.executable
ok(eng.available, '分层引擎可用（会起子进程，但下面把起进程那步换掉了）')

import core.psdlayer as _P                                 # noqa: E402

_captured = {}
_real_spawn = _P.spawn


class _FakeProc:
    """冒充一个 Popen 对象：调用方只用到 poll / communicate / pid。"""
    pid = 0

    def __init__(self):
        self.returncode = 0

    def poll(self):
        return self.returncode

    def communicate(self, timeout=None):
        return b'', b''


def _fake_spawn(argv, **kw):
    """冒充算法子进程：把进度文件和结果文件按约定写出来，别的什么都不做。

    ⚠️ 桩的是 `core.psdlayer.spawn`（= core/proc.py 里那个起进程的封装），
    不再是 `subprocess.run` —— 引擎为了能在用户点「终止」时把子进程
    **连它整组一起杀掉**，已经从 run 换成了 Popen（见 core/proc.py 顶部）。
    桩错地方的话，这里会真的去起算法 venv（跑得动但很慢，且不再是单测）。
    """
    _captured['argv'] = list(argv)
    wd = os.path.dirname(argv[argv.index('--json-out') + 1])
    with open(os.path.join(wd, 'progress.json'), 'w', encoding='utf-8') as f:
        json.dump({'phase': 'layer', 'index': 2, 'total': 3,
                   'name': '蓝色小花'}, f, ensure_ascii=False)
    _captured['during'] = eng.progress()      # ← 就在"跑动中"查一次
    with open(argv[argv.index('--json-out') + 1], 'w', encoding='utf-8') as f:
        json.dump({'ok': True, 'layers': [], 'outputs': {}}, f)
    return _FakeProc()


_P.spawn = _fake_spawn
try:
    _res, _wd = eng.run(_tiny_png())
finally:
    _P.spawn = _real_spawn

_argv = _captured.get('argv') or []
ok('--task-timeout' in _argv, '单任务超时传给了算法子进程')
ok_eq(_argv[_argv.index('--task-timeout') + 1] if '--task-timeout' in _argv else None,
      str(eng.task_timeout), '传的就是引擎里那个值')
ok('--progress-out' in _argv, '进度落盘位置也传下去了')
if '--progress-out' in _argv:
    ok_eq(os.path.dirname(_argv[_argv.index('--progress-out') + 1]),
          _argv[_argv.index('--json-out') + 1].rsplit(os.sep, 1)[0],
          '进度文件跟这次调用的结果文件同目录（一次调用一份，互不串台）')

_during = _captured.get('during') or {}
ok(_during.get('running') is True, '跑动中 running=True')
ok_eq(_during.get('index'), 2, '读得到"正在跑第几层"')
ok_eq(_during.get('total'), 3, '也读得到总层数')
ok(isinstance(_during.get('elapsed'), float) and _during['elapsed'] >= 0,
   '带上了已经等了多久（秒）', _during.get('elapsed'))
ok_eq(_during.get('name'), '蓝色小花', '层名也带出来了（界面能直接显示）')

ok_eq(eng.progress().get('running'), False, '跑完就不再报 running（界面据此收起秒表）')

# 上面把 subprocess.run 换掉了，所以参数/进度这条链路的**算法侧**还没被真跑过。
# 真起一次算法自己的 venv（只到"读图失败"就返回，不联网、不花钱）把契约钉死：
# 参数拼错、argparse 不认，都会在这里现形（argparse 认不出会直接退 2）。
import subprocess as _sp                                  # noqa: E402

_venv_py = eng._auto_python()
if not _venv_py:
    print('  --   没装 .venv，跳过算法侧 CLI / 进度落盘的实跑')
else:
    _tmpd = tempfile.mkdtemp(prefix='pf-test-psd-')
    _r = _sp.run([_venv_py, '-m', 'rhpsd.service', 'run',
                  '--input', os.path.join(OUTDIR, 'no-such-file.png'),
                  '--outdir', os.path.join(_tmpd, 'out'),
                  '--json-out', os.path.join(_tmpd, 'result.json'),
                  '--task-timeout', str(eng.task_timeout),
                  '--progress-out', os.path.join(_tmpd, 'progress.json'),
                  '--max-layers', '4', '--max-side', '2048'],
                 cwd=eng.root, capture_output=True, text=True, timeout=120)
    ok_eq(_r.returncode, 0,
          '算法侧 CLI 认这两个新参数（不认的话 argparse 会退 2）')
    _read_back = None
    if os.path.exists(os.path.join(_tmpd, 'result.json')):
        _read_back = json.load(open(os.path.join(_tmpd, 'result.json'),
                                    encoding='utf-8'))
    ok(_read_back is not None and _read_back.get('ok') is False,
       '读图失败时也老老实实写 {ok:false}（恒有 ok 字段的老契约没破）',
       _read_back)

    # 进度文件：算法每层原子写一次，读方永远看不到半截
    _code = ("import os,sys; sys.path.insert(0,'.');"
             "from rhpsd.pipeline import Config, _progress;"
             "p=os.path.join(sys.argv[1],'progress.json');"
             "_progress(Config(progress_out=p), phase='layer',"
             " index=2, total=3, name='蓝色小花');"
             "sys.stdout.write(open(p,encoding='utf-8').read())")
    _rp = _sp.run([_venv_py, '-c', _code, _tmpd],
                  cwd=eng.root, capture_output=True, text=True, timeout=120)
    ok_eq(_rp.returncode, 0, '算法侧能把进度写出来')
    try:
        _pd = json.loads(_rp.stdout or '{}')
    except ValueError:
        _pd = {}
    ok_eq(_pd.get('index'), 2, '写出的进度带层号')
    ok_eq(_pd.get('total'), 3, '也带总层数')
    ok(isinstance(_pd.get('ts'), float), '带上时间戳（读方据此判新旧）', _pd.get('ts'))
    ok(not os.path.exists(os.path.join(_tmpd, 'progress.json.tmp')),
       '临时文件已改名收走 —— 原子替换，读方看不到半截文件')

# ------------------------- [6] 库图交给织印师 → 两个选项都能提案
print('\n[6] 库图「交给织印师」：读库落到织印师那一槽，两条提案都守闸门')
# 前端那张花型卡上的「交给织印师」对库图走的是 pick_library：
# 服务端自己读库，**不按地址找** —— 前端手上只有 /api/library/file/… 这个下载
# 地址，它不是磁盘上的生成物，按地址找必然找不到（换成 hand_off 就静默失败）。
_ing = app.library.ingest_bytes(_tiny_png(), source='import', name='青花玫瑰园',
                                tag=False, check_pattern=False)
ok(_ing.get('ok') is True and bool(_ing.get('id')),
   '往（临时）库里放一张真图当素材', _ing.get('error'))
PID = _ing.get('id')

SID2 = 't-v08-lib'
sess2 = app.sessions.get(SID2)
sess2.act_as('designer')
_r = app.runtime.pick_library(SID2, PID, 'engineer')
ok(_r.get('ok') is True, '库图交得过去（服务端读库，前端不用先下再传）', _r.get('error'))
ok(bool(_r.get('url')), '回传了可显示的地址（界面上要把这张图摆出来）', _r.get('url'))
sess2.act_as('engineer')
ok(bool((sess2.last_image or {}).get('b64')), '织印师那一槽拿到图了')
ok_eq((sess2.last_image or {}).get('origin'), 'library', '来路记的是"从库里选的"')
sess2.act_as('designer')
ok(not sess2.last_image, '花型师那一槽没被污染（用户点的是"交给织印师"）')
sess2.act_as('engineer')

se = app.runtime._ctx(sess2).get('seamless')
ok(se is not None and se.installed, '四方连续引擎在这台机器上是装好的')
# 体检那一步要起算法子进程，这里打桩 —— 本用例验的是提案这一层
_real_probe = se.probe
se.probe = lambda blob, ext='.png': {'summary': '图幅 8×8 · 地比例 12%，留白偏少',
                                     'route_hint': 'model', 'warnings': []}
try:
    _r3 = app.runtime.propose(SID2, 'seamless_pattern', {}, 'engineer')
finally:
    se.probe = _real_probe
ok(_r3.get('ok') is True, '库图能提「四方连续」方案', _r3.get('error'))
_p3 = _r3.get('pending') or {}
ok_eq(_p3.get('action'), 'seamless_pattern', '方案挂在 seamless_pattern 上（前端照着它选卡片）')
ok_eq(_p3.get('cost'), se.cost, '报价用的是引擎里那个点数')
ok_eq(_p3.get('cost'), 30, '也就是 30 点（和卡片上的口径一致）')
ok('四方连续' in (_p3.get('summary') or ''), '方案里写明了做的是四方连续', _p3.get('summary'))
ok(bool(_p3.get('probe')), '体检结论也带上了（卡片上要摆"输入体检"那行）', _p3.get('probe'))
ok(_p3.get('count') == 1, '就是一张（四方连续是确定性产物，不是出几张挑一张）')
_acts2 = {k: (v or {}).get('action') for k, v in (sess2.pending_actions or {}).items()}
ok_eq(_acts2.get('engineer'), 'seamless_pattern', '落进织印师那一槽')
ok(_acts2.get('designer') is None, '花型师那一槽没被污染')

# 库里没有的图：给一句人话，别把 traceback 甩到界面上
_bad = app.runtime.pick_library(SID2, 'PF-LIB-不存在', 'engineer')
ok(_bad.get('ok') is False and '库里没' in (_bad.get('error') or ''),
   '库里没有这张图时说人话', _bad.get('error'))

print('\n结果：%d 通过 / %d 失败' % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
