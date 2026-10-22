#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""新算法桥接层的离线冒烟（不调模型、不花钱）。

跑法：python3 测试/test_algo_bridge.py

覆盖：
    [1] Algo 找得到算法目录自带的 .runtime（node / sharp / uv）
    [2] pipeline --dry-run 体检：零成本跑出候选顺序（四方连续提案靠它）
    [3] sharp 内联缩小（四方连续快速路径用）
    [4] 四方连续引擎 probe / describe / 进度语义
    [5] 分层 PSD 引擎 plan（不跑算法）+ 缓存伪装跑动的进度
    [6] 描稿引擎 describe（模型 / 点数，不真跑）

为什么要单测：这三个功能的「引擎」从旧 Python 子进程换成了新算法的 Node/uv
步骤，调用链、可用性判断、进度形状全变了。这层错了，三个技能会一起坏，
而且报错都发生在真正花钱的那一步 —— 必须在不花钱的地方先钉住。
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.dirname(HERE)
sys.path.insert(0, AGENT)

from core.config import Config                                # noqa: E402
from core.imagegen import _png                                # noqa: E402
from core.algo import Algo, AlgoError                         # noqa: E402
from core.seamless import SeamlessEngine                      # noqa: E402
from core.psdlayer import PsdLayerEngine                      # noqa: E402
from core.tracealgo import TraceEngine                        # noqa: E402

PASS = FAIL = 0


