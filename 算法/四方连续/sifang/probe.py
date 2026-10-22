"""输入体检：花钱之前，先把这张图"能量成什么样、该走哪条路"量出来。

为什么要有这一层
    四方连续有两条路线，代价差着两个数量级：
        纯算法    零成本、完全确定性，但接缝带上会留羽化重影；
        模型补缝  要调云端编辑模型，贵，但能把接缝处真正补出新内容。
    选错路线不是"效果差一点"，而是白花钱（图上没纹理可补）或白做（该补的没补）。

    "这张图能不能靠补缝修好"是**可测量的**，不用问模型。判断的依据是补缝法的
    隐含前提：图内部是好的，只有被切开的边缘是坏的，而且窄带里的改动能被
    周围纹理**掩盖**。所以只要量两件事：

        有纹理面积占比   —— 有多少材料可以拿来遮掩补进去的内容
        空白（干净底色）占比 —— 补缝区域有多少会落在没有纹理可藏的地方

    两张实测样本正好构成一对反例：
        深底密集花卉  有纹理 96.2% / 空白  0%   → 满地平接，补缝最擅长
        花框花卉      有纹理 51.6% / 空白 45.2% → 框架式定位构图，接缝落在白底上
    后者硬走补缝，结果就是那道"没纹理可藏的浅色带"。

这一层刻意只做判断、不做决定
    输出的 route_hint 是**建议**，不是命令。最终走哪条路由 pipeline 的质量闸门定：
    两条路都跑出来，谁的评级高用谁。体检的价值是让用户在掏点数之前就看到
    "这张图大概率适不适合"，而不是出完图才发现。
"""
from __future__ import annotations

from . import metrics, seam
from .io_utils import resize, working_size

# ---- 判定阈值 ----------------------------------------------------------
# 地比例分档（按有纹理面积占比，对齐行业口径：清地 <40% / 混地 40~60% / 满地 >60%）
GROUND_CLEAR = 0.40
GROUND_FULL = 0.60

BLANK_HEAVY = 0.40      # 空白块占比超过此值 → 接缝大半落在空地上
TEXTURE_THIN = 0.25     # 有纹理占比低于此值 → 找不够元素来接缝
TEXTURE_EMPTY = 0.03    # 低于此值基本是纯色图，没有可平铺的内容


def probe(image, band_frac: float = 0.12, align_frac: float = 0.03,
          sharp_band_frac: float = 0.08) -> dict:
    """给一张花型图做体检。纯算法、零成本、不调任何模型。"""
    w0, h0 = image.shape[1], image.shape[0]
    ww, hh = working_size(w0, h0)
    work = resize(image, (ww, hh))

    textured, blank = metrics.coverage(work)

    # 纯算法这条路**实测**跑一遍再报成绩，而不是拿特征去估算。
    # 它零成本、秒级，而且拿到的是这张图上真实发生过的数字：
    # 用户看到"纯算法在你这张图上能到什么水平"，比看一堆抽象指标有用得多。
    centered = seam.half_offset(work)
    baseline = seam.baseline(work, band_frac, align_frac)
    base_rep = metrics.evaluate(baseline, centered, sharp_band_frac)

    ground = ("清地" if textured < GROUND_CLEAR
              else ("混地" if textured < GROUND_FULL else "满地"))

    warnings: list[str] = []
    if textured < TEXTURE_EMPTY:
        route_hint = "reject"
        warnings.append("这张图基本是纯色或空白，没有可以平铺的图案内容，"
                        "做成四方连续没有意义。")
    elif blank >= BLANK_HEAVY or textured < TEXTURE_THIN:
        route_hint = "pure-algorithm"
        if blank >= BLANK_HEAVY:
            warnings.append(
                "留白占了 %.0f%% —— 接缝有一大半落在空地上，那里没有纹理可藏，"
                "补缝补不出内容，反而容易在空地上留下一道浅色带。"
                % (blank * 100))
        else:
            warnings.append(
                "有纹理的地方只占 %.0f%%，找不出足够的元素来接缝。"
                % (textured * 100))
        warnings.append("建议先用纯算法直出（边界已经逐像素对齐，不漏缝），"
                        "不满意再上模型补缝。")
    else:
        route_hint = "model"

    # 纯算法已经够好时，别让用户白花一次模型调用
    if route_hint == "model" and base_rep.grade == "优" and base_rep.sharp_keep >= 0.95:
        route_hint = "pure-algorithm"
        warnings.append("纯算法在这张图上已经到「优」、接缝带细节基本没损耗，"
                        "不调模型也够用。")

    return {
        "input": {"width": w0, "height": h0,
                  "aspect": round(w0 / float(h0 or 1), 3)},
        "working_size": {"width": ww, "height": hh},
        "textured_ratio": round(textured, 3),
        "blank_ratio": round(blank, 3),
        "ground": ground,
        "baseline": base_rep.as_dict(),
        "route_hint": route_hint,
        "warnings": warnings,
        "summary": _summary(width=w0, height=h0, textured=textured, blank=blank,
                            ground=ground, rep=base_rep, route=route_hint),
    }


def _summary(width: int, height: int, textured: float, blank: float, ground: str,
             rep, route: str) -> str:
    """压成一句话，给确认卡片和机器人转述用。"""
    bits = [f"{width}×{height}", f"{ground}（有纹理 {textured:.0%}"]
    bits[-1] += (f" · 留白 {blank:.0%}）" if blank > 0.02 else "）")
    tail = {"reject": "没有可平铺的图案内容",
            "pure-algorithm": "留白偏多，建议纯算法直出",
            "model": "适合走模型补缝"}.get(route, route)
    bits.append(tail)
    if rep.grade in ("优", "良"):
        bits.append(f"纯算法基线已到「{rep.grade}」")
    return " · ".join(bits)
