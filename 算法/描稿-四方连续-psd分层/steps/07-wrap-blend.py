#!/usr/bin/env python3
"""2×2 画布 inpaint 的修补带后处理（L33/L34 思路 + 颗粒回灌）。

输入：base（1024 底图）、tile（_wrapinpaint 输出的中心裁剪 tile）、strip
做法（全部只作用在修补十字带内，外缘不动以保证 wrap）：
  ① 色调补偿：把带内低频拉到与环带一致（ramp 在带边归零，避免新硬边）
  ② 颗粒回灌：把「对齐后的原图」高频按 w 混回带内 —— 只回高频（σ=4），
     不会带回原图那点几何错位造成的暗线；模型输出普遍偏平滑（L32）
  ③ 边界羽化：带边 alpha 用带噪声扰动的 ramp，破掉直线边
输出：处理后的 tile + JSON 指标（带内外低频差 / 颗粒比）
"""
import argparse, json, sys
import numpy as np
from PIL import Image, ImageFilter

ap = argparse.ArgumentParser()
ap.add_argument('--base'); ap.add_argument('--tile'); ap.add_argument('--out')
ap.add_argument('--strip', type=int, default=82)
ap.add_argument('--offset', default='')          # "x,y"，缺省中心
ap.add_argument('--tone', type=float, default=1.0)   # 0=不做色调补偿
ap.add_argument('--grain', type=float, default=0.5)  # 高频回灌权重
ap.add_argument('--feather', type=int, default=14)   # 带边羽化宽度
ap.add_argument('--noise', type=int, default=10)     # 羽化边界扰动幅度
ap.add_argument('--sigma-hp', type=float, default=4.0)
ap.add_argument('--sigma-lp', type=float, default=21.0)
a = ap.parse_args()

def load(p):
    return np.asarray(Image.open(p).convert('RGB'), dtype=np.float32)

base = load(a.base); tile = load(a.tile)
H, W, _ = tile.shape
bh, bw, _ = base.shape
ox, oy = (W // 2, H // 2)
if a.offset:
    ox, oy = [int(v) for v in a.offset.split(',')]
# 与 tile 对齐的“原图内容”
orig = np.roll(base, shift=(-oy, -ox), axis=(0, 1))[:H, :W]

B = a.strip
yy, xx = np.mgrid[0:H, 0:W]
def soft_mask(extra=0):
    d = np.maximum(np.abs(xx - W // 2), np.abs(yy - H // 2))  # 切比雪夫距离
    # 带内=1，带边 feash 内渐变到 0
    return np.clip((B + extra - d) / max(1.0, a.feather), 0.0, 1.0)

alpha = soft_mask()
# 边界扰动：长波噪声让边界不规则（±noise px），破掉直线边
rng = np.random.default_rng(7)
nz = rng.normal(0, 1, (H // 8 + 2, W // 8 + 2))
nz = np.asarray(Image.fromarray(((nz - nz.min()) / (np.ptp(nz) + 1e-9) * 255).astype(np.uint8))
                .resize((W, H), Image.BICUBIC), dtype=np.float32) / 255.0
alpha = np.clip(alpha + (nz - 0.5) * (a.noise / max(1.0, a.feather)) * (alpha > 0) * (alpha < 1), 0, 1)
alpha3 = alpha[..., None]

out = tile.copy()
# ① 色调补偿：把带内低频拉到和「环带」（带外一圈）一致
if a.tone > 0:
    def blur(img, s):
        return np.asarray(Image.fromarray(np.clip(img, 0, 255).astype(np.uint8))
                          .filter(ImageFilter.GaussianBlur(s)), dtype=np.float32)
    lp_t = blur(tile, a.sigma_lp)
    band = (alpha > 0.95)
    ring = ((alpha <= 0.0)
            & (np.abs(xx - W // 2) <= B * 2) & (np.abs(yy - H // 2) <= B * 2))
    if band.any() and ring.any():
        delta = lp_t[ring].mean(0) - lp_t[band].mean(0)
        out = out + (alpha3 * (delta * a.tone))

# ② 颗粒回灌：只回高频
if a.grain > 0:
    hp = orig - np.asarray(Image.fromarray(np.clip(orig, 0, 255).astype(np.uint8))
                           .filter(ImageFilter.GaussianBlur(a.sigma_hp)), dtype=np.float32)
    out = out + alpha3 * a.grain * hp

out = np.clip(out, 0, 255)

# ③ 边界羽化：带外还原成原图，带边按 alpha 混合
final = alpha3 * out + (1 - alpha3) * orig
final = np.clip(final, 0, 255).astype(np.uint8)
Image.fromarray(final).save(a.out)

# 指标：带内/环带 低频差与颗粒比（和 03b 同口径）
def blink(img, s):
    return np.asarray(Image.fromarray(np.clip(img, 0, 255).astype(np.uint8))
                      .filter(ImageFilter.GaussianBlur(s)), dtype=np.float32)
def stats(img):
    lp = blink(img, 19.0); hp = img - lp
    band = np.zeros((H, W), bool); band[B:-B, B:-B] = True
    inn = band | (np.abs(xx - W // 2) <= B) | (np.abs(yy - H // 2) <= B)
    ring = (~inn) & (np.abs(xx - W // 2) < B * 3) & (np.abs(yy - H // 2) < B * 3)
    d_lp = float(abs(lp[inn].mean() - lp[ring].mean())) if ring.any() else 0.0
    hp_ratio = float(hp[inn].std() / max(hp[ring].std(), 1e-6))
    return d_lp, hp_ratio
d0, g0 = stats(tile); d1, g1 = stats(np.asarray(final, dtype=np.float32))
print(json.dumps({'out': a.out, 'strip': B, 'offset': [ox, oy], 'tone': a.tone,
                  'grain': a.grain, 'feather': a.feather,
                  'before': {'d_lp': round(d0, 2), 'hp_ratio': round(g0, 2)},
                  'after': {'d_lp': round(d1, 2), 'hp_ratio': round(g1, 2)}}))
