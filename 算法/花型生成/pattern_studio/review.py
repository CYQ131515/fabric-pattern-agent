"""花型 AI 审核（⑧）：可平铺/密度这些能算的交给我，好不好看交给视觉模型，
但**先给它客观数据**，再让它只做判断——这样审核才不会变成"彩虹屁"。

三层证据：
  ① 几何实测（本地，免费）：覆盖率/间距/接缝/重叠率/重叠观感/聚簇度
  ② 视觉审理（视觉大模型，按次计费）：合理 / 美观 / 面料适配 / 重叠是否好看
  ③ 结构化裁决（JSON）：分数 + 问题清单（带位置、严重度、可执行修法）

问题代码 → 自动修法（改参数 0 成本重排，不重新生图）
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

import numpy as np
from PIL import Image

# ---------------------------------------------------------------- 问题代码表
ISSUE_CODES: dict[str, str] = {
    # 构图 / 合理
    "cluster": "局部聚簇：某处元素挤成一团",
    "void": "视野空洞：某块区域明显空",
    "flat_rhythm": "节奏呆板：元素等大等距，像瓷砖",
    "no_hierarchy": "主次不分：找不出主花 / 主花不突出",
    "direction_conflict": "方向打架：花头朝向互相矛盾、枝叶流向乱",
    "crowded": "太挤：元素快碰到，没有气口",
    "too_sparse": "太空：整体元素不足，像没做完",
    "repetition": "重复感强：一眼看出平铺格子",
    # 重叠
    "overlap_random": "重叠随意：该接的地方没接、不该压的地方硬压",
    "overlap_center": "中心相撞：一个元素的中心压在另一个主体上",
    "overlap_muddy": "重叠发糊：上下层明度/色相太接近，看不出前后",
    "overlap_large": "大块面互压：两朵大花叠成一团色块",
    # 造型 / 风格
    "motif_deformed": "元素造型崩坏：花瓣/叶子结构不成立",
    "motif_inconsistent": "元素不同套：笔触/线宽/细节密度不一致",
    "style_drift": "风格跑偏：混进了别的风格元素",
    "edge_artifact": "边缘脏：白边/黑边/锯齿/抠图残影",
    # 色彩 / 工艺
    "color_muddy": "色彩脏：色数过多或补色相撞",
    "color_low_contrast": "明度层次不足：远看糊成一团",
    "color_off_palette": "跑色：出现不在这套配色里的颜色",
    "print_min_line": "线太细：印花会断线（<0.15mm）",
    "print_too_many_colors": "色数超了：超出印花工艺上限",
    "print_gradient": "渐变风险：圆网/丝网印不了连续渐变",
}

# 问题代码 → 参数调整（自动修，0 API 成本）
FIX_MAP: dict[str, dict[str, Any]] = {
    "cluster":            {"evenness": +0.15, "gap": +0.03},
    "void":               {"density": +0.10},
    "too_sparse":         {"density": +0.12},
    "crowded":            {"density": -0.10, "gap": +0.04},
    "flat_rhythm":        {"evenness": -0.25, "rotate_deg": +6},
    "no_hierarchy":       {"element_scale": +0.15},
    "repetition":         {"rotate_deg": +8, "preset_next": True},
    "direction_conflict": {"rotate_deg": -8},
    "overlap_random":     {"overlap_scale": 0.75},
    "overlap_center":     {"overlap_scale": 0.7, "overlay_edge": True},
    "overlap_muddy":      {"overlap_scale": 0.6, "bg_fade": +0.05},
    "overlap_large":      {"overlap_scale": 0.6},
    "motif_inconsistent": {"regenerate": True},
    "motif_deformed":     {"regenerate": True},
    "style_drift":        {"regenerate": True},
    "edge_artifact":      {"regenerate": True},
    "color_muddy":        {"bg_fade": +0.10},
    "color_low_contrast": {"bg_fade": +0.10},
}

_RUBRIC = """你是纺织印花厂的花型质检总监，挑剔但讲证据。判断这张花型能不能投产。

