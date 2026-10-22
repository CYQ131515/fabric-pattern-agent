"""知识库节点（0 号节点）：从 skill 的 references/*.md 里"学习"规则，变成可执行的决策。

设计原则
--------
1. **确定性优先**：Markdown 表格/列表能解析的绝不让 LLM 猜；LLM 只做兜底（可选）。
2. **可追溯**：每条规则记录来源（文件、标题、行号），报告里能说清"为什么这么排"。
3. **可失效**：按文件 sha256 判断知识库是否过期；过期就自动重新学习。
4. **只做建议**：知识给出 plan（骨式/接版/参数/坑/验收项），用户显式参数永远优先。

用法
----
    from pattern_studio import kb
    info = kb.ensure_fresh()          # 缺/过期则自动学习
    plan = kb.plan("国风碎花 满版", spec)
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

REF_GLOB = "references/*.md"
KB_VERSION = 1


# ---------------------------------------------------------------- 定位与扫描
def skill_dir() -> Path:
    import os

    env = os.environ.get("PATTERN_STUDIO_SKILL_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".agents" / "skills" / "pattern-design-atlas"


def kb_path() -> Path:
    import os

    env = os.environ.get("PATTERN_STUDIO_KB")
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parent.parent / "knowledge" / "kb.json"


def scan(sdir: Path | None = None) -> list[dict]:
    sdir = sdir or skill_dir()
    out: list[dict] = []
    for p in sorted(sdir.glob(REF_GLOB)):
        data = p.read_bytes()
        out.append({
            "file": str(p.relative_to(sdir)),
            "path": str(p),
            "sha256": hashlib.sha256(data).hexdigest()[:16],
            "bytes": len(data),
            "lines": data.count(b"\n") + 1,
            "mtime": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(p.stat().st_mtime)),
        })
    return out


def staleness() -> dict:
    """返回 {stale, known, current, changed}"""
    cur = scan()
    kp = kb_path()
    if not kp.exists():
        return {"stale": True, "reason": "no-kb", "current": cur}
    try:
        old = json.loads(kp.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"stale": True, "reason": "bad-kb", "current": cur}
    known = {s["file"]: s["sha256"] for s in old.get("_meta", {}).get("sources", [])}
    changed = [s["file"] for s in cur if known.get(s["file"]) != s["sha256"]]
    removed = [f for f in known if f not in {s["file"] for s in cur}]
    return {"stale": bool(changed or removed), "reason": "changed" if changed else ("removed" if removed else "fresh"),
            "changed": changed, "removed": removed, "current": cur}


# ---------------------------------------------------------------- Markdown 解析
def _split_sections(text: str) -> list[dict]:
    """按 ## / ### 切节，保留行号，便于追溯。"""
    lines = text.splitlines()
    secs: list[dict] = []
    cur = {"h1": "", "h2": "", "h3": "", "start": 0, "lines": []}
    for i, ln in enumerate(lines):
        m = re.match(r"^(#{1,3})\s+(.*)$", ln)
        if m:
            if cur["lines"]:
                cur["end"] = i
                secs.append(cur)
            level, title = len(m.group(1)), m.group(2).strip()
            cur = {"h1": cur["h1"], "h2": cur["h2"], "h3": cur["h3"],
                   "start": i + 1, "lines": [], "title": title, "level": level}
            if level == 1:
                cur["h1"], cur["h2"], cur["h3"] = title, "", ""
            elif level == 2:
                cur["h2"], cur["h3"] = title, ""
            else:
                cur["h3"] = title
        else:
            cur["lines"].append(ln)
    if cur["lines"]:
        cur["end"] = len(lines)
        secs.append(cur)
    return secs


def _tables(lines: list[str]) -> list[list[list[str]]]:
    """把一节里的所有 Markdown 表格解析成 [[row...], ...]。"""
    out, block = [], []
    for ln in lines:
        if ln.strip().startswith("|") and ln.count("|") >= 2:
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue
            block.append(cells)
        else:
            if len(block) >= 2:
                out.append(block)
            block = []
    if len(block) >= 2:
        out.append(block)
    return out


