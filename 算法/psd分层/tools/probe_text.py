"""试 SAM3.1 提示词的各种写法，看哪种真能检出东西。

用法
    RUNNINGHUB_API_KEY=xxx python tools/probe_text.py
    RUNNINGHUB_API_KEY=xxx python tools/probe_text.py --texts "flower" "花朵" "flower:24"
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rhpsd.client import (IMAGE_FIELD, IMAGE_NODE, OUT_TARGET, TEXT_FIELD,
                          TEXT_NODE, WORKFLOW_ID, RunningHub)

DEFAULT_TEXTS = [
    "花朵:24",          # 工作流里的原始示例（中文）
    "花朵",
    "花",
    "flower",
    "flowers",
    "flower:24",
    "flowers:24",
    "pink flower:24",
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default=os.path.join(ROOT, "inputs/深蓝底水彩花卉.jpeg"))
    ap.add_argument("--texts", nargs="*", default=DEFAULT_TEXTS)
    ap.add_argument("--outdir", default=os.path.join(ROOT, "out/probe_text"))
    ap.add_argument("--all-nodes", action="store_true",
                    help="同时看 432（反相）那个输出")
    args = ap.parse_args()

    rh = RunningHub()
    os.makedirs(args.outdir, exist_ok=True)

    im = Image.open(args.image).convert("RGB")
    src = os.path.join(args.outdir, "_input.png")
    im.save(src)
    fn = rh.upload(src)
    print(f"上传完成，图 {im.size}\n")

    print(f"{'提示词':<18}{'目标层不透明':>12}{'反相层不透明':>12}{'耗时':>8}")
    print("-" * 52)
    for t in args.texts:
        nodes = [
            {"nodeId": IMAGE_NODE, "fieldName": IMAGE_FIELD, "fieldValue": fn},
            {"nodeId": TEXT_NODE, "fieldName": TEXT_FIELD, "fieldValue": t},
        ]
        t0 = time.time()
        try:
            tid, outs = rh.run(WORKFLOW_ID, nodes, poll=3.0, timeout=600)
        except Exception as exc:                            # noqa: BLE001
            print(f"{t:<18}  失败 {type(exc).__name__}: {str(exc)[:60]}")
            continue
        dt = time.time() - t0

        cov = {}
        for o in outs:
            nid = str(o.get("nodeId"))
            if not args.all_nodes and nid != OUT_TARGET:
                continue
            dst = os.path.join(args.outdir, f"{t.replace(':','_').replace('/','_')}_{nid}.png")
            rh.download(o["fileUrl"], dst)
            a = np.asarray(Image.open(dst).convert("RGBA"), dtype=np.float64)
            cov[nid] = float((a[..., 3] > 127).mean())
        main_cov = cov.get(OUT_TARGET, float("nan"))
        inv_cov = cov.get("432", float("nan"))
        print(f"{t:<18}{main_cov * 100:>11.1f}%{inv_cov * 100:>11.1f}%{dt:>7.0f}s")

    print(f"\n产物在 {args.outdir}")


if __name__ == "__main__":
    main()
