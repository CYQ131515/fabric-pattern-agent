#!/usr/bin/env python3
"""第 6 步（底色净化）：把描稿的底色刷干净（平色化）

做法：
1) 低饱和高亮像素 → 背景候选；
2) 只保留面积≥2% 的连通背景块（避免误伤浅色花瓣）；
3) 取背景中"局部方差最小 60%"像素的中位色作为干净底色；
4) 羽化替换，最多 iter 轮，直到背景亮度标准差 < 2。
用法: python steps/06-clean-background.py --image 图.png --out fixed.png [--iters 3]
"""
import numpy as np, argparse, json
from PIL import Image
from scipy import ndimage as ndi

ap = argparse.ArgumentParser()
ap.add_argument('--image', required=True); ap.add_argument('--out', required=True)
ap.add_argument('--iters', type=int, default=3)
ap.add_argument('--sat', type=float, default=0.25)
ap.add_argument('--val', type=float, default=0.55)
ap.add_argument('--feather', type=int, default=3)
a = ap.parse_args()

arr = np.asarray(Image.open(a.image).convert('RGB'), dtype=np.float32)
H, W, _ = arr.shape
cur = arr.copy()

def bg_mask(x):
    g = x / 255.0
    mx = g.max(axis=2); mn = g.min(axis=2)
    sat = (mx - mn) / np.maximum(mx, 1e-6); val = mx
    m = (sat < a.sat) & (val > a.val)
    m = ndi.binary_opening(m, np.ones((3, 3)))
    m = ndi.binary_closing(m, np.ones((5, 5)))
    lab, n = ndi.label(m)
    out = np.zeros_like(m)
    for k in range(1, n + 1):
        comp = (lab == k)
        if comp.mean() < 0.02: continue
        # 含较多高饱和像素 → 是浅色花瓣/花芯，不能当背景刷
        if float((sat[comp] > 0.4).mean()) > 0.01: continue
        out |= comp
    return out

used = 0
for it in range(a.iters):
    used = it + 1
    bg = bg_mask(cur)
    if bg.mean() < 0.05: break
    loc = ndi.uniform_filter(cur, size=(11, 11, 1)).mean(axis=2)
    thr = np.percentile(loc[bg], 60)
    clean = cur[bg & (loc <= thr)]
    target = np.median(clean, axis=0)
    soft = ndi.binary_erosion(bg, np.ones((3, 3)))
    alpha = np.clip(ndi.gaussian_filter(soft.astype(np.float32), sigma=a.feather) * 1.8, 0, 1)[..., None]
    cur = np.clip(cur * (1 - alpha) + target[None, None, :] * alpha, 0, 255)
    v = cur[bg].mean(axis=1)
    if float(v.std()) < 2.0: break

out = cur.astype(np.uint8)
Image.fromarray(out).save(a.out)
bg = bg_mask(cur)
g = cur / 255.0
print(json.dumps({'step': '06-clean-background', 'image': a.image, 'out': a.out,
                  'iters_used': used, 'bg_frac': round(float(bg.mean()), 3),
                  'bg_val_std': round(float((g.max(axis=2)[bg] * 255).std()), 2)}, ensure_ascii=False))
