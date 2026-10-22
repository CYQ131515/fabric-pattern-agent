"""
pattern_studio.verify —— 第 7 层：自检

三个必须过的硬指标（不靠肉眼）：
  1. **不重叠**：把所有 placement 重新在独立网格上光栅化，
     sum(单元素面积) 与 union(并集面积) 的差就是重叠面积 → 必须 < 0.1%
  2. **密度达标**：union 覆盖率要与目标 density 对得上（允许 ±30% 相对误差）
  3. **可平铺**：tile 的左右/上下边界"接缝梯度"不应显著高于内部梯度
     （背景不是无缝图时，这一项会立刻飙高 —— 用来抓 AI 背景不可平铺的问题）

另外统计元素使用次数、最小间距（环面距离），便于调参。
"""
from __future__ import annotations

import math

import numpy as np
from PIL import Image

from .layout import COLLISION_RES, _blit_wrap, render_mask


def _erode(mask: np.ndarray, r: int = 1) -> np.ndarray:
    """二值腐蚀（环面），用于把"边界像素级差异"和"真实重叠"分开。"""
    out = mask
    for _ in range(r):
        acc = out.copy()
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dx or dy:
                    acc &= np.roll(np.roll(out, dy, axis=0), dx, axis=1)
        out = acc
    return out


def render_mask_hi(el, size_px, angle: float, flip: bool, grid: int, tile: tuple[int, int]) -> np.ndarray:
    """用元素**原始 alpha**（而不是 96px 碰撞代理）在高分辨率网格上重画掩膜。

    这是独立校验的关键：碰撞代理是低分辨率的，放大后边界会偏粗/偏细几个百分点，
    如果用代理去"验"代理，会得到一堆假重叠。这里直接从源图 alpha 出发。
    """
    from PIL import Image

    w_px, h_px = size_px
    tw, th = tile
    gw = max(1, int(round(w_px / tw * grid)))
    gh = max(1, int(round(h_px / th * grid)))
    a = el.image.split()[-1].resize((gw, gh), Image.BILINEAR)
    if flip:
        a = a.transpose(Image.FLIP_LEFT_RIGHT)
    if abs(angle) > 0.01:
        a = a.rotate(angle, expand=True, resample=Image.BILINEAR)
    return np.asarray(a) > 128


def overlap_stats(elements: dict, result: dict, tile: tuple[int, int], grid: int = 768,
                  erode: int = 0, hi: bool = False, k: int = 5) -> dict:
    """独立重算重叠面积（不复用求解器的占据图，避免"自己验自己"）。

    erode>0 时先把每个元素腐蚀掉若干像素：只剩下"内部实心区"的重叠，
    从而排除栅格化边界带来的 1px 假重叠。
    """
    W, H = tile
    union = np.zeros((grid, grid), bool)
    total = 0
    per_masks = []
    for p in result["placements"]:
        el = elements.get(p.element_id)
        if el is None:
            continue
        patch = (render_mask_hi(el, p.size_px, p.angle, p.flip, grid, (W, H))
                 if hi else render_mask(el.mask, p.size_px, p.angle, p.flip, grid, (W, H)))
        ph, pw = patch.shape
        x0 = int(round(p.cx / W * grid - pw / 2)); y0 = int(round(p.cy / H * grid - ph / 2))
        single = np.zeros((grid, grid), bool)
        _blit_wrap(single, patch, x0, y0, "or")
        if erode:
            single = _erode(single, erode)
        per_masks.append(single)
        total += int(single.sum())
        union |= single
    union_n = int(union.sum())
    overlap = max(0, total - union_n)
    return {
        "grid": grid,
        "evenness": evenness_stats_union(union, k),
        "union_px": union_n,
        "sum_px": total,
        "overlap_px": overlap,
        "overlap_frac": overlap / float(grid * grid),
        "coverage": union_n / float(grid * grid),
        "n": len(per_masks),
    }


