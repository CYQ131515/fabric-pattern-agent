#!/usr/bin/env python3
"""埋点与运行时钩子的离线单测（对着 core/telemetry.py + core/runtime.py）。

跑法：python3 测试/test_telemetry.py

盯四件事：
  1. 埋点写坏了/目录没了/文件半截 —— **绝不能让主流程跟着崩**（统计是顺路记的）；
  2. 归类的口径要对：失败能不能看出是超时、限流、鉴权还是网络；
  3. 「提案 → 确认」要按 pid 计数 —— 用户在卡片上连点几下张数按钮，
     不能被算成提了好几次案（那会把转化率自己刷穿）；
  4. 前端报上来的东西只收白名单字段，多报的一律丢掉。

用临时目录，不碰真项目数据，也不需要起服务。
"""
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from core import telemetry as T                          # noqa: E402
from core.runtime import (AgentRuntime, gen_outcome,     # noqa: E402
                          _fail_reason)
from core.session import Session                         # noqa: E402

PASS = FAIL = 0


def ok(cond, label, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('  ✓ ' + label)
    else:
        FAIL += 1
        print('  ✗ ' + label + ('  → %s' % (extra,) if extra else ''))


class FakeCfg:
    def __init__(self, tel_dir):
        self._tel = tel_dir

    def path(self, key):
        if key == 'telemetry':
            return self._tel
        raise KeyError(key)


class FakeStore:
    """顶掉 SessionStore：只记 append，按 id 回会话。"""

    def __init__(self):
        self.recs = []
        self.sess = {}

    def append(self, sess, rec):
        self.recs.append((sess.id, rec))

    def get(self, sid):
        return self.sess.get(sid)


class FakeReg:
    def __init__(self, res):
        self.res = res
        self.calls = []

    def run(self, name, args, ctx):
        self.calls.append((name, args))
        if isinstance(self.res, Exception):
            raise self.res
        return self.res


def make_rt(tel_dir, sess=None):
    rt = AgentRuntime.__new__(AgentRuntime)      # 绕开构造（不建 LLM/生图客户端）
    rt.tel = T.Telemetry(tel_dir)
    rt.sessions = FakeStore()
    rt._ctx = lambda s: {}
    if sess is not None:
        rt.sessions.sess[sess.id] = sess
    return rt


def main():
    tmp = tempfile.mkdtemp(prefix='pf-tel-test-')
    try:
        # ------------------------------------------------------------ 存储
        print('[1] 落盘：一天一个文件、只追加、读得回来')
        d1 = os.path.join(tmp, 't1')
        tel = T.Telemetry(d1)
        ok(tel.record('visit', sid='s1', ref='https://a', w=1440), '写入返回 True')
        ok(tel.record('gen', sid='s1', skill='generate_pattern', ok=1, ms=900),
           '第二条也写进去了')
        ok(os.path.exists(os.path.join(d1, time.strftime('%Y-%m-%d') + '.jsonl')),
           '按天分文件')
        ev = T.load(d1, 1)
        ok(len(ev) == 2 and [e['k'] for e in ev] == ['visit', 'gen'],
           '按时间升序读回来', str([e['k'] for e in ev]))
        ok(ev[0]['ref'] == 'https://a' and ev[1]['ms'] == 900, '字段原样存回')
        ok('t' in ev[0] and isinstance(ev[0]['t'], float), '每条都带时间戳')
        bk = T.by_kind(ev)
        ok(set(bk) == {'visit', 'gen'} and len(bk['gen']) == 1, 'by_kind 分桶')

        print('\n[2] 空值字段不落盘（不写一堆 null 占地方）')
        tel.record('gen', sid='s1', skill='edit_pattern', ok=1, reason=None,
                   model='', n=0)
        last = T.load(d1, 1)[-1]
        ok('reason' not in last and 'model' not in last, '空串/None 不写进去',
           json.dumps(last, ensure_ascii=False))
        ok(last.get('n') == 0, '数字 0 是有效值，要留住')

        print('\n[3] 坏数据必须被吞掉，不能让看板跟着挂')
        d2 = os.path.join(tmp, 't2')
        os.makedirs(d2, exist_ok=True)
        p = os.path.join(d2, time.strftime('%Y-%m-%d') + '.jsonl')
        open(p, 'w', encoding='utf-8').write(
            '{"k":"gen","t":1\n不是 json\n\n[1,2,3]\n' +
            json.dumps({"k": "gen", "t": time.time(), "ok": 1}) + '\n')
        ev2 = T.load(d2, 1)
        ok(len(ev2) == 1, '半截 JSON / 非对象 / 空行全部跳过', str(len(ev2)))
        ok(T.load('/tmp/pf-绝对不存在的目录-xyz', 7) == [], '目录不存在 → 空列表')
        ok(T.load('', 7) == [], '目录为空 → 空列表')
        bad = T.Telemetry(os.path.join(tmp, 'nope'))
        bad.dir = p + '/sub'          # 路径中间是个文件 → 目录建不出来
        ok(bad.record('gen', ok=1) is False, '写不进去也返回 False 而不是抛异常')
        # 目录被清理工具删掉之后要能自己长回来。以前「建过」缓存成永久事实，
        # 删一次就永久静默失败：看板从此永远是空的，还不报错。
        d2b = os.path.join(tmp, 't2b')
        t2b = T.Telemetry(d2b)
        ok(t2b.record('visit', sid='s1') is True, '先写一条')
        shutil.rmtree(d2b)
        ok(t2b.record('visit', sid='s2') is True, '目录被删掉后还能接着写')
        ok(len(T.load(d2b, 1)) == 1, '新目录里只有删掉之后那条')

        print('\n[4] 关掉 / 改目录走环境变量')
        os.environ['PATTERNFLOW_TELEMETRY_DIR'] = os.path.join(tmp, 'env')
        os.environ.pop('PATTERNFLOW_TELEMETRY', None)
        ok(T.Telemetry.from_config(FakeCfg('/x')).dir == os.path.join(tmp, 'env'),
           '环境变量优先于配置')
        os.environ['PATTERNFLOW_TELEMETRY'] = '0'
        t_off = T.Telemetry.from_config(FakeCfg('/x'))
        ok(t_off.enabled is False and t_off.record('gen', ok=1) is False,
           'PATTERNFLOW_TELEMETRY=0 时彻底不写')
        os.environ.pop('PATTERNFLOW_TELEMETRY')
        os.environ.pop('PATTERNFLOW_TELEMETRY_DIR')
        ok(T.Telemetry.from_config(FakeCfg('/y')).dir == '/y', '没设环境变量就用配置')

        # ------------------------------------------------------- 结果判定
        print('\n[4b] 来路只留域名')
        ok(T.ref_host('https://weibo.com/s?q=秘密&token=abc') == 'weibo.com',
           '砍掉路径与参数')
        ok(T.ref_host('https://www.xiaohongshu.com/explore') == 'xiaohongshu.com',
           '去掉 www. 前缀')
        ok(T.ref_host('http://127.0.0.1:8787/admin') == '127.0.0.1:8787',
           '本机带端口要留着（8787 和 8790 得分得清）')
        ok(T.ref_host('weibo.com') == 'weibo.com', '没有协议的也认')
        ok(T.ref_host('') == '' and T.ref_host(None) == '', '空来路给空串')
        ok(T.ref_host('不是个地址') == '', '解析不出来就给空（不把原文塞进去）')

        print('\n[5] 一次出图算成算败')
        ok(gen_outcome({'ok': True, 'images': [1]}) == (True, ''),
           '有图 → 成功')
        ok(gen_outcome({'ok': True, 'downloads': [1]}) == (True, ''),
           '只有 PSD 下载也算成功（分层 PSD 就没有预览图）')
        ok(gen_outcome({'ok': True}) == (False, 'empty'),
           '说成功却什么都没有 → empty')
        ok(gen_outcome({'error': '任务超时了'}) == (False, 'timeout'), '超时归类')
        ok(gen_outcome({'error': '请求过于频繁 429'}) == (False, 'rate_limit'),
           '限流归类')
        ok(gen_outcome({'error': 'API Key 无效 401'}) == (False, 'auth'),
           '鉴权归类')
        ok(gen_outcome({'error': 'Connection refused'}) == (False, 'network'),
           '网络归类')
        ok(gen_outcome({'error': '图片太大'}) == (False, 'input'), '输入归类')
        ok(gen_outcome({'error': '没说要改什么。至少给 color 中的一项'})
           == (False, 'input'), '没说清要改什么也算输入问题（否则会掉进 other，白埋）')
        ok(gen_outcome({'error': '没有配 RunningHub Key'}) == (False, 'not_ready'),
           '没配置归类')
        ok(gen_outcome({'error': '说不清什么毛病'}) == (False, 'other'), '兜底 other')
        ok(gen_outcome(None) == (False, 'bad_result'), '技能返回了个非字典')

        # ------------------------------------------------------- 运行时钩子
        print('\n[6] 提案 → 确认：按 pid 计数，不重复计')
        d3 = os.path.join(tmp, 't3')
        s = Session('s9')
        rt = make_rt(d3, s)
        rt._set_pending(s, 'designer', {'action': 'generate_pattern',
                                        'cost': 240, 'count': 4, 'args': {}})
        pid = s.pending_actions['designer']['pid']
        ok(bool(pid), '挂卡片时自动编了 pid：%s' % pid)
        # 用户在卡片上切张数：同动作 → 沿用 pid，不重复记提案
        rt._set_pending(s, 'designer', {'action': 'generate_pattern',
                                        'cost': 60, 'count': 1,
                                        'pid': pid, 'args': {}})
        props = [e for e in T.load(d3, 1) if e['k'] == 'proposal']
        ok(len(props) == 1, '同一张卡片改参数只算一次提案', str(len(props)))
        ok(props[0]['pid'] == pid and props[0]['cost'] == 240,
           '记的是第一次挂出来的那版（报价 240）', json.dumps(props[0]))
        # 换成另一个技能 → 新 pid，算新提案
        rt._set_pending(s, 'designer', {'action': 'trace_pattern',
                                        'cost': 60, 'args': {}})
        props = [e for e in T.load(d3, 1) if e['k'] == 'proposal']
        ok(len(props) == 2 and props[1]['skill'] == 'trace_pattern',
           '换了动作就是新提案', str([p['skill'] for p in props]))
        ok(s.pending_actions['designer']['pid'] != pid, '新提案换了新 pid')
        # 清空不算提案
        rt._set_pending(s, 'designer', None)
        ok(len([e for e in T.load(d3, 1) if e['k'] == 'proposal']) == 2,
           '清空待确认方案不会多记一次提案')

        print('\n[7] 执行：成败 / 耗时 / 点数 / 张数 / 是不是来自卡片')
        d4 = os.path.join(tmp, 't4')
        s2 = Session('s10')
        rt2 = make_rt(d4, s2)
        rt2.registry = FakeReg({'ok': True, 'images': [1, 2, 3, 4],
                                'cost': 240, 'model': 'img-x'})
        rt2._exec(s2, 'designer', 'generate_pattern', {'confirmed': True}, pid='p1')
        e = [x for x in T.load(d4, 1) if x['k'] == 'gen'][-1]
        ok(e['ok'] == 1 and e['skill'] == 'generate_pattern' and e['n'] == 4,
           '成功 + 张数 4', json.dumps(e, ensure_ascii=False))
        ok(e['points'] == 240 and e['model'] == 'img-x', '点数与模型记下来')
        ok(e['pid'] == 'p1' and e['phase'] == 'exec', '带上提案编号（能对上转化率）')
        ok(isinstance(e['ms'], int), '带耗时（毫秒）')

        rt2.registry = FakeReg({'ok': True, 'downloads': [{'url': 'a'}]})
        rt2._exec(s2, 'engineer', 'layer_psd', {'confirmed': True}, pid='p2')
        e = [x for x in T.load(d4, 1) if x['k'] == 'gen'][-1]
        ok(e['ok'] == 1 and e['n'] == 1, '分层 PSD 靠 downloads 判成功')

        rt2.registry = FakeReg({'error': '连接超时'})
        rt2._exec(s2, 'engineer', 'trace_pattern', {'confirmed': True})
        e = [x for x in T.load(d4, 1) if x['k'] == 'gen'][-1]
        ok(e['ok'] == 0 and e['reason'] == 'timeout', '失败带归类原因', str(e))

        rt2.registry = FakeReg(RuntimeError('boom'))
        raised = False
        try:
            rt2._exec(s2, 'designer', 'edit_pattern', {'confirmed': True})
        except RuntimeError:
            raised = True
        ok(raised, '技能抛异常要**照样往上抛**（不能因为埋点吞掉业务错误）')
        e = [x for x in T.load(d4, 1) if x['k'] == 'gen'][-1]
        ok(e['ok'] == 0 and e['reason'] == 'RuntimeError',
           '抛异常也留了一条记录（否则这类失败在统计里是隐形的）', str(e))

        print('\n[8] 埋点自己坏了，不许影响出图')
        rt2.tel = T.Telemetry('')          # 相当于整个关掉
        rt2.registry = FakeReg({'ok': True, 'images': [1], 'cost': 60})
        r = rt2._exec(s2, 'designer', 'generate_pattern', {'confirmed': True})
        ok(r.get('ok') is True, '埋点写不进去，出图结果照原样返回')

        print('\n[9] 知识库命中率')
        d5 = os.path.join(tmp, 't5')

        class FakeKB:
            enabled = True
            agents = ['assistant']
            docs = [1]
            qa = []

            def lookup(self, q, agent=None):
                hit = '多少钱' in q
                return {"hit": hit, "best": 0.4 if hit else 0.0, "qa": [], "chunks": [1]}

        rt3 = make_rt(d5, Session('s11'))
        rt3.kb = FakeKB()
        sess3 = rt3.sessions.sess['s11']
        rt3._kb_lookup('assistant', '多少钱一米', sess3)
        rt3._kb_lookup('assistant', '交期多久', sess3)
        ev = [x for x in T.load(d5, 1) if x['k'] == 'kb']
        ok(len(ev) == 2 and ev[0]['hit'] == 1 and ev[1]['hit'] == 0,
           '命中 / 没命中各记一条', str([e['hit'] for e in ev]))
        ok(ev[0]['q'] == '多少钱一米' and ev[0]['sid'] == 's11',
           '问题原文记下来（未命中的就是该补的资料清单）', json.dumps(ev[0], ensure_ascii=False))
        rt3.kb.docs, rt3.kb.qa = [], []
        ok(rt3._kb_lookup('assistant', '空库', sess3) is None,
           '空库整条链路不参与（也就不会记一条假"未命中"）')

        print('\n[10] 会话结束方式：聊完 / 在等回答 / 压着卡片 / 进来就走')
        s4 = Session('s12')
        rt4 = make_rt(os.path.join(tmp, 't6'), s4)
        ok(rt4.end_reason('没这个会话') == 'unknown', '找不到会话 → unknown')
        ok(rt4.end_reason('s12') == 'bounce', '一句话没说 → 进来就走')
        s4.add_turn('designer', '你好', '你想做什么？', [])
        ok(rt4.end_reason('s12') == 'asking', '我方最后一句是问句 → 在等回话')
        s4.add_turn('designer', '再来', '好了，图在上面。', [])
        ok(rt4.end_reason('s12') == 'done', '给了完整答复 → 聊完走的')
        s4.pending_actions['designer'] = {'action': 'generate_pattern'}
        ok(rt4.end_reason('s12') == 'pending', '桌上还压着没点的卡片 → 优先报 pending')

        print('\n[11] 前端上报：只收白名单，坏值不炸')
        d7 = os.path.join(tmp, 't7')
        s5 = Session('s13')
        rt5 = make_rt(d7, s5)
        s5.add_turn('designer', 'hi', '好的。', [])
        ok(rt5.track({'event': 'visit', 'session_id': 's13',
                      'ref': 'https://weibo.com/s?q=客户名&token=abc',
                      'w': '1440', 'h': 900, 'agent': 'designer',
                      '聊天内容': '这句不该被存'}).get('ok') is True, 'visit 收下')
        v = [x for x in T.load(d7, 1) if x['k'] == 'visit'][-1]
        ok(v['ref'] == 'weibo.com' and v['w'] == 1440 and v['sid'] == 's13',
           '来路只留域名（路径和参数一律不入库）', json.dumps(v, ensure_ascii=False))
        ok('q=' not in json.dumps(v) and 'token' not in json.dumps(v),
           'Referer 里的搜索词/token 一个都不留')
        ok('聊天内容' not in v, '白名单之外的一律丢掉（不整段收）')
        ok(rt5.track({'event': 'visit', 'session_id': 's13',
                      'ref': ''}).get('ok') is True, '没有来路也能记（直接打开）')
        ok([x for x in T.load(d7, 1) if x['k'] == 'visit'][-1].get('ref') is None,
           '空来路不写字段（看板上归到"直接打开 / 收藏夹"）')
        ok(rt5.track({'event': 'click', 'session_id': 's13',
                      'act': 'confirm-gen'}).get('ok') is True, 'click 收下')
        ok([x for x in T.load(d7, 1) if x['k'] == 'click'][-1]['act'] == 'confirm-gen',
           '记下点的是哪个按钮')
        ok(rt5.track({'event': 'leave', 'session_id': 's13', 'dwell': 33}).get('ok') is True,
           'leave 收下')
        lv = [x for x in T.load(d7, 1) if x['k'] == 'leave'][-1]
        ok(lv['reason'] == 'done' and lv['turns'] == 1 and lv['dwell'] == 33,
           '离开时由服务端判"是聊完走的"，并带上聊了几轮',
           json.dumps(lv, ensure_ascii=False))
        ok(rt5.track({'event': 'whatever'}).get('ok') is False,
           '不认识的类型直接忽略（不写脏数据）')
        ok(rt5.track({}).get('ok') is False, '空事件忽略')
        rt5.track({'event': 'visit', 'session_id': 's13', 'w': '很宽', 'h': None})
        v2 = [x for x in T.load(d7, 1) if x['k'] == 'visit'][-1]
        ok('w' not in v2 and 'h' not in v2, '尺寸不是数字就丢掉，不写 NaN', str(v2))

        class Boom:
            def get(self, sid):
                raise RuntimeError('会话库炸了')

        rt5.sessions = Boom()
        ok(rt5.track({'event': 'leave', 'session_id': 'x', 'dwell': 1}).get('ok') is True,
           '取会话出错也不能让埋点接口报错', '')

        print('\n[12] 只许调「喂给过它」的工具')
        # 工具清单是照角色配的，但工具循环以前是**照名字直接跑** ——
        # 模型喊一个没给它的技能，客服页上就能挂出花型师的确认卡片
        # （真链路冒烟里假模型就这么干过，凭空多一条 agent=assistant 的提案）。

        class FakeLLM:
            def __init__(self):
                self.round = 0

            def chat(self, messages, tools=None):
                self.round += 1
                if self.round == 1:
                    return {"content": "", "tool_calls": [
                        {"id": "c1", "type": "function",
                         "function": {"name": "get_pattern_detail", "arguments": "{}"}},
                        {"id": "c2", "type": "function",
                         "function": {"name": "generate_pattern", "arguments": "{}"}}]}
                return {"content": "好了", "tool_calls": []}

        class FakeReg2:
            def __init__(self):
                self.ran = []

            def schemas(self, names):
                return [{"type": "function",
                         "function": {"name": n, "parameters": {}}} for n in names]

            def get(self, name):
                return Named(confirm=True)

            def run(self, name, args, ctx):
                self.ran.append(name)
                return {"ok": True, "images": [1]}

        class Named:
            def __init__(self, confirm=False):
                self.confirm = confirm

        class FakeCfg2:
            settings = {"llm": {"max_tool_rounds": 3}}

            def agent_conf(self, agent):
                return {"skills": ["get_pattern_detail"]}     # 客服就这一件

        rt6 = make_rt(os.path.join(tmp, 't8'), Session('s14'))
        rt6.cfg = FakeCfg2()
        rt6.registry = FakeReg2()
        rt6.llm = FakeLLM()
        rt6._system = lambda *a, **k: 'sys'
        rt6._persist_image = lambda s: None      # 这个用例只关心「跑没跑」
        txt, used, _, _, _ = rt6._chat_llm('assistant', rt6.sessions.sess['s14'], '帮我生成花型')
        ok(rt6.registry.ran == ['get_pattern_detail'],
           '越权那件工具没被执行', str(rt6.registry.ran))
        ok('generate_pattern' not in used, '也不记进「用过的技能」')
        ok(txt == '好了', '回一条「没这件工具」后模型能自己收口')
        sess6 = rt6.sessions.sess['s14']
        ok(not (sess6.pending_actions or {}).get('assistant'),
           '客服页上不会凭空多出一张待确认卡片')

        print('\n[13] 真看板目录没被碰过')
        # 跑一次回归会记几十条出图/提案（会话号 t-tweak-gen、xchar…）。
        # 那些要是落进真看板目录，运营看到的「成功率」「转化率」就是测试造的假数。
        # 各测试必须像隔离图库那样，把 PATTERNFLOW_TELEMETRY_DIR 也指到临时目录。
        from core.config import Config                   # noqa: E402
        os.environ.pop('PATTERNFLOW_TELEMETRY_DIR', None)
        real = T.Telemetry.from_config(Config()).dir
        ok(os.path.isdir(real) is False
           or not [e for e in T.load(real, 1)
                   if e.get('sid') in ('s9', 's10', 's11', 's12', 's13', 's14')],
           '本次单测的事件一条也没落进真看板目录（%s）' % real)

        # 防复发：会跑真实链路（真 App / 真 AgentRuntime）的测试，必须把埋点
        # 也隔离掉。这条是照 test_library 的教训加的 —— 它以前只隔离了图库和
        # 出图目录，于是每跑一次回归，真看板上就多出几条测试造的出图记录。
        # 认两种隔离写法：环境变量，或者显式把 paths 里的 telemetry 指走。
        leaky = []
        for fn in sorted(os.listdir(HERE)):
            if not (fn.startswith('test_') and fn.endswith('.py')):
                continue
            src = io.open(os.path.join(HERE, fn), encoding='utf-8').read()
            # \b 会把 FakeApp( 挡在外面（'e' 和 'A' 之间没有词边界）
            runs_real = 'AgentRuntime(' in src or re.search(r'\bApp\(', src)
            isolated = ('PATTERNFLOW_TELEMETRY_DIR' in src
                        or "'telemetry'" in src or '"telemetry"' in src)
            if runs_real and not isolated:
                leaky.append(fn)
        ok(not leaky, '跑真链路的测试都隔离了埋点目录', str(leaky))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print('\n结果：%d 通过 / %d 失败' % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
