#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""入库闸门回归：后台导入的每张图都必须先过自动打标签算法（离线）。

覆盖：
    [1] 适配层 core/atag.py：装没装 / 探活 / 打标结果的归一化
        （ok / 不是花型 / 打标失败 / 超时 / 引擎没回结果）
    [2] 四维列表 → 花型库单值标签的映射：第一条当主标签，
        词表外原话进 rejected，一维里的其余条并进关键词（客户真会那么说）
    [3] 闸门 import_via_atag：引擎没就绪 / 不是花型 → 一律不入库；
        跑通了才带着标签入库，且**不再调云端 tagger**（一张图只打一次）
    [4] 扫目录用的文件版闸门：move=True 入库成功才删源文件
    [5] library_scan 开跑前先探活：引擎没起就拒绝开始（别让运营白等）
    [6] 概览自检里有「入库自动打标」这一行，且不泄漏端点地址

子进程全部打桩（core.atag.subprocess.run），不需要真模型、不需要联网。
跑法：python3 测试/test_atag_import.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.dirname(HERE)
sys.path.insert(0, AGENT)

# 埋点也隔离：本文件会建真 App（后台自检要用），把埋点指到临时目录，
# 不让测试事件落进运营真看到的看板（test_telemetry 的元测试会扫这一条）。
os.environ['PATTERNFLOW_TELEMETRY_DIR'] = tempfile.mkdtemp(prefix='pf-atag-imp-tel-')

from core import admin                                        # noqa: E402
from core import atag as atag_mod                            # noqa: E402
from core.atag import AtagEngine                             # noqa: E402
from core.imagegen import _png                               # noqa: E402
from core.library import PatternLibrary                      # noqa: E402

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


VOCAB = {
    'style': ['法式浪漫', '新中式国风'],
    'element': ['花卉', '几何'],
    'color': ['蓝白青花', '黑白灰'],
    'scene': ['女装连衣裙', '衬衫'],
}

TAGGED = {
    'status': 'ok', 'style': ['法式浪漫', '宫廷古典'],
    'element': ['花卉', '叶片藤蔓'], 'color': ['蓝白青花'],
    'scene': ['女装连衣裙'],
    'free': {'style': ['水墨仙气']},
    'colors': ['靛蓝', '瓷白'], 'keywords': ['小碎花'],
    'desc': '蓝白花卉散点', 'reason': '看着像', 'confidence': 0.9,
    'elapsed_ms': 5200, 'prompt_version': 'atag-v2',
}


# ---------------------------------------------------------------- 假引擎
class FakeEngine:
    """替掉真引擎。available/ready/tag 三个旋钮覆盖闸门的全部分支。"""

    def __init__(self, available=True, ready=True, tag=None):
        self.available = available
        self._ready = ready
        self._tag = tag if tag is not None else dict(TAGGED, ok=True)
        self.missing_note = '假引擎没装好'
        self.probe_calls = 0

    def probe(self, fresh=False):
        self.probe_calls += 1
        return {'ok': self._ready, 'ready': self._ready,
                'note': '就绪' if self._ready else '引擎没起'}

    def tag_blob(self, blob):
        return self._tag

    def to_library_tags(self, res):
        return AtagEngine.to_library_tags(res)

    def describe(self):
        return {'enabled': True, 'installed': self.available,
                'available': self.available, 'note': ''}


class NeverTagger:
    """ preset 路径下**不允许**被调用 —— 一张图只该被打一次。"""

    def tag(self, *a, **kw):
        raise AssertionError('preset_tags 给了还调云端 tagger，一张图被打了两遍')

    def describe(self):
        return {'enabled': False}


class FakeCfg:
    def __init__(self, settings):
        self.settings = settings
        self.secrets = {}


class FakeApp:
    def __init__(self, lib, cfg=None):
        self.library = lib
        self.cfg = cfg or FakeCfg({})


def newlib(tmp, name='library'):
    return PatternLibrary(None, root=os.path.join(tmp, name),
                          tagger=NeverTagger(), vocab=VOCAB)


def patch_engine(monkey_engine):
    """把闸门用的引擎换成假的。返回原对象供恢复。"""
    old = admin._atag_engine
    admin._atag_engine = lambda app: monkey_engine
    return old


# ------------------------------------------------- 子进程打桩（适配层用）
class FakeProc:
    def __init__(self, rc=0, out=b'', err=b''):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def stub_subprocess(payload, rc=0, err=b''):
    """按 argv 里的 --json-out 写结果，模拟算法子进程。

    同时记下被叫了几次、参数长什么样 —— 闸门要对每张图起一次子进程，
    这个计数就是"打过一次"的证据。
    """
    calls = []

    def run(argv, **kw):
        calls.append(list(argv))
        jp = argv[argv.index('--json-out') + 1] if '--json-out' in argv else None
        if jp and payload is not None:
            with open(jp, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False)
        return FakeProc(rc, err=err)
    return run, calls


