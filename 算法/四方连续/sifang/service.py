#!/usr/bin/env python3
"""四方连续 · 对外服务入口（给主程序调用，不是给人用的）。

为什么要单独开一个入口，而不是让主程序直接 import
    主程序（PatternFlow 后端）是**刻意零依赖**的：它只用标准库，连 JPEG 解码
    都是自己调 sips 转 PNG 再自研解码。这个算法要 numpy + Pillow，两者绑在一起
    等于把"要不要装第三方包"这件事从算法层扩散到整个服务。
    所以拆开：算法带自己的 venv，主程序按需起子进程调用。环境没装好只影响
    这一个功能，不会把整个服务拖垮；算法也能自己单独升级。

契约（主程序只依赖这几条，改动必须向后兼容）
    命令
        python -m sifang.service probe --input <图> --json-out <结果.json>
        python -m sifang.service run   --input <图> --outdir <目录> --json-out <结果.json>
                                      [--key <APIKey>] [--no-model] [--candidates N]

    输出
        --json-out 指定的文件里是一个 JSON 对象，永远有 "ok" 字段；
        失败时是 {"ok": false, "error": "人话", "stage": "..."}。
        stdout 也打一份同样的 JSON（方便手工跑），日志一律走 stderr。

    退出码
        0 = 成功（业务失败也算，看 JSON 里的 ok）
        2 = 参数错误
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback

from . import metrics
from .apimart import (ANALYZE_MODEL, DEFAULT_ANALYZE_BASE, DEFAULT_BASE,
                      IMAGE_MODEL, APIMart)
from .io_utils import load_image, save_image
from .pipeline import Config, run as run_pipeline
from .probe import probe as probe_image


def _emit(payload: dict, json_out: str | None, code: int = 0) -> int:
    """把结果写进 --json-out，同时打一份到 stdout。日志都在 stderr，互不干扰。"""
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if json_out:
        tmp = json_out + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, json_out)          # 原子替换：读方永远看不到半截文件
    sys.stdout.write(text + "\n")
    sys.stdout.flush()
    return code


def _fail(json_out: str | None, error: str, stage: str, code: int = 0) -> int:
    return _emit({"ok": False, "error": error, "stage": stage,
                  "trace": traceback.format_exc()[-600:]}, json_out, code)


# --------------------------------------------------------------------- probe
def cmd_probe(args) -> int:
    img = load_image(args.input)
    info = probe_image(img, band_frac=args.band, align_frac=args.align_band,
                       sharp_band_frac=args.sharp_band)
    return _emit({"ok": True, **info}, args.json_out)


# ----------------------------------------------------------------------- run
def cmd_run(args) -> int:
    img = load_image(args.input)
    w0, h0 = img.shape[1], img.shape[0]

    # 体检先跑：它的结论要一并放进报告，主程序也靠它决定要不要调模型
    info = probe_image(img, band_frac=args.band, align_frac=args.align_band,
                       sharp_band_frac=args.sharp_band)

    use_model = not args.no_model and bool(args.key)
    client = None
    if use_model:
        client = APIMart(args.key, base_url=args.base_url,
                         analyze_key=args.analyze_key,
                         analyze_base=args.analyze_base)
        am = args.analyze_model or ANALYZE_MODEL
        im = args.image_model or IMAGE_MODEL
        if client.split_lines:
            print("诊断 %s @ %s · 出图 %s @ %s"
                  % (am, client.analyze_base, im, client.image_base), file=sys.stderr)
        else:
            print("单网关 %s · 诊断 %s · 出图 %s"
                  % (client.image_base, am, im), file=sys.stderr)
    elif not args.no_model and not args.key:
        print("没给 key，只跑纯算法", file=sys.stderr)

    cfg = Config(band_frac=args.band, align_frac=args.align_band,
                 mask_band_frac=args.mask_band, n_candidates=args.candidates,
                 do_analyze=not args.no_analyze,
                 restore_original_size=not args.keep_size,
                 sharp_band_frac=args.sharp_band)
    if args.analyze_model:
        cfg.analyze_model = args.analyze_model
    if args.image_model:
        cfg.image_model = args.image_model

    res = run_pipeline(img, client=client, cfg=cfg)

    outdir = os.path.abspath(args.outdir)
    os.makedirs(outdir, exist_ok=True)
    outputs = {}
    for key, arr in (("tile", res.tile), ("preview", res.preview),
                     ("compare", res.compare)):
        path = os.path.join(outdir, f"{key}.png")
        save_image(arr, path)
        outputs[key] = path
    for name in ("to_edit", "mask", "model_output", "baseline"):
        if name in res.intermediates:
            path = os.path.join(outdir, f"{name}.png")
            save_image(res.intermediates[name], path)
            outputs[name] = path

    for line in res.log:
        print("· " + line, file=sys.stderr)

    report = {
        "ok": True,
        "route": res.route,
        "engine": "model" if res.route == "model" else "pure-algorithm",
        "input": {"width": w0, "height": h0},
        "working_size": info["working_size"],
        "probe": info,
        "seam_report": res.report.as_dict(),
        "baseline_report": res.baseline_report.as_dict(),
        "fidelity": res.fidelity.as_dict() if res.fidelity else None,
        "analysis": res.analysis,
        "image_prompt": res.prompt,
        "models": {"analyze": cfg.analyze_model if client else None,
                   "image": cfg.image_model if client else None},
        "gateways": ({"analyze": client.analyze_base, "image": client.image_base,
                      "split": client.split_lines, "mask": client.mask_supported}
                     if client else None),
        "outputs": outputs,
        "log": res.log,
        "cost": round(client.cost_total, 4) if client else 0.0,
    }
    # 报告落盘一份，便于事后复盘（主程序不依赖它，是人查用）
    with open(os.path.join(outdir, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return _emit(report, args.json_out)


# ---------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sifang.service",
                                description="四方连续生成器 · 对外服务入口")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("-i", "--input", required=True, help="输入花型图")
        sp.add_argument("--json-out", default=None, help="结果 JSON 写到这个文件")
        sp.add_argument("--band", type=float, default=0.12, help="纯算法羽化带占比")
        sp.add_argument("--align-band", type=float, default=0.03, help="收尾对齐带占比")
        sp.add_argument("--sharp-band", type=float, default=0.08, help="保析度统计带占比")

    sp = sub.add_parser("probe", help="只做输入体检，不调模型、零成本")
    common(sp)
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("run", help="完整执行：算法搭骨架 + 模型补缝 + 出图")
    common(sp)
    sp.add_argument("-o", "--outdir", required=True, help="产出目录")
    sp.add_argument("--key", default=os.environ.get("APIMART_API_KEY", ""),
                    help="出图网关的 key，也可用环境变量 APIMART_API_KEY")
    sp.add_argument("--base-url", default=os.environ.get("APIMART_BASE_URL", DEFAULT_BASE),
                    help="出图网关地址")
    sp.add_argument("--analyze-key", default=os.environ.get("DEEPSEEK_API_KEY", ""),
                    help="诊断模型的 key；不给就沿用出图那套（单网关模式）")
    sp.add_argument("--analyze-base", default=os.environ.get("DEEPSEEK_BASE_URL",
                                                             DEFAULT_ANALYZE_BASE),
                    help="诊断模型的网关地址，默认 DeepSeek 官方")
    sp.add_argument("--no-model", action="store_true", help="只跑纯算法，不调模型")
    sp.add_argument("--no-analyze", action="store_true", help="跳过诊断模型")
    sp.add_argument("--analyze-model", default=None)
    sp.add_argument("--image-model", default=None)
    sp.add_argument("--candidates", type=int, default=1,
                    help="出图跑几个候选挑最优（模型不可复现，建议 2~3）")
    sp.add_argument("--mask-band", type=float, default=0.07,
                    help="中央十字带宽度占比，越大给模型的自由度越高")
    sp.add_argument("--keep-size", action="store_true",
                    help="最终 tile 保持处理尺寸，不缩回原图尺寸")
    sp.set_defaults(func=cmd_run)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        return _fail(args.json_out, "读不到输入图：%s" % exc, "load")
    except Exception as exc:                       # noqa: BLE001
        return _fail(args.json_out, "%s: %s" % (type(exc).__name__, exc), "run")


if __name__ == "__main__":
    sys.exit(main())
