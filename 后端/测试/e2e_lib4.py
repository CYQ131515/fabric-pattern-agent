#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真链路验证（打真模型，不打生图）：跑法 python3 tools/e2e_lib4.py

    [1] 花型师：需求跟花型库对不上 → 应当引导 AI 生成，且一次报 4 张
    [1c] 需求卡齐了 + 库里没对上 → 这时才该主动把 AI 生成摆出来
    [2] 织印师：传照片说描稿 → 必须走通，不再出现「模型调用失败」那种话
    [3] 「我想要…」→ 必须先查库；没查过库不许冒出生成提案（服务端硬闸门）
    [4] 需求卡没补齐 → 先问用户，不许拿半张卡推生成/报价；用户明说了才放行

只走到「提案」为止，不点确认，所以不产生出图额度。
"""
import base64
import json
import os
import re
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.dirname(HERE)
sys.path.insert(0, AGENT)

BASE = os.environ.get('AGENT_BASE', 'http://127.0.0.1:8787')
STAMP = str(int(time.time()))
PHOTO = os.path.join(HERE, 'samples', '小碎花测试图.png')
if not os.path.exists(PHOTO):
    PHOTO = None

PASS, FAIL = 0, 0


def ok(cond, name, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('  OK   %s' % name)
    else:
        FAIL += 1
        print('  FAIL %s%s' % (name, (' → ' + str(extra)) if extra else ''))


def post(path, payload):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=240) as r:
        return json.loads(r.read().decode('utf-8'))


def chat(agent, message, sid, image=None):
    p = {'agent': agent, 'message': message, 'session_id': sid}
    if image:
        p['image'] = image
        p['image_mime'] = 'image/png'
    t0 = time.time()
    res = post('/api/chat', p)
    print('       （%s 用了 %.1fs，技能 %s，模式 %s）'
          % (agent, time.time() - t0, res.get('skills'), res.get('mode')))
    return res


def show(tag, text):
    print('       ┌─ %s' % tag)
    for line in (text or '').split('\n'):
        print('       │ ' + line)
    print('       └─')


TECH = ('模型调用失败', 'max_tokens', '规则模式', 'LLMError', 'Traceback',
        '切换到本地')


def main():
    print('BASE=%s\n' % BASE)

    # ============================================ [1] 库里没对上 → 引导 AI 生成
    print('[1] 花型师：需求跟库对不上（电子游戏元素 + 淡绿 + 床品）')
    sid = 'e2e-lib4-des-%s' % STAMP
    r1 = chat('designer', '我想要电子游戏元素的花型，配色要淡绿，用在床品上，帮我找找', sid)
    show('第 1 轮回复', r1.get('reply'))
    ok(r1.get('mode') == 'llm', '第 1 轮走的是模型（不是兜底）', r1.get('mode'))
    rep = r1.get('reply') or ''
    for bad in TECH:
        ok(bad not in rep, '回复里不出现「%s」' % bad)
    # 需求卡还差「风格」这一维，所以第一轮的正确动作是**先把缺的问出来**，
    # 不是急着推 AI 生成。硬断言"第一轮就得提生成"会随机挂 ——
    # 模型有时候会自己替用户定风格，有时候会先问，两种都对。
    ok(r1.get('pending') is None, '先问用户，不擅自提方案（提案要等用户点头）')
    ok(('？' in rep or '?' in rep or '风格' in rep),
       '把缺的那一维问出来了（而不是硬推近似款）', rep[:80])

    print('\n[1b] 用户说「好，就按这个生成」')
    r2 = chat('designer', '好，就按这个生成吧', sid)
    show('第 2 轮回复', r2.get('reply'))
    pend = r2.get('pending') or {}
    ok(pend.get('action') == 'generate_pattern', '提出生成方案', pend.get('action'))
    ok(pend.get('count') == 4, '方案里是 4 张', pend.get('count'))
    ok(pend.get('cost') == 240, '报价 240 点（60×4 的总价）', pend.get('cost'))
    ok(not pend.get('args', {}).get('confirmed'), '提案绝不带 confirmed')
    # 真正的硬要求在这儿：库里没对上 → 必须问「要不要 AI 生成」，
    # 并且把"一次 4 张、一共多少点"讲明白。
    # 判**整段对话**而不是某一轮：模型有时在第一轮就把库和报价讲完、
    # 第二轮只给确认卡片，有时反过来。用户点确认前这些话必须都说到了，
    # 至于分在哪一轮说，那是模型的自由。
    talk = rep + '\n' + (r2.get('reply') or '')
    ok(('4 张' in talk or '四张' in talk), '说清一次给 4 张', talk[:140])
    ok('240' in talk, '总点数说在明面上', talk[:140])

    # 查没查库是两种走法，各有各的硬要求 —— 别指望模型每次都走同一条路：
    #   查了库 → 必须如实说"没对上"，并主动提议 AI 生成；
    #   没查（用户这句已经点名「生成吧」，人设允许直接提方案）→ 不必再问一遍，
    #   但**不许凭空说库里有没有**（人设里明令禁止）。
    searched = ('search_patterns' in (r1.get('skills') or [])
                or 'search_patterns' in (r2.get('skills') or []))
    if searched:
        # 「没真正对得上的」「都没命中」「没有对得上的」都算说了 —— 别用死字符串卡措辞
        said_miss = ('库' in talk
                     and re.search(r'(没|无|不)[^。！\n]{0,12}(对上|对得|命中|有)', talk))
        ok(bool(said_miss), '查过库 → 如实说了"没对上"', talk[:120])
        # 说清了没对上之后，"生成这条路"必须摆出来 —— 但**提案卡片就算数**，
        # 不必强求出现「AI」两个字：这一轮卡缺风格，正确动作是先问（闸门也要求这样），
        # 用户回"就按这个生成"之后才挂卡片。死抠字眼会让用例随机挂。
        offered = bool(pend.get('action')) or bool(
            re.search(r'AI|生成一版|出一版|生成一份|帮我生成', talk))
        ok(offered, '库里没对上 → 把生成这条路摆出来了（提案卡片也算）', talk[:100])
    else:
        ok(not any(x in talk for x in ('库里有', '库里没有', '库里没')),
           '没查库 → 没有凭空说库里有没有', talk[:120])

    # ============== [1c] 需求卡齐了、库里又没对上 → 这时才该主动推 AI 生成
    # 上一条把「先问」和「推生成」的先后摆对了：卡不齐先问（闸门扣着），
    # 卡齐了、库里又真没有，才轮到主动问一句"要不要 AI 生成"。
    print('\n[1c] 卡齐了 + 库里没对上 → 主动把 AI 生成摆到台面上')
    sid1c = 'e2e-lib4-full-%s' % STAMP
    r1c = chat('designer',
               '我想要电子游戏元素的花型，配色淡绿，场景床品，风格复古波普，帮我找找', sid1c)
    show('第 1 轮回复', r1c.get('reply'))
    rep = r1c.get('reply') or ''
    seen1c = r1c.get('skills') or []
    full_brief = {k: v for k, v in (r1c.get('brief') or {}).items() if v}
    if len(full_brief) >= 4 and 'search_patterns' in seen1c:
        ok(re.search(r'(没|无|不)[^。！\n]{0,12}(对上|对得|命中)', rep),
           '四维齐全 → 查了库，并如实说"没对上"', rep[:120])
        ok(re.search(r'AI|生成一版|出一版|生成一份', rep)
           or bool((r1c.get('pending') or {}).get('action')),
           '库里没对上 → 主动把生成摆出来（或直接挂方案等确认）', rep[:110])
    else:
        # 模型把某一维自己定了、或没查库 —— 这不是本节的判定点，如实标出来别假绿
        print('       （这一轮卡只齐了 %d 维、技能 %s，跳过本节判定）'
              % (len(full_brief), seen1c))

    # ============================================ [2] 织印师描稿
    print('\n[2] 织印师：传照片说描稿（上次就是这里被 token 卡住）')
    sid2 = 'e2e-lib4-eng-%s' % STAMP
    img = None
    if PHOTO:
        with open(PHOTO, 'rb') as f:
            img = base64.b64encode(f.read()).decode()
        print('       用样图：%s（%.1f KB）' % (os.path.basename(PHOTO),
                                              len(img) / 1024.0))
    r3 = chat('engineer', '把这张图片给我描一下', sid2, img)
    show('织印师回复', r3.get('reply'))
    ok(r3.get('ok') is True, '接口正常返回')
    ok(r3.get('mode') == 'llm', '走的是模型（没被 token 卡回规则兜底）', r3.get('mode'))
    r3rep = r3.get('reply') or ''
    for bad in TECH:
        ok(bad not in r3rep, '回复里不出现「%s」' % bad)
    ok('trace_pattern' in (r3.get('skills') or []), '调到了描稿技能', r3.get('skills'))
    pend3 = r3.get('pending') or {}
    ok(pend3.get('action') == 'trace_pattern', '给出描稿方案', pend3.get('action'))
    ok(bool(pend3.get('qc')), '方案里带照片体检结论', pend3.get('qc'))
    ok(pend3.get('cost') == 60, '描稿按 60 点报', pend3.get('cost'))
    ok('四方连续' in r3rep or '花型稿' in r3rep, '产出说清了（不含四方连续）', r3rep[:100])

    # ============================== [3] 「我想要」→ 先查库，别直接开生成
    # 用户原话：「用户并没有说要生成，只是说我想要，那就应该优先先在花型库里做匹配，
    # 花型库没有匹配的才要生成」。这条现在由**服务端硬闸门**保证，不靠模型自觉：
    # 需求卡上有条件却没按同一套条件查过库 → generate_pattern 只拿回 need_search，卡片不挂。
    print('\n[3] 「我想要…」→ 先查库，库里没有才谈生成')
    sid3 = 'e2e-lib4-search-%s' % STAMP
    seen, pends = [], []
    r4 = chat('designer', '我想要莫兰迪色系的抽象几何花型，用在女装衬衫上，帮我找找', sid3)
    show('第 1 轮回复', r4.get('reply'))
    seen += (r4.get('skills') or [])
    if (r4.get('pending') or {}).get('action'):
        pends.append(r4['pending']['action'])
    ok('search_patterns' in seen, '「我想要…」第一轮就查了库（没直接开生成）', seen)

    r5 = chat('designer', '风格就莫兰迪抽象几何、女装衬衫，按这个来', sid3)
    show('第 2 轮回复', r5.get('reply'))
    seen += (r5.get('skills') or [])
    if (r5.get('pending') or {}).get('action'):
        pends.append(r5['pending']['action'])

    gen_pends = [a for a in pends if 'generate' in a]
    # 硬不变量：只要冒出了生成提案，这个会话里此前一定查过库。
    ok(('search_patterns' in seen) or (not gen_pends),
       '有生成提案 → 之前必已查过库（闸门生效）', '技能=%s 提案=%s' % (seen, pends))
    if gen_pends:
        print('       （本轮出了生成提案 %s，且已查过库 —— 符合预期）' % gen_pends)

    # ================== [4] 需求卡没补齐 → 先问用户，别拿半张卡推生成
    # 用户原话：「怎么现在又不根据需求卡问用户了」。上一轮把「先查库」加进来时，
    # 把「按需求卡的缺口追问」挤成了尾巴：卡上只有一个「卡通」，机器人却先报
    # 「一次 4 张、共 240 点」。现在服务端硬闸门（need_brief）扣住方案。
    print('\n[4] 卡没补齐 → 先问用户（别拿半张卡推生成、别报价）')
    sid4 = 'e2e-lib4-half-%s' % STAMP
    r6 = chat('designer', '我想要一张卡通图案', sid4)
    show('第 1 轮回复', r6.get('reply'))
    rep6 = r6.get('reply') or ''
    ok(r6.get('mode') == 'llm', '走的是模型', r6.get('mode'))
    ok((r6.get('pending') or {}).get('action') is None,
       '卡上只有一个「卡通」→ 不提生成方案', (r6.get('pending') or {}).get('action'))
    ok(not re.search(r'\d{2,3}\s*点', rep6),
       '卡没齐就不报点数', (re.search(r'[^，。；\n]{0,14}\d{2,3}\s*点[^，。；\n]{0,8}', rep6) or [''])[0])
    ok(('？' in rep6 or '?' in rep6)
       or any(w in rep6 for w in ('元素', '配色', '用在哪', '什么品类', '什么场景')),
       '把缺的那一维问出来（元素 / 配色 / 用在哪）', rep6[:90])
    for bad in TECH:
        ok(bad not in rep6, '回复里不出现「%s」' % bad)

    print('\n[4b] 用户明说「生成吧」→ 就该放行（别把闸门用成"永远不给"）')
    r7 = chat('designer', '就按现在这些，帮我生成一份吧', sid4)
    show('第 2 轮回复', r7.get('reply'))
    pend7 = r7.get('pending') or {}
    ok(pend7.get('action') == 'generate_pattern',
       '用户明说要生成 → 方案放行', '技能=%s 提案=%s'
       % (r7.get('skills'), pend7.get('action')))

    print('\n结果：%d 通过 / %d 失败' % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
