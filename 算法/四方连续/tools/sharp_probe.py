"""验证"修缝保析度"指标：用同一块区域修补前的样子做分母，比值才可解释。

用法：先跑一次 run.py 留下 intermediates，再用本脚本复盘。
    PYTHONPATH=. python tools/sharp_probe.py 输入图 out目录
"""
from __future__ import annotations

import sys

import numpy as np

from sifang import metrics, seam
from sifang.io_utils import load_image, resize, working_size


def gray(a):
    return a[..., :3].mean(2).astype(np.float64) if a.ndim == 3 else a.astype(np.float64)


def sharpness_map(g):
    """5 点 Laplacian 的绝对值。羽化模糊会先打掉它；补新内容会抬高它。"""
    lap = (4 * g - np.roll(g, 1, 0) - np.roll(g, -1, 0)
           - np.roll(g, 1, 1) - np.roll(g, -1, 1))
    return np.abs(lap)


def seam_zone(h, w, frac, gap=4):
    """中央十字带，但挖掉正中 gap 像素 —— 那里是硬缝本身，不算内容。"""
    bw, bh = max(4, int(w * frac)), max(4, int(h * frac))
    yy, xx = np.mgrid[0:h, 0:w]
    on_v = np.abs(xx - w // 2) < bw
    on_h = np.abs(yy - h // 2) < bh
    near_seam = (np.abs(xx - w // 2) <= gap) | (np.abs(yy - h // 2) <= gap)
    return (on_v | on_h) & ~near_seam


def ratio(a, b, frac=0.10, gap=4, keep_textured_only=False):
    """a 的接缝带清晰度 ÷ b 的（内容对齐、同位置）。"""
    h, w = a.shape[:2]
    z = seam_zone(h, w, frac, gap)
    sa = sharpness_map(gray(a))[z]
    sb = sharpness_map(gray(b))[z]
    if keep_textured_only:
        # 只比"修补前本来就有内容"的地方：留白/平背景两边都是一片平，
        # 会把差异稀释掉（羽化抹糊的地方本来就没东西可抹）。
        thr = np.median(sb)
        m = sb > thr
        if m.sum() > 64:
            sa, sb = sa[m], sb[m]
    return float(sa.mean() / max(1e-6, sb.mean()))


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

    cand = {
        "参照：原图本身（应为 1.000）": to_edit,
        "纯算法（肉眼糊）": seam.half_offset(seam.baseline(work, 0.12, 0.03)),
        "模型+compose（肉眼干净）": seam.half_offset(
            seam.wrap_align(undo(seam.compose(to_edit, mo, mask)), 0.03)),
    }
    print(f"\n=== {src} ===")
    print(f"  {'':30s} {'保析度':>8s} {'只比有内容的':>12s}   ghosting  visibility")
    for name, centered in cand.items():
        v1 = ratio(centered, to_edit)
        v2 = ratio(centered, to_edit, keep_textured_only=True)
        r = metrics.evaluate(undo(centered))
        print(f"  {name:28s} {v1:8.3f} {v2:12.3f}   {r.ghosting:8.3f} {r.seam_visibility:11.3f}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
