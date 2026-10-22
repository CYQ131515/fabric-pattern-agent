#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三项改动回归（服务端，不连网、不花钱）。

    [1] 库里没对上 → 引导 AI 生成（matched 由本地算法定）
    [2] 没查过库不许提生成方案；条件变了要重查
    [2b] 张数听用户的：n=1/2/4、报价跟着走、卡片上切张数不被闸门卡住
    [2c] 需求卡没补齐 → 先问用户，不许拿半张卡推生成（连打回 2 次放行）
    [3] 思考吃光 token 预算 → 自动加大预算重试；兜底话术拟人化，不漏技术词
    [4] （前端部分见 tools/test_v06_stop.mjs）跑动中的主动终止

跑法：python3 tools/test_v06_lib4.py
生图走 mock provider，模型被替换成假的，所以全程零网络。
"""
import base64
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.dirname(HERE)
sys.path.insert(0, AGENT)

os.environ['PATTERNFLOW_IMAGE_PROVIDER'] = 'mock'
# 出图产物挪去临时目录，别堆进项目的 generated/
os.environ['PATTERNFLOW_IMAGE_OUTDIR'] = tempfile.mkdtemp(prefix='pf-test-img-')
# 图库目录也挪走：出图会自动入库，别把测试产物混进真图库
os.environ['PATTERNFLOW_LIBRARY_DIR'] = tempfile.mkdtemp(prefix='pf-test-lib-')
# 埋点也挪走：跑一次回归会记下几十条出图/提案，混进真看板就成了假数据
os.environ['PATTERNFLOW_TELEMETRY_DIR'] = tempfile.mkdtemp(prefix='pf-test-tel-')

from core.config import Config                          # noqa: E402
from core.persona import load_all as load_personas      # noqa: E402
from core.skills import SkillRegistry                   # noqa: E402
from core.session import SessionStore                   # noqa: E402
from core.runtime import AgentRuntime                   # noqa: E402
from core.llm import LLMClient, LLMError                # noqa: E402
from core import rules                                  # noqa: E402

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


class OffLLM:
    enabled = False

    def chat(self, *a, **k):
        raise AssertionError('这个用例不该真的调模型')


class DeadLLM:
    """模型挂掉：运行时应当转规则兜底，并且话要说得像人。"""
    enabled = True

    def __init__(self, err=None):
        self.err = err or LLMError('思考过程把 token 预算吃光了，这次没吐出正文')

    def chat(self, *a, **k):
        raise self.err


def build(tmpdir, llm=None):
    cfg = Config()
    data = {}
    for k, fn in (('patterns', 'patterns.json'), ('trends', 'trends.json'),
                  ('vocab', 'vocab.json')):
        p = os.path.join(cfg.path('data'), fn)
        data[k] = (json.load(open(p, encoding='utf-8'))
                   if os.path.exists(p) else [])
    rt = AgentRuntime(cfg, load_personas(cfg.path('personas')),
                      SkillRegistry(cfg.path('skills')),
                      SessionStore(os.path.join(tmpdir, 'sessions')), data)
    rt.llm = llm or OffLLM()
    return rt, data, cfg


PATS = [
    # 这一组刻意只在「风格」上沾边（跟截图里那两款一样：风格对得上，元素和配色都不是）
    {'id': 'PF-X1', 'name': '童梦星球', 'style': '童趣插画', 'element': '几何',
     'color': '高饱和撞色', 'scene': '童装', 'heat': 60, 'desc': '', 'l2': []},
    {'id': 'PF-X2', 'name': '甜蜜浆果', 'style': '童趣插画', 'element': '波点',
     'color': '马卡龙甜彩', 'scene': '童装', 'heat': 55, 'desc': '', 'l2': []},
]


def main():
    tmp = tempfile.TemporaryDirectory()
    rt, data, cfg = build(tmp.name)

    # ================================================ [1] 库里有货 / 没货的判定
    print('\n[1] 「库里有对得上的吗」由本地算法定，不交给模型感觉')
    ctx = {'patterns': PATS, 'vocab': data.get('vocab') or {}}

    r = rt.registry.run('search_patterns', {'style': '童趣插画', 'element': '抽象笔触',
                                            'color': '莫兰迪灰调',
                                            'scene': '家居布艺'}, ctx)
    ok_eq(r.get('matched'), False, '四个维度只中一个 → matched=false（那是沾边，不是有货）')
    ok_eq(r.get('best_hit'), 1, '最高命中维度数是 1')
    ok_eq(r.get('need_hit'), 2, '四个维度时需要命中 2 个才算对上')
    ok('matched=false' in (r.get('note') or ''), 'note 里说明该怎么用这个字段')

    r = rt.registry.run('search_patterns', {'style': '童趣插画', 'element': '几何',
                                            'color': '高饱和撞色',
                                            'scene': '童装'}, ctx)
    ok_eq(r.get('matched'), True, '命中 3 个维度 → matched=true')

    r = rt.registry.run('search_patterns', {'color': '马卡龙甜彩'}, ctx)
    ok_eq(r.get('matched'), True, '只给一个维度、且命中 → matched=true')
    r = rt.registry.run('search_patterns', {'color': '薄荷绿'}, ctx)
    ok_eq(r.get('matched'), False, '只给一个维度但没命中 → matched=false')
    r = rt.registry.run('search_patterns', {}, ctx)
    ok_eq(r.get('known_dims'), 0, '一个维度都没给时不硬判（known_dims=0）')

    # ================================================ [2] 先查库，再生成
    print('\n[2] 没查过库不许提生成方案（「我想要」≠「生成」）')
    ok_eq(rt.imagegen.variants, 4, '默认张数来自配置 image.variants')
    sess = rt.sessions.get('t-lib4-gen')
    sess.brief = {'style': '童趣插画', 'element': '几何', 'color': '淡绿',
                  'scene': '家居布艺'}
    ctx2 = rt._ctx(sess)

    # 用户说「我想要一张卡通风格的图案」，模型却直接开提案 —— 出过的 bug。
    # 出图要花点数，能查到就不该画，所以这条必须由服务端拦，不能指望模型自觉。
    blocked = rt.registry.run('generate_pattern', {'confirmed': False}, ctx2)
    ok_eq(blocked.get('need_search'), True, '没查过库直接提案 → 被拦下')
    ok(blocked.get('need_confirm') is None, '被拦时不挂卡片（没有 need_confirm）')
    ok('search_patterns' in (blocked.get('hint') or ''), '提示里点名要它先调检索')

    # 查一次库（同一套条件）之后就放行
    rt.registry.run('search_patterns', {}, ctx2)
    prop = rt.registry.run('generate_pattern', {'confirmed': False}, ctx2)
    ok_eq(prop.get('need_confirm'), True, '查过库之后 → 正常挂方案')
    ok_eq(prop.get('count'), 4, '方案里写明一次 4 张')
    ok_eq(prop.get('cost'), 240, '报价是 4 张的总价（60×4），不是单张价')
    ok('生成 4 张' in (prop.get('summary') or ''), '摘要里说清张数', prop.get('summary'))
    ok('4 张' in (prop.get('hint') or ''), 'hint 提醒模型报总价', prop.get('hint'))

    # 需求卡改过之后再提案，得重新查一次 —— 不能拿上一轮的检索结果蒙混过关
    sess.brief = dict(sess.brief, color='薄荷绿')
    again = rt.registry.run('generate_pattern', {'confirmed': False}, rt._ctx(sess))
    ok_eq(again.get('need_search'), True, '条件变了、没重查 → 又被拦')
    rt.registry.run('search_patterns', {}, rt._ctx(sess))
    ok_eq(rt.registry.run('generate_pattern', {'confirmed': False},
                          rt._ctx(sess)).get('need_confirm'), True, '重查之后放行')

    # 卡片上的按钮（retune）不能被这道闸门卡住：方案早就提过了
    tun = rt.registry.run('generate_pattern',
                          {'confirmed': False, '_retune': True}, rt._ctx(sess))
    ok_eq(tun.get('need_confirm'), True, '带 _retune 的重跑提案不走向闸门')

    ctx2 = rt._ctx(sess)
    done = rt.registry.run('generate_pattern', {'confirmed': True}, ctx2)
    ok_eq(done.get('ok'), True, '确认后真出图（mock）')
    ok_eq(len(done.get('images') or []), 4, '确实落了 4 张')
    ok_eq(done.get('count'), 4, 'count 回传实际张数')
    ok_eq(done.get('cost'), 240, '扣费按 4 张算')
    bodies = set()
    for f in done['images']:
        with open(f['path'], 'rb') as fh:
            bodies.add(fh.read())
    ok_eq(len(bodies), 4, '四张内容互不相同（不是同一张复制四份）')

    # ================================================ [2b] 张数听用户的
    print('\n[2b] 「我想要一张」就该出一张，不许劝他多出')
    only1 = rt.registry.run('generate_pattern', {'confirmed': False, 'n': 1}, sess2ctx(rt))
    ok_eq(only1.get('count'), 1, '显式传 n=1 时只出一张（张数可覆盖）')
    ok_eq(only1.get('cost'), 60, '单张时的报价也回到 60')
    ok_eq(rt.registry.run('generate_pattern', {'confirmed': False, 'n': 2},
                          sess2ctx(rt)).get('cost'), 120, 'n=2 → 120 点')
    ok_eq(rt.registry.run('generate_pattern', {'confirmed': False, 'n': 9},
                          sess2ctx(rt)).get('count'), 4, '超过 4 张按 4 张处理')
    ok_eq(rt.registry.run('generate_pattern', {'confirmed': False, 'n': 0},
                          sess2ctx(rt)).get('count'), 4, 'n=0 这种脏参数当没传，回到默认 4 张')
    # 卡片上点「1 张」走的就是这条路：retune_pending 把 n 覆盖进 pending 再重跑提案
    rt._set_pending(sess, 'designer', {'action': 'generate_pattern', 'args': {}, 'cost': 240})
    rt.registry.run('search_patterns', {}, rt._ctx(sess))
    rr = rt.retune_pending('t-lib4-gen', {'n': 1}, 'designer')
    ok(rr.get('ok') is True, '卡片上切张数成功', rr.get('error'))
    ok_eq(rr['pending'].get('count'), 1, '卡片变成 1 张')
    ok_eq(rr['pending'].get('cost'), 60, '报价跟着降到 60 点')
    ok('_retune' not in (rr['pending'].get('args') or {}), '_retune 只是信号，不写回卡片')
    ok_eq((rr['pending'].get('args') or {}).get('n'), 1, '张数写进了卡片的参数里（确认时按它执行）')

    # ================================================ [2c] 需求卡没补齐就先问用户
    print('\n[2c] 卡没补齐 → 先问，不许拿半张卡推生成（用户原话："怎么又不根据需求卡问用户了"）')
    s3 = rt.sessions.get('t-lib4-halfbrief')
    s3.brief = {'style': '卡通', 'element': None, 'color': None, 'scene': None}
    # 出过的 bug：用户说「我想要一张卡通图案」，卡上只有 style，
    # 模型却直接报「一次 4 张、共 240 点」，追问只剩尾巴。
    s3.last_user_text = '我想要一张卡通图案'
    half = rt.registry.run('generate_pattern', {'confirmed': False}, rt._ctx(s3))
    ok_eq(half.get('need_brief'), ['element', 'color', 'scene'], '卡缺三维 → 被扣住')
    ok(half.get('need_confirm') is None, '被扣住时不挂卡片（更不会报价）')
    ok(half.get('cost') is None, '被扣住时连点数都不给', half.get('cost'))
    ok('先别提方案' in (half.get('hint') or ''), 'hint 明说先问、别推方案', half.get('hint'))
    ok('元素' in (half.get('hint') or ''), 'hint 点名缺哪一维（说人话）')

    # 「我想要」不算生成指令 —— 用户为这句话抱怨过两次，别再把它当指令用。
    # 换个会话，别把上面那份的打回计数用掉。
    s3b = rt.sessions.get('t-lib4-want')
    s3b.brief = {'style': '卡通', 'element': None, 'color': None, 'scene': None}
    s3b.last_user_text = '我想要一张卡通的图案'
    ok_eq(rt.registry.run('generate_pattern', {'confirmed': False},
                          rt._ctx(s3b)).get('need_brief'), ['element', 'color', 'scene'],
          '「我想要」不算生成指令，仍然先问')

    # 连打回 2 次就放行：人设里「追问最多 2 轮」，不设上限的话
    # 用户回一句「嗯」会被永远卡在追问里（s3 已经用过一次，这次是第 2 次）
    s3.last_user_text = '嗯'
    ok_eq(rt.registry.run('generate_pattern', {'confirmed': False},
                          rt._ctx(s3)).get('need_brief'), ['element', 'color', 'scene'],
          '第 2 次尝试仍然先问')
    rt.registry.run('search_patterns', {}, rt._ctx(s3))
    ok_eq(rt.registry.run('generate_pattern', {'confirmed': False},
                          rt._ctx(s3)).get('need_confirm'), True,
          '第 3 次尝试放行（问过两轮了，别再问了）')

    # 用户明确说要出图 / 把选择权交回来 → 不再追问
    s4 = rt.sessions.get('t-lib4-explicit')
    s4.brief = {'style': '卡通', 'element': None, 'color': None, 'scene': None}
    rt.registry.run('search_patterns', {}, rt._ctx(s4))
    s4.last_user_text = '给我生成一张卡通的图案'
    ok_eq(rt.registry.run('generate_pattern', {'confirmed': False},
                          rt._ctx(s4)).get('need_confirm'), True,
          '用户明说「生成」→ 直接放行')
    s4.last_user_text = '随便，你决定'
    ok_eq(rt.registry.run('generate_pattern', {'confirmed': False},
                          rt._ctx(s4)).get('need_confirm'), True,
          '用户说「随便 / 你决定」→ 不再追问，直接放行')

    # 卡片上的按钮不能被这道闸门卡住；模型自己编 prompt_full 也绕不过去
    s5 = rt.sessions.get('t-lib4-buttons')
    s5.brief = {'style': '卡通', 'element': None, 'color': None, 'scene': None}
    ok_eq(rt.registry.run('generate_pattern', {'confirmed': False, '_retune': True},
                          rt._ctx(s5)).get('need_confirm'), True,
          '卡片上切张数/还原度 → 不被追问闸门卡住')
    ok_eq(rt.registry.run('generate_pattern',
                          {'confirmed': False, 'prompt_full': '卡通风格，可爱的卡通角色',
                           'n': 1}, rt._ctx(s5)).get('need_brief'),
          ['element', 'color', 'scene'],
          '模型自己编的 prompt_full 绕不过闸门（真重放走 confirmed=True）')
    rt.registry.run('search_patterns', {}, rt._ctx(s5))
    ok_eq(rt.registry.run('generate_pattern',
                          {'confirmed': False, 'prompt_full': '卡通风格，可爱的卡通角色',
                           'n': 1}, rt._ctx(s5)).get('need_brief'),
          ['element', 'color', 'scene'],
          '第 2 次带 prompt_full 也照样被扣住')
    ok_eq(getattr(s5, 'brief_blocks', 0), 2, '打回计数累到 2（追问两轮）')
    ok_eq(rt.registry.run('generate_pattern', {'confirmed': False},
                          rt._ctx(s5)).get('need_confirm'), True,
          '第 3 次放行，方案挂出来')
    ok_eq(getattr(s5, 'brief_blocks', 0), 0, '方案挂出来后追问计数归零')

    # ================================================ [3] 规则兜底也走同一条逻辑
    print('\n[3] 离线兜底同样「没对上就推 AI 生成」')
    class S:
        def __init__(self, brief):
            self.brief = dict(brief)
            self.rounds = 9
            self.last_photo = None

    txt, picks = rules.reply_designer('就按这个来', S({
        'style': '童趣插画', 'element': '抽象笔触', 'color': '莫兰迪灰调',
        'scene': '家居布艺'}), PATS, rt.imagegen)
    ok('现成的没有' in txt, '如实说库里没有', txt[:40])
    ok('AI 生成' in txt, '引导用户改用 AI 生成')
    ok('4 张' in txt and '240 点' in txt, '把「4 张 / 240 点」讲明白')
    ok_eq(len(picks), 2, '近似款只给 2 款当参考，不铺一排当答案')

    txt2, _ = rules.reply_designer('就按这个来', S({
        'style': '童趣插画', 'element': '几何', 'color': '高饱和撞色',
        'scene': '童装'}), PATS, rt.imagegen)
    ok('从库里筛到' in txt2, '库里有货时照旧出候选')
    ok('AI 生成' not in txt2, '有货时不要往 AI 生成上带')

    # ================================================ [4] 截断自动加大预算
    print('\n[4] 思考吃光 token 预算 → 自动加大预算重试，而不是降级')
    ok(cfg.settings['llm'].get('max_tokens', 0) >= 16000,
       'llm.max_tokens 已调大', cfg.settings['llm'].get('max_tokens'))
    llm = LLMClient(cfg)
    calls = []

    def fake_once(messages, tools, temperature, max_tokens, model, timeout, extra):
        calls.append(max_tokens)
        if len(calls) == 1:
            e = LLMError('思考过程把 token 预算吃光了')
            e.truncated = True
            raise e
        return {'content': '直接给你结论', 'tool_calls': [],
                'finish_reason': 'stop', 'usage': None}

    llm._once = fake_once
    res = llm.chat([{'role': 'user', 'content': '你好'}], tools=[{'t': 1}])
    ok_eq(res['content'], '直接给你结论', '重试成功后正常返回正文')
    ok_eq(len(calls), 2, '只重试一次')
    ok_eq(calls[0], cfg.settings['llm']['max_tokens'], '第一次用配置的预算')
    ok(calls[1] > calls[0], '第二次预算明显加大', calls)

    # 两次都被截断：第一次的半截正文不该丢
    llm2 = LLMClient(cfg)
    n2 = []

    def fake2(messages, tools, temperature, max_tokens, model, timeout, extra):
        n2.append(max_tokens)
        e = LLMError('又被截断了')
        e.truncated = True
        e.raw = '半截答复'
        raise e

    llm2._once = fake2
    res2 = llm2.chat([{'role': 'user', 'content': '你好'}])
    ok_eq(res2['content'], '半截答复', '两次都截断时，保住第一次的半截正文')

    # 非截断错误不该重试
    llm3 = LLMClient(cfg)
    n3 = []

    def fake3(*a, **k):
        n3.append(1)
        raise LLMError('HTTP 401：invalid api key')

    llm3._once = fake3
    try:
        llm3.chat([{'role': 'user', 'content': '你好'}])
        ok(False, '鉴权失败应当往上抛')
    except LLMError:
        ok(True, '鉴权失败照旧往上抛')
    ok_eq(len(n3), 1, '真失败不浪费一次重试')

    # ================================================ [5] 失败话术拟人化
    print('\n[5] 兜底话术拟人化，不漏技术词')
    tmp2 = tempfile.TemporaryDirectory()
    rt2, _, _ = build(tmp2.name, llm=DeadLLM())
    out = rt2.chat('designer', '电子游戏元素配淡绿，用在床品上', 't-lib4-dead')
    reply = out.get('reply') or ''
    for bad in ('模型调用失败', 'max_tokens', '规则模式', 'LLMError', '异常'):
        ok(bad not in reply, '回复里不出现「%s」' % bad, reply[:60])
    ok('先按常规给你捋一版' in reply, '换成人话交代，再给结果', reply[:60])
    ok_eq(out.get('mode'), 'rule', '仍然走的是兜底链路（只是话变好听了）')

    # 织印师拿到照片说描稿：兜底也要接住话头，别答成无关的功能清单
    sess3 = rt2.sessions.get('t-lib4-photo')
    # 照片是**织印师这条线**上的：会话记忆按角色分槽，写之前得先绑定角色，
    # 否则写进去的是花型师那一槽，织印师开口就成了"我手上没有照片"。
    sess3.act_as('engineer')
    sess3.last_photo = {'b64': base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'\x00' * 40).decode(),
                        'mime': 'image/png'}
    out3 = rt2.chat('engineer', '把这张图片给我描一下', 't-lib4-photo')
    r3 = out3.get('reply') or ''
    ok('照片我收到了' in r3, '先接住「你的照片我收到了」', r3[:50])
    ok('四方连续' not in r3.split('照片我收到了')[1][:10], '不会跑题去讲一套功能清单')

    tmp.cleanup()
    tmp2.cleanup()
    print('\n结果：%d 通过 / %d 失败' % (PASS, FAIL))
    return 1 if FAIL else 0


def sess2ctx(rt):
    return rt._ctx(rt.sessions.get('t-lib4-gen'))


if __name__ == '__main__':
    sys.exit(main())
