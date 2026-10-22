#!/usr/bin/env python3
"""第 6 步（区域框选修复，Poisson 版）

用途：只修复图片里的一小块（一条缝线/一处瑕疵），其它像素尽量不动。
流程：裁 patch（带上下文）→ 放大 → 蒙版留给 AI → 下载 → 缩回 → cv2.seamlessClone（Poisson）贴回。
Poisson 融合可消除"框边新缝"。

用法:
  # 自动修一条竖线/横线（分段）
  python steps/06-repair-region.py --image tile.png --out fixed.png --line v:512 \
      --pad 80 --mask-width 26 --seg 320 --scale 3
  # 任意框选（用户指定区域）
  python steps/06-repair-region.py --image tile.png --out fixed.png \
      --box 400,300,200,200 --mask-box 30,30,140,140 --scale 3
"""
import sys, os, json, time, argparse, base64, subprocess
import numpy as np
import cv2
from PIL import Image
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
env = {}
for line in open(os.path.join(ROOT, '.env'), encoding='utf-8'):
    if '=' in line and not line.strip().startswith('#'):
        k, v = line.strip().split('=', 1); env[k] = v
BASE, KEY = env['APIMART_BASE'], env['APIMART_KEY']

ap = argparse.ArgumentParser()
ap.add_argument('--image', required=True); ap.add_argument('--out', required=True)
ap.add_argument('--box'); ap.add_argument('--mask-box')
ap.add_argument('--line'); ap.add_argument('--pad', type=int, default=80)
ap.add_argument('--mask-width', type=int, default=26)
ap.add_argument('--seg', type=int, default=320)
ap.add_argument('--scale', type=float, default=3)
ap.add_argument('--feather', type=int, default=12)
ap.add_argument('--model', default='gpt-image-2.5-sunburst')
ap.add_argument('--samples', type=int, default=3, help='每段生成候选数，按打分选最优')
ap.add_argument('--engine', choices=['ai','donor'], default='ai', help='ai=生成模型; donor=图内最佳匹配条带(免费)')
a = ap.parse_args()

I = np.asarray(Image.open(a.image).convert('RGB'))
H, W, _ = I.shape
out = I.copy()

def api_inpaint(patch_png, mask_png, tag):
    payload = {'model': a.model, 'prompt': 'This is a close-up of a watercolor floral textile painting. A thin transparent strip hides a small part of the painting. Continue the painting naturally across the strip: petals, leaves, specks, brush strokes and paper grain must flow continuously and match the surroundings; keep everything outside the strip identical; no new motifs, no colour shift, no visible seam.', 'size': 'auto', 'n': 1,
               'image_urls': ['data:image/png;base64,' + base64.b64encode(patch_png).decode()],
               'mask_url': 'data:image/png;base64,' + base64.b64encode(mask_png).decode()}
    last = None
    for _try in range(4):
        try:
            r = requests.post(BASE.rstrip('/') + '/images/generations', headers={'Authorization': 'Bearer ' + KEY, 'Content-Type': 'application/json'}, json=payload, timeout=120)
            j = r.json(); tid = j['data'][0]['task_id']; break
        except Exception as e:
            last = e; time.sleep(4)
    else:
        raise last
    for _ in range(120):
        time.sleep(3)
        d = None
        for _try in range(4):
            try:
                d = requests.get(BASE.rstrip('/') + f'/tasks/{tid}', headers={'Authorization': 'Bearer ' + KEY}, timeout=180).json()['data']; break
            except Exception:
                time.sleep(4)
        if d is None: continue
        if d['status'] == 'completed':
            url = d['result']['images'][0]['url'][0]
            last = None
            for _try in range(4):
                try:
                    return requests.get(url, timeout=180).content, d.get('cost', 0)
                except Exception as e:
                    last = e; time.sleep(4)
            raise last
        if d['status'] == 'failed':
            raise RuntimeError('inpaint failed: ' + json.dumps(d)[:200])
    raise RuntimeError('timeout')