def _list_items(lines: list[str]) -> list[str]:
    items = []
    for ln in lines:
        m = re.match(r"^\s*(?:[-*]|\d+\.)\s+(.*)$", ln)
        if m:
            items.append(re.sub(r"\*\*|`", "", m.group(1)).strip())
    return items


def _prose(lines: list[str]) -> str:
    out = []
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith("|") or s.startswith("```"):
            continue
        out.append(re.sub(r"\*\*|`|>", "", s))
    return " ".join(out).strip()


def _strip_md(s: str) -> str:
    # 只去掉"1. "这种编号前缀（要求点号后有空格），否则会把 "0.22" 吃成 "22"
    return re.sub(r"\*\*|`|^\d+\.\s+(?=\S)", "", s).strip()


def _row_named(rows: list[list[str]], *keywords: str) -> list[str] | None:
    for r in rows:
        first = _strip_md(r[0]) if r else ""
        if any(k in first for k in keywords):
            return r
    return None


# ---------------------------------------------------------------- 学习（结构化）
def learn(sdir: Path | None = None, llm=None, verbose: bool = True) -> dict:
    """解析 skill 的 references/*.md，产出结构化知识库。"""
    sdir = sdir or skill_dir()
    srcs = scan(sdir)
    if not srcs:
        raise FileNotFoundError(f"skill 里没有 {REF_GLOB}：{sdir}")

    kb: dict = {
        "_meta": {"version": KB_VERSION, "learned_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                  "skill_dir": str(sdir), "sources": srcs,
                  "note": "由 pattern_studio.kb.learn() 自动生成；改 skill 后下次 run 会自动重学"},
        "skeletons": [], "repeat_modes": [], "cut_methods": [], "combos": [],
        "selection": [], "pitfalls": [], "gates": [], "tool_map": [],
        "styles": [],
        "sections": [],
    }

    for src in srcs:
        text = Path(src["path"]).read_text(encoding="utf-8")
        for sec in _split_sections(text):
            kb["sections"].append({
                "file": src["file"], "h2": sec.get("h2"), "h3": sec.get("h3"),
                "title": sec.get("title"), "start": sec.get("start"), "end": sec.get("end"),
                "text": _prose(sec["lines"])[:600],
            })
            lines = sec["lines"]
            title = sec.get("h3") or sec.get("h2") or ""
            tables = _tables(lines)
            items = _list_items(lines)
            prose = _prose(lines)

            # 三大骨式：h3 里出现"散点式/连缀式/重叠式"
            m = re.match(r"^(\d+\.\s*)?(散点式|连缀式|重叠式)", title)
            if m:
                kb["skeletons"].append({
                    "name": m.group(2), "file": src["file"], "line": sec.get("start"),
                    "desc": prose[:300],
                    "subtypes": [i for i in items][:8],
                    "tables": tables[:2],
                })
            # 接版：表格里出现 1/2、1/3 等比例
            if "接版" in (sec.get("h2") or "") or "跳接" in title:
                for t in tables:
                    for r in t:
                        joined = " ".join(r)
                        if re.search(r"1/2|1/3|1/4|2/5|半跳", joined) and len(r) >= 2:
                            kb["repeat_modes"].append({
                                "ratio": _strip_md(r[0]), "name": _strip_md(r[1]) if len(r) > 1 else "",
                                "note": _strip_md(r[2]) if len(r) > 2 else "",
                                "file": src["file"], "line": sec.get("start")})
                if "平接" in prose or "平接" in title:
                    kb["repeat_modes"].append({
                        "ratio": "平接", "name": "平排/方形回头",
                        "note": "单元上下左右直接对齐，拼接简单但网格感强；适合几何、定位花、简单小碎花",
                        "file": src["file"], "line": sec.get("start")})
            # 开刀法：四切/对角/卷折
            if "开刀" in (sec.get("h2") or "") or "开刀" in title:
                for t in tables:
                    for r in t:
                        joined = " ".join(r)
                        if any(k in joined for k in ("四切法", "对角开刀", "卷折法")):
                            kb["cut_methods"].append({
                                "name": _strip_md(r[0]), "how": _strip_md(r[1]) if len(r) > 1 else "",
                                "use": _strip_md(r[2]) if len(r) > 2 else "",
                                "file": src["file"], "line": sec.get("start")})
            # 组合衍生
            if "组合" in (sec.get("h2") or ""):
                for t in tables:
                    for r in t:
                        if len(r) >= 3:
                            kb["combos"].append({"combo": _strip_md(r[0]), "how": _strip_md(r[1]),
                                                 "effect": _strip_md(r[2]), "file": src["file"]})
            # 选型表
            if "选型" in (sec.get("h2") or ""):
                for t in tables:
                    for r in t:
                        if len(r) >= 3 and "排列方式" not in r[0]:
                            kb["selection"].append({"arrangement": _strip_md(r[0]),
                                                    "look": _strip_md(r[1]),
                                                    "use": _strip_md(r[2]), "file": src["file"]})
            # 避坑
            if "避坑" in (sec.get("h2") or "") or "坑" in title:
                for it in items:
                    kb["pitfalls"].append({"text": it, "file": src["file"], "line": sec.get("start")})
            # 验收自检
            if "验收" in (sec.get("h2") or "") or "自检" in title:
                for t in tables:
                    for r in t:
                        if len(r) >= 3 and "检查" not in r[1]:
                            kb["gates"].append({"name": _strip_md(r[1]), "criterion": _strip_md(r[2]),
                                               "file": src["file"]})
            # 风格配方表（表头含"风格"和"关键词"）
            for t in tables:
                if not t:
                    continue
                head = [c.strip() for c in t[0]]
                if "风格" in head and "关键词" in head:
                    idx = {h: i for i, h in enumerate(head)}
                    for r in t[1:]:
                        if len(r) < len(head) or not r[idx["风格"]].strip():
                            continue
                        def get(k: str) -> str:
                            i = idx.get(k)
                            return _strip_md(r[i]) if i is not None and i < len(r) else ""
                        def nums(k: str):
                            v = get(k).replace("±", "").replace("°", "").strip()
                            try:
                                return float(v)
                            except Exception:  # noqa: BLE001
                                return None
                        if get("骨式") or get("接版"):
                            kb["styles"].append({
                                "name": get("风格"),
                                "keywords": [x.strip() for x in re.split(r"[,，、]", get("关键词")) if x.strip()],
                                "skeleton": get("骨式"), "repeat": get("接版"),
                                "presets": [x.strip() for x in re.split(r"[,，、]", get("预设")) if x.strip()],
                                "rotate": nums("旋转"), "layer": get("层叠"),
                                "density": nums("目标覆盖"), "element_scale": nums("元素缩放"),
                                "elements": get("元素类型"),
                                "pitfalls": [x.strip() for x in re.split(r"[;；]", get("避坑")) if x.strip()],
                                "file": src["file"], "line": sec.get("start"),
                            })
            # 工具映射
            if "工具" in (sec.get("h2") or "") or "映射" in title:
                for t in tables:
                    for r in t:
                        if len(r) >= 3 and "概念" not in r[0]:
                            kb["tool_map"].append({"concept": _strip_md(r[0]),
                                                   "param": _strip_md(r[1]),
                                                   "status": _strip_md(r[2]), "file": src["file"]})

    # 去重（接版表按 ratio+name 去重，避免平接重复）
    for key in ("skeletons", "cut_methods", "combos", "selection", "pitfalls", "gates", "tool_map"):
        seen, uniq = set(), []
        for x in kb[key]:
            sig = json.dumps(x, ensure_ascii=False, sort_keys=True)
            if sig in seen:
                continue
            seen.add(sig)
            uniq.append(x)
        kb[key] = uniq
    seen, uniq = set(), []
    for x in kb["repeat_modes"]:
        sig = (x.get("ratio"), x.get("name"))
        if sig in seen:
            continue
        seen.add(sig)
        uniq.append(x)
    kb["repeat_modes"] = uniq

    kp = kb_path()
    kp.parent.mkdir(parents=True, exist_ok=True)
    kp.write_text(json.dumps(kb, ensure_ascii=False, indent=1), encoding="utf-8")
    if verbose:
        print(f"[知识节点] 已学习 {len(srcs)} 篇 skill 文档 → {kp}")
        print(f"[知识节点] 骨式 {len(kb['skeletons'])} · 接版 {len(kb['repeat_modes'])} · "
              f"开刀 {len(kb['cut_methods'])} · 组合 {len(kb['combos'])} · 选型 {len(kb['selection'])} · "
              f"避坑 {len(kb['pitfalls'])} · 验收 {len(kb['gates'])} · 工具映射 {len(kb['tool_map'])}")
    return kb