给你：A. 几何实测（机器算的，可信）B. 花型图（**这是无缝循环单元**：元素跨出边界是正常的，
会在对面接回来，不要当成缺陷；只有"跨界元素过多/过于集中在边界"才算问题）。
按 0–10 打分：composition 构图合理｜beauty 美观｜fabric 面料适配（缩到 30cm 可读、线宽够、色数合理）｜
overlap 重叠观感（10=小压大、贴边缘、层次分明的手绘感；5=有几处生硬；0=中心相撞/大块面对撞/同明度糊；
画面无重叠属平面风格，给 7 并注明）｜originality 元素是否同套、有无串风格。

问题清单：最多 5 条，每条 code/severity(high|mid|low)/where/what/fix，**what 和 fix 各 ≤18 字**，只写图上真看到的。
五维分数不要都给同一个数，除非确实都同样好/差。
code 可选：cluster void flat_rhythm no_hierarchy direction_conflict crowded too_sparse repetition
overlap_random overlap_center overlap_muddy overlap_large motif_deformed motif_inconsistent
style_drift edge_artifact color_muddy color_low_contrast print_min_line print_too_many_colors print_gradient

只输出一行 JSON（无 markdown、无思考过程）：
{"scores":{"composition":0,"beauty":0,"fabric":0,"overlap":0,"originality":0},"overall":0,
"verdict":"pass|revise|reject","summary":"≤30字","issues":[{"code":"","severity":"","where":"","what":"","fix":""}],
"keep":["≤12字"]}
verdict：overall≥8 且无 high → pass；≥6 或仅 1 个 high → revise；其余 reject。
"""


def norm_score(v) -> float:
    """把模型可能给的 0–100 分归一化到 0–10。"""
    try:
        x = float(v)
    except Exception:  # noqa: BLE001
        return 0.0
    if x > 10.5:
        x = x / 10.0
    return round(max(0.0, min(10.0, x)), 1)


def _strip_json(text: str) -> dict | None:
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j < 0:
        return None
    try:
        return json.loads(t[i : j + 1])
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------ ① 几何实测证据

def placements_from_layout(elements: dict, layout_json: dict, tile: tuple[int, int],
                           grid: int = 512) -> list:
    """从 draft_*_layout.json 重建 placements（含 mask_res），用于事后审核/复审。"""
    from . import layout as L

    W, H = float(tile[0]), float(tile[1])
    by_id = elements
    out = []
    for rec in layout_json.get("placements", []):
        el = by_id.get(rec.get("element_id"))
        if el is None:
            continue
        sw, sh = rec.get("size_px") or (64, 64)
        ang = float(rec.get("angle") or 0.0)
        flip = bool(rec.get("flip"))
        patch = L.render_mask(el.mask, (sw, sh), ang, flip, grid, (W, H))
        out.append(L.Placement(
            element_id=el.id, name=getattr(el, "name", el.id),
            cx=float(rec["cx"]), cy=float(rec["cy"]), angle=ang, flip=flip,
            size_px=(int(sw), int(sh)), mask_res=patch,
            area_frac=float(patch.sum()) / (grid * grid)))
    return out


def geometry_report(elements: dict, result: dict, tile: tuple[int, int], *,
                    bg: Image.Image | None = None, grid: int = 512) -> dict:
    """本地量出"重叠观感"的客观指标（免费，且比模型肉眼更准）。

    用**真实掩膜**算相交，不用"中心距近似"（后者对细长叶子会误判）。
    重叠分三类：
      * edge_ok     小压大、只盖住对方一小部分 → 自然遮挡（好）
      * dead_center 小元素压在大元素体内、且盖住大元素中心（花心被挡） → 难看
      * mutual      两个尺寸相近的元素互相盖住很大比例 → 两块面糊在一起（难看）
      * muddy       上述重叠里，两层平均明度差 <10 → 看不出前后
    """
    from . import layout as L

    W, H = float(tile[0]), float(tile[1])
    pls = result.get("placements") or []
    if not pls:
        return {"n": 0}

    canv: list[np.ndarray] = []
    meta = []
    for p_ in pls:
        patch = getattr(p_, "mask_res", None)
        c = np.zeros((grid, grid), bool)
        if patch is not None:
            ph, pw = patch.shape
            x0 = int(round(p_.cx / W * grid - pw / 2)); y0 = int(round(p_.cy / H * grid - ph / 2))
            L._blit_wrap(c, patch, x0, y0, "or")
        canv.append(c)
        el = elements.get(p_.element_id) if isinstance(elements, dict) else None
        lay = L.element_layer(el) if el is not None else 1
        lum = 128.0
        if el is not None:
            try:
                a = np.asarray(el.image.convert("RGBA"), dtype=np.float32)
                m = a[..., 3] > 120
                if m.any():
                    lum = float(a[..., :3].mean(axis=2)[m].mean())
            except Exception:  # noqa: BLE001
                pass
        area = float(c.sum()) or float(p_.area_frac or 0.0) * grid * grid
        meta.append({"i": len(meta), "p": p_, "layer": lay, "area": max(1.0, area), "lum": lum})

    edge_ok = dead = mutual = muddy = contact = 0
    pairs_detail = []
    for i in range(len(meta)):
        for j in range(i + 1, len(meta)):
            a, b = meta[i], meta[j]
            if not canv[i].any() or not canv[j].any():
                continue
            inter = int((canv[i] & canv[j]).sum())
            if inter <= 0:
                continue
            contact += 1
            frac_a = inter / a["area"]
            frac_b = inter / b["area"]
            small_frac = max(frac_a, frac_b)          # 小元素被盖比例
            ratio = min(a["area"], b["area"]) / max(a["area"], b["area"])
            big, small = (a, b) if a["area"] >= b["area"] else (b, a)
            small_c = canv[small["i"]]
            big_c = canv[big["i"]]
            # 「压住大元素的视觉中心」才是问题：取大元素掩膜质心，看是否被小元素盖住
            ys, xs = np.nonzero(big_c)
            if ys.size:
                cy_i, cx_i = int(ys.mean()), int(xs.mean())
                center_covered = bool(small_c[cy_i, cx_i])
            else:
                center_covered = False
            inter_frac_tile = inter / float(grid * grid)
            if abs(a["lum"] - b["lum"]) < 10.0 and inter_frac_tile > 0.005:
                muddy += 1
            big_enough = max(a["area"], b["area"]) > 0.02 * grid * grid
            if ratio > 0.7:
                if big_enough and small_frac > 0.30:
                    mutual += 1
                    pairs_detail.append(("mutual", round(small_frac, 2)))
            else:
                if center_covered and small_frac > 0.10:
                    dead += 1
                    pairs_detail.append(("dead_center", round(small_frac, 2)))
                elif small_frac > 0.02:
                    edge_ok += 1

    # 聚簇：1/8 网格
    K = 8
    cell_count = np.zeros((K, K), int)
    for m in meta:
        cell_count[min(K - 1, int(m["p"].cy / H * K)), min(K - 1, int(m["p"].cx / W * K))] += 1
    cc = cell_count[cell_count > 0]
    cluster = float(cc.max()) if cc.size else 0.0

    mask = np.zeros((grid, grid), bool)
    for c in canv:
        mask |= c
    total_area = float(sum(c.sum() for c in canv))
    union_area = float(mask.sum()) or 1.0
    overlap_area_frac = round(max(0.0, (total_area - union_area) / union_area), 4)
    K2 = 6
    bs = grid // K2
    blocks = np.array([[mask[by * bs:(by + 1) * bs, bx * bs:(bx + 1) * bs].mean()
                        for bx in range(K2)] for by in range(K2)])
    mean_cov = float(blocks.mean())
    empty_blocks = int(sum(1 for by in range(K2) for bx in range(K2)
                           if blocks[by, bx] < max(0.002, mean_cov * 0.2)))
    return {
        "n": len(pls),
        "coverage": round(mean_cov, 4),
        "overlap_area_frac": overlap_area_frac,
        "contact_pairs": contact,
        "overlap_edge_ok": edge_ok,
        "overlap_dead_center": dead,
        "overlap_mutual": mutual,
        "overlap_muddy_pairs": muddy,
        "cluster_max_per_cell": cluster,
        "empty_blocks": empty_blocks,
        "block_cv": round(float(blocks.std()) / mean_cov, 3) if mean_cov else 0.0,
        "layered": any(m["layer"] < 2 for m in meta) and any(m["layer"] >= 2 for m in meta),
        "seamless_tile": True,
        "spacing_cv": ((result.get("evenness") or {}).get("nn_cv")),
        "_examples": pairs_detail[:5],
    }


def geometry_flags(g: dict) -> list[dict]:
    """把几何数据翻译成"问题条目"。阈值随元素数量自适应，并按超标倍数分严重度——
    正常的手绘层叠会有少量"盖住中心"，只有明显过量才算缺陷。"""
    n = max(1, int(g.get("n") or 1))
    expect_per_cell = n / 64.0
    cluster_thr = max(6.0, 3.0 * expect_per_cell)

    def sev(cnt: int, ratio: float, floor: float) -> str | None:
        thr = max(floor, ratio * n)
        if cnt >= 2.0 * thr:
            return "high"
        if cnt >= thr:
            return "mid"
        return None

    out: list[dict] = []
    sv = sev(int(g.get("overlap_dead_center") or 0), 0.06, 3)
    if sv:
        out.append({"code": "overlap_center", "severity": sv, "where": "多处",
                    "what": f"{g['overlap_dead_center']} 处小元素盖住了主花的视觉中心",
                    "fix": "压上去的元素只贴边缘，别压花心"})
    sv = sev(int(g.get("overlap_mutual") or 0), 0.04, 2)
    if sv:
        out.append({"code": "overlap_large", "severity": sv, "where": "多处",
                    "what": f"{g['overlap_mutual']} 处尺寸相近的元素互相盖住一大块",
                    "fix": "主花之间禁止深度重叠"})
    sv = sev(int(g.get("overlap_muddy_pairs") or 0), 0.06, 3)
    if sv:
        out.append({"code": "overlap_muddy", "severity": sv, "where": "多处",
                    "what": f"{g['overlap_muddy_pairs']} 处重叠的上下层明度太接近",
                    "fix": "拉开明度或加描边"})
    of = float(g.get("overlap_area_frac") or 0.0)
    if of > 0.42:
        out.append({"code": "overlap_large", "severity": "high", "where": "整体",
                    "what": f"叠得太多（重叠面积占元素总面积的 {of:.0%}）", "fix": "整体降低叠层额度"})
    elif of > 0.25:
        out.append({"code": "overlap_large", "severity": "mid", "where": "整体",
                    "what": f"叠得偏多（重叠面积占 {of:.0%}）", "fix": "适当降低叠层额度"})
    if (g.get("cluster_max_per_cell") or 0) > 2 * cluster_thr:
        out.append({"code": "cluster", "severity": "high", "where": "局部",
                    "what": f"单个 1/8 网格里挤了 {int(g['cluster_max_per_cell'])} 个元素",
                    "fix": "提高均匀度、加大间距"})
    elif (g.get("cluster_max_per_cell") or 0) > cluster_thr:
        out.append({"code": "cluster", "severity": "mid", "where": "局部",
                    "what": f"单个 1/8 网格里挤了 {int(g['cluster_max_per_cell'])} 个元素",
                    "fix": "提高均匀度、加大间距"})
    if (g.get("empty_blocks") or 0) >= max(2, 0.12 * 36):
        out.append({"code": "void", "severity": "mid", "where": "局部",
                    "what": f"{g['empty_blocks']} 个 1/6 区域几乎空着", "fix": "提高密度或在空块补小元素"})
    if (g.get("spacing_cv") or 0) > 0.30:
        out.append({"code": "flat_rhythm", "severity": "low", "where": "整体",
                    "what": f"元素间距差异大（CV {g['spacing_cv']}）", "fix": "提高均匀度"})
    return out


def build_prompt(g: dict, tile_desc: str) -> str:
    return (f"{_RUBRIC}\n\n【A. 几何实测数据】\n{json.dumps(g, ensure_ascii=False)}\n\n"
            f"【B. 花型图】{tile_desc}\n请严格按 JSON 格式输出。")


# 视觉模型价格（USD / 1M tokens，公开价估算，仅用于成本记账）
PRICE_EST: dict[str, tuple[float, float]] = {
    "gemini-3-flash-preview": (0.30, 2.50),
    "gemini-3.1-flash-lite-preview": (0.10, 0.40),
    "gemini-3-pro-preview": (1.25, 10.0),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5-nano": (0.05, 0.40),
    "gpt-5": (1.25, 10.0),
    "qwen3.6-flash": (0.15, 1.20),
}


def usage_cost(model: str, usage: dict | None) -> float:
    if not usage:
        return 0.0
    pin, pout = PRICE_EST.get(model, (0.3, 2.0))
    return round((usage.get("prompt_tokens", 0) * pin + usage.get("completion_tokens", 0) * pout) / 1e6, 5)


# ------------------------------------------------------------ ② 视觉审理
def review(models: list[Image.Image], g: dict, chat, tile_desc: str = "单版 tile（可平铺循环单元）",
           ) -> dict:
    """调用视觉模型审理；chat(system, user, images) 由 imagegen 提供。"""
    prompt = build_prompt(g, tile_desc)
    raw = ""
    try:
        raw = chat("你是严格的印花花型质检总监。", prompt, models)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"审核模型调用失败：{e}"}
    data = _strip_json(raw)
    if not data:
        return {"ok": False, "error": "审核模型返回的不是 JSON", "raw": raw[:500]}
    data["ok"] = True
    data.setdefault("issues", [])
    data.setdefault("scores", {})
    data["overall"] = norm_score(data.get("overall"))
    if isinstance(data.get("scores"), dict):
        data["scores"] = {k: norm_score(v) for k, v in data["scores"].items()}
    # 校正：几何硬伤必须出现在 issues 里
    have = {i.get("code") for i in data["issues"] if isinstance(i, dict)}
    for f in geometry_flags(g):
        if f["code"] not in have:
            data["issues"].append(f)
    # 复算 verdict（不信模型的自我判定）
    highs = sum(1 for i in data["issues"] if i.get("severity") == "high")
    if data["overall"] >= 8 and highs == 0:
        data["verdict"] = "pass"
    elif data["overall"] >= 7.5 and highs == 0:
        data["verdict"] = "pass_with_notes"     # 可用，但有小建议
    elif data["overall"] >= 6 and highs <= 1:
        data["verdict"] = "revise"
    else:
        data["verdict"] = "reject"
    return data


# ------------------------------------------------------------ ②b 编排：初审 + 终审
def triage(drafts: list[dict], chat_fn, model: str, tile_desc: str) -> list[dict]:
    """并行审多版，返回每版结果。drafts: [{index, image, geometry}]"""
    from concurrent.futures import ThreadPoolExecutor

    def one(d: dict) -> dict:
        import time as _t

        t0 = _t.perf_counter()
        out = review([d["image"]], d.get("geometry") or {}, 
                     lambda sysm, usr, imgs, m=model: chat_fn(sysm, usr, imgs, m), tile_desc)
        out["index"] = d["index"]
        out["model"] = model
        out["secs"] = round(_t.perf_counter() - t0, 1)
        return out

    if not drafts:
        return []
    with ThreadPoolExecutor(max_workers=min(4, len(drafts))) as ex:
        return list(ex.map(one, drafts))


def triage_sheet(images: list[Image.Image], chat_fn, model: str) -> dict:
    """把各版拼成一张带编号的横图，1 次调用同时打分（比逐版审省钱 2/3）。

    版数是动态的：产品侧可能只要 1~2 版，提示词不能写死「三个版本」，
    否则模型会照着三版编造不存在的 #2/#3，下游按版号取图就会取空。
    """
    from PIL import ImageDraw

    n = max(1, len(images))
    nums = " ".join(f"#{i + 1}" for i in range(n))
    th: list[Image.Image] = []
    for im in images:
        t = im.convert("RGB").copy()
        t.thumbnail((520, 520), Image.LANCZOS)
        th.append(t)
    gap, head = 12, 34
    W = sum(t.width for t in th) + gap * (len(th) + 1)
    H = max(t.height for t in th) + head + gap * 2
    sheet = Image.new("RGB", (W, H), (245, 242, 236))
    dr = ImageDraw.Draw(sheet)
    x = gap
    for i, t in enumerate(th):
        sheet.paste(t, (x, head + gap))
        dr.text((x + 4, 10), f"#{i + 1}", fill=(20, 20, 20))
        x += t.width + gap
    prompt = (f"下面是一张花型的 {n} 个排布版本（左到右 {nums}），风格/元素/底色相同，只有排布不同。\n"
              f"请给这 {n} 版逐一打分，并从 {nums} 里挑出最好的一版（best 必须是其中之一）。\n"
              "评判：构图合理、美观、适合做面料、重叠是否自然好看。\n"
              "各版之间必须有区分度，五维分数不要雷同；只给图上真实存在的版本号，不要编造。\n"
              "只输出一行 JSON：\n"
              '{"versions":[{"v":1,"composition":0,"beauty":0,"fabric":0,"overlap":0,"overall":0,'
              '"verdict":"pass|revise|reject","note":"≤15字"}],"best":1,"why":"≤25字"}')
    import time as _t

    t0 = _t.perf_counter()
    raw = chat_fn("你是严格的印花花型质检总监。", prompt, [sheet], model)
    secs = round(_t.perf_counter() - t0, 1)
    data = _strip_json(raw) or {}
    vers = []
    for v in (data.get("versions") or []):
        if not isinstance(v, dict):
            continue
        v = dict(v)
        v["overall"] = norm_score(v.get("overall"))
        for k in ("composition", "beauty", "fabric", "overlap"):
            if k in v:
                v[k] = norm_score(v[k])
        vers.append(v)
    out = {"ok": bool(data), "model": model, "secs": secs, "versions": vers,
           "best": data.get("best"), "why": data.get("why")}
    if not data:
        out["raw"] = (raw or "")[:300]
    return out


# 这些问题的根因在"元素库"，改排布参数治不好 —— 要重出元素/改提示词
ELEMENT_LEVEL = {"motif_inconsistent", "motif_deformed", "style_drift", "edge_artifact",
                 "repetition", "color_muddy", "color_low_contrast", "color_off_palette",
                 "print_min_line", "print_too_many_colors", "print_gradient"}


def next_actions(data: dict) -> list[str]:
    """按问题根因给出"下一步该干什么"，排布类和元素类分开说。"""
    issues = [i for i in (data.get("issues") or []) if isinstance(i, dict)]
    layout_codes = [i.get("code") for i in issues if i.get("code") and i["code"] not in ELEMENT_LEVEL]
    elem_codes = sorted({i.get("code") for i in issues if i.get("code") in ELEMENT_LEVEL})
    acts: list[str] = []
    if layout_codes:
        acts.append(f"排布类问题（{', '.join(sorted(set(layout_codes)))}）：可调 `--density/--evenness/--rotate-deg/--overlap-allow` 后 `--reuse` 重排（0 API 成本）")
    if elem_codes:
        acts.append(f"元素类问题（{', '.join(elem_codes)}）：根因在元素库/配色，**改排布无用** —— 多出 1–2 种元素变体（换提示词或加一张九宫格 $0.0085），或调整配色后再拼")
    if not acts:
        acts.append("无阻塞问题，可直接交付（仍建议人看一眼风格是否对客户口味）")
    return acts


def review_markdown(data: dict, title: str = "AI 审核") -> str:
    """把审核结果写成给人看的一段 Markdown。"""
    lines = [f"## {title}", ""]
    sc = data.get("scores") or {}
    names = {"composition": "构图", "beauty": "美观", "fabric": "面料适配",
             "overlap": "重叠观感", "originality": "原创统一"}
    lines.append("| 维度 | " + " | ".join(n for n in names.values()) + " | 综合 | 结论 |")
    lines.append("|---|" + "---|" * len(names) + "---|---|")
    lines.append("| 分数 | " + " | ".join(str(sc.get(k, "-")) for k in names) + f" | **{data.get('overall', '-')}** | "
                 f"{ {'pass':'✅ 通过','pass_with_notes':'✅ 可用（有小建议）','revise':'⚠️ 需修改','reject':'❌ 打回'}.get(data.get('verdict'), data.get('verdict')) } |")
    lines.append("")
    if data.get("summary"):
        lines.append(f"**总评**：{data['summary']}")
        lines.append("")
    keep = data.get("keep") or []
    if keep:
        lines.append("**保留**：" + "；".join(str(k) for k in keep[:3]))
        lines.append("")
    acts = next_actions(data)
    if acts:
        lines.append("**建议下一步**：")
        for a in acts:
            lines.append(f"- {a}")
        lines.append("")
    issues = data.get("issues") or []
    if issues:
        lines.append("| 严重度 | 问题 | 位置 | 现象 | 建议 |")
        lines.append("|---|---|---|---|---|")
        for it in issues:
            if not isinstance(it, dict):
                continue
            sev = {"high": "🔴 高", "mid": "🟡 中", "low": "🟢 低"}.get(it.get("severity"), it.get("severity"))
            lines.append(f"| {sev} | {it.get('code')} | {it.get('where')} | {it.get('what')} | {it.get('fix')} |")
    return "\n".join(lines)


# ------------------------------------------------------------ ③ 自动修
def suggest_params(data: dict, params: dict) -> tuple[dict, list[str]]:
    """把审核结论翻译成参数调整（只改能 0 成本重排的项）。"""
    p = dict(params)
    applied: list[str] = []
    need_regen = False
    codes = [i.get("code") for i in data.get("issues", []) if i.get("code") in FIX_MAP]
    for code in codes:
        for k, v in FIX_MAP[code].items():
            if k == "regenerate":
                need_regen = True
                continue
            if k == "preset_next":
                continue
            if k == "overlap_scale":
                cur = p.get("overlap_scale", 1.0)
                p["overlap_scale"] = round(max(0.2, cur * float(v)), 3)
                applied.append(f"叠层额度 ×{v} → {p['overlap_scale']}")
            elif k == "overlay_edge":
                p["overlay_edge"] = True
                applied.append("叠放改为贴边（不让小元素压中心）")
            elif k == "bg_fade":
                p["bg_fade"] = round(min(0.85, (p.get("bg_fade") or 0.45) + float(v)), 3)
                applied.append(f"背景压淡 → {p['bg_fade']}")
            elif k == "element_scale":
                p["element_scale"] = round(min(2.2, (p.get("element_scale") or 1.0) + float(v)), 3)
                applied.append(f"元素放大 → {p['element_scale']}")
            elif k in ("density", "gap", "evenness", "rotate_deg"):
                cur = p.get(k)
                if cur is None:
                    cur = {"density": 0.45, "gap": 0.12, "evenness": 0.8, "rotate_deg": 8}[k]
                p[k] = round(max(0.0, min(1.0 if k != "rotate_deg" else 45, cur + float(v))), 3)
                applied.append(f"{k} → {p[k]}")
    p["_need_regen"] = need_regen
    return p, applied
