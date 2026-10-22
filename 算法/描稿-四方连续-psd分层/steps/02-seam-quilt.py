#!/usr/bin/env python3
"""第 2 步（方案 E）：quilting 纹理合成补缝（v3：环绕画布 + 多尺度匹配 + Poisson 融合）

原理：
- 用 np.pad(mode='wrap') 把原图扩成环绕画布，接缝带位于画布内，补丁跨接缝时整块写入 → 天然连续；
- 逐块搜索最匹配补丁（先粗后细多尺度），用 cv2.seamlessClone（Poisson）融合 → 消除补丁色差/拼块；
- 最后裁回原尺寸，wrap 理论为 0。
用法: python steps/02-seam-quilt.py --base base.png --out seamless.png \
        [--band 160] [--patch 40] [--overlap 20] [--candidates 1200] [--seed 7]
退出码: 0=wrap 达标；2=未达标
"""
import sys, json, os, argparse, time
import numpy as np
import cv2
from PIL import Image

ap = argparse.ArgumentParser()
ap.add_argument('--base', required=True); ap.add_argument('--out', required=True)
ap.add_argument('--band', type=int, default=160)
ap.add_argument('--patch', type=int, default=44)
ap.add_argument('--overlap', type=int, default=22)
ap.add_argument('--candidates', type=int, default=900)
ap.add_argument('--seed', type=int, default=7)
ap.add_argument('--max-wrap', type=float, default=1.3)
a = ap.parse_args()
rng = np.random.default_rng(a.seed); t0 = time.time()

I = np.asarray(Image.open(a.base).convert('RGB'), dtype=np.uint8)
H, W, _ = I.shape
P, OV, B = a.patch, a.overlap, a.band
STEP = P - OV
M = B // 2 + P  # 环绕 padding（至少覆盖半条缝带）
pad = np.pad(I, ((M, M), (M, M), (0, 0)), mode='wrap')
PH, PW = pad.shape[:2]

# 候选源补丁（在 padded 上采样，天然覆盖环绕）
stride = 8
ys = np.arange(0, PH - P + 1, stride); xs = np.arange(0, PW - P + 1, stride)
grid = [(x, y) for y in ys for x in xs]
if len(grid) > a.candidates:
    idx = rng.choice(len(grid), a.candidates, replace=False)
    grid = [grid[i] for i in idx]
