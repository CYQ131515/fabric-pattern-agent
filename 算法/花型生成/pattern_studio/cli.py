"""pattern_studio.cli —— 端到端命令行（节点架构）

流水线是 10 个节点组成的图：
    knowledge → spec → prompts → assets → background → layout → compose → verify → review → deliver

其中 0 号 knowledge 节点会**自主读取 skill 知识库**（解析 references/*.md），
得出排布方案（骨式 / 接版 / 预设 / 旋转 / 层叠），后续节点按这份方案工作；
skill 改了会自动重新学习（按文件 sha256 判断过期）。

    python -m pattern_studio learn                 # 只学习 knowledge 库
    python -m pattern_studio run "国风水彩牡丹 疏朗"   # 完整流水线（默认会用知识库）
    python -m pattern_studio run "..." --no-kb     # 不用知识库（完全按参数/默认值）
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from . import kb as kb_mod, nodes, prompts, spec as spec_mod
from .util import log as _log

# 模型档位（成本为实测单张价）
PROFILES = {
    "cheapest": {"sheet": "gpt-image-2", "bg": "gpt-image-2", "llm": "deepseek-v4-flash", "quality": "medium"},
    "balanced": {"sheet": "gpt-image-2", "bg": "z-image-turbo", "llm": "deepseek-v4-flash", "quality": "medium"},
    "fast":     {"sheet": "z-image-turbo", "bg": "z-image-turbo", "llm": "deepseek-v4-flash", "quality": None},
    "best":     {"sheet": "mai-image-2.6-flash", "bg": "flux-2-pro", "llm": "gpt-5-nano", "quality": None},
}


def load_dotenv(path: Path | None = None) -> None:
    """极简 .env 读取（不覆盖已有环境变量）。"""
    p = path or Path(__file__).resolve().parent.parent / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    print("[env] 已载入 .env", flush=True)


# ------------------------------------------------------------------ run
def run(args) -> int:
    from . import imagegen

    t0 = time.time()
    out = Path(args.out).expanduser() if args.out else None
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)

    prof = PROFILES.get(args.profile, PROFILES["balanced"])
    provider = imagegen.make_provider(args.provider)
    if args.provider in ("apimart", "auto") and getattr(provider, "name", "") == "apimart":
        provider.sheet_model = args.model_sheet or prof["sheet"]
        provider.bg_model = args.model_bg or prof["bg"]
        provider.llm_model = args.model_llm or prof["llm"]
        provider.quality = prof["quality"]
        _log(f"模型档位 {args.profile}: 九宫格={provider.sheet_model} 背景={provider.bg_model} 需求解析={provider.llm_model}")

    ctx = {"args": args, "out": out, "t0": t0, "provider": provider,
           "profile": prof, "trace": []}
    ctx = nodes.default_graph().run(ctx)
    return 0 if ctx.get("ok_all") else 1


# ------------------------------------------------------------------ learn
def cmd_learn(args) -> int:
    sdir = Path(args.skill_dir).expanduser() if args.skill_dir else None
    kk = kb_mod.learn(sdir, verbose=True)
    if args.json:
        print(json.dumps({k: (len(v) if isinstance(v, list) else v) for k, v in kk.items()
                          if not k.startswith("_")}, ensure_ascii=False, indent=1))
    plan = kb_mod.plan(" ".join(args.example or ["国风碎花 满版"]))
    print("[知识节点] 示例判定：" + kb_mod.explain(plan))
    print(f"[知识节点] 知识库文件：{kb_mod.kb_path()}")
    return 0


# ------------------------------------------------------------------ 解析器
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pattern_studio",
                                 description="关键词 → 九宫格元素 → 三版无缝花型图稿（节点架构 + skill 知识库）")
    sub = ap.add_subparsers(dest="cmd")

    r = sub.add_parser("run", help="完整流水线（10 个节点）")
    r.add_argument("keywords", help='需求关键词，如 "国风水彩牡丹 疏朗"')
    r.add_argument("-o", "--out", default=None, help="输出目录（默认 out/<标题>）")
    r.add_argument("--prints", type=int, default=None, help="出几版（默认 3）")
    r.add_argument("--tile", type=int, nargs=2, default=None, metavar=("W", "H"), help="tile 尺寸 px")
    r.add_argument("--density", type=float, default=None, help="疏密度 0–1（越大越满）")
    r.add_argument("--presets", default=None,
                   help="逗号分隔的排版预设（默认由知识库决定）：grid,halfdrop,brick,diagonal,radial,stripes,phyllotaxis,scatter")
    r.add_argument("--provider", default="auto", choices=["auto", "mock", "openai", "comfy", "apimart"])
    r.add_argument("--profile", default="balanced", choices=list(PROFILES), help="apimart 模型档位")
    r.add_argument("--model-sheet", default=None, help="覆盖九宫格模型")
    r.add_argument("--model-bg", default=None, help="覆盖背景模型")
    r.add_argument("--model-llm", default=None, help="覆盖需求解析模型")
    r.add_argument("--bg-count", type=int, default=1, help="AI 背景张数")
    r.add_argument("--gen-size", type=int, default=1024, help="生图分辨率")
    r.add_argument("--seed", type=int, default=7)
    r.add_argument("--gap", type=float, default=0.12, help="元素最小间距（相对基准边长）")
    r.add_argument("--evenness", type=float, default=0.8, help="排布均匀度 0–1")
    r.add_argument("--rotate-deg", type=float, default=None, help="允许的随机旋转角（默认由知识库决定）")
    r.add_argument("--mirror-prob", type=float, default=None, help="随机镜像概率（默认由知识库决定）")
    r.add_argument("--review", default="auto", choices=["off", "auto", "strict"], help="AI 审核")
    r.add_argument("--review-triage-model", default="gemini-3-flash-preview")
    r.add_argument("--review-deep-model", default="gpt-5-mini")
    r.add_argument("--review-rounds", type=int, default=None)
    r.add_argument("--review-no-autofix", action="store_true")
    r.add_argument("--spec-file", default=None, help="直接读手写 spec")
    r.add_argument("--count", type=int, default=None)
    r.add_argument("--element-scale", type=float, default=None,
                   help="元素整体缩放（默认由知识库配方决定）")
    r.add_argument("--overlap-allow", default=None, help='如 "0:0.10,1:0.35,2:0.65"')
    r.add_argument("--overlap-mode", default=None, choices=["none", "layered"],
                   help="叠层模式（默认由知识库决定：重叠式骨式会自动开）")
    r.add_argument("--bg-color", default=None)
    r.add_argument("--bg-mode", default="auto", choices=["auto", "cover", "mirror", "tile", "flat"])
    r.add_argument("--bg-fade", type=float, default=None)
    r.add_argument("--bg-desaturate", type=float, default=0.35)
    r.add_argument("--shadow", action="store_true")
    r.add_argument("--svg", action="store_true")
    r.add_argument("--elements-dir", default=None)
    r.add_argument("--sheets-dir", default=None)
    r.add_argument("--reuse", action="store_true")
    r.add_argument("--no-llm", action="store_true")
    r.add_argument("--llm-prompts", action="store_true")
    # 知识节点
    r.add_argument("--no-kb", action="store_true", help="不读 skill 知识库（纯参数驱动）")
    r.add_argument("--skill-dir", default=None, help="skill 目录（默认 ~/.agents/skills/pattern-design-atlas）")
    r.add_argument("--no-review", action="store_true", help="（同 --review off）")
    r.set_defaults(func=run)

    lr = sub.add_parser("learn", help="学习 skill 知识库（解析 references/*.md）")
    lr.add_argument("--skill-dir", default=None)
    lr.add_argument("--example", nargs="*", help="用这段关键词试跑一次判定")
    lr.add_argument("--json", action="store_true", help="打印解析统计")
    lr.set_defaults(func=cmd_learn)

    s = sub.add_parser("spec", help="只看需求解析结果")
    s.add_argument("keywords")
    s.add_argument("--no-llm", action="store_true")
    s.set_defaults(func=lambda a: print(json.dumps(spec_mod.analyze(a.keywords, use_llm=not a.no_llm),
                                                  ensure_ascii=False, indent=1)))

    p = sub.add_parser("prompts", help="只看生成的提示词")
    p.add_argument("keywords")
    p.add_argument("--seed", type=int, default=7)
    p.set_defaults(func=lambda a: print(json.dumps(
        prompts.all_prompts(spec_mod.analyze(a.keywords), seed=a.seed), ensure_ascii=False, indent=1)))
    return ap


def main(argv=None) -> int:
    load_dotenv()
    ap = build_parser()
    a = ap.parse_args(argv)
    if not getattr(a, "cmd", None):
        ap.print_help()
        return 1
    if getattr(a, "no_review", False):
        a.review = "off"
    if getattr(a, "skill_dir", None):
        os.environ["PATTERN_STUDIO_SKILL_DIR"] = str(Path(a.skill_dir).expanduser())
    return int(a.func(a) or 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
