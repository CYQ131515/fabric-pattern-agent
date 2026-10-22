"""图生图改图：只改指定维度，其余全部"保持原样"。

底图来源优先级：
    1. 本会话最近一张图（刚生成的 > 用户上传的）——存在 session.last_image
    2. 调用方显式传入的 image（base64）

改图最容易跑偏的地方是"改了配色连造型一起变了"，
所以 build_edit_prompt 会为每个维度写死「其余保持不变」。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from core.imagegen import (build_edit_prompt, last_image_record,
                           EDIT_LABELS, ImageGenError)

CHANGE_KEYS = ('color', 'element', 'density', 'scale', 'composition', 'style')


def run(args, ctx):
    gen = ctx.get('imagegen')
    if gen is None:
        return {"error": "本服务没启用生图模块"}
    sess = ctx.get('session')

    changes = {k: (args.get(k) or '').strip() for k in CHANGE_KEYS}
    changes = {k: v for k, v in changes.items() if v}
    # prompt_full = 「重新生成」时重放用的完整提示词，有它就不必再拼改图指令
    full = (args.get('prompt_full') or '').strip()
    if not changes and not (args.get('prompt') or '').strip() and not full:
        return {"error": "没说要改什么。至少给 color / element / density / scale / "
                         "composition / style 中的一项，或用 prompt 描述。"}

    base = None
    origin = None
    if sess is not None and getattr(sess, 'last_image', None):
        base = sess.last_image.get('b64')
        origin = sess.last_image.get('origin')
    if args.get('image'):
        base = args['image']
        origin = 'inline'
    if not base:
        # 这句话模型会转述给用户，所以用第二人称，别写"请先让用户…"（那是说给模型听的）。
        return {"error": "现在手上没有可改的底图。改图得先有一张图："
                         "上传一张花型图，或者先按需求出一张，之后我照着它改。"}

    if full:
        prompt = full
    else:
        prev_prompt = (getattr(sess, 'last_image', None) or {}).get('prompt') or ''
        try:
            prompt = build_edit_prompt(prev_prompt, changes)
        except ImageGenError as e:
            return {"error": str(e)}
        addon = (args.get('prompt') or '').strip()
        if addon:
            prompt = addon + '。' + prompt

    if not args.get('confirmed'):
        return {
            "need_confirm": True,
            "action": "edit",
            "cost": gen.cost,
            "changes": changes,
            "prompt": prompt,
            "base_origin": origin,
            "summary": "在现有花型上" + '、'.join(
                '%s为%s' % (EDIT_LABELS.get(k, k).replace('改', ''), v)
                for k, v in changes.items()),
            "hint": "说清改哪一项、花多少点，让用户确认后再执行。",
        }

    try:
        res = gen.generate(prompt, image=base,
                           image_mime=(getattr(sess, 'last_image', None) or {}).get('mime', 'image/png'),
                           tag='edit')
    except ImageGenError as e:
        return {"error": str(e)}

    files = res['files']
    if sess is not None and files:
        # 改完这张就成了新的底图：想接着改下一项，得能拿它当输入。
        sess.last_image = last_image_record(files[0], prompt)
    return {
        "ok": True,
        "images": files,
        "count": len(files),
        "cost": res.get('cost'),
        "mock": res.get('mock'),
        "changes": changes,
        "prompt": prompt,
        "provider": res.get('provider'),
        "model": res.get('model'),
        "note": "已改好并保存，告诉用户改了哪一项。",
    }
