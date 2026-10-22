"""平铺判定 —— 判断一张图到底**能不能四方连续地铺开**。

这是整条产线的主过滤器，不是可选优化
    图片搜索按关键词捞回来的东西五花八门：穿着花裙子的模特、摆在桌上的
    色卡、印在抱枕上的效果图、原图案的一小块特写。用户的要求里"不管水印
    和清晰度"可以放宽，但**"必须是真正的平铺图"是硬条件** —— 一张铺开就
    露缝的图，进了库只是让客户白挑一轮。

判定只有一条原理，但两个坑很深

  **原理**：真正的平铺单元，把它的右边界和左边界放在一起应该是**连续的**
  —— 它在原图案里本来就挨着。于是算两个量：
      接缝差 = mean|最右列 - 最左列|
      常规差 = mean|第 i 列 - 第 i+1 列|（图内部相邻列，全图统计）
  然后看**比值**。这是尺度无关的：深色花型和浅色花型的绝对差异差着好几倍，
  但真平铺的比值都落在 1.0 附近（见下）。

  ⚠️ 坑一：**真平铺的比值是 ≈1，不是 ≈0。** 一开始按"接缝差应该接近 0"
  去想，结果真图被判成假。后来用合成图（正弦干涉图，数学上严格周期）标定
  才看清：接缝处本来就是一次正常的相邻列跳变，所以比值天然在 1 附近。
  判据只能定在"比值明显大于 1 就算接不上"。

  ⚠️ 坑二：**有些图根本不能靠比值判**。相邻像素几乎不相关的图（噪点、
  高感光照片），接缝差和常规差都是同一个统计量，比值恒等于 1 —— 无论
  它铺得开铺不开。所以必须再加一道**绝对上限**：接缝差自身不能太大。
  纹理图的比值过不了这道，才拦得下来。

两条**被合成标定推翻**的早期设计（记下来免得再走回去）

  · ~~最外圈标准差守卫~~：本意是拦"花型贴在白色卡纸上的展示图"，结果
    把**居中单花 + 素底**这种最常见的合法平铺一起拦了 —— 它的最外圈就是
    一片素色，标准差接近 0，和卡纸边一模一样。纯图像统计分不开这两者，
    要分得靠语义（见下面的视觉复核）。已删除。
  · ~~纹理门槛 1.2~~：本意是拦纯色/渐变，结果把**稀疏花型**（大半张是
    素底，只有一朵花）一起拦了 —— 全图相邻列差被素底摊薄到 1.0 以下。
    现在只保留"纯色"这一个极端值（低于 0.12 才算没内容）；渐变由比值
    自己拦（渐变的接缝差是一整段色阶，比值几百）。

最后两条工程决定
    · **缩到 256 长边再算**。接缝判定关心结构不关心像素；缩图让大小图同
      成本，也顺手抹掉 JPEG 的 8×8 块噪声（块噪声正好卡在边界列上，
      不缩图会抬高真平铺的接缝差）。
    · **不用 numpy**。判定靠 Pillow 的 ImageChops.difference 就能做完 ——
      两个整图差分一次算完，比逐列循环快几个数量级，也少一个依赖。
"""
from __future__ import annotations

import io

from PIL import Image, ImageChops, ImageStat

# 判定用的工作尺寸（长边）。256 足够看清结构，又便宜。
SAMPLE = 256

# 接缝比值上限。合成标定：真平铺（不同周期数 / JPEG 60 / 高斯模糊）
# 实测 1.05~1.42，非平铺（人像照、随机噪声、素材照）实测 1.6~26。
# 1.55 落在中间那道空隙里，两边都留了余量。
RATIO_MAX = 1.55

# 接缝差自身的绝对上限。专门拦"比值为 1 但其实是噪声/照片"那一类
# （见坑二）。34 是灰度级，实测真平铺（JPEG 60）也就 10~25。
EDGE_ABS_MAX = 34.0

# 纯色判定：全图相邻列平均差低于它 = 没有内容。
SOLID_MAX = 0.12

# 小于这个边长的图不判（缩略图铺开也看不清，统计样本也太少）。
MIN_SIDE = 200

