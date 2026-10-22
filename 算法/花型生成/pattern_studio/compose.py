"""
pattern_studio.compose —— 第 6 层：渲染（无缝拼成最终图稿）

无缝的实现（这里借鉴了 patterninja 的思路，但细节不同）
----------------------------------------------------
patterninja：SVG 里对每个元素放 3×3 个 `<use>`，靠外层视口裁掉跨界部分。
本实现：位图元素，把每个元素在 9 个偏移 (−W,0,W)×(−H,0,H) 上各合成一次，
        越界部分自然贴到对边 —— 数学上等价，但支持 alpha 位图与任意旋转。
        背景同理：要么用本身可平铺的 AI 背景，要么用 `mirror` 模式把背景强制做成无缝。
"""
from __future__ import annotations

import base64
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

PREVIEW_N = 2


# ------------------------------------------------------------------ 背景

def seam_ratio(img: Image.Image) -> float:
    """粗糙接缝评分：左右/上下边界差 vs 内部相邻差中位数。用于判断 AI 背景能不能直接平铺。"""
    import numpy as np

    a = np.asarray(img.convert("RGB"), dtype=np.float32)
    if a.shape[0] < 8 or a.shape[1] < 8:
        return 1.0
    inner_h = float(np.median(np.abs(np.diff(a, axis=1)).mean(axis=(0, 2))))
    seam_h = float(np.abs(a[:, 0, :] - a[:, -1, :]).mean())
    inner_v = float(np.median(np.abs(np.diff(a, axis=0)).mean(axis=(1, 2))))
    seam_v = float(np.abs(a[0, :, :] - a[-1, :, :]).mean())
    return max(seam_h / max(1e-6, inner_h), seam_v / max(1e-6, inner_v))


def auto_fade_amount(bg: Image.Image, base: float = 0.35) -> float:
    """按背景"花不花"自动决定压淡比例：越花越压。

    生图模型给的背景经常是完整花稿（和主体同强度），压 30% 根本压不住；
    用亮度标准差估计"信息量"，映射到 0.35–0.78 的压淡比例。
    """
    import numpy as np

    lum = np.asarray(bg.convert("L"), dtype=np.float32) / 255.0
    std = float(lum.std())
    return round(max(base, min(0.78, base + 3.2 * std)), 3)


def fade_background(bg: Image.Image, paper=(245, 240, 230, 255), amount: float = 0.45,
                    desaturate: float = 0.35) -> Image.Image:
    """把 AI 背景压成"暗纹"：降饱和 + 与纸色混合。

    这一步是必须的：生图模型给的"背景图案"往往和主体元素同一个视觉强度，
    直接铺底会变成"花压花"。压淡后主体才跳出来，也更接近真实印花的分层关系。
    """
    from PIL import ImageEnhance

    out = bg.convert("RGBA")
    if desaturate > 0:
        out = ImageEnhance.Color(out.convert("RGB")).enhance(max(0.0, 1.0 - desaturate)).convert("RGBA")
    if amount > 0:
        overlay = Image.new("RGBA", out.size, tuple(int(x) for x in paper))
        out = Image.blend(out, overlay, max(0.0, min(0.9, amount)))
    return out


