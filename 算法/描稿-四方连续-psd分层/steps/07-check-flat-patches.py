#!/usr/bin/env python3
"""第 7 步（交付前检查）：矩形补丁 / 色块瑕疵检测

背景：修复带（inpaint 区域、quilt 边缘、donor 贴回区域）在合成图上可能表现为
"一块矩形区域的亮度/颗粒与周围不一致"——用户肉眼可见，但直线/接缝检测器抓不到。
本检查在合成图上做块级统计分析，找出"矩形异常补丁"。

用法: python steps/07-check-flat-patches.py --psd 文件.psd [--json]
      也可 --image 图.png（直接查合成图/无缝图）
退出码: 0=通过；2=检测到矩形补丁/色差块
"""
import sys, json, argparse, re
import numpy as np
from PIL import Image, ImageFilter
from scipy import ndimage as ndi

ap = argparse.ArgumentParser()
ap.add_argument('--psd'); ap.add_argument('--image')
ap.add_argument('--json', action='store_true')
ap.add_argument('--block', type=int, default=32)
ap.add_argument('--z', type=float, default=4.0)
a = ap.parse_args()

if a.psd:
    from psd_tools import PSDImage
    im = PSDImage.open(a.psd).composite().convert('L')
    tag = a.psd
elif a.image:
    im = Image.open(a.image).convert('L'); tag = a.image
else:
    print('缺少 --psd 或 --image'); sys.exit(1)

g = np.asarray(im, dtype=np.float32)
H, W = g.shape
B = a.block
hp = g - np.asarray(Image.fromarray(g.astype(np.uint8)).filter(ImageFilter.GaussianBlur(2)), dtype=np.float32)
lp = np.asarray(Image.fromarray(g.astype(np.uint8)).filter(ImageFilter.GaussianBlur(10)), dtype=np.float32)

gh, gw = H // B, W // B
mean_lp = np.zeros((gh, gw), dtype=np.float32)
std_hp = np.zeros((gh, gw), dtype=np.float32)
for i in range(gh):
    for j in range(gw):
        blk_lp = lp[i*B:(i+1)*B, j*B:(j+1)*B]
        blk_hp = hp[i*B:(i+1)*B, j*B:(j+1)*B]
        mean_lp[i, j] = blk_lp.mean()
        std_hp[i, j] = blk_hp.std()

def anomaly(z_map, thr):
    return np.abs(z_map) >= thr

def zscore(x):
    med = ndi.median_filter(x, size=9, mode='reflect')
    res = x - med
    mad = np.median(np.abs(x - np.median(x))) + 1e-6
    return res / (mad * 1.4826)

z_lp = zscore(mean_lp); z_hp = zscore(std_hp)
mask = anomaly(z_lp, a.z) | anomaly(z_hp, a.z)
mask = ndi.binary_closing(mask, np.ones((3, 3)))
lab, n = ndi.label(mask)
patches = []
for k in range(1, n + 1):
    ys, xs = np.nonzero(lab == k)
    if len(ys) == 0: continue
    y0, y1, x0, x1 = ys.min(), ys.max(), xs.min(), xs.max()
    bw, bh = (x1 - x0 + 1) * B, (y1 - y0 + 1) * B
    if bw < W * 0.05 or bh < H * 0.05: continue
    fill = len(ys) / max(1, (x1 - x0 + 1) * (y1 - y0 + 1))
    if fill < 0.45: continue
    if bw * bh < W * H * 0.01: continue
    patches.append({
        'bbox_px': [int(x0*B), int(y0*B), int(bw), int(bh)],
        'bbox_norm': [round(x0/gw, 3), round(y0/gh, 3), round(bw/W, 3), round(bh/H, 3)],
        'fill': round(float(fill), 2),
        'lp_z': round(float(np.max(np.abs(z_lp[ys, xs]))), 1),
        'hp_z': round(float(np.max(np.abs(z_hp[ys, xs]))), 1),
        'mean_lp_inside': round(float(mean_lp[ys, xs].mean()), 1),
        'std_hp_inside': round(float(std_hp[ys, xs].mean()), 1),
    })
patches.sort(key=lambda p: -(p['bbox_px'][2] * p['bbox_px'][3]))
ok = len(patches) == 0
res = {'target': tag, 'size': [W, H], 'ok': ok, 'block': B, 'z_threshold': a.z,
       'rect_patches': patches[:8],
       'rule': '块级低频/高频统计离群且成矩形（面积≥1%、边长≥5%、填充率≥0.45）→ 矩形补丁瑕疵'}
print(json.dumps(res, ensure_ascii=False, indent=2 if a.json else None))
sys.exit(0 if ok else 2)
