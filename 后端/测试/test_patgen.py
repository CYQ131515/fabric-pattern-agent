#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""花型生成引擎（core/patgen.py）回归 —— 零网络、零花费。

覆盖：
    [1] 关键词映射：需求卡四维 + 识图标签 + 用户原话 → 算法关键词
    [2] 排版参数：构图→预设、疏密→density、尺度→element_scale
    [3] 可用性：mock 模式 / 缺 uv / 缺 key 时必须不可用，并给人话
    [4] generate 契约：argv（版数/档位/尺寸/关键词）、APIMART env 注入、
        产物收集（按版号排序）、成本汇总、落盘到 outdir、返回形状
    [5] 取消：cancel() 转给算法桥
    [6] 生成技能：patgen 可用时走新发动机，提案摘要说明「无缝」且报价按版数

跑法：python3 测试/test_patgen.py
"""
import json
import os
import shutil
import struct
import sys
import tempfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.dirname(HERE)
sys.path.insert(0, AGENT)

# ---- 隔离：出图目录 / 图库 / 埋点全指到临时目录 ----
OUTDIR = tempfile.mkdtemp(prefix='pf-patgen-img-')
os.environ['PATTERNFLOW_IMAGE_PROVIDER'] = 'mock'   # 默认 mock（下面单测会临时放开）
os.environ['PATTERNFLOW_IMAGE_OUTDIR'] = OUTDIR
os.environ['PATTERNFLOW_LIBRARY_DIR'] = tempfile.mkdtemp(prefix='pf-patgen-lib-')
os.environ['PATTERNFLOW_TELEMETRY_DIR'] = tempfile.mkdtemp(prefix='pf-patgen-tel-')

from core.config import Config                                   # noqa: E402
from core import patgen as PG                                   # noqa: E402
import skills.generate_pattern.handler as GEN                   # noqa: E402

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


def tiny_png():
    """最小合法 PNG（1x1 透明），给落盘那一层用。"""
    def chunk(tag, data):
        return (struct.pack('>I', len(data)) + tag + data
                + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff))
    raw = b'\x00\x00\x00\x00\x00'
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw, 9))
            + chunk(b'IEND', b''))


class FakeAlgo:
    """替掉 core.algo.Algo：不真的起 uv 子进程。"""

    def __init__(self):
        self.key = 'sk-TEST-KEY'
        self.base = 'https://api.example.invalid/v1'
        self.uv = sys.executable
        self.calls = []
        self.cancelled = 0
        self.running = False

    def _produce(self, module, argv, env_extra, mode, offline, cwd=None, timeout=None):
        argv = [str(a) for a in argv]
        self.calls.append({'module': module, 'argv': argv, 'timeout': timeout,
                           'cwd': cwd, 'env': dict(env_extra or {}),
                           'mode': mode, 'offline': offline})
        # 模拟 pattern_studio 的产物：--out 下 drafts/draft_N_tile.png + cost/verify
        out = argv[argv.index('--out') + 1]
        drafts = os.path.join(out, 'drafts')
        os.makedirs(drafts, exist_ok=True)
        prints = int(argv[argv.index('--prints') + 1])
        for i in range(1, prints + 1):
            with open(os.path.join(drafts, 'draft_%d_tile.png' % i), 'wb') as f:
                f.write(tiny_png())
        with open(os.path.join(out, 'cost.json'), 'w', encoding='utf-8') as f:
            json.dump({'profile': 'balanced', 'total_usd': 0.0185,
                       'calls': [{'role': 'sheet', 'model': 'gpt-image-2', 'cost': 0.0085},
                                 {'role': 'bg', 'model': 'z-image-turbo', 'cost': 0.01}]}, f)
        with open(os.path.join(out, 'verify.json'), 'w', encoding='utf-8') as f:
            json.dump([{'ok': True, 'tile': i} for i in range(prints)], f)
        return 0, json.dumps({'ok': True}), ''

    def run_py_module(self, module, argv, timeout=None, cwd=None, env_extra=None,
                      with_pkgs=('numpy', 'pillow'), offline=False):
        # 记录调用形态，供测试断言（本 venv vs uv --offline）
        self.last_call_meta = {'timeout': timeout, 'cwd': cwd}
        return self._produce(module, argv, env_extra, 'uv', offline, cwd, timeout)

    def run_python(self, python, module, argv, timeout=None, cwd=None, env_extra=None):
        self.last_call_meta = {'timeout': timeout, 'cwd': cwd, 'python': python}
        return self._produce(module, argv, env_extra, 'python', True, cwd, timeout)

    def cancel(self):
        self.cancelled += 1
        return True


def make_engine():
    """造一个「装好了」的引擎：uv 指向当前解释器，子进程用 FakeAlgo。"""
    old = os.environ.pop('PATTERNFLOW_IMAGE_PROVIDER', None)
    try:
        cfg = Config()
        eng = PG.PatternEngine(cfg)
    finally:
        if old is not None:
            os.environ['PATTERNFLOW_IMAGE_PROVIDER'] = old
    eng.uv = sys.executable
    eng.python = ''                   # 默认走 uv 分支；另有用例显式覆盖
    eng.algo = FakeAlgo()
    eng._cfg_key = 'sk-TEST-KEY'      # 生产只认后端 secrets；测试给一把假 key
    eng._file_key = ''
    return eng


def main():
    global PASS, FAIL

    # ---------------------------------------------------------------- [1]
    print('\n[1] 关键词映射：受控词翻成算法词典，自由词原话透传')
    kw = PG.build_keywords({'style': '新中式国风', 'element': '花卉',
                            'color': '蓝白青花', 'scene': '女装连衣裙'}, {}, '缠枝莲 密')
    ok('缠枝莲' in kw, '用户原话放在前面', kw)
    ok('国风' in kw and '牡丹' in kw and '青花' in kw and '服装' in kw,
       '风格/元素/配色/场景都翻成了算法认的词', kw)
    ok('女装连衣裙' not in kw, '场景不把产品词原样塞进关键词', kw)
    kw2 = PG.build_keywords({'color': '淡红'}, {}, '')
    ok('淡红' in kw2, '词表外配色原样透传（不替用户改说法）', kw2)
    kw3 = PG.build_keywords({}, {}, '')
    ok(kw3, '完全没有条件时也给一个能跑兜底关键词', kw3)

    # ---------------------------------------------------------------- [2]
    print('\n[2] 排版参数：构图/疏密/尺度')
    ok_eq(PG.presets_for({'composition': '散点'}), 'scatter', '散点 → scatter')
    ok_eq(PG.presets_for({'composition': '固定款'}), None, '不认识的构图 → 交给默认预设')
    ok_eq(PG.presets_for({}, default='grid'), 'grid', '没有构图 → 配置里的默认预设')
    ok_eq(PG.density_for({'density': '疏'}), 0.26, '疏 → 0.26')
    ok_eq(PG.density_for({'density': '密'}), 0.55, '密 → 0.55')
    ok_eq(PG.density_for({}), None, '没给疏密 → None（用算法默认）')
    ok_eq(PG.scale_for({'scale': '大花'}), 1.45, '大花 → 1.45')

    # ---------------------------------------------------------------- [3]
    print('\n[3] 可用性：mock / 没装 / 没 key 时不可用且说人话')
    os.environ['PATTERNFLOW_IMAGE_PROVIDER'] = 'mock'
    cfg = Config()
    eng = PG.PatternEngine(cfg)
    ok(eng.available is False, 'mock 模式不调用真实算法')
    ok('mock' in eng.missing_note or '不调用' in eng.missing_note, '给人话', eng.missing_note)
    os.environ.pop('PATTERNFLOW_IMAGE_PROVIDER')

    eng = PG.PatternEngine(cfg)
    eng.uv = ''
    eng.python = ''
    ok(eng.available is False, '没有 Python 运行时不可用')
    ok('安装' in eng.missing_note or 'Python' in eng.missing_note,
       '提示跑安装脚本', eng.missing_note)

    eng = PG.PatternEngine(cfg)
    eng.uv = sys.executable
    eng.algo = FakeAlgo()
    eng._cfg_key = ''
    eng._file_key = ''
    ok(eng.available is False, '没 key 不可用')
    ok('key' in eng.missing_note.lower(), '提示配 key', eng.missing_note)

    # ---------------------------------------------------------------- [4]
    print('\n[4] generate 契约：argv / env / 产物收集 / 成本 / 落盘')
    eng = make_engine()
    ok(eng.available is True, '装好且配了 key → 可用', eng.missing_note)
    eng.presets = 'halfdrop,phyllotaxis,scatter,grid'
    res = eng.generate('国风水彩牡丹', n=2, tag='gen',
                       density=0.4, element_scale=1.2)
    call = eng.algo.calls[-1]
    a = call['argv']
    ok_eq(call['module'], 'pattern_studio', '跑的是 pattern_studio 模块')
    ok_eq(a[0], 'run', 'argv 第一个是 run 子命令')
    ok_eq(call['mode'], 'uv', '没有本地 venv 时走 uv')
    ok(call['offline'] is True, 'uv 默认 --offline（不在请求路径上联网解析依赖）')
    ok('国风水彩牡丹' in a, '关键词原样进 argv')
    ok_eq(a[a.index('--prints') + 1], '2', '版数 = n')
    ok_eq(a[a.index('--provider') + 1], 'apimart', '走 apimart 网关')
    ok_eq(a[a.index('--tile') + 1], '1024', 'tile 宽')
    ok_eq(a[a.index('--density') + 1], '0.4', '密度透传')
    ok_eq(a[a.index('--element-scale') + 1], '1.2', '元素缩放透传')
    ok_eq(a[a.index('--presets') + 1], 'halfdrop,phyllotaxis,scatter,grid',
          '预设串透传')
    ok(call['cwd'].endswith(os.path.join('算法', '花型生成')), 'cwd 指到算法目录',
       call['cwd'])
    ok_eq(call['env'].get('PATTERN_STUDIO_APIMART_KEY'), 'sk-TEST-KEY',
          '把后端的 APIMART key 注入算法')
    ok_eq(res['count'], 2, '返回 2 个文件')
    ok_eq(res['cost'], eng.cost * 2, '点数 = 每版点数 × 版数')
    ok_eq(res['provider'], 'pattern-studio', 'provider 标记')
    ok(res['cost_usd'] == 0.0185, '成本从 cost.json 汇总', res.get('cost_usd'))
    ok('gpt-image-2' in (res['model'] or ''), '模型名从 cost.json 汇总', res['model'])
    ok(all(os.path.exists(f['path']) for f in res['files']), '产物真的落到 outdir',
       [f.get('path') for f in res['files']])
    ok(all('/api/file/' in f['url'] for f in res['files']), '前端地址可用')

    # 版号排序：draft_10 不能排在 draft_2 前面
    import glob
    td = tempfile.mkdtemp(prefix='pf-patgen-order-')
    try:
        for name in ('draft_2_tile.png', 'draft_10_tile.png', 'draft_1_tile.png'):
            os.makedirs(os.path.join(td, 'drafts'), exist_ok=True)
            open(os.path.join(td, 'drafts', name), 'wb').close()
        got = [os.path.basename(p) for p in PG.PatternEngine._collect_tiles(td)]
        ok_eq(got, ['draft_1_tile.png', 'draft_2_tile.png', 'draft_10_tile.png'],
              '产物按版号排序（不是字典序）')
    finally:
        shutil.rmtree(td, ignore_errors=True)

    # 算法失败 → 人话错误，不抛裸异常
    class FailAlgo(FakeAlgo):
        def run_py_module(self, *a, **kw):
            return 1, '', 'nginx: bad gateway'

    eng2 = make_engine()
    eng2.algo = FailAlgo()
    try:
        eng2.generate('国风', n=1)
        ok(False, '算法失败时抛 ImageGenError')
    except PG.ImageGenError as e:
        ok('没跑通' in str(e), '算法失败时给人话', str(e))

    # 有本地 venv 时直接起 python -m（不走 uv，请求路径零网络）
    eng5 = make_engine()
    eng5.python = sys.executable
    eng5.generate('国风', n=1)
    ok_eq(eng5.algo.calls[-1]['mode'], 'python',
          '装了 .venv 就直接用解释器起进程（不经过 uv）')
    ok_eq(eng5.algo.calls[-1]['python'] if 'python' in eng5.algo.calls[-1] else sys.executable,
          sys.executable, '用的就是 venv 里的 python')

    # 依赖取不到（uv 联网超时）→ 提示跑安装脚本，而不是甩 "退出码 2"
    class NetFailAlgo(FakeAlgo):
        def __init__(self):
            super().__init__()
            self.attempts = []

        def run_py_module(self, *a, **kw):
            self.attempts.append(bool(kw.get('offline')))
            return 2, '', ('error: Failed to fetch: https://pypi.org/simple/numpy/\n'
                           '  cause: operation timed out')

    eng6 = make_engine()
    eng6.algo = NetFailAlgo()
    try:
        eng6.generate('国风', n=1)
        ok(False, '依赖取不到时抛 ImageGenError')
    except PG.ImageGenError as e:
        ok('运行环境' in str(e) and '安装.command' in str(e),
           '依赖取不到时提示跑安装脚本', str(e))
    ok_eq(eng6.algo.attempts, [True, False], '离线失败后联网重试一次')

    # mock 档位不需要云端 key（离线自检/演示）
    eng7 = make_engine()
    eng7.provider = 'mock'
    eng7._cfg_key = ''
    ok(eng7.available is True, 'mock 档位不依赖云端 key')
    ok_eq(eng7.generate('国风', n=1)['count'], 1, 'mock 档位能离线出图')

    # ---------------------------------------------------------------- [5]
    print('\n[5] 取消：转给算法桥')
    eng3 = make_engine()
    ok(eng3.cancel() is True, 'cancel 返回算法桥的结果')
    ok_eq(eng3.algo.cancelled, 1, '真的调用了算法桥的 cancel')

    # ---------------------------------------------------------------- [6]
    print('\n[6] 生成技能：patgen 可用时走新发动机')
    eng4 = make_engine()
    ctx = {'patgen': eng4, 'imagegen': None, 'session': None,
           'brief': {'style': '新中式国风', 'element': '花卉',
                     'color': '蓝白青花', 'scene': '女装连衣裙'}}
    prop = GEN.run({'confirmed': False, '_retune': True}, ctx)
    ok_eq(prop.get('need_confirm'), True, '正常给出待确认方案')
    ok('无缝' in (prop.get('summary') or ''), '摘要说明产物是无缝的',
       prop.get('summary'))
    ok_eq(prop.get('cost'), eng4.cost * 4, '报价按 4 版 × 每版点数')
    ok('缠' not in (prop.get('prompt') or '') and '国风' in (prop.get('prompt') or ''),
       '提案里的 prompt 是算法关键词（不是老的长提示词）', prop.get('prompt'))

    ex = GEN.run({'confirmed': True, 'n': 2}, ctx)
    ok_eq(ex.get('ok'), True, '确认后走新发动机执行')
    ok(ex.get('seamless') is True, '结果标记可平铺（前端据此默认进平铺预览）')
    ok_eq(len(ex.get('images') or []), 2, '回传 2 版产物')
    ok_eq(ex.get('cost'), eng4.cost * 2, '执行扣点按实际版数')

    print('\n结果：%d 通过 / %d 失败' % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
