#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""四项细化回归（服务端，不连网、不花钱）。

    [1] 配色自由度：词表外的说法（「淡红」）照收，不再被换成"最接近的档"
    [2] 描稿不读需求卡：花型师的配色不许渗进织印师的描稿提示词
    [3] 设计师生图成功后自动重置需求卡（改图/描稿不重置）
    [4] 重新生成：last_gen 记录 + regenerate 重放同一条提示词

跑法：python3 tools/test_v06_tweaks.py
生图走 mock provider，LLM 被替换成"未配置"，所以全程零网络。
"""
import base64
import json
import os
import struct
import sys
import tempfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.dirname(HERE)
sys.path.insert(0, AGENT)

os.environ['PATTERNFLOW_IMAGE_PROVIDER'] = 'mock'
# 出图产物挪去临时目录：不然每跑一次回归就往项目的 generated/ 里堆几十张 mock 图，
# 跟用户的真图混在一起分不出来。
os.environ['PATTERNFLOW_IMAGE_OUTDIR'] = tempfile.mkdtemp(prefix='pf-test-img-')
# 图库目录也挪走：出图会自动入库，别把测试产物混进真图库
os.environ['PATTERNFLOW_LIBRARY_DIR'] = tempfile.mkdtemp(prefix='pf-test-lib-')
# 埋点也挪走：跑一次回归会记下几十条出图/提案，混进真看板就成了假数据
os.environ['PATTERNFLOW_TELEMETRY_DIR'] = tempfile.mkdtemp(prefix='pf-test-tel-')

from core.config import Config                        # noqa: E402
from core.persona import load_all as load_personas    # noqa: E402
from core.skills import SkillRegistry                 # noqa: E402
from core.session import Session, SessionStore        # noqa: E402
from core.runtime import AgentRuntime                 # noqa: E402
from core.imagegen import build_prompt, free_color_en  # noqa: E402

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


# ---------------------------------------------------------------- 造图 / 搭环境
def png(w, h, ch, px):
    raw = bytearray()
    stride = w * ch
    for y in range(h):
        raw.append(0)
        raw += px[y * stride:(y + 1) * stride]

    def chunk(tag, body):
        return (struct.pack('>I', len(body)) + tag + body
                + struct.pack('>I', zlib.crc32(tag + body) & 0xFFFFFFFF))
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2 if ch == 3 else 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(bytes(raw), 6))
            + chunk(b'IEND', b''))


def make_photo(w=900, h=900):
    """造一张像照片的图：白底蓝花，梯度够，体检能过。描稿要用。"""
    px = bytearray(w * h * 3)
    for y in range(h):
        for x in range(w):
            i = (y * w + x) * 3
            cx, cy = x % 120, y % 120
            d = ((cx - 60) ** 2 + (cy - 60) ** 2) ** 0.5
            if d < 26:
                px[i], px[i + 1], px[i + 2] = 40, 70, 150
            elif d < 34:
                px[i], px[i + 1], px[i + 2] = 120, 150, 210
            else:
                px[i] = px[i + 1] = px[i + 2] = 252
    return png(w, h, 3, px)


def b64(blob):
    return base64.b64encode(blob).decode()


class OffLLM:
    """把模型关掉，逼运行时走本地规则 —— 测试不连网、不花额度。"""
    enabled = False

    def chat(self, *a, **k):
        raise AssertionError('测试里不该真的调模型')


def build(tmpdir):
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
    rt.llm = OffLLM()
    return rt, data


def main():
    tmp = tempfile.TemporaryDirectory()
    rt, data = build(tmp.name)

    # ============================================================ [1] 配色自由度
    print('\n[1] 配色自由度：用户说得出颜色的，照他说的来')
    ctx = {'vocab': data.get('vocab') or {}}
    r = rt.registry.run('update_brief', {'color': '淡红'}, ctx)
    ok_eq(r.get('accepted', {}).get('color'), '淡红',
          '词表外的「淡红」被采纳（不再退回）')
    ok(bool(r.get('off_vocab')), '但会记一条 off_vocab 提示')
    note = ((r.get('off_vocab') or [{}])[0].get('note') or '')
    ok('已按用户说的采用' in note, '提示里写明「按用户说的采用」', note)
    ok('不要替他改成别的说法' in note, '提示里禁止模型替用户换档')
    ok('rejected' not in r, '不再有「被拒绝」这个出口')

    r2 = rt.registry.run('update_brief', {'color': '粉紫梦幻'}, ctx)
    ok(not r2.get('off_vocab'), '标准档位不产生 off_vocab 噪音')

    ok('palette' in free_color_en('雾霾蓝'), '词表外颜色能拼出英文提示',
       free_color_en('雾霾蓝'))
    ok_eq(free_color_en(''), '', '空值不炸')
    p = build_prompt({'style': '法式浪漫', 'element': '花卉', 'color': '淡红',
                      'scene': '女装连衣裙'})
    ok('淡红' in p, '自由配色原样进中文提示词')
    ok('pale red' in p, '并且补上英文，模型抓得更准')

    # ============================================================ [2] 描稿不吃需求卡
    print('\n[2] 描稿不读需求卡（设计师那边是设计师）')
    sess = rt.sessions.get('t-tweak-trace')
    sess.last_photo = {'b64': b64(make_photo()), 'mime': 'image/png'}
    sess.brief = {'style': '法式浪漫', 'element': '花卉',
                  'color': '粉紫梦幻', 'scene': '女装连衣裙'}
    ctx = rt._ctx(sess)
    ok_eq(ctx['brief'].get('color'), '粉紫梦幻', '上下文里确实带着花型师的配色（构造前提）')
    tr = rt.registry.run('trace_pattern', {'confirmed': False}, ctx)
    ok(tr.get('need_confirm') is True, '描稿正常提案', tr)
    tp = tr.get('prompt') or ''
    ok('粉紫梦幻' not in tp, '花型师的配色没进描稿提示词')
    ok('法式浪漫' not in tp, '花型师的风格也没进')
    ok('女装连衣裙' not in tp, '场景同样没进')
    # 2026-10-20 起：描稿默认提示词移到算法侧，服务端传的 prompt 只装
    # 「用户本轮额外要求」——没有额外要求时它就是空的（需求卡照样不能进来）。
    ok_eq(tp, '', '没有额外要求时 prompt 为空（默认提示词归算法侧）')

    tr2 = rt.registry.run('trace_pattern',
                          {'confirmed': False, 'prompt': '配色改成雾霾蓝'}, ctx)
    ok('雾霾蓝' in (tr2.get('prompt') or ''),
       '用户这一轮亲口说的要求照旧能进', tr2.get('prompt'))

    # ============================================================ [3] 出图后重置需求卡
    print('\n[3] 设计师生图成功后需求卡自动重置')
    sid = 't-tweak-gen'
    sess = rt.sessions.get(sid)
    sess.brief = {'style': '新中式国风', 'element': '花卉',
                  'color': '淡红', 'scene': '女装连衣裙'}
    res = rt.run_action(sid, 'generate_pattern', {})
    ok(res.get('ok') is True, '生图执行成功', res)
    ok(res.get('brief_reset') is True, '回传 brief_reset（前端要同步清本地那份）')
    ok(not any(sess.brief.values()), '会话里的需求卡被清空', sess.brief)
    ok_eq(res.get('brief'), {}, '回给前端的 brief 是空的')
    gen = res.get('gen') or {}
    ok_eq(gen.get('name'), 'generate_pattern', '记下了重放要用的技能名')
    ok(bool(gen.get('args', {}).get('prompt_full')), '记下了完整提示词（重放的依据）')
    ok('confirmed' not in gen.get('args', {}), '记录里不带 confirmed（不能绕过闸门）')

    print('\n[3b] 改图 / 描稿不重置需求卡，失败也不重置')
    sess.brief = {'style': '法式浪漫', 'element': '花卉',
                  'color': '淡红', 'scene': None}
    ed = rt.run_action(sid, 'edit_pattern', {'color': '雾霾蓝'})
    ok(ed.get('ok') is True, '改图执行成功', ed)
    ok(not ed.get('brief_reset'), '改图不重置需求卡')
    ok_eq(sess.brief.get('color'), '淡红', '需求卡原样留着')

    sid2 = 't-tweak-fail'
    sess2 = rt.sessions.get(sid2)
    sess2.brief = {'style': '法式浪漫', 'element': None, 'color': '淡红', 'scene': None}
    bad = rt.run_action(sid2, 'edit_pattern', {'color': '雾霾蓝'})   # 没有底图 → 失败
    ok(not bad.get('ok'), '没有底图时改图失败', bad)
    ok_eq(sess2.brief.get('color'), '淡红', '失败了需求卡不动')

    # ============================================================ [4] 重新生成
    print('\n[4] 重新生成 = 按同一套参数重放')
    ok_eq((rt.sessions.get(sid).last_gen or {}).get('name'), 'edit_pattern',
          '最近一次出图是改图，last_gen 跟着更新')
    again = rt.run_action(sid, 'regenerate', {})
    ok(again.get('ok') is True, '重放成功', again)
    ok_eq(again.get('prompt'), ed.get('prompt'),
          '重放用的是同一条提示词（需求卡已清空，靠 prompt_full 才拼得回来）')
    ok(bool(again.get('images')), '重放有产出')

    gen_first = rt.run_action(sid, 'generate_pattern', {})
    gen_again = rt.run_action(sid, 'regenerate', {})
    ok_eq(gen_again.get('prompt'), gen_first.get('prompt'), '生图重放也一致')
    ok_eq((gen_again.get('gen') or {}).get('args', {}).get('prompt_full'),
          (gen_first.get('gen') or {}).get('args', {}).get('prompt_full'),
          '重放后记录的提示词不变（可以反复重生成）')

    sess.brief = {'style': None, 'element': None, 'color': '雾霾蓝', 'scene': None}
    replay = rt.run_action(sid, 'regenerate', {})
    ok(not replay.get('brief_reset'), '重放不再重复重置需求卡（它是"再来一版"，不是新需求）')
    ok_eq(sess.brief.get('color'), '雾霾蓝', '重放期间用户刚写的需求不会被抹掉')

    spec = {'name': 'generate_pattern',
            'args': {'prompt_full': '面料印花花型设计，薄荷绿，抽象笔触。'}}
    by_client = rt.run_action(sid, 'regenerate', {'spec': spec})
    ok_eq(by_client.get('prompt'), spec['args']['prompt_full'],
          '前端带回来的 spec 优先（服务重启丢了会话也能重放）')

    empty = rt.run_action('t-tweak-nothing', 'regenerate', {})
    ok(not empty.get('ok'), '没有记录时报错', empty)
    ok('重新生成' in (empty.get('error') or ''), '错误说清是"没有可重新生成的记录"',
       empty.get('error'))

    bad_act = rt.run_action(sid, 'regenerate', {'spec': {'name': 'search_patterns'}})
    ok(not bad_act.get('ok'), '非出图类技能不许借重新生成执行', bad_act)

    # ============================================================ [5] 角色隔离
    print('\n[5] 需求卡不跨角色（对话层）')
    iso = 't-tweak-iso'
    sess = rt.sessions.get(iso)
    d = rt.chat('designer', '我想要淡红的玫瑰', iso, brief={'color': '粉紫梦幻'})
    ok_eq(sess.brief.get('color'), '粉紫梦幻', '花型师能写需求卡')
    ok_eq(d.get('brief', {}).get('color'), '粉紫梦幻', '并且把需求卡回传给前端')

    e = rt.chat('engineer', '照着这张描一下', iso, brief={'color': '蓝白青花'})
    ok_eq(sess.brief.get('color'), '粉紫梦幻', '织印师传进来的 brief 被忽略（不改会话）')
    ok_eq(e.get('brief'), {}, '织印师不回传需求卡（前端就不会 merge 进去）')

    print('\n[6] 系统提示里也不给织印师看需求卡')
    s_eng = rt._system('engineer', sess)
    ok('当前需求卡' not in s_eng, '织印师的系统提示里没有需求卡')
    ok('粉紫梦幻' not in s_eng, '也就不会冒出花型师那边的配色')
    ok('## 生图与改图' not in s_eng, '织印师看不到生图技能说明（它没有那个技能）')
    s_des = rt._system('designer', sess)
    ok('当前需求卡' in s_des, '花型师照旧看得到需求卡')
    ok('粉紫梦幻' in s_des, '内容也在')
    ok('## 生图与改图' in s_des, '花型师的生图说明照旧')

    # ============================================================ [7] 重载后底图不丢
    print('\n[7] 重启后底图不丢：刚生成的图还能接着改')
    sid7 = 't-tweak-reload'
    g7 = rt.run_action(sid7, 'generate_pattern', {'prompt': '底图恢复用例'})
    ok(g7.get('ok') is True, '先生成一张', g7.get('error'))
    li = rt.sessions.get(sid7).last_image or {}
    ok(bool(li.get('b64')), '内存里记下了底图')
    ok(bool(li.get('path')), '底图记录带着文件路径（重启后靠它读回来）', sorted(li.keys()))

    def resolve(url):
        name = os.path.basename((url or '').split('?')[0])
        p = os.path.join(rt.imagegen.outdir, name)
        return p if os.path.exists(p) else None

    store2 = SessionStore(os.path.join(tmp.name, 'sessions'), resolve=resolve)

    # 老格式：只写了 url、没写 path（修这个 bug 之前的记录长这样）
    s_old = Session('t-tweak-oldstyle')
    store2._mem['t-tweak-oldstyle'] = s_old
    store2.append(s_old, {'type': 'last_image', 'agent': 'designer',
                          'last_image': {'url': li.get('url'), 'mime': 'image/png',
                                         'origin': 'generated', 'prompt': '老记录'}})
    del store2._mem['t-tweak-oldstyle']
    back_old = store2.get('t-tweak-oldstyle')
    ok(bool((back_old.last_image or {}).get('b64')),
       '老记录（只有 url）也能按地址把图找回来')

    # 更老的格式：连 agent 都没有（按角色分槽之前的记录）。
    # 这种一律丢弃 —— 分不清是谁的底图，恢复回来就是给下一个机器人当"上文"用。
    s_legacy = Session('t-tweak-legacyimg')
    store2._mem['t-tweak-legacyimg'] = s_legacy
    store2.append(s_legacy, {'type': 'last_image',
                             'last_image': {'url': li.get('url'), 'mime': 'image/png',
                                            'origin': 'generated', 'prompt': '无主记录'}})
    del store2._mem['t-tweak-legacyimg']
    back_legacy = store2.get('t-tweak-legacyimg')
    ok(back_legacy.last_image is None,
       '没有 agent 的旧底图记录不恢复（分不清是谁的，恢复就会串台）')

    rt.sessions = store2                      # 模拟重启：换成从盘上重建的会话
    ok(bool((store2.get(sid7).last_image or {}).get('b64')), '重载后图体回来了')
    ed7 = rt.run_action(sid7, 'edit_pattern', {'color': '雾霾蓝'})
    ok(ed7.get('ok') is True, '重载之后改图成功（修之前这里会说"没有底图"）',
       ed7.get('error'))

    # ============================================================ [8] 跨机器人记忆隔离
    # 用户实测的 bug：先让织印师做四方连续，再切到花型师要花型图案，
    # 花型师开口就是"在现有花型上改配色"——因为它看见了织印师那条线。
    print('\n[8] 跨机器人不串味：织印师那条线，花型师看不见')
    sid8 = 't-tweak-xbot'
    s8 = rt.sessions.get(sid8)

    se = rt.run_action(sid8, 'generate_pattern', {'prompt': '织印师这边的底图'}, 'engineer')
    ok(se.get('ok') is True, '织印师这边出了一张图（模拟四方连续的底图）', se.get('error'))
    se_url = ((se.get('images') or [{}])[0] or {}).get('url')
    s8.act_as('engineer')
    ok(bool(s8.last_image), '织印师自己的槽里有底图')
    s8.act_as('designer')
    ok(s8.last_image is None, '花型师那一槽是空的（看不到织印师的底图）')

    sys_d = rt._system('designer', s8)
    ok('你手上有一张' not in sys_d, '花型师的系统提示里不说"你手上有一张底图"')
    ok('你手上没有底图' in sys_d, '系统提示明确告诉花型师：手上没图')
    ok('底图只认你自己这条线' in sys_d, '系统提示写死了这条边界（模型不许脑补别人的图）')

    rt.chat('engineer', '帮我把这张做成四方连续', sid8)
    rt.chat('designer', '给我一些花型图案', sid8)
    eh = json.dumps(s8.history('engineer'), ensure_ascii=False)
    dh = json.dumps(s8.history('designer'), ensure_ascii=False)
    ok(eh != dh, '两个机器人的历史不是同一份')
    ok(len(s8.history('designer')) == 2, '花型师的历史只有它自己的一问一答',
       len(s8.history('designer')))
    ok('四方连续' in eh, '织印师聊的四方连续在它自己那边')
    ok('四方连续' not in dh, '织印师聊的四方连续没出现在花型师的历史里')

    s8.act_as('designer')
    ok(s8.last_gen is None, '花型师没有可重放的记录（拿不到织印师那次）')
    bad_regen = rt.run_action(sid8, 'regenerate', {}, 'designer')
    ok(not bad_regen.get('ok'), '花型师点"重新生成"拿不到织印师的记录', bad_regen)

    # ============================================================ [9] 显式交接
    print('\n[9] 跨机器人传图走明路：点一下才过去')
    h = rt.hand_off(sid8, se_url, 'engineer')
    ok(h.get('ok') is True, '交接成功', h.get('error'))
    s8.act_as('engineer')
    ok(bool(s8.last_image), '织印师槽里有图了')
    ok((s8.last_image or {}).get('origin') == 'handoff', '来源标成 handoff（不是"我生成的"）')
    s8.act_as('designer')
    ok(s8.last_image is None, '交接只进目标那一槽，花型师那边还是空的')

    tmp.cleanup()
    print('\n结果：%d 通过 / %d 失败' % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
