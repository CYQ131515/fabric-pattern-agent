"""四方连续：把一张花型图做成可无缝平铺的四方连续单元。

它做的是什么
    一张花型不能平铺，是因为"最右一列"和"最左一列"对不上、"最下一行"和
    "最上一行"对不上。做法是先把四个问题边从画面边界挪到画面正中
    （问题从"四条边"集中成"一个十字"），此时画面外围全是原图内容、天然连续；
    再只修中央那个十字；修完移回来 —— 平铺就无缝了。

两条路线，代价差两个数量级
    纯算法    零成本、完全确定性，边界逐像素对齐、一定不漏缝；代价是羽化带
              会把接缝两侧半透明叠在一起，局部有重影。
    模型补缝  调云端编辑模型，在中央十字带里真正补出新内容。模型不可复现，
              所以跑几个候选挑最好的；改完 mask 带外强制用原图像素还原 ——
              实测模型会顺手把整张图重画一遍，保真只能靠架构强制，不能靠它自觉。

    选哪条不是"效果差一点"的问题：留白多的图（接缝大半落在空地上）补缝补不出
    东西，还容易在空地上留下一道浅色带 —— 那是白花钱。所以先跑一次**输入体检**
    （纯算法、零成本、秒级），把"这张图适合怎么走"量出来给用户看，再让他决定。

底图来源
    session.last_image —— **本机器人这一槽**里的最近一张图（用户在这个会话里
    上传的 / 从库选给它的 / 刚描稿出来的 / 用户点「交给织印师」交过来的）。
    退而用 session.last_photo（只记用户上传的照片，不会被生成的图挤掉）。
    记忆是按角色分槽的：花型师刚生成的图**不在这儿**，要做就先让用户
    显式交过来（库选 / 上传），不靠会话自动串。

对话上下文
    和描稿一样，**不看需求卡**。需求卡是花型师那条链路的产物，跟这边的
    接缝处理没关系（用户明确要求过："设计师那边是设计师，这里是这里"）。
"""
import base64
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from core.imagegen import last_image_record, sniff
from core.seamless import SeamlessError



def _pick_image(sess):
    """找一张可处理的底图。优先本会话最近一张，退而用用户上传的照片。

    返回记录本身（里面带 b64）。标不标 origin 不影响处理 ——
    四方连续对"这张图哪来的"不敏感，上传的、生成的一样做。
    """
    li = getattr(sess, 'last_image', None) or {}
    if li.get('b64'):
        return li
    ph = getattr(sess, 'last_photo', None) or {}
    if ph.get('b64'):
        return ph
    return None




