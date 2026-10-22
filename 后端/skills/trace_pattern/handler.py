"""描稿仿稿：用户照片 → 平面花型设计稿（只属于织印师）。

跟 generate_pattern 的区别：
    生图的输入是文字，模型自由发挥；
    描稿的输入是照片，模型要"忠实提取"，还得把拍摄条件（褶皱、光影、
    透视、背景、人体）统统丢掉，只留图案本身。

为什么先跑体检：
    用户拍的照片质量参差不齐。糊了、太暗、图案只占一个小角的图丢给生图模型，
    模型不会拒绝，只会闷头脑补一张"看起来像那么回事"的图 —— 用户拿到才发现
    跟原图案没关系，点数已经扣了。所以出图前先量一下，量完把结论摊给用户看，
    让他自己决定要不要花这个点数。糊到没有结构的直接拦。

照片来源：
    session.last_photo —— 只记用户上传的照片，不会被后续生成的图覆盖。

对话上下文：
    本技能**不看需求卡**。需求卡属于花型师那条链路，描稿只认照片 +
    用户本轮显式说出来的额外要求（args['prompt']）。
"""
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from core.imagegen import last_image_record, ImageGenError
from core import photo_qc

MIME_SUFFIX = {'image/jpeg': '.jpg', 'image/jpg': '.jpg', 'image/png': '.png',
               'image/webp': '.webp', 'image/heic': '.heic'}


def _pick_photo(sess):
    """找一张可描的照片。优先 last_photo，退而用本轮上传的 last_image。"""
    ph = getattr(sess, 'last_photo', None)
    if ph and ph.get('b64'):
        return ph, '上次上传的照片'
    li = getattr(sess, 'last_image', None) or {}
    if li.get('b64') and li.get('origin') == 'upload':
        return li, '这次上传的照片'
    return None, None


def _qc(photo):
    try:
        blob = base64.b64decode(photo['b64'])
        suffix = MIME_SUFFIX.get((photo.get('mime') or '').lower(), '.jpg')
        return photo_qc.qc_from_bytes(blob, suffix=suffix)
    except Exception as e:
        # 体检本身失败不该挡住描稿，降级成"没体检"
        return {'level': 'unknown', 'usable': True, 'issues': [],
                'advice': '', 'error': '%s: %s' % (type(e).__name__, e)}


# 只把这几个字段交给模型和前端。原始指标（clip / blur_ratio / sharpness）不放出去：
# 它们要么不可靠（clip 分不清白底和过曝），要么需要结合标定数据才能解读，
# 模型拿到数字会自作主张地编出一套"亮部层次损失"之类的结论，反而误导用户。
QC_PUBLIC = ('level', 'usable', 'issues', 'advice', 'w', 'h')


def _qc_public(qc):
    return {k: qc[k] for k in QC_PUBLIC if k in qc}


def _qc_line(qc):
    """把体检结论压成一行，给确认卡片用。"""
    if qc.get('level') == 'unknown':
        return ''
    tag = {'good': '照片质量良好', 'fair': '照片质量一般', 'poor': '照片质量较差'}.get(
        qc['level'], '照片质量未知')
    detail = '；'.join(qc.get('issues') or [])
    return tag + ('（%s）' % detail if detail else '')


