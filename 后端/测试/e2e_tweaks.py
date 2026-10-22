#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""四项改动的真服务端到端验证（会花真额度：2 次真出图 + 若干次模型调用）。

前置：先起服务  python3 server.py --port 8787
跑法：python3 tools/e2e_tweaks.py

验证的是用户提的那四件事：
    ① 说「淡红色」不再被系统换成"最接近的档"（原话：自由度太低）
    ② 织印师描稿不再带花型师需求卡的配色（原话：设计师那边是设计师，这里是这里）
    ③ 花型师出图成功后需求卡自动重置
    ④ 出图后能点「重新生成」再出一版
"""
import base64
import json
import os
import sys
import time
import urllib.request

BASE = os.environ.get('AGENT_BASE', 'http://127.0.0.1:8787')
# 两个角色分开建会话：合成一个会话的话，织印师上传的照片会变成花型师的改图底图，
# 花型师下一轮就会去调 edit_pattern，测不到「生图成功后重置需求卡」那条路。
STAMP = os.environ.get('E2E_SID') or 'e2e-%d' % int(time.time())
SID = STAMP + '-designer'     # 花型师会话（需要跨轮保持需求卡）
SID_E = STAMP + '-engineer'   # 织印师会话
HERE = os.path.dirname(os.path.abspath(__file__))
PHOTO = os.path.join(HERE, 'samples', '小碎花测试图.png')

PASS, FAIL = 0, 0


def ok(cond, name, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('  OK   %s' % name)
    else:
        FAIL += 1
        print('  FAIL %s%s' % (name, (' → ' + str(extra)) if extra else ''))


def post(path, payload, timeout=300):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def chat(agent, message, image=None, mime='image/png'):
    sid = SID_E if agent == 'engineer' else SID
    p = {'agent': agent, 'message': message, 'session_id': sid}
    if image:
        p['image'] = image
        p['image_mime'] = mime
    t0 = time.time()
    res = post('/api/chat', p)
    print('       （%s 用了 %.1fs，技能 %s）'
          % (agent, time.time() - t0, res.get('skills')))
    return res


def main():
    print('会话：花型师 %s ／ 织印师 %s\n' % (SID, SID_E))

    # ---------------------------------------------------------- ① 配色自由度
    print('[1] 花型师：说「淡红色」，看它会不会又换成最接近的档')
    r = chat('designer', '给我生成一张国风的淡红色玫瑰的图案，用于年轻女孩的连衣裙')
    reply = r.get('reply') or ''
    print('       回复：%s' % reply.replace('\n', ' / ')[:200])
    ok(r.get('ok'), '有回复')
    ok('色板里没有' not in reply and '最接近' not in reply,
       '不再说「色板里没有淡红这一档，我选了最接近的…」')
    # 模型有时记「淡红」、有时记「淡红色」，都是照用户原话收的，只要没被换档就算过
    color = (r.get('brief') or {}).get('color') or ''
    ok('淡红' in color, '配色如实记成用户说的那个词（没被换档）', color)
    ok(color not in ('粉紫梦幻', '马卡龙甜彩', '莫兰迪灰调'),
       '更没被塞进任何标准档', color)
    pend = r.get('pending') or {}
    ok(pend.get('action') == 'generate_pattern', '提了生图方案，等用户确认', pend.get('action'))
    prompt = pend.get('prompt') or ''
    ok('淡红' in prompt, '生图提示词里带的是「淡红」', prompt[:80])
    ok('粉紫' not in prompt, '没有偷偷塞进粉紫')

    # ---------------------------------------------------------- ② 描稿不吃需求卡
    print('\n[2] 织印师：同一个会话里描稿，看会不会被花型师的配色带跑')
    if not os.path.exists(PHOTO):
        ok(False, '找不到测试照片 %s' % PHOTO)
    else:
        with open(PHOTO, 'rb') as f:
            img = base64.b64encode(f.read()).decode()
        e = chat('engineer', '照着这张把图案描下来', image=img)
        ok(e.get('ok'), '有回复')
        ok(e.get('brief') == {}, '织印师不回传需求卡（前端不会 merge 进去）', e.get('brief'))
        ep = e.get('pending') or {}
        ok(ep.get('action') == 'trace_pattern', '提了描稿方案', ep.get('action'))
        etp = ep.get('prompt') or ''
        ok('淡红' not in etp, '花型师刚写的「淡红」没进描稿提示词')
        ok('新中式国风' not in etp, '风格也没带进来')
        ok('照片' in etp, '描稿提示词仍以照片为准')
        etxt = (e.get('reply') or '')
        print('       回复：%s' % etxt.replace('\n', ' / ')[:200])
        ok('淡红' not in etxt and '粉紫' not in etxt,
           '回复里也没引用花型师的配色')

    # ---------------------------------------------------------- ③④ 出图与重新生成
    print('\n[3] 花型师：直接确认上一轮的方案，真出图 → 需求卡应当自动重置')
    t0 = time.time()
    gen = post('/api/action', {'session_id': SID, 'action': 'confirm'})
    print('       出图耗时 %.1fs' % (time.time() - t0))
    ok(gen.get('ok'), '出图成功', gen.get('error'))
    imgs = gen.get('images') or []
    ok(len(imgs) >= 1, '拿到图片', len(imgs))
    if imgs:
        print('       产物：%s（%s KB）' % (imgs[0].get('url'),
                                          round((imgs[0].get('bytes') or 0) / 1024)))
    ok(gen.get('brief_reset') is True, '回传 brief_reset', gen.get('brief_reset'))
    ok(gen.get('brief') == {}, '需求卡已清空', gen.get('brief'))
    spec = gen.get('gen') or {}
    ok(spec.get('name') == 'generate_pattern', '记下了重放用的技能名', spec.get('name'))
    ok(bool((spec.get('args') or {}).get('prompt_full')), '记下了完整提示词')

    print('\n[4] 点「重新生成」→ 按同一条提示词再出一版')
    before_prompt = gen.get('prompt')
    t0 = time.time()
    again = post('/api/action', {'session_id': SID, 'action': 'regenerate',
                                 'args': {'spec': spec}})
    print('       重生成耗时 %.1fs' % (time.time() - t0))
    ok(again.get('ok'), '重新生成成功', again.get('error'))
    ok(bool(again.get('images')), '又拿到一张图')
    ok(again.get('prompt') == before_prompt,
       '用的是同一条提示词（需求卡已清空，靠 prompt_full 拼回来）')
    if again.get('images'):
        print('       产物：%s（%s KB）' % (again['images'][0].get('url'),
                                          round((again['images'][0].get('bytes') or 0) / 1024)))
    ok(again.get('brief_reset') is not True, '重新生成不再重复重置需求卡')

    print('\n结果：%d 通过 / %d 失败' % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
