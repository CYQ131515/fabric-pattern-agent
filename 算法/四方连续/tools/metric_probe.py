"""指标判别力的对照实验：找出哪个统计量真的能区分"羽化糊掉"和"模型补好"。

用法：先跑一次 run.py 留下 intermediates，再用本脚本复盘。
"""
from __future__ import annotations

import sys

import numpy as np
from PIL import Image, ImageFilter

from sifang import metrics, seam
from sifang.io_utils import load_image, resize, working_size


def gray(a):
    return a[..., :3].mean(2).astype(np.float64) if a.ndim == 3 else a.astype(np.float64)


def band_masks(h, w, frac):
    kb = max(2, int(min(h, w) * frac))
    m = np.zeros((h, w), bool)
    m[:, :kb] = True
    m[:, -kb:] = True
    m[:kb, :] = True
    m[-kb:, :] = True
    return m, ~m


def highfreq(g, radius=3.0):
    """高通能量：原图减高斯模糊。羽化≈模糊，会先打掉这一项。"""
    im = Image.fromarray(np.clip(g, 0, 255).astype(np.uint8))
    blur = np.asarray(im.filter(ImageFilter.GaussianBlur(radius)), np.float64)
    return np.abs(g - blur)


def laplacian(g):
    """5 点 Laplacian 的绝对值，保持数组形状。"""
    lap = (4 * g - np.roll(g, 1, 0) - np.roll(g, -1, 0)
           - np.roll(g, 1, 1) - np.roll(g, -1, 1))
    return np.abs(lap)


def block_std(g, block=16):
    h, w = g.shape
    hh, ww = h // block * block, w // block * block
    return g[:hh, :ww].reshape(hh // block, block, ww // block, block).std(axis=(1, 3))


def ratio(edge, inner, drop_below_median=True):
    """边缘带 ÷ 内部。为了不被空白块稀释，只留高于内部中位数的块/像素。"""
    if drop_below_median:
        thr = float(np.median(inner))
        e, i = edge[edge > thr], inner[inner > thr]
        if e.size > 8 and i.size > 8:
            edge, inner = e, i
    return float(np.mean(edge) / max(1e-6, np.mean(inner)))


def report(name, t, frac=0.08):
    g = gray(t)
    h, w = g.shape
    m, inner = band_masks(h, w, frac)

    bs = block_std(g)
    kb = max(1, int(min(h, w) * frac) // 16)
    bs_edge = np.concatenate([bs[:kb].ravel(), bs[-kb:].ravel(),
                              bs[:, :kb].ravel(), bs[:, -kb:].ravel()])
    bs_inner = bs[kb:-kb, kb:-kb].ravel()

    vals = {
        "块std(现行ghosting)": ratio(bs_edge, bs_inner),
        "高通能量": ratio(highfreq(g)[m], highfreq(g)[inner]),
        "Laplacian": ratio(laplacian(g)[m], laplacian(g)[inner]),
        "高通·不丢低对比": ratio(highfreq(g)[m], highfreq(g)[inner], False),
        "Laplacian·不丢低对比": ratio(laplacian(g)[m], laplacian(g)[inner], False),
    }
    r = metrics.evaluate(t)
    print(f"\n[{name}]  boundary={r.boundary_error:.3f}  visibility={r.seam_visibility:.3f}  "
          f"ghosting={r.ghosting:.3f}  {r.grade}")
    for k, v in vals.items():
        print(f"    {k:22s} {v:.4f}")


def main(src, outdir):
    lo = load_image(src)
    w0, h0 = lo.shape[1], lo.shape[0]
    ww, hh = working_size(w0, h0)
    work = resize(lo, (ww, hh))
    to_edit = seam.half_offset(work)
    mask = load_image(f"{outdir}/mask.png")[..., 0]
    mo = load_image(f"{outdir}/model_output.png")
    if mo.shape[:2] != (hh, ww):
        mo = resize(mo, (ww, hh))

    def undo(a):
        return np.roll(np.roll(a, -(hh // 2), axis=0), -(ww // 2), axis=1)

    base = seam.baseline(work, 0.12, 0.03)
    mdl = seam.wrap_align(undo(seam.compose(to_edit, mo, mask)), 0.03)

    report("纯算法（肉眼糊）", base)
    report("模型+compose（肉眼干净）", mdl)
    report("参照 原图直接平铺", work)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
