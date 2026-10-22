"""
pattern_studio.prompts —— 第 2 层：Spec → 生图提示词

三段提示词：
  A. 九宫格元素图（sheet）：一次生成 3×3=9 个互相独立的元素，白底，一格一物。
  B. 单元素图（fallback）：模型不擅长出九宫格时，逐元素生成。
  C. 无缝背景（background）：与元素同风格、低对比、无主体，供铺底。

为什么九宫格要这样写（实战经验）：
  * 必须显式说 "3×3 grid" + "nine separate isolated elements" + 明确"每格只有一个"，
    否则模型会画成一张组合插画；
  * 必须显式禁掉 text / label / grid lines / border / shadow / reflection / collage；
  * 九宫格里的元素要"居中 + 留白"，否则切格时容易切断；
  * 统一风格串 + 统一调色板串放在最前，减少格子之间的风格漂移。
"""
from __future__ import annotations

import random

SHEET_NEGATIVE = (
    "text, words, letters, numbers, watermark, signature, logo, labels, captions, "
    "grid lines, black borders, frames, cell dividers, collage, mood board, multiple objects in one cell, "
    "overlapping elements, cropped element, element touching cell edge, element cut by cell border, "
    "drop shadow, reflection, ground shadow, 3d render, photo, gradient background, colored background, "
    "busy background, scenery, full composition, repeating pattern"
)

ELEMENT_NEGATIVE = (
    "text, watermark, signature, multiple objects, collage, background scenery, "
    "drop shadow, 3d render, photo, gradient background, frame, border"
)

BG_NEGATIVE = (
    "text, watermark, signature, logo, focal subject, centered flower, large motif, "
    "border, frame, vignette, gradient band, obvious repeat seam, 3d, photo, noise"
)

# 风格补充（把"风格词"翻译成模型能吃的画法描述）
STYLE_SUFFIX = {
    "traditional Chinese art": "elegant oriental composition, controlled ink brushwork, decorative but restrained, muted traditional pigments",
    "watercolor": "soft watercolor washes, pigment granulation, translucent layered petals, slightly bleeding edges",
    "gongbi fine line painting": "fine even outlines, flat layered mineral colors, crisply defined petals",
    "blue-and-white porcelain painting": "cobalt indigo on porcelain white, graded blue washes, fine outline",
    "ink wash painting": "monochrome ink, dry brush texture, expressive splashes",
    "paper cut art": "clean cut silhouettes, bold negative space, symmetric shapes",
    "Dunhuang mural art": "earthy mineral pigments, decorative ribbons and clouds",
    "baroque floral": "ornate acanthus scrolls, rich shading, dramatic curves",
    "minimalist line art": "single-weight clean outlines, generous negative space, flat limited color",
    "hand-drawn illustration": "loose hand-drawn strokes, textured paper feel",
    "geometric graphic": "flat geometric abstraction, hard edges",
    "embroidery style": "satin stitch texture, thread-like strokes, slight relief",
}


def palette_clause(spec: dict) -> str:
    p = spec.get("palette") or {}
    colors = ", ".join(p.get("colors") or [])
    return f"strict color palette: {colors}" + (f" ({p.get('name')})" if p.get("name") else "")


def _dedupe_phrases(parts: list[str]) -> list[str]:
    """去掉互相包含的画法描述，避免 prompt 里同一句话出现两遍。"""
    out: list[str] = []
    for p in parts:
        p = p.strip(" ;,")
        if not p:
            continue
        low = p.lower()
        if any(low == o.lower() or (low in o.lower() and low != o.lower()) for o in out):
            continue
        out = [o for o in out if not (o.lower() in low and o.lower() != low)]
        out.append(p)
    return out


