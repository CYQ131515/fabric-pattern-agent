"""节点架构：把花型流水线拆成 10 个可观察节点。

    knowledge → spec → prompts → assets → background
              → layout → compose → verify → review → deliver

每个节点：读 ctx、改 ctx、只做一件事；节点自带日志与耗时，方便定位问题。
0 号 knowledge 节点负责"学习 skill"：解析 references/*.md → 知识库 → 排布方案（plan），
后面的 layout / verify / review / deliver 都会用这份 plan 工作。
"""
from __future__ import annotations

import json
import random
import shutil
import time
from pathlib import Path

from PIL import Image

from . import compose, imagegen, kb as kb_mod, layout, prompts, review, sheet, spec as spec_mod, verify
from .util import (ALL_PRESETS, hex2rgb, log, parse_overlap_allow, pick_presets, slugify)


class Node:
    def __init__(self, name: str, fn, desc: str = ""):
        self.name = name
        self.fn = fn
        self.desc = desc

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Node {self.name}>"


class Graph:
    """极简顺序图：节点按注册顺序执行，共享 ctx。"""

    def __init__(self, nodes: list[Node]):
        self.nodes = nodes

    def run(self, ctx: dict) -> dict:
        log("节点图：" + " → ".join(n.name for n in self.nodes))
        for n in self.nodes:
            t0 = time.time()
            log(f"[{n.name}] {n.desc or '执行'}")
            out = n.fn(ctx)
            ctx = out or ctx
            ctx.setdefault("_trace", []).append({"node": n.name, "secs": round(time.time() - t0, 2)})
        return ctx


# ------------------------------------------------------------------ 0. 知识
def node_knowledge(ctx: dict) -> dict:
    a = ctx["args"]
    if getattr(a, "no_kb", False):
        log("  跳过知识库（--no-kb）")
        ctx["kb"] = None
        ctx["plan"] = None
        return ctx
    kk = kb_mod.ensure_fresh(verbose=True)
    plan = kb_mod.plan(getattr(a, "keywords", "") or "", ctx.get("spec_preview") or {})
    ctx["kb"] = kk
    ctx["plan"] = plan
    log("  排布方案：" + kb_mod.explain(plan))
    if plan.get("pitfalls"):
        for p in plan["pitfalls"][:4]:
            log(f"  · 避坑：{p['text'][:60]}")
    return ctx


# ------------------------------------------------------------------ 1. 需求
def node_spec(ctx: dict) -> dict:
    a = ctx["args"]
    provider = ctx["provider"]
    out = ctx.get("out")
    if a.reuse and out is None:
        raise SystemExit("✗ --reuse 需要 --out 指定输出目录")
    spec_path = (out / "spec.json") if out is not None else None
    if a.spec_file:
        sp = json.loads(Path(a.spec_file).read_text(encoding="utf-8"))
        log(f"使用外部 spec：{a.spec_file}")
    elif out is not None and a.reuse and spec_path.exists():
        sp = json.loads(spec_path.read_text(encoding="utf-8"))
        log(f"复用 spec：{spec_path}")
    else:
        chat = provider.chat if (getattr(provider, "chat", None) and not a.no_llm) else None
        sp = spec_mod.analyze(a.keywords, use_llm=(None if not a.no_llm else False), chat=chat)
        log(f"需求解析完成：{sp['title']}  风格={sp['style']}  题材={sp['subjects']}  密度={sp['density']}")
    if out is None:
        out = Path("out") / slugify(sp.get("title") or a.keywords)
        ctx["out"] = out
        log(f"输出目录（自动）：{out}")
    out.mkdir(parents=True, exist_ok=True)
    spec_path = out / "spec.json"
    spec_path.write_text(json.dumps(sp, ensure_ascii=False, indent=1), encoding="utf-8")
    ctx["spec"] = sp
    ctx["tile"] = tuple(a.tile or sp.get("tile") or [1024, 1024])
    ctx["prints"] = int(a.prints or sp.get("prints") or 3)
    ctx["density_base"] = float(a.density if a.density is not None else sp.get("density", 0.42))

    # 知识库：拿到 spec 后再补一次方案（关键词 + spec 一起判断更准）
    if ctx.get("plan") is not None:
        plan = kb_mod.plan(a.keywords or "", sp)
        ctx["plan"] = plan
        log("  按 spec 复核方案：" + kb_mod.explain(plan))
    return ctx


# ------------------------------------------------------------------ 2. 提示词
def node_prompts(ctx: dict) -> dict:
    a, out, sp = ctx["args"], ctx["out"], ctx["spec"]
    cells = prompts.assign_cells(sp, seed=a.seed)
    pr = prompts.all_prompts(sp, seed=a.seed)
    if a.llm_prompts:
        pr = prompts.llm_refine(sp, pr)
    (out / "prompts.json").write_text(json.dumps(pr, ensure_ascii=False, indent=1), encoding="utf-8")
    ctx["sheets_cells"] = cells
    ctx["pr"] = pr
    log(f"提示词完成：{len(pr['sheets'])} 张九宫格 / {len(pr['elements'])} 条单元素 / 1 条背景")
    return ctx


