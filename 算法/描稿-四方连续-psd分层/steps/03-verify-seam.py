#!/usr/bin/env python3
"""第 3 步（审查）：整图接缝检测（离群直线判据）

为什么不能只看外缘：平移补缝会把原图边缘移到图片内部；必须扫全图。
判据：接缝表现为"贯穿式离群直线"——某列/行的跨线梯度均值显著高于其他列/行（离群），
且该高梯度覆盖较长跨度（不是局部物体边缘）。
用法: python steps/03-verify-seam.py --image work/seamless.png [--json]
退出码: 0=通过, 2=疑似接缝
"""
import sys, json, argparse
import numpy as np
from PIL import Image

ap = argparse.ArgumentParser()
ap.add_argument('--image', required=True)
ap.add_argument('--width', type=int, default=1024)
ap.add_argument('--json', action='store_true')
ap.add_argument('--z', type=float, default=8.0)        # 离群 z 阈值
ap.add_argument('--ratio', type=float, default=1.8)    # 相对中位数倍数阈值
ap.add_argument('--frac', type=float, default=0.18)    # 高梯度覆盖跨度下限（自然物体边缘通常更低）
a = ap.parse_args()

im = Image.open(a.image).convert('L')
if im.width > a.width:
    im = im.resize((a.width, max(1, round(a.width * im.height / im.width))), Image.LANCZOS)
g = np.asarray(im, dtype=np.float32)
H, W = g.shape

def line_scores(diff_mean, diff_frac, axis_name, span_len):
    med = float(np.median(diff_mean)); mad = float(np.median(np.abs(diff_mean - med))) + 1e-6
    sigma = mad * 1.4826
    out = []
    for i in range(2, len(diff_mean) - 1):
        z = (diff_mean[i] - med) / (sigma + 1e-6)
        if z >= a.z and diff_mean[i] >= med * a.ratio and diff_frac[i] >= a.frac:
            out.append({'axis': axis_name, 'pos': round(i / span_len, 3), 'px': int(i),
                        'mean': round(float(diff_mean[i]), 2), 'frac': round(float(diff_frac[i]), 2), 'z': round(float(z), 1)})
    return out, med, sigma

# 垂直：每列跨线梯度（对所有行取均值）
dcol = np.abs(np.diff(g, axis=1))            # H x (W-1)
col_mean = dcol.mean(axis=0); col_frac = (dcol > 10).mean(axis=0)
# 水平：每行跨线梯度（对所有列取均值）
drow = np.abs(np.diff(g, axis=0))            # (H-1) x W
row_mean = drow.mean(axis=1); row_frac = (drow > 10).mean(axis=1)

v_lines, vmed, vsig = line_scores(col_mean, col_frac, 'vertical', W)
h_lines, hmed, hsig = line_scores(row_mean, row_frac, 'horizontal', H)
susp = v_lines + h_lines

wrapX = float(np.abs(g[:, 0] - g[:, -1]).mean())
wrapY = float(np.abs(g[0, :] - g[-1, :]).mean())
adjX = float(np.abs(np.diff(g, axis=1)).mean()) + 1e-6
adjY = float(np.abs(np.diff(g, axis=0)).mean()) + 1e-6

# 外缘基线要用「同分布的高分位」而不是全局均值：
# 平移无缝的图，外缘那条差其实就是原图里某一对相邻列/行，它的对比度可能本来
# 就比全局均值高很多（深底+大花这类高方差图尤其明显）。用全局均值当基线会把
# 真正的无缝图判死（2026-10-21 深蓝底花型实测：wrap 1.4 但图其实是平移法无缝的）。
# 取相邻列/行差的 P95 作基线：外缘差只要不超过「图内典型高对比相邻对」的 1.2 倍
# 就算接上；真正的错缝会明显高于图内绝大多数相邻对。
p95X = float(np.percentile(sorted(np.abs(np.diff(g, axis=1)).mean(axis=0)), 95)) + 1e-6
p95Y = float(np.percentile(sorted(np.abs(np.diff(g, axis=0)).mean(axis=1)), 95)) + 1e-6
baseX = max(adjX, p95X)
baseY = max(adjY, p95Y)
fail_wrap = (wrapX > max(6.0, 1.2 * baseX)) or (wrapY > max(6.0, 1.2 * baseY))
ok = (len(susp) == 0) and (not fail_wrap)
res = {
    'image': a.image, 'size': [W, H], 'ok': ok,
    'suspicious_lines': susp[:12],
    'wrap': {'wrapX': round(wrapX, 2), 'wrapY': round(wrapY, 2), 'ratioX': round(wrapX / adjX, 2), 'ratioY': round(wrapY / adjY, 2), 'ratioX_p95': round(wrapX / baseX, 2), 'ratioY_p95': round(wrapY / baseY, 2), 'fail': bool(fail_wrap)},
    'baseline': {'col_median': round(vmed, 2), 'col_sigma': round(vsig, 2), 'row_median': round(hmed, 2), 'row_sigma': round(hsig, 2)},
    'rule': '整图扫描：跨线梯度离群（z≥%.1f 且 ≥%.1f×中位）且跨度≥%.0f%% 的直线 → 接缝' % (a.z, a.ratio, a.frac * 100),
}
print(json.dumps(res, ensure_ascii=False, indent=2 if a.json else None))
sys.exit(0 if ok else 2)