def style_clause(spec: dict) -> str:
    styles = spec.get("style_en") or spec.get("style") or []
    detail = list(spec.get("style_detail_en") or [])
    extra = [STYLE_SUFFIX.get(s, "") for s in styles if STYLE_SUFFIX.get(s)]
    # 优先用更"画法化"的 STYLE_SUFFIX；风格细节作补充，重复句去掉
    raw: list[str] = []
    for chunk in extra + detail:
        raw.extend(x.strip() for x in chunk.split(",") if x.strip())
    phrases = _dedupe_phrases(raw)
    head = f"{' + '.join(styles)} style" if styles else ""
    return ", ".join([head] + phrases) if head else ", ".join(phrases)


def assign_cells(spec: dict, seed: int = 0) -> list[list[dict]]:
    """把元素按 cells 次数展开、随机打散、每 9 个一组（九宫格）。

    返回 [[{element, element_index, variant, cell}, ...9 个], ...]
    """
    rng = random.Random(seed)
    pool: list[dict] = []
    for ei, el in enumerate(spec.get("elements") or []):
        for v in range(int(el.get("cells", 1) or 1)):
            pool.append({"element_index": ei, "element": el, "variant": v})
    if not pool:
        return []
    # 不够 9 个就补（随机复取，标注 variant 递增，提示词里体现"另一种姿态"）
    n_sheets = max(1, (len(pool) + 8) // 9)
    while len(pool) < n_sheets * 9:
        pick = rng.choice(pool[: max(1, len(pool))])
        pool.append({**pick, "variant": pick["variant"] + 10})
    rng.shuffle(pool)
    return [pool[i : i + 9] for i in range(0, n_sheets * 9, 9)]


def _cell_line(i: int, cell: dict) -> str:
    el = cell["element"]
    v = cell["variant"]
    pose = "" if v < 10 else " (different pose/angle variation)"
    return f"cell {i + 1}: {el.get('describe') or el.get('name')}{pose}"


def sheet_prompt(spec: dict, cells: list[dict], sheet_index: int = 0) -> dict:
    """生成一张九宫格元素图的 prompt。"""
    n = len(cells)
    cells_txt = "; ".join(_cell_line(i, c) for i, c in enumerate(cells))
    prompt = (
        f"a 3 by 3 grid sheet with {n} separate isolated elements on a pure white background. "
        f"{style_clause(spec)}. {palette_clause(spec)}. "
        f"Each cell contains exactly ONE element, centered, fully inside its cell with a clear white "
        f"margin of at least 12% on every side, elements strictly separated and never overlapping "
        f"or crossing cell borders, nothing cropped by a cell edge. "
        f"Flat lay, even lighting, no shadow, sharp clean edges. "
        f"{cells_txt}. "
        f"Design sheet for textile print production, high resolution, consistent stroke weight and style across all cells."
    )
    return {
        "kind": "sheet",
        "index": sheet_index,
        "prompt": prompt,
        "negative_prompt": SHEET_NEGATIVE,
        "cells": [
            {"cell": i + 1, "element_index": c["element_index"], "element": c["element"].get("name"),
             "variant": c["variant"], "describe": c["element"].get("describe")}
            for i, c in enumerate(cells)
        ],
    }


def element_prompt(spec: dict, el: dict, variant: int = 0) -> dict:
    """单元素 prompt（九宫格不理想时的兜底，或只想要单元素素材时用）。"""
    pose = "" if variant == 0 else " alternative pose/angle, same style"
    prompt = (
        f"{el.get('describe') or el.get('name')}{pose}. {style_clause(spec)}. {palette_clause(spec)}. "
        f"One single isolated motif centered on a pure white background, generous margin, "
        f"flat lay, no shadow, clean sharp edges, textile print element, high resolution."
    )
    return {"kind": "element", "prompt": prompt, "negative_prompt": ELEMENT_NEGATIVE, "variant": variant}


def background_prompt(spec: dict) -> dict:
    bg = spec.get("background") or {}
    prompt = (
        f"{bg.get('prompt') or 'seamless repeating textile print background'}. "
        f"{style_clause(spec)}. {palette_clause(spec)}. "
        f"Absolutely seamless and tileable in both directions, edge-to-edge continuous motif. "
        f"**Very low contrast tonal background**: monochrome tint, small delicate motifs no larger than "
        f"8% of the image width, sparse, watermark-like, 12% opacity feel, plenty of negative space, "
        f"no focal point, no big flower heads, no large blooms, even visual weight, "
        f"square format, flat lay, subtle paper/linen texture."
    )
    return {"kind": "background", "prompt": prompt, "negative_prompt": BG_NEGATIVE,
            "describe_cn": bg.get("describe")}


def background_variant_prompt(spec: dict, variant: int) -> dict:
    """给第 2/3 版配一张同族但不同的背景（色相/密度微调）。"""
    base = background_prompt(spec)
    mods = [
        "slightly more open spacing, calmer",
        "denser motif spacing, richer texture",
        "softer wash, lighter value, more negative space",
    ]
    base["prompt"] = base["prompt"] + f" {mods[variant % len(mods)]}."
    base["variant"] = variant
    return base


def llm_refine(spec: dict, pr: dict) -> dict:
    """可选：让大模型把模板提示词改写得更有画面感（失败自动保留模板）。

    只在配置了 LLM 时生效；模板本身已经可用，所以这步是"锦上添花"。
    """
    from . import spec as spec_mod

    if not spec_mod.llm_enabled():
        return pr
    system = (
        "你是 AI 绘画提示词工程师，专攻纺织印花素材。"
        "把给定的提示词改写成一条更有效的英文生图 prompt：保持所有硬约束（九宫格、白底、一格一物、"
        "不重叠、无文字/边框/阴影、统一风格与配色），可以补充镜头/光线/材质/笔触细节。"
        "只输出 JSON：{\"prompt\": \"...\"}"
    )
    for sheet in pr.get("sheets", []):
        cell_txt = "; ".join(f"cell {c['cell']}: {c['describe']}" for c in sheet["cells"])
        user = f"风格：{spec.get('style')}；配色：{spec.get('palette', {}).get('colors')}\n九宫格内容：{cell_txt}\n原始提示词：{sheet['prompt']}"
        got = spec_mod.llm_json(system, user, temperature=0.8)
        if got and isinstance(got.get("prompt"), str) and len(got["prompt"]) > 80:
            sheet["prompt_template"] = sheet["prompt"]
            sheet["prompt"] = got["prompt"]
            sheet["llm_refined"] = True
    bg = pr.get("background")
    if bg:
        got = spec_mod.llm_json(
            system,
            f"这是一张无缝平铺背景的提示词，请改写并强调 tileable/seamless/low contrast/no focal subject：{bg['prompt']}",
            temperature=0.8)
        if got and isinstance(got.get("prompt"), str) and len(got["prompt"]) > 60:
            bg["prompt_template"] = bg["prompt"]
            bg["prompt"] = got["prompt"]
            bg["llm_refined"] = True
    return pr


def all_prompts(spec: dict, seed: int = 0) -> dict:
    sheets = assign_cells(spec, seed)
    return {
        "sheets": [sheet_prompt(spec, cells, i) for i, cells in enumerate(sheets)],
        "elements": [element_prompt(spec, el, v) for el in spec.get("elements") or [] for v in range(int(el.get("cells", 1) or 1))],
        "background": background_prompt(spec),
        "background_variants": [background_variant_prompt(spec, v) for v in range(2)],
    }


if __name__ == "__main__":
    import json
    import sys

    sys.path.insert(0, ".")
    from pattern_studio import spec as spec_mod

    sp = spec_mod.analyze(" ".join(sys.argv[1:]) or "国风水彩牡丹 疏朗")
    print(json.dumps(all_prompts(sp)["sheets"][0], ensure_ascii=False, indent=1)[:2000])
