"""把"挖掉花型后留下的洞"补成一块能看的底布。

为什么需要：底布层要是不补，关掉所有花型层就只剩棋盘格。
为什么敢补：被补的像素在任何图层组合下都不会露出来（露出的地方 alpha 必为 1），
所以补得再不像，合成结果仍然严格等于原图。

补法分两级：
1. **按图案周期拼贴**（主力）。这是平铺图，图案自身就是重复的，所以可以先估出
   重复周期，然后从"知道的地方"平移一块真实纹理过来填洞。借来的是真纹理，
   不是插值出来的糊。
2. **最近邻扩散**（兜底）。拼贴借不满的残余，用距离变换找最近已知像素补上。
"""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageFilter


def fill_holes(rgb: np.ndarray, hole: np.ndarray, smooth: bool = True,
               tile: bool = True) -> np.ndarray:
    """rgb: float (H,W,3)；hole: bool (H,W)。返回同形状的底布。

    这是**平铺图**（四方连续），所以最优的补法不是插值，是拿图案自己的
    重复周期去"借"一块真实纹理过来 —— 借来的像素是真的底布纹理，
    比平滑涂抹自然得多。借不满的残余再用最近邻扩散兜底。
    """
    rgb = np.asarray(rgb, dtype=np.float64)
    hole = hole.astype(bool)
    if not hole.any():
        return rgb.copy()
    if hole.all():
        # 整幅都被标成花型了，没有可参考的底，退回原图
        return rgb.copy()

    remaining = hole.copy()
    out = rgb.copy()
    n_hole = int(hole.sum())
    tiled = 0
    if tile:
        periods = estimate_periods(rgb, hole, top=4)
        # 一个周期能借到的有限，就多借几圈（2 倍、3 倍周期）——
        # 平铺图整倍数位移仍然是同一个图案，借来的依旧是真纹理
        shifts: list[tuple[int, int]] = []
        for k in (1, 2, 3, 4):
            for dx, dy in periods:
                if k * abs(dx) < rgb.shape[1] * 3 and k * abs(dy) < rgb.shape[0] * 3:
                    shifts.append((dx * k, dy * k))
        for dx, dy in shifts:
            if not remaining.any():
                break
            src = np.roll(np.roll(rgb, dy, 0), dx, 1)
            src_ok = np.roll(np.roll(~hole, dy, 0), dx, 1)
            take = remaining & src_ok
            if take.any():
                out[take] = src[take]
                tiled += int(take.sum())
                remaining &= ~take

    if remaining.any():                       # 残余用最近邻扩散
        try:
            from scipy.ndimage import distance_transform_edt
            # 注意：索引的是 out（已含拼贴借来的像素），不是 rgb ——
            # 索引 rgb 会把洞里的花型像素当成"已知颜色"取回来
            _, idx = distance_transform_edt(remaining, return_indices=True)
            src = out[tuple(idx)]
        except Exception:                     # noqa: BLE001
            src = _fill_diffusion(out, remaining)
        out = np.where(remaining[..., None], src, out)

    # 借来的本来就是真纹理，再去糊它就只剩坏处；只有大面积还在靠扩散兜底时才需要抹平
    if smooth and (tiled / max(1, n_hole)) < 0.5:
        r = max(1, min(rgb.shape[:2]) // 200)
        blurred = np.asarray(
            Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))
            .filter(ImageFilter.GaussianBlur(r)), dtype=np.float64)
        # 只在洞里用平滑后的颜色，洞外保持原图像素不变
        out = np.where(hole[..., None], blurred, rgb)
    return out


def estimate_periods(rgb: np.ndarray, hole: np.ndarray, top: int = 4,
                     max_shift_ratio: float = 0.55,
                     min_shift_ratio: float = 0.05,
                     coarse: int = 80,
                     min_score: float = 0.25) -> list[tuple[int, int]]:
    """估图案的重复周期：找几个"平移后和原图最像"的**足够大**的位移。

    两个必须的约束（都踩过）：
    - **排除小位移**。相邻像素天然高度相关，不加下限会一路选到 (5,5) 这种
      假周期，等于没平移，一个洞都填不上。
    - 只看两边都已知的像素，否则统计会被花型像素污染。
    """
    h, w = rgb.shape[:2]
    s = max(1, int(round(max(h, w) / coarse)))
    gh, gw = max(6, h // s), max(6, w // s)
    g = np.asarray(Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8))
                   .resize((gw, gh), Image.BILINEAR).convert("L"),
                   dtype=np.float64)
    known = np.asarray(Image.fromarray((~hole).astype(np.uint8) * 255)
                       .resize((gw, gh), Image.BILINEAR), dtype=np.float64) > 200

    min_m, max_m = max(1, int(gh * min_shift_ratio)), max(2, int(gh * max_shift_ratio))
    min_n, max_n = max(1, int(gw * min_shift_ratio)), max(2, int(gw * max_shift_ratio))

    best: list[tuple[float, int, int]] = []
    for dy in range(min_m, max_m):
        for dx in range(min_n, max_n):
            if dx < min_n and dy < min_m:
                continue
            m = known[:-dy, :-dx] & known[dy:, dx:]
            if m.sum() < 64:
                continue
            a = np.where(m, g[:-dy, :-dx], 0.0)[m]
            b = np.where(m, g[dy:, dx:], 0.0)[m]
            av, bv = a - a.mean(), b - b.mean()
            den = float(np.sqrt((av * av).sum() * (bv * bv).sum())) + 1e-6
            score = float((av * bv).sum()) / den
            if score >= min_score:
                best.append((score, dx * s, dy * s))

    best.sort(reverse=True)
    picked: list[tuple[int, int]] = []
    sep = min(h, w) * 0.08
    for _, dx, dy in best:
        if all(abs(dx - px) + abs(dy - py) > sep for px, py in picked):
            picked.append((dx, dy))
        if len(picked) >= top:
            break
    if not picked:                            # 找不到周期就退回"远距离借用"
        picked = [(w // 3, h // 3), (-w // 3, h // 3), (w // 3, -h // 3)]
    return picked


def _fill_diffusion(rgb: np.ndarray, hole: np.ndarray,
                    iters: int = 400) -> np.ndarray:
    """scipy 不可用时的兜底：反复用已知/已填邻居的平均去填未知像素。"""
    out = rgb.copy()
    known = ~hole
    for _ in range(iters):
        if known.all():
            break
        acc = np.zeros_like(out)
        cnt = np.zeros(out.shape[:2], dtype=np.float64)
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            src = np.roll(np.roll(out, dx, 0), dy, 1)
            m = np.roll(np.roll(known, dx, 0), dy, 1)
            acc += src * m[..., None]
            cnt += m
        nz = cnt > 0
        cand = np.zeros_like(out)
        cand[nz] = acc[nz] / cnt[nz][..., None]
        out = np.where(known[..., None], out, cand)
        known = known | (cnt > 0)
    return out
