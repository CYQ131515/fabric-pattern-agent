#!/usr/bin/env python3
"""第 4 步（方案 B）：色层分色 PSD —— k-means 调色板 + 纯色层 + 形状蒙版
用法:
  python steps/04-layers-color.py src.png out.psd --height 9382 --k 8 [--width auto]
输出: RGB 8bit / 300PPI / Background + (K-1) 个色层
"""
import sys, json, time, argparse
import numpy as np
from PIL import Image
from psd_tools import PSDImage
from psd_tools.api.layers import PixelLayer
from psd_tools.constants import Resource, Compression
from psd_tools.psd.image_resources import ImageResource, ResoulutionInfo

ap = argparse.ArgumentParser()
ap.add_argument('src'); ap.add_argument('out')
ap.add_argument('--height', type=int, default=9382, help='输出高度 px（300dpi 下 9382≈79.4cm）')
ap.add_argument('--width', type=int, default=0, help='输出宽度，0=按原图比例')
ap.add_argument('--k', type=int, default=8)
ap.add_argument('--ppi', type=int, default=300)
ap.add_argument('--group-name', default='Colors', help='色层分组名')
ap.add_argument('--preserve-pixels', dest='preserve', action='store_true', default=True, help='每层保留原始像素（默认，合成=原图）')
ap.add_argument('--solid-colors', dest='preserve', action='store_false', help='每层用纯色调色板颜色（旧行为，会丢渐变）')
a = ap.parse_args()
t0 = time.time()

src = Image.open(a.src).convert('RGB')
if a.width:
    TW = a.width
else:
    TW = max(1, round(a.height * src.width / src.height))
TH = a.height
K = a.k

# 1) 小图 k-means 得调色板
sw = 1024; sh = max(1, round(1024 * src.height / src.width))
small = np.asarray(src.resize((sw, sh), Image.LANCZOS), dtype=np.float32).reshape(-1, 3)
rng = np.random.default_rng(7)
cent = small[rng.choice(len(small), size=K, replace=False)].copy()
for _ in range(14):
    d = ((small[:, None, :] - cent[None, :, :]) ** 2).sum(axis=2)
    lab = d.argmin(axis=1)
    for k in range(K):
        sel = lab == k
        if sel.any(): cent[k] = small[sel].mean(axis=0)
cent = np.clip(cent, 0, 255)

# 2) 全尺寸逐块分配最近色
big = np.asarray(src.resize((TW, TH), Image.LANCZOS), dtype=np.uint8)
H, W, _ = big.shape
labels = np.empty((H, W), dtype=np.uint8)
chunk = 512
for y0 in range(0, H, chunk):
    y1 = min(H, y0 + chunk)
    block = big[y0:y1].reshape(-1, 3).astype(np.int16)
    d = ((block[:, None, :].astype(np.int32) - cent[None, :, :].astype(np.int32)) ** 2).sum(axis=2)
    labels[y0:y1] = d.argmin(axis=1).reshape(y1 - y0, W).astype(np.uint8)
counts = np.bincount(labels.ravel(), minlength=K)

# 3) 建 PSD：出现最多的色做底层，其余为色层
psd = PSDImage.new('RGB', (W, H))
bg_k = int(counts.argmax())
bg = tuple(int(v) for v in cent[bg_k])
# 背景层：单独在根层（不放进分组）
def bbox_of(mask):
    ys, xs = np.nonzero(mask)
    if len(ys) == 0:
        return None
    y0, y1 = max(0, ys.min() - 2), min(H, ys.max() + 3)
    x0, x1 = max(0, xs.min() - 2), min(W, xs.max() + 3)
    return y0, y1, x0, x1

def make_layer(name, mask, color):
    bb = bbox_of(mask)
    if bb is None:
        return None
    y0, y1, x0, x1 = bb
    cm = (mask[y0:y1, x0:x1].astype(np.uint8) * 255)
    if a.preserve:
        rgb = np.where(mask[y0:y1, x0:x1][..., None], big[y0:y1, x0:x1], 0).astype(np.uint8)
    else:
        rgb = np.tile(np.array(color, dtype=np.uint8), (y1 - y0, x1 - x0, 1))
    layer = PixelLayer.frompil(Image.fromarray(rgb, 'RGB'), psd, name, top=y0, left=x0)
    layer.create_mask(Image.fromarray(cm, 'L'), top=y0, left=x0)
    return layer

bg_mask = labels == bg_k
psd.append(make_layer('Background', bg_mask, bg))
meta = [{'name': 'Background', 'hex': '#%02x%02x%02x' % bg, 'coverage': round(float(counts[bg_k] / labels.size) * 100, 2)}]
color_layers = []
n = 0
for k in [int(x) for x in np.argsort(-counts) if x != bg_k]:
    m = labels == k
    color = tuple(int(v) for v in cent[k])
    n += 1
    name = 'Color%02d #%02x%02x%02x' % (n, *color)
    layer = make_layer(name, m, color)
    if layer is not None:
        color_layers.append(layer)
    meta.append({'name': name, 'hex': '#%02x%02x%02x' % color, 'coverage': round(float(counts[k] / labels.size) * 100, 2)})
# 所有颜色分层放进一个分组；背景保持在外面
group = psd.create_group(color_layers, name=a.group_name)
if group.parent is None:
    psd.append(group)

ppi = a.ppi << 16
res = ResoulutionInfo(horizontal=ppi, horizontal_unit=1, width_unit=1, vertical=ppi, vertical_unit=1, height_unit=1)
psd.image_resources[Resource.RESOLUTION_INFO] = ImageResource(key=Resource.RESOLUTION_INFO, data=res)
psd.save(a.out, compression=Compression.RLE)
print(json.dumps({'out': a.out, 'size': [W, H], 'cm': [round(W / a.ppi * 2.54, 2), round(H / a.ppi * 2.54, 2)],
                  'seconds': round(time.time() - t0), 'layers': meta}, ensure_ascii=False))