# =================================================================== 用例
def t_normalize():
    print('\n[1] 适配层：打标结果的归一化')
    eng = AtagEngine(FakeCfg({'atag': {'dir': '../算法/自动打标签'}}))

    r = AtagEngine._normalize({'ok': True, 'record': TAGGED})
    ok(r['ok'] and not r['not_pattern'] and not r['empty'], 'status=ok → 可用')
    ok_eq(r['style'], ['法式浪漫', '宫廷古典'], '四维原样带回（列表）')
    ok_eq(r['colors'], ['靛蓝', '瓷白'], '主色带回')
    ok_eq(r['desc'], '蓝白花卉散点', '描述带回')
    ok_eq(r['prompt_version'], 'atag-v2', '提示词版本带回（排查用）')

    r = AtagEngine._normalize({'ok': True, 'record': dict(TAGGED,
                                                          status='not_pattern',
                                                          reason='一张截图')})
    ok(r['not_pattern'] and not r['ok'], '不是花型 → 单独分支，不当成可用')
    ok('截图' in r['error'], '拒绝原因带模型的判断', r['error'])

    r = AtagEngine._normalize({'ok': True, 'record': dict(TAGGED,
                                                          status='failed',
                                                          error='模型没回 JSON')})
    ok(not r['ok'] and not r['not_pattern'], '打标失败 → 不可用，也不算"不是花型"')
    ok_eq(r['error'], '模型没回 JSON', '失败原因透传')

    r = AtagEngine._normalize({'ok': True, 'record': dict(TAGGED,
                                                          status='empty')})
    ok(r['ok'] and r['empty'], '是花型但四维没标签 → 算过闸门，标 empty')

    r = AtagEngine._normalize({'ok': False, 'error': '连不上引擎'})
    ok(not r['ok'] and r['error'] == '连不上引擎', '服务层直接失败 → 原样透传')

    print('\n[2] 四维列表 → 库条目的单值标签')
    tags = AtagEngine.to_library_tags(dict(TAGGED, ok=True))
    ok_eq(tags['style'], '法式浪漫', '一维多标取第一条当主标签')
    ok_eq(tags['element'], '花卉', '元素同理')
    ok_eq(tags['color'], '蓝白青花', '配色同理')
    ok_eq(tags['scene'], '女装连衣裙', '场景同理')
    ok_eq(tags['rejected'], {'style': '水墨仙气'}, '词表外原话进 rejected')
    ok('宫廷古典' in tags['keywords'] and '叶片藤蔓' in tags['keywords'],
       '一维里其余的标并进关键词（客户真会那么搜）', tags['keywords'])
    ok('水墨仙气' in tags['keywords'], '词表外原话也并进关键词')
    ok_eq(tags['desc'], '蓝白花卉散点', '描述带着走')
    ok_eq(tags['ok'], True, '带上 ok，库那边才认')
    ok(AtagEngine.to_library_tags(None) is None, '没打过标 → 不给标签（调用方自己兜）')


