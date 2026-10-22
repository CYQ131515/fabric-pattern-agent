"""SAM3.1 提示词的稳定性和写法。

两件事：
1. 同一个提示词重复跑，结果稳不稳（不稳就没法交付）
2. 能不能绕过 RH_Translator，直接给节点 436 喂英文

用法
    RUNNINGHUB_API_KEY=xxx python tools/probe_repeat.py
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rhpsd.client import (IMAGE_FIELD, IMAGE_NODE, TEXT_FIELD, TEXT_NODE,
                          WORKFLOW_ID, RunningHub)

TARGET = "432"          # 实测：目标层在这个节点（Mask Invert 之后）


def coverage(rh: RunningHub, outs: list[dict], outdir: str, tag: str) -> dict:
    cov = {}
    for o in outs:
        nid = str(o.get("nodeId"))
        dst = os.path.join(outdir, f"{tag}_{nid}.png")
        rh.download(o["fileUrl"], dst)
        a = np.asarray(Image.open(dst).convert("RGBA"), dtype=np.float64)
        cov[nid] = float((a[..., 3] > 127).mean())
    return cov


def main() -> None:
    rh = RunningHub()
    outdir = os.path.join(ROOT, "out/probe_repeat")
    os.makedirs(outdir, exist_ok=True)

    im = Image.open(os.path.join(ROOT, "inputs/深蓝底水彩花卉.jpeg")).convert("RGB")
    src = os.path.join(outdir, "_input.png")
    im.save(src)
    fn = rh.upload(src)

    cases = [
        ("重复花朵24-1", [{"nodeId": TEXT_NODE, "fieldName": TEXT_FIELD,
                          "fieldValue": "花朵:24"}]),
        ("重复花朵24-2", [{"nodeId": TEXT_NODE, "fieldName": TEXT_FIELD,
                          "fieldValue": "花朵:24"}]),
        ("重复花朵24-3", [{"nodeId": TEXT_NODE, "fieldName": TEXT_FIELD,
                          "fieldValue": "花朵:24"}]),
        ("花朵32(默认)", [{"nodeId": TEXT_NODE, "fieldName": TEXT_FIELD,
                          "fieldValue": "花朵:32"}]),
        ("绕翻译直接喂436", [{"nodeId": "436", "fieldName": "text",
                            "fieldValue": "flower"}]),
        ("绕翻译+count", [{"nodeId": "436", "fieldName": "text",
                          "fieldValue": "flower:24"}]),
    ]

    print(f"{'用例':<16}{'432目标':>9}{'440反相':>9}{'耗时':>7}   判定")
    print("-" * 58)
    for tag, extra in cases:
        nodes = [{"nodeId": IMAGE_NODE, "fieldName": IMAGE_FIELD,
                  "fieldValue": fn}] + extra
        t0 = time.time()
        try:
            _, outs = rh.run(WORKFLOW_ID, nodes, poll=3.0, timeout=600)
        except Exception as exc:                            # noqa: BLE001
            print(f"{tag:<16}  失败 {type(exc).__name__}: {str(exc)[:50]}")
            continue
        dt = time.time() - t0
        cov = coverage(rh, outs, outdir, tag)
        t = cov.get(TARGET, float("nan"))
        i = cov.get("440", float("nan"))
        verdict = ("退化:全选" if t > 0.85 else
                   "退化:空" if t < 0.003 else "ok")
        print(f"{tag:<16}{t * 100:>8.1f}%{i * 100:>8.1f}%{dt:>6.0f}s   {verdict}")

    print(f"\n产物在 {outdir}")


if __name__ == "__main__":
    main()
