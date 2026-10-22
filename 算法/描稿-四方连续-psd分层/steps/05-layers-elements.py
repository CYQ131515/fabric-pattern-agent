#!/usr/bin/env python3
"""第 5 步（元素分层 PSD）：前景/背景（对象级）分层，对标 uniseven 的 PSD。

对标依据（2026-10-21 对照分析，样本 output.psd）：
    - 他们的分层不是按颜色，而是「全部图案元素（花/叶/枝）一张软蒙版」+「重绘的干净底布」；
    - 前景层保留原图像素（mask 内与原图 diff=0），蒙版有 ~20% 羽化 → 抠得完整、无毛边；
    - 另有隐藏的「原始高清图」与「反向蒙版背景备选」供切换。

我们旧色层 PSD（04-layers-color.py）适合印刷分色，但关掉背景后色层并集会有
量化噪声碎点/硬边。这个脚本用「边界泛洪 + 梯度/色距」得到整组图案元素的蒙版：
    内部 alpha=1，边缘一圈用色距反解软过渡 → 无毛边、无碎块、无暗边；
    背景用背景像素扩散补一张干净底布。

用法:
  python steps/05-layers-elements.py src.png out.psd --height 9382
输出: RGB 8bit / 300PPI
"""
import sys, json, time, argparse
from collections import deque
import numpy as np
from PIL import Image, ImageFilter
from psd_tools import PSDImage
from psd_tools.api.layers import PixelLayer
from psd_tools.constants import Resource, Compression
from psd_tools.psd.image_resources import ImageResource, ResoulutionInfo

ap = argparse.ArgumentParser()
ap.add_argument('src'); ap.add_argument('out')
ap.add_argument('--height', type=int, default=9382)
ap.add_argument('--width', type=int, default=0)
ap.add_argument('--ppi', type=int, default=300)
ap.add_argument('--k', type=int, default=8, help='--keep-colors 时的调色板色数')
ap.add_argument('--fg-dist', type=float, default=55.0, help='背景色距阈值（越大前景越多）')
ap.add_argument('--grad-pct', type=float, default=65.0, help='低梯度分位（越大背景越容易扩散）')
ap.add_argument('--dilate', type=int, default=2, help='前景蒙版初始外扩像素（1024 尺度）')
ap.add_argument('--open', type=int, default=2, help='形态学开运算半径（1024 尺度），去掉背景织纹碎块')
ap.add_argument('--min-blob', type=float, default=1.0e-3, help='开运算后保留的连通域面积阈值（占比）')
ap.add_argument('--min-area', type=float, default=2.5e-4, help='碎块面积阈值（占比）')
ap.add_argument('--feather', type=float, default=1.2, help='边缘羽化（1024 尺度）')
ap.add_argument('--keep-colors', action='store_true', help='同时放隐藏的 Colors 分组（色层），便于切换')
ap.add_argument('--fg-mask', default='', help='外部前景蒙版 PNG（如 SAM3 工作流输出）；给了就用它代替区域生长')
a = ap.parse_args()
t0 = time.time()

src = Image.open(a.src).convert('RGB')
TW = a.width or max(1, round(a.height * src.width / src.height))
TH = a.height
S = 1024

small_img = src.resize((S, S), Image.LANCZOS)
a_small = np.asarray(small_img, dtype=np.float32)

# ---------- 1. 背景色估计 + 色距/梯度 ----------
border = np.concatenate([a_small[:8].reshape(-1, 3), a_small[-8:].reshape(-1, 3),
                         a_small[:, :8].reshape(-1, 3), a_small[:, -8:].reshape(-1, 3)])
bgc = np.median(border, axis=0)
dist = np.sqrt(((a_small - bgc) ** 2).sum(2))
gray = a_small.mean(2)
gx = np.abs(np.diff(gray, axis=1, prepend=gray[:, :1]))
gy = np.abs(np.diff(gray, axis=0, prepend=gray[:1, :]))
grad = gx + gy

