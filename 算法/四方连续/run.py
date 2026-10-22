#!/usr/bin/env python3
"""四方连续生成器 · 命令行入口。

用法
    # 纯算法（不需要 key，结果确定性、零成本）
    python run.py -i 花型.jpg -o out/

    # 全流程：诊断 + 补缝（两条线各走各的网关）
    python run.py -i 花型.jpg -o out/ --key sk-出图网关的key \
        --analyze-key sk-deepseek官方的key

    # key 也可以走环境变量
    export APIMART_API_KEY=sk-出图网关的key
    export DEEPSEEK_API_KEY=sk-deepseek官方的key
    python run.py -i 花型.jpg -o out/

产出（都在 -o 目录下）
    tile.png          最终四方连续单元，拿去印花的就是这张
    preview_3x3.png   3x3 平铺预览，肉眼查接缝
    compare.png       原图 / 纯算法 / 本次结果 三方对比
    to_edit.png       送进模型的待修补图（接缝已推到中央十字）
    mask.png          给模型的中央十字带 mask（渠道不支持时模型收不到）
    model_output.png  模型原始输出（未经还原，用于复盘）
    report.json       全部指标与诊断结果
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from sifang import Config, run
from sifang.apimart import (ANALYZE_MODEL, DEFAULT_ANALYZE_BASE, DEFAULT_BASE,
                            IMAGE_MODEL, APIMart)
from sifang.io_utils import load_image, save_image
from sifang import metrics


def main() -> int:
    p = argparse.ArgumentParser(description="把一张花型图变成四方连续图案")
    p.add_argument("-i", "--input", required=True, help="输入花型图")
    p.add_argument("-o", "--out", default="out", help="输出目录")
    p.add_argument("--key", default=os.environ.get("APIMART_API_KEY", ""),
                   help="出图网关的 key，也可用环境变量 APIMART_API_KEY")
    p.add_argument("--base-url", default=os.environ.get("APIMART_BASE_URL", DEFAULT_BASE),
                   help="出图网关地址")
    p.add_argument("--analyze-key", default=os.environ.get("DEEPSEEK_API_KEY", ""),
                   help="诊断模型的 key；不给就沿用出图那套（单网关模式）")
    p.add_argument("--analyze-base", default=os.environ.get("DEEPSEEK_BASE_URL",
                                                            DEFAULT_ANALYZE_BASE),
                   help="诊断模型的网关地址，默认 DeepSeek 官方")
    p.add_argument("--no-model", action="store_true", help="只跑纯算法，不调模型")
    p.add_argument("--no-analyze", action="store_true", help="跳过诊断模型")
    p.add_argument("--analyze-model", default=None, help="覆盖诊断模型")
    p.add_argument("--image-model", default=None, help="覆盖出图模型")
    p.add_argument("--candidates", type=int, default=1,
                   help="出图跑几个候选挑最优（模型不可复现，建议 2~3）")
    p.add_argument("--mask-band", type=float, default=0.07,
                   help="中央十字带宽度占比，越大给模型的自由度越高")
    p.add_argument("--band", type=float, default=0.12, help="纯算法羽化带占比")
    p.add_argument("--align-band", type=float, default=0.03, help="收尾对齐带占比")
    p.add_argument("--keep-size", action="store_true",
                   help="最终 tile 保持处理尺寸，不缩回原图尺寸")
    args = p.parse_args()

    img = load_image(args.input)
    cfg = Config(band_frac=args.band, align_frac=args.align_band,
                 mask_band_frac=args.mask_band, n_candidates=args.candidates,
                 do_analyze=not args.no_analyze,
                 restore_original_size=not args.keep_size)
    if args.analyze_model:
        cfg.analyze_model = args.analyze_model
    if args.image_model:
        cfg.image_model = args.image_model

    client = None
    if not args.no_model:
        if not args.key:
            print("没给 key，且没有 APIMART_API_KEY 环境变量。", file=sys.stderr)
            print("要么加 --key，要么加 --no-model 只跑纯算法。", file=sys.stderr)
            return 2
        client = APIMart(args.key, base_url=args.base_url,
                         analyze_key=args.analyze_key,
                         analyze_base=args.analyze_base)
        if client.split_lines:
            print(f"诊断 {cfg.analyze_model} @ {client.analyze_base}")
            print(f"出图 {cfg.image_model} @ {client.image_base}")
        else:
            print(f"网关 {client.image_base}")
            print(f"诊断模型 {cfg.analyze_model} · 出图模型 {cfg.image_model}")

    res = run(img, client=client, cfg=cfg)

    os.makedirs(args.out, exist_ok=True)
    save_image(res.tile, os.path.join(args.out, "tile.png"))
    save_image(res.preview, os.path.join(args.out, "preview_3x3.png"))
    save_image(res.compare, os.path.join(args.out, "compare.png"))
    for name in ("to_edit", "mask", "model_output", "baseline"):
        if name in res.intermediates:
            save_image(res.intermediates[name], os.path.join(args.out, f"{name}.png"))

    print("\n" + "\n".join("· " + s for s in res.log))
    print()
    print(metrics.seam_report_text(res.report))
    if res.fidelity is not None:
        print()
        print(metrics.fidelity_text(res.fidelity))
    print(f"\n走的路线: {res.route}")

    if res.analysis:
        print("\n大模型诊断")
        a = res.analysis
        print(f"  图案类型    {a.get('pattern_type', '-')}")
        print(f"  地比例      {a.get('density', '-')}")
        print(f"  适合平铺    {a.get('suitable', '-')}")
        for s in a.get("main_seam_problems") or []:
            print(f"  · {s}")
        print(f"  补缝策略    {a.get('repair_strategy', '-')}")
        print(f"  图像指令    {res.prompt[:160]}")
    if client is not None:
        print(f"\n本次出图成本  {client.cost_total:.4f}")

    report = {
        "input": os.path.abspath(args.input),
        "output_dir": os.path.abspath(args.out),
        "route": res.route,
        "seam_report": res.report.as_dict(),
        "baseline_report": res.baseline_report.as_dict(),
        "fidelity": res.fidelity.as_dict() if res.fidelity else None,
        "analysis": res.analysis,
        "image_prompt": res.prompt,
        "log": res.log,
        "cost": client.cost_total if client else 0.0,
    }
    with open(os.path.join(args.out, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"\n产出目录 {os.path.abspath(args.out)}")
    print("  tile.png / preview_3x3.png / compare.png / report.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