def load() -> dict | None:
    kp = kb_path()
    if not kp.exists():
        return None
    try:
        return json.loads(kp.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def ensure_fresh(verbose: bool = True) -> dict:
    """缺 / 过期 → 自动重新学习；否则直接用。"""
    st = staleness()
    if st["stale"]:
        if verbose and st.get("changed"):
            print(f"[知识节点] skill 有更新（{', '.join(st['changed'])}）→ 重新学习")
        return learn(verbose=verbose)
    kb = load() or learn(verbose=verbose)
    if verbose:
        n = len(kb.get("_meta", {}).get("sources", []))
        print(f"[知识节点] 知识库已就绪（{n} 篇文档，{kb['_meta'].get('learned_at')}）")
    return kb


# ---------------------------------------------------------------- 决策规则
SKELETON_RULES = [
    ("连缀式", r"缠枝|藤蔓|卷草|连缀|波线|互锁|连续纹|二方连续|藤|枝蔓"),
    ("重叠式", r"底纹|地纹|浮花|浮纹|重叠|双层|影纹|暗纹|层次纹"),
    ("散点式", r"碎花|散点|小花|花卉|花型|满版|满地花|离散|ditsy|野花"),
]
REPEAT_RULES = [
    ("平接", r"格子|几何|定位|条格|小面积|童装|简约|边框|团花"),
    ("1/2 跳接", r"满版|满印|大面积|女装|连衣裙|碎花|家纺|窗帘|真丝|旗袍"),
]

# 骨式 → 排版预设（工具现有能力）
SKELETON_PRESETS = {
    "散点式": ["halfdrop", "scatter", "phyllotaxis"],
    "连缀式": ["brick", "halfdrop", "diagonal"],
    "重叠式": ["scatter", "halfdrop", "phyllotaxis"],
}
REPEAT_PRESET = {"平接": "grid", "1/2 跳接": "halfdrop"}


def _hit(text: str, rules) -> tuple[str | None, str | None]:
    for name, pat in rules:
        m = re.search(pat, text)
        if m:
            return name, m.group(0)
    return None, None


def plan(keywords: str, spec: dict | None = None) -> dict:
    """根据知识库 + 关键词给出排布方案（只做建议，用户参数优先）。"""
    kb = load() or learn(verbose=False)
    spec = spec or {}
    text = " ".join([keywords or "", " ".join(spec.get("style") or []),
                     " ".join(spec.get("subjects") or []), spec.get("notes") or ""])

    # ① 风格配方表优先（关键词命中数最多的一行）
    best_style, best_hits = None, []
    for row in (kb.get("styles") or []):
        hits = [k for k in row.get("keywords") or [] if k and k.lower() in text.lower()]
        if len(hits) > len(best_hits):
            best_style, best_hits = row, hits

    sk, sk_hit = _hit(text, SKELETON_RULES)
    rp, rp_hit = _hit(text, REPEAT_RULES)
    sk = (best_style or {}).get("skeleton") or sk or "散点式"
    rp = (best_style or {}).get("repeat") or rp or "1/2 跳接"

    presets = list((best_style or {}).get("presets") or SKELETON_PRESETS.get(sk, ["halfdrop", "scatter"]))
    if rp == "平接" and "grid" not in presets:
        presets = ["grid"] + presets

    # 重叠式 → 开层叠；散点 → 适度旋转翻转（避坑第 3 条）
    layer = (best_style or {}).get("layer") or ""
    overlap_mode = "layered" if (sk == "重叠式" or "层叠" in layer) else "none"
    rotate = (best_style or {}).get("rotate")
    if rotate is None:
        rotate = 0 if sk == "连缀式" else 10
    mirror = 0.2 if sk != "连缀式" else 0.05
    density_hint = (best_style or {}).get("density")
    escale_hint = (best_style or {}).get("element_scale")

    # 命中的知识条目（可追溯）
    evid: list[dict] = []
    for sec in kb.get("sections") or []:
        title = (sec.get("title") or "")
        h2 = (sec.get("h2") or "")
        key = sk.replace("式", "")
        hit = (key and key in title) or ("接版" in h2 and ("跳接" in title or "平接" in title)) \
            or ("骨式" in h2 and key and key in title)
        if hit:
            evid.append({"file": sec["file"], "title": title or h2, "line": sec.get("start")})
    pitfalls = [p for p in (kb.get("pitfalls") or [])][:6]
    gates = [g for g in (kb.get("gates") or [])][:6]
    cut = [c for c in (kb.get("cut_methods") or [])]
    tools = [t for t in (kb.get("tool_map") or [])]

    style_pitfalls = [{"text": t, "file": (best_style or {}).get("file"),
                       "line": (best_style or {}).get("line")}
                      for t in ((best_style or {}).get("pitfalls") or [])]
    return {
        "style": (best_style or {}).get("name"),
        "style_hits": best_hits,
        "skeleton": sk, "skeleton_hit": sk_hit,
        "repeat": rp, "repeat_hit": rp_hit,
        "presets": presets[:3],
        "overlap_mode": overlap_mode,
        "rotate_deg": rotate, "mirror_prob": mirror,
        "density": density_hint, "element_scale": escale_hint,
        "style_pitfalls": style_pitfalls,
        "pitfalls": pitfalls, "gates": gates, "cut_methods": cut,
        "tool_status": tools,
        "evidence": evid[:6],
        "sources": [s["file"] for s in (kb.get("_meta", {}).get("sources") or [])],
        "kb_learned_at": kb.get("_meta", {}).get("learned_at"),
    }


def explain(p: dict) -> str:
    """把 plan 变成一行人类可读的说明。"""
    bits = []
    if p.get("style"):
        bits.append(f"风格配方={p['style']}（命中 {'/'.join(p.get('style_hits') or [])}）")
    bits += [f"骨式={p['skeleton']}" + (f"（命中「{p['skeleton_hit']}」）" if p.get("skeleton_hit") else ""),
            f"接版={p['repeat']}" + (f"（命中「{p['repeat_hit']}」）" if p.get("repeat_hit") else ""),
            f"预设=" + "/".join(p["presets"]),
            f"层叠={p['overlap_mode']}"]
    return " · ".join(bits)


def review_criteria(p: dict, kb: dict | None = None) -> str:
    """把知识库的"避坑 + 验收"变成审核提示词的附加标准。"""
    kb = kb or load() or {}
    lines = ["【工厂标准·必须对照】"]
    for g in (kb.get("gates") or [])[:8]:
        lines.append(f"- {g['name']}：{g['criterion']}")
    for it in (kb.get("pitfalls") or [])[:6]:
        lines.append(f"- 避坑：{it['text']}")
    return "\n".join(lines)
