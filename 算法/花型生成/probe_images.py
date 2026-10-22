#!/usr/bin/env python3
"""apimart 图像模型横评：同一九宫格提示词，比较出图质量/耗时/单张成本。

用法: python3 probe_images.py [模型名...]
产物: /tmp/apimart_test/<model>.png + 终端汇总表
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://api.apimart.ai"
KEY = os.environ.get("APIMART_KEY", "")

PROMPT = (
    "a 3 by 3 grid sheet with 9 separate isolated elements on a pure white background. "
    "traditional Chinese art + watercolor style, soft watercolor washes, pigment granulation, "
    "muted palette #c8434a, #3f5a56, #efe6d9, #2f3f3a, #e8b04b. "
    "Each cell contains exactly ONE element, centered with a generous white margin, "
    "elements strictly separated and never overlapping or crossing cell borders. "
    "cell 1: one fully blooming peony, top view; cell 2: one half-open peony; "
    "cell 3: one small peony bud with short stem; cell 4: a cluster of peony leaves and a curved stem; "
    "cell 5: one fully blooming peony; cell 6: one half-open peony; "
    "cell 7: a peony leaf sprig; cell 8: a small auspicious cloud motif; cell 9: another peony bud. "
    "Flat lay, even lighting, no shadow, sharp clean edges, no text, no labels, no grid lines, "
    "no borders, no frames, no collage."
)

MODELS = [
    "z-image-turbo",
    "seedream-5-0-flash",
    "seedream-5-0-lite",
    "qwen-image-3.0",
    "gemini-3.1-flash-image-preview",
    "gpt-image-1.5",
    "flux-2-flex",
    "mai-image-2.6-flash",
    "wan2.7-image",
    "grok-imagine-image",
]


def call(path: str, body: dict | None = None, timeout: int = 120):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def submit(model: str, size="1024x1024") -> str:
    """提交任务；按 400 报错逐步"减参数"（尺寸写法 / response_format / n / quality）。"""
    body = {"model": model, "prompt": PROMPT, "n": 1, "size": size, "response_format": "b64_json"}
    shed = ["response_format", "n", "quality", "resolution"]
    for _ in range(6):
        try:
            res = call("/v1/images/generations", body)
            break
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "replace")
            low = msg.lower()
            if "aspect ratio" in low or "size" in low and "ratio" in low:
                m = re.search(r"supported ratios:\s*([^\"]+)", msg)
                ratios = [x.strip() for x in m.group(1).split(",")] if m else []
                body["size"] = "1:1" if "1:1" in ratios or not ratios else ratios[0]
            elif "response format" in low or "response_format" in low:
                body.pop("response_format", None)
            elif 'parameter "size" is not supported' in low or "not supported for asynchronous" in low:
                body.pop("size", None)
            elif "resolution" in low:
                body["resolution"] = "1K"
            else:
                dropped = False
                for k in shed:
                    if k in body:
                        body.pop(k)
                        dropped = True
                        break
                if not dropped:
                    raise
    else:
        raise RuntimeError("参数降级失败")
    data = res.get("data")
    if isinstance(data, list) and data and isinstance(data[0], dict) and data[0].get("task_id"):
        return data[0]["task_id"]
    raise RuntimeError(json.dumps(res)[:400])


def poll(task_id: str, timeout: int = 240) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        res = call(f"/v1/tasks/{task_id}")
        d = res.get("data") or {}
        if d.get("status") in ("completed", "succeeded", "success"):
            return d
        if d.get("status") in ("failed", "error", "cancelled"):
            raise RuntimeError(f"task failed: {json.dumps(d)[:300]}")
        time.sleep(2.0)
    raise TimeoutError(f"task {task_id} timeout")


def download(url: str, path: Path) -> int:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    path.write_bytes(data)
    return len(data)


def main():
    global KEY
    KEY = KEY or os.environ.get("APIMART_KEY", "")
    if not KEY:
        sys.exit("需要 APIMART_KEY 环境变量")
    out = Path("/tmp/apimart_test")
    out.mkdir(parents=True, exist_ok=True)
    models = sys.argv[1:] or MODELS
    rows = []
    for m in models:
        t0 = time.time()
        rec = {"model": m}
        try:
            tid = submit(m)
            d = poll(tid)
            urls = (d.get("result") or {}).get("images") or []
            url = urls[0]["url"][0] if urls and isinstance(urls[0].get("url"), list) else None
            if not url:
                raise RuntimeError("no image url: " + json.dumps(d)[:300])
            n = download(url, out / f"{m}.png")
            rec.update(ok=True, cost=d.get("cost"), secs=round(time.time() - t0, 1),
                       bytes=n, latency_reported=d.get("actual_time"))
        except Exception as e:  # noqa: BLE001
            rec.update(ok=False, error=str(e)[:200], secs=round(time.time() - t0, 1))
        rows.append(rec)
        print(json.dumps(rec, ensure_ascii=False), flush=True)
    (out / "probe_results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
