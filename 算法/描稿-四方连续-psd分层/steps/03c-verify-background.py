#!/usr/bin/env python3
"""第 3c 步（审查）：描稿底色干净度检查

描稿类图片的硬性要求：底色必须干净（平色、无明显阴影/渐变/杂色）。
做法：取低饱和高亮区域作为背景，统计其亮度离散度、饱和度均值、低频梯度。
用法: python steps/03c-verify-background.py --image 图.png [--max-std 3.5] [--max-sat 0.06]
退出码: 0=干净；2=底色不干净
"""
import sys, json, argparse
import numpy as np
from PIL import Image, ImageFilter

ap = argparse.ArgumentParser()
ap.add_argument('--image', required=True)
ap.add_argument('--max-std', type=float, default=3.5)
ap.add_argument('--max-sat', type=float, default=0.06)
ap.add_argument('--min-frac', type=float, default=0.15, help='背景至少占图比例')
ap.add_argument('--json', action='store_true')
a = ap.parse_args()

im = Image.open(a.image).convert('RGB')
g = np.asarray(im, dtype=np.float32) / 255.0
mx = g.max(axis=2); mn = g.min(axis=2); sat = np.where(mx > 1e-6, (mx - mn) / mx, 0)
val = mx
bg = (sat < 0.25) & (val > 0.55)
# 只统计真正平坦的背景像素（排除浅色花瓣/纹理）
gray0 = np.asarray(im.convert('L'), dtype=np.float32)
hpf = np.abs(gray0 - np.asarray(im.convert('L').filter(ImageFilter.GaussianBlur(2)), dtype=np.float32))
flat = bg & (hpf < 3.0)
if flat.mean() >= 0.10:
    bg = flat
frac = float(bg.mean())
if frac < a.min_frac:
    res = {'image': a.image, 'ok': False, 'reason': 'background-too-small', 'bg_frac': round(frac, 3)}
else:
    v = val[bg] * 255.0
    s = sat[bg]
    gray = np.asarray(im.convert('L'), dtype=np.float32)
    lp = np.asarray(im.convert('L').filter(ImageFilter.GaussianBlur(6)), dtype=np.float32)
    gx = np.abs(np.diff(lp, axis=1)).mean(); gy = np.abs(np.diff(lp, axis=0)).mean()
    std_v = float(v.std())
    mean_s = float(s.mean())
    dirty = (std_v > a.max_std) or (mean_s > a.max_sat) or (max(gx, gy) > 1.2)
    res = {'image': a.image, 'ok': not dirty, 'bg_frac': round(frac, 3),
           'bg_val_std': round(std_v, 2), 'bg_sat_mean': round(mean_s, 4),
           'bg_lowfreq_grad': round(float(max(gx, gy)), 2),
           'thresholds': {'max_std': a.max_std, 'max_sat': a.max_sat},
           'rule': '背景区亮度标准差 / 饱和度 / 低频梯度 任一超阈值 → 底色不干净'}
print(json.dumps(res, ensure_ascii=False, indent=2 if a.json else None))
sys.exit(0 if res['ok'] else 2)