def run(args, ctx):
    engine = ctx.get('seamless')
    gen = ctx.get('imagegen')
    sess = ctx.get('session')

    if gen is None:
        return {"error": "本服务没启用生图模块"}
    if engine is None:
        return {"error": "本服务没装四方连续引擎"}
    if not engine.available:
        # 直接说清怎么修，别甩一句"环境异常"让用户去猜
        return {"error": engine.missing_note}

    photo = _pick_image(sess)
    if not photo:
        return {"error": "现在手上没有可以做的图。四方连续要有一张底图："
                         "上传一张花型图，或者在花型库里选一张给我，"
                         "然后我照着它做。"}

    try:
        blob = base64.b64decode(photo['b64'])
    except Exception as e:
        return {"error": "这张图的数据读不出来（%s），换一张重新上传试试。" % type(e).__name__}
    ext, _mime = sniff(blob)

    # ---------- 提案：只体检，不出图、不扣点 ----------
    if not args.get('confirmed'):
        try:
            info = engine.probe(blob, ext)
        except SeamlessError as e:
            return {"error": str(e)}

        if info.get('route_hint') == 'reject':
            return {"error": "这张图做不了四方连续：%s\n"
                             "换一张有明确图案的花型图，或者让花型师按需求出一张。"
                             % '；'.join(info.get('warnings') or [])}

        # 提案只说人话：这张图能不能做、多少钱、多久；技术细节一律不展开。
        return {
            "need_confirm": True,
            "action": "seamless",
            "cost": engine.cost,
            "count": 1,
            "probe": info,
            "summary": info.get('summary') or '可以做：图案适合做四方连续。',
            "hint": (
                "回复只用一两句话，不要写成报告："
                "① 直接说这张图能不能做（最多补一句原因）；"
                "② 这次做 1 张、扣 %d 点，大约几十秒到两分钟，确认后开工。"
                "**不要**提图案特征、数字、路线或算法细节，也不要让用户选路线。"
                "只提案，用户点了确认才执行。"
                % engine.cost
            ),
        }

    # ---------- 执行 ----------
    # 路线由算法内部自动选（体检 → 逐条候选 → 接缝审查），这里不再插手。
    try:
        result, workdir = engine.convert(blob, ext)
    except SeamlessError as e:
        return {"error": str(e)}

    try:
        files = _publish(gen, result)
    except Exception as e:                           # noqa: BLE001
        return {"error": "四方连续算完了，但产物没存下来（%s: %s）"
                         % (type(e).__name__, e)}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    if not files:
        return {"error": "四方连续没产出可用的图，可能这张图本身没有可平铺的内容。"}

    if sess is not None:
        # 交付单元接着可以当底图用：再改配色、再做一次连续都行。
        sess.last_image = last_image_record(files[0], '四方连续')

    rep = result.get('seam_report') or {}
    grade_b = bool(rep.get('degraded'))
    reviewed = rep.get('reviewed', True)
    fid = result.get('fidelity') or {}
    probe = result.get('probe') or {}

    if not reviewed:
        note = ("四方连续出图了。本次**接缝审查是关闭的**（按用户要求），"
                "所以**不要**说「A 级」「通过审查」「保证无缝」这类话；"
                "告诉用户：① 图已生成、花的造型和配色按原图保留；"
                "② 点开大图可切「平铺预览」看效果、想留档就下载；"
                "③ 如果平铺看到接缝，可以让他说一声，再单独跑一次修复。"
                "三四句话，别写成报告。")
        note += "三四句话，别写成报告。"
        return {
            "ok": True,
            "images": files,
            "count": len(files),
            "cost": engine.cost,
            "engine": result.get('route'),
            "engine_label": result.get('route_label'),
            "route": result.get('route'),
            "seam_report": rep,
            "quality": None,
            "degraded": False,
            "warnings": result.get('warnings') or [],
            "fidelity": fid,
            "probe": probe,
            "input": result.get('input'),
            "models": result.get('models'),
            "note": note,
        }

    note = ("四方连续做好了。告诉用户：① 这是一张可以无缝平铺的单元图，"
            "花的造型和配色都按原图保留，接缝评级是「%s」；"
            "② **点开大图**，下方可以切「平铺预览」看铺开后的效果，"
            "想留档就在放大界面里下载。"
            "别说「已存进花型库」也别说「没存」—— 库是内部资产，"
            "用户不需要知道有这个东西，别顺嘴泄露。"
            % (rep.get('grade') or '—'))
    if grade_b:
        note += ("另外**必须如实交代**：这次接缝审查没拿到 A 级（原因：%s），"
                 "交付的是 **B 级** —— 平铺不会有对不上的硬线，"
                 "但修复带在 1:1 放大看可能有轻微可见的软带/颗粒差；"
                 "接受不了可以说「重新生成」再试一次。"
                 "**不要**把 B 级说成完美，也不要把原因展开成技术报告。"
                 % (rep.get('reason') or '修复带轻微可见'))
    else:
        note += "交付就这一张图，**不要**引导用户接着改配色、改疏密或再做一次。"
    note += "三四句话，别写成报告。"

    return {
        "ok": True,
        "images": files,
        "count": len(files),
        "cost": engine.cost,
        "engine": result.get('route'),
        "engine_label": result.get('route_label'),
        "route": result.get('route'),
        "seam_report": rep,
        "quality": rep.get('grade'),
        "degraded": grade_b,
        "warnings": result.get('warnings') or [],
        "fidelity": fid,
        "probe": probe,
        "input": result.get('input'),
        "models": result.get('models'),
        "note": note,
    }


def _fallback_reason(log):
    """从算法日志里挑一句能说明"为什么没走成模型"的话。给排查和转述用。"""
    for line in reversed(list(log or [])):
        if '失败' in line or '退回' in line:
            return line.strip()
    return ''


def _publish(gen, result):
    """只把四方连续的**单元图**存进出图目录，换回可访问的地址。

    3×3 平铺预览图和接缝对比图**不再分发**：交互上只显示这一张单元图。
    要看平铺效果，直接在放大视图里切「平铺预览」—— 单元图本身就是无缝的，
    铺开比一张静态预览图更能说明问题。顺带少占两份磁盘。
    """
    path = (result.get("outputs") or {}).get("tile")
    if not path or not os.path.exists(path):
        return []
    with open(path, "rb") as f:
        blob = f.read()
    return list(gen.save([blob], tag="seamless-tile").get('files') or [])
