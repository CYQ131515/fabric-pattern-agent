#!/usr/bin/env python3
"""第 7 步：复刻 uniseven（元七）PSD 结构 —— 逆向结论的可执行版本。

逆向样本：`output.psd`（8096²，psd-tools 1.17.4 写入）。实测结论：
    层序（自下而上）：
      1「原始高清图」 visible=False，无蒙版，带 PROTECTED_SETTING（锁定）
      2「背景备选」   visible=False，蒙版 = 反相前景（mask 与前景逐像素互补，和=255），像素=原图
      3「背景1」      visible=True，无蒙版，像素 = 背景板（前景区域被背景扩散填平）
      4「整体前景」   visible=True，蒙版 = 前景软蒙版，像素 = 原图（与层 1/2 像素完全相同）
    合成公式：C = 背景板·(1-α) + 原图·α
    背景板：把前景区域用「只取背景像素的归一化高斯卷积」填平（σ≈32@1024 时与原板残差最小 ≈8.6），
            不是整图高斯模糊（残差 25+）、也不是普通 inpaint（9.1~11.4）。
    蒙版：覆盖 ≈61%，32.6% 全透明 / 54.3% 全不透明 / 12.8% 过渡；过渡带 ≈14px@8096（≈1.8px@1024）
          → 蒙版像是在 ~1K 分辨率算好后放大 + 轻羽化；边界与图像边缘相关（边界处梯度中位数 18.7 > 全图 12.3）。

用法:
  python steps/07-psd-uniseven-clone.py src.png out.psd --height 8096 [--matte matte.png]
输出: RGB 8bit / 300PPI，层名/层序/显隐/蒙版语义与 uniseven 样本一致
"""
import sys, json, time, struct, argparse
import numpy as np
from PIL import Image, ImageFilter
from psd_tools import PSDImage
from psd_tools.api.layers import PixelLayer
from psd_tools.constants import Resource, Compression, Tag
from psd_tools.psd.image_resources import ImageResource, ResoulutionInfo
from psd_tools.psd.tagged_blocks import TaggedBlock

ap = argparse.ArgumentParser()
ap.add_argument('src'); ap.add_argument('out')
ap.add_argument('--height', type=int, default=8096, help='输出高度（他们样本 8096 @300dpi≈68.55cm）')
ap.add_argument('--width', type=int, default=0)
ap.add_argument('--ppi', type=int, default=300)
ap.add_argument('--matte', default='', help='外部前景蒙版 PNG（不传则用区域生长现算）')
ap.add_argument('--sigma', type=float, default=32.0, help='背景板归一化卷积 σ（1024 尺度）')
ap.add_argument('--feather', type=float, default=1.2)
a = ap.parse_args()
t0 = time.time()

src = Image.open(a.src).convert('RGB')
TW = a.width or max(1, round(a.height * src.width / src.height))
TH = a.height
S = 1024
small = src.resize((S, S), Image.LANCZOS)
a_small = np.asarray(small, dtype=np.float32)


# ---------- 前景蒙版 ----------
def flood_mask():
    img = a_small
    gray = img.mean(2)
    border = np.concatenate([img[:8].reshape(-1, 3), img[-8:].reshape(-1, 3),
                             img[:, :8].reshape(-1, 3), img[:, -8:].reshape(-1, 3)])
    bgc = np.median(border, axis=0)
    dist = np.sqrt(((img - bgc) ** 2).sum(2))
    gx = np.abs(np.diff(gray, axis=1, prepend=gray[:, :1]))
    gy = np.abs(np.diff(gray, axis=0, prepend=gray[:1, :]))
    grad = gx + gy

    def dil(m, it=1):
        for _ in range(it):
            m = m | np.roll(m, 1, 0) | np.roll(m, -1, 0) | np.roll(m, 1, 1) | np.roll(m, -1, 1)
        return m

    from collections import deque
    passable = (dist < 55) & (grad < np.percentile(grad, 65))
    seed = np.zeros_like(passable)
    seed[0, :] = passable[0, :]; seed[-1, :] = passable[-1, :]
    seed[:, 0] = passable[:, 0]; seed[:, -1] = passable[:, -1]
    seen = seed.copy()
    dq = deque(map(tuple, np.argwhere(seed)))
    while dq:
        y, x = dq.popleft()
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ny, nx = y + dy, x + dx
            if 0 <= ny < S and 0 <= nx < S and passable[ny, nx] and not seen[ny, nx]:
                seen[ny, nx] = True; dq.append((ny, nx))
    fg = ~seen
    return dil(fg, 1)