def run(args, ctx):
    # 描稿有自己的生图客户端（默认沿用花型设计师那套，后台可单独换）。
    # 拿不到独立实例时退回共用的那个 —— 老调用点（测试脚本等）不用改。
    gen = ctx.get('tracegen') or ctx.get('imagegen')
    if gen is None:
        return {"error": "本服务没启用生图模块"}
    sess = ctx.get('session')

    photo, src = _pick_photo(sess)
    if not photo:
        return {"error": "还没有可以描稿的照片。请先上传一张实拍的裙子 / 布料 / 样衣照片"
                         "（手机随手拍也行），我再从上面提取图案。"
                         "如果你手上有现成的花型图，直接上传后说『改配色』这类需求，"
                         "改图那条路更适合。"}

    qc = _qc(photo)
    if not qc.get('usable', True):
        return {"error": "这张照片没法描稿：%s。\n%s\n"
                         "可以换个方式：重新拍一张清晰的照片（光线均匀、正对图案、"
                         "让图案占满画面），或者直接用文字描述你要的风格，"
                         "让花型师重新设计一款。" % (
                             '；'.join(qc.get('issues') or []) or '画面缺少可提取的图案细节',
                             qc.get('advice') or '')}

    # 还原度三档已去掉（2026-10-20）：新算法只有「忠实复刻」一种行为，
    # 用户额外要求仍然走 args['prompt']，作为附注追加给算法。
    note = (args.get('prompt') or '').strip()
    # 一次只出 1 张（2026-10-20 产品确认去掉多候选）

    # 刻意**不读** ctx['brief']。
    # 需求卡是花型师那边的产物：用户上一轮在设计师那里说了「粉紫梦幻」，
    # 织印师描稿时就会照着那个配色走 —— 用户明确说过不要这样（"设计师那边是设计师，
    # 这里是这里"）。描稿的唯一依据是照片；额外要求只能来自用户本轮的原话
    # （模型把它放进 args['prompt']）。
    # prompt_full 是「重新生成」时重放的提示词；新算法里 prompt 的含义是
    # 「用户额外要求」（默认提示词在算法侧），所以重放也只需要这段附注。
    prompt = (args.get('prompt_full') or '').strip() or note

    if not args.get('confirmed'):
        bits = ['从%s提取印花图案' % (src or '照片'), '产出平面花型设计稿']
        if qc.get('level') == 'fair':
            bits.append('照片质量一般，还原度可能打折')
        elif qc.get('level') == 'poor':
            bits.append('照片质量较差，出来的图可能跟原图案有偏差')
        return {
            "need_confirm": True,
            "action": "trace",
            "cost": gen.cost,
            "count": 1,
            "prompt": prompt,
            "qc": _qc_public(qc),
            "summary": '描稿仿稿 · ' + '；'.join(bits),
            "hint": "回复只用三句话，一两句就够，不要展开：① 照片体检的结论（%s），"
                    "差就说差在哪、还能不能救；② 这次出 1 张；③ 产出是一张平面花型设计稿，"
                    "不含四方连续（那是另外一步）。**不要**提「还原度」「风格档位」这类选项——"
                    "现在只有一种忠实复刻的产出。不要列确认清单，不要重复识图标签，"
                    "不要写 Markdown 大标题。只提案，用户点了确认才执行。"
                    % (_qc_line(qc) or '未做体检'),
        }

    gen_kw = {}
    try:
        from core.tracealgo import TraceEngine
        if isinstance(gen, TraceEngine):        # 只有新算法引擎认 route；老 ImageGen 没有
            gen_kw['route'] = (args or {}).get('route')
    except Exception:                            # noqa: BLE001
        pass
    try:
        res = gen.generate(prompt, image=photo['b64'],
                           image_mime=photo.get('mime') or 'image/jpeg', tag='trace',
                           **gen_kw)
    except ImageGenError as e:
        return {"error": str(e)}

    files = res['files']
    if sess is not None and files:
        # 描出来的稿子也是底图：用户可以接着改配色、调疏密。
        sess.last_image = last_image_record(files[0], prompt)
    return {
        "ok": True,
        "images": files,
        "count": len(files),
        "cost": res.get('cost'),
        "mock": res.get('mock'),
        "prompt": prompt,
        "provider": res.get('provider'),
        "model": res.get('model'),
        "qc": _qc_public(qc),
        "note": "描稿完成，已保存。热情地告诉用户：这是从照片里提出来的平面花型稿，"
                "褶皱、光影、背景都去掉了。**接下来只有两步**：做成四方连续，"
                "或者生成分层 PSD —— 界面上已经摆好这两个按钮，点一个就行。"
                "两三句就够，别写成报告；**不要**再提改配色、改疏密度、重新生成"
                "（那些不是描稿之后该做的事）。",
    }
