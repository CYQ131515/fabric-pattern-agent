#!/usr/bin/env python3
"""PSD 分层「半天没有响应」的修复回归。

跑法：python3 测试/test_v11_psd_wait.py

用户报的现象
    点「生成 PSD 文件」，界面停在
        ⟳ 正在看图、定分层方案…  已等 1:11   [终止]
    整整一分多钟没有任何新信息，看着像卡死。

查下来是三条**互相独立**的原因叠在一起（实测复现到 1:11 这个量级）：
    ① 算法侧 analyze.reachable() 串行探 8 家预设网关、每家超时 8 秒。
       preflight 一旦失败（网络抖一下就够了），就要连着干等 8×8=64 秒
       才吐一句话 —— 而这段时间**一个进度点都不写**。
    ② cmd_plan 看图失败会 _fail 掉**整个提案**，可它自己的话术里却写着
       "可以直接执行，会按默认单层来分"，自相矛盾。
    ③ 前端在提案阶段不轮询进度（proposeForEngineer 没传 psd:true），
       秒表旁边那行字永远是空的；而终止只 abort 了前端 fetch，
       服务端子进程照跑 —— 用户以为停了，云端还在烧。

这个用例把四条都钉住：
    [1] core/proc.py  —— 杀进程要杀**整组**，不能只杀最外面那层
    [2] 引擎 cancel() —— 用户点终止，子进程真的死、不再报 running
    [3] 超时话术按阶段分开 —— 提案超时不能甩"分层跑超时了"
    [4] plan() 有进度，progress() 报得出 cmd / label
    [5] runtime.cancel_run() 把两家引擎都叫上
    [6] 服务端确实挂出了 cancel_run 这条路由
    [7] 算法侧（真 venv）：探活并发；看图失败仍照常给方案
"""

import os
import sys

# 2026-10-20：描稿/四方连续/分层 PSD 已换成新算法（算法/描稿-四方连续-psd分层），
# 这条用例针对的是旧引擎（RunningHub SAM3.1、纯 Python 子进程、代理摘除、
# 逐层进度与逐层超时），这些内部实现已不存在。保留原文作历史参考；
# 需要对照旧行为时设置 PATTERNFLOW_LEGACY_TESTS=1 再跑。
if os.environ.get('PATTERNFLOW_LEGACY_TESTS') != '1':
    print('（旧引擎回归已归档，跳过。设置 PATTERNFLOW_LEGACY_TESTS=1 可强制运行）')
    sys.exit(0)

import json
import os
import subprocess
import sys
import time
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
ROOT = os.path.dirname(BACKEND)
ALGO = os.path.join(ROOT, '算法', 'psd分层')

sys.path.insert(0, BACKEND)

import core.psdlayer as _P                                  # noqa: E402
from core.psdlayer import PsdLayerEngine, PsdLayerError     # noqa: E402
from core.proc import kill_tree, spawn                      # noqa: E402

PASS = FAIL = 0


def ok(cond, label, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('  ✓ ' + label)
    else:
        FAIL += 1
        print('  ✗ ' + label + ('  → %s' % (extra,) if extra else ''))


def ok_eq(got, want, label):
    ok(got == want, label, '实际 %r，期望 %r' % (got, want))


class StubCfg:
    """只给 PsdLayerEngine 看 settings / secrets / api_key，别的它不碰。"""
    settings = {'psdlayer': {}, 'llm': {}}
    secrets = {'runninghub_api_key': 'stub-key'}
    api_key = 'stub-vlm-key'


def engine():
    return PsdLayerEngine(StubCfg())


_TINY_PNG = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00'
             b'\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc'
             b'\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82')

eng = engine()
_REAL_SPAWN = spawn                # 记住真实现，用例里要反复换桩