# "图形感"采样：缩到这么小，数一下还剩多少种颜色。
# 平铺花型是**平面图形**（限色、成片），照片是连续色调（几乎每个像素都不同）。
# 这个量只用于排序和事后筛查，**不做硬门槛**（假阳性代价太高）。
GRAPHIC_SAMPLE = 64


def _mean_abs(a, b):
    """两张同尺寸图逐像素差的平均值（跨通道取平均）。"""
    st = ImageStat.Stat(ImageChops.difference(a, b))
    return sum(st.mean) / max(1, len(st.mean))


def _load(src):
    """src 可以是路径 / bytes / Image。统一回一个 RGB 的 PIL 图。"""
    if isinstance(src, Image.Image):
        im = src
    elif isinstance(src, (bytes, bytearray)):
        im = Image.open(io.BytesIO(src))
    else:
        im = Image.open(src)
    im.load()
    has_alpha = im.mode in ("RGBA", "LA", "PA") or (
        im.mode == "P" and "transparency" in (im.info or {}))
    if has_alpha:
        # 透明底合成到白底再判：透明区域在 RGB 里会变成黑，
        # 而"黑对黑"的接缝差恒为 0 —— 那是另一种假阳性。
        rgba = im.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        im = bg
    else:
        im = im.convert("RGB")
    return im, has_alpha


def _sample(im, sample=SAMPLE):
    """缩到长边 sample，保持比例。已经更小的不放大（放大只引入插值伪影）。"""
    w, h = im.size
    if max(w, h) <= sample:
        return im
    k = sample / float(max(w, h))
    return im.resize((max(8, int(round(w * k))), max(8, int(round(h * k)))),
                     Image.LANCZOS)


def _axis(im, axis):
    """算一个方向上的 (接缝差, 常规差)。

    axis='h' 看左右接缝（图能不能横向重复），'v' 看上下接缝。
    两个整图差分就出结果，不逐列循环。
    """
    w, h = im.size
    if axis == "h":
        if w < 4:
            return 0.0, 0.0
        edge = _mean_abs(im.crop((w - 1, 0, w, h)), im.crop((0, 0, 1, h)))
        typ = _mean_abs(im.crop((0, 0, w - 1, h)), im.crop((1, 0, w, h)))
        return edge, typ
    if h < 4:
        return 0.0, 0.0
    edge = _mean_abs(im.crop((0, h - 1, w, h)), im.crop((0, 0, w, 1)))
    typ = _mean_abs(im.crop((0, 0, w, h - 1)), im.crop((0, 1, w, h)))
    return edge, typ


def _graphicness(im):
    """0~1 的"平面图形感"：色数 / 采样像素数。

    平铺花型是平面图形 —— 限色、色块成片，缩到 64×64 后剩下的颜色很少
    （实测 0.05~0.45）；照片是连续色调，几乎每个采样点都不同（0.9 以上）。
    只用来排序和事后筛查，**不做硬门槛**。
    """
    try:
        small = im.resize((GRAPHIC_SAMPLE, GRAPHIC_SAMPLE), Image.LANCZOS)
        got = small.getcolors(maxcolors=GRAPHIC_SAMPLE * GRAPHIC_SAMPLE)
        if got is None:                     # 超过上限 = 颜色极多
            return 1.0
        return len(got) / float(GRAPHIC_SAMPLE * GRAPHIC_SAMPLE)
    except Exception:                                        # noqa: BLE001
        return 0.0


def _corner_diff(im):
    """四角亮度 与 全图平均亮度 的差（灰度级）。

    **只做诊断，不做门槛** —— 这一条是被数据打回去的：
    合成标定里，"花型贴在白卡纸上"（角部 255 白，图整体偏灰）差 +11~+16，
    而"素底 + 居中的一朵大花"（角部是素底，图整体被花压暗）差 +47~+84。
    合法花型的偏差比假图还大，方向还一样 —— 这个量根本分不开两者。

    留着它是因为它对**人工复核**有用：偏差特别大的图（不管是贴卡纸还是
    四周压暗的打样照）值得先看一眼。真正的判据是下面的接缝比值。
    """
    try:
        sm = im.convert("L").resize((8, 8), Image.BOX)
        px = sm.load()
        vals = [px[x, y] for y in range(8) for x in range(8)]
        corners = [px[0, 0], px[7, 0], px[0, 7], px[7, 7]]
        return sum(corners) / 4.0 - sum(vals) / 64.0
    except Exception:                                        # noqa: BLE001
        return 0.0


