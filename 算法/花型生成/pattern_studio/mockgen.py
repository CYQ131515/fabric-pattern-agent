"""
pattern_studio.mockgen —— 无模型时的"程序化代笔"

作用有两个：
  1. **让整条流水线在没有生图 API 时也能跑通**（CI / 演示 / 调参）；
  2. 给"九宫格拆元素 → 去背 → 拼版 → 无缝"这几段提供一个可控的测试台：
     元素形状、数量、位置、颜色全已知，出问题能判断是拆格错还是排版错。

产物与真实模型**同构**：
  * `make_sheet()` 输出 1024² 白底九宫格（每格一个元素、留白、互不重叠）；
  * `make_background()` 输出可平铺的背景（用周期函数 + 环绕绘制保证无缝）。

画法上刻意模仿"水彩"：2× 超采样 + LANCZOS 降采样得到柔和边缘，
再叠一层低频纸纹与颗粒（numpy 生成，天然可平铺）。
"""
from __future__ import annotations

import math
import random

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

SS = 2  # 超采样倍数


# ------------------------------------------------------------------ 颜色工具

def hx(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def mix(a, b, t: float):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def rgba(c, a: int = 255):
    return (c[0], c[1], c[2], a)


def pick_palette(spec: dict) -> dict:
    """从 spec 的五色板里推断：花瓣深/浅、叶、点缀、底色。"""
    cols = [hx(c) for c in (spec.get("palette") or {}).get("colors") or ["#c8434a", "#3f5a56", "#efe6d9"]]
    while len(cols) < 4:
        cols.append(cols[-1])
    warm = max(cols[:4], key=lambda c: c[0] - c[2])          # 偏红 → 花
    cool = min(cols[:4], key=lambda c: c[0] - c[2] * 0.6)    # 偏绿/蓝 → 叶
    light = max(cols, key=lambda c: sum(c))
    return {
        "petal": warm,
        "petal_light": mix(warm, light, 0.55),
        "petal_deep": mix(warm, (30, 20, 24), 0.30),
        "leaf": cool,
        "leaf_light": mix(cool, light, 0.45),
        "accent": cols[-1],
        "paper": mix(light, (255, 255, 255), 0.75),
        "ink": mix(warm, (25, 22, 24), 0.72),
    }


def _rot(pts, cx, cy, ang):
    ca, sa = math.cos(ang), math.sin(ang)
    return [(cx + x * ca - y * sa, cy + x * sa + y * ca) for x, y in pts]


def _petal_poly(cx, cy, ang, length, width, curve=0.35):
    """一片叶子/花瓣的轮廓（尖头椭圆 + 弯曲），返回旋转后的点列。"""
    n = 22
    pts = []
    for i in range(n):
        t = i / (n - 1)
        # 宽度包络：两端收尖，中段最宽
        w = math.sin(math.pi * t) ** 0.65 * width
        x = (t - 0.5) * length
        y = w * (0.6 if t < 0.5 else -0.6) * 1.0 + curve * length * (t - 0.5) ** 2
        pts.append((x, y))
    for i in range(n - 2, 0, -1):
        t = i / (n - 1)
        w = math.sin(math.pi * t) ** 0.65 * width
        x = (t - 0.5) * length
        y = -w * (0.6 if t < 0.5 else -0.6) * 1.0 + curve * length * (t - 0.5) ** 2
        pts.append((x, y))
    return _rot(pts, cx, cy, ang)


# ------------------------------------------------------------------ 形状绘制

def draw_flower(d: ImageDraw.ImageDraw, cx, cy, r, pal, openness=1.0, rng=None, petals=None):
    rng = rng or random.Random(0)
    n = petals or rng.randint(10, 16)
    # 外圈大瓣
    for i in range(n):
        ang = 2 * math.pi * i / n + rng.uniform(-0.06, 0.06)
        L = r * rng.uniform(0.9, 1.15) * (0.75 + 0.25 * openness)
        W = r * rng.uniform(0.30, 0.40)
        col = pal["petal"] if i % 2 == 0 else pal["petal_light"]
        d.polygon(_petal_poly(cx, cy, ang, L, W), fill=rgba(col, 235))
    # 内圈小瓣（更亮）
    if openness > 0.45:
        m = max(5, n - 3)
        for i in range(m):
            ang = 2 * math.pi * i / m + 0.3
            L = r * rng.uniform(0.42, 0.62) * openness
            W = r * rng.uniform(0.16, 0.24)
            d.polygon(_petal_poly(cx, cy, ang, L, W), fill=rgba(pal["petal_light"], 240))
    # 花心
    r2 = r * 0.14 * (0.8 + 0.4 * openness)
    d.ellipse([cx - r2, cy - r2, cx + r2, cy + r2], fill=rgba(pal["accent"], 255))
    for i in range(7):
        a = 2 * math.pi * i / 7
        rr = r * 0.05
        px, py = cx + math.cos(a) * r2 * 0.55, cy + math.sin(a) * r2 * 0.55
        d.ellipse([px - rr, py - rr, px + rr, py + rr], fill=rgba(pal["ink"], 220))


def draw_bud(d: ImageDraw.ImageDraw, cx, cy, r, pal, rng=None):
    rng = rng or random.Random(1)
    # 花苞：一枚胖水滴 + 萼片
    body = _petal_poly(cx, cy - r * 0.1, -math.pi / 2, r * 1.1, r * 0.62, curve=0.1)
    d.polygon(body, fill=rgba(pal["petal"], 240))
    d.polygon(_petal_poly(cx, cy - r * 0.15, -math.pi / 2, r * 0.75, r * 0.3, curve=0.05),
              fill=rgba(pal["petal_light"], 235))
    for s in (-1, 1):
        d.polygon(_petal_poly(cx + s * r * 0.18, cy + r * 0.42, math.pi / 2 + s * 0.5,
                              r * 0.55, r * 0.16), fill=rgba(pal["leaf"], 240))
    # 茎
    d.line([cx, cy + r * 0.5, cx + r * 0.08, cy + r * 1.15], fill=rgba(pal["leaf"], 235), width=max(2, int(r * 0.07)))


def draw_leaves(d: ImageDraw.ImageDraw, cx, cy, r, pal, rng=None):
    """一枝 3 片叶的小枝：两片大叶 + 一片顶叶，带主脉（mock 用的"能看"版本）。"""
    rng = rng or random.Random(2)
    up = -math.pi / 2
    base = up + rng.uniform(-0.35, 0.35)
    stem_top = (cx + math.cos(base) * r * 0.85, cy + math.sin(base) * r * 0.85)
    d.line([(cx - math.cos(base) * r * 0.25, cy - math.sin(base) * r * 0.25), stem_top],
           fill=rgba(pal["leaf"], 240), width=max(3, int(r * 0.07)))
    spec = [(base - 0.75, 0.72, 0.30), (base + 0.75, 0.72, 0.30), (base + rng.uniform(-0.2, 0.2), 0.62, 0.24)]
    for i, (a, lf, wf) in enumerate(spec):
        L, W = r * lf, r * wf
        px, py = stem_top if i == 2 else (cx, cy)
        col = pal["leaf"] if i == 0 else pal["leaf_light"]
        d.polygon(_petal_poly(px, py, a, L, W, curve=0.08), fill=rgba(col, 242))
        tip = (px + math.cos(a) * L * 0.92, py + math.sin(a) * L * 0.92)
        d.line([(px, py), tip], fill=rgba(mix(col, (30, 40, 34), 0.45), 170), width=max(1, int(r * 0.02)))


def draw_cloud(d: ImageDraw.ImageDraw, cx, cy, r, pal, rng=None):
    rng = rng or random.Random(3)
    col = pal["accent"]
    # 祥云：一串圆 + 卷尾
    for i in range(4):
        a = rng.uniform(0, 2 * math.pi)
        rr = r * rng.uniform(0.22, 0.34)
        px = cx + math.cos(a) * r * 0.35 * (i / 3)
        py = cy + math.sin(a) * r * 0.25 * (i / 3) - r * 0.1 * (i / 3)
        d.ellipse([px - rr, py - rr, px + rr, py + rr], outline=rgba(col, 245), width=max(2, int(r * 0.09)))
    for k in range(2):
        pts = [(cx + r * 0.7 * math.cos(2.2 * t + k * 3.0) * (0.9 - 0.25 * t),
                cy + r * 0.7 * math.sin(2.2 * t + k * 3.0) * (0.9 - 0.25 * t)) for t in np.linspace(0, 2.6, 26)]
        d.line(pts, fill=rgba(col, 235), width=max(2, int(r * 0.08)), joint="curve")


def draw_vine(d: ImageDraw.ImageDraw, cx, cy, r, pal, rng=None):
    rng = rng or random.Random(4)
    base = rng.uniform(0, math.pi)
    pts = []
    for t in np.linspace(0, 1, 40):
        a = base + 2.4 * math.pi * t
        rad = r * (0.25 + 0.75 * t)
        pts.append((cx + math.cos(a) * rad, cy + math.sin(a) * rad * 0.85))
    d.line(pts, fill=rgba(pal["leaf"], 240), width=max(2, int(r * 0.07)), joint="curve")
    for i in range(6):
        t = 0.12 + 0.75 * i / 5
        a = base + 2.4 * math.pi * t
        rad = r * (0.25 + 0.75 * t)
        px, py = cx + math.cos(a) * rad, cy + math.sin(a) * rad * 0.85
        d.polygon(_petal_poly(px, py, a + 1.3, r * 0.34, r * 0.13),
                  fill=rgba(pal["leaf_light"] if i % 2 else pal["leaf"], 235))
        if i % 2 == 0:
            d.ellipse([px - r * 0.07, py - r * 0.07, px + r * 0.07, py + r * 0.07],
                      fill=rgba(pal["petal"], 240))


def draw_butterfly(d: ImageDraw.ImageDraw, cx, cy, r, pal, rng=None):
    col = pal["petal"]
    for s in (-1, 1):
        for k, (rx, ry, ang) in enumerate(((0.62, 0.42, 0.5), (0.45, 0.32, -0.35))):
            pts = _petal_poly(cx + s * r * 0.30, cy + k * r * 0.16, s * ang, r * rx, r * ry, curve=0.1)
            d.polygon(pts, fill=rgba(col if k == 0 else pal["petal_light"], 238))
    d.line([cx, cy - r * 0.35, cx, cy + r * 0.35], fill=rgba(pal["ink"], 240), width=max(2, int(r * 0.08)))
    for s in (-1, 1):
        d.line([cx, cy - r * 0.3, cx + s * r * 0.22, cy - r * 0.55], fill=rgba(pal["ink"], 220),
               width=max(1, int(r * 0.035)))


def draw_dots(d: ImageDraw.ImageDraw, cx, cy, r, pal, rng=None):
    rng = rng or random.Random(5)
    cols = [pal["accent"], pal["petal"], pal["leaf"], pal["petal_light"]]
    for i in range(14):
        a = rng.uniform(0, 2 * math.pi)
        rad = r * rng.uniform(0.1, 0.85)
        rr = r * rng.uniform(0.04, 0.11)
        px, py = cx + math.cos(a) * rad, cy + math.sin(a) * rad
        d.ellipse([px - rr, py - rr, px + rr, py + rr], fill=rgba(cols[i % 4], 235))


def draw_tiny_flower(d, cx, cy, r, pal, rng=None):
    rng = rng or random.Random(6)
    n = 5
    for i in range(n):
        a = 2 * math.pi * i / n
        d.polygon(_petal_poly(cx, cy, a, r * 0.8, r * 0.3), fill=rgba(pal["petal_light"], 235))
    d.ellipse([cx - r * 0.14, cy - r * 0.14, cx + r * 0.14, cy + r * 0.14], fill=rgba(pal["accent"], 245))


# ------------------------------------------------------------------ 元素/九宫格

SHAPE_BY_KEYWORD = [
    (("盛开", "主纹样", "bloom"), "flower"),
    (("半开", "半"), "flower_mid"),
    (("花苞", "bud"), "bud"),
    (("枝叶", "叶", "leaf", "枝"), "leaves"),
    (("祥云", "云", "cloud"), "cloud"),
    (("缠枝", "藤", "vine", "线性"), "vine"),
    (("蝴蝶", "butterfly"), "butterfly"),
    (("圆点", "点", "dot"), "dots"),
    (("小碎花", "tiny"), "tiny"),
]


def shape_for(element) -> str:
    if not isinstance(element, dict):
        element = {"name": str(element)}
    name = (element.get("name") or "") + " " + (element.get("describe") or "")
    low = name.lower()
    for keys, shape in SHAPE_BY_KEYWORD:
        if any(k.lower() in low for k in keys):
            return shape
    return "flower"


def draw_element(img: Image.Image, spec: dict, element: dict, box, rng) -> None:
    """在 img 的 box=(x0,y0,x1,y1) 内画一个元素（居中、留白 18%）。"""
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    r = min(x1 - x0, y1 - y0) * 0.5 * 0.82
    d = ImageDraw.Draw(img, "RGBA")
    pal = pick_palette(spec)
    shape = shape_for(element)
    if shape == "flower":
        draw_flower(d, cx, cy, r, pal, openness=1.0, rng=rng, petals=rng.randint(11, 15))
    elif shape == "flower_mid":
        draw_flower(d, cx, cy, r * 0.9, pal, openness=0.62, rng=rng, petals=rng.randint(8, 11))
    elif shape == "bud":
        draw_bud(d, cx, cy, r * 0.9, pal, rng=rng)
    elif shape == "leaves":
        draw_leaves(d, cx, cy, r, pal, rng=rng)
    elif shape == "cloud":
        draw_cloud(d, cx, cy, r * 0.95, pal, rng=rng)
    elif shape == "vine":
        draw_vine(d, cx, cy, r, pal, rng=rng)
    elif shape == "butterfly":
        draw_butterfly(d, cx, cy, r * 0.95, pal, rng=rng)
    elif shape == "dots":
        draw_dots(d, cx, cy, r, pal, rng=rng)
    else:
        draw_tiny_flower(d, cx, cy, r * 0.8, pal, rng=rng)


def _paper_texture(size: int, pal, strength: float = 0.04) -> Image.Image:
    """低频纸纹 + 颗粒，叠加在整张图上（让 mock 更接近水彩纸）。"""
    rng = np.random.default_rng(7)
    small = rng.normal(0, 1, (size // 8, size // 8))
    tex = np.array(Image.fromarray(((small - small.min()) / (np.ptp(small) + 1e-6) * 255).astype(np.uint8))
                   .resize((size, size), Image.BICUBIC), dtype=np.float32) / 255.0
    grain = rng.normal(0, 1, (size, size)).astype(np.float32) * 0.35
    field = (tex - 0.5) * 2.0 + grain
    arr = np.array(pal, dtype=np.float32)
    out = np.zeros((size, size, 3), np.float32)
    for i in range(3):
        out[..., i] = arr[i] * (1 + field * strength)
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))


def make_sheet(spec: dict, cells: list[dict], size: int = 1024, sheet_index: int = 0, seed: int = 0) -> Image.Image:
    """程序化生成 1024² 白底九宫格（每格一个元素）。"""
    rng = random.Random(seed * 100 + sheet_index)
    pal = pick_palette(spec)
    S = size * SS
    img = Image.new("RGBA", (S, S), (255, 255, 255, 255))
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    cell = S // 3
    pad = int(cell * 0.06)
    for i, c in enumerate(cells[:9]):
        row, col = divmod(i, 3)
        box = (col * cell + pad, row * cell + pad, (col + 1) * cell - pad, (row + 1) * cell - pad)
        draw_element(layer, spec, c["element"], box, rng)
    img = Image.alpha_composite(img, layer)
    img = img.resize((size, size), Image.LANCZOS)
    # 纸纹只给元素区域加一点点（白底保持干净，方便去背）
    tex = _paper_texture(size, (250, 246, 240), strength=0.02).convert("RGBA")
    tex.putalpha(60)
    img = Image.alpha_composite(img, tex)
    return img


# ------------------------------------------------------------------ 背景

def make_background(spec: dict, size: int = 1024, variant: int = 0, seed: int = 0) -> Image.Image:
    """可平铺的程序化背景：所有绘制都做 9 次环绕（±size），保证无缝。"""
    rng = random.Random(seed * 31 + variant)
    pal = pick_palette(spec)
    S = size * SS
    base = mix(pal["paper"], pal["petal_light"], 0.10 + 0.08 * variant)
    img = Image.new("RGBA", (S, S), rgba(base, 255))
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer, "RGBA")

    n_motif = 26 + variant * 6
    for _ in range(n_motif):
        kind = rng.choice(["leaf", "leaf", "wash", "tiny", "ring"])
        x, y = rng.uniform(0, S), rng.uniform(0, S)
        r = S * rng.uniform(0.03, 0.09)
        a = rng.uniform(0, math.pi)
        # 环绕绘制
        for dx in (-S, 0, S):
            for dy in (-S, 0, S):
                px, py = x + dx, y + dy
                if not (-S * 0.3 < px < S * 1.3 and -S * 0.3 < py < S * 1.3):
                    continue
                if kind == "leaf":
                    col = pal["leaf_light"] if rng.random() < 0.5 else pal["leaf"]
                    d.polygon(_petal_poly(px, py, a, r * 1.9, r * 0.55, curve=0.18), fill=rgba(col, 52))
                elif kind == "wash":
                    col = pal["petal_light"] if rng.random() < 0.6 else pal["accent"]
                    d.ellipse([px - r * 1.6, py - r * 1.2, px + r * 1.6, py + r * 1.2], fill=rgba(col, 26))
                elif kind == "tiny":
                    d.ellipse([px - r * 0.28, py - r * 0.28, px + r * 0.28, py + r * 0.28],
                              fill=rgba(pal["accent"], 60))
                else:
                    d.ellipse([px - r, py - r, px + r, py + r], outline=rgba(pal["petal"], 34),
                              width=max(1, int(r * 0.14)))
    layer = layer.filter(ImageFilter.GaussianBlur(radius=SS * 2.2))
    img = Image.alpha_composite(img, layer)

    # 纸纹（周期无关，天然可平铺）
    tex = _paper_texture(size, (255, 255, 255), strength=0.05).convert("RGBA")
    tex.putalpha(36)
    img = img.resize((size, size), Image.LANCZOS)
    img = Image.alpha_composite(img, tex)
    return img


if __name__ == "__main__":
    import sys

    sys.path.insert(0, ".")
    from pattern_studio import prompts as P
    from pattern_studio import spec as S

    sp = S.analyze("国风水彩牡丹 疏朗")
    sheets = P.assign_cells(sp)
    make_sheet(sp, sheets[0], 1024).save("/tmp/mock_sheet.png")
    make_background(sp, 1024).save("/tmp/mock_bg.png")
    print("saved /tmp/mock_sheet.png /tmp/mock_bg.png")