def ok(cond, label, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('  ✓ ' + label)
    else:
        FAIL += 1
        print('  ✗ ' + label + ('  → %s' % (extra,) if extra else ''))


def tiny_png():
    """64×64 的彩色条纹图：足够 sharp / pipeline 处理，又很小。"""
    return _png(64, 64, lambda x, y: (x * 3 % 256, y * 3 % 256, (x + y) % 256))


def main():
    cfg = Config()

    print('\n[1] 运行时：算法目录自带 .runtime')
    algo = Algo(cfg)
    ok(algo.installed, '引擎自检通过（node + sharp + uv + 步骤齐全）',
       algo.missing_note)
    ok(bool(algo.node) and os.access(algo.node, os.X_OK), 'node 可执行', algo.node)
    ok(os.path.isdir(algo.sharp), 'sharp 目录在', algo.sharp)
    ok(bool(algo.uv) and os.access(algo.uv, os.X_OK), 'uv 可执行', algo.uv)
    ok(os.path.isdir(algo.root), '算法目录在', algo.root)

    print('\n[2] 体检：pipeline --dry-run（零成本）')
    png = tiny_png()
    rep = algo.dry_run(png, '.png', timeout=120)
    ok(bool(rep.get('attempts')), '给出了候选顺序', rep.get('attempts'))
    ok(all(a in ('fast', 'det', 'ai', 'tile-multi', 'regenerate')
           for a in rep.get('attempts')), '候选名都在已知集合里', rep.get('attempts'))
    ok('features' in rep and 'lowGradFrac' in (rep.get('features') or {}),
       '带回了图像特征（策略依据）', rep.get('features'))

    print('\n[3] sharp 内联缩小（快速路径）')
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, 'in.png')
        out = os.path.join(td, 'out.png')
        with open(src, 'wb') as f:
            f.write(png)
        algo.shrink(src, out, max_width=32, timeout=60)
        ok(os.path.exists(out) and os.path.getsize(out) > 0, '缩图成功', out)

    print('\n[4] 四方连续引擎：probe / describe / 进度')
    se = SeamlessEngine(cfg)
    d = se.describe()
    ok(d.get('available') is True, '引擎可用', d.get('available'))
    ok(d.get('cost') == 30, '点数仍是 30（口径不变）', d.get('cost'))
    info = se.probe(png, '.png')
    ok(info.get('route_hint') in ('pure-algorithm', 'model', 'reject'),
       '体检给出路线建议', info.get('route_hint'))
    ok(isinstance(info.get('summary'), str) and info.get('summary'),
       '体检有一句人话摘要', info.get('summary'))
    se._begin('convert')
    prog = se.progress()
    ok(prog.get('running') is True and prog.get('label'), '跑动中有进度可报', prog)
    ok(isinstance(prog.get('elapsed'), float), '进度带已等秒数')
    ok(se.conf.get('ai_postfix') is True, 'AI 补缝默认接带后处理（L39）')
    ok(int(se.conf.get('base_max_side') or 0) == 1024, '修缝前底图归一 1024')
    ok(os.path.exists(se.algo.step('09-seam-postfix.py')), '后处理步骤文件在')
    ok(se._allowed(['ai', 'det', 'regenerate']) == ['ai2', 'det', 'regenerate'],
       '候选顺序里 ai 自动换成 ai2', se._allowed(['ai', 'det', 'regenerate']))
    se._end()
    ok(se.progress().get('running') is False, '收尾后不再报 running')

    print('\n[4b] B 级挑选：wrap 断了的直接淘汰，挑缺陷分最小的')

    def _rev(wrap_fail, lines, rx, ry, reason='', hp=1.0, d_lp=0.0):
        return {'detail': {'wrap': {'fail': wrap_fail, 'ratioX': rx, 'ratioY': ry},
                           'suspicious_lines': [{}] * lines, 'reason': reason,
                           'repair': {'ok': False, 'hp_ratio': hp, 'd_lp': d_lp}}}

    hard_line = {'kind': 'ai', 'review': _rev(False, 1, 1.02, 1.03, '接缝可见')}
    soft = {'kind': 'ai', 'review': _rev(False, 0, 1.05, 1.04,
                                         '修复带可见（亮度/颗粒不连续）', hp=0.8)}
    soft_bad = {'kind': 'det', 'review': _rev(False, 0, 1.05, 1.04,
                                             '修复带可见（亮度/颗粒不连续）', hp=0.5)}
    broken = {'kind': 'regenerate', 'review': _rev(True, 0, 1.5, 1.4)}
    ok(se._defect_score(hard_line['review']) is None, '检出贯穿线 → 不能当 B 级（硬缺陷）')
    ok(se._defect_score(broken['review']) is None, 'wrap 断裂 → 不参与 B 级')
    best = se._best_b([hard_line, soft, soft_bad, broken])
    ok(best is soft, '只在「03 通过 + 03b 软带」里挑缺陷最小的', best and best['kind'])
    ok(se._best_b([hard_line, broken]) is None, '全是硬缺陷 → 不给 B 级（宁可不交付）')

    print('\n[5] 分层 PSD 引擎：plan + 缓存伪装跑动')
    ps = PsdLayerEngine(cfg)
    pd = ps.describe()
    ok(pd.get('available') is True, '引擎可用（本地色层，不需要 key）',
       pd.get('available'))
    ok(pd.get('structure') == 'Background(root) + Colors(group)',
       'PSD 结构固定为根层 + Colors 分组', pd.get('structure'))
    plan = ps.plan(png, max_layers=3)
    names = [x['name'] for x in (plan.get('plan') or {}).get('layers') or []]
    ok(len(names) == 3 and names[0] == 'Color01', '提案给出 3 层色层名', names)
    ps.begin_cached_run(total_layers=3, duration=0.01)
    ok(ps.progress().get('running') is True, '缓存命中也能报 running（伪装）')
    ps.end_cached_run()
    ok(ps.progress().get('running') is False, '伪装跑动收尾')

    print('\n[6] 描稿引擎：describe（不真跑）')
    te = TraceEngine(cfg)
    td = te.describe()
    ok(td.get('model') == 'gpt-image-2.5-sunburst',
       '描稿模型用算法侧 2.5 世代（不是生图那套模型 id）', td.get('model'))
    ok(td.get('cost') == 60, '点数沿用 60', td.get('cost'))
    ok(td.get('variants') == 1, '一次只出 1 张（旧的多候选已去掉）', td.get('variants'))
    ok(td.get('unwarp') is True and td.get('passes') == 2,
       '描稿内置两遍：先平铺还原再清洗（R1）', td.get('passes'))
    ok(td.get('size_final') == '2048x2048', '最终交付 2048²', td.get('size_final'))
    ok(td.get('enabled') in (True, False), '可用性是一个明确布尔值', td.get('enabled'))

    print('\n[7] 错误透传：网络失败不要 URL 编码堆栈，要一句人话')
    node_stack = ('file:///Users/x/%E8%8A%B1%E5%9E%8B/steps/01-clean.mjs:36\n'
                  'const r = await generateImage(...)\n'
                  'TypeError: fetch failed\n'
                  '    at async file:///x/01-clean.mjs:36:11\n'
                  '  [cause]: Error: connect ETIMEDOUT 198.18.0.39:443\n'
                  'Node.js v24.21.0')
    msg = Algo.error_summary('', node_stack)
    ok('连不上生图网关' in msg, '网络类错误翻译成人话', msg)
    ok('%E6' not in msg and 'Node.js' not in msg, '不把 URL 编码路径/Node 版本甩给用户', msg)
    ok(Algo.error_summary('', "Error: 生成结果没有图片 URL\n    at x:1:1") ==
       '生成结果没有图片 URL', '普通 Node 错误只留错误行（去掉 Error: 前缀）')
    ok(Algo.error_summary('', 'nginx: bad gateway') == 'nginx: bad gateway',
       '非 Node 的 stderr 原样保留最后一句')

    se2 = SeamlessEngine(cfg)
    if se2.available:
        se2.conf['base_max_side'] = 0        # 跳过缩图（要起 node），直接测失败路径

        def _fake_probe(blob, ext='.png', timeout=None):
            return {'attempts': ['det', 'ai2', 'regenerate'], 'features': {},
                    'warnings': []}

        def _fake_clean(src, base, metas, deadline=None):
            raise AlgoError('AI 清洗失败了。连不上生图网关（APIMART）：网络或代理不通。')

        se2._probe_report = _fake_probe
        se2._ensure_base = _fake_clean
        try:
            se2.convert(b'not-an-image')
            ok(False, '清洗失败时抛错')
        except Exception as e:                        # noqa: BLE001
            m = str(e)
            ok('AI 清洗' in m, '清洗失败直接报真因（不再等三条路线都跑一遍）', m)
            ok('三条路线' not in m, '不误报成「接缝审查全灭」', m)
            ok('%E6' not in m, '不出现 URL 编码路径', m)
    else:
        print('  （当前算法环境不可用，跳过清洗失败路径）')


    print('\n[8] 描稿路线二 / PSD 元素分层（2026-10-21 对标 uniseven 新增）')
    from core.tracealgo import TraceEngine as _TE, ROUTE2_PROMPT as _R2
    te2 = _TE(cfg)
    td2 = te2.describe()
    ok(td2.get('route') in ('route1', 'route2'), '描稿路线有明确默认值', td2.get('route'))
    ok('route1' in (td2.get('routes') or []) and 'route2' in (td2.get('routes') or []),
       '路线一（两遍，默认）和路线二（单遍）都可用', td2.get('routes'))
    ok('COMPLETE' in _R2, '路线二提示词含「元素完整不切断」构图约束')
    ok('relief' in _R2.lower(), '路线二提示词要求保留印花浮雕/层次')

    from core.psdlayer import PsdLayerEngine as _PSD, DEFAULT_CONF as _PD
    pd = _PSD(cfg)
    dd = pd.describe()
    ok(dd.get('mode') in ('color', 'elements'), 'PSD 分层模式有明确默认值', dd.get('mode'))
    ok(_PD.get('mode') == 'color', '默认仍是按颜色分色', _PD.get('mode'))
    pd.mode = 'elements'
    plan2 = pd.plan(b'x')
    names = [it.get('name') for it in (plan2.get('plan') or {}).get('layers') or []]
    ok(names == ['Foreground', 'Background'], '元素分层提案：前景 + 背景', names)
    ok(pd.installed, '元素分层脚本存在且算法环境可用')
    pd.mode = 'color'


    print('\n[9] SAM3.1（RunningHub）语义蒙版接入')
    from core.psdlayer import PsdLayerEngine as _PSD3
    pd3 = _PSD3(cfg)
    d3 = pd3.describe()
    ok(d3.get('matte') in ('flood', 'sam3'), 'PSD 前景来源有明确默认值', d3.get('matte'))
    ok(d3.get('matte') == 'flood', '默认仍是本地区域生长（不依赖外网）', d3.get('matte'))
    import os as _os
    ok(_os.path.exists(pd3.algo.step('06-matte-sam3-rh.py')), 'SAM3 蒙版步骤文件存在')
    ok(_os.path.exists(pd3.algo.step('05-layers-elements.py')), '元素分层 PSD 步骤文件存在')
    ok(hasattr(pd3, 'rh_key') and callable(pd3.rh_key), '引擎有 RunningHub key 读取入口')
    rh = (cfg.settings.get('runninghub') or {})
    ok(bool(rh.get('workflow_id')), 'RunningHub 工作流 id 已配置', rh.get('workflow_id'))
    ok(str(rh.get('api_key_env') or '') == 'RUNNINGHUB_API_KEY', 'key 走环境变量名，不写进代码')


    print('\n[10] 接缝审查开关（用户要求关闭时：出图即交付、结果标"未审"）')
    class _FakeSeam(SeamlessEngine):
        @property
        def available(self): return True
        @property
        def missing_note(self): return ''

        def __init__(self, cfg):
            super().__init__(cfg)
            self.conf = dict(self.conf)
            self.conf['review'] = False
            self.conf['base_max_side'] = 0
            self.conf['allow_grade_b'] = False

        def _probe_report(self, *a, **k):
            return {'attempts': ['det', 'ai2'], 'features': {}}

        def _ensure_base(self, src, base, metas, deadline=None):
            open(base, 'wb').write(b'')

        def _seam(self, kind, base, flat, metas, deadline=None):
            open(flat, 'wb').write(b'')

    fs = _FakeSeam(cfg)
    fd = fs.describe()
    ok(fd.get('review') is False, 'describe 暴露 review=False', fd.get('review'))
    res, wd = fs.convert(b'not-a-real-image', '.png')
    rep = res.get('seam_report') or {}
    ok(res.get('ok') is True, '审查关闭时出图即成功')
    ok(res.get('route') == 'ai2', '关审时按画质优先（ai2 先于 det），不拿直出凑数', res.get('route'))
    ok(rep.get('reviewed') is False, '结果明确标 reviewed=False', rep.get('reviewed'))
    ok(rep.get('grade') == '未审', '评级是「未审」而不是 A/B', rep.get('grade'))
    ok(any('接缝审查' in w for w in (res.get('warnings') or [])),
       '带一条「已关闭接缝审查」的告警', res.get('warnings'))
    import shutil as _sh
    _sh.rmtree(wd, ignore_errors=True)

    # 默认（review=True）时 describe 仍报 true，行为不变
    se3 = SeamlessEngine(cfg)
    ok(se3.describe().get('review') in (True, False), '默认 review 是明确布尔值',
       se3.describe().get('review'))

    print('\n结果：%d 通过 / %d 失败' % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