# ============================================= [1] 杀进程要杀整组
print('\n[1] core/proc.py：终止要把子进程连它的后代一起收掉')
_outer = spawn(['/bin/sh', '-c', 'sleep 30 & sleep 30'])
time.sleep(0.35)
ok(_outer.poll() is None, '子进程起来了、还活着')
_killed = kill_tree(_outer)
time.sleep(0.25)
ok(_killed is True, 'kill_tree 承认发过信号')
ok(_outer.poll() is not None, '子进程死了 —— 不再占着 CPU、不再往云端发请求')
ok(kill_tree(_outer) is False, '已经死掉的进程再杀一次是安全的空操作（不抛异常）')

# ============================================= [2] 引擎 cancel()
print('\n[2] 引擎 cancel()：用户点终止，服务端这次真的停')
eng2 = engine()
ok(eng2.available, '分层引擎在这台机器上可用（.venv 与算法代码都在）')

_started = {}
_P.spawn = lambda argv, cwd=None, env=None: _started.__setitem__(
    'proc', subprocess.Popen(['/bin/sh', '-c', 'sleep 60'], start_new_session=True)
) or _started['proc']

_res = {}


def _work():
    try:
        eng2.plan(_TINY_PNG)
        _res['err'] = None
    except Exception as e:                                  # noqa: BLE001
        _res['err'] = e


_t = threading.Thread(target=_work, daemon=True)
_t.start()
for _ in range(300):                       # 等它进入"跑动中"
    if eng2.progress().get('running'):
        break
    time.sleep(0.02)
ok(eng2.progress().get('running') is True, '跑动中 progress() 报 running=True')
ok(eng2.progress().get('cmd') == 'plan', '进度里带着这次跑的是哪个阶段', eng2.progress().get('cmd'))
ok_eq(eng2.cancel(), True, 'cancel() 承认掐掉了一次')
_t.join(timeout=10)
ok(not _t.is_alive(), '被掐之后 plan() 立刻返回，不再干等到超时')
ok(isinstance(_res.get('err'), PsdLayerError) and '停掉' in str(_res.get('err')),
   '抛的是"这次已经停掉了"（不是超时、不是网关错误）', _res.get('err'))
time.sleep(0.2)
ok(_started['proc'].poll() is not None,
   '算法子进程真的死了 —— 终止不是只断前端那条 fetch')
ok(eng2.progress().get('running') is False, '跑完之后不再报 running（界面据此收起秒表）')
ok_eq(eng2.cancel(), False, '没有任务在跑时 cancel() 回 False（不是错误）')

_P.spawn = _REAL_SPAWN               # 还原真实现

print('\n[2b] 缓存命中的"伪装跑动"也要能被终止')
_eng2b = engine()
_eng2b.begin_cached_run(total_layers=3, duration=30)
_pb = _eng2b.progress()
ok(_pb.get('running') is True, '假跑动期间同样报 running（和真跑动看不出区别）')
ok('cached' not in _pb, '进度里不暴露"这是缓存复用"')
_wait_result = []
_t0 = time.time()


def _waiter():
    _wait_result.append(_eng2b.wait_or_stop(30))


_wt = threading.Thread(target=_waiter, daemon=True)
_wt.start()
time.sleep(0.2)
ok(_eng2b.cancel() is True, '缓存跑动也算掐掉了（返回 True）')
_wt.join(timeout=5)
ok(_wait_result == [True], '等待立刻被打断，不是睡满 30 秒', _wait_result)
ok(time.time() - _t0 < 5, '确实很快就返回了', '%.2fs' % (time.time() - _t0))
_eng2b.end_cached_run()
ok(_eng2b.progress().get('running') is False,
   '收工后不再报 running —— 界面不会看到"终止了却还在跑"')

# ============================================= [3] 超时话术分阶段
print('\n[3] 超时话术：提案超时不能说成"分层跑超时"')
ok('看图定层' in PsdLayerEngine.SLOW_NOTE['plan'],
   'plan 超时说的是"看图定层"（这一步压根没跑图层）', PsdLayerEngine.SLOW_NOTE['plan'])