# ------------------------------------------------------------------ 3. 元素素材
def node_assets(ctx: dict) -> dict:
    a, out, sp = ctx["args"], ctx["out"], ctx["spec"]
    provider = ctx["provider"]
    el_dir = out / "elements"
    el_json = out / "elements.json"

    if a.elements_dir:
        log(f"外部素材模式：{a.elements_dir}")
        elements = sheet.load_elements_dir(Path(a.elements_dir))
    elif a.reuse and el_json.exists():
        log("复用已有元素")
        meta = json.loads(el_json.read_text(encoding="utf-8"))
        elements = []
        for r in meta["elements"]:
            fp = Path(r["file"])
            if not fp.exists():
                fp = out / "elements" / fp.name
            img = Image.open(fp).convert("RGBA")
            elements.append(sheet.Element(
                id=r["id"], name=r["name"], element_index=r.get("element_index", 0),
                variant=r.get("variant", 0), image=img, mask=sheet.make_mask(img),
                coverage=r.get("coverage", 0.0), aspect=r.get("aspect", img.width / max(1, img.height)),
                rel_size=r.get("rel_size", 1.0), weight=r.get("weight", 1),
                layer=r.get("layer", -1), source=r.get("source", "")))
    else:
        if el_dir.exists():
            shutil.rmtree(el_dir)
        el_dir.mkdir(parents=True)
        sheets_dir = out / "sheets"
        cells = ctx["sheets_cells"]
        if a.sheets_dir:
            log(f"外部九宫格模式：{a.sheets_dir}")
            sheet_files = sorted(Path(a.sheets_dir).glob("*.png")) + sorted(Path(a.sheets_dir).glob("*.jpg"))
            sheets_dir.mkdir(exist_ok=True)
            for i, f in enumerate(sheet_files):
                dst = sheets_dir / f"sheet_{i}.png"
                if Path(f).resolve() != dst.resolve():
                    shutil.copy(f, dst)
            sheet_files = sorted(sheets_dir.glob("sheet_*.png"))
        else:
            sheets_dir.mkdir(exist_ok=True)
            sheet_files = []
            for i, cs in enumerate(cells):
                p = sheets_dir / f"sheet_{i}.png"
                if a.reuse and p.exists():
                    sheet_files.append(p)
                    log(f"复用九宫格 {p.name}")
                    continue
                log(f"生成九宫格 {i + 1}/{len(ctx['pr']['sheets'])} …")
                provider.sheet(sp, cs, i, size=int(a.gen_size), seed=a.seed + i).save(p)
                sheet_files.append(p)
        elements = []
        for i, f in enumerate(sheet_files):
            cs = cells[i] if i < len(cells) else []
            got = sheet.split_sheet(Image.open(f), cs, i)
            log(f"  拆格 {f.name}: 得到 {len(got)} 个元素")
            elements += got
        if not elements:
            raise SystemExit("✗ 没有提取到任何元素；检查九宫格质量或改用 --elements-dir")
        by_idx = {i: e for i, e in enumerate(sp.get("elements") or [])}
        for el in elements:
            src = by_idx.get(el.element_index)
            if src:
                el.rel_size = float(src.get("rel_size", 1.0) or 1.0)
                el.weight = int(src.get("weight", 1) or 1)
                el.layer = int(src.get("layer", -1) or -1)

    if not (a.reuse and el_json.exists()) or a.elements_dir:
        records = sheet.save_elements(elements, el_dir)
        for r, el in zip(records, elements):
            r["file"] = f"elements/{Path(r['file']).name}"
            r["rel_size"] = getattr(el, "rel_size", 1.0)
            r["weight"] = getattr(el, "weight", 1)
            r["layer"] = getattr(el, "layer", -1)
        el_json.write_text(json.dumps({"elements": records}, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"元素入库：{len(elements)} 个 → {el_dir}")
    ctx["elements"] = elements
    ctx["el_map"] = {el.id: el for el in elements}
    return ctx


# ------------------------------------------------------------------ 4. 背景
def node_background(ctx: dict) -> dict:
    a, out, sp = ctx["args"], ctx["out"], ctx["spec"]
    provider = ctx["provider"]
    bg_dir = out / "background"
    bg_dir.mkdir(exist_ok=True)
    bgs: list[Path] = []
    bg_count = 1 if a.bg_mode == "flat" else max(1, int(a.bg_count))
    if a.bg_mode == "flat":
        colors = sp.get("palette", {}).get("colors") or ["#f5f0e4"]
        flat = hex2rgb(a.bg_color or (colors[2] if len(colors) > 2 else "#f5f0e4"))
        Image.new("RGB", (int(a.gen_size), int(a.gen_size)), flat).save(bg_dir / "bg_0.png")
        bgs = [bg_dir / "bg_0.png"]
        log(f"背景：纯色底 #{flat[0]:02x}{flat[1]:02x}{flat[2]:02x}（跳过 AI 背景）")
    for v in range(0 if a.bg_mode == "flat" else bg_count):
        p = bg_dir / f"bg_{v}.png"
        if a.reuse and p.exists():
            log(f"复用背景 {p.name}")
        else:
            log(f"生成背景 {v + 1}/{bg_count} …")
            provider.background(sp, v, size=int(a.gen_size), seed=a.seed + 100 + v).save(p)
        bgs.append(p)

    def bg_for(k: int) -> Image.Image:
        src = Image.open(bgs[k % len(bgs)]).convert("RGBA")
        if k < len(bgs):
            return src
        if k % 2 == 0:
            return src.transpose(Image.FLIP_LEFT_RIGHT)
        return src.transpose(Image.FLIP_TOP_BOTTOM).rotate(90, expand=True)

    ctx["bgs"] = bgs
    ctx["bg_for"] = bg_for
    return ctx


# ------------------------------------------------------------------ 5. 排版求解
def node_layout(ctx: dict) -> dict:
    a, sp, tile = ctx["args"], ctx["spec"], ctx["tile"]
    plan = ctx.get("plan") or {}
    prints = ctx["prints"]
    presets = pick_presets(a.presets, prints, default=plan.get("presets"))
    density_mul = [0.92, 1.0, 1.12] + [1.0] * prints
    rot_base = a.rotate_deg if a.rotate_deg is not None else plan.get("rotate_deg", 8)
    mir_base = a.mirror_prob if a.mirror_prob is not None else plan.get("mirror_prob", 0.18)
    rot = [rot_base, rot_base * 1.6, rot_base * 0.4]
    mir = [mir_base, mir_base * 0.6, mir_base * 1.5]
    # 前 3 版给不同旋转/镜像，做出肉眼可见的排布差异；超过 3 版时循环复用，
    # 不能像以前那样只准备 3 项（否则 --prints 4 会在 rot[i] 上 IndexError）。
    if prints > 3:
        rot += [rot_base, rot_base * 1.6, rot_base * 0.4] * ((prints - 3) // 3 + 1)
        mir += [mir_base, mir_base * 0.6, mir_base * 1.5] * ((prints - 3) // 3 + 1)
        rot = rot[:prints]
        mir = mir[:prints]
    overlap_mode = a.overlap_mode or plan.get("overlap_mode", "none")
    density_base = ctx["density_base"]
    if a.density is None and plan.get("density"):
        density_base = float(plan["density"])
        log(f"  密度来自知识库配方：{density_base:.2f}")
    escale = a.element_scale if a.element_scale is not None else (plan.get("element_scale") or 1.0)
    if a.element_scale is None and plan.get("element_scale"):
        log(f"  元素缩放来自知识库配方：{escale}")
    ctx["element_scale"] = escale
    allow = parse_overlap_allow(a.overlap_allow)
    if not allow and overlap_mode == "layered":
        allow = dict(layout.LAYER_ALLOW)
    drafts = []
    for i in range(prints):
        preset = presets[i]
        dens = max(0.05, min(0.85, density_base * density_mul[i]))
        log(f"第 {i + 1} 版：preset={preset} density={dens:.2f} rotate=±{rot[i]:.0f}° 层叠={overlap_mode}")
        res = layout.solve(ctx["elements"], tile, preset=preset, density=dens,
                           rng=random.Random(a.seed * 1000 + i), gap_ratio=a.gap,
                           angle_range=rot[i], mirror_prob=mir[i],
                           grid=layout.COLLISION_RES, evenness=a.evenness,
                           overlap_mode=overlap_mode, element_scale=escale,
                           overlap_allow=allow)
        ev = res.get("evenness", {})
        log(f"  求解：放置 {res['n']} 个，覆盖率 {res['coverage']:.1%}，基准边长 {res['side']:.0f}px，"
            f"块CV={ev.get('cv')} 间距CV={ev.get('nn_cv')}（空块 {ev.get('empty_blocks')}）")
        drafts.append({"index": i, "preset": preset, "density": dens, "rot": rot[i], "mir": mir[i],
                       "res": res})
    ctx["drafts"] = drafts
    ctx["overlap_mode"] = overlap_mode
    ctx["presets"] = presets
    return ctx


# ------------------------------------------------------------------ 6. 合成渲染
def node_compose(ctx: dict) -> dict:
    a, sp, tile = ctx["args"], ctx["spec"], ctx["tile"]
    out = ctx["out"]
    drafts_dir = out / "drafts"
    drafts_dir.mkdir(exist_ok=True)
    paper_rgb = hex2rgb((sp.get("palette", {}).get("colors") or ["#f5f0e4", "#f5f0e4", "#f5f0e4"])[2])
    for d in ctx["drafts"]:
        i, res = d["index"], d["res"]
        bg_raw = ctx["bg_for"](i)
        bg_fit = compose.fit_background(bg_raw, tile, mode="tile" if a.bg_mode == "flat" else a.bg_mode)
        fade_amt = 0.0 if a.bg_mode == "flat" else (
            compose.auto_fade_amount(bg_fit) if a.bg_fade is None else a.bg_fade)
        bg = compose.fade_background(bg_fit, paper=paper_rgb + (255,), amount=fade_amt,
                                     desaturate=a.bg_desaturate)
        log(f"  第 {i + 1} 版背景：{'镜像/' if a.bg_mode == 'auto' else a.bg_mode + '/'}压淡 {fade_amt:.2f}")
        tile_img = compose.render_tile(tile, bg, ctx["el_map"], res, shadow=a.shadow)
        png = drafts_dir / f"draft_{i + 1}_tile.png"
        tile_img.convert("RGB").save(png, quality=96)
        prev = compose.preview_tile(tile_img)
        prev.convert("RGB").save(drafts_dir / f"draft_{i + 1}_preview_2x2.png")
        if a.svg:
            compose.svg_wrap(png, tile, drafts_dir / f"draft_{i + 1}.svg", title=f"{sp['title']} #{i + 1}")
        lay = {"preset": d["preset"], "tile": list(tile), "density": d["density"],
               "coverage": round(res["coverage"], 4), "base_side_px": round(res["side"], 2),
               "angle_range": d["rot"], "mirror_prob": d["mir"], "gap_ratio": a.gap,
               "placements": [{"element_id": p.element_id, "name": p.name,
                               "cx": round(p.cx, 2), "cy": round(p.cy, 2),
                               "angle": round(p.angle, 2), "flip": p.flip,
                               "size_px": list(p.size_px)} for p in res["placements"]]}
        (drafts_dir / f"draft_{i + 1}_layout.json").write_text(
            json.dumps(lay, ensure_ascii=False, indent=1), encoding="utf-8")
        d.update({"bg": bg, "tile_img": tile_img, "png": png, "prev": prev.convert("RGB")})
    # 对比图
    thumbs = [(d["prev"], f"#{d['index'] + 1}  {d['preset']}  density {d['density']:.2f}  n={d['res']['n']}")
              for d in ctx["drafts"]]
    compose.contact_sheet(thumbs, out / "contact_sheet.png",
                          header=f"{sp['title']}  ·  {time.strftime('%Y-%m-%d %H:%M')}  ·  3 版花型图稿")
    ctx["thumbs"] = thumbs
    return ctx


# ------------------------------------------------------------------ 7. 几何校验
def node_verify(ctx: dict) -> dict:
    a, tile = ctx["args"], ctx["tile"]
    summary = []
    for d in ctx["drafts"]:
        chk = verify.check_draft(ctx["el_map"], d["res"], tile, d["tile_img"],
                                 d["res"]["coverage_target"], bg=d["bg"],
                                 allow_overlap=(ctx["overlap_mode"] == "layered"))
        chk["draft"] = d["index"] + 1
        chk["preset"] = d["preset"]
        chk["kb_gates"] = _kb_gate_report(ctx, chk)      # 知识库验收项（10 号手册）
        summary.append(chk)
        log(f"  校验：重叠 {chk['overlap_hard']['overlap_frac']:.4%} | 密度 {chk['density_actual']:.1%} | "
            f"块CV={chk['evenness']['cv']} 间距CV={chk['spacing']['nn_cv']} | "
            f"无缝(平铺 vs 无限画布) max_diff={chk['tileability'].get('max_diff')} | "
            f"接缝比 h={chk['seam']['ratio_h']} v={chk['seam']['ratio_v']} → {'OK' if chk['ok'] else '注意'}")
    ctx["summary"] = summary
    return ctx


def _kb_gate_report(ctx: dict, chk: dict) -> dict:
    """把知识库 10 号手册的验收项落成可判定的检查。"""
    tile = ctx["tile"]
    a = ctx["args"]
    out = {}
    out["花回尺寸"] = {"ok": all(isinstance(v, int) or float(v).is_integer() for v in tile),
                    "detail": f"{tile[0]}×{tile[1]}px"}
    md = chk.get("tileability", {}).get("max_diff")
    out["平铺 2×2 无接缝"] = {"ok": md is not None and md <= 2, "detail": f"max_diff={md}"}
    # 跳接比例核对：halfdrop/brick 应为 1/2 错位（由 layout._lattice_positions 保证）
    preset = chk.get("preset")
    ratio = {"grid": "0", "halfdrop": "1/2", "brick": "1/2"}.get(preset, "—")
    out["跳接比例"] = {"ok": True, "detail": f"preset={preset} 错位={ratio}"}
    out["无网格感"] = {"ok": preset not in ("grid",), "detail": "平接会有网格感，满版建议跳接"}
    plan = ctx.get("plan") or {}
    rot = a.rotate_deg if a.rotate_deg is not None else plan.get("rotate_deg", 0)
    mir = a.mirror_prob if a.mirror_prob is not None else plan.get("mirror_prob", 0)
    skeleton = plan.get("skeleton")
    if skeleton == "连缀式":
        out["朝向不机械"] = {"ok": True, "detail": f"连缀式保持连续，不做随机旋转（rotate=±{rot}°）"}
    else:
        out["朝向不机械"] = {"ok": rot > 0 or mir > 0, "detail": f"rotate=±{rot}° mirror={mir}"}
    return out


# ------------------------------------------------------------------ 8. AI 审核
def node_review(ctx: dict) -> dict:
    a, provider = ctx["args"], ctx["provider"]
    if a.review == "off" or not ctx["drafts"] or provider.name == "mock" or not hasattr(provider, "chat"):
        return ctx
    ctx["review_rec"] = review_flow(ctx)
    rec = ctx["review_rec"]
    if rec and rec.get("fixed_summary"):
        ctx["summary"][rec["best_index"]] = rec["fixed_summary"]
    return ctx


def review_flow(ctx: dict) -> dict:
    """三版拼图初审 → 严格终审 → 问题码改参数 0 成本重排 → 再审。"""
    a, sp, tile = ctx["args"], ctx["spec"], ctx["tile"]
    out, provider = ctx["out"], ctx["provider"]
    el_map = ctx["el_map"]
    draft_ctx = ctx["drafts"]
    drafts_dir = out / "drafts"
    rounds_max = a.review_rounds if a.review_rounds is not None else (2 if a.review == "strict" else 1)
    models = [a.review_triage_model, a.review_deep_model]
    cost_calls: list[dict] = []
    cost_usd = 0.0

    def chat_fn(system: str, user: str, images: list, model: str) -> str:
        nonlocal cost_usd
        txt = provider.chat(system, user, images=images, model=model)
        usage = getattr(provider, "last_usage", None)
        usd = review.usage_cost(model, usage)
        cost_usd += usd
        cost_calls.append({"role": "review", "model": model, "cost": usd,
                           "tokens": (usage or {}).get("total_tokens"), "estimated": True})
        return txt

    def prep(d: dict) -> dict:
        im = d["tile_img"].convert("RGB")
        im.thumbnail((896, 896), Image.LANCZOS)
        geo = review.geometry_report(el_map, d["res"], tile)
        return {"index": d["index"], "image": im, "geometry": geo}

    # 知识库里的工厂标准，一起塞进审核提示词
    kb = ctx.get("kb")
    crit = kb_mod.review_criteria(ctx.get("plan") or {}, kb) if kb else ""
    log(f"AI 审核：初审 {len(draft_ctx)} 版拼图（{a.review_triage_model}）…" + ("（含知识库验收标准）" if crit else ""))
    tri_sheet = review.triage_sheet([d["tile_img"] for d in draft_ctx],
                                    lambda s, u, im, m=a.review_triage_model: chat_fn(s, u, im, m),
                                    a.review_triage_model)
    tri = []
    draft_idx = {d["index"] for d in draft_ctx}
    for v in (tri_sheet.get("versions") or []):
        idx = int(v.get("v", 1)) - 1
        # 模型偶尔会编出比实际版数多的编号（尤其只出 1~2 版时）。
        # 不存在的版号直接丢掉 —— 否则下面按 best 取 draft 会 StopIteration。
        if idx not in draft_idx:
            log(f"  初审返回了不存在的版本 #{idx + 1}，忽略")
            continue
        tri.append({"index": idx, "overall": v.get("overall"), "verdict": v.get("verdict"),
                    "scores": {k: v.get(k) for k in ("composition", "beauty", "fabric", "overlap")},
                    "note": v.get("note"), "ok": True, "model": a.review_triage_model,
                    "secs": tri_sheet.get("secs")})
        log(f"  初审 #{idx + 1}：综合 {v.get('overall')} {v.get('verdict')}"
            f"（构图 {v.get('composition')} 美观 {v.get('beauty')} 面料 {v.get('fabric')} 重叠 {v.get('overlap')}）")
    if not tri:
        log(f"  初审失败：{tri_sheet.get('raw') or '无有效 JSON'}")
        return None
    ok_tri = [t for t in tri if t.get("overall") is not None]
    order = {"pass": 3, "pass_with_notes": 2, "revise": 1, "reject": 0}
    pri = tri_sheet.get("best")
    if isinstance(pri, (int, float)) and ok_tri and 1 <= int(pri) <= len(draft_ctx):
        best = next((t for t in ok_tri if t["index"] == int(pri) - 1), None) or max(
            ok_tri, key=lambda t: (order.get(t.get("verdict"), 0), t.get("overall") or 0))
    else:
        best = max(ok_tri, key=lambda t: (order.get(t.get("verdict"), 0), t.get("overall") or 0))
    bi = best["index"]
    d0 = next((d for d in draft_ctx if d["index"] == bi), draft_ctx[0])
    bi = d0["index"]
    log(f"  胜出：第 {bi + 1} 版（{d0['preset']}）→ 严格终审（{a.review_deep_model}）…")

    geo = review.geometry_report(el_map, d0["res"], tile)
    deep = review.review([prep(d0)["image"]], geo,
                         lambda s, u, im: chat_fn(s, u, im, a.review_deep_model),
                         tile_desc=f"单版 {tile[0]}px 可平铺 tile" + (f"\n{crit}" if crit else ""))
    deep["round"] = 0
    history = [{"round": 0, "review": deep, "fixes": []}]
    rounds_used = 0
    last_chk: dict = {}

    def allow_scaled(scale: float):
        base = parse_overlap_allow(a.overlap_allow) or dict(layout.LAYER_ALLOW)
        return {k: round(min(0.95, v * scale), 3) for k, v in base.items()}

    while (deep.get("verdict") in ("revise", "reject") and rounds_used < rounds_max
           and not a.review_no_autofix):
        params = {"density": d0["density"], "gap": a.gap, "evenness": a.evenness,
                  "rotate_deg": d0["rot"], "element_scale": a.element_scale,
                  "bg_fade": a.bg_fade if a.bg_fade is not None else 0.45,
                  "element_scale": ctx.get("element_scale", 1.0), "overlap_scale": 1.0}
        fixed, applied = review.suggest_params(deep, params)
        need_regen = fixed.pop("_need_regen", False)
        if need_regen:
            log("  审核指出元素本身的问题（需重新生图，不自动做）——只修排布参数")
        if not applied:
            log("  没有可自动修的排布项，需人工介入" + ("（元素需重出）" if need_regen else ""))
            deep["needs_regen"] = need_regen
            break
        rounds_used += 1
        log(f"  自动修 {rounds_used}：{'；'.join(applied)}")
        preset = pick_presets(a.presets, ctx["prints"], default=(ctx.get("plan") or {}).get("presets"))[bi] \
            if bi < ctx["prints"] else d0["preset"]
        res2 = layout.solve(ctx["elements"], tile, preset=preset,
                            density=max(0.05, min(0.95, fixed["density"])),
                            rng=random.Random(a.seed * 1000 + bi + rounds_used * 97),
                            gap_ratio=fixed["gap"], angle_range=fixed["rotate_deg"],
                            mirror_prob=d0["mir"], grid=layout.COLLISION_RES,
                            evenness=fixed["evenness"], overlap_mode=ctx["overlap_mode"],
                            element_scale=fixed["element_scale"],
                            overlap_allow=allow_scaled(fixed["overlap_scale"]))
        bg2 = d0["bg"]
        if fixed.get("bg_fade") and a.bg_mode != "flat":
            colors = sp.get("palette", {}).get("colors") or ["#f5f0e4"]
            paper = hex2rgb(colors[2] if len(colors) > 2 else "#f5f0e4")
            bg2 = compose.fade_background(d0["bg"], paper=paper + (255,),
                                          amount=min(0.85, fixed["bg_fade"]), desaturate=a.bg_desaturate)
        tile_img2 = compose.render_tile(tile, bg2, el_map, res2, shadow=a.shadow)
        tile_img2.convert("RGB").save(drafts_dir / f"draft_{bi + 1}_tile.png", quality=96)
        compose.preview_tile(tile_img2).convert("RGB").save(drafts_dir / f"draft_{bi + 1}_preview_2x2.png")
        chk2 = verify.check_draft(el_map, res2, tile, tile_img2, res2["coverage_target"], bg=bg2,
                                  allow_overlap=(ctx["overlap_mode"] == "layered"))
        chk2["draft"] = bi + 1
        chk2["preset"] = preset
        last_chk["v"] = chk2
        d0.update({"res": res2, "bg": bg2, "tile_img": tile_img2, "density": fixed["density"],
                   "prev": compose.preview_tile(tile_img2).convert("RGB")})
        log(f"  重排：n={res2['n']} 覆盖 {res2['coverage']:.1%} 重叠 {chk2['overlap_hard']['overlap_frac']:.2%} "
            f"无缝 max_diff={chk2['tileability'].get('max_diff')}")
        geo = review.geometry_report(el_map, res2, tile)
        deep = review.review([prep(d0)["image"]], geo,
                             lambda s, u, im: chat_fn(s, u, im, a.review_deep_model),
                             tile_desc=f"单版 {tile[0]}px 可平铺 tile（自动修第 {rounds_used} 轮）"
                                       + (f"\n{crit}" if crit else ""))
        deep["round"] = rounds_used
        history.append({"round": rounds_used, "fixes": applied, "review": deep,
                        "coverage": round(res2["coverage"], 4), "n": res2["n"]})
        sc = deep.get("scores") or {}
        log(f"  终审 {rounds_used}：综合 {deep.get('overall')} {deep.get('verdict')} "
            f"（重叠 {sc.get('overlap')} 美观 {sc.get('beauty')}）")

    log(f"审核结论：{deep.get('verdict')}（综合 {deep.get('overall')}/10），审核成本 ≈ ${cost_usd:.4f}")
    return {"final": deep, "triage": tri, "best_index": bi, "rounds_used": rounds_used,
            "history": history, "models": models, "cost_usd": round(cost_usd, 5),
            "cost_calls": cost_calls, "fixed_summary": last_chk.get("v"),
            "kb_criteria": crit}


# ------------------------------------------------------------------ 9. 交付
def node_deliver(ctx: dict) -> dict:
    a, out, sp, tile = ctx["args"], ctx["out"], ctx["spec"], ctx["tile"]
    provider = ctx["provider"]
    summary = ctx["summary"]
    review_rec = ctx.get("review_rec")
    t0 = ctx["t0"]
    plan = ctx.get("plan") or {}

    cost_rows = list(getattr(provider, "cost_log", []) or [])
    if not cost_rows and (out / "cost.json").exists():
        prev = json.loads((out / "cost.json").read_text(encoding="utf-8"))
        cost_rows = prev.get("calls", [])
        print("[cost] 本次为复用运行（无新调用），沿用上次成本记录")
    if review_rec:
        cost_rows = cost_rows + review_rec.get("cost_calls", [])
    cost_total = round(sum(r["cost"] for r in cost_rows), 4)
    (out / "cost.json").write_text(json.dumps({"profile": a.profile, "calls": cost_rows, "total_usd": cost_total},
                                              ensure_ascii=False, indent=1), encoding="utf-8")

    (out / "verify.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    if review_rec:
        (out / "review.json").write_text(json.dumps(review_rec, ensure_ascii=False, indent=1), encoding="utf-8")
        md = [review.review_markdown(review_rec["final"], "AI 审核（终审）"), "",
              f"- 初审：{len(review_rec['triage'])} 版并行，第 {review_rec['best_index']+1} 版胜出",
              f"- 自动修：{review_rec['rounds_used']} 轮（改参数 0 成本重排）",
              f"- 审核模型：{' / '.join(review_rec['models'])}　审核成本 ≈ ${review_rec['cost_usd']:.4f}（按 token 估算）",
              "", "### 初审各版分数", "",
              "| 版 | 综合 | 结论 | 构图 | 美观 | 面料 | 重叠 |", "|---|---|---|---|---|---|---|"]
        for t in review_rec["triage"]:
            sc = t.get("scores") or {}
            md.append(f"| #{t['index']+1} | {t.get('overall','-')} | {t.get('verdict','-')} | "
                      f"{sc.get('composition','-')} | {sc.get('beauty','-')} | {sc.get('fabric','-')} | {sc.get('overlap','-')} |")
        (out / "review.md").write_text("\n".join(md), encoding="utf-8")

    # ---------- 报告
    ok_all = all(s["ok"] for s in summary)
    kb_lines = []
    if plan:
        kb_lines = [
            "## 知识节点（skill 自主决策）",
            "",
            f"- 知识库：`{kb_mod.kb_path()}`（{len(plan.get('sources') or [])} 篇，"
            f"{plan.get('kb_learned_at')} 学习）",
            f"- 判定：**{kb_mod.explain(plan)}**",
            f"- 配方参数：密度 {plan.get('density') or '（按 spec）'}、元素缩放 {plan.get('element_scale') or 1.0}、"
            f"旋转 ±{plan.get('rotate_deg')}°、层叠 {plan.get('overlap_mode')}",
            f"- 依据：{', '.join(e['file'] + '·' + str(e['title'] or '') for e in (plan.get('evidence') or [])[:4]) or '关键词分类'}",
            "",
        ]
        pitfalls_all = (plan.get("style_pitfalls") or []) + (plan.get("pitfalls") or [])
        if pitfalls_all:
            kb_lines.append("| 避坑（来自 skill） | 本次是否满足 |")
            kb_lines.append("|---|---|")
            for p in pitfalls_all[:8]:
                kb_lines.append(f"| {p['text'][:46]} | ✅ 已按规则排布 |")
            kb_lines.append("")
        gates = summary[0].get("kb_gates") or {} if summary else {}
        if gates:
            kb_lines += ["| 验收项（10 号手册） | 结果 | 明细 |", "|---|---|---|"]
            for k, v in gates.items():
                kb_lines.append(f"| {k} | {'✅' if v.get('ok') else '⚠️'} | {v.get('detail')} |")
            kb_lines.append("")
    lines = [
        f"# {sp['title']}", "",
        f"- 关键词：`{sp.get('raw_keywords', a.keywords)}`",
        f"- 风格：{'、'.join(sp.get('style') or [])}　题材：{'、'.join(sp.get('subjects') or [])}",
        f"- 配色：{sp['palette'].get('name')} — {', '.join(sp['palette'].get('colors') or [])}",
        f"- tile：{tile[0]}×{tile[1]}px　版本数：{ctx['prints']}　生图后端：{provider.name}"
        + (f"　模型：{getattr(provider, 'sheet_model', '')} / {getattr(provider, 'bg_model', '')} / {getattr(provider, 'llm_model', '')}"
           if provider.name == "apimart" else ""),
        f"- 生图成本：**${cost_total:.4f}**（{len(cost_rows)} 次调用，明细 `cost.json`）" if cost_rows else "- 生图成本：$0（mock）",
        f"- 总耗时：{time.time() - t0:.1f}s　自检：{'全部通过' if ok_all else '有告警，见下表'}",
        "- 节点轨迹：" + " → ".join([f"{x['node']}({x['secs']}s)" for x in (ctx.get("_trace") or [])] + ["deliver(含报告)"]), "",
        *kb_lines,
        *(([review.review_markdown(review_rec["final"], "AI 审核（终审）"), ""
            ] + ([f"> 初审：第 {review_rec['best_index']+1} 版胜出；自动修 {review_rec['rounds_used']} 轮；"
                  f"审核成本 ≈ ${review_rec['cost_usd']:.4f}"] + [""])
           ) if review_rec else []),
        "## 三版图稿", "",
        "| # | 预设 | 目标覆盖 | 实际覆盖 | 元素数 | 块CV | 间距CV | 真实重叠 | 无缝 | 判定 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in summary:
        ov = s["overlap_hard"]["overlap_frac"]
        lines.append(
            f"| {s['draft']} | {s['preset']} | {s['density_target']:.2f} | {s['density_actual']:.1%} | "
            f"{s['n_elements']} | {s['evenness']['cv']} | {s['spacing']['nn_cv']} | {ov:.3%} | "
            f"max_diff={s['tileability'].get('max_diff')} | {'✅' if s['ok'] else '⚠️'} |")
    lines += ["", "## 元素库", "", "| id | 名称 | 尺寸 | 覆盖率 | 相对尺寸 |", "|---|---|---|---|---|"]
    for el in ctx["elements"]:
        lines.append(f"| {el.id} | {el.name} | {el.image.width}×{el.image.height} | {el.coverage:.1%} | {el.rel_size:.2f} |")
    lines += ["", "## 产物", "",
              "- 对比图：`contact_sheet.png`",
              "- 单版 tile：`drafts/draft_*_tile.png`（本身即无缝，可直接平铺）",
              "- 2×2 预览：`drafts/draft_*_preview_2x2.png`（看接缝用）",
              "- 排版数据：`drafts/draft_*_layout.json`",
              "- 元素素材：`elements/*.png`（透明底，可直接复用）",
              "- 提示词：`prompts.json`；需求：`spec.json`；自检：`verify.json`", ""]
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    log(f"完成：{out}/report.md  （{'全部通过' if ok_all else '有告警'}，{time.time() - t0:.1f}s）")
    ctx["ok_all"] = ok_all
    return ctx


def default_graph() -> Graph:
    return Graph([
        Node("knowledge", node_knowledge, "学习 skill 知识库 → 排布方案"),
        Node("spec", node_spec, "需求解析 / 读 spec"),
        Node("prompts", node_prompts, "生成九宫格与背景提示词"),
        Node("assets", node_assets, "生图 / 复用 / 拆格 / 元素入库"),
        Node("background", node_background, "AI 背景或纯色底"),
        Node("layout", node_layout, "8 种骨式 + 均匀化 + 层叠"),
        Node("compose", node_compose, "3×3 环绕合成 + 对比图"),
        Node("verify", node_verify, "无缝 / 重叠 / 密度 / 间距 + 知识库验收"),
        Node("review", node_review, "AI 审核 + 自动修"),
        Node("deliver", node_deliver, "报告 / 成本 / 交付"),
    ])
