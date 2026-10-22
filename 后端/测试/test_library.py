#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地花型库回归（离线，不连网、不花钱）。

覆盖：
    [1] 图片尺寸解析（PNG / JPEG，纯标准库）
    [2] 入库：落原图 + 生成缩略图 + 写索引
    [3] 判重：同内容第二次入库不重复；**并发**入库既不撞号也不重复
    [4] 闸门：不是花型图的（截图/人物）不入库；明确跳过校验时留着并标"待复核"
    [5] 模型挂了照样入库（status=untagged），绝不丢图
    [6] 检索：命中维度优先 / 关键词 / matched 判定 / 分页
    [7] 改标签、重打标签、删除（含原图删除与目录守卫）
    [8] search_patterns 把内置款与库图合并，且真图排在前面

跑法：python3 tools/test_library.py
标签由假 tagger 给，全程零网络。
"""
import base64
import json
import os
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.dirname(HERE)
sys.path.insert(0, AGENT)

from core.library import PatternLibrary, image_size          # noqa: E402
from core.imagegen import _png                               # noqa: E402

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


def png(color=(230, 90, 90), size=24):
    return _png(size, size, lambda x, y: color)


def jpeg_stub():
    """造一个最小合法 JPEG 头 + 一个 SOF0 段，只为验证尺寸解析。"""
    head = b'\xff\xd8'
    sof = b'\xff\xc0' + bytes([0x00, 0x11, 0x08]) + \
        (37).to_bytes(2, 'big') + (61).to_bytes(2, 'big') + b'\x03' + b'\x00' * 9
    return head + sof + b'\xff\xd9'


VOCAB = {
    'style': ['法式浪漫', '新中式国风'],
    'element': ['花卉', '几何'],
    'color': ['蓝白青花', '黑白灰'],
    'scene': ['女装连衣裙', '衬衫'],
}


class FakeTagger:
    """假打标签：按调用次数给不同的标签，便于验证归一与覆盖逻辑。

    boom=True 表示一直炸；boom=N 表示前 N 次炸（用来模拟"模型一会儿好一会儿坏"）。
    """

    def __init__(self, seq=None, enabled=True, boom=False):
        self.seq = list(seq or [])
        self.calls = 0
        self._enabled = enabled
        self.boom = boom
        self.seen_bytes = []

    @property
    def enabled(self):
        return self._enabled

    def tag(self, b64, mime='image/png'):
        if self.boom is True:
            raise RuntimeError('模型挂了')
        if isinstance(self.boom, int) and self.boom > 0:
            self.boom -= 1
            raise RuntimeError('模型挂了')
        self.calls += 1
        base = {"ok": True, "name": '测试花型%d' % self.calls,
                "element": '花卉', "color": '蓝白青花', "style": '法式浪漫',
                "scene": '女装连衣裙', "composition": '散点', "density": '中',
                "scale": '中花', "colors": ['靛蓝', '瓷白'],
                "keywords": ['小碎花', '通勤'], "fabric": '雪纺',
                "desc": '一句话说明', "reason": '看着像', "rejected": {},
                "raw": {}, "truncated": False, "usage": None}
        if self.seq:
            base.update(self.seq.pop(0))
        return base

    def describe(self):
        return {"enabled": self._enabled}


def newlib(tmp, tagger=None, **kw):
    return PatternLibrary(None, root=os.path.join(tmp, 'library'),
                          tagger=tagger or FakeTagger(), vocab=VOCAB)


def main():
    tmp = tempfile.mkdtemp(prefix='pf_lib_test_')
    try:
        run_all(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print('\n%s' % ('—' * 58))
    print('通过 %d · 失败 %d' % (PASS, FAIL))
    return 1 if FAIL else 0


def run_all(tmp):
    # ---------- [1] 尺寸解析 ----------
    print('\n[1] 图片尺寸解析')
    p = os.path.join(tmp, 'a.png')
    with open(p, 'wb') as f:
        f.write(png(size=37))
    ok_eq(image_size(p), (37, 37), 'PNG 尺寸读对')
    p2 = os.path.join(tmp, 'b.jpg')
    with open(p2, 'wb') as f:
        f.write(jpeg_stub())
    ok_eq(image_size(p2), (61, 37), 'JPEG 尺寸读对')
    ok_eq(image_size(os.path.join(tmp, 'nope.png')), (0, 0), '文件不存在返回 (0,0)')

    # ---------- [2] 入库 ----------
    print('\n[2] 入库：原图 + 缩略图 + 索引')
    lib = newlib(tmp)
    r = lib.ingest_bytes(png((10, 20, 30)), source='import', src_name='x.png')
    ok(r.get('ok') and not r.get('dup'), '入库成功', r)
    pid = r['id']
    item = r['item']
    ok_eq(item['name'], '测试花型1', '名字来自 AI')
    ok_eq(item['style'], '法式浪漫', '风格写入')
    ok_eq(item['keywords'], ['小碎花', '通勤'], '关键词保留')
    ok_eq(item['status'], 'ready', '状态就绪')
    ok_eq(item['source_label'], '客户来图', '来源中文名')
    ok(os.path.exists(os.path.join(lib.root, item['file'])), '原图落盘')
    ok(os.path.exists(os.path.join(lib.thumb_dir, pid + '.jpg')), '缩略图生成')
    ok_eq(item['image'], '/api/library/file/%s?thumb=1' % pid, '缩略图地址')
    ok_eq(item['full'], '/api/library/file/%s' % pid, '原图地址')
    ok(os.path.exists(lib.index_path), '索引落盘')
    with open(lib.index_path, encoding='utf-8') as f:
        raw = json.load(f)
    ok_eq(raw['count'], 1, '索引里 1 条')

    # ---------- [3] 判重 ----------
    print('\n[3] 判重（含并发）')
    same = png((10, 20, 30))
    r2 = lib.ingest_bytes(same, source='import')
    ok(r2.get('dup') and r2.get('id') == pid, '同内容第二次判重，不新增')
    ok_eq(lib.stats()['count'], 1, '库里仍然只有 1 张')

    lib2 = newlib(tmp + '_x')
    blob = png((200, 100, 50), size=28)
    results, lock = [], threading.Lock()

    def worker():
        r = lib2.ingest_bytes(blob, source='import')
        with lock:
            results.append(r)

    ts = [threading.Thread(target=worker) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    fresh = [r for r in results if r.get('ok') and not r.get('dup')]
    ok_eq(len(fresh), 1, '6 路并发同一张图 → 只有 1 次真入库')
    ok_eq(len(set(r['id'] for r in fresh)), 1, '只占了一个 id')
    ok_eq(lib2.stats()['count'], 1, '库里只有 1 条记录')
    ok_eq(len(os.listdir(lib2.orig_dir)), 1, 'orig/ 只有一个文件（没写重）')

    lib3 = newlib(tmp + '_y', tagger=FakeTagger(enabled=False))
    blobs = [png((i * 20 + 10, 40, 40), size=26) for i in range(5)]
    res3, lock3 = [], threading.Lock()

    def worker3(b):
        r = lib3.ingest_bytes(b, source='import')
        with lock3:
            res3.append(r)

    ts = [threading.Thread(target=worker3, args=(b,)) for b in blobs]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    ids = [r['id'] for r in res3 if r.get('ok')]
    ok_eq(len(ids), 5, '5 张不同的图并发入库全部成功')
    ok_eq(len(set(ids)), 5, '5 个 id 互不重复')
    ok_eq(len(os.listdir(lib3.orig_dir)), 5, '5 个原图文件')

    # ---------- [4] 非花型闸门 ----------
    print('\n[4] 非花型图闸门')
    lib4 = newlib(tmp + '_z', tagger=FakeTagger(seq=[
        {"ok": False, "not_pattern": True, "reason": '是人物照片'}]))
    r4 = lib4.ingest_bytes(png((250, 250, 250)), source='import')
    ok(not r4.get('ok') and r4.get('not_pattern'), '非花型 → 不入库', r4)
    ok_eq(lib4.stats()['count'], 0, '库里没加东西')
    ok_eq(len(os.listdir(lib4.orig_dir)), 0, '原图也没留下')

    lib5 = newlib(tmp + '_w', tagger=FakeTagger(seq=[
        {"ok": False, "not_pattern": True, "reason": '不像花型'}]))
    r5 = lib5.ingest_bytes(png((250, 250, 250)), source='ai-gen', check_pattern=False)
    ok(r5.get('ok'), 'AI 产出不校验图案来源 → 照样入库')
    ok_eq(r5['item']['status'], 'need_review', '但标成待复核，不冒充好图')

    # ---------- [5] 模型挂了不丢图 ----------
    print('\n[5] 打标签失败不丢图')
    lib6 = newlib(tmp + '_v', tagger=FakeTagger(boom=1))
    r6 = lib6.ingest_bytes(png((11, 22, 33)), source='import')
    ok(r6.get('ok'), '模型炸了照样入库')
    ok_eq(r6['item']['status'], 'untagged', '状态是待打标签')
    ok(r6['item'].get('tag_error'), '错误记下来了')
    ok(os.path.exists(os.path.join(lib6.root, r6['item']['file'])), '原图在')
    r6b = lib6.retag(r6['id'])
    ok(r6b.get('ok'), '模型恢复后能重新打标签')
    ok_eq(r6b['item']['status'], 'ready', '重打后状态就绪')

    # ---------- [6] 检索 ----------
    print('\n[6] 检索')
    lib7 = newlib(tmp + '_s', tagger=FakeTagger(seq=[
        {"name": '青花缠枝', "style": '新中式国风', "element": '花卉', "color": '蓝白青花',
         "scene": '女装连衣裙', "keywords": ['青花', '对花'], "colors": ['靛蓝']},
        {"name": '灰色几何', "style": '法式浪漫', "element": '几何', "color": '黑白灰',
         "scene": '衬衫', "keywords": ['通勤'], "colors": ['灰']},
        {"name": '蓝白小簇', "style": '新中式国风', "element": '花卉', "color": '蓝白青花',
         "scene": '衬衫', "keywords": ['小碎花'], "colors": ['蓝', '白']},
    ]))
    for i in range(3):
        lib7.ingest_bytes(png((i * 30 + 10, 60, 90), size=22), source='import')

    res = lib7.search(brief={'style': '新中式国风', 'element': '花卉', 'color': '蓝白青花'})
    ok(res['items'][0]['hit'] == 3 and res['items'][1]['hit'] == 3,
       '两款三项全中，并列排最前')
    ok(sorted([res['items'][0]['name'], res['items'][1]['name']]) == ['蓝白小簇', '青花缠枝'],
       '排前面的正是这两款')
    ok_eq(res['items'][0]['hit'], 3, '三项全中 hit=3')
    ok(res['matched'], '多维度命中 ≥2 → matched')

    res = lib7.search(brief={'color': '蓝白青花'})
    ok_eq(res['best_hit'], 1, '只给一维 → 命中 1')
    ok(res['matched'], '单维度命中 1 就算对上')

    res = lib7.search(brief={'style': '法式浪漫', 'color': '蓝白青花'})
    ok(not res['matched'], '两个维度只中 1 个 → 不算对上（沾边而已）')
    ok_eq(res['count'], 3, '非严格模式：沾边的也带回来当参考')

    res = lib7.search(brief={'style': '新中式国风'}, strict=True)
    ok_eq(res['total'], 2, '严格模式：只给风格对得上的 2 张')
    ok(all(i['style'] == '新中式国风' for i in res['items']), '严格模式下不混别的风格')
    res = lib7.search(brief={'style': '法式浪漫'}, strict=True)
    ok_eq(res['total'], 1, '严格模式按其它风格也筛得准')
    res = lib7.search(brief={'style': '法式浪漫', 'color': '蓝白青花'}, strict=True)
    ok_eq(res['total'], 0, '严格模式下两个条件都要满足')

    res = lib7.search(q='对花')
    ok_eq(res['items'][0]['name'], '青花缠枝', '关键词命中排前')
    res = lib7.search(q='对花', strict=True)
    ok_eq(res['total'], 1, '严格模式下关键词只留命中的')
    res = lib7.search(q='不存在的词')
    ok_eq(res['count'], 3, '关键词没命中也不报错，按热度返回')

    res = lib7.search(brief={'element': '花卉'}, limit=1)
    ok_eq(res['count'], 1, 'limit 生效')
    ok_eq(res['total'], 3, 'total 是分页前总数')
    res = lib7.search(brief={'element': '花卉'}, limit=1, offset=1)
    ok_eq(res['count'], 1, 'offset 翻页')

    res = lib7.search(source='ai-gen')
    ok_eq(res['total'], 0, '按来源筛（库里全是 import）')

    # ---------- [7] 改 / 删 ----------
    print('\n[7] 改标签与删除')
    pid7 = lib7.items[0]['id']
    u = lib7.update(pid7, {'name': '我改的名字', 'color': '黑白灰',
                           'keywords': '显瘦，通勤, 对花', 'id': 'HACK', 'file': 'HACK'})
    ok(u['ok'], '改标签成功')
    ok_eq(u['item']['name'], '我改的名字', '名字改了')
    ok_eq(u['item']['color'], '黑白灰', '配色改了')
    ok_eq(u['item']['keywords'], ['显瘦', '通勤', '对花'], '关键词字符串拆成数组')
    ok_eq(u['item']['id'], pid7, 'id 不许被改（白名单挡住）')
    ok_eq(u['item']['tag_mode'], 'manual', '标记成人工改过')
    res = lib7.search(brief={'color': '黑白灰'})
    ok(any(i['id'] == pid7 for i in res['items']), '改完立刻按新标签检索到')

    before = len(os.listdir(lib7.orig_dir))
    d = lib7.remove(pid7)
    ok(d['ok'], '删除成功')
    ok_eq(len(lib7.orig_dir and os.listdir(lib7.orig_dir)), before - 1, '原图删掉了')
    ok(not os.path.exists(os.path.join(lib7.thumb_dir, pid7 + '.jpg')), '缩略图删掉了')
    ok_eq(lib7.stats()['count'], 2, '库里少一条')
    ok(lib7.get(pid7) is None, '查不到了')
    ok(not lib7.remove(pid7)['ok'], '删不存在的返回失败')

    # ---------- [8] search_patterns 合并 ----------
    print('\n[8] 检索技能合并内置款与库图')
    from core.skills import SkillRegistry
    skills = os.path.join(AGENT, 'skills')
    reg = SkillRegistry(skills)
    lib8 = newlib(tmp + '_m', tagger=FakeTagger(seq=[
        {"name": '库里的青花', "style": '新中式国风', "element": '花卉', "color": '蓝白青花',
         "scene": '衬衫'}]))
    lib8.ingest_bytes(png((33, 66, 99)), source='import')
    ctx = {
        'patterns': [{"id": 'PF-OWN-1', "name": '内置青花', "style": '新中式国风',
                      "element": '花卉', "color": '蓝白青花', "scene": '丝巾',
                      "heat": 90, "desc": '', "l2": []}],
        'library': lib8,
        'brief': {'style': '新中式国风', 'element': '花卉', 'color': '蓝白青花'},
    }
    out = reg.run('search_patterns', {}, ctx)
    ok_eq(out['count'], 2, '内置款 + 库图都出来了')
    ok(out['items'][0].get('lib') is True, '真图排在最前面')
    ok(out['items'][0]['image'].startswith('/api/library/'), '真图带图片地址')
    ok_eq(out['items'][1].get('lib'), False, '内置款排在后面')
    ok_eq(out['lib_count'], 1, 'lib_count 统计对')
    ok(out['matched'], '合并后仍判 matched')

    out2 = reg.run('search_patterns', {}, {'patterns': [], 'library': lib8, 'brief': {}})
    ok_eq(out2['lib_total'], 1, '没维度时也报出库内总数')

    # 库检索抛异常不能把整个检索带崩
    class BadLib:
        enabled = True

        def search(self, **kw):
            raise RuntimeError('库炸了')

    out3 = reg.run('search_patterns', {}, {'patterns': ctx['patterns'],
                                           'library': BadLib(),
                                           'brief': {'style': '新中式国风'}})
    ok_eq(out3['count'], 1, '库挂了仍返回内置款')
    ok('库炸了' in (out3['note'] or ''), 'note 里说明了库没检索成功')

    # ---------- [9] 出图产出自动入库 ----------
    print('\n[9] AI 出图后自动进库（后台，不拖慢返回）')
    os.environ['PATTERNFLOW_IMAGE_PROVIDER'] = 'mock'
    # 出图产物挪去临时目录，别堆进项目的 generated/
    os.environ['PATTERNFLOW_IMAGE_OUTDIR'] = tempfile.mkdtemp(prefix='pf-test-img-')
    # 图库目录也挪走：出图会自动入库，别把测试产物混进真图库
    os.environ['PATTERNFLOW_LIBRARY_DIR'] = tempfile.mkdtemp(prefix='pf-test-lib-')
    # 埋点也挪走：出图会记一笔，混进真看板就成了假数据
    os.environ['PATTERNFLOW_TELEMETRY_DIR'] = tempfile.mkdtemp(prefix='pf-test-tel-')
    from core.config import Config                          # noqa: E402
    from core.persona import load_all as load_personas      # noqa: E402
    from core.skills import SkillRegistry as SR2            # noqa: E402
    from core.session import SessionStore                   # noqa: E402
    from core.runtime import AgentRuntime                   # noqa: E402
    cfg = Config()
    data = {'patterns': [], 'trends': [],
            'vocab': {"style": ['法式浪漫'], "element": ['花卉'],
                      "color": ['蓝白青花'], "scene": ['丝巾']}}
    lib9 = PatternLibrary(cfg, root=os.path.join(tmp, 'auto'), tagger=FakeTagger(), vocab=VOCAB)
    rt = AgentRuntime(cfg, load_personas(cfg.path('personas')),
                      SR2(os.path.join(AGENT, 'skills')),
                      SessionStore(os.path.join(tmp, 'sess')), data, library=lib9)
    sid = 'auto1'
    sess = rt.sessions.get(sid)
    sess.brief.update({'style': '法式浪漫', 'element': '花卉'})

    ok_eq(lib9.stats()['count'], 0, '出图前库是空的')
    res = rt.run_action(sid, 'generate_pattern',
                        {'n': 2, 'prompt_full': '测试用花型，蓝白小碎花'}, 'designer')
    ok(res.get('ok') and res.get('images'), '出图成功', res.get('error'))
    ok_eq(len(res['images']), 2, '出了 2 张')
    ok_eq(lib9.stats()['count'], 0, '返回时还没入库（是后台在收，不占等待时间）')
    for _ in range(60):                       # 等后台线程打完标签
        if lib9.stats()['count'] >= 2:
            break
        time.sleep(0.2)
    ok_eq(lib9.stats()['count'], 2, '两张图都自动进库了')
    ok(all(p['source'] == 'ai-gen' for p in lib9.items), '来源标成 AI 生成')
    ok(all(p.get('name') for p in lib9.items), '每张都有自己的名字')
    hit = lib9.search(brief={'style': '法式浪漫', 'element': '花卉'})
    ok(hit['matched'] and hit['total'] == 2, '出完图马上就能按标签检索到')

    # 关掉开关就不该再自动收
    lib9.auto_ingest = False
    before = lib9.stats()['count']
    rt.run_action(sid, 'generate_pattern', {'n': 1, 'prompt_full': '再来一张'}, 'designer')
    time.sleep(1.5)
    ok_eq(lib9.stats()['count'], before, 'auto_ingest=false 时不入库')

    # 库里挂了也不能影响出图
    class BoomLib(PatternLibrary):
        def ingest_file(self, *a, **k):
            raise RuntimeError('库炸了')

    lib10 = BoomLib(cfg, root=os.path.join(tmp, 'boom'), tagger=FakeTagger(), vocab=VOCAB)
    rt.library = lib10
    res = rt.run_action(sid, 'generate_pattern', {'n': 1, 'prompt_full': 'x'}, 'designer')
    time.sleep(1.0)
    ok(res.get('ok') and res.get('images'), '入库炸了照样把图给用户')

    # ---------- [10] 图库目录能被环境变量覆盖 ----------
    # 跑实链路 e2e 时真会出图，出图又会自动入库 —— 没有这个开关，
    # 测试产物会一批批混进客户来图那个真图库（2026-10-06 已踩过一次）。
    print('\n[10] 图库目录可用环境变量覆盖（e2e 隔离开关）')
    env_dir = os.environ.get('PATTERNFLOW_LIBRARY_DIR')
    ok(bool(env_dir), '本进程已把图库指到临时目录', env_dir)
    lib_env = PatternLibrary(Config())
    ok(os.path.abspath(lib_env.root) == os.path.abspath(env_dir or ''),
       'PatternLibrary 认的是环境变量给的目录（不是配置里的）', lib_env.root)
    ok(lib_env.index_path.startswith(os.path.abspath(env_dir or '\x00')),
       '索引也跟着搬过去了', lib_env.index_path)

    # ---------- [11] 产物入库：处理好的图过算法闸门，上传的原图不进库 ----------
    # 2026-10-12 用户定的规矩：库里存的是"处理好的图"（描稿仿稿/四方连续的
    # 成品），不是客户上传来做处理的照片。成品入库前和后台导入同一条闸门
    # （core/atag.py）：没过自动打标签算法不许存；上传/交过来的底图则一张
    # 都不该出现在库里。
    print('\n[11] 描稿/四方连续的成品过算法闸门入库；上传的原图不进库')
    # 前面的用例把库换成过 BoomLib、也把 auto_ingest 关过 —— 先还原成"能正常入库"
    # 的状态再验，否则下面的用例会因为"库本来就收不进去"而**假通过**。
    lib9.auto_ingest = True
    rt.library = lib9
    sk = rt.registry.get('seamless_pattern')
    ok(sk is not None, 'skills/ 里能挂上 seamless_pattern')
    ok(rt._skill_ingests('seamless_pattern') is True,
       'seamless_pattern 现在声明入库（成品要留档）', getattr(sk, 'ingest', None))
    ok(rt._skill_ingests('layer_psd') is False,
       'layer_psd 仍声明不入库（交付的是文件不是图）')
    from core.runtime import INGEST_SOURCE, GATED_SOURCES          # noqa: E402
    ok_eq(INGEST_SOURCE.get('seamless_pattern'), 'seamless', '四方连续成品记来源 seamless')
    ok_eq(INGEST_SOURCE.get('trace_pattern'), 'trace', '描稿成品记来源 trace')
    ok('trace' in GATED_SOURCES and 'seamless' in GATED_SOURCES,
       '这两个来源的入库走算法闸门', GATED_SOURCES)

    # 假打标引擎 + "一张图只打一次"：闸门给了 preset_tags 就不许再问云端 tagger
    class FakeAtag:
        """假打标引擎。available/ready/tag 三个旋钮覆盖闸门的全部分支。"""

        def __init__(self, available=True, ready=True, tag=None):
            self.available = available
            self._ready = ready
            self._tag = tag if tag is not None else dict(ATAGGED, ok=True)
            self.missing_note = '假引擎没装好'
            self.probe_calls = 0
            self.tagged = []

        def probe(self, fresh=False):
            self.probe_calls += 1
            return {'ok': self._ready, 'ready': self._ready,
                    'note': '就绪' if self._ready else '引擎没起'}

        def tag_blob(self, blob):
            self.tagged.append(blob)
            return self._tag

        def to_library_tags(self, res):
            from core.atag import AtagEngine
            return AtagEngine.to_library_tags(res)

    class NeverTagger:
        """闸门路径下**不允许**被调用 —— 一张图只该被打一次标签。"""

        def tag(self, *a, **kw):
            raise AssertionError('preset_tags 给了还调云端 tagger，一张图被打了两遍')

        def describe(self):
            return {'enabled': False}

    import hashlib                                                  # noqa: E402
    ATAGGED = {
        'status': 'ok', 'style': ['法式浪漫'],
        'element': ['花卉'], 'color': ['蓝白青花'], 'scene': ['女装连衣裙'],
        'free': {}, 'colors': ['靛蓝', '瓷白'], 'keywords': ['小碎花'],
        'desc': '蓝白花卉散点', 'reason': '看着像', 'confidence': 0.9,
    }
    lib9._tagger = NeverTagger()
    fake_atag = FakeAtag()
    rt.atag = fake_atag

    # 造一份**真实存在**的产物文件再走一遍收口 —— 文件不存在时 _auto_ingest 会
    # 自己跳过，那样即使闸门坏了这个用例也会假通过。
    outdir = os.environ['PATTERNFLOW_IMAGE_OUTDIR']
    shot = os.path.join(outdir, 'seamless-fake.png')
    with open(shot, 'wb') as f:
        f.write(png())
    n0 = lib9.stats()['count']
    rt._settle(sess, 'engineer', 'seamless_pattern', {'confirmed': True},
               {'ok': True, 'engine': 'pure',
                'images': [{'name': 'seamless-fake.png', 'path': shot,
                            'url': '/api/file/seamless-fake.png',
                            'mime': 'image/png', 'bytes': os.path.getsize(shot)}]})
    for _ in range(60):                   # 等后台线程过完闸门
        if lib9.stats()['count'] > n0:
            break
        time.sleep(0.2)
    ok_eq(lib9.stats()['count'], n0 + 1, '四方连续的成品进库了')
    if lib9.stats()['count'] == n0 + 1:
        it = lib9.items[-1]
        ok_eq(it['source'], 'seamless', '来源标成四方连续')
        ok_eq(it.get('style'), '法式浪漫', '标签来自算法闸门（preset_tags）')
        ok_eq(it.get('color'), '蓝白青花', '四维标签照闸门给的落')
        ok_eq(it.get('tag_engine'), 'atag', '标记出是算法打的标')
    ok(len(fake_atag.tagged) == 1, '闸门真跑了打标（一次）', len(fake_atag.tagged))

    # 描稿成品同理（同一条闸门）
    shot2 = os.path.join(outdir, 'trace-fake.png')
    with open(shot2, 'wb') as f:
        f.write(png((12, 80, 160)))
    n1 = lib9.stats()['count']
    rt._settle(sess, 'engineer', 'trace_pattern', {'confirmed': True},
               {'ok': True,
                'images': [{'name': 'trace-fake.png', 'path': shot2,
                            'url': '/api/file/trace-fake.png',
                            'mime': 'image/png', 'bytes': os.path.getsize(shot2)}]})
    for _ in range(60):
        if lib9.stats()['count'] > n1:
            break
        time.sleep(0.2)
    ok_eq(lib9.stats()['count'], n1 + 1, '描稿的成品也进库了')
    if lib9.stats()['count'] == n1 + 1:
        ok_eq(lib9.items[-1]['source'], 'trace', '来源标成描稿仿稿')

    # 引擎没就绪：成品不入库，但用户照样拿到图（库少一份，不能卡交付）
    fake_atag._ready = False
    shot3 = os.path.join(outdir, 'seamless-fake2.png')
    with open(shot3, 'wb') as f:
        f.write(png((200, 120, 20)))
    n2 = lib9.stats()['count']
    res = rt._settle(sess, 'engineer', 'seamless_pattern', {'confirmed': True},
                     {'ok': True, 'engine': 'pure',
                      'images': [{'name': 'seamless-fake2.png', 'path': shot3,
                                  'url': '/api/file/seamless-fake2.png',
                                  'mime': 'image/png',
                                  'bytes': os.path.getsize(shot3)}]})
    time.sleep(1.2)
    ok(res.get('ok') is True and res.get('images'), '引擎没就绪也照样把成品给用户')
    ok_eq(lib9.stats()['count'], n2, '引擎没就绪 → 没打过标，不准入库')
    fake_atag._ready = True

    # 不是花型：闸门拦下，不入库
    fake_atag._tag = {'ok': False, 'not_pattern': True,
                      'error': '这张图看起来不是面料花型（画面与花型无关）。'}
    shot4 = os.path.join(outdir, 'seamless-fake3.png')
    with open(shot4, 'wb') as f:
        f.write(png((90, 90, 90)))
    n3 = lib9.stats()['count']
    rt._settle(sess, 'engineer', 'seamless_pattern', {'confirmed': True},
               {'ok': True, 'engine': 'pure',
                'images': [{'name': 'seamless-fake3.png', 'path': shot4,
                            'url': '/api/file/seamless-fake3.png',
                            'mime': 'image/png',
                            'bytes': os.path.getsize(shot4)}]})
    time.sleep(1.2)
    ok_eq(lib9.stats()['count'], n3, '算法说不是花型 → 不入库（库不是相册）')
    fake_atag._tag = dict(ATAGGED, ok=True)

    # 客户上传 / 交过来的底图：一张都不该进库（库里只有"处理好的图"）
    seen = []
    _real_ingest = lib9.ingest_bytes

    def _spy_ingest(blob, *a, **kw):
        seen.append(hashlib.md5(blob).hexdigest())
        return _real_ingest(blob, *a, **kw)

    lib9.ingest_bytes = _spy_ingest
    try:
        import base64 as _b64
        photo_blob = png((250, 30, 30))
        rt._remember_image(sess, _b64.b64encode(photo_blob).decode(),
                           'image/png', 'upload')
        rt.hand_off(sid, '/api/file/' + os.path.basename(shot), 'engineer')
        time.sleep(0.5)
        ok(hashlib.md5(photo_blob).hexdigest() not in seen,
           '客户上传的照片没有进库')
        ok(all(m != hashlib.md5(photo_blob).hexdigest() for m in seen),
           '交到底图槽里的图也不会被顺手收进库')
    finally:
        lib9.ingest_bytes = _real_ingest

    # ---------- [12] 客服助理接上了真检索 ----------
    # 2026-10-13 用户提的：客服以前只有 get_pattern_detail（客户得先报编号），
    # 描述需求它只能干说"转给设计师"。现在配了 search_patterns ——
    # 这条用例钉住"客户跟客服说一句话，真的会去查库"，不是配了名字没接线。
    # 2026-10-14：客服不再管趋势（用户要求客服对话里不要趋势与花型风格）。
    print('\n[12] 客服助理：客户一句话 → 真去查库（不再只会转交）')
    sk_assist = rt.cfg.agent_conf('assistant').get('skills') or []
    ok('search_patterns' in sk_assist, '客服配了检索技能', sk_assist)
    ok('get_pattern_detail' in sk_assist, '单款详情也在', sk_assist)
    ok('list_trends' not in sk_assist, '趋势从客服摘掉了（要方向找花型设计师）', sk_assist)
    ok(not ({'generate_pattern', 'edit_pattern', 'trace_pattern',
             'seamless_pattern', 'layer_psd'} & set(sk_assist)),
       '客服不许出图/跑算法（专业活转交）', sk_assist)
    ok('assistant' in (rt.cfg.settings['kb'].get('agents') or []),
       '知识库仍只开给客服（问答 + 检索组合拳）',
       rt.cfg.settings['kb'].get('agents'))
    from core.skills import SkillRegistry                          # noqa: E402
    reg_all = SkillRegistry(os.path.join(AGENT, 'skills'))
    ok(all(n in reg_all.skills for n in sk_assist),
       '配的技能名都真实存在（拼错一个线上就静默少个能力）',
       [n for n in sk_assist if n not in reg_all.skills])
    # 线上 settings.json 与默认配置一致（改一处忘一处是老毛病）
    live = json.load(open(os.path.join(os.path.dirname(HERE), 'config',
                                       'settings.json'), encoding='utf-8'))
    live_sk = ((live.get('agents') or {}).get('assistant') or {}).get('skills') or []
    ok(live_sk == sk_assist, '线上 settings.json 跟默认配置一致（防漂移）', live_sk)

    class SearchLLM:
        """照客服的真实用法：第一轮喊检索，第二轮收口。"""
        def __init__(self):
            self.round = 0
            self.seen_tools = None
            self.enabled = True

        def chat(self, messages, tools=None):
            self.round += 1
            self.seen_tools = [t['function']['name'] for t in (tools or [])]
            if self.round == 1:
                return {'content': '', 'tool_calls': [
                    {'id': 'c1', 'type': 'function',
                     'function': {'name': 'search_patterns',
                                  'arguments': json.dumps(
                                      {'style': '法式浪漫', 'element': '花卉'})}}]}
            return {'content': '库里有两款对得上，你看看合不合适。',
                    'tool_calls': []}

    fake_llm = SearchLLM()
    real_llm = rt.llm
    rt.llm = fake_llm
    try:
        out = rt.chat('assistant', '有没有法式浪漫花卉款的花型', session_id=sid)
    finally:
        rt.llm = real_llm
    ok('search_patterns' in (out.get('skills') or []),
       '客服真的去查库了（不是干说）', out.get('skills'))
    ok(bool(out.get('picks')), '查到的款带回给前端（带图，界面直接展示）',
       out.get('picks'))
    ok(bool(out.get('picks_items')), '完整资料也带上了（真图要靠它拿地址）')
    ok(fake_llm.seen_tools and 'search_patterns' in fake_llm.seen_tools,
       '检索工具确实喂给了这个角色', fake_llm.seen_tools)
    ok(not out.get('pending'), '检索免费无确认卡片（不弹扣费闸门）')
    ok(out.get('reply'), '有回复文案', out.get('reply'))


if __name__ == '__main__':
    sys.exit(main())