def fit_background(bg: Image.Image, tile: tuple[int, int], mode: str = "cover") -> Image.Image:
    """把背景图处理成 tile 尺寸。

    cover  : 等比铺满 + 居中裁切（AI 出的是"整张图案"，通常本身就是无缝的）
    mirror : 四象限镜像，**强制无缝**（AI 背景不可平铺时的保险方案，会有对称感）
    tile   : 直接拉伸到 tile（假设已经是可平铺的无缝图）
    """
    W, H = tile
    bg = bg.convert("RGBA")
    if mode == "auto":
        mode = "cover" if seam_ratio(bg) < 1.35 else "mirror"
    if mode == "tile":
        return bg.resize((W, H), Image.LANCZOS)
    if mode == "mirror":
        half = bg.resize((W // 2, H // 2), Image.LANCZOS)
        q1 = half
        q2 = half.transpose(Image.FLIP_LEFT_RIGHT)
        q3 = half.transpose(Image.FLIP_TOP_BOTTOM)
        q4 = q3.transpose(Image.FLIP_LEFT_RIGHT)
        out = Image.new("RGBA", (W, H))
        out.paste(q1, (0, 0)); out.paste(q2, (W // 2, 0))
        out.paste(q3, (0, H // 2)); out.paste(q4, (W // 2, H // 2))
        return out
    # cover
    scale = max(W / bg.width, H / bg.height)
    nw, nh = max(W, int(round(bg.width * scale))), max(H, int(round(bg.height * scale)))
    bg = bg.resize((nw, nh), Image.LANCZOS)
    return bg.crop(((nw - W) // 2, (nh - H) // 2, (nw - W) // 2 + W, (nh - H) // 2 + H))


def transform_element(el_img: Image.Image, size_px: tuple[int, int], angle: float,
                      flip: bool) -> Image.Image:
    """把元素缩放到目标尺寸 + 旋转 + 镜像（保持 RGBA）。"""
    w, h = max(1, size_px[0]), max(1, size_px[1])
    img = el_img.resize((w, h), Image.LANCZOS)
    if flip:
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
    if abs(angle) > 0.01:
        img = img.rotate(angle, expand=True, resample=Image.BICUBIC)
    return img


def _composite_wrap(canvas: Image.Image, patch: Image.Image, x: int, y: int) -> None:
    """把 patch 以 (x,y) 为左上角合成到 canvas 上，**越界部分绕回对边**。

    注意：PIL 的 alpha_composite 对负坐标/越界是"裁剪"而不是"环绕"，
    直接传负数会把跨界的元素切掉 —— 这正是接缝的根源。这里手动切成 9 份再贴。
    """
    W, H = canvas.size
    pw, ph = patch.size
    for dx in (-W, 0, W):
        for dy in (-H, 0, H):
            px, py = x + dx, y + dy
            x0, y0 = max(0, px), max(0, py)
            x1, y1 = min(W, px + pw), min(H, py + ph)
            if x1 <= x0 or y1 <= y0:
                continue
            sx0, sy0 = x0 - px, y0 - py
            sub = patch.crop((sx0, sy0, sx0 + (x1 - x0), sy0 + (y1 - y0)))
            canvas.alpha_composite(sub, dest=(x0, y0))


def render_tile(tile: tuple[int, int], bg: Image.Image, elements: dict, result: dict,
                shadow: bool = False) -> Image.Image:
    """把 placements 合成到背景上，3×3 环绕 → 得到一张真正可平铺的 tile。"""
    W, H = tile
    canvas = bg.copy().convert("RGBA")
    if shadow:
        shadow_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    for p in result["placements"]:
        el = elements.get(p.element_id)
        if el is None:
            continue
        patch = transform_element(el.image, p.size_px, p.angle, p.flip)
        pw, ph = patch.size
        x = int(round(p.cx - pw / 2)); y = int(round(p.cy - ph / 2))
        if shadow:
            sh = Image.new("RGBA", patch.size, (0, 0, 0, 0))
            alpha = patch.split()[-1].point(lambda v: int(v * 0.28))
            sh.putalpha(alpha)
            _composite_wrap(shadow_layer, sh, x + 4, y + 6)
        _composite_wrap(canvas, patch, x, y)
    if shadow:
        canvas = Image.alpha_composite(canvas, shadow_layer)
    return canvas


def render_infinite(tile: tuple[int, int], bg: Image.Image, elements: dict, result: dict,
                    nx: int = 2, ny: int = 2) -> Image.Image:
    """"无限画布"参照渲染：把每个元素直接摆在 (cx+kW, cy+mH) 的所有格位上，不经过 tile 裁剪。

    它的作用是给"无缝"提供**可判定的 ground truth**：
    把 tile 平铺 nx×ny 的结果，必须与这里逐像素完全相同 —— 这才是真无缝。
    """
    W, H = tile
    CW, CH = W * nx, H * ny
    canvas = Image.new("RGBA", (CW, CH))
    for j in range(ny):
        for i in range(nx):
            canvas.paste(bg, (i * W, j * H))
    for p in result["placements"]:
        el = elements.get(p.element_id)
        if el is None:
            continue
        patch = transform_element(el.image, p.size_px, p.angle, p.flip)
        pw, ph = patch.size
        for k in range(-1, nx + 1):
            for m in range(-1, ny + 1):
                x = int(round(p.cx + k * W - pw / 2))
                y = int(round(p.cy + m * H - ph / 2))
                if x >= CW or y >= CH or x + pw <= 0 or y + ph <= 0:
                    continue
                tmp = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
                tmp.alpha_composite(patch, dest=(x, y))
                canvas = Image.alpha_composite(canvas, tmp)
    return canvas


def preview_tile(tile_img: Image.Image, n: int = PREVIEW_N) -> Image.Image:
    """把 tile 平铺 n×n，用于人眼检查接缝。"""
    W, H = tile_img.size
    out = Image.new("RGBA", (W * n, H * n))
    for j in range(n):
        for i in range(n):
            out.paste(tile_img, (i * W, j * H))
    return out


# ------------------------------------------------------------------ 交付物

CJK_FONTS = (
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)


def _font(size: int):
    for path in CJK_FONTS:
        try:
            return ImageFont.truetype(path, size)
        except Exception:  # noqa: BLE001
            continue
    return ImageFont.load_default()


def contact_sheet(items: list[tuple[Image.Image, str]], out_path: Path,
                  cell: int = 760, gap: int = 16, header: str = "") -> None:
    """把三版图稿并排拼成一张对比图（带标题），交付/评审用。"""
    n = len(items)
    W = n * cell + (n + 1) * gap
    head = 64 if header else 0
    H = cell + gap * 2 + head + 16
    canvas = Image.new("RGB", (W, H), (22, 22, 26))
    d = ImageDraw.Draw(canvas)
    if header:
        d.text((gap, 18), header, font=_font(34), fill=(235, 232, 226))
    for i, (img, label) in enumerate(items):
        t = img.copy().convert("RGB")
        t.thumbnail((cell, cell), Image.LANCZOS)
        x = gap + i * (cell + gap) + (cell - t.width) // 2
        y = head + gap + (cell - t.height) // 2
        canvas.paste(t, (x, y))
        d.text((gap + i * (cell + gap) + 8, head + gap + cell + 2), label,
               font=_font(24), fill=(200, 196, 190))
    canvas.save(out_path, quality=95)


def svg_wrap(png_path: Path, tile: tuple[int, int], out_svg: Path, title: str = "") -> None:
    """把位图 tile 包成 SVG（方便交付给设计软件 / 当纹理用）。

    注意：这**不是**矢量稿；真要矢量元素，需要后续用 vtracer 之类做位图转矢量。
    """
    b64 = base64.b64encode(Path(png_path).read_bytes()).decode()
    W, H = tile
    out_svg.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="{W}" height="{H}" viewBox="0 0 {W} {H}">'
        f"<title>{title}</title>"
        f'<image x="0" y="0" width="{W}" height="{H}" xlink:href="data:image/png;base64,{b64}"/>'
        f"</svg>", encoding="utf-8")