def score_candidate(gen_np, orig_np, mask_box):
    """候选打分：边界连续性 + 颜色合理性（Lab）+ 杂色惩罚。越小越好。"""
    mx, my, mw, mh = mask_box
    h, w = gen_np.shape[:2]
    m = np.zeros((h, w), np.uint8); m[my:my+mh, mx:mx+mw] = 255
    ring = cv2.dilate(m, np.ones((9, 9), np.uint8)) - cv2.erode(m, np.ones((5, 5), np.uint8))
    inside = m > 0
    # 边界连续性：环带内 gen vs orig 差异
    boundary = float(np.abs(gen_np.astype(np.float32) - orig_np.astype(np.float32))[ring > 0].mean()) if (ring > 0).any() else 0.0
    # 颜色合理性：掩膜内均值 vs 环带均值（Lab）
    lab_g = cv2.cvtColor(gen_np, cv2.COLOR_RGB2LAB).astype(np.float32)
    lab_o = cv2.cvtColor(orig_np, cv2.COLOR_RGB2LAB).astype(np.float32)
    inside_mean = lab_g[inside].mean(axis=0)
    ring_mean = lab_o[ring > 0].mean(axis=0) if (ring > 0).any() else inside_mean
    color_dist = float(np.linalg.norm(inside_mean - ring_mean))
    # 杂色惩罚：掩膜内高饱和/异常色相占比（相对整块 patch 分布）
    hsv = cv2.cvtColor(gen_np, cv2.COLOR_RGB2HSV).astype(np.float32)
    sat = hsv[..., 1][inside]
    patch_sat_p95 = float(np.percentile(hsv[..., 1], 95)) + 1e-6
    outlier = float((sat > max(patch_sat_p95, 120)).mean()) if len(sat) else 0.0
    return boundary * 1.0 + color_dist * 2.0 + outlier * 200.0

