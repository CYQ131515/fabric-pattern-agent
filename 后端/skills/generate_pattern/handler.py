"""文生图：需求卡 → 生图提示词 → 调生图模型 → 落盘。

确认闸门：confirm:true 的技能不会在第一次调用时就真跑，
先返回 need_confirm 方案（含点数），由服务端存成 pending_action，
等用户在界面上点「确认生成」才由 /api/action 确定性执行——扣费动作不交给模型。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from core.imagegen import build_prompt, last_image_record, ImageGenError
# 新版花型生成：关键词 → 九宫格元素 → 自动排版 → 无缝 tile，见 core/patgen.py。
# 老 imagegen 保留给「定向改图」和落盘；mock/回滚时这里会自动退回老路径。
from core.patgen import build_keywords, presets_for, density_for, scale_for
# 「用户这轮是不是在明确要出图」的词表只有一份，放在 core/rules.py ——
# 规则兜底和这里必须认同一套词，否则会出现"闸门放行了、兜底还把人往库里带"。
from core.rules import GEN_WORDS, CEDE_WORDS, hits as _hits

DIMS = ('style', 'element', 'color', 'scene')
DIM_CN = {'style': '风格', 'element': '元素', 'color': '配色', 'scene': '场景'}


def run(args, ctx):
    pat = ctx.get('patgen')
    # 用哪台发动机由「装没装好」决定：mock 模式 / 没配 key / 算法没装 → 退回老生图，
    # 这样演示与离线回归不会因为换算法而全红。
    use_pat = bool(pat is not None and getattr(pat, 'available', False))
    gen = pat if use_pat else ctx.get('imagegen')
    if gen is None:
        return {"error": "本服务没启用生图模块"}
    sess = ctx.get('session')
    brief = dict(ctx.get('brief') or {})
    extra = {}
    if sess is not None and getattr(sess, 'image_tags', None):
        extra = dict(sess.image_tags.get('tags') or {})

    # prompt 在两条路上含义不同：
    #   新算法 = 中文需求关键词（算法自己解析 + 拆元素 + 排版）；
    #   老生图 = 一条完整的中文生图提示词。
    # prompt_full 是「重新生成」时原样重放的参数，两条路都优先用它。
    prompt = (args.get('prompt_full') or '').strip()
    if not prompt:
        addon = (args.get('prompt') or '').strip()
        if use_pat:
            prompt = build_keywords(brief, extra, addon)
        else:
            prompt = build_prompt(brief, extra)
            if addon:
                prompt = addon + '。' + prompt

    # 张数：用户没指定就按配置的 variants 来（默认 4）。
    # 一次给几张不同的让用户挑，比一张一张试快得多——库里有对得上的就走检索，
    # 对不上才走生成，这时候"多给几个方向"尤其有用。
    default_n = max(1, int(getattr(gen, 'variants', 1) or 1))
    try:
        n = int(args.get('n') or default_n)
    except (TypeError, ValueError):
        n = default_n
    n = max(1, min(4, n))

    if not args.get('confirmed'):
        # ---- 闸门二：需求卡没补齐，就先回去问用户 ----
        # 实测（会话 pf-muwflqi6-fb8k2）：用户说「我想要一张卡通图案」，卡上只有
        # style，模型却把「要不要 AI 出 4 张、共 240 点」摆到主位，追问只剩尾巴。
        # 卡不齐的时候，按它检索本身是半盲的 —— 这时候得出的「库里没对上」
        # 根本不算数，更不该拿它去推生成。所以先扣住。
        #
        # 三种情况豁免：
        #   1. 用户在确认卡片上切张数/还原度 → `_retune`（方案早提过了，别再拦）
        #   2. 用户这轮明确说了要出图（「生成一张」「出图」…）
        #   3. 用户把选择权交回来了（「随便」「你决定」）
        # 再加一个上限：连打回 2 次就放行，免得用户答「嗯」时被卡死在追问里。
        #
        # `prompt_full` **不豁免**：真正的「重新生成」是 runtime.regenerate 带着
        # confirmed=True 重放的，压根不走到这里；提案路径上出现 prompt_full
        # 只可能是模型自己编的 —— 那正是要拦的情况。
        blocked = int(getattr(sess, 'brief_blocks', 0) or 0) if sess is not None else 0
        missing = [d for d in DIMS if not brief.get(d)]
        if missing and not args.get('_retune') and blocked < 2 and sess is not None:
            txt = getattr(sess, 'last_user_text', '') or ''
            if not _hits(txt, GEN_WORDS) and not _hits(txt, CEDE_WORDS):
                sess.brief_blocks = blocked + 1
                return {
                    "need_brief": missing,
                    "brief": {k: v for k, v in brief.items() if v},
                    "hint": "需求卡还差「%s」，**先别提方案、先别报价** —— "
                            "把最影响结果的那一项问出来（一次只问一个）。"
                            "用户想先看库里有什么的话，可以顺手用手上已有的条件查一次，"
                            "但这一轮的重点是问，不是推生成。"
                            "等他补齐、或者说「就按现在这些出」，再回来提方案。"
                            % '、'.join(DIM_CN[d] for d in missing),
                }

        # ---- 闸门一：花型库没有匹配的才要生成 ----
        # 「花型库没有匹配的才要生成」—— 这条必须由服务端说了算。
        # 出图是要扣点的，先查库是最省钱的路径；而模型很容易把
        # 「我想要一张卡通风格的图案」当成"生成指令"直接开提案（实测就是这样，
        # 用户抱怨"怎么现在都只会生成了"）。所以这里卡一道：
        # 需求卡上有条件、却没有按同一套条件查过库 → 打回，让它先检索。
        #
        # 两个豁免：
        #   `_retune` 是服务端 retune_pending 带进来的（用户在卡片上切张数/还原度）：
        #   那时候方案早就提过了，不能再拦一次，否则卡片上的按钮会突然点不动。
        #   用户**这一轮明说了要出图**（「生成 / 画一张 / 出图」…）：他已经自己做了
        #   "不看库、直接画"这个选择，再把他推回去检索就是没照办他的要求
        #   （用户原话：「我明显是想要生成图案，但是他不给我生成」）。
        #   没有动作词的「我想要一张卡通图案」照旧先查库 —— 那条闸门当初就是为它设的。
        want = {k: v for k, v in brief.items() if v}
        last = getattr(sess, 'last_search', None) if sess is not None else None
        said = getattr(sess, 'last_user_text', '') if sess is not None else ''
        asked_gen = _hits(said, GEN_WORDS) or _hits(said, CEDE_WORDS)
        if want and not args.get('_retune') and not asked_gen \
                and (not last or last.get('brief') != want):
            return {
                "need_search": True,
                "brief": want,
                "hint": "先调用 `search_patterns` 查一次库再提生成方案。"
                        "库里没有对得上的款才该用 AI 生成；"
                        "用户说「我想要…/帮我找…」时更要先查，别直接开提案。",
            }
        filled = {k: v for k, v in brief.items() if v}
        # 方案真挂出来了 → 这一串「追问」告一段落，计数归零
        if sess is not None:
            sess.brief_blocks = 0
        if use_pat:
            # 新版算法每版本身就是无缝 tile，不需要再让织印师做四方连续；
            # 但分层 PSD 仍然要交给织印师。
            summary = ("按需求卡%s生成 %d 版可直接平铺的无缝花型"
                       "（九宫格元素自动排版，接缝已逐像素校验）" % (
                           '（' + '、'.join(filled.values()) + '）' if filled else '（暂无标签）',
                           n))
            hint = ("告诉用户一次会出 %d 版、每版元素数量和排布都不同，而且每版都是"
                    "可平铺的无缝单元；说清一共多少点，让他确认后再生成。" % n)
        else:
            summary = "按需求卡%s生成 %d 张不一样的面料花型（%s）" % (
                '（' + '、'.join(filled.values()) + '）' if filled else '（暂无标签）',
                n, '平面花型稿，四方连续另由织印师处理')
            hint = "告诉用户一次会出 %d 张不同的、一共花多少点，让他确认后再生成。" % n
        return {
            "need_confirm": True,
            "action": "generate",
            "cost": gen.cost * n,
            "count": n,
            "variants": default_n,
            "prompt": prompt,
            "brief": filled,
            "summary": summary,
            "hint": hint,
        }

    try:
        if use_pat:
            # 排版参数从识图标签来（用户传了参考图时才有）；没有就用算法默认。
            res = gen.generate(prompt, n=n, tag='gen',
                               presets=presets_for(extra),
                               density=density_for(extra),
                               element_scale=scale_for(extra))
        else:
            res = gen.generate(prompt, n=n, tag='gen')
    except ImageGenError as e:
        return {"error": str(e)}

    files = res['files']
    if sess is not None and files:
        # 一次出多张时，底图默认取第一张（界面上「接着改」那几个按钮就是改它）。
        sess.last_image = last_image_record(files[0], prompt)
    got = len(files)
    if use_pat:
        note = (("已生成并保存 %d 版无缝花型（本来要 %d 版）。" % (got, n) if got < n
                 else "已生成并保存 %d 版无缝花型。" % got) +
                "告诉用户这些都是可直接平铺的无缝单元，每版元素数量和排布不同；"
                "哪版合意直接说编号，还能改配色/疏密度/元素，或者点「重新生成」再出一批；"
                "要分层 PSD 可以交给织印师。需求卡已自动清空。")
    else:
        note = (("已生成并保存 %d 张（本来要 %d 张）。" % (got, n) if got < n
                 else "已生成并保存 %d 张。" % got) +
                "告诉用户图已出，其中哪张合意可以直接说；还能改配色/疏密度/元素，"
                "或者点「重新生成」再出一批。需求卡已自动清空。")
    return {
        "ok": True,
        "images": files,
        "count": got,
        "asked": n,
        "cost": res.get('cost'),
        "mock": res.get('mock'),
        "prompt": prompt,
        "provider": res.get('provider'),
        "model": res.get('model'),
        # 新版引擎的每版产物本身就是可平铺的无缝 tile；前端据此默认进平铺预览。
        "seamless": bool(use_pat),
        # 出图成功 → 请 runtime 把需求卡清空：这一张已经落地了，
        # 留着旧标签只会让用户下一句话被上一张的配色/风格粘住。
        "reset_brief": True,
        "note": note,
    }