def center_dist(elements: dict, result: dict, tile: tuple[int, int]) -> dict:
    """中心点环面距离统计（诊断用；形状不规则，中心距小不代表真的重叠）。"""
    W, H = tile
    ps = result["placements"]
    ds = []
    for i in range(len(ps)):
        for j in range(i + 1, len(ps)):
            a, b = ps[i], ps[j]
            dx = abs(a.cx - b.cx); dx = min(dx, W - dx)
            dy = abs(a.cy - b.cy); dy = min(dy, H - dy)
            ds.append(math.hypot(dx, dy))
    if not ds:
        return {"min_center_dist_px": None, "median_center_dist_px": None}
    ds.sort()
    return {"min_center_dist_px": round(ds[0], 1), "median_center_dist_px": round(ds[len(ds) // 2], 1)}


def seam_score(tile_img: Image.Image) -> dict:
    """接缝评分：左右边界列差 vs 内部相邻列差的中位数。

    ratio 接近 1 = 接缝与内部一样连续（好）；明显 > 1 = 接缝处有硬边。
    """
    a = np.asarray(tile_img.convert("RGB"), dtype=np.float32)
    if a.shape[0] < 4 or a.shape[1] < 4:
        return {"ratio_h": None, "ratio_v": None}
    dh = np.abs(np.diff(a, axis=1)).mean(axis=(0, 2))          # 每列的列间差
    seam_h = float(np.abs(a[:, 0, :] - a[:, -1, :]).mean())
    inner_h = float(np.median(dh))
    dv = np.abs(np.diff(a, axis=0)).mean(axis=(1, 2))
    seam_v = float(np.abs(a[0, :, :] - a[-1, :, :]).mean())
    inner_v = float(np.median(dv))
    return {
        "seam_h": round(seam_h, 2), "inner_h": round(inner_h, 2),
        "ratio_h": round(seam_h / max(1e-6, inner_h), 2),
        "seam_v": round(seam_v, 2), "inner_v": round(inner_v, 2),
        "ratio_v": round(seam_v / max(1e-6, inner_v), 2),
    }


def tileability(tile_img: Image.Image, tile: tuple[int, int], bg, elements: dict,
                result: dict, nx: int = 2, ny: int = 2) -> dict:
    """硬判定：tile 平铺 2×2  ==  无限画布直接渲染（逐像素）"""
    from . import compose

    tiled = compose.preview_tile(tile_img, nx)
    ref = compose.render_infinite(tile, bg, elements, result, nx=nx, ny=ny)
    a = np.asarray(tiled.convert("RGB"), dtype=np.int16)
    b = np.asarray(ref.convert("RGB"), dtype=np.int16)
    if a.shape != b.shape:
        return {"ok": False, "reason": f"shape {a.shape} vs {b.shape}"}
    d = np.abs(a - b).max(axis=2)
    return {
        "ok": bool(int(d.max()) <= 2),
        "max_diff": int(d.max()),
        "mean_diff": round(float(d.mean()), 5),
        "pixels_gt2_frac": round(float((d > 2).mean()), 6),
    }


def evenness_stats_union(union: np.ndarray, k: int = 5) -> dict:
    """块覆盖率变异系数（越小越均匀）；空块数直接反映"有没有秃块"。"""
    g = union.shape[0]
    bs = max(1, g // k)
    bc = np.array([[union[by * bs:(by + 1) * bs, bx * bs:(bx + 1) * bs].mean()
                    for bx in range(k)] for by in range(k)])
    mean = float(bc.mean())
    return {
        "cv": round(float(bc.std()) / mean, 4) if mean > 1e-9 else 0.0,
        "empty_blocks": int((bc < mean * 0.15).sum()),
        "min_cov": round(float(bc.min()), 4),
        "max_cov": round(float(bc.max()), 4),
    }


def spacing_stats(result: dict, tile: tuple[int, int]) -> dict:
    """独立复算最近邻中心距的变异系数。"""
    W, H = tile
    ps = result["placements"]
    if len(ps) < 3:
        return {"nn_cv": 0.0, "nn_min": 0.0, "nn_mean": 0.0}
    ds = []
    for i, a in enumerate(ps):
        best = float("inf")
        for j, b in enumerate(ps):
            if i == j:
                continue
            dx = abs(a.cx - b.cx); dx = min(dx, W - dx)
            dy = abs(a.cy - b.cy); dy = min(dy, H - dy)
            d = math.hypot(dx, dy)
            best = min(best, d)
        ds.append(best)
    m = float(np.mean(ds))
    return {
        "nn_cv": round(float(np.std(ds)) / m, 4) if m > 1e-9 else 0.0,
        "nn_min": round(float(np.min(ds)), 1),
        "nn_mean": round(m, 1),
    }


def check_draft(elements: dict, result: dict, tile: tuple[int, int],
                tile_img: Image.Image, density_target: float,
                bg=None, allow_overlap: bool = False) -> dict:
    ov = overlap_stats(elements, result, tile, grid=COLLISION_RES)                  # 求解器同栅格

    k_ev = 5 if tile[0] >= 800 else 4
    ov_hard = overlap_stats(elements, result, tile, grid=1024, erode=1, hi=True, k=k_ev)  # 全分辨率 alpha
    gap = center_dist(elements, result, tile)
    seam = seam_score(tile_img)
    density_ok = True
    if ov["coverage"] > 0:
        ratio = ov["coverage"] / max(1e-6, density_target)
        density_ok = 0.55 <= ratio <= 1.9
    # 同栅格阈值 0.01%（栅格化边界噪声），全分辨率阈值 0.05%
    # 叠层模式（layered）允许有意的遮挡，重叠加的是"是否超额度"而不是"是否为 0"
    if allow_overlap:
        overlap_ok = ov_hard["overlap_frac"] < 0.12
    else:
        overlap_ok = ov["overlap_frac"] < 1e-4 and ov_hard["overlap_frac"] < 5e-4
    tile_ok = True
    tile_chk = {"ok": None, "max_diff": None}
    if bg is not None:
        tile_chk = tileability(tile_img, tile, bg, elements, result)
        tile_ok = tile_chk["ok"]
    ok = overlap_ok and density_ok and tile_ok
    return {
        "ok": bool(ok),
        "evenness": ov_hard["evenness"],
        "spacing": spacing_stats(result, tile),
        "center_dist": gap,
        "overlap": ov,
        "overlap_hard": ov_hard,

        "seam": seam,
        "tileability": tile_chk,
        "density_target": density_target,
        "density_actual": round(ov["coverage"], 4),
        "density_ratio": round(ov["coverage"] / max(1e-6, density_target), 3),
        "n_elements": ov["n"],
    }