ok('分层' in PsdLayerEngine.SLOW_NOTE['run'], 'run 超时才说"分层跑超时"')
ok('plan' in PsdLayerEngine.SLOW_NOTE and 'run' in PsdLayerEngine.SLOW_NOTE,
   '两个阶段各有各的话术')


class _HangProc:
    """永远不返回的子进程：communicate 直接抛超时。"""
    pid = 424242                     # 一个不存在的 pid —— kill_tree 会退到 kill()

    def __init__(self):
        self.returncode = None
        self.killed = False

    def poll(self):
        return None

    def kill(self):
        self.killed = True

    def communicate(self, timeout=None):
        raise subprocess.TimeoutExpired('rhpsd', timeout)


_kills = []
_real_kill_tree = _P.kill_tree
_P.kill_tree = lambda p: (_kills.append(p) is None and True)
_hang = _HangProc()
_P.spawn = lambda argv, cwd=None, env=None: _hang

eng3 = engine()
try:
    eng3.plan(_TINY_PNG)
    _plan_msg = ''
except Exception as e:                                      # noqa: BLE001
    _plan_msg = str(e)
ok('看图定层' in _plan_msg, '提案超时的文案指向"看图定层"', _plan_msg)
ok('分层跑' not in _plan_msg, '不会甩一句和实际阶段对不上的"分层跑超时"')
ok(len(_kills) >= 1, '超时之后会把子进程掐掉（不留孤儿继续烧钱）')

try:
    eng3.run(_TINY_PNG)
    _run_msg = ''
except Exception as e:                                      # noqa: BLE001
    _run_msg = str(e)
ok('分层' in _run_msg, 'run 阶段超时的话术照旧', _run_msg)

_P.kill_tree = _real_kill_tree

# ============================================= [4] plan 阶段的进度
print('\n[4] plan() 也要写进度：界面才知道"它在做什么"')
_seen = {}


class _OkProc:
    returncode = 0

    def __init__(self, wd):
        self._wd = wd

    def poll(self):
        return 0

    def communicate(self, timeout=None):
        return b'', b''


def _prog_spawn(argv, cwd=None, env=None):
    """冒充算法：把 plan 阶段的进度和结果写出来。"""
    _seen['argv'] = list(argv)
    wd = os.path.dirname(argv[argv.index('--json-out') + 1])
    with open(os.path.join(wd, 'progress.json'), 'w', encoding='utf-8') as f:
        json.dump({'phase': 'analyzing',
                   'label': '正在请视觉模型看这张图、定分层'}, f, ensure_ascii=False)
    _seen['during'] = eng4.progress()      # ← 就在"提案跑动中"查一次
    with open(argv[argv.index('--json-out') + 1], 'w', encoding='utf-8') as f:
        json.dump({'ok': True, 'summary': '主花',
                   'plan': {'layers': [{'name': '主花', 'prompt': '花朵:16'}]}},
                  f, ensure_ascii=False)
    return _OkProc(wd)


eng4 = engine()
_P.spawn = _prog_spawn
try:
    _info = eng4.plan(_TINY_PNG)
finally:
    _P.spawn = _REAL_SPAWN

_argv = _seen.get('argv') or []
ok('--progress-out' in _argv, 'plan 把进度落盘位置传给了算法子进程')
if '--progress-out' in _argv and '--json-out' in _argv:
    ok_eq(os.path.dirname(_argv[_argv.index('--progress-out') + 1]),
          os.path.dirname(_argv[_argv.index('--json-out') + 1]),
          '进度文件和这次的结果文件同目录（一次调用一份，互不串台）')
_during = _seen.get('during') or {}
ok_eq(_during.get('running'), True, '提案跑动中 running=True')
ok_eq(_during.get('cmd'), 'plan', '进度里标了这是提案阶段（界面据此选措辞）')
ok(bool(_during.get('label')), '带得出"正在做什么"这句话', _during.get('label'))
ok_eq(_info.get('summary'), '主花', '方案照常返回')
ok(eng4.progress().get('running') is False, '提案跑完就不再报 running')

