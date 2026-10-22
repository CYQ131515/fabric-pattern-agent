#!/usr/bin/env python3
"""可选：服务商同款两层 PSD（Background + Foreground 近白蒙版），用于兼容既有交付规范
用法: python steps/05-flat-psd.py src.png out.psd [--height 9382] [--width 0]
"""
import sys, argparse, json
import numpy as np
from PIL import Image
from psd_tools import PSDImage
from psd_tools.api.layers import PixelLayer
from psd_tools.constants import Resource, Compression
from psd_tools.psd.image_resources import ImageResource, ResoulutionInfo

ap = argparse.ArgumentParser()
ap.add_argument('src'); ap.add_argument('out')
ap.add_argument('--height', type=int, default=9382)
ap.add_argument('--width', type=int, default=0)
ap.add_argument('--ppi', type=int, default=300)
a = ap.parse_args()

src = Image.open(a.src).convert('RGB')
TW = a.width or max(1, round(a.height * src.width / src.height))
TH = a.height
img = src.resize((TW, TH), Image.LANCZOS)

psd = PSDImage.new('RGB', (TW, TH))
psd.append(PixelLayer.frompil(img, psd, 'Background'))
fg = PixelLayer.frompil(img, psd, 'Foreground')
mask = np.full((TH, TW), 255, dtype=np.uint8)
mask[5:-5, 5:-5] = 252  # 近白蒙版（与服务商一致的"几乎全白"）
fg.create_mask(Image.fromarray(mask, 'L'))
psd.append(fg)
ppi = a.ppi << 16
res = ResoulutionInfo(horizontal=ppi, horizontal_unit=1, width_unit=1, vertical=ppi, vertical_unit=1, height_unit=1)
psd.image_resources[Resource.RESOLUTION_INFO] = ImageResource(key=Resource.RESOLUTION_INFO, data=res)
psd.save(a.out, compression=Compression.RLE)
print(json.dumps({'out': a.out, 'size': [TW, TH]}))
