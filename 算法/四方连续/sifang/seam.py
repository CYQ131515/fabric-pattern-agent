"""接缝处理：全部是确定性算法，用 NumPy + Pillow 实现，不调用模型。

核心思路（为什么这样能无缝）
    一张图之所以不能平铺，是因为"最右一列"和"最左一列"对不上、
    "最下一行"和"最上一行"对不上。把这四个问题边从画面边界挪到画面正中，
    问题就从"四条边"集中成"一个十字"。此时画面外围全是原图内容，天然连续。
    模型只需要修中央那个十字，改完再移回来 —— 平铺就无缝了。

    half_offset      边界 → 中央十字
    central_cross_mask  给模型划出"只许改这里"的范围
    compose          带外一律还原成原图像素（保真的根本保障）
    wrap_align       收尾时把边界在像素级真正对齐，兜住模型的最后一点误差
"""
from __future__ import annotations

import numpy as np


def _as_float(a: np.ndarray) -> np.ndarray:
    return a.astype(np.float32)


def _as_uint8(a: np.ndarray) -> np.ndarray:
    return np.clip(np.round(a), 0, 255).astype(np.uint8)


def half_offset(arr: np.ndarray) -> np.ndarray:
    """平移半幅：把接缝从四条边推到画面中央。"""
    h, w = arr.shape[:2]
    return np.roll(np.roll(arr, h // 2, axis=0), w // 2, axis=1)


def _cosine_ramp(n: int) -> np.ndarray:
    """0→1→0 的余弦钟形权重，用于接缝带的交叉溶解。"""
    t = np.linspace(-np.pi, np.pi, n, dtype=np.float32)
    return 0.5 * (1.0 + np.cos(t))


def feather_seam(arr: np.ndarray, band: int, axis: int) -> np.ndarray:
    """羽化抹平一条位于中点附近的内部接缝。

    在接缝两侧各取 band 宽，用钟形权重把图像与"朝接缝方向平移 band 的副本"混合，
    接缝正中完全取副本，向带外逐渐回到原图 —— 带外一个像素都不动。

    axis=0 抹横向接缝（沿 y 混合），axis=1 抹竖向接缝（沿 x 混合）。
    """
    if band < 1:
        return arr
    size = arr.shape[axis]
    seam = size // 2
    band = min(band, seam, size - seam)
    if band < 1:
        return arr

    a = _as_float(arr)
    shifted = np.roll(a, band, axis=axis)

    lo, hi = seam - band, seam + band + 1
    ramp = _cosine_ramp(hi - lo)
    w = ramp[None, :, None] if axis == 1 else ramp[:, None, None]

    out = a.copy()
    if axis == 1:
        out[:, lo:hi] = (1.0 - w) * a[:, lo:hi] + w * shifted[:, lo:hi]
    else:
        out[lo:hi] = (1.0 - w) * a[lo:hi] + w * shifted[lo:hi]
    return _as_uint8(out)


def central_cross_mask(shape: tuple[int, ...], band_frac: float = 0.07) -> np.ndarray:
    """中央十字带 mask：255 = 允许模型改，0 = 禁止。

    带子要窄 —— 它不是"给模型发挥的画布"，只是接缝那一条。
    0.07 时覆盖画面约 26%。
    """
    h, w = shape[:2]
    mask = np.zeros((h, w), dtype=np.uint8)
    bw = max(4, int(w * band_frac))
    bh = max(4, int(h * band_frac))
    mask[:, max(0, w // 2 - bw): w // 2 + bw] = 255
    mask[max(0, h // 2 - bh): h // 2 + bh, :] = 255
    return mask


def _box_blur(a: np.ndarray, radius: int) -> np.ndarray:
    """可分离 box blur，给 mask 做软边，避免合成处留下硬边。"""
    if radius < 1:
        return a
    k = np.ones(2 * radius + 1, dtype=np.float32) / (2 * radius + 1)
    a = np.apply_along_axis(lambda m: np.convolve(m, k, mode="same"), 0, a)
    a = np.apply_along_axis(lambda m: np.convolve(m, k, mode="same"), 1, a)
    return a


def compose(base: np.ndarray, edited: np.ndarray, mask: np.ndarray,
            feather: int = 12) -> np.ndarray:
    """强制还原：mask 之外一律用 base 的像素，模型画了什么全部丢掉。

    这是保真的根本保障，不是可选项 —— 少了它，"保真"只能靠模型自觉，
    而实测证明模型不自觉（它会顺手把整张图重画一遍）。
    """
    alpha = _box_blur((mask > 0).astype(np.float32), feather)
    a = alpha[..., None]
    out = _as_float(base) * (1.0 - a) + _as_float(edited) * a
    return _as_uint8(out)


def fold_blend_edges(arr: np.ndarray, band: int) -> np.ndarray:
    """折叠混合：让对边在像素层面严格接续。

    右边缘带按 smoothstep 权重逐渐替换为"左边缘带的反向副本"，
    带末端恰好等于左边缘第一个像素，于是右边缘与左边缘严格相等。
    会带来一点镜像感，band 取小时不明显。
    """
    if band < 1:
        return arr
    a = _as_float(arr)
    h, w = a.shape[:2]
    band = min(band, w // 2, h // 2)
    if band < 1:
        return arr

    t = np.linspace(0.0, 1.0, band, dtype=np.float32)
    u = t * t * (3.0 - 2.0 * t)                      # smoothstep

    out = a.copy()
    uw = u[None, :, None]
    out[:, w - band:] = (1.0 - uw) * out[:, w - band:] + \
        uw * a[:, band - 1::-1][:, :band]
    uh = u[:, None, None]
    out[h - band:, :] = (1.0 - uh) * out[h - band:, :] + \
        uh * a[band - 1::-1, :][:band, :]
    return _as_uint8(out)


def wrap_align(arr: np.ndarray, band_frac: float = 0.03) -> np.ndarray:
    """收尾：让对边在像素层面真正接续，同时尽量少动画面。

    1. 把接缝卷到画面中央（此时"边界"变成原图内部，天然连续）
    2. 用很窄的羽化带抹平中央接缝
    3. 卷回原位，再用极窄的折叠混合把边界严格对齐
    """
    h, w = arr.shape[:2]
    bx = max(1, int(w * band_frac / 2))
    by = max(1, int(h * band_frac / 2))

    off = half_offset(arr)
    off = feather_seam(off, bx, axis=1)
    off = feather_seam(off, by, axis=0)

    back = np.roll(np.roll(off, -(h // 2), axis=0), -(w // 2), axis=1)
    return fold_blend_edges(back, band=max(2, int(min(h, w) * band_frac / 2)))


def baseline(img: np.ndarray, band_frac: float = 0.12,
             align_frac: float = 0.03) -> np.ndarray:
    """纯算法基线：位移 + 羽化 + 边界对齐，不调用任何模型。

    永远可用，结果在数学上一定无缝；代价是羽化带会有重影。
    用来给模型路线兜底，也用来当对照。

    注意最后要移回原位：输出的 tile 与输入同朝向，
    否则拿它和原图比保真度会得到一堆假差异。
    """
    h, w = img.shape[:2]
    off = half_offset(img)
    off = feather_seam(off, max(1, int(w * band_frac / 2)), axis=1)
    off = feather_seam(off, max(1, int(h * band_frac / 2)), axis=0)
    back = np.roll(np.roll(off, -(h // 2), axis=0), -(w // 2), axis=1)
    return wrap_align(back, align_frac)
