#!/usr/bin/env python3
"""对**已经跑完的产物目录**做一次相似去重（离线、秒级、不调模型）。

为什么需要单独一个工具：
    相似去重已经接在产线里（`similar.py`），新跑的批次自动带。但之前
    跑完的那些目录（比如用户反馈的那批纸雕花卉）是旧代码落的，里面
    还留着"同一张图的两三个副本"。重跑一遍要十几分钟，不如就地清一遍 ——
    这一步只读图算指纹，不联网、不调模型。

默认**只报告**，不动文件。确认要删再加 `--apply`：

    ./.venv/bin/python tools/dedup.py --outdir 产物/out_1017_1550
    ./.venv/bin/python tools/dedup.py --outdir 产物/out_1017_1550 --apply

`--apply` 走的顺序和产线一致：**先留档（rejected.json / _rejected）→ 再删
→ 最后重建索引**。反了就只能留一堆空壳，或者索引里挂着死条目。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wsearch import screen, service, similar, store                      # noqa: E402


def main():
    p = argparse.ArgumentParser(description="对已有产物做相似去重（默认只报告）")
    p.add_argument("--outdir", required=True)
    p.add_argument("--sim", type=float, default=similar.NEAR,
                   help="相似阈值（默认 %.3f，越低越狠）" % similar.NEAR)
    p.add_argument("--apply", action="store_true", help="真的删（默认只报告）")
    a = p.parse_args()

    outdir = service._outdir(a.outdir)
    if not os.path.exists(os.path.join(outdir, "index.json")):
        print("这个目录里没有 index.json：%s" % outdir)
        return 1

    recs, _info = service._existing_records(outdir)
    if not recs:
        print("目录里没有可读的图。")
        return 1
    kept, drop = similar.dedup(recs, near=a.sim, log=print)
    print("\n%s" % outdir)
    print("  %d 张 → 留 %d 张，剔 %d 张" % (len(recs), len(kept), len(drop)))
    for r in sorted(drop, key=lambda x: -x["sim"]):
        it = r.get("index_item") or {}
        print("    %s ← %s  %.4f"
              % (it.get("id", "?")[-4:], (r.get("near_of") or "?")[-4:], r["sim"]))

    if not drop:
        return 0
    if not a.apply:
        print("\n这只是报告。要真删加 --apply（会先留档到 rejected.json / _rejected/）")
        return 0

    screen.dump_rejected(drop, outdir, append=True, log=print)
    gone = service._drop_bad(outdir, [r for r in drop if r.get("index_item")])
    index, paths, st = store.append([], outdir, log=print)
    print("\n删了 %d 张，现在 %d 张。索引：%s"
          % (len(gone), len(index.get("items") or []), paths["index"]))
    print("被删的图和理由在 rejected.json（可以整个删）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