def dilate(m, it=1):
    for _ in range(it):
        m = m | np.roll(m, 1, 0) | np.roll(m, -1, 0) | np.roll(m, 1, 1) | np.roll(m, -1, 1)
    return m

def flood_from_border(passable):
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
                seen[ny, nx] = True
                dq.append((ny, nx))
    return seen

def drop_small(mask, min_area):
    H, W = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    out = mask.copy()
    for y0, x0 in np.argwhere(mask):
        if seen[y0, x0]:
            continue
        st = [(y0, x0)]; seen[y0, x0] = True; pix = []
        while st:
            y, x = st.pop(); pix.append((y, x))
            for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                ny, nx = y + dy, x + dx
                if 0 <= ny < H and 0 <= nx < W and mask[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True; st.append((ny, nx))
        if len(pix) < min_area:
            for y, x in pix:
                out[y, x] = False
    return out

# ---------- 2. 前景：外部蒙版优先（SAM3 语义分割），否则区域生长 ----------
if a.fg_mask:
    _m = Image.open(a.fg_mask).convert('L').resize((S, S), Image.LANCZOS)
    fg = np.asarray(_m) > 128
    a.open = 0                      # 语义蒙版已经干净，不再用形态学开运算
else:
    bg_like = (dist < a.fg_dist) & (grad < np.percentile(grad, a.grad_pct))
    fg = ~flood_from_border(bg_like)
    if a.dilate:
        fg = dilate(fg, a.dilate)

def erode(m, it=1):
    for _ in range(it):
        m = m & np.roll(m, 1, 0) & np.roll(m, -1, 0) & np.roll(m, 1, 1) & np.roll(m, -1, 1)
    return m

# 形态学开运算：背景织纹是细碎纹理，先腐蚀断掉，再按连通域面积筛掉碎块，
# 最后回扩恢复花/叶的原始大小（这是"去毛边/去多余色块"的关键一步）。
if a.open:
    fg = erode(fg, a.open)
    fg = drop_small(fg, max(8, int(a.min_blob * S * S)))
    fg = dilate(fg, a.open + 1)
fg = drop_small(fg, max(8, int(a.min_area * S * S)))

# 只填小洞（背景大孔保留）：从四边泛洪，未到达的洞小于 0.4% 面积才填
_bgm = ~fg
_seed = np.zeros_like(_bgm)
_seed[0, :] = _bgm[0, :]; _seed[-1, :] = _bgm[-1, :]
_seed[:, 0] = _bgm[:, 0]; _seed[:, -1] = _bgm[:, -1]
_seen = _seed.copy()
_dq = deque(map(tuple, np.argwhere(_seed)))
while _dq:
    _y, _x = _dq.popleft()
    for _dy, _dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        _ny, _nx = _y + _dy, _x + _dx
        if 0 <= _ny < S and 0 <= _nx < S and _bgm[_ny, _nx] and not _seen[_ny, _nx]:
            _seen[_ny, _nx] = True
            _dq.append((_ny, _nx))
_holes = ~_seen
_hole_seen = np.zeros_like(_holes)
for _y0, _x0 in np.argwhere(_holes):
    if _hole_seen[_y0, _x0]:
        continue
    _st = [(_y0, _x0)]; _hole_seen[_y0, _x0] = True; _pix = []
    while _st:
        _y, _x = _st.pop(); _pix.append((_y, _x))
        for _dy, _dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            _ny, _nx = _y + _dy, _x + _dx
            if 0 <= _ny < S and 0 <= _nx < S and _holes[_ny, _nx] and not _hole_seen[_ny, _nx]:
                _hole_seen[_ny, _nx] = True; _st.append((_ny, _nx))
    if len(_pix) < 0.004 * S * S:
        for _y, _x in _pix:
            fg[_y, _x] = True

# ---------- 3. 软蒙版：遮罩轻微羽化后放大到印刷尺寸 ----------
alpha_img = Image.fromarray((fg.astype(np.uint8) * 255)).filter(ImageFilter.GaussianBlur(0.6))

# ---------- 4. 背景板：背景像素向外扩散填充，再柔化 ----------
known = ~fg
plate = np.asarray(small_img, dtype=np.float32)
num = plate * known[..., None]
den = known.astype(np.float32)
for _ in range(90):
    n_img = Image.fromarray(np.clip(num, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(2.0))
    d_img = Image.fromarray((np.clip(den, 0, 1) * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(2.0))
    nb = np.asarray(n_img, dtype=np.float32)
    db = np.asarray(d_img, dtype=np.float32) / 255.0
    est = nb / np.maximum(db, 1e-3)[..., None]
    num = np.where(known[..., None], plate, est)
    den = np.where(known, 1.0, np.maximum(db, 0.35))
plate = (num / np.maximum(den, 1e-3)[..., None]).clip(0, 255)
plate_img = Image.fromarray(plate.astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.6))

# ---------- 5. 全分辨率与 PSD ----------
big = np.asarray(src.resize((TW, TH), Image.LANCZOS), dtype=np.uint8)
alpha_full = np.asarray(alpha_img.resize((TW, TH), Image.LANCZOS), dtype=np.uint8)
plate_full = np.asarray(plate_img.resize((TW, TH), Image.LANCZOS), dtype=np.uint8)

psd = PSDImage.new('RGB', (TW, TH))

def add_layer(name, rgb, mask=None, visible=True):
    layer = PixelLayer.frompil(Image.fromarray(rgb, 'RGB'), psd, name)
    if mask is not None:
        layer.create_mask(Image.fromarray(mask, 'L'))
    layer.visible = visible
    psd.append(layer)
    return layer

add_layer('Original HD', big, visible=False)
add_layer('Background Alt', plate_full, mask=(255 - alpha_full), visible=False)
add_layer('Background', plate_full, visible=True)
add_layer('Foreground', big, mask=alpha_full, visible=True)

if a.keep_colors:
    px = a_small.reshape(-1, 3)
    rng = np.random.default_rng(7)
    cent = px[rng.choice(len(px), size=a.k, replace=False)].copy()
    for _ in range(14):
        d = ((px[:, None, :] - cent[None, :, :]) ** 2).sum(axis=2)
        lab = d.argmin(axis=1)
        for k in range(a.k):
            sel = lab == k
            if sel.any():
                cent[k] = px[sel].mean(axis=0)
    cent = np.clip(cent, 0, 255)
    lab = ((px[:, None, :] - cent[None, :, :]) ** 2).sum(axis=2).argmin(axis=1).reshape(S, S)
    bg_k = int(np.bincount(lab.ravel()).argmax())
    colors = []
    for k in [int(x) for x in np.argsort(-np.bincount(lab.ravel())) if x != bg_k]:
        m = np.asarray(Image.fromarray((((lab == k) & fg).astype(np.uint8) * 255)).resize((TW, TH), Image.LANCZOS))
        rgb = np.where((m > 127)[..., None], big, 0).astype(np.uint8)
        color = tuple(int(v) for v in cent[k])
        colors.append(add_layer('Color%02d #%02x%02x%02x' % (len(colors) + 1, *color),
                                rgb, mask=m, visible=False))
    psd.create_group(colors, name='Colors').visible = False

ppi = a.ppi << 16
res = ResoulutionInfo(horizontal=ppi, horizontal_unit=1, width_unit=1,
                      vertical=ppi, vertical_unit=1, height_unit=1)
psd.image_resources[Resource.RESOLUTION_INFO] = ImageResource(key=Resource.RESOLUTION_INFO, data=res)
psd.save(a.out, compression=Compression.RLE)

print(json.dumps({
    'out': a.out, 'size': [TW, TH],
    'cm': [round(TW / a.ppi * 2.54, 2), round(TH / a.ppi * 2.54, 2)],
    'seconds': round(time.time() - t0),
    'foreground_coverage': round(float((alpha_full > 128).mean()), 4),
    'feather_fraction': round(float(((alpha_full > 8) & (alpha_full < 247)).mean()), 4),
    'layers': [l.name for l in psd], 'mode': 'elements',
}, ensure_ascii=True))
