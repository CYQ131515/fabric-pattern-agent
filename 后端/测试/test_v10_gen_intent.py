#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「用户明说要生成，机器人却把他推去花型库挑选」这条返工的回归（服务端，不连网不花钱）。

起因是用户截图投诉：
    用户：给我生成一张国风的，并且带有浪漫元素的花型图案，元素有玫瑰花，菊花，
          牡丹花，水墨画风格
    机器人：刚才我这边绕得有点久、没接上，先按常规给你捋一版：
            按你给的条件（新中式国风、花卉、莫兰迪灰调、女装连衣裙），从库里筛到 6 款…
            打开花型选择页可以只看图案…

三处毛病，这一份全钉住：
    [1] 闸门一（先查库再生成）不该再拦「用户明说要出图」这件事
    [2] 「我想要 X」照旧要查库 —— 别把闸门拆了
    [3] 生成动作词的词表全站只有一份（规则兜底与技能闸门认同一套）
    [4] 规则兜底认生成意图：明说要出图就照办，别把人往库里带
    [5] 规则兜底不许把需求卡里的旧值说成「你给的条件」
    [6] 需求卡一个维度都没有时不硬凑着画
    [7] 模型这条路挂掉时，服务端要自己把方案卡挂出来（"先做后说"）

跑法：python3 测试/test_v10_gen_intent.py
生图走 mock provider；模型只在 [7] 里被换成"一定失败"的桩，其余零网络。
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.dirname(HERE)
sys.path.insert(0, AGENT)

os.environ['PATTERNFLOW_IMAGE_PROVIDER'] = 'mock'
os.environ['PATTERNFLOW_IMAGE_OUTDIR'] = tempfile.mkdtemp(prefix='pf-test-img-')
os.environ['PATTERNFLOW_LIBRARY_DIR'] = tempfile.mkdtemp(prefix='pf-test-lib-')
os.environ['PATTERNFLOW_TELEMETRY_DIR'] = tempfile.mkdtemp(prefix='pf-test-tel-')

from core.config import Config                          # noqa: E402
from core.persona import load_all as load_personas      # noqa: E402
from core.skills import SkillRegistry                   # noqa: E402
from core.session import SessionStore                   # noqa: E402
from core.runtime import AgentRuntime                   # noqa: E402
from core.llm import LLMError                           # noqa: E402
from core import rules                                  # noqa: E402
import skills.generate_pattern.handler as GEN           # noqa: E402

PASS, FAIL = 0, 0

# 用户截图里那句话，一个字不改
MSG = ('给我生成一张国风的，并且带有浪漫元素的花型图案，'
       '元素有玫瑰花，菊花，牡丹花，水墨画风格')


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
    """不在测试范围里的调用一律不许联网。"""
    enabled = False

    def chat(self, *a, **k):
        raise AssertionError('这个用例不该真的调模型')


class DeadLLM:
    """模型这条路挂掉：运行时应转规则兜底。"""
    enabled = True

    def chat(self, *a, **k):
        raise LLMError('假装模型超时')


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