def donor_fill(box, band, cost_acc):
    """图内 donor 条带 + Poisson：在整图搜索与缝两侧上下文最匹配的条带，贴回。"""
    x, y, w, h = box
    best = None
    for dy in range(-160, 161, 40):
        for dx in list(range(-360, -79, 40)) + list(range(80, 361, 40)):
            sx0 = x + dx; sy0 = y + dy
            if sy0 < 0 or sy0 + h > H or sx0 < 0 or sx0 + band > W: continue
            src = I[sy0:sy0+h, sx0:sx0+band]
            L = np.abs(src[:, 0].astype(np.float32) - I[y:y+h, x-1].astype(np.float32)).mean()
            R = np.abs(src[:, -1].astype(np.float32) - I[y:y+h, x+band].astype(np.float32)).mean()
            sc = L + R
            if best is None or sc < best[0]: best = (sc, src.copy())
    src = best[1]
    m = np.zeros((H, W), np.uint8); m[y:y+h, x:x+band] = 255
    src_full = np.zeros_like(I); src_full[y:y+h, x:x+band] = src
    center = (x + band // 2, y + h // 2)
    try: return cv2.seamlessClone(src_full, I, m, center, cv2.NORMAL_CLONE)
    except Exception:
        I2 = I.copy(); I2[y:y+h, x:x+band] = src; return I2

def repair_patch(box, mask_box, cost_acc):
    x, y, w, h = box
    crop = Image.fromarray(I[y:y+h, x:x+w])
    up_w = int(min(1536, max(256, w * a.scale))); up_h = int(up_w * h / w)
    if up_h > 2048: up_h = 2048; up_w = int(up_h * w / h)
    up = crop.resize((up_w, up_h), Image.LANCZOS)
    import io
    b1 = io.BytesIO(); up.save(b1, 'PNG')
    mx, my, mw, mh = mask_box
    sx, sy = up_w / w, up_h / h
    mask = np.zeros((up_h, up_w, 4), dtype=np.uint8); mask[..., :3] = 255
    x0, x1 = int(mx*sx), int((mx+mw)*sx); y0, y1 = int(my*sy), int((my+mh)*sy)
    mask[y0:y1, x0:x1, 3] = 0
    b2 = io.BytesIO(); Image.fromarray(mask, 'RGBA').save(b2, 'PNG')
    orig_np = np.asarray(I[y:y+h, x:x+w]).copy()
    best = None
    for si in range(max(1, a.samples)):
        body, cost = api_inpaint(b1.getvalue(), b2.getvalue(), f'seg{si}')
        cost_acc.append(cost)
        cand = Image.open(io.BytesIO(body)).convert('RGB').resize((w, h), Image.LANCZOS)
        cand_np = np.asarray(cand)
        sc = score_candidate(cand_np, orig_np, mask_box)
        if best is None or sc < best[0]:
            best = (sc, cand_np)
    gen_np = best[1]
    # Poisson：把 gen 以 mask_box 区域无缝贴入 out
    m = np.zeros((h, w), dtype=np.uint8); m[my:my+mh, mx:mx+mw] = 255
    center = (x + mx + mw // 2, y + my + mh // 2)
    try:
        blended = cv2.seamlessClone(gen_np, out, m, center, cv2.NORMAL_CLONE)
        return blended
    except Exception:
        # 退化：feather 融合
        alpha = cv2.GaussianBlur(m.astype(np.float32) / 255.0, (0, 0), max(1, a.feather / 2.0))
        alpha = alpha[..., None]
        patch = out.copy(); patch[y:y+h, x:x+w] = (out[y:y+h, x:x+w] * (1 - alpha) + gen_np * alpha).astype(np.uint8)
        return patch

costs = []
if a.box:
    x, y, w, h = [int(v) for v in a.box.split(',')]
    if a.mask_box:
        mx, my, mw, mh = [int(v) for v in a.mask_box.split(',')]
    else:
        mx, my, mw, mh = int(w*0.2), int(h*0.2), int(w*0.6), int(h*0.6)
    out = repair_patch((x, y, w, h), (mx, my, mw, mh), costs)
else:
    axis, pos = a.line.split(':'); pos = int(pos)
    if axis == 'v':
        step = max(80, a.seg - 2*a.feather)
        ys = list(range(0, H, step))
        for i, y0 in enumerate(ys):
            h = min(a.seg, H - y0)
            if h < 60: continue
            x0 = max(0, pos - a.pad); w = min(W - x0, 2*a.pad)
            if a.engine == 'donor':
                out = donor_fill((pos - a.mask_width//2, y0, a.mask_width, h), a.mask_width, costs)
            else:
                mh = max(20, h - 2*a.feather); my = (h - mh)//2
                mx = (w - a.mask_width)//2
                out = repair_patch((x0, y0, w, h), (mx, my, a.mask_width, mh), costs)
    else:
        step = max(80, a.seg - 2*a.feather)
        xs = list(range(0, W, step))
        for x0 in xs:
            w = min(a.seg, W - x0)
            if w < 60: continue
            y0 = max(0, pos - a.pad); h = min(H - y0, 2*a.pad)
            if a.engine == 'donor':
                out = donor_fill((x0, pos - a.mask_width//2, w, a.mask_width), a.mask_width, costs)
            else:
                mw = max(20, w - 2*a.feather); mx = (w - mw)//2
                my = (h - a.mask_width)//2
                out = repair_patch((x0, y0, w, h), (mx, my, mw, a.mask_width), costs)

Image.fromarray(out).save(a.out)
g = out.astype(np.float32).mean(axis=2)
meta = {'step': '06-repair-region', 'image': a.image, 'out': a.out, 'mode': 'poisson', 'engine': a.engine,
        'box': a.box, 'line': a.line, 'scale': a.scale, 'segments': len(costs),
        'wrapX': round(float(np.abs(g[:,0]-g[:,-1]).mean()),2), 'wrapY': round(float(np.abs(g[0,:]-g[-1,:]).mean()),2),
        'cost_usd': round(float(sum(costs)),5)}
open(os.path.splitext(a.out)[0]+'.meta.json','w').write(json.dumps(meta, ensure_ascii=False, indent=2))
print(json.dumps(meta, ensure_ascii=False))