def t_engine_shell(tmp):
    print('\n[3] 适配层：装没装 / 探活 / 起子进程')
    root = os.path.join(tmp, 'algo')
    os.makedirs(os.path.join(root, 'atag'))
    with open(os.path.join(root, 'atag', 'service.py'), 'w') as f:
        f.write('# stub\n')
    os.makedirs(os.path.join(root, '.venv', 'bin'))
    py = os.path.join(root, '.venv', 'bin', 'python')
    with open(py, 'w') as f:
        f.write('#!/bin/sh\n')

    eng = AtagEngine(FakeCfg({'atag': {'dir': root, 'python': py}}))
    ok(eng.installed and eng.available, '代码和 venv 都在 → 装好了')
    ok('安装' not in eng.missing_note, '装好了就不会给"去安装"的话')

    eng2 = AtagEngine(FakeCfg({'atag': {'dir': os.path.join(tmp, '没有')}}))
    ok(not eng2.installed and not eng2.available, '目录不存在 → 没装好')
    ok('atag.dir' in eng2.missing_note, '没装好时的话能照着排查（指出配置项）',
       eng2.missing_note)

    eng3 = AtagEngine(FakeCfg({'atag': {'enabled': False, 'dir': root}}))
    ok(not eng3.available, 'enabled=false → 不可用')
    ok('enabled' in eng3.missing_note, '话里说清是被关掉了')

    # 探活：就绪
    real_run, calls = stub_subprocess({'ok': True, 'available': True,
                                       'model_ready': True, 'note': '就绪'})
    old = atag_mod.subprocess.run
    atag_mod.subprocess.run = real_run
    try:
        r = eng.probe(fresh=True)
        ok(r['ready'] and r['ok'], '探活：引擎起着、模型在 → ready')
        ok('http' not in json.dumps(r, ensure_ascii=False),
           '探活结果里没有端点地址（这个结果会上页面）')
        ok(len(calls) == 1 and 'probe' in calls[0], '探活起了一次子进程', calls)

        # 探活：服务起着但没有要用的模型
        atag_mod.subprocess.run, _ = stub_subprocess(
            {'ok': True, 'available': True, 'model_ready': False,
             'note': '服务通了，但里面没有这个模型。'})
        r = eng.probe(fresh=True)
        ok(not r['ready'], '模型不在 → 不算就绪')
        ok('启动本地模型' in r['note'], '话里告诉运营怎么起引擎', r['note'])

        # 探活：连不上
        atag_mod.subprocess.run, _ = stub_subprocess(
            {'ok': False, 'error': '连不上', 'note': '引擎没起'})
        r = eng.probe(fresh=True)
        ok(not r['ready'] and r['note'], '连不上 → 不就绪且有人话')

        # 探活：子进程没写结果文件
        atag_mod.subprocess.run, _ = stub_subprocess(None, rc=1, err=b'boom')
        r = eng.probe(fresh=True)
        ok(not r['ready'] and '退出码' in r['note'], '没回结果 → 不就绪，带上退出码')

        # 探活缓存：20 秒内重复问不起新子进程
        atag_mod.subprocess.run, calls2 = stub_subprocess(
            {'ok': True, 'available': True, 'model_ready': True, 'note': '就绪'})
        atag_mod._PROBE_CACHE.update({'key': None, 'ts': 0.0, 'result': None})
        eng.probe(fresh=True)
        eng.probe()
        eng.probe()
        ok(len(calls2) == 1, '状态页连问三次只起一次子进程（缓存生效）', len(calls2))

        # 打标：正常
        atag_mod.subprocess.run, calls3 = stub_subprocess(
            {'ok': True, 'record': TAGGED, 'summary': '风格：法式浪漫'})
        blob = _png(24, 24, lambda x, y: (200, 60, 60))
        r = eng.tag_blob(blob)
        ok(r['ok'] and r['style'] == ['法式浪漫', '宫廷古典'], '打标：结果归一化回来')
        ok('one' in calls3[0] and '--vocab' in calls3[0],
           '打标走 one 子命令，且带上主程序词表', calls3[0])
        ok('--max-side' in calls3[0], '带上了缩图边长（不为难本地模型）')

        # 打标：不是花型
        atag_mod.subprocess.run, _ = stub_subprocess(
            {'ok': True, 'record': dict(TAGGED, status='not_pattern',
                                        reason='这是张截图')})
        r = eng.tag_blob(blob)
        ok(r['not_pattern'], '打标：不是花型有单独分支')

        # 打标：服务层失败
        atag_mod.subprocess.run, _ = stub_subprocess(
            {'ok': False, 'error': '连不上视觉模型服务', 'stage': 'probe'})
        r = eng.tag_blob(blob)
        ok(not r['ok'] and '连不上' in r['error'], '打标：引擎挂了 → 不可用')

        # 打标：超时（不起异常，回一句人话）
        def boom(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 180)
        atag_mod.subprocess.run = boom
        r = eng.tag_blob(blob)
        ok(not r['ok'] and '超时' in r['error'], '打标：超时 → 不可用且说清原因')
    finally:
        atag_mod.subprocess.run = old