def main():
    tmp = tempfile.TemporaryDirectory()
    rt, data, cfg = build(tmp.name)

    # ================================================ [3] 词表只有一份
    print('\n[3] 生成动作词的词表全站只有一份（两处各抄一份就会互相打架）')
    ok(GEN.GEN_WORDS is rules.GEN_WORDS, '技能的闸门用的是 rules 里那一份')
    ok(GEN.CEDE_WORDS is rules.CEDE_WORDS, '「交回选择权」的词表也是同一份')
    for t, want in (('给我生成一张国风的图案', True),
                    ('帮我画一张水墨的', True),
                    ('出一张卡通图案', True),
                    ('就这样出', True),
                    ('随便你定', True),
                    ('我想要一张卡通图案', False),
                    ('帮我找找水墨的', False),
                    ('有没有国风的款', False),
                    ('帮我把这张照片描下来', False)):
        ok_eq(rules.wants_generate(t), want, '「%s」→ wants_generate=%s' % (t, want))
    ok(rules.hits('生成 一张', rules.GEN_WORDS), '词中间有空格也认（用户常这么打）')

    # ================================================ [1] 明说要出图 → 闸门放行
    print('\n[1] 用户明说要出图 → 不再被「先查库」拦下')
    s1 = rt.sessions.get('t-gen-1')
    s1.brief = {'style': '新中式国风', 'element': '花卉'}
    s1.last_user_text = MSG                      # 用户原话里有「生成」
    ok(rules.wants_generate(s1.last_user_text), '这句话被认成"要生成"')
    p = rt.registry.run('generate_pattern', {'confirmed': False}, rt._ctx(s1))
    ok_eq(p.get('need_search'), None, '没有被 need_search 拦')
    ok_eq(p.get('need_confirm'), True, '直接拿到待确认方案')
    ok_eq(p.get('cost'), 240, '报价按配置的 4 张算（60×4）')
    ok(p.get('summary'), '方案摘要也在', p.get('summary'))

    # ================================================ [2] 没说要生成 → 闸门照旧
    print('\n[2] 「我想要 / 帮我找」照旧要先查库 —— 闸门没被拆掉')
    s2 = rt.sessions.get('t-gen-2')
    # 卡给齐，专门验**闸门一**（先查库）；卡不齐时先冒出来的是闸门二 need_brief
    s2.brief = {'style': '新中式国风', 'element': '花卉',
                'color': '莫兰迪灰调', 'scene': '女装连衣裙'}
    s2.last_user_text = '我想要一张国风花卉的图案'
    b = rt.registry.run('generate_pattern', {'confirmed': False}, rt._ctx(s2))
    ok_eq(b.get('need_search'), True, '「我想要…」还是被拦下')
    ok(b.get('need_confirm') is None, '被拦时不挂卡片')

    # 卡不齐 + 没说要生成 → 先回去问缺的维度（闸门二）
    s2b = rt.sessions.get('t-gen-2b')
    s2b.brief = {'style': '新中式国风', 'element': '花卉'}
    s2b.last_user_text = '我想要一张国风花卉的图案'
    b2 = rt.registry.run('generate_pattern', {'confirmed': False}, rt._ctx(s2b))
    ok_eq(b2.get('need_brief'), ['color', 'scene'], '卡不齐 → 先问缺的维度')
    ok(b2.get('need_confirm') is None, '同样不挂卡片')

    # 卡片上的按钮（retune）永远不被拦
    s2.last_user_text = MSG
    t = rt.registry.run('generate_pattern', {'confirmed': False, '_retune': True},
                        rt._ctx(s2))
    ok_eq(t.get('need_confirm'), True, '带 _retune 的提案不走向闸门')

    # ================================================ [4][5][6] 规则兜底
    print('\n[4] 兜底认生成意图：明说要出图就照办，别把人往花型库里带')
    s4 = rt.sessions.get('t-gen-4')
    s4.brief = {'style': '新中式国风', 'element': '花卉'}
    text, _ = rules.reply('designer', MSG, s4, data.get('patterns') or [],
                          rt.imagegen)
    ok('花型选择页' not in text, '不再甩「打开花型选择页」', text[:80])
    ok(('生成' in text) or ('出图' in text), '说的是去出图', text[:80])
    ok('240' in text, '把点数报出来了', text[:120])

    print('\n[5] 兜底文案只能引用**用户这一轮真说过**的维度')
    s5 = rt.sessions.get('t-gen-5')
    # 需求卡里躺着几轮前定的配色/场景，用户这一轮一个字都没提
    s5.brief = {'style': '新中式国风', 'color': '莫兰迪灰调', 'scene': '女装连衣裙'}
    s5.last_user_text = MSG
    text, _ = rules.reply('designer', MSG, s5, data.get('patterns') or [],
                          rt.imagegen)
    ok('莫兰迪灰调' not in text, '没把需求卡旧值说成「你说的」', text[:120])
    ok('女装连衣裙' not in text, '场景同理', text[:120])

    print('\n[6] 需求卡一个维度都没有时不硬凑着画')
    s6 = rt.sessions.get('t-gen-6')
    s6.brief = {k: None for k in ('style', 'element', 'color', 'scene')}
    text, _ = rules.reply('designer', '随便给我生成一张吧', s6,
                          data.get('patterns') or [], rt.imagegen)
    ok('花型选择页' not in text, '不甩花型库清单', text[:80])
    ok('?' in text or '？' in text, '改成先要一样最要紧的', text[:100])

    # ================================================ [7] 模型挂了 → 服务端挂方案
    print('\n[7] 模型这条路挂掉时，服务端自己把方案卡挂出来（"先做后说"）')
    tmp2 = tempfile.TemporaryDirectory()
    rt2, data2, _ = build(tmp2.name, llm=DeadLLM())
    s7 = rt2.sessions.get('t-gen-7')
    s7.brief = {'style': '新中式国风', 'element': '花卉'}
    out = rt2.chat('designer', MSG, 't-gen-7')
    ok_eq(out.get('mode'), 'rule', '降级到规则引擎（模型确实挂了）')
    pend = out.get('pending') or {}
    ok_eq(pend.get('action'), 'generate_pattern', '方案卡真挂上了')
    ok_eq(pend.get('cost'), 240, '卡上的点数是对的')
    ok('生成' in (out.get('reply') or '') or '出图' in (out.get('reply') or ''),
       '回复说的是去出图', (out.get('reply') or '')[:80])
    ok('花型选择页' not in (out.get('reply') or ''),
       '不再是"去花型库里挑"', (out.get('reply') or '')[:120])
    ok('失败' not in (out.get('reply') or '') and 'LLMError' not in (out.get('reply') or ''),
       '技术词不甩给用户')

    # 卡挂在**设计师自己那一槽**里，别的角色看不见
    ok_eq((rt2._pending_of(s7, 'engineer')), None, '织印师那槽没被写脏')

    # 需求卡全空时不硬凑（模型挂了也一样）
    s7b = rt2.sessions.get('t-gen-7b')
    s7b.brief = {k: None for k in ('style', 'element', 'color', 'scene')}
    out2 = rt2.chat('designer', '随便给我生成一张吧', 't-gen-7b')
    ok_eq(out2.get('pending'), None, '卡上什么都没有 → 不挂方案，先问清楚')

    # 用户只在找的时候也不该被挂生成卡
    s7c = rt2.sessions.get('t-gen-7c')
    s7c.brief = {'style': '新中式国风', 'element': '花卉'}
    out3 = rt2.chat('designer', '帮我找找国风花卉的', 't-gen-7c')
    ok_eq(out3.get('pending'), None, '只是找 → 不挂生成卡')

    tmp.cleanup()
    tmp2.cleanup()
    print('\n结果：%d 通过 / %d 失败' % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