print('\n[4b] 进度还没落下来时也要有一句垫底的话（别让界面空着）')
_eng5 = engine()
_eng5._current = {'cmd': 'plan', 'workdir': os.path.join(HERE, 'samples'),
                  't0': time.time(), 'proc': None}
_p5 = _eng5.progress()
ok(bool(_p5.get('label')), '进度文件读不到时给默认说明', _p5.get('label'))
ok_eq(_p5.get('cmd'), 'plan', '阶段照报')
_eng5._current = None

# ============================================= [5] runtime.cancel_run
print('\n[5] runtime.cancel_run()：把两家引擎都叫上')


class _FakeEngine:
    def __init__(self, ret):
        self.ret = ret
        self.calls = 0

    def cancel(self):
        self.calls += 1
        return self.ret


from core.runtime import AgentRuntime                        # noqa: E402


class _Sess:
    pass


_s = _Sess()
_s.psdlayer = _FakeEngine(True)
_s.seamless = _FakeEngine(False)
_out = AgentRuntime.cancel_run(_s)
ok_eq(_out.get('ok'), True, '终止接口一律回 ok（前端是 fire-and-forget）')
ok_eq(_s.psdlayer.calls, 1, 'PSD 分层引擎被叫到了')
ok_eq(_s.seamless.calls, 1, '四方连续引擎被叫到了')
ok_eq(_out.get('stopped_any'), True, '至少掐掉了一个')


class _FakeBoom:
    def cancel(self):
        raise RuntimeError('引擎炸了')


_s2 = _Sess()
_s2.psdlayer = _FakeBoom()
_s2.seamless = _FakeEngine(True)
_out2 = AgentRuntime.cancel_run(_s2)
ok_eq(_out2.get('ok'), True, '某一家引擎报错也不影响整体终止（不把界面卡住）')
ok_eq(_out2.get('stopped').get('seamless'), True, '另一家照常被终止')

_s3 = _Sess()
_out3 = AgentRuntime.cancel_run(_s3)
ok_eq(_out3.get('stopped_any'), False, '两家都不在跑时也算正常（用户连点两下不该报错）')

# ============================================= [6] 服务端路由
print('\n[6] 服务端确实挂出了 cancel_run')
_srv = open(os.path.join(BACKEND, 'server.py'), encoding='utf-8').read()
ok("'cancel_run'" in _srv, 'server.py 里有 cancel_run 这条动作')
ok('APP.runtime.cancel_run' in _srv, '接到运行时的 cancel_run 上')
ok(_srv.index("'cancel_run'") < _srv.index("act == 'hand_off'")
   if "act == 'hand_off'" in _srv else True,
   '在 hand_off 之前拦下（终止不该被别的分支抢先）')

# ============================================= [7] 算法侧（真 venv）
print('\n[7] 算法侧：探活要并发，看图失败不拒提案')
_venv = eng._auto_python()
if not _venv:
    print('  --   没装 .venv，跳过算法侧实跑')
