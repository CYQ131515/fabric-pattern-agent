"""接缝体检：纯算法指标，用来判断"缝修好没有"和"花还是不是那朵花"。

四个指标各回答一个问题：
    boundary_error    对边像素差多少（0 最好）
    seam_visibility   平铺后接缝会不会跳出来（1.0 最好）
    ghosting          边缘带有没有被抹糊/重影（1.0 最好）
    fidelity          画面主体有没有被模型改掉
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


def gray(img: np.ndarray) -> np.ndarray:
    if img.ndim == 3:
        return img[..., :3].mean(axis=2)
    return img


@dataclass
class SeamReport:
    boundary_error: float
    seam_visibility: float
    ghosting: float
    covered_ratio: float      # 有纹理的面积占比
    white_ratio: float        # 空白（干净底色）面积占比
    grade: str
    sharp_keep: float = 1.0   # 接缝带细节保留率（1.0 = 没动过；需要参考图才算得出）

    def as_dict(self) -> dict:
        return {
            "boundary_error": round(self.boundary_error, 3),
            "seam_visibility": round(self.seam_visibility, 3),
            "ghosting": round(self.ghosting, 3),
            "sharp_keep": round(self.sharp_keep, 3),
            "covered_ratio": round(self.covered_ratio, 3),
            "white_ratio": round(self.white_ratio, 3),
            "grade": self.grade,
        }


@dataclass
class FidelityReport:
    core_diff: float
    correlation: float
    changed_ratio: float
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "core_diff": round(self.core_diff, 2),
            "correlation": round(self.correlation, 3),
            "changed_ratio": round(self.changed_ratio, 3),
        }


def boundary_error(tile: np.ndarray) -> float:
    """左右边缘列、上下边缘行的平均绝对差。单位 0~255。"""
    g = gray(tile).astype(np.float64)
    dx = np.abs(g[:, -1] - g[:, 0]).mean()
    dy = np.abs(g[-1, :] - g[0, :]).mean()
    return float((dx + dy) / 2.0)


def seam_visibility(tile: np.ndarray, margin: int = 3) -> float:
    """平铺 2x2 后，接缝处的梯度强度 ÷ 画面内部平均梯度强度。

    接近 1.0 = 接缝和画面内部一样正常，肉眼看不出来；明显大于 1 = 有硬边。
    """
    g = gray(tile).astype(np.float64)
    h, w = g.shape
    tiled = np.tile(g, (2, 2))
    gy, gx = np.gradient(tiled)
    mag = np.sqrt(gx * gx + gy * gy)

    v = mag[:, w - margin: w + margin].mean()
    hh = mag[h - margin: h + margin, :].mean()
    interior = mag[margin: h - margin, margin: w - margin].mean()
    if interior < 1e-6:
        return 1.0
    return float(((v + hh) / 2.0) / interior)


def ghosting(tile: np.ndarray, band_frac: float = 0.08, block: int = 16) -> float:
    """边缘带的局部对比度 ÷ 画面内部的局部对比度。

    羽化把两侧的花半透明叠在一起时，接缝在数学上完美接续（边界误差测不出来），
    但叠出来那片对比度会塌陷、结构会重影。所以单独量这一项。

    只比"有花的地方"：白底块两边一样平，会把差异稀释掉。
    """
    g = gray(tile).astype(np.float64)
    h, w = g.shape
    k = max(4, int(min(h, w) * band_frac))

    hh, ww = (h // block) * block, (w // block) * block
    blk = g[:hh, :ww].reshape(hh // block, block, ww // block, block)
    contrast = blk.std(axis=(1, 3))

    kb = max(1, k // block)
    inner = contrast[kb:-kb, kb:-kb].ravel()
    edge = np.concatenate([contrast[:kb].ravel(), contrast[-kb:].ravel(),
                           contrast[:, :kb].ravel(), contrast[:, -kb:].ravel()])
    if inner.size < 8 or edge.size < 8:
        return 1.0

    thr = float(np.median(inner))
    ei, ee = inner[inner > thr], edge[edge > thr]
    if ei.size < 8 or ee.size < 8:
        ei, ee = inner, edge
    base = float(ei.mean())
    return 1.0 if base < 1e-6 else float(ee.mean() / base)


def _sharpness(g: np.ndarray) -> np.ndarray:
    """5 点 Laplacian 的绝对值。羽化模糊会先打掉它，补新内容会抬高它。"""
    lap = (4 * g - np.roll(g, 1, 0) - np.roll(g, -1, 0)
           - np.roll(g, 1, 1) - np.roll(g, -1, 1))
    return np.abs(lap)


def repair_sharpness(tile: np.ndarray, ref: np.ndarray, band_frac: float = 0.08,
                     gap: int = 4) -> float:
    """修缝有没有把接缝带的细节弄丢 / 弄多。1.0 = 完全原样保留。

    为什么不复用 seam_visibility：那个是"边缘带 ÷ 画面内部"，跨了两块内容。
    留白图的边缘天生比内部空，比值天生远离 1（实测能到 0.5），没法定量，
    也无法跨图比较。这里的分母是【同一块区域修补前的样子】，内容逐像素对齐，
    1.0 才是真的"没动过"：
        < 0.85  明显糊了（羽化把两侧半透明叠在一起）
        > 1.30  补出了不该有的细节（成片小花堆在接缝上）

    tile 是交付姿态（接缝在画面边界），ref 是待修补姿态（接缝在画面正中），
    函数内部自己把 tile 滚成同姿态。

    只比"修补前本来就有内容"的像素：平背景两边都是一片平，会把差异稀释掉。
    """
    h, w = tile.shape[:2]
    centered = np.roll(np.roll(tile, h // 2, axis=0), w // 2, axis=1)
    if ref.shape[:2] != (h, w):
        from .io_utils import resize
        ref = resize(ref, (w, h))

    bw, bh = max(4, int(w * band_frac)), max(4, int(h * band_frac))
    yy, xx = np.mgrid[0:h, 0:w]
    zone = ((np.abs(xx - w // 2) < bw) | (np.abs(yy - h // 2) < bh))
    # 挖掉正中 gap 像素：那里是硬缝本身，不是内容
    zone &= ~((np.abs(xx - w // 2) <= gap) | (np.abs(yy - h // 2) <= gap))

    sa = _sharpness(gray(centered))[zone]
    sb = _sharpness(gray(ref))[zone]
    if sb.size < 64:
        return 1.0
    keep = sb > np.median(sb)
    if keep.sum() > 64:
        sa, sb = sa[keep], sb[keep]
    base = float(sb.mean())
    return 1.0 if base < 1e-6 else float(sa.mean() / base)


def coverage(img: np.ndarray, block: int = 16, contrast_thr: float = 8.0,
             white_thr: float = 244.0) -> tuple[float, float]:
    """地比例：返回 (有纹理面积占比, 空白面积占比)。

    分块算局部对比度，对比度高的块算"有花"；
    块均值接近纯白的算"空地"。
    """
    g = gray(img).astype(np.float64)
    h, w = g.shape
    hh, ww = (h // block) * block, (w // block) * block
    blk = g[:hh, :ww].reshape(hh // block, block, ww // block, block)
    blk = blk.transpose(0, 2, 1, 3).reshape(-1, block * block)
    return float((blk.std(axis=1) > contrast_thr).mean()), \
        float((blk.mean(axis=1) > white_thr).mean())


def _grade(be: float, sv: float, gh: float) -> str:
    if be < 2.0 and sv < 1.35:
        return "优" if gh >= 0.80 else "良"
    if be < 6.0 and sv < 1.8:
        return "良"
    if be < 14.0 and sv < 2.5:
        return "一般"
    return "差"


def evaluate(tile: np.ndarray, ref: np.ndarray | None = None,
             sharp_band_frac: float = 0.08) -> SeamReport:
    be = boundary_error(tile)
    sv = seam_visibility(tile)
    gh = ghosting(tile)
    cov, wht = coverage(tile)
    sk = repair_sharpness(tile, ref, sharp_band_frac) if ref is not None else 1.0
    return SeamReport(be, sv, gh, cov, wht, _grade(be, sv, gh), sk)


def fidelity(before: np.ndarray, after: np.ndarray,
             band_frac: float = 0.16) -> FidelityReport:
    """比较模型处理前后，接缝带之外是否原样保留。

    before = 待修补图（接缝在正中），after = 模型输出，两者必须同尺寸同朝向。
    中央十字带内的改动是任务本身要求的，不计入。
    """
    a = before.astype(np.float64)
    b = after.astype(np.float64)
    if a.shape[:2] != b.shape[:2]:
        from .io_utils import resize
        a = resize(before, (b.shape[1], b.shape[0])).astype(np.float64)

    h, w = a.shape[:2]
    bw, bh = max(8, int(w * band_frac)), max(8, int(h * band_frac))
    yy, xx = np.mgrid[0:h, 0:w]
    zone = (np.abs(xx - w // 2) < bw) | (np.abs(yy - h // 2) < bh)

    diff = np.abs(a - b).mean(axis=2)
    core = diff[~zone]
    if core.size == 0:
        return FidelityReport(0.0, 1.0, 0.0)

    ga, gb = gray(a)[~zone], gray(b)[~zone]
    corr = 1.0 if ga.std() < 1e-6 or gb.std() < 1e-6 else \
        float(np.corrcoef(ga, gb)[0, 1])
    return FidelityReport(float(core.mean()), corr, float((core > 12).mean()))


def seam_report_text(r: SeamReport) -> str:
    lines = [
        "接缝体检",
        f"  边界误差      {r.boundary_error:8.3f}   (0 最佳，>14 肉眼可见硬缝)",
        f"  接缝可见度    {r.seam_visibility:8.3f}   (1.0 最佳，>2 有明显硬边)",
        f"  重影          {r.ghosting:8.3f}   (1.0 最佳，<0.8 说明边缘被抹糊)",
        f"  保析度        {r.sharp_keep:8.3f}   (1.0 最佳，<0.85 接缝带糊了)",
        f"  有纹理占比    {r.covered_ratio:8.1%}   空白占比 {r.white_ratio:.1%}",
        f"  综合评级      {r.grade}",
    ]
    return "\n".join(lines)


def fidelity_text(f: FidelityReport) -> str:
    return "\n".join([
        "保真度（最终 tile 对比原图）",
        f"  主体平均差    {f.core_diff:8.2f}   (越小越保真，>8 说明画面被重画)",
        f"  结构相关      {f.correlation:8.3f}   (>0.97 才算没改花型)",
        f"  改动像素占比  {f.changed_ratio:8.1%}",
    ])
