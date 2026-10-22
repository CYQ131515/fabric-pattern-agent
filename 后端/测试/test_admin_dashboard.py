#!/usr/bin/env python3
"""后台「接口自检」与「数据看板」的离线单测。

跑法：python3 测试/test_admin_dashboard.py

盯三件事：
  1. 自检清单要**覆盖所有接口**（少一个就是"页面上看着全绿、其实没配"）；
  2. 自检的返回值里**不能出现网关地址和密钥**（连掩码也不行）——
     这一页经常被截图丢群里，泄漏一次就是事故；
  3. 看板的聚合数要算得对（点数 = 报价 × 次数、按天归集、空数据不崩）。

用假的 app/cfg，不碰真项目数据，也不需要起服务。
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from core import admin as adminapi            # noqa: E402

PASS = FAIL = 0


def ok(cond, label, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('  ✓ ' + label)
    else:
        FAIL += 1
        print('  ✗ ' + label + ('  → %s' % (extra,) if extra else ''))


# ---------------------------------------------------------------- 假对象
class FakeCfg:
    def __init__(self, settings, secrets, paths=None):
        self.settings = settings
        self.secrets = secrets
        self._paths = paths or {}
        self.model = (settings.get('llm') or {}).get('model') or ''
        self.base_url = (settings.get('llm') or {}).get('base_url') or ''
        self.api_key = (secrets.get('api_key') or '').strip()

    @property
    def llm_enabled(self):
        return bool(self.api_key)

    def path(self, key):
        return self._paths.get(key, '/tmp/nonexistent-' + key)


class FakeLib:
    def __init__(self, items):
        self.items = items

    def stats(self):
        by_source, by_status = {}, {}
        for p in self.items:
            s = p.get('source') or 'import'
            by_source[s] = by_source.get(s, 0) + 1
            t = p.get('status') or 'ready'
            by_status[t] = by_status.get(t, 0) + 1
        return {"count": len(self.items), "by_source": by_source,
                "by_status": by_status, "root": '/x', "enabled": True}


class FakeKB:
    def __init__(self, docs=0, qa=0):
        self.docs = list(range(docs))
        self.qa = list(range(qa))


class FakeGen:
    def __init__(self, outdir, enabled=True, key='k', cost=60, variants=4):
        self.outdir = outdir
        self.enabled = enabled
        self.api_key = key
        self.cost = cost
        self.variants = variants


class FakeReg:
    def __init__(self, costs):
        self.skills = {k: None for k in costs}
        self._costs = costs

    def describe(self):
        return [{"name": k, "cost": v, "description": '', "version": '0',
                 "confirm": False, "ingest": True} for k, v in self._costs.items()]


class FakeApp:
    def __init__(self, cfg, items, outdir, costs, docs=0, qa=0,
                 img_key='img-k', trace_key=''):
        self.cfg = cfg
        self.library = FakeLib(items)
        self.kb = FakeKB(docs, qa)
        self.imagegen = FakeGen(outdir, key=img_key)
        self.tracegen = FakeGen(outdir, key=trace_key or img_key)
        self.registry = FakeReg(costs)


SETTINGS = {
    "llm": {"base_url": "https://api.example.com/v1", "model": "m-1"},
    "vision": {"enabled": True, "model": ""},
    "image": {"enabled": True, "provider": "volcengine",
              "base_url": "https://ark.example.com/api/v3",
              "model": "img-1", "cost": 60, "variants": 4},
    "trace": {"provider": "", "base_url": "", "model": ""},
    "seamless": {"enabled": True, "cost": 30, "base_url": "https://se.example.com/v1",
                 "analyze_base": "", "image_model": "se-img",
                 "analyze_model": "se-an"},
    "psdlayer": {"enabled": True, "cost": 45, "max_layers": 4,
                 "vlm_base": "", "vlm_model": ""},
    # 本文件要测「没配就如实报未就绪」，所以把新算法目录指到不存在的地方，
    # 否则算法目录自带 .runtime + .env，会真的被判成可用。
    "algo": {"dir": "/tmp/nonexistent-algo-for-tests"},
    "kb": {"enabled": True, "agents": ["assistant"]},
    "agents": {}, "paths": {},
}
SECRETS = {"api_key": "sk-REALKEY123456", "image_api_key": "sk-IMGKEY123456",
           "seamless_api_key": "sk-SEAMLESS123456",
           "runninghub_api_key": "rh-RUNNINGHUB123456"}

COSTS = {'update_brief': 0, 'search_patterns': 0, 'generate_pattern': 60,
         'edit_pattern': 60, 'trace_pattern': 60, 'seamless_pattern': 30,
         'layer_psd': 45, 'get_pattern_detail': 0, 'list_trends': 0}


def write_sessions(d, rows):
    """rows: [(sid, [(type, payload), ...]), ...]"""
    os.makedirs(d, exist_ok=True)
    for sid, recs in rows:
        with open(os.path.join(d, sid + '.jsonl'), 'w', encoding='utf-8') as f:
            for rec in recs:
                f.write(json.dumps(rec, ensure_ascii=False) + '\n')


def turn(ts, agent, skills, user=''):
    return {"type": "turn",
            "turn": {"t": ts, "agent": agent, "user": user, "reply": 'ok',
                     "skills": skills}}


DAY = 86400
# NOW 必须落在**真实的今天**：analytics 的按天窗口是拿真实 time.time() 切的
# （admin.analytics 里 now = time.time()）。曾经把 fixture 钉死在某个过去日期
# （2026-10-12），当天全绿；午夜一过，窗口最后一天变成空的今天，
# 「最后一天 3 轮」那一组断言集体飘走（2026-10-13 凌晨真炸过一次，4 条）。
# 所以取真实当前时间，偏移仍按 DAY 推算 —— 「今天/昨天/4 天前」的含义不变。
import time as _t                                        # noqa: E402
NOW = _t.time()


def main():
    tmp = tempfile.mkdtemp(prefix='pf-dash-test-')
    sess = os.path.join(tmp, 'sessions')
    outdir = os.path.join(tmp, 'generated')
    os.makedirs(outdir, exist_ok=True)

    # 两天的会话：4 天前 1 轮、昨天 2 轮、今天 3 轮
    write_sessions(sess, [
        ('s1', [
            {"type": "brief", "brief": {"style": "法式浪漫", "element": "花卉",
                                        "color": "蓝白青花", "scene": "丝巾"}},
            turn(NOW - 4 * DAY, 'designer', ['update_brief'], '先看看'),
            turn(NOW - DAY, 'designer', ['update_brief', 'search_patterns'],
                 '想要蓝白青花的丝巾'),
            turn(NOW - DAY, 'engineer', ['seamless_pattern'], '做成四方连续'),
        ]),
        ('s2', [
            {"type": "brief", "brief": {"style": "法式浪漫", "element": "花卉"}},
            turn(NOW, 'designer', ['update_brief', 'generate_pattern'], '出四张'),
            turn(NOW, 'designer', ['search_patterns'], '有没有蓝白青花的'),
            turn(NOW, 'engineer', ['layer_psd'], '分个层'),
        ]),
    ])
    # 出图目录：今天 2 个文件
    for i in range(2):
        p = os.path.join(outdir, 'gen-%d.png' % i)
        open(p, 'wb').write(b'x')
        os.utime(p, (NOW, NOW))

    items = [
        {"style": "法式浪漫", "element": "花卉", "color": "蓝白青花",
         "scene": "丝巾", "colors": ["靛蓝", "白色"], "source": "import",
         "status": "ready", "added": NOW - DAY},
        {"style": "法式浪漫", "element": "几何", "color": "黑白灰",
         "scene": "衬衫", "colors": ["黑色"], "source": "ai-gen",
         "status": "ready", "added": NOW},
        {"style": "童趣插画", "element": "花卉", "color": "马卡龙甜彩",
         "scene": "童装", "colors": ["靛蓝"], "source": "import",
         "status": "untagged", "added": NOW},
    ]

    cfg = FakeCfg(SETTINGS, SECRETS, paths={'sessions': sess})
    app = FakeApp(cfg, items, outdir, COSTS, docs=1, qa=2,
                  img_key='sk-IMGKEY123456', trace_key='')
    cfg.api_key = SECRETS['api_key']

    try:
        # ---------------------------------------------------------- 自检
        print('[1] 接口自检：覆盖所有接口')
        rows = adminapi._api_checks(app)
        names = [r['name'] for r in rows]
        for want in ('对话大模型', '识图模型', '生图接口', '描稿仿稿',
                     '四方连续 · 接缝', '四方连续 · 审查',
                     '分层 PSD · 拆层', '分层 PSD · 结构'):
            ok(want in names, '自检里有「%s」' % want)
        ok(len(rows) >= 8, '至少 8 个接口', str(len(rows)))
        groups = [r['group'] for r in rows]
        for g in ('通用', '花型设计师', '织印师'):
            ok(g in groups, '分了「%s」这一组' % g)

        print('\n[2] 自检不许泄漏网关地址和密钥')
        blob = json.dumps(rows, ensure_ascii=False)
        for bad in ('http://', 'https://', 'sk-', 'rh-', '••', 'api.example',
                    'REALKEY', 'RunningHub Key 尾巴'):
            ok(bad not in blob, '自检里没有 %r' % bad)
        ok(all(set(r.keys()) == {'group', 'name', 'ok', 'note', 'info'}
               for r in rows), '每条自检只有 分组/名称/是否就绪/说明/参数')

        print('\n[3] 没配的接口要如实报未就绪')
        empty = FakeApp(FakeCfg(dict(SETTINGS, llm=dict(SETTINGS['llm'])), {},
                                paths={'sessions': sess}),
                        [], outdir, COSTS, img_key='', trace_key='')
        rows2 = adminapi._api_checks(empty)
        by = {r['name']: r for r in rows2}
        ok(by['对话大模型']['ok'] is False, '没配对话 Key → 未就绪')
        ok(by['生图接口']['ok'] is False, '没配生图 Key → 未就绪')
        ok(by['描稿仿稿']['ok'] is False, '没配描稿 Key → 未就绪')
        ok(by['四方连续 · 接缝']['ok'] is False, '没配四方连续 Key → 未就绪')
        ok(by['分层 PSD · 拆层']['ok'] is False,
           '算法目录不可用 → 分层 PSD 未就绪（不再依赖 RunningHub）')
        ok(by['对话大模型']['note'], '未就绪也有一句人话说明')

        # -------------------------------------------------------- 数据看板
        print('\n[4] 数据看板：KPI 与角色分布')
        an = adminapi.analytics(app, days=7)
        k = an['kpi']
        ok(k['sessions'] == 2, '会话数 2', str(k['sessions']))
        ok(k['turns'] == 6, '对话轮次 6', str(k['turns']))
        ok(k['agents'] == 2, '出现过的角色 2 个', str(k['agents']))
        ok(k['library'] == 3, '花型库 3 张', str(k['library']))
        ok(k['generated'] == 2, '出图文件 2 个', str(k['generated']))
        ok(k['skills'] == len(COSTS), '技能数跟注册表一致')
        ok(k['kb_docs'] == 1 and k['kb_qa'] == 2, '知识库篇数/问答数')
        ok(k['avg_turns'] == 3.0, '平均轮次/会话 = 6÷2 = 3.0', str(k['avg_turns']))
        ok(k['untagged'] == 1, '未打标待处理 1 张', str(k['untagged']))
        ok(dict(an['agents']) == {'designer': 4, 'engineer': 2},
           '角色分布 4/2', str(an['agents']))

        print('\n[4b] 环比：最近一半 vs 前一半')
        cmp = an['compare']
        # days=7 → half=3；近 3 天 = 今天 3 + 昨天 2 + 前天 0 = 5，
        # 前 3 天 = 4 天前那 1 轮 = 1
        ok(cmp['turns']['cur'] == 5 and cmp['turns']['prev'] == 1,
           '对话 近3天 5 次 / 前3天 1 次', str(cmp['turns']))
        ok(cmp['turns']['pct'] == 400, '涨幅 400%', str(cmp['turns']['pct']))
        ok(cmp['gen']['pct'] is None and cmp['gen']['cur'] == 2,
           '上一段没数据时 pct 给 None（不编百分比）', str(cmp['gen']))

        print('\n[5] 数据看板：点数按报价 × 次数估算')
        sk = {s['name']: s for s in an['skills']}
        ok(sk['generate_pattern']['count'] == 1 and sk['generate_pattern']['points'] == 60,
           '生成花型 1 次 60 点', str(sk['generate_pattern']))
        ok(sk['seamless_pattern']['points'] == 30, '四方连续 1 次 30 点')
        ok(sk['layer_psd']['points'] == 45, '分层 PSD 1 次 45 点')
        ok(sk['search_patterns']['points'] == 0, '免费的技能算 0 点')
        ok(k['points'] == 135,
           '预估点数合计 135（60+30+45）', str(k['points']))
        # 改了后台点数，估算要跟着变（不能读 skill.json 里写死的值）
        cfg2 = FakeCfg(dict(SETTINGS, image=dict(SETTINGS['image'], cost=99)),
                       SECRETS, paths={'sessions': sess})
        an2 = adminapi.analytics(FakeApp(cfg2, items, outdir, COSTS), days=7)
        sk2 = {s['name']: s for s in an2['skills']}
        ok(sk2['generate_pattern']['points'] == 99, '改后台单张点数后估算跟着变',
           str(sk2['generate_pattern']['points']))

        print('\n[6] 数据看板：按天归集')
        ser = an['series']
        ok(len(ser) == 7, '近 7 天就有 7 个点', str(len(ser)))
        ok([r['d'] for r in ser] == sorted(r['d'] for r in ser), '日期是升序的')
        ok(ser[-1]['turns'] == 3, '最后一天 3 轮', str(ser[-1]))
        ok(ser[-2]['turns'] == 2 and ser[-2]['seamless'] == 1
           and ser[-2]['ingest'] == 1, '前一天 2 轮 / 1 次四方连续 / 入 1 张',
           str(ser[-2]))
        ok(ser[-1]['gen'] == 2, '最后一天出图 2 个', str(ser[-1]))
        ok(ser[-1]['psd'] == 1, '最后一天 1 次分层 PSD')
        ok(adminapi.analytics(app, days=999)['days'] == 90
           and adminapi.analytics(app, days=0)['days'] == 1, '区间夹在 1~90 天')

        print('\n[7] 数据看板：用户想要什么 / 找了什么')
        dem = an['demand']
        ok(dict(dem['style']) == {'法式浪漫': 2}, '风格 top：法式浪漫 2 次', str(dem['style']))
        ok(dict(dem['color']) == {'蓝白青花': 1}, '配色 top')
        ok(dem['scene'] and dem['scene'][0] == ['丝巾', 1], '场景 top')
        ok(dem['element'][0] == ['花卉', 2], '元素 top：花卉 2 次', str(dem['element']))
        q = dict(an['queries'])
        ok(q.get('蓝白青花') == 2, '检索热词命中「蓝白青花」2 次', str(an['queries']))
        ok(q.get('丝巾') == 1, '检索热词命中「丝巾」1 次')

        print('\n[8] 数据看板：花型库画像与织印师用量')
        L = an['library']
        ok(L['count'] == 3, '库总数')
        ok(dict(L['by_source']) == {'import': 2, 'ai-gen': 1}, '来源分布')
        ok(dict(L['by_status']) == {'ready': 2, 'untagged': 1}, '状态分布')
        ok(L['dims']['style'][0] == ['法式浪漫', 2], '库内风格 top')
        ok(L['colors'][0] == ['靛蓝', 2], '颜色 top：靛蓝 2', str(L['colors']))
        eu = an['engine_usage']
        ok(eu['seamless'] == {'count': 1, 'points': 30}, '四方连续用量', str(eu['seamless']))
        ok(eu['psd'] == {'count': 1, 'points': 45}, '分层 PSD 用量')
        ok(eu['trace'] == {'count': 0, 'points': 0}, '描稿没用过就是 0')

        print('\n[9] 数据看板：账号与充值如实说明没有')
        ac = an['accounts']
        ok(ac['available'] is False, '没有账号/计费后端 → available=False')
        ok(len(ac['reason']) > 10, '给了一句人能看懂的原因')

        print('\n[10] 空数据不崩')
        empty_dir = os.path.join(tmp, 'empty')
        os.makedirs(empty_dir, exist_ok=True)
        cfgE = FakeCfg(SETTINGS, SECRETS, paths={'sessions': empty_dir})
        anE = adminapi.analytics(FakeApp(cfgE, [], empty_dir, COSTS), days=3)
        ok(anE['ok'] is True, '没有会话也没有库，照样返回 ok')
        ok(anE['kpi']['turns'] == 0 and anE['kpi']['library'] == 0, '各项归零')
        ok(len(anE['series']) == 3, '时间线还是给满')
        ok(anE['queries'] == [] and anE['agents'] == [], '空列表不是 null')

        print('\n[11] 会话列表：最后活跃时间要取得到')
        rows = adminapi.session_list(app)['items']
        ok(all(r['last'] for r in rows), '每条都有最后活跃时间（不再恒为空）',
           str([r['last'] for r in rows]))
        ok(rows[0]['id'] == 's2', '按最后活跃倒序，今天的排前面',
           str([r['id'] for r in rows]))
        ok(rows[0]['turns'] == 3, 's2 有 3 轮')
        ok(all('day' in r for r in rows), '每条都带 day（按日期筛要靠它）')

        print('\n[11b] 会话列表：按 ID / 按日期筛')
        f1 = adminapi.session_list(app, q='s2')
        ok([r['id'] for r in f1['items']] == ['s2'], 'ID 片段命中 s2', str(f1['items']))
        ok(f1['matched'] == 1 and f1['total'] == 2, '筛出 1 个，总数仍是 2',
           '%s/%s' % (f1['matched'], f1['total']))
        ok(f1['q'] == 's2', '把查询条件回给前端，好显示"在筛什么"')
        ok([r['id'] for r in adminapi.session_list(app, q='S2')['items']] == ['s2'],
           'ID 搜索不分大小写')
        ok(adminapi.session_list(app, q='搜不到的东西')['items'] == [], '搜不到就是空列表')
        # 时区一致：会话里的 t 和这里的日期都由同一台机器算，所以直接比字面
        # （fixture：s1 最后活动在昨天，s2 在今天 —— 日期串按 NOW 推算，别写死）
        d_y, d_t = (_t.strftime('%Y-%m-%d', _t.localtime(NOW - DAY)),
                    _t.strftime('%Y-%m-%d', _t.localtime(NOW)))
        ok([r['id'] for r in adminapi.session_list(app, day=d_y)['items']] == ['s1'],
           '按日期筛：昨天只有 s1', d_y)
        ok([r['id'] for r in adminapi.session_list(app, day=d_t)['items']] == ['s2'],
           '按日期筛：今天只有 s2', d_t)
        ok(adminapi.session_list(app, day='2020-01-01')['items'] == [], '空日期段给空列表')
        ok(adminapi.session_list(app, day=d_y, q='s2')['items'] == [],
           'ID 与日期是"且"，不是"或"')
        # 关键：筛选必须在**全部**会话上做。若跟在 limit 后面筛，
        # 列表只带最近 1 条，老会话就永远搜不出来了。
        ok([r['id'] for r in adminapi.session_list(app, limit=1)['items']] == ['s2'],
           '不筛时只给最近 1 条（默认仍是分页的）')
        ok([r['id'] for r in adminapi.session_list(app, limit=1, q='s1')['items']] == ['s1'],
           '筛的时候在全部会话上筛 —— 昨天的会话也搜得到')

        # ------------------------------------------------ 六块原来没落盘的指标
        print('\n[12] 埋点落盘 → 看板读得回来')
        from core import telemetry as telmod
        tel_dir = os.path.join(tmp, 'telemetry')
        tl = telmod.Telemetry(tel_dir)
        # ① 出图成败 + ④ 耗时与真实点数
        tl.record('gen', agent='designer', sid='s2', skill='generate_pattern',
                  ok=1, ms=8000, points=240, n=4, pid='p1', model='img-1')
        tl.record('gen', agent='engineer', sid='s2', skill='seamless_pattern',
                  ok=1, ms=2000, points=30, n=1, pid='p3')
        tl.record('gen', agent='engineer', sid='s1', skill='trace_pattern',
                  ok=0, reason='timeout', ms=5000)
        # ② 提案：p1 被确认，p2 一直没人点
        tl.record('proposal', agent='designer', sid='s2', skill='generate_pattern',
                  cost=240, n=4, pid='p1')
        tl.record('proposal', agent='engineer', sid='s2', skill='trace_pattern',
                  cost=60, n=1, pid='p2')
        # ③ 知识库
        tl.record('kb', agent='assistant', sid='s1', hit=1, best=0.42, qa=1, q='多少钱一米')
        tl.record('kb', agent='assistant', sid='s2', hit=1, best=0.31, chunks=2)
        tl.record('kb', agent='assistant', sid='s2', hit=0, best=0.0, q='交期多久')
        # ⑤ 前端
        tl.record('visit', sid='s1', ref='https://weibo.com/x', page='designer', w=1440, h=900)
        tl.record('visit', sid='s2', ref='', page='engineer', w=390, h=844)
        tl.record('click', sid='s2', act='confirm-gen', page='designer')
        # ⑥ 会话结束方式
        tl.record('leave', sid='s1', dwell=180, reason='done', turns=3)
        tl.record('leave', sid='s2', dwell=12, reason='pending', turns=1)
        ok(len(telmod.load(tel_dir, 1)) == 13, '13 条事件落盘并读得回来',
           str(len(telmod.load(tel_dir, 1))))

        cfgT = FakeCfg(SETTINGS, SECRETS,
                       paths={'sessions': sess, 'telemetry': tel_dir})
        anT = adminapi.analytics(FakeApp(cfgT, items, outdir, COSTS), days=7)

        g = anT['gen']
        ok(g['attempts'] == 3 and g['ok'] == 2 and g['fail'] == 1,
           '出图 3 次 / 成 2 败 1', str(g))
        ok(g['rate'] == 66.7, '成功率 66.7%', str(g['rate']))
        ok(dict((r[0], r[2]) for r in g['by_reason']) == {'timeout': 1},
           '失败归到「超时」这一类', str(g['by_reason']))
        ok(g['avg_ms'] == 5000 and g['p50_ms'] == 5000 and g['p90_ms'] == 8000,
           '耗时均值/中位/90 分位', '%s %s %s' % (g['avg_ms'], g['p50_ms'], g['p90_ms']))
        ok(g['points'] == 270, '实际花掉 270 点（240+30）', str(g['points']))
        ok(g['by_skill'][0]['name'] == 'generate_pattern'
           and g['by_skill'][0]['avg_ms'] == 8000, '按技能拆分', str(g['by_skill'][0]))

        f = anT['funnel']
        ok(f['proposals'] == 2 and f['confirmed'] == 1 and f['unconfirmed'] == 1,
           '提案 2 / 确认 1 / 没点 1', str(f))
        ok(f['conv'] == 50.0, '转化率 50%', str(f['conv']))
        ok(f['stuck_points'] == 60, '没人点的卡片上压着 60 点', str(f['stuck_points']))
        ok(f['direct'] == 1, '没经卡片直接执行的算 1 次（不混进转化率）', str(f['direct']))

        k2 = anT['kb_stat']
        ok(k2['lookups'] == 3 and k2['hits'] == 2 and k2['rate'] == 66.7,
           '知识库 3 问 2 中（66.7%）', str(k2))
        ok(k2['top_miss'] == [['交期多久', 1]], '没命中的问题原样列出来（就是该补的资料）',
           str(k2['top_miss']))

        fe = anT['frontend']
        ok(fe['visits'] == 2 and fe['visitors'] == 2, '来访 2 次 / 2 个会话', str(fe))
        ok(fe['clicks'] == 1 and fe['acts'] == [['confirm-gen', 1]],
           '点了哪些按钮', str(fe['acts']))
        ok(dict(fe['refs']).get('直接打开 / 收藏夹') == 1
           and dict(fe['refs']).get('weibo.com') == 1,
           '来路只留域名（完整 Referer 里可能带搜索词，写入时就被砍掉了）',
           str(fe['refs']))
        ok(fe['avg_dwell'] == 96, '平均停留 (180+12)÷2 = 96 秒', str(fe['avg_dwell']))
        ok(dict(fe['pages']) == {'designer': 1, 'engineer': 1}, '停在哪个页面', str(fe['pages']))

        ed = anT['ending']
        ok(ed['total'] == 2, '2 次离开', str(ed))
        ok(dict((i[0], i[2]) for i in ed['items']) == {'done': 1, 'pending': 1},
           '聊完走的 1 / 压着卡片走的 1（可能就是卡住的）', str(ed['items']))
        ok(ed['avg_turns'].get('done') == 3.0 and ed['avg_turns'].get('pending') == 1.0,
           '两种走法的平均轮次分开算', str(ed['avg_turns']))

        ok(anT['coverage']['events'] == 13 and anT['coverage']['since'],
           '页面上能说清埋点从哪天起有数据', str(anT['coverage']))
        ok(anT['series'][-1]['ok'] == 2 and anT['series'][-1]['fail'] == 1
           and anT['series'][-1]['visit'] == 2,
           '成功/失败/来访按天挂回时间线', str(anT['series'][-1]))

        print('\n[12b] 埋点缺失 / 坏行都不能让看板崩')
        cfgN = FakeCfg(SETTINGS, SECRETS, paths={'sessions': sess,
                                                'telemetry': '/tmp/pf-没有这个目录'})
        anN = adminapi.analytics(FakeApp(cfgN, items, outdir, COSTS), days=7)
        ok(anN['gen']['attempts'] == 0 and anN['gen']['rate'] is None,
           '没有埋点：次数 0、成功率给 None（不编 0% 假装算过）', str(anN['gen']['rate']))
        ok(anN['funnel']['conv'] is None, '没有提案：转化率给 None')
        ok(anN['kb_stat']['rate'] is None, '没有检索：命中率给 None')
        ok(anN['ending']['items'] == [], '没有离开事件就是空列表')
        bad_dir = os.path.join(tmp, 'bad')
        os.makedirs(bad_dir, exist_ok=True)
        bad_path = os.path.join(bad_dir, _t.strftime('%Y-%m-%d') + '.jsonl')
        with open(bad_path, 'w', encoding='utf-8') as fh:
            fh.write('{"k":"gen","t":1\n')          # 半截 JSON
            fh.write('不是 json\n')
            fh.write(json.dumps({'k': 'gen', 't': _t.time(), 'ok': 1, 'ms': 100}) + '\n')
        cfgB = FakeCfg(SETTINGS, SECRETS,
                       paths={'sessions': sess, 'telemetry': bad_dir})
        anB = adminapi.analytics(FakeApp(cfgB, items, outdir, COSTS), days=7)
        ok(anB['ok'] is True and anB['gen']['attempts'] == 1,
           '坏行跳过，好的那行照样统计', str(anB['gen']['attempts']))
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print('\n结果：%d 通过 / %d 失败' % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
