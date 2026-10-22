#!/usr/bin/env python3
"""eval_sheets.py —— 用真实流水线的拆格器给各模型的九宫格打分。

评分维度（都是流水线真正在意的）：
  1. 可拆出元素数 / 9        —— 空格、粘连、被切断都会掉分
  2. 平均覆盖率（元素占 bbox）—— 太小说明元素画得太小，拼版会空
  3. 底色亮度 / 中性度        —— 白底最好抠；米黄/带纹理也行，但要中性
  4. 格线检测                 —— 模型画出灰色分隔线时，抠图会带出细线，直接判废
  5. 触边率                   —— 元素贴到格子边缘，抠图会被切断

用法: python3 eval_sheets.py /tmp/apimart_test/*.png
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, ".")
from pattern_studio import sheet as SH  # noqa: E402


def grid_line_score(img: Image.Image, dip_thr: float = 2.0, gap: int = 3) -> float:
    """检测 1/3、2/3 处是否有"贯穿整幅的细线"（生图模型画格线的典型形态）。

    做法：逐行比较候选列与左右邻居的亮度差，统计"每行都有凹陷"的比例。
    只有真正贯穿整幅的线才会得到高比例；纸张纹理/元素遮挡不会。
    返回 0–1（1 = 非常像格线）。
    """
    a = np.asarray(img.convert("L"), dtype=np.float32)
    h, w = a.shape
    best = 0.0
    for axis in (0, 1):
        n = w if axis == 0 else h
        for k in (1, 2):
            base = int(round(n * k / 3))
            for off in range(-4, 5):
                i = base + off
                if i - gap < 0 or i + gap >= n:
                    continue
                if axis == 0:
                    line, left, right = a[:, i], a[:, i - gap], a[:, i + gap]
                else:
                    line, left, right = a[i, :], a[i - gap, :], a[i + gap, :]
                dip = ((left + right) / 2.0 - line) > dip_thr
                frac = float(dip.mean())
                best = max(best, frac)
    return round(float(best), 3)


def neighbor_delta(neigh: float, base: float) -> float:
    # 归一化分母：同类纸张纹理的正常波动约 2–6 灰度级
    return max(4.0, abs(neigh - base) * 0.5)


def bg_stats(img: Image.Image) -> dict:
    a = np.asarray(img.convert("RGB"), dtype=np.float32)
    h, w = a.shape[:2]
    ring = np.concatenate([
        a[:6, :, :].reshape(-1, 3), a[-6:, :, :].reshape(-1, 3),
        a[:, :6, :].reshape(-1, 3), a[:, -6:, :].reshape(-1, 3)])
    med = np.median(ring, axis=0)
    return {"bg_med": [int(x) for x in med], "bg_lum": round(float(med.mean()), 1),
            "bg_chroma": round(float(med.max() - med.min()), 1)}


def evaluate(path: Path) -> dict:
    img = Image.open(path).convert("RGB")
    cells_meta = [{"cell": i + 1, "element": f"cell{i + 1}"} for i in range(9)]
    els = SH.split_sheet(img, cells_meta, 0)
    covs = [e.coverage for e in els]
    # 触边率：元素 bbox 是否顶到单元格边界
    touch = 0
    W, H = img.size
    cw, ch = W // 3, H // 3
    for i in range(9):
        row, col = divmod(i, 3)
        cell = img.crop((col * cw, row * ch, (col + 1) * cw, (row + 1) * ch))
        got = SH.extract_cell(cell)
        if not got:
            continue
        im, mask, cov = got
        if im.width >= cw * 0.92 or im.height >= ch * 0.92:
            touch += 1
    return {
        "file": path.name,
        "n_extracted": len(els),
        "mean_coverage": round(float(np.mean(covs)), 3) if covs else 0.0,
        "grid_lines": grid_line_score(img),
        "touch_border": touch,
        **bg_stats(img),
    }


def main():
    files = [Path(p) for p in sys.argv[1:] if not p.endswith("_compare.png")]
    if not files:
        files = sorted(Path("/tmp/apimart_test").glob("*.png"))
        files = [f for f in files if not f.name.startswith("_")]
    rows = []
    for f in files:
        try:
            r = evaluate(f)
            rows.append(r)
            print(json.dumps(r, ensure_ascii=False), flush=True)
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"file": f.name, "error": str(e)[:150]}, ensure_ascii=False), flush=True)
    Path("/tmp/apimart_test/eval.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    # 汇总排序：可拆数 → 格线 → 覆盖率
    good = [r for r in rows if r.get("n_extracted")]
    good.sort(key=lambda r: (-r["n_extracted"], r["grid_lines"], -r["mean_coverage"]))
    print("\n== 排序（可拆数优先，其次格线，其次覆盖率）==")
    for r in good:
        print(f"{r['file']:<42} n={r['n_extracted']}/9  格线={r['grid_lines']:.2f}  "
              f"覆盖={r['mean_coverage']:.1%}  底色亮度={r['bg_lum']}  触边={r['touch_border']}")


if __name__ == "__main__":
    main()