def t_gate(tmp):
    print('\n[4] 闸门：没过算法的图一张都不准进库')
    lib = newlib(tmp, 'g1')
    app = FakeApp(lib)
    blob = _png(24, 24, lambda x, y: (60, 90, 200))

    # 引擎没装好 → 拒绝，库里空的
    old = patch_engine(FakeEngine(available=False))
    try:
        r = admin.import_via_atag(app, blob, src_name='客户来图.jpg')
        ok(not r['ok'] and r['error'] == '假引擎没装好', '没装好 → 拒绝入库')
        ok_eq(len(lib.items), 0, '库里还是空的')

        # 引擎起着但没就绪 → 拒绝
        eng = FakeEngine(ready=False)
        admin._atag_engine = lambda a: eng
        r = admin.import_via_atag(app, blob, src_name='客户来图.jpg')
        ok(not r['ok'] and '还没就绪' in r['error'], '没就绪 → 拒绝入库', r['error'])
        ok_eq(len(lib.items), 0, '库里还是空的')

        # 算法说不是花型 → 拒绝，连原图都不落
        eng = FakeEngine(tag={'ok': False, 'not_pattern': True,
                              'error': '这张图看起来不是面料花型（截图）。'})
        admin._atag_engine = lambda a: eng
        r = admin.import_via_atag(app, blob, src_name='截图.png')
        ok(not r['ok'] and r['not_pattern'], '不是花型 → 拒绝入库')
        ok_eq(len(lib.items), 0, '库里还是空的（原图也没落盘）')
        orig = os.path.join(lib.root, 'orig')
        ok(not (os.path.isdir(orig) and os.listdir(orig)), '一张原图都没落盘')

        # 算法跑通了 → 入库，带标签
        eng = FakeEngine()
        admin._atag_engine = lambda a: eng
        r = admin.import_via_atag(app, blob, src_name='客户来图A.jpg',
                                  source='upload')
        ok(r['ok'] and r.get('id'), '跑通了 → 入库')
        it = r['item']
        ok_eq(it['style'], '法式浪漫', '主标签落库（单值）')
        ok_eq(it['element'], '花卉', '元素落库')
        ok_eq(it['name'], '客户来图A', '名字取文件名（atag 不起名）')
        ok_eq(it['status'], 'ready', '状态是已入库')
        ok_eq(it['tag_mode'], 'ai', '标记是 AI 打的')
        ok_eq(it['source'], 'upload', '来源记上传')
        ok_eq(it['keywords'], ['小碎花', '宫廷古典', '叶片藤蔓', '水墨仙气'],
              '关键词带上其余标签和词表外原话')
        ok_eq(it['tag_rejected'], {'style': '水墨仙气'}, '词表外原话有记下来')

        # 同一张再导 → 判重，不调第二次算法
        n_before = eng.probe_calls
        r2 = admin.import_via_atag(app, blob, src_name='客户来图A-改名.jpg')
        ok(r2.get('dup') and len(lib.items) == 1, '内容相同 → 判重，不重复入库')
        ok(eng.probe_calls == n_before + 1, '判重前也过了闸门（先探活再查重）')

        # 打标失败（模型炸了）→ 拒绝，不落"没标签的图"
        eng = FakeEngine(tag={'ok': False, 'not_pattern': False,
                              'error': '模型没回可解析的 JSON'})
        admin._atag_engine = lambda a: eng
        r = admin.import_via_atag(app, _png(30, 30, lambda x, y: (10, 10, 10)),
                                  src_name='炸.jpg')
        ok(not r['ok'] and '没有入库' in r['error'], '打标失败 → 拒绝入库', r['error'])
        ok_eq(len(lib.items), 1, '库里没多出东西')

        # 调用方显式给了名字就用调用方的
        eng = FakeEngine()
        admin._atag_engine = lambda a: eng
        r = admin.import_via_atag(app, _png(31, 31, lambda x, y: (10, 200, 10)),
                                  src_name='乱七八糟的名字.png', name='青花玫瑰')
        ok(r['ok'] and r['item']['name'] == '青花玫瑰', '显式名字优先')
    finally:
        admin._atag_engine = old


def t_file_gate(tmp):
    print('\n[5] 扫目录的文件版闸门')
    lib = newlib(tmp, 'g2')
    app = FakeApp(lib)
    src = os.path.join(tmp, '客户来图B.jpg')
    with open(src, 'wb') as f:
        f.write(_png(24, 24, lambda x, y: (200, 60, 60)))

    old = patch_engine(FakeEngine())
    try:
        r = admin.import_file_via_atag(app, src, move=True)
        ok(r['ok'], '文件版闸门：入库成功')
        ok(not os.path.exists(src), 'move=True 且入库成功 → 源文件删掉')

        # 不是花型 → 源文件留着（不能因为被拒绝就把客户的图删了）
        src2 = os.path.join(tmp, '截图.png')
        with open(src2, 'wb') as f:
            f.write(_png(24, 24, lambda x, y: (250, 250, 250)))
        admin._atag_engine = lambda a: FakeEngine(
            tag={'ok': False, 'not_pattern': True, 'error': '不是花型（截图）。'})
        r = admin.import_file_via_atag(app, src2, move=True)
        ok(not r['ok'] and r['not_pattern'], '不是花型 → 拒绝')
        ok(os.path.exists(src2), '被拒绝的图不能删源文件（客户的资料）')

        # 读不了的文件 → 明确报错，不炸
        r = admin.import_file_via_atag(app, os.path.join(tmp, '不存在.jpg'))
        ok(not r['ok'] and '读不了' in r['error'], '文件读不了 → 明确报错')
    finally:
        admin._atag_engine = old


