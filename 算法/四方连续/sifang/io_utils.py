"""读写、尺寸规整、平铺拼图。全部走 Pillow + NumPy，不引入额外依赖。"""
from __future__ import annotations

import math
import os

import numpy as np
from PIL import Image

# APIMart 出图网关的硬约束（实测）：
#   边长必须是 16 的倍数；总像素不小于 655360；长宽比不超过 3:1。
SNAP = 16
MIN_PIXELS = 655_360
MAX_RATIO = 3.0


def load_image(path: str) -> np.ndarray:
    """读成 RGB uint8 的 HxWx3 数组。"""
    with Image.open(path) as im:
        return np.asarray(im.convert("RGB"), dtype=np.uint8)


def save_image(arr: np.ndarray, path: str) -> None:
    Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).save(path)


def to_pil(arr: np.ndarray) -> Image.Image:
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


def from_pil(im: Image.Image) -> np.ndarray:
    return np.asarray(im.convert("RGB"), dtype=np.uint8)


def working_size(w: int, h: int, snap: int = SNAP,
                 min_pixels: int = MIN_PIXELS,
                 max_ratio: float = MAX_RATIO) -> tuple[int, int]:
    """把任意尺寸规整成网关能接受的尺寸，并尽量贴近原图（少缩放一次）。

    步骤：先按比例放大到满足最小像素（不够的话），再把长宽比夹到 3:1 以内，
    最后各自对齐到 snap 的倍数。

    注意两个坑：
    1) 对齐到 snap 的倍数时会四舍五入，可能把结果舍到最小像素线以下
       （例如 508x500 按比例算出 816x803，h 进位到 800 → 652800 像素，
       比 655360 差一点，网关直接 HTTP 400 拒掉）。所以最后必须再校正一次。
    2) 校正时必须**等比放大**。早先的实现是"只往短边加"，遇到 6.7:1 的长条图
       会被拉成 1.4:1，图直接变形。
    """
    scale = 1.0
    if w * h < min_pixels:
        scale = (min_pixels / float(w * h)) ** 0.5

    tw = max(snap, int(round(w * scale / snap)) * snap)
    th = max(snap, int(round(h * scale / snap)) * snap)

    # 长宽比夹到 3:1 以内（缩长边，保持 snap 对齐）
    if max(tw, th) > max_ratio * min(tw, th):
        if tw > th:
            tw = max(snap, int(round(max_ratio * th / snap)) * snap)
        else:
            th = max(snap, int(round(max_ratio * tw / snap)) * snap)

    # 补足最小像素：等比放大，两个方向一起抬，向上取整到 snap
    if tw * th < min_pixels:
        k = (min_pixels / float(tw * th)) ** 0.5
        tw = max(snap, int(math.ceil(tw * k / snap)) * snap)
        th = max(snap, int(math.ceil(th * k / snap)) * snap)

    # 兜底：万一 ceil 后仍不足（浮点极边界），两边同时再加一格
    guard = 0
    while tw * th < min_pixels and guard < 128:
        tw += snap
        th += snap
        guard += 1
    return tw, th


def resize(arr: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """等比/非等比缩放到指定 (w, h)，用 Lanczos。"""
    if (arr.shape[1], arr.shape[0]) == size:
        return arr
    return np.asarray(to_pil(arr).resize(size, Image.LANCZOS), dtype=np.uint8)


def tile_preview(arr: np.ndarray, n: int = 3, gap: int = 0,
                 bg: int = 255) -> np.ndarray:
    """平铺 n x n，用来肉眼查接缝。gap>0 时留白缝，方便数格子。"""
    h, w = arr.shape[:2]
    if gap <= 0:
        return np.tile(arr, (n, n, 1))

    out = np.full((h * n + gap * (n - 1), w * n + gap * (n - 1), 3), bg, np.uint8)
    for i in range(n):
        for j in range(n):
            y, x = i * (h + gap), j * (w + gap)
            out[y:y + h, x:x + w] = arr
    return out


def side_by_side(images: list[np.ndarray], labels: list[str] | None = None,
                 height: int = 640, gap: int = 16) -> np.ndarray:
    """把多张图缩到同一高度横向排开，顶上留一条写标签。"""
    scaled = []
    for im in images:
        w = max(1, int(im.shape[1] * height / im.shape[0]))
        scaled.append(resize(im, (w, height)))

    bar = 36 if labels else 0
    total_w = sum(s.shape[1] for s in scaled) + gap * (len(scaled) - 1)
    out = np.full((height + bar, total_w, 3), 255, np.uint8)
    x = 0
    for s in scaled:
        out[bar:bar + height, x:x + s.shape[1]] = s
        x += s.shape[1] + gap

    if labels:
        from PIL import ImageDraw
        canvas = to_pil(out)
        d = ImageDraw.Draw(canvas)
        f = font(18)
        x = 0
        for lab, s in zip(labels, scaled):
            d.text((x + 8, 10), lab, fill=(30, 30, 30), font=f)
            x += s.shape[1] + gap
        out = np.asarray(canvas, dtype=np.uint8)
    return out


# PIL 自带字体不含中文，标签会渲染成方块。按优先级找系统里的中文字体。
_FONT_CANDIDATES = (
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)
_font_cache: dict[int, object] = {}


def font(size: int = 18):
    """拿一个能显示中文的字体，找不到就退回 PIL 默认字体。"""
    from PIL import ImageFont

    if size in _font_cache:
        return _font_cache[size]
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                _font_cache[size] = ImageFont.truetype(path, size)
                return _font_cache[size]
            except Exception:                       # noqa: BLE001
                continue
    _font_cache[size] = ImageFont.load_default()
    return _font_cache[size]


def to_data_url(arr: np.ndarray, fmt: str = "JPEG", quality: int = 92) -> str:
    """打包成 data URL，给大模型接口用。"""
    import base64
    import io

    buf = io.BytesIO()
    im = to_pil(arr)
    if fmt.upper() == "JPEG":
        im.save(buf, "JPEG", quality=quality, optimize=True)
        mime = "image/jpeg"
    else:
        im.save(buf, "PNG")
        mime = "image/png"
    return f"data:{mime};base64," + base64.b64encode(buf.getvalue()).decode()


def mask_to_data_url(mask: np.ndarray) -> str:
    """把内部 mask（255 = 允许改）转成网关要的 RGBA PNG。

    GPT Image 系列的语义是"alpha=0 的地方才重绘"，所以要把 mask 取反塞进 alpha。
    必须是带 alpha 的 PNG —— 传灰度图网关不认。
    """
    import base64
    import io

    from PIL import ImageOps

    m = Image.fromarray(np.clip(mask, 0, 255).astype(np.uint8), "L")
    rgba = Image.merge("RGBA", (m, m, m, ImageOps.invert(m)))
    buf = io.BytesIO()
    rgba.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