else:
    _script = r'''
import json, sys, time
sys.path.insert(0, ".")
from rhpsd import analyze as A

# ① 探活必须并发：把 preflight 换成"睡够 timeout 再失败"，
#    串行的话 8 家要 8×timeout，并发只要 ~1×timeout。
def slow(base, key, timeout=8):
    time.sleep(timeout)
    return False, "stub"
A.preflight = slow
A._REACH_CACHE.clear()
t = time.time(); A.reachable(timeout=0.3); par = time.time() - t

# 对照组：串着来一遍，证明这个用例真的测到了东西
t = time.time()
for _n, _c in A.PROVIDERS.items():
    A.preflight(_c["base"], "", timeout=0.3)
ser = time.time() - t

print(json.dumps({"parallel": par, "serial": ser, "n": len(A.PROVIDERS)}))
'''
    _r = subprocess.run([_venv, '-c', _script], cwd=ALGO,
                        capture_output=True, text=True, timeout=180)
    _data = {}
    try:
        _data = json.loads((_r.stdout or '').strip().splitlines()[-1])
    except Exception:                                       # noqa: BLE001
        pass
    _n = _data.get('n') or 8
    ok(_r.returncode == 0 and _data, '算法侧脚本跑通', (_r.stderr or '')[-200:])
    if _data:
        ok(_data['parallel'] < _data['serial'] / 2,
           '并发探活明显快于串行（%d 家网关）' % _n,
           '并发 %.2fs vs 串行 %.2fs' % (_data['parallel'], _data['serial']))
        ok(_data['parallel'] < 1.2,
           '并发之后总耗时≈单个 timeout，不再累加',
           '%.2fs（timeout=0.3，%d 家）' % (_data['parallel'], _n))

    # ② 看图失败 → 提案照给（ok:true，退回默认单层），note 是人话
    _tmp = os.path.join(HERE, '.v11-plan.json')
    _tmpg = os.path.join(HERE, '.v11-plan-prog.json')
    for _p in (_tmp, _tmpg):
        if os.path.exists(_p):
            os.remove(_p)
    _env = dict(os.environ)
    _env.update({'PYTHONPATH': '.', 'PYTHONWARNINGS': 'ignore',
                 'DEEPSEEK_API_KEY': 'sk-bogus-key-for-test',
                 'DEEPSEEK_BASE_URL': 'https://api.deepseek.com/v1'})
    _r2 = subprocess.run(
        [_venv, '-m', 'rhpsd.service', 'plan',
         '--input', os.path.join(ALGO, 'inputs', '深蓝底水彩花卉.jpeg'),
         '--max-layers', '4', '--progress-out', _tmpg, '--json-out', _tmp],
        cwd=ALGO, env=_env, capture_output=True, text=True, timeout=180)
    _d2 = {}
    if os.path.exists(_tmp):
        _d2 = json.load(open(_tmp, encoding='utf-8'))
    ok_eq(_d2.get('ok'), True,
          '看图失败时提案仍然成立（ok:true）—— 不再把用户挡在门外')
    _layers = [x.get('name') for x in (_d2.get('plan') or {}).get('layers') or []]
    ok(len(_layers) >= 1, '退回了可执行的默认方案', _layers)
    ok('没跑成' in (_d2.get('note') or '') or '默认' in (_d2.get('note') or ''),
       'note 如实说明了这是退回的默认方案', _d2.get('note'))
    ok('http' not in (_d2.get('note') or '').lower()
       and '--' not in (_d2.get('note') or ''),
       'note 是人话（网关地址、CLI 参数这些不让用户看）', _d2.get('note'))
    ok(bool(_d2.get('note_detail')),
       '技术细节另存 note_detail，排错时拿得到', (_d2.get('note_detail') or '')[:60])
    _pg = {}
    if os.path.exists(_tmpg):
        _pg = json.load(open(_tmpg, encoding='utf-8'))
    ok_eq(_pg.get('phase'), 'done', '提案阶段也把进度写到了"完成"')
    for _p in (_tmp, _tmpg):
        if os.path.exists(_p):
            os.remove(_p)

    # ③ plan 子命令认新参数（argparse 不认会直接退 2）
    _r3 = subprocess.run(
        [_venv, '-m', 'rhpsd.service', 'plan', '--input', '/no/such.png',
         '--json-out', '/dev/null', '--progress-out', '/dev/null',
         '--vlm-timeout', '20', '--vlm-retries', '1'],
        cwd=ALGO, capture_output=True, text=True, timeout=60)
    ok(_r3.returncode != 2, 'plan 子命令认 --progress-out / --vlm-timeout / --vlm-retries')
    _src = open(os.path.join(ALGO, 'rhpsd', 'service.py'), encoding='utf-8').read()
    ok('--vlm-timeout' in _src and 'vlm_timeout' in _src,
       '提案阶段自己有独立的预算（不沿用 run 那套 120×3）')

print('\n结果：%d 通过 / %d 失败' % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
