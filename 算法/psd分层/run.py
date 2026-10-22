#!/usr/bin/env python3
"""花型图 → PSD 分层（SAM3.1 分割，RunningHub 云端工作流）。

用法
    export RUNNINGHUB_API_KEY=b52a...        # RunningHub，必填
    export DEEPSEEK_API_KEY=sk-...           # 分层诊断（DeepSeek 官网），可选

    python run.py -i 花型.jpg -o out/

    # 不诊断，手动指定两层
    python run.py -i 花型.jpg -o out/ --no-analyze \
        --layer "主体花=花朵:24" --layer "叶片=叶子:20"

    # 换视觉网关（任意 OpenAI 兼容的 /chat/completions 都行）
    python run.py -i 花型.jpg --vlm-provider dashscope --vlm-key sk-xxx
    python run.py -i 花型.jpg --vlm-base-url https://xxx/v1 \
                              --vlm-model 某视觉模型 --vlm-key sk-xxx

产出
    分层.psd          打开就是分层文件，中文图层名
    layers/           每层单独 RGBA PNG
    composite.png     合成结果（应该和原图一模一样）
    ground.png        补好的底布
    report.json       指标 + 诊断 + 每层提示词
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
from PIL import Image

from rhpsd import analyze as analyze_mod
from rhpsd.client import RunningHub, WORKFLOW_ID
from rhpsd.pipeline import Config, run as run_pipeline
from rhpsd.psd import verify_psd, write_psd


def load_image(path: str) -> np.ndarray:
    im = Image.open(path)
    if im.mode == "RGBA":
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[3])
        im = bg
    elif im.mode != "RGB":
        im = im.convert("RGB")
    return np.asarray(im)


def main() -> int:
    p = argparse.ArgumentParser(description="花型图 → PSD 分层（SAM3.1）")
    p.add_argument("-i", "--input", required=True)
    p.add_argument("-o", "--out", default="out")
    p.add_argument("--rh-key", default=os.environ.get("RUNNINGHUB_API_KEY", ""))
    # 视觉模型（分层诊断）。默认 DeepSeek 官网 deepseek-flash，给了 key 才诊断。
    p.add_argument("--vlm-key", default=os.environ.get(
        "VLM_API_KEY", os.environ.get("DEEPSEEK_API_KEY", "")))
    p.add_argument("--apimart-key", default="", help=argparse.SUPPRESS)
    p.add_argument("--vlm-provider", default=os.environ.get("VLM_PROVIDER") or "deepseek",
                   choices=sorted(analyze_mod.PROVIDERS),
                   help="预设网关：" + " / ".join(sorted(analyze_mod.PROVIDERS)))
    p.add_argument("--vlm-base-url", default=os.environ.get("VLM_BASE_URL", ""),
                   help="任意 OpenAI 兼容地址，会覆盖 --vlm-provider")
    p.add_argument("--vlm-model", default=os.environ.get("VLM_MODEL", ""),
                   help="视觉模型名，会覆盖 provider 默认模型")
    p.add_argument("--workflow", default=WORKFLOW_ID)
    p.add_argument("--max-layers", type=int, default=4)
    p.add_argument("--max-side", type=int, default=2048,
                   help="最长边超过这个值就缩小，省时间和 RH 币")
    p.add_argument("--instance", default="",
                   help="机器规格 default(24G)/plus(48G)/ultra(84G)")
    p.add_argument("--no-analyze", action="store_true", help="跳过 deepseek 诊断")
    p.add_argument("--layer", action="append", default=[],
                   help="手动指定层，格式 图层名=提示词，可重复；"
                        "给了就听你的，不再诊断")
    p.add_argument("--fallback-prompt", default="花朵:24")
    p.add_argument("--chain", action="store_true",
                   help="链式分割：下一层拿上一层扣完的图去分（只换分割输入，"
                        "像素仍取原图）。避免层间重复检出，但上一层错了会级联")
    p.add_argument("--chain-fill", choices=("ground", "white"), default="ground",
                   help="链式时抠掉的区域怎么填：ground=按图案周期补全（默认）"
                        " / white=填白")
    p.add_argument("--keep-raw", action="store_true",
                   help="保留工作流原始返回图，方便排查")
    args = p.parse_args()

    if not args.rh_key:
        print("缺 RUNNINGHUB_API_KEY（--rh-key 或环境变量）", file=sys.stderr)
        return 2

    img = load_image(args.input)
    h0, w0 = img.shape[:2]
    print(f"输入 {args.input}  {w0}x{h0}", file=sys.stderr)

    rh = RunningHub(args.rh_key)
    cfg = Config(workflow_id=args.workflow, max_layers=args.max_layers,
                 max_side=args.max_side, instance_type=args.instance,
                 do_analyze=not args.no_analyze,
                 fallback_prompt=args.fallback_prompt,
                 keep_raw=args.keep_raw,
                 vlm_provider=args.vlm_provider,
                 vlm_base_url=args.vlm_base_url,
                 vlm_model=args.vlm_model,
                 vlm_key=args.vlm_key or args.apimart_key,
                 chain=args.chain, chain_fill=args.chain_fill)

    if args.layer:                      # 手动指定层，不再诊断
        manual = []
        for s in args.layer:
            if "=" not in s:
                print(f"--layer 格式不对：{s}（应为 图层名=提示词）", file=sys.stderr)
                return 2
            n, v = s.split("=", 1)
            manual.append({"name": n.strip(), "prompt": v.strip()})
        cfg.manual_layers = manual

    outdir = os.path.abspath(args.out)
    os.makedirs(outdir, exist_ok=True)

    res = run_pipeline(img, rh, api_key=args.vlm_key or args.apimart_key,
                       cfg=cfg, workdir=outdir)

    work_img = np.asarray(Image.fromarray(img).resize(
        (res.composite.shape[1], res.composite.shape[0]), Image.LANCZOS)
    ).astype(np.float64)

    psd_path = os.path.join(outdir, "分层.psd")
    write_psd(psd_path, res.rgbs, res.alphas, res.names)
    ver = verify_psd(psd_path, work_img, res.names)

    layers_dir = os.path.join(outdir, "layers")
    os.makedirs(layers_dir, exist_ok=True)
    outputs = {"psd": psd_path}
    for i, name in enumerate(res.names):
        rgba = np.zeros((*res.rgbs[i].shape[:2], 4), dtype=np.uint8)
        rgba[..., :3] = np.clip(res.rgbs[i], 0, 255).astype(np.uint8)
        rgba[..., 3] = np.clip(res.alphas[i] * 255, 0, 255).astype(np.uint8)
        path = os.path.join(layers_dir, f"{i:02d}_{name}.png")
        Image.fromarray(rgba, "RGBA").save(path)
        outputs[f"layer_{i}_{name}"] = path
    for key, arr in (("composite", res.composite), ("ground", res.ground)):
        path = os.path.join(outdir, f"{key}.png")
        Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).save(path)
        outputs[key] = path

    print("\n" + "\n".join("· " + s for s in res.log))
    print()
    print("图层（从底到顶）")
    for i, n in enumerate(res.names):
        a = res.alphas[i]
        print(f"  [{i}] {n:<10} 不透明占比 {float((a > 0.5).mean()) * 100:5.1f}%")
    print()
    print(f"合成 ≡ 原图    最大差 {res.checks['stack_max_diff']} 灰阶 / "
          f"平均 {res.checks['stack_mean_diff']}")
    print(f"PSD 回读       {ver['layer_count']} 层，名称一致 {ver['names_match']}，"
          f"最大差 {ver['recon_max_diff']} 灰阶")
    print(f"工作流任务数   {rh.n_tasks}")

    report = {
        "ok": True,
        "input": os.path.abspath(args.input),
        "output_dir": outdir,
        "workflow": args.workflow,
        "plan": res.plan,
        # 实际试过的每一次提示词（含退化重试，可能和 plan 不同，排查用）
        "used_prompts": res.used_prompts,
        "layers": [{"index": i, "name": n} for i, n in enumerate(res.names)],
        "checks": res.checks,
        "psd_check": ver,
        "n_tasks": rh.n_tasks,
        "outputs": outputs,
        "log": res.log,
    }
    with open(os.path.join(outdir, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"\n产出目录 {outdir}")
    print("  分层.psd / layers/ / composite.png / ground.png / report.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
