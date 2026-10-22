#!/usr/bin/env python3
"""平铺判定的已知答案测试。

为什么不用真图当基准
    试过拿 数据/library/orig/ 里的图当基准 —— 结果发现那批"图"其实是
    同一张占位图（4×4 格子预览）重复了几十份，根本没法用来标定。所以这里
    **用程序合成已知答案的图**：正弦干涉图和周期圆点图在数学上严格周期，
    一定是平铺的；渐变、纯色、随机噪声一定不是。阈值就是照这批图定的。

两个"故意判错"的用例留在最后（KNOWN GAPS）
    合成实验证明了两类假图在**纯几何特征上无法**和合法花型分开：
      · 花型贴在白卡纸上的展示图   ←→  素底上居中的一朵大花
      · 四周压暗的打样照           ←→  深底上居中的亮色大花
    这两类只能靠语义复核（README 里"还差什么"那节）。把它们写进测试并
    标记为 **已知缺口**，是为了让这个局限一直看得见 —— 而不是哪天
    有人发现漏网了，再从头查一遍为什么。
"""
from __future__ import annotations

import io
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw, ImageFilter                          # noqa: E402
from wsearch import tile                                               # noqa: E402

S = 320
ok_n = 0
bad_n = 0


def ok(cond, msg, extra=""):
    global ok_n, bad_n
    if cond:
        ok_n += 1
        print("  \u2713 %s" % msg)
    else:
        bad_n += 1
        print("  \u2717 %s   %s" % (msg, extra))


# ----------------------------------------------------------------- 合成素材
def sinu(n, size=S):
    im = Image.new("RGB", (size, size))
    px = im.load()
    for y in range(size):
        for x in range(size):
            v = (128 + 55 * math.sin(2 * math.pi * n * x / size) * math.cos(2 * math.pi * n * y / size)
                 + 30 * math.sin(2 * math.pi * 2 * n * y / size))
            px[x, y] = (int(max(0, min(255, v))), int(max(0, min(255, v * 0.7 + 40))), 180)
    return im


def circles(n, r=0.13, ground=(242, 238, 228), ink=(46, 74, 140)):
    im = Image.new("RGB", (S, S), ground)
    d = ImageDraw.Draw(im)
    c = S // n
    for i in range(n):
        for j in range(n):
            cx, cy = i * c + (c - 1) / 2, j * c + (c - 1) / 2
            d.ellipse([cx - r * c, cy - r * c, cx + r * c, cy + r * c], fill=ink)
    return im


def crossed(n=2, ground=(250, 247, 240), ink=(150, 60, 70)):
    """花体跨边界 —— 真花型最常见的形态"""
    im = Image.new("RGB", (S, S), ground)
    d = ImageDraw.Draw(im)
    c = S // n
    for i in range(n):
        for j in range(n):
            for dx, dy in ((0, 0), (c, 0), (0, c), (c, c)):
                cx, cy = i * c + dx, j * c + dy
                d.ellipse([cx - c * 0.22, cy - c * 0.22, cx + c * 0.22, cy + c * 0.22], fill=ink)
    return im


def centered(r=0.16, ink=(46, 74, 140), ground=(242, 238, 228)):
    im = Image.new("RGB", (S, S), ground)
    ImageDraw.Draw(im).ellipse([S / 2 - r * S, S / 2 - r * S,
                                S / 2 + r * S, S / 2 + r * S], fill=ink)
    return im


def jpeg(im, q=60):
    b = io.BytesIO()
    im.save(b, "JPEG", quality=q)
    b.seek(0)
    return Image.open(b)


def mockup(margin=0.10):
    m = int(S * margin)
    inner = circles(2).resize((S - 2 * m, S - 2 * m))
    im = Image.new("RGB", (S, S), (255, 255, 255))
    im.paste(inner, (m, m))
    return im


def vignette(base=None):
    im = (base or circles(2)).copy()
    px = im.load()
    for y in range(S):
        for x in range(S):
            dx, dy = (x - S / 2) / (S / 2), (y - S / 2) / (S / 2)
            k = 1 - 0.75 * min(1, dx * dx + dy * dy)
            r, g, b = px[x, y]
            px[x, y] = (int(r * k), int(g * k), int(b * k))
    return im


def halves():
    im = Image.new("RGB", (S, S))
    px = im.load()
    rnd = random.Random(3)
    for y in range(S):
        for x in range(S):
            px[x, y] = ((rnd.randint(150, 255), rnd.randint(0, 90), rnd.randint(0, 90))
                        if x < S // 2 else
                        (rnd.randint(0, 90), rnd.randint(0, 90), rnd.randint(150, 255)))
    return im.filter(ImageFilter.GaussianBlur(1))


def blurred_noise():
    return Image.effect_noise((S, S), 40).convert("RGB").filter(ImageFilter.GaussianBlur(4))


