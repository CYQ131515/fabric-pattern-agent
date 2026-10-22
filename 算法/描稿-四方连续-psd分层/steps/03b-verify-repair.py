#!/usr/bin/env python3
"""第 3b 步（审查）：修复带连续性检查

针对"修复带（inpaint/quilt/donor 区域）与周围亮度/颗粒不一致"的瑕疵：
给定修复带位置（v:cx,width / h:cy,width），比较带内与带外相邻区域的
低频亮度差和高频颗粒比；超阈值判为可见补丁。

用法: python steps/03b-verify-repair.py --image seamless.png --bands "v:440,36;h:512,36"
退出码: 0=通过；2=修复带可见
"""
import sys, json, argparse
import numpy as np
from PIL import Image, ImageFilter

ap = argparse.ArgumentParser()
ap.add_argument('--image', required=True)
ap.add_argument('--bands', required=True, help='例如 v:440,36;h:512,36（多条用 ; 分隔）')
ap.add_argument('--ring', type=int, default=40)
ap.add_argument('--max-dlp', type=float, default=1.5)
ap.add_argument('--hp-ratio', type=float, default=1.35)
a = ap.parse_args()

g = np.asarray(Image.open(a.image).convert('L'), dtype=np.float32)
H, W = g.shape
lp = np.asarray(Image.fromarray(g.astype(np.uint8)).filter(ImageFilter.GaussianBlur(3)), dtype=np.float32)
hp = g - np.asarray(Image.fromarray(g.astype(np.uint8)).filter(ImageFilter.GaussianBlur(2)), dtype=np.float32)

def stats(region):
    if region.size == 0: return None
    return float(region.mean()), float(region.std())

bad = []
for spec in a.bands.split(';'):
    spec = spec.strip()
    if not spec: continue
    axis, rest = spec.split(':')
    pos, width = [int(v) for v in rest.split(',')]
    if axis == 'v':
        x0, x1 = max(0, pos - width // 2), min(W, pos + width // 2)
        inner_lp = lp[:, x0:x1]; inner_hp = hp[:, x0:x1]
        out_lp = np.concatenate([lp[:, max(0, x0 - a.ring):x0], lp[:, x1:min(W, x1 + a.ring)]], axis=1)
        out_hp = np.concatenate([hp[:, max(0, x0 - a.ring):x0], hp[:, x1:min(W, x1 + a.ring)]], axis=1)
    else:
        y0, y1 = max(0, pos - width // 2), min(H, pos + width // 2)
        inner_lp = lp[y0:y1, :]; inner_hp = hp[y0:y1, :]
        out_lp = np.concatenate([lp[max(0, y0 - a.ring):y0, :], lp[y1:min(H, y1 + a.ring), :]], axis=0)
        out_hp = np.concatenate([hp[max(0, y0 - a.ring):y0, :], hp[y1:min(H, y1 + a.ring), :]], axis=0)
    d_lp = abs(float(inner_lp.mean()) - float(out_lp.mean()))
    r_hp = (float(inner_hp.std()) + 1e-6) / (float(out_hp.std()) + 1e-6)
    visible = (d_lp > a.max_dlp) or (r_hp > a.hp_ratio) or (r_hp < 1.0 / a.hp_ratio)
    if visible:
        bad.append({'axis': axis, 'pos': pos, 'width': width, 'd_lp': round(d_lp, 2), 'hp_ratio': round(r_hp, 2),
                    'reason': 'lp' if d_lp > a.max_dlp else 'grain'})
res = {'image': a.image, 'bands': a.bands, 'ok': len(bad) == 0, 'visible_bands': bad,
       'rule': '带内外低频亮度差>%s 或颗粒比>%s → 修复带可见' % (a.max_dlp, a.hp_ratio)}
print(json.dumps(res, ensure_ascii=False))
sys.exit(0 if res['ok'] else 2)