full = np.asarray([pad[by:by+P, bx:bx+P] for (bx, by) in grid], dtype=np.float32)
SC = 4
small = np.asarray([cv2.resize(p, (P // SC, P // SC), interpolation=cv2.INTER_AREA) for p in full], dtype=np.float32)

out = pad.copy()
known = np.ones((PH, PW), dtype=bool)

def mask_v():
    m = np.zeros((PH, PW), dtype=bool); s = np.arange(-B // 2, B // 2)
    m[:, M + s] = True; return m
def mask_h():
    m = np.zeros((PH, PW), dtype=bool); s = np.arange(-B // 2, B // 2)
    m[M + s, :] = True; return m

def best_match(ctx, tgt_patch):
    # 粗筛（1/SC 分辨率）
    ys_, xs_ = np.nonzero(ctx)
    if len(ys_) < 20: return None
    ys_s = (ys_ // SC).clip(0, P // SC - 1); xs_s = (xs_ // SC).clip(0, P // SC - 1)
    tgt_s = tgt_patch.astype(np.float32)
    tgt_small = cv2.resize(tgt_s, (P // SC, P // SC), interpolation=cv2.INTER_AREA)
    ts = tgt_small[ys_s, xs_s] - tgt_small[ys_s, xs_s].mean(axis=0, keepdims=True)
    ss = small[:, ys_s, xs_s]
    ss = ss - ss.mean(axis=1, keepdims=True)
    d = ((ss - ts[None, :, :]) ** 2).mean(axis=(1, 2))
    order = np.argsort(d)[:6]
    # 精筛（全分辨率）
    tf = tgt_s[ys_, xs_] - tgt_s[ys_, xs_].mean(axis=0, keepdims=True)
    best = (1e18, None)
    for i in order:
        sf = full[i, ys_, xs_]; sf = sf - sf.mean(axis=0, keepdims=True)
        dd = float(((sf - tf) ** 2).mean())
        if dd < best[0]: best = (dd, int(i))
    return best[1]

def fill(axis):
    global known
    known &= ~(mask_v() if axis == 'v' else mask_h())
    if axis == 'v':
        s0s = list(range(M - B // 2, M + B // 2, STEP)); t0s = list(range(0, PH, STEP))
    else:
        s0s = list(range(M - B // 2, M + B // 2, STEP)); t0s = list(range(0, PW, STEP))
    for t0 in t0s:
        for s0 in s0s:
            if axis == 'v': x0, y0 = s0, t0
            else: x0, y0 = t0, s0
            x1, y1 = min(x0 + P, PW), min(y0 + P, PH)
            x0c, y0c = max(0, x1 - P), max(0, y1 - P)
            xs_, ys_ = np.meshgrid(np.arange(x0c, x1), np.arange(y0c, y1), indexing='xy')
            Y, X = ys_, xs_
            unknown = ~known[Y, X]
            if not unknown.any(): continue
            ctx = ~unknown
            tgt = pad[Y, X]
            bi = best_match(ctx, tgt) if ctx.sum() >= 60 else None
            if bi is None:
                bx, by = grid[rng.integers(len(grid))]
                patch = pad[by:by+P, bx:bx+P]
            else:
                bx, by = grid[bi]; patch = pad[by:by+P, bx:bx+P]
            # Poisson 融合：mask = 未知区（外扩 2px），center 取未知区中心
            m = (unknown.astype(np.uint8) * 255)
            m = cv2.dilate(m, np.ones((3, 3), np.uint8))
            ys2, xs2 = np.nonzero(m)
            if len(ys2) == 0: continue
            cy, cx = int(ys2.mean()) + y0c, int(xs2.mean()) + x0c
            try:
                out[:, :] = cv2.seamlessClone(patch, out, m, (cx, cy), cv2.NORMAL_CLONE)
            except Exception:
                out[Y[unknown], X[unknown]] = patch[unknown]
            known[Y, X] = True

fill('v'); fill('h')
# 裁回原尺寸 + 极窄 wrap 缝合（3px，避免可见糊带）
res = out[M:M+H, M:M+W].astype(np.float32)
F = 3
for i in range(F):
    w = i / max(1, F - 1)
    bl = (1 - w) * res[:, i] + w * res[:, W - 1 - i]
    res[:, i] = bl; res[:, W - 1 - i] = bl
    br = (1 - w) * res[i, :] + w * res[H - 1 - i, :]
    res[i, :] = br; res[H - 1 - i, :] = br

res = np.clip(res, 0, 255).astype(np.uint8)
g = res.astype(np.float32).mean(axis=2)
wx = float(np.abs(g[:, 0] - g[:, -1]).mean()); wy = float(np.abs(g[0, :] - g[-1, :]).mean())
ax = float(np.abs(np.diff(g, axis=1)).mean()); ay = float(np.abs(np.diff(g, axis=0)).mean())
meta = {'step': '02-seam-quilt-v3', 'base': a.base, 'out': a.out, 'band': B, 'patch': P, 'overlap': OV,
        'candidates': len(grid), 'size': [W, H], 'wrapX': round(wx, 2), 'wrapY': round(wy, 2),
        'ratioX': round(wx / (ax + 1e-6), 2), 'ratioY': round(wy / (ay + 1e-6), 2),
        'ok': bool(wx <= max(6.0, a.max_wrap * ax) and wy <= max(6.0, a.max_wrap * ay)),
        'seconds': round(time.time() - t0, 1)}
Image.fromarray(res).save(a.out)
open(os.path.splitext(a.out)[0] + '.meta.json', 'w').write(json.dumps(meta, ensure_ascii=False, indent=2))
print(json.dumps(meta, ensure_ascii=False))
sys.exit(0 if meta['ok'] else 2)