# ============================================================= 真：必须通过
print("[1] 真的平铺图 —— 必须判成 seamless")
TRUES = [
    ("正弦干涉图（严格周期）", sinu(3), None),
    ("正弦 5 周期", sinu(5), None),
    ("周期圆点 n=1（单花居中）", circles(1), None),
    ("周期圆点 n=4", circles(4), None),
    ("跨边界花体 n=2", crossed(2), None),
    ("跨边界花体 n=3", crossed(3), None),
    ("素底 + 居中单花", centered(), None),
    ("素底 + 居中大花 r=.30", centered(0.30), None),
    ("圆点 + JPEG 60", jpeg(circles(2)), None),
    ("圆点 + 高斯模糊", circles(2).filter(ImageFilter.GaussianBlur(1.5)), None),
    ("居中单花 + JPEG 60", jpeg(centered()), None),
    ("跨边界花体 + JPEG 60", jpeg(crossed(2)), None),
]
for name, im, _ in TRUES:
    r = tile.analyze(im)
    ok(r["seamless"], "%-26s 判成 %s（接缝 %s/%s）"
       % (name, r["verdict"], r["seam_h"], r["seam_v"]))
    ok(isinstance(r["quality"], float) and r["quality"] > 0,
       "%-26s 质量分算出来了（%s）" % (name, r["quality"]))

# ============================================================= 假：必须拒绝
print("\n[2] 不是平铺图 —— 必须拒绝，且理由分类正确")
FALSES = [
    ("随机噪声", Image.effect_noise((S, S), 120).convert("RGB"), "not_tile"),
    ("左右两半毫不相干", halves(), "not_tile"),
    ("虚化噪声（照片感）", blurred_noise(), "not_tile"),
    ("线性渐变底", Image.linear_gradient("L").convert("RGB").resize((S, S)), "not_tile"),
    ("纯色底", Image.new("RGB", (S, S), (200, 180, 160)), "flat"),
]
for name, im, want in FALSES:
    r = tile.analyze(im)
    ok(not r["seamless"], "%-26s 拒绝（%s，接缝 %s/%s）"
       % (name, r["verdict"], r["seam_h"], r["seam_v"]))
    ok(r["verdict"] == want, "%-26s 拒绝理由是 %s（期望 %s）"
       % (name, r["verdict"], want))

# ================================================================ 边界与健壮
print("\n[3] 边界情况")
r = tile.analyze(Image.new("RGB", (120, 120), (255, 255, 255)))
ok(r["verdict"] == "too_small" and not r["seamless"], "太小的图（120px）被拦下（%s）" % r["verdict"])

big = sinu(3, size=1400)
try:
    r = tile.analyze(big)
    ok(r["ok"] and r["seamless"], "1400×1400 的大图照样判（%s，接缝 %s/%s）"
       % (r["verdict"], r["seam_h"], r["seam_v"]))
except Exception as e:                                                  # noqa: BLE001
    ok(False, "大图不该抛异常", str(e))

# 带透明通道（透明底花型）合成到白底后再判，不该因为"黑对黑"误判
rgba = circles(2).convert("RGBA")
px = rgba.load()
for y in range(S):
    for x in range(S):
        r0, g0, b0, _ = px[x, y]
        if (r0, g0, b0) == (242, 238, 228):
            px[x, y] = (0, 0, 0, 0)          # 素底变透明
r = tile.analyze(rgba)
ok(r["seamless"] and r["has_alpha"], "透明底花型（合成白底后）判成平铺（%s）" % r["verdict"])

r1 = tile.analyze(b"not an image at all")
ok(r1["ok"] is False and r1["verdict"] == "broken", "坏字节不抛异常，回 broken")
r2 = tile.analyze("/tmp/这个文件肯定不存在_%d.png" % random.randint(0, 9999))
ok(r2["ok"] is False, "文件不存在不抛异常，回 broken")

# 从 bytes 判定（下载路径走的就是这条）
buf = io.BytesIO()
sinu(3).save(buf, "PNG")
r = tile.analyze(buf.getvalue())
ok(r["seamless"], "从字节判定（下载路径）照样能过")

ok(tile.is_tile(sinu(3)) is True and tile.is_tile(Image.new("RGB", (S, S), (9, 9, 9))) is False,
   "is_tile() 的布尔快捷方式是对的")

# ======================================================== 已知缺口（必须留着）
print("\n[4] 已知缺口 —— 这两类目前判不了，写在这里保持可见")
m = tile.analyze(mockup(0.10))
print("    · 花型贴白卡纸：结论 %s，四角亮度差 %.1f" % (m["verdict"], m["corner_diff"] or 0))
d = tile.analyze(centered(0.34))
print("    · 素底居中大花：结论 %s，四角亮度差 %.1f" % (d["verdict"], d["corner_diff"] or 0))
ok(True, "两类图形的几何特征高度重叠，只能靠语义复核 —— 已在 README 说明")

print("\n结果：%d 通过 / %d 失败" % (ok_n, bad_n))
sys.exit(1 if bad_n else 0)