def analyze(src, *, sample=SAMPLE) -> dict:
    """判定一张图。永远返回一个 dict（失败也算，带 ok=False 和一句人话）。

    关键字段
        verdict   seamless / flat / not_tile / too_small / broken
        seamless  布尔，等同 verdict == 'seamless'（**硬闸门就用它**）
        seam_h/v  两个方向的接缝比值（越小越平；≤RATIO_MAX 算过关）
        edge_h/v  两个方向的接缝绝对差（灰度级）
        texture   图内部相邻列的平均差（判断有没有内容）
        graphic   平面图形感 0~1（照片接近 1，花型远低于 1；供排序）
        quality   0~1 的平铺质量分，供排序
        flags     判定过程里触发的问题（给排错看，不进用户话术）
    """
    out = {"ok": False, "seamless": False, "verdict": "broken", "quality": 0.0,
           "seam_h": None, "seam_v": None, "edge_h": None, "edge_v": None,
           "texture": None, "graphic": None, "corner_diff": None, "size": [0, 0],
           "has_alpha": False, "flags": []}
    try:
        im, has_alpha = _load(src)
    except Exception as e:                                   # noqa: BLE001
        out["note"] = "这张图读不出来（%s）" % type(e).__name__
        return out
    out["has_alpha"] = has_alpha

    w, h = im.size
    out["size"] = [int(w), int(h)]
    out["ok"] = True
    if min(w, h) < MIN_SIDE:
        out["verdict"] = "too_small"
        out["note"] = "图太小（%d×%d），铺开看不清" % (w, h)
        out["flags"].append("too_small")
        return out

    im = _sample(im, sample)
    out["graphic"] = round(_graphicness(im), 3)
    out["corner_diff"] = round(_corner_diff(im), 3)

    eh, th = _axis(im, "h")
    ev, tv = _axis(im, "v")
    texture = (th + tv) / 2.0
    out["texture"] = round(texture, 3)
    out.update({"edge_h": round(eh, 3), "edge_v": round(ev, 3)})

    # 纯色：真的没有内容（渐变不走这条，它有自己的特征）
    if texture < SOLID_MAX:
        out["verdict"] = "flat"
        out["note"] = "整张图是纯色，没有花型内容"
        out["flags"].append("flat")
        return out

    # 接缝比值。比值为 1 时看不出区别，所以补一道绝对值上限（坑二）。
    sh = eh / max(th, SOLID_MAX)
    sv = ev / max(tv, SOLID_MAX)
    out.update({"seam_h": round(sh, 3), "seam_v": round(sv, 3)})
    ok_h = (sh <= RATIO_MAX) and (eh <= EDGE_ABS_MAX)
    ok_v = (sv <= RATIO_MAX) and (ev <= EDGE_ABS_MAX)
    out["flags"] += [n for n, good in (("seam_h", ok_h), ("seam_v", ok_v)) if not good]

    if ok_h and ok_v:
        worst = max(sh, sv)
        out["seamless"] = True
        out["verdict"] = "seamless"
        # 质量分：比值 ≤1 给 0.5~1.0，越往里越高；到上限附近收敛到 0.3。
        # 不归零 —— 免得排序时把"刚好过关"的一律压到底，它们其实是能用的。
        if worst <= 1.0:
            q = 0.5 + 0.5 * (1.0 - worst)
        else:
            q = max(0.30, 0.50 - (worst - 1.0) * 0.30)
        out["quality"] = round(q, 3)
        out["note"] = "接缝对得上，能四方连续铺开"
    else:
        out["seamless"] = False
        out["verdict"] = "not_tile"
        out["quality"] = 0.0
        out["note"] = "左右或上下接不上，铺开会露缝"
    return out


def is_tile(src):
    """只要一个布尔。给"这张图能不能入库"这类单点判断用。"""
    return bool(analyze(src).get("seamless"))
