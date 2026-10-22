#!/usr/bin/env python3
"""02-seam-ai 的修补带后处理（wrap 强制还原 + AI 贡献收敛 + 带均值匹配）。

背景（2026-10-20，密集大花蓝罂粟实测）：
    02-seam-ai 的修法是把接缝移到中心十字再让模型重画。它有两个残留：
      ① 竖带是全高的，模型重画的竖带在图像上/下边缘不自我衔接 → 平铺后
         tile 边界出现「对不上的缝」（wrapY 1.51，肉眼就是两条断缝）；
      ② 带内内容被重画后，整体明度与环带有 ~4 个灰阶的差 → 03b 判修复带可见。
    本步只做「不改变图案结构」的收尾，三步：
      A. AI 贡献系数：out = off + k*(ai - off)。**默认 k=1**（完全用 AI）。
         实测 k<1 会把 off 里原来那道接缝按比例漏回来，中心出现硬线；
         保留参数只是为了实验。
      B. wrap 强制还原：图像四边 M px 内用平滑 ramp 强制回到 off（平移后的图），
         保证 tile 外缘逐像素连续——这是 L19「蒙版外强制还原」在带法上的等价物
      C. 带均值匹配：按 03b 的口径（每轴 band 宽 180、环带 40、低频 σ=3）
         迭代把带内均值拉到环带均值，消除可见的亮度台阶
输入：base（清洗后的底图，1024）、ai（02-seam-ai 的输出）
输出：out + JSON 指标
"""
import argparse
import json
import sys

import numpy as np
from PIL import Image, ImageFilter

ap = argparse.ArgumentParser()
ap.add_argument('--base', required=True)
ap.add_argument('--ai', required=True)
ap.add_argument('--out', required=True)
ap.add_argument('--k', type=float, default=1.0, help='AI 贡献系数（1=完全用 AI；<1 会把原图接缝漏回来，默认不混）')
ap.add_argument('--border', type=int, default=16, help='外缘强制还原宽度 px（wrap 硬保证）')
ap.add_argument('--band', type=int, default=180, help='03b 口径的修复带宽度 px')
ap.add_argument('--ring', type=int, default=40, help='03b 口径的环带宽度 px')
ap.add_argument('--rounds', type=int, default=5, help='带均值匹配迭代次数')
ap.add_argument('--gain', type=float, default=1.0)
ap.add_argument('--ramp', type=int, default=20, help='带边平滑宽度（防止新硬边）')
a = ap.parse_args()


def load(p):
    return np.asarray(Image.open(p).convert('RGB'), dtype=np.float32)


base = load(a.base)
ai = load(a.ai)
if base.shape != ai.shape:
    raise SystemExit('base 与 ai 尺寸不一致：%s vs %s' % (base.shape, ai.shape))
H, W, _ = base.shape
# 平移一半后的底图：02-seam-ai 的合成基准就是它（见 L11）
off = np.roll(base, shift=(-(H // 2), -(W // 2)), axis=(0, 1))
yy, xx = np.mgrid[0:H, 0:W]

# ---- A. AI 贡献收敛 ----
cur = off + a.k * (ai - off)

# ---- B. wrap 强制还原（外缘 M px 平滑回到 off）----
d = np.minimum.reduce([xx, yy, W - 1 - xx, H - 1 - yy]).astype(np.float32)
t = np.clip(1.0 - d / max(1, a.border), 0.0, 1.0)
t = t * t * (3 - 2 * t)
cur = cur * (1 - t[..., None]) + off * t[..., None]

# ---- C. 带均值匹配（与 03b 同口径）----
HW = a.band // 2
y0, y1 = max(0, H // 2 - HW), min(H, H // 2 + HW)
x0, x1 = max(0, W // 2 - HW), min(W, W // 2 + HW)


def lp_gray(img):
    g = np.clip(img.mean(2), 0, 255).astype(np.uint8)
    return np.asarray(Image.fromarray(g).filter(ImageFilter.GaussianBlur(3)), dtype=np.float32)


wh = np.clip((HW - np.abs(yy - H // 2)) / max(1, a.ramp), 0.0, 1.0)
wh = wh * wh * (3 - 2 * wh)
wv = np.clip((HW - np.abs(xx - W // 2)) / max(1, a.ramp), 0.0, 1.0)
wv = wv * wv * (3 - 2 * wv)

dh = dv = 0.0
for _ in range(max(0, a.rounds)):
    lp = lp_gray(cur)
    inner_h = lp[y0:y1, :]
    out_h = np.concatenate([lp[max(0, y0 - a.ring):y0, :],
                            lp[y1:min(H, y1 + a.ring), :]], axis=0)
    dh = float(out_h.mean() - inner_h.mean())
    inner_v = lp[:, x0:x1]
    out_v = np.concatenate([lp[:, max(0, x0 - a.ring):x0],
                            lp[:, x1:min(W, x1 + a.ring)]], axis=1)
    dv = float(out_v.mean() - inner_v.mean())
    cur = cur + (wh * dh + wv * dv)[..., None] * a.gain
    cur = np.clip(cur, 0, 255)

# 收尾：外缘再强制一次（C 步不应溢出到边，但保险）
cur = cur * (1 - t[..., None]) + off * t[..., None]
cur = np.clip(cur, 0, 255).astype(np.uint8)
Image.fromarray(cur).save(a.out)

g = cur.astype(np.float32).mean(2)
wx = float(np.abs(g[:, 0] - g[:, -1]).mean())
wy = float(np.abs(g[0, :] - g[-1, :]).mean())
ax = float(np.abs(np.diff(g, axis=1)).mean())
ay = float(np.abs(np.diff(g, axis=0)).mean())
print(json.dumps({'out': a.out, 'size': [W, H], 'k': a.k, 'border': a.border,
                  'band': a.band, 'rounds': a.rounds,
                  'wrapRatioX': round(wx / max(ax, 1e-6), 2),
                  'wrapRatioY': round(wy / max(ay, 1e-6), 2),
                  'residual_dh': round(dh, 2), 'residual_dv': round(dv, 2)},
                 ensure_ascii=False))
