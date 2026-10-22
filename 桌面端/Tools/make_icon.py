#!/usr/bin/env python3
"""由 Resources/icon_source.jpg 生成 macOS 图标与网页品牌小图。

源图是粉色五瓣花（AI 生成）；这里统一做圆角透明 + 多尺寸 icns。
没有源图时会报错退出，避免不小心把程序化旧图标又装回去。
"""
import os
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.normpath(os.path.join(HERE, "..", "Resources"))
SRC = os.path.join(RES, "icon_source.jpg")
S = 1024

if not os.path.exists(SRC):
    raise SystemExit(f"缺少源图：{SRC}")

img = Image.open(SRC).convert("RGBA")
# 居中裁成正方形，避免源图比例不一致
w, h = img.size
side = min(w, h)
img = img.crop(((w - side) // 2, (h - side) // 2, (w - side) // 2 + side, (h - side) // 2 + side))
img = img.resize((S, S), Image.LANCZOS)

# macOS 图标圆角（约 22%）
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.222), fill=255)
img.putalpha(mask)
img.save(os.path.join(RES, "icon_1024.png"), "PNG")

# 生成 iconset
iconset = os.path.join(RES, "AppIcon.iconset")
os.system(f'rm -rf "{iconset}" && mkdir -p "{iconset}"')
specs = [(16, ""), (32, ""), (32, "@2x"), (64, "@2x"), (128, ""), (256, ""),
         (256, "@2x"), (512, ""), (512, "@2x"), (1024, "@2x")]
for size, suffix in specs:
    img.resize((size, size), Image.LANCZOS).save(
        os.path.join(iconset, f"icon_{size}x{size}{suffix}.png"))
print("iconset ready:", iconset)

# 网页品牌小图（侧栏 32px / favicon 64px）
img.resize((96, 96), Image.LANCZOS).save(os.path.join(RES, "brand-96.png"), "PNG")
img.resize((64, 64), Image.LANCZOS).save(os.path.join(RES, "brand-64.png"), "PNG")
print("brand png ready")