def t_scan_gate(tmp):
    print('\n[6] 扫目录：开跑前先探活')
    lib = newlib(tmp, 'g3')
    app = FakeApp(lib)
    d = os.path.join(tmp, '待扫')
    os.makedirs(d)
    with open(os.path.join(d, 'a.png'), 'wb') as f:
        f.write(_png(24, 24, lambda x, y: (1, 2, 3)))

    old = patch_engine(FakeEngine(ready=False))
    try:
        r = admin.library_scan(app, {'path': d, 'workers': 2})
        ok(not r['ok'] and '还没就绪' in r['error'], '引擎没起 → 拒绝开跑', r['error'])
        ok(not admin.SCAN_STATE['running'], '没有真起线程（不会白跑一遍）')

        admin._atag_engine = lambda a: FakeEngine(available=False)
        r = admin.library_scan(app, {'path': d})
        ok(not r['ok'], '没装好 → 也拒绝开跑')

        # 就绪了才放行（真跑一次，一张图）
        admin._atag_engine = lambda a: FakeEngine()
        r = admin.library_scan(app, {'path': d, 'workers': 1})
        ok(r.get('ok') and r.get('total') == 1, '就绪 → 放行开跑', r)
        for _ in range(100):
            if not admin.SCAN_STATE['running']:
                break
            import time
            time.sleep(0.1)
        ok(admin.SCAN_STATE['stats'].get('ok') == 1, '扫进来一张',
           admin.SCAN_STATE['stats'])
        ok_eq(len(lib.items), 1, '库里确实多了一张')
    finally:
        admin._atag_engine = old


def t_checks():
    print('\n[7] 概览自检：有这一行，且不泄漏地址')
    from core.config import Config
    from core.app import App
    app = App(who='test', with_runtime=False)
    rows = admin._api_checks(app)
    by = {r['name']: r for r in rows}
    ok('入库自动打标' in by, '自检里有「入库自动打标」')
    row = by['入库自动打标']
    ok(row['ok'] is True, '算法装着 → 就绪', row)
    blob = json.dumps(rows, ensure_ascii=False)
    for bad in ('http://', 'https://', '11435', 'mlx-community'):
        ok(bad not in blob, '自检里没有 %r' % bad)
    ok(all(set(r.keys()) == {'group', 'name', 'ok', 'note', 'info'} for r in rows),
       '每条自检还是那五个字段')

    # 没装好时这一行要如实报未就绪
    real = atag_mod.AtagEngine
    try:
        class NoInstall(AtagEngine):
            @property
            def available(self):
                return False
        atag_mod.AtagEngine = NoInstall
        rows2 = admin._api_checks(app)
        by2 = {r['name']: r for r in rows2}
        ok(by2['入库自动打标']['ok'] is False, '没装好 → 自检报未就绪')
        ok('自动打标签' in by2['入库自动打标']['note'], '话里指出去哪儿装',
           by2['入库自动打标']['note'])
    finally:
        atag_mod.AtagEngine = real

    # state() 里的 atag 段：形状对、没地址
    old = admin._atag_engine
    try:
        admin._atag_engine = lambda a: FakeEngine()
        st = admin.state(app)
        ok(st['atag']['ready'] is True and 'note' in st['atag'], 'state 带 atag 状态')
        admin._atag_engine = lambda a: FakeEngine(ready=False)
        st2 = admin.state(app)
        ok(st2['atag']['ready'] is False and st2['atag']['note'], '没就绪也带回一句人话')
        ok('http' not in json.dumps(st2['atag'], ensure_ascii=False), 'atag 段没有端点地址')
    finally:
        admin._atag_engine = old


def main():
    tmp = tempfile.mkdtemp(prefix='pf_atag_imp_')
    # 真 App 那一节（后台自检）会把图库指到临时目录 —— 项目里踩过：
    # 测试产物混进客户来图那个真图库，手工删了 21 条（见 library.py 的警告）。
    os.environ['PATTERNFLOW_LIBRARY_DIR'] = os.path.join(tmp, 'library')
    try:
        t_normalize()
        t_engine_shell(tmp)
        t_gate(tmp)
        t_file_gate(tmp)
        t_scan_gate(tmp)
        t_checks()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print('\n%s' % ('—' * 58))
    print('通过 %d · 失败 %d' % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
