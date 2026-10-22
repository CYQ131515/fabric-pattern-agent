"""离线把两条路线（纯算法 / 模型+compose）都还原出来做肉眼对比。

跑完 run.py 之后用它复盘：命令行只交付赢的那条，但两条都值得看一眼。
"""
from __future__ import annotations

import sys

import numpy as np
from PIL import Image, ImageDraw

from sifang import metrics, seam
from sifang.io_utils import (font, load_image, resize, save_image, tile_preview,
                             working_size)


def undo_half(a: np.ndarray) -> np.ndarray:
    h, w = a.shape[:2]
    return np.roll(np.roll(a, -(h // 2), axis=0), -(w // 2), axis=1)


def seam_crop(t: np.ndarray, s: int = 360) -> np.ndarray:
    """把真实的接缝十字滚到画面正中再裁一块。"""
    r = np.roll(np.roll(t, t.shape[0] // 2, axis=0), t.shape[1] // 2, axis=1)
    cy, cx = r.shape[0] // 2, r.shape[1] // 2
    return r[cy - s // 2:cy + s // 2, cx - s // 2:cx + s // 2]


def score(r: metrics.SeamReport) -> float:
    return (r.boundary_error * 2.0
            + abs(r.seam_visibility - 1.0) * 4.0
            + abs(r.ghosting - 1.0) * 30.0
            + abs(r.sharp_keep - 1.0) * 30.0)


def main(src: str, outdir: str, band: float = 0.12, align: float = 0.03) -> int:
    lo = load_image(src)
    w0, h0 = lo.shape[1], lo.shape[0]
    ww, hh = working_size(w0, h0)
    work = resize(lo, (ww, hh))
    to_edit = seam.half_offset(work)

    mask = load_image(f"{outdir}/mask.png")[..., 0]
    mo = load_image(f"{outdir}/model_output.png")
    if mo.shape[:2] != (hh, ww):
        mo = resize(mo, (ww, hh))

    base = seam.baseline(work, band, align)
    mdl = seam.wrap_align(undo_half(seam.compose(to_edit, mo, mask)), align)

    ref = to_edit
    for name, t in (("纯算法", base), ("模型+compose", mdl)):
        r = metrics.evaluate(t, ref)
        print(f"{name:12s} 边界 {r.boundary_error:.3f}  可见度 {r.seam_visibility:.3f}  "
              f"重影 {r.ghosting:.3f}  保析 {r.sharp_keep:.3f}  {r.grade}  "
              f"score={score(r):.2f}")

    save_image(resize(base, (w0, h0)), f"{outdir}/tile_baseline.png")
    save_image(resize(mdl, (w0, h0)), f"{outdir}/tile_model.png")

    panels = [seam_crop(work), seam_crop(base), seam_crop(mdl)]
    names = ["原图（硬缝）", "纯算法", "模型 + compose"]
    z = 340
    ps = [np.asarray(Image.fromarray(p).resize((z, z), Image.LANCZOS)) for p in panels]
    gap = np.full((z, 14, 3), 236, np.uint8)
    row = np.concatenate([ps[0], gap, ps[1], gap, ps[2]], 1)
    cv = Image.new("RGB", (row.shape[1], z + 42), (255, 255, 255))
    cv.paste(Image.fromarray(row), (0, 42))
    d = ImageDraw.Draw(cv)
    f = font(17)
    x = 0
    for n in names:
        d.text((x + 8, 12), n, fill=(20, 20, 20), font=f)
        x += z + 14
    save_image(np.asarray(cv), f"{outdir}/接缝特写.png")

    both = np.concatenate(
        [tile_preview(resize(base, (w0, h0)), 3),
         np.full((h0 * 3, 18, 3), 236, np.uint8),
         tile_preview(resize(mdl, (w0, h0)), 3)], axis=1)
    save_image(both, f"{outdir}/两条路线_3x3.png")
    print(f"saved {outdir}/接缝特写.png 与 {outdir}/两条路线_3x3.png")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
