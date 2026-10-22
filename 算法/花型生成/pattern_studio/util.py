"""共用小工具（日志 / 颜色 / 预设 / 参数解析）。"""
from __future__ import annotations

import math
import random
import re
import time
from pathlib import Path


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def slugify(text: str, maxlen: int = 24) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", (text or "").strip())
    return (s[:maxlen] or "pattern").strip("_")


def hex2rgb(h: str) -> tuple[int, int, int]:
    h = (h or "#000000").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


ALL_PRESETS = ["grid", "halfdrop", "brick", "diagonal", "radial", "stripes", "phyllotaxis", "scatter"]


def pick_presets(name: str | None, n: int, default: list[str] | None = None) -> list[str]:
    """把 --presets 解析成 n 个预设；不够就循环补。"""
    if not name:
        base = list(default or ["halfdrop", "phyllotaxis", "scatter"])
    else:
        base = [p.strip() for p in str(name).split(",") if p.strip()]
        base = [p for p in base if p in ALL_PRESETS] or list(default or ["halfdrop"])
    return [base[i % len(base)] for i in range(max(1, n))]


def parse_overlap_allow(text: str | None) -> dict | None:
    """解析 "0:0.10,1:0.35,2:0.62" → {0:0.10, 1:0.35, 2:0.62}"""
    if not text:
        return None
    out: dict[int, float] = {}
    for part in str(text).split(","):
        part = part.strip()
        if ":" not in part:
            continue
        k, v = part.split(":", 1)
        try:
            out[int(k.strip())] = float(v.strip())
        except ValueError:
            continue
    return out or None
