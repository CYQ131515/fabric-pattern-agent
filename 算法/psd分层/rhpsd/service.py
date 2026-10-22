#!/usr/bin/env python3
"""PSD 分层 · 对外服务入口（给主程序调用，不是给人用的）。

为什么要有这一层
    主程序（PatternFlow 后端）刻意零依赖，只用标准库；这个算法要
    numpy + Pillow + psd-tools。绑在一起等于把"要不要装第三方包"
    从算法层扩散到整个服务。所以拆开：算法带自己的 venv，主程序起
    子进程调用，环境坏了只影响这一个功能。

契约（主程序只依赖这几条，改动必须向后兼容）
    命令
        python -m rhpsd.service run --input <图> --outdir <目录>
                                   --json-out <结果.json>
                                   [--max-layers N] [--no-analyze]
                                   [--task-timeout 秒] [--progress-out <进度.json>]
                                   [--layer 图层名=提示词] ...

        python -m rhpsd.service plan --input <图> --json-out <结果.json>
                                    [--max-layers N]
                                    [--progress-out <进度.json>]
                                    [--vlm-timeout 秒] [--vlm-retries N]

        --task-timeout  单个云端任务的等待上限（默认 180 秒）
        --progress-out  进度原子写到这里，主程序跑的时候会来读；
                        run： {phase, index, total, name, done, attempt, ts}
                        plan：{phase, label, total, ts}
                        phase ∈ reading / analyzing / done（plan）

        plan 是**提案**用的轻量命令：只调一次视觉模型看图定层，不跑云端
        工作流。它**永远返回 ok:true**（除非图本身读不出来）—— 看图失败
        就退回默认单层方案，在 note 里如实说明，绝不把整个提案拒掉。

    环境（由主程序注入，不落盘、不进配置文件）
        RUNNINGHUB_API_KEY  必填，跑 SAM3.1 工作流用
        DEEPSEEK_API_KEY    可选，分层诊断（看图决定分几层）用；
                            不给就跳过诊断，退回默认单层
        DEEPSEEK_BASE_URL / DEEPSEEK_VLM_MODEL  可选，覆盖诊断用的网关

    输出
        --json-out 里是一个 JSON 对象，永远有 "ok" 字段；
        失败时 {"ok": false, "error": "人话", "stage": "..."}。
        stdout 也打一份同样的 JSON，日志一律走 stderr。

    退出码
        0 = 跑完了（业务失败也算，看 JSON 的 ok）
        2 = 参数错误
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback

import numpy as np
from PIL import Image

from .pipeline import Config, run as run_pipeline
from .psd import verify_psd, write_psd


def _emit(payload: dict, json_out: str | None, code: int = 0) -> int:
    """结果写进 --json-out，同时 stdout 打一份。日志在 stderr，互不干扰。"""
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if json_out:
        tmp = json_out + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, json_out)      # 原子替换：读方永远看不到半截文件
    sys.stdout.write(text + "\n")
    sys.stdout.flush()
    return code


def _fail(json_out: str | None, error: str, stage: str) -> int:
    return _emit({"ok": False, "error": error, "stage": stage,
                  "trace": traceback.format_exc()[-600:]}, json_out)


def load_image(path: str) -> np.ndarray:
    im = Image.open(path)
    if im.mode == "RGBA":
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[3])
        im = bg
    elif im.mode != "RGB":
        im = im.convert("RGB")
    return np.asarray(im)


# --------------------------------------------------------------------- plan
def _plan_progress(path: str, **fields) -> None:
    """plan 阶段的进度落盘（原子替换，和 pipeline._progress 一个写法）。

    提案阶段看着"只调一次视觉模型"，但在网络不配合的时候这一次能等几十秒。
    界面在此之前完全没有信号，用户分不清"在跑"和"死了" —— 所以这里把
    "读到哪一步了"如实写出去，前端照着显示。
    纯旁观信息：写失败绝不影响提案，所以只吞 OSError。
    """
    if not path:
        return
    fields["ts"] = time.time()
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(fields, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def cmd_plan(args) -> int:
    """只看图不分图：跑一次视觉诊断，回答"这张图打算怎么分层"。

    费用只有一次视觉模型调用（几分钱），不跑云端工作流。给产品用做
    **提案**：先告诉用户会分成哪几层，他点头才花钱执行。

    ⚠️ 看图失败**不等于提案失败**。这里以前是 `_fail` 掉整个提案的，
    但同一段话术自己就写着"可以直接执行，会按默认单层来分"—— 自相矛盾，
    而且用户点的是"先给我看看方案"，看到一句错误弹回来完全不知道为什么。
    改成分层方案退回默认单层、照常把提案摆出去，只在 note 里如实说一句
    看图没跑成。提案本来就只是个预览，不该因为这个把人挡在门外。
    """
    prog_out = str(getattr(args, "progress_out", "") or "")
    _plan_progress(prog_out, phase="reading", label="正在读图")
    try:
        img = load_image(args.input)
    except Exception:                                      # noqa: BLE001
        return _fail(args.json_out, "这张图读不出来，换一张重新上传试试。", "input")

    cfg = Config(max_layers=max(1, int(args.max_layers or 4)),
                 vlm_provider=os.environ.get("DEEPSEEK_PROVIDER", "deepseek"),
                 vlm_base_url=os.environ.get("DEEPSEEK_BASE_URL", ""),
                 vlm_model=os.environ.get("DEEPSEEK_VLM_MODEL", ""))

    # 看图这一步的预算必须**自己收紧**，不能沿用 run 的那套（timeout 120 ×
    # 重试 3）。父进程给的进程级上限是 timeout_plan（默认 120 秒），沿用的话
    # 最坏耗时能到 369 秒 —— 客户端自己的超时永远轮不到生效，用户看到的是
    # 一路干等到父进程把它 kill 掉。这里按"提案要快"定：单次 45 秒、不重试。
    vlm_timeout = max(10, int(getattr(args, "vlm_timeout", 0) or 45))
    vlm_retries = max(1, int(getattr(args, "vlm_retries", 0) or 1))

    h, w = img.shape[:2]
    plan, note, detail = None, "", ""
    try:
        from . import analyze as analyze_mod
        base, model, key = analyze_mod.resolve(
            provider=cfg.vlm_provider, base_url=cfg.vlm_base_url,
            model=cfg.vlm_model)
        if not key:
            note = "没配视觉模型的 key"
        else:
            _plan_progress(prog_out, phase="analyzing",
                           label="正在请视觉模型看这张图、定分层")
            plan = analyze_mod.analyze(img, key, max_layers=cfg.max_layers,
                                       model=model, base_url=base,
                                       timeout=vlm_timeout, retries=vlm_retries)
    except Exception as exc:                               # noqa: BLE001
        # note 是给**界面**看的一句话，必须是人话 —— 网关地址、CLI 参数
        # 这些让人照着敲的东西一律不进 note，留给 note_detail 排错用。
        note = "看图这一步没跑成，先按默认单层给方案。"
        detail = "%s: %s" % (type(exc).__name__, exc)

    if not (plan and plan.get("layers")):
        # 退回默认方案 —— 提案照给，用户想执行照样能执行
        plan = {"layers": [{"name": "花型", "prompt": cfg.fallback_prompt}]}
        if not note:
            note = "没看出能单独成层的元素，先按默认单层给方案。"

    _plan_progress(prog_out, phase="done",
                   label="方案定了", total=len(plan.get("layers") or []))
    return _emit({
        "ok": True,
        "width": int(w), "height": int(h),
        "plan": plan,
        "note": note,
        "note_detail": detail,          # 只给排错看，不往界面上送
        "summary": "、".join(x["name"] for x in (plan.get("layers") or [])),
    }, args.json_out)


# ---------------------------------------------------------------------- run
def cmd_run(args) -> int:
    if not os.environ.get("RUNNINGHUB_API_KEY", "").strip():
        return _fail(args.json_out,
                     "没配 RunningHub 的 key，跑不了 SAM3.1 工作流。"
                     "到后台「密钥」里填上 runninghub_api_key 再试。", "config")

    try:
        img = load_image(args.input)
    except Exception:                                      # noqa: BLE001
        return _fail(args.json_out, "这张图读不出来，换一张重新上传试试。", "input")

    try:
        from .client import RunningHub
        rh = RunningHub(os.environ["RUNNINGHUB_API_KEY"].strip())
    except Exception:                                      # noqa: BLE001
        return _fail(args.json_out, "连不上 RunningHub，稍等一会儿再试。", "client")

    cfg = Config(max_layers=max(1, int(args.max_layers or 4)),
                 max_side=max(512, int(args.max_side or 2048)),
                 timeout=max(30.0, float(getattr(args, "task_timeout", 180.0) or 180.0)),
                 progress_out=str(getattr(args, "progress_out", "") or ""),
                 do_analyze=not args.no_analyze,
                 vlm_provider=os.environ.get("DEEPSEEK_PROVIDER", "deepseek"),
                 vlm_base_url=os.environ.get("DEEPSEEK_BASE_URL", ""),
                 vlm_model=os.environ.get("DEEPSEEK_VLM_MODEL", ""))
    if args.layer:
        manual = []
        for s in args.layer:
            if "=" in s:
                n, v = s.split("=", 1)
                manual.append({"name": n.strip(), "prompt": v.strip()})
        cfg.manual_layers = manual or None

    os.makedirs(args.outdir, exist_ok=True)
    try:
        res = run_pipeline(img, rh, cfg=cfg, workdir=args.outdir)
    except Exception as exc:                               # noqa: BLE001
        return _fail(args.json_out,
                     "分层没做完：%s（%s）" % (exc, type(exc).__name__), "pipeline")

    # 算法内部按 max_side 缩过图，回读校验要对着同一份尺寸比
    work = np.asarray(Image.fromarray(img).resize(
        (res.composite.shape[1], res.composite.shape[0]), Image.LANCZOS)
    ).astype(np.float64)

    psd_path = os.path.join(args.outdir, "分层.psd")
    try:
        write_psd(psd_path, res.rgbs, res.alphas, res.names)
        ver = verify_psd(psd_path, work, res.names)
    except Exception:                                      # noqa: BLE001
        return _fail(args.json_out, "PSD 写出来了但自检没过，这版不能用。", "verify")

    layers_dir = os.path.join(args.outdir, "layers")
    os.makedirs(layers_dir, exist_ok=True)
    outputs, rows = {}, []
    for i, name in enumerate(res.names):
        rgba = np.zeros((*res.rgbs[i].shape[:2], 4), dtype=np.uint8)
        rgba[..., :3] = np.clip(res.rgbs[i], 0, 255).astype(np.uint8)
        rgba[..., 3] = np.clip(res.alphas[i] * 255, 0, 255).astype(np.uint8)
        path = os.path.join(layers_dir, "%02d_%s.png" % (i, name))
        Image.fromarray(rgba, "RGBA").save(path)
        outputs["layer_%d" % i] = path
        rows.append({"index": i, "name": name,
                     "coverage": round(float((res.alphas[i] > 0.5).mean()), 4)})
    for key, arr in (("composite", res.composite), ("ground", res.ground)):
        path = os.path.join(args.outdir, key + ".png")
        Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).save(path)
        outputs[key] = path
    outputs["psd"] = psd_path

    # 合成 ≡ 原图是这套算法的立足点，掉了就不是"分层"而是"重画"，必须报出来
    max_diff = float(res.checks.get("stack_max_diff") or 0)
    return _emit({
        "ok": True,
        "outputs": outputs,
        "layers": rows,
        "plan": res.plan,
        "used_prompts": res.used_prompts,
        "checks": res.checks,
        "psd_check": ver,
        "n_tasks": rh.n_tasks,
        "log": res.log,
        "summary": "%d 层，合成 ≡ 原图 最大差 %s 灰阶" % (len(rows), max_diff),
    }, args.json_out)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="rhpsd.service")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--input", "-i", required=True)
    r.add_argument("--outdir", required=True)
    r.add_argument("--json-out", default="")
    r.add_argument("--max-layers", type=int, default=4)
    r.add_argument("--max-side", type=int, default=2048)
    # 单个云端任务的等待上限。主程序按"必须小于进程级上限"传进来。
    r.add_argument("--task-timeout", type=float, default=180.0)
    # 进度落盘位置（原子替换）。主程序在跑的时候会来读，用来告诉用户
    # "跑到第几层了、等了多久"。不给就不写。
    r.add_argument("--progress-out", default="")
    r.add_argument("--no-analyze", action="store_true")
    r.add_argument("--layer", action="append", default=[])
    r.set_defaults(func=cmd_run)

    q = sub.add_parser("plan")
    q.add_argument("--input", "-i", required=True)
    q.add_argument("--json-out", default="")
    q.add_argument("--max-layers", type=int, default=4)
    # 提案阶段的阶段进度（读图 → 问视觉模型 → 方案定了）。不给就不写。
    q.add_argument("--progress-out", default="")
    # 看图这一步的预算。必须自己收紧，理由见 cmd_plan 里的说明。
    q.add_argument("--vlm-timeout", type=int, default=45)
    q.add_argument("--vlm-retries", type=int, default=1)
    q.set_defaults(func=cmd_plan)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except SystemExit:
        raise
    except Exception:                                      # noqa: BLE001
        return _fail(getattr(args, "json_out", ""),
                     "分层服务内部出错：%s" % traceback.format_exc()[-300:], "fatal")


if __name__ == "__main__":
    sys.exit(main())