if a.matte:
    m_img = Image.open(a.matte).convert('L').resize((S, S), Image.LANCZOS)
    fg = np.asarray(m_img) > 128
    alpha = np.asarray(m_img, dtype=np.float32) / 255.0
else:
    fg = flood_mask()
    alpha = fg.astype(np.float32)
    alpha_img = Image.fromarray((alpha * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(a.feather))
    alpha = np.asarray(alpha_img, dtype=np.float32) / 255.0

mask_img = Image.fromarray((alpha * 255).astype(np.uint8))

# ---------- 背景板：背景像素的归一化高斯卷积 ----------
def blur(arr, sig):
    return np.asarray(Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
                      .filter(ImageFilter.GaussianBlur(sig)), dtype=np.float32)


w = 1.0 - alpha
num = np.stack([blur(a_small[..., c] * w, a.sigma) for c in range(3)], 2)
den = blur(w * 255.0, a.sigma) / 255.0
plate = (num / np.maximum(den, 1e-3)[..., None]).clip(0, 255)
plate_img = Image.fromarray(plate.astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.0))

# ---------- 全尺寸 ----------
big = np.asarray(src.resize((TW, TH), Image.LANCZOS), dtype=np.uint8)
alpha_full = mask_img.resize((TW, TH), Image.LANCZOS)
inv_full = Image.fromarray(255 - np.asarray(alpha_full, dtype=np.uint8))
plate_full = np.asarray(plate_img.resize((TW, TH), Image.LANCZOS), dtype=np.uint8)

psd = PSDImage.new('RGB', (TW, TH))


def luni_bytes(s):
    return struct.pack('>I', len(s)) + s.encode('utf-16-be')


def add_layer(name_ascii, unicode_name, rgb, mask=None, visible=True, protected=False):
    layer = PixelLayer.frompil(Image.fromarray(rgb, 'RGB'), psd, name_ascii)
    if mask is not None:
        layer.create_mask(mask, top=0, left=0)
    layer.visible = visible
    rec = layer._record
    rec.tagged_blocks[Tag.UNICODE_LAYER_NAME] = TaggedBlock(
        key=Tag.UNICODE_LAYER_NAME, data=luni_bytes(unicode_name))
    if protected:
        rec.tagged_blocks[Tag.PROTECTED_SETTING] = TaggedBlock(
            key=Tag.PROTECTED_SETTING, data=struct.pack('>I', 2147483648))
    psd.append(layer)
    return layer


add_layer('OriginalHD', '原始高清图', big, visible=False, protected=True)
add_layer('BgAlt', '背景备选', big, mask=inv_full, visible=False)
add_layer('Bg1', '背景1', plate_full, visible=True)
add_layer('Foreground', '整体前景', big, mask=alpha_full, visible=True)

ppi = a.ppi << 16
res = ResoulutionInfo(horizontal=ppi, horizontal_unit=1, width_unit=1,
                      vertical=ppi, vertical_unit=1, height_unit=1)
psd.image_resources[Resource.RESOLUTION_INFO] = ImageResource(
    key=Resource.RESOLUTION_INFO, data=res)
psd.save(a.out, compression=Compression.RLE)

af = np.asarray(alpha_full, dtype=np.uint8)
print(json.dumps({
    'ok': True, 'out': a.out, 'size': [TW, TH],
    'cm': [round(TW / a.ppi * 2.54, 2), round(TH / a.ppi * 2.54, 2)],
    'layers': [str(l.name) for l in psd],
    'coverage': round(float((af > 128).mean()), 4),
    'soft_fraction': round(float(((af > 8) & (af < 247)).mean()), 4),
    'sigma': a.sigma, 'seconds': round(time.time() - t0),
}, ensure_ascii=True))
