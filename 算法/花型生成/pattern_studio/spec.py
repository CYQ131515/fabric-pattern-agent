"""
pattern_studio.spec —— 第 1 层：关键词 → 结构化需求（Spec）

设计要点
--------
1. **规则兜底**：不依赖任何模型也能跑通（词典分层：风格 / 题材 / 色彩 / 密度 / 用途）。
2. **大模型可插拔**：配置了 OPENAI_API_KEY（或任意 OpenAI 兼容端点）就用大模型改写/补全 Spec，
   失败自动回落规则结果，保证流水线永不中断。
3. 输出的 Spec 是下游唯一的输入契约（见 README 的 schema 一节）。
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass, field, asdict

# ------------------------------------------------------------------ 词典

STYLES: dict[str, list[str]] = {
    "国风": ["国风", "中国风", "新中式", "中式", "东方", "古典", "宫廷"],
    "水彩": ["水彩", "watercolor", "晕染", "淡彩", "湿画"],
    "工笔": ["工笔", "没骨", "白描", "重彩"],
    "青花": ["青花", "蓝白", "瓷", "釉"],
    "水墨": ["水墨", "墨韵", "泼墨", "写意"],
    "剪纸": ["剪纸", "窗花", "刻纸"],
    "敦煌": ["敦煌", "壁画", "藻井", "飞天"],
    "巴洛克": ["巴洛克", "洛可可", "复古花卉", "复古", "法式"],
    "极简": ["极简", "现代", "简约", "北欧", "minimal"],
    "手绘": ["手绘", "插画", "绘本", "涂鸦"],
    "几何": ["几何", "抽象", "构成", "包豪斯"],
    "刺绣": ["刺绣", "苏绣", "绣花", "钩针"],
    "和风": ["和风", "日式", "浮世绘"],
    "韩系": ["韩系", "小碎花", "法式小碎花"],
}

SUBJECTS_EN = {
    "牡丹": "peony", "梅花": "plum blossom", "兰花": "orchid", "竹": "bamboo",
    "菊花": "chrysanthemum", "荷花": "lotus", "玫瑰": "rose", "郁金香": "tulip",
    "山茶": "camellia", "桂花": "osmanthus", "樱花": "cherry blossom", "百合": "lily",
    "绣球": "hydrangea", "蝴蝶": "butterfly", "鸟": "bird", "云纹": "auspicious cloud",
    "水波": "water ripple", "缠枝": "interlocking vine branch", "果实": "fruit",
}

STYLE_EN = {
    "国风": "traditional Chinese art", "水彩": "watercolor", "工笔": "gongbi fine line painting",
    "青花": "blue-and-white porcelain painting", "水墨": "ink wash painting",
    "剪纸": "paper cut art", "敦煌": "Dunhuang mural art", "巴洛克": "baroque floral",
    "极简": "minimalist line art", "手绘": "hand-drawn illustration",
    "几何": "geometric graphic", "刺绣": "embroidery style", "和风": "Japanese ukiyo-e",
    "韩系": "delicate ditsy floral", "刺绣2": "embroidery",
}

STYLE_DETAIL_EN = {
    "国风": "elegant oriental aesthetics, controlled brushwork, decorative but restrained",
    "水彩": "soft watercolor washes, visible pigment granulation, bleeding edges, translucent layers",
    "工笔": "fine even outlines, flat layered mineral colors, precise petals",
    "青花": "indigo on porcelain white, cobalt blue gradients, delicate outline",
    "水墨": "monochrome ink with dry brush texture and splashes",
    "剪纸": "bold symmetric cut-paper silhouettes, clean negative space",
    "敦煌": "earthy mineral pigments, decorative cloud and ribbon motifs",
    "巴洛克": "ornate acanthus scrolls, rich shading, dramatic composition",
    "极简": "single-weight outlines, generous negative space, flat colors",
    "手绘": "loose hand-drawn strokes, textured paper feel",
    "几何": "flat geometric abstraction, hard edges, limited palette",
    "刺绣": "satin-stitch texture, thread-like strokes, slight relief",
    "和风": "flat decorative planes, quiet color harmony",
    "韩系": "tiny delicate motifs, airy spacing",
}

SUBJECTS: dict[str, list[str]] = {
    "牡丹": ["牡丹", "peony"],
    "梅花": ["梅花", "梅", "plum"],
    "兰花": ["兰花", "兰"],
    "竹": ["竹", "bamboo"],
    "菊花": ["菊花", "菊", "chrysanthemum"],
    "荷花": ["荷花", "莲花", "荷", "莲", "lotus"],
    "玫瑰": ["玫瑰", "月季", "rose"],
    "郁金香": ["郁金香", "tulip"],
    "山茶": ["山茶", "茶花"],
    "桂花": ["桂花"],
    "樱花": ["樱花", "sakura"],
    "百合": ["百合"],
    "绣球": ["绣球"],
    "蝴蝶": ["蝴蝶", "butterfly"],
    "鸟": ["喜鹊", "仙鹤", "鸟", "雀", "锦鸡"],
    "云纹": ["祥云", "云纹", "云"],
    "水波": ["水波", "涟漪", "浪花"],
    "缠枝": ["缠枝", "藤蔓", "枝蔓", "藤"],
    "果实": ["果实", "石榴", "枇杷", "柿子"],
}

# 传统色 → hex（用于在没有明确配色时给出可用的调色板）
COLOR_WORDS: dict[str, str] = {
    "胭脂": "#c8434a", "朱砂": "#c0392b", "绯红": "#e26a6a", "海棠": "#e88a8a",
    "黛青": "#3f5a56", "黛": "#2f3f3a", "墨": "#1f2320", "烟灰": "#8a8f8c",
    "月白": "#eee9dd", "米白": "#f5f0e4", "杏": "#f0c99b", "藤黄": "#e8b04b",
    "石青": "#3b6e8f", "群青": "#2c4a7c", "靛": "#33475e", "湖蓝": "#7aa8b8",
    "藕荷": "#c9a7b8", "丁香": "#b9a7c9", "竹青": "#7a9a6a", "橄榄": "#8a8f4a",
    "金": "#c9a227", "赭石": "#a0603a", "茶": "#8a6a4a", "藏蓝": "#2b3a55",
}

STYLE_PALETTES: dict[str, dict] = {
    "国风": {"name": "胭脂·黛青·月白", "colors": ["#c8434a", "#3f5a56", "#efe6d9", "#2f3f3a", "#e8b04b"]},
    "水彩": {"name": "淡彩·藕荷·竹青", "colors": ["#e88a8a", "#c9a7b8", "#9ab3a4", "#f0c99b", "#7a9a8c"]},
    "工笔": {"name": "石青·朱砂·藤黄", "colors": ["#3b6e8f", "#c0392b", "#e8b04b", "#7a9a6a", "#f5f0e4"]},
    "青花": {"name": "靛蓝·月白", "colors": ["#2c4a7c", "#3b6e8f", "#7aa8b8", "#eee9dd", "#1f2320"]},
    "水墨": {"name": "墨·烟灰·月白", "colors": ["#1f2320", "#5c6360", "#8a8f8c", "#eee9dd", "#c9a227"]},
    "敦煌": {"name": "赭石·石青·金", "colors": ["#a0603a", "#3b6e8f", "#c9a227", "#c0392b", "#f0c99b"]},
    "巴洛克": {"name": "绛紫·金·墨绿", "colors": ["#5b2b3a", "#c9a227", "#2f4a3a", "#e8d9c0", "#8a2f3a"]},
    "极简": {"name": "米白·烟灰·墨", "colors": ["#f5f0e4", "#c9c4b8", "#8a8f8c", "#2f3f3a"]},
    "和风": {"name": "绀青·朱·米白", "colors": ["#2b3a55", "#c0392b", "#f5f0e4", "#e8b04b", "#7a9a6a"]},
    "刺绣": {"name": "藏蓝·珊瑚·金", "colors": ["#2b3a55", "#e0796a", "#c9a227", "#eee9dd", "#7a9a8c"]},
}

DENSITY_WORDS = [
    (["疏", "留白", "空灵", "轻盈", "少量", "稀疏"], 0.26),
    (["中等", "自然", "适中"], 0.40),
    (["密", "繁复", "满版", "丰富", "密集", "厚重"], 0.55),
]

USAGE_WORDS = ["印花", "面料", "壁纸", "背景墙", "包装", "丝巾", "家居", "服装", "布艺"]

# 非花类题材：不套用"盛开/半开/花苞"的生长阶段
NON_FLOWER = {"云纹", "水波", "缠枝", "蝴蝶", "鸟", "果实", "竹"}

# 风格 → 建议的辅助元素（点缀），让画面不只有主花
ACCENTS: dict[str, list[str]] = {
    "国风": ["祥云", "缠枝", "蝴蝶"],
    "水彩": ["小碎花", "叶片", "水滴"],
    "工笔": ["枝干", "叶脉", "蝴蝶"],
    "青花": ["缠枝", "云纹", "水波"],
    "水墨": ["墨点", "飞白", "叶片"],
    "敦煌": ["祥云", "飘带", "宝相花"],
    "巴洛克": ["卷草", "缎带", "珍珠"],
    "极简": ["线条", "圆点", "叶片"],
    "和风": ["水波", "云纹", "樱花"],
    "刺绣": ["卷草", "花苞", "叶片"],
}

ELEMENT_TEMPLATES: dict[str, str] = {
    "bloom": "one fully blooming {subj}, top view, isolated single element",
    "half": "one half-open {subj} flower, three-quarter view, isolated single element",
    "bud": "one small closed {subj} bud with short stem, isolated single element",
    "leaf": "a small cluster of {subj} leaves and a curved stem, isolated single element",
    "cloud": "a small decorative auspicious cloud motif, isolated single element",
    "butterfly": "a small decorative butterfly motif, isolated single element",
    "vine": "a flowing interlocking vine ornament, isolated single element",
    "grass": "a few delicate grass blades, isolated single element",
    "fruit": "a small decorative fruit motif, isolated single element",
    "dot": "a tiny decorative dot cluster, isolated single element",
    "bird": "a small decorative bird motif, isolated single element",
}

ELEMENT_TYPES = list(ELEMENT_TEMPLATES)

# LLM 精修用的 schema（只让它做"选择与判断"，英文文案由模板生成，稳定性远高于让模型写英文）
REFINE_SCHEMA = """{
  "title": "短标题",
  "style_cn": ["风格词，从国风/水彩/工笔/青花/水墨/剪纸/敦煌/巴洛克/极简/手绘/和风/刺绣中选1-2个"],
  "subjects_cn": ["题材词，如 牡丹"],
  "palette": {"name": "配色名", "colors": ["#rrggbb", "#rrggbb", "#rrggbb", "#rrggbb", "#rrggbb"]},
  "elements": [{"cn": "元素中文名", "type": "bloom|half|bud|leaf|cloud|butterfly|vine|grass|fruit|dot|bird",
                "cells": 1|2|3, "weight": 1-4, "rel_size": 0.5-1.4}],
  "density": 0.15-0.65,
  "background_cn": "一句话描述背景（题材/冷暖/疏密）",
  "background_en": "English prompt for a SEAMLESS repeating background, low contrast, no focal subject",
  "notes": "一句话说明"
}"""

SPEC_SCHEMA_HINT = """{
  "title": "短标题",
  "style": ["风格词"], "subjects": ["题材词"],
  "palette": {"name": "配色名", "colors": ["#rrggbb", ...]},
  "elements": [{"name": "元素名", "describe": "英文生图描述（单个元素，无背景）", "weight": 1-4, "rel_size": 0.4-1.4, "cells": 1-3}],
  "background": {"describe": "中文描述", "prompt": "英文生图描述（无缝平铺背景，低对比，无主体）"},
  "density": 0.2-0.6, "tile": [1024,1024], "prints": 3, "notes": "一句话说明"
}"""


# ------------------------------------------------------------------ LLM 适配

def llm_enabled() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY") or os.environ.get("PATTERN_STUDIO_LLM_BASE"))


def llm_json(system: str, user: str, timeout: int = 90, temperature: float = 0.6) -> dict | None:
    """调用 OpenAI 兼容的 chat/completions，返回解析后的 JSON；不可用/失败返回 None。"""
    base = os.environ.get("PATTERN_STUDIO_LLM_BASE") or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    key = os.environ.get("OPENAI_API_KEY") or ""
    model = os.environ.get("PATTERN_STUDIO_LLM_MODEL", "gpt-4o-mini")
    if not key and "127.0.0.1" not in base and "localhost" not in base:
        return None
    body = {
        "model": model,
        "temperature": temperature,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        text = data["choices"][0]["message"]["content"]
        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
        return json.loads(text)
    except Exception as e:  # noqa: BLE001
        print(f"[spec] LLM 不可用或返回异常，回落规则解析：{e}")
        return None


# ------------------------------------------------------------------ 规则解析

def _hits(text: str, table: dict[str, list[str]]) -> list[str]:
    out = []
    for canon, alts in table.items():
        if any(a.lower() in text.lower() for a in alts + [canon]):
            out.append(canon)
    return out


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_keywords(text: str) -> dict:
    """关键词 → Spec（纯规则，无模型依赖）。"""
    text = _clean(text)
    styles = _hits(text, STYLES) or ["水彩"]
    subjects = _hits(text, SUBJECTS) or ["牡丹"]

    # 配色：显式颜色词优先，其次风格默认板
    picked = [COLOR_WORDS[w] for w in COLOR_WORDS if w in text]
    if len(picked) >= 3:
        palette = {"name": "自定义·" + re.sub(r"[^\u4e00-\u9fa5]", "", text)[:8], "colors": picked[:6]}
    else:
        palette = dict(STYLE_PALETTES.get(styles[0], STYLE_PALETTES["水彩"]))
        if picked:
            palette["colors"] = (picked + [c for c in palette["colors"] if c not in picked])[:5]

    density = 0.40
    for words, val in DENSITY_WORDS:
        if any(w in text for w in words):
            density = val
            break

    # 元素表：主花（主体）分 盛开/半开/花苞，加枝叶，再加风格化点缀
    elements: list[dict] = []
    for i, subj in enumerate(subjects[:3]):
        w = 4 if i == 0 else 3
        sen = SUBJECTS_EN.get(subj, subj)
        if subj in NON_FLOWER:
            elements += [
                {"name": f"{subj}·主纹样", "describe": f"one large decorative {sen} motif, isolated single element",
                 "weight": w, "rel_size": 1.2, "cells": 2},
                {"name": f"{subj}·小纹样", "describe": f"one small decorative {sen} motif, isolated single element",
                 "weight": w - 1, "rel_size": 0.7, "cells": 2},
                {"name": f"{subj}·线性纹样", "describe": f"a flowing linear {sen} ornament, isolated single element",
                 "weight": 2, "rel_size": 1.0, "cells": 1},
            ]
            continue
        elements += [
            {"name": f"{subj}·盛开", "describe": f"one fully blooming {sen}, top view, isolated single element",
             "weight": w, "rel_size": 1.25, "cells": 2},
            {"name": f"{subj}·半开", "describe": f"one half-open {sen} flower, three-quarter view, isolated single element",
             "weight": w - 1, "rel_size": 0.95, "cells": 2},
            {"name": f"{subj}·花苞", "describe": f"one small closed {sen} bud with short stem, isolated single element",
             "weight": 2, "rel_size": 0.6, "cells": 1},
        ]
        elements.append({"name": f"{subj}·枝叶", "describe": f"a small cluster of {sen} leaves and a curved stem, isolated single element",
                         "weight": 2, "rel_size": 1.0, "cells": 2})
    for acc in ACCENTS.get(styles[0], ["叶片"])[:2]:
        elements.append({"name": f"点缀·{acc}", "describe": f"a small decorative {SUBJECTS_EN.get(acc, acc)} motif, isolated single element",
                         "weight": 1, "rel_size": 0.55, "cells": 1})

    usage = next((u for u in USAGE_WORDS if u in text), "印花")
    style_en = " / ".join(STYLE_EN.get(x, x) for x in styles[:2])
    bg_desc = f"{style_en} 风格、以{'、'.join(subjects[:2])}的{'叶与枝' if subjects else '花叶'}为主的低对比暗纹背景，适合{usage}"
    subj_en = [SUBJECTS_EN.get(x, x) for x in subjects[:2]]
    if set(subjects) & NON_FLOWER:
        motif_txt = f"{', '.join(subj_en)} ornaments"
    else:
        motif_txt = f"soft {', '.join(subj_en)} leaves, petals and branch silhouettes"
    bg_prompt = (
        f"seamless repeating wallpaper pattern, {style_en} style, {motif_txt}, low contrast, "
        f"muted palette, edge-to-edge tileable, no focal subject, no text, subtle paper texture, "
        f"flat lay, textile print background"
    )

    spec = {
        "title": f"{''.join(styles[:1])}{''.join(subjects[:1])}{usage}",
        "raw_keywords": text,
        "style": styles,
        "style_en": [STYLE_EN.get(x, x) for x in styles],
        "style_detail_en": [STYLE_DETAIL_EN.get(x, "") for x in styles if STYLE_DETAIL_EN.get(x)],
        "subjects": subjects,
        "subjects_en": [SUBJECTS_EN.get(x, x) for x in subjects],
        "palette": palette,
        "elements": elements,
        "background": {"describe": bg_desc, "prompt": bg_prompt},
        "density": round(density, 3),
        "tile": [1024, 1024],
        "prints": 3,
        "notes": f"规则解析：风格={styles}，题材={subjects}，用途={usage}",
    }
    return spec


# ------------------------------------------------------------------ 主入口

def _strip_json(text: str) -> dict | None:
    text = re.sub(r"^\s*```(?:json)?|```\s*$", "", (text or "").strip(), flags=re.M).strip()
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(text[i : j + 1])
    except json.JSONDecodeError:
        return None


def _style_en_from_cn(styles_cn: list[str]) -> list[str]:
    out = []
    for cn in styles_cn:
        for canon, alts in STYLES.items():
            if cn == canon or cn in alts or any(a in cn for a in alts):
                out.append(STYLE_EN.get(canon, canon))
                break
    return out or ["watercolor"]


def _subject_en_from_cn(subjects_cn: list[str]) -> list[str]:
    out = []
    for cn in subjects_cn:
        for canon, alts in SUBJECTS.items():
            if cn == canon or cn in alts or any(a in cn for a in alts):
                out.append(SUBJECTS_EN.get(canon, canon))
                break
        else:
            out.append(cn)
    return out or ["peony"]


def refine_with_llm(spec: dict, keywords: str, chat) -> dict:
    """用大模型精修 Spec。

    关键设计：**只让模型做"选风格/定题材/挑元素类型/给配色"**，
    英文生图描述由 ELEMENT_TEMPLATES 生成 —— 便宜模型写英文不可靠，
    但"判断该有哪些元素"这件事它们做得很好。
    chat: callable(system, user) -> str
    """
    system = (
        "你是纺织印花设计总监。把一句中文需求变成结构化的花型设计方案。"
        "只输出 JSON，不要解释。元素用 type 字段表达（不要写英文描述）。"
        f"可用 type：{', '.join(ELEMENT_TYPES)}。"
        f"JSON schema：{REFINE_SCHEMA}"
    )
    user = (
        f"需求：{keywords}\n"
        f"规则初稿（可修正、可增删元素，但保持字段结构）：{json.dumps(spec, ensure_ascii=False)}\n"
        "要求：元素 6–10 个，必须包含主花的不同开放度（bloom/half/bud）与枝叶（leaf），"
        "再按风格加 1–2 个点缀（cloud/vine/butterfly/grass/dot/bird 之一）；"
        "配色给 5 个能印刷的 hex；背景必须是无主体的无缝暗纹。"
    )
    try:
        got = _strip_json(chat(system, user))
    except Exception as e:  # noqa: BLE001
        print(f"[spec] LLM 精修失败，使用规则结果：{e}")
        return spec
    if not got:
        print("[spec] LLM 返回不是合法 JSON，使用规则结果")
        return spec

    merged = dict(spec)
    if got.get("title"):
        merged["title"] = str(got["title"])[:28]
    styles_cn = [str(x) for x in (got.get("style_cn") or []) if x]
    if styles_cn:
        merged["style"] = styles_cn
        merged["style_en"] = _style_en_from_cn(styles_cn)
        detail = [STYLE_DETAIL_EN.get(_style_cn_key(styles_cn, 0), "")]
        merged["style_detail_en"] = [d for d in detail if d]
    subjects_cn = [str(x) for x in (got.get("subjects_cn") or []) if x]
    if subjects_cn:
        merged["subjects"] = subjects_cn
        merged["subjects_en"] = _subject_en_from_cn(subjects_cn)

    pal = got.get("palette") or {}
    colors = [str(c) for c in (pal.get("colors") or []) if re.fullmatch(r"#[0-9a-fA-F]{6}", str(c))]
    if len(colors) >= 3:
        merged["palette"] = {"name": pal.get("name") or merged["palette"].get("name", "自定义"), "colors": colors[:6]}

    els = got.get("elements") or []
    built = []
    for i, e in enumerate(els[:10]):
        t = str(e.get("type") or "").lower()
        if t not in ELEMENT_TEMPLATES:
            continue
        subj_en = merged["subjects_en"][0] if merged["subjects_en"] else "flower"
        cn = str(e.get("cn") or t)
        built.append({
            "name": cn,
            "describe": ELEMENT_TEMPLATES[t].format(subj=subj_en),
            "type": t,
            "weight": max(1, min(4, int(e.get("weight", 2) or 2))),
            "rel_size": max(0.4, min(1.5, float(e.get("rel_size", 1.0) or 1.0))),
            "cells": max(1, min(3, int(e.get("cells", 1) or 1))),
        })
    if len(built) >= 4:
        merged["elements"] = built

    try:
        merged["density"] = max(0.1, min(0.7, float(got.get("density", merged["density"]))))
    except (TypeError, ValueError):
        pass

    bg_en = got.get("background_en")
    if isinstance(bg_en, str) and len(bg_en) > 40:
        merged["background"] = {
            "describe": str(got.get("background_cn") or merged["background"].get("describe", "")),
            "prompt": bg_en if "seamless" in bg_en.lower() else "seamless repeating wallpaper pattern, " + bg_en,
        }
    elif got.get("background_cn"):
        merged["background"] = dict(merged.get("background") or {},
                                    describe=str(got["background_cn"]))
    merged["notes"] = (str(got.get("notes") or "") + " [LLM 精修]").strip()
    return merged


def _style_cn_key(styles_cn: list[str], idx: int) -> str:
    cn = styles_cn[idx] if styles_cn and idx < len(styles_cn) else ""
    for canon, alts in STYLES.items():
        if cn == canon or cn in alts or any(a in cn for a in alts):
            return canon
    return "水彩"


def analyze(keywords: str, use_llm: bool | None = None, chat=None) -> dict:
    """关键词 → Spec。use_llm=None 时按环境变量自动判断。"""
    spec = parse_keywords(keywords)
    if use_llm is None:
        # 传入了 chat（如 apimart provider）就直接用，不必依赖 OPENAI_API_KEY
        use_llm = bool(chat is not None) or (llm_enabled() and os.environ.get("PATTERN_STUDIO_NO_LLM") != "1")
    if not use_llm:
        return spec
    if chat is not None:
        return refine_with_llm(spec, keywords, chat)

    system = (
        "你是纺织印花设计总监，负责把一句中文需求翻译成结构化的花型设计方案。"
        "只输出 JSON，不要解释。元素描述用英文（下游要送生图模型），其余可用中文。"
        f"JSON schema：{SPEC_SCHEMA_HINT}"
    )
    user = (
        f"需求：{keywords}\n"
        f"规则初稿（可修正、可增删元素，但保持字段结构）：{json.dumps(spec, ensure_ascii=False)}\n"
        "要求：元素 6–10 个，必须包含主花的不同开放度、枝叶、点缀；每个元素描述必须强调 "
        "'isolated single element, plain white background, no shadow, no text'；"
        "背景描述必须是无主体的无缝平铺暗纹。"
    )
    got = llm_json(system, user)
    if not got:
        return spec
    # 合并：保留规则里可靠的字段，模型输出覆盖其余
    merged = dict(spec)
    merged.update({k: v for k, v in got.items() if v not in (None, "", [], {})})
    merged.setdefault("raw_keywords", keywords)
    merged["notes"] = (merged.get("notes") or "") + " [LLM 增强]"
    return merged


if __name__ == "__main__":  # 快速自测
    import sys

    s = analyze(" ".join(sys.argv[1:]) or "国风水彩牡丹花型图案")
    print(json.dumps(s, ensure_ascii=False, indent=1))
