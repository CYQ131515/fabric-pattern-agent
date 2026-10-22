"""一个配置文件管三件事：**模型、爬虫目标、筛选项**。

为什么单独开一层
    这三样是"用户要改的"，其余是"代码的脾气"。以前它们散在：模型走环境变量
    （或后端已配的那份）、爬虫目标写死在 `providers.DEFAULT_ORDER`、筛选项是
    命令行开关。想改一次得记三个地方 —— 于是有了这个文件。

    **优先级（同一样东西按这个顺序取）**
        命令行参数  >  环境变量  >  `config.json`  >  代码里的默认值
    也就是说：配置文件是"日常设置"，临时想覆盖就加命令行参数，不会把文件改脏。

文件放哪
    默认 `算法/全网搜花型/config.json`（跟 run.py 同一层，改完不用重启）。
    想用另一份：设环境变量 `WSEARCH_CONFIG=/path/to/xxx.json`。
    测试里设 `WSEARCH_NO_CONFIG=1` 就会**完全忽略**配置文件（否则跑测试
    会被本机的模型设置带跑偏）。

规矩（跟项目里其它配置一个口径）
    · **没有这个文件 = 一切照旧**，不报错、不生成、不刷日志；
    · 解析失败只记一条警告然后当没有，**绝不因为配置写错就崩掉一轮搜索**；
    · 未知字段忽略（这样以后加字段不会把老版本的文件读崩）；
    · `api_key` 可以直接写在这里图省事，但**别提交到仓库** —— 更推荐
      留空、用 `api_key_env` 指环境变量（见 config.json 里的注释字段）。
"""
from __future__ import annotations

import json
import os
import threading

# 每一段的默认值。**只写"没配置时该是什么"，别在这里塞站点/key。**
DEFAULTS = {
    "ai": {
        "base_url": "",             # 对话模型（需求分析）OpenAI 兼容地址
        "api_key": "",              # 直接写 key（省事但不建议提交）
        "api_key_env": "WSEARCH_LLM_KEY",
        "model": "",
        "vision_base_url": "",      # 视觉模型（语义甄别）地址；留空=跟对话模型同一家
        "vision_api_key": "",
        "vision_api_key_env": "WSEARCH_VLM_KEY",
        "vision_model": "",
        "timeout": 0,               # 0 = 各层自己的默认
    },
    "crawlers": {
        "disabled": [],             # 关掉哪些站（用 providers 里的名字，如 "bing"）
        "custom": [],               # 加站：见 config.json 里的示例字段
    },
    "filters": {
        "quality_gate": True,       # 不看接缝，也拦"合集/展示图/白边/糊图"的质量闸门
        "tile_check": True,         # 四/方连续检测本身开不开（False=完全不跑接缝判定）
        "tile_gate": False,         # 接缝要不要当筛选条件（默认不筛）
        "screen": True,             # 语义甄别（看图复核）开不开
        "similar": True,            # 相似去重开不开
        "similar_threshold": 0.995,
        "min_side": 0,              # >0 时：最短边小于它的图不要
        "drop_flat": True,          # 整张纯色的图要不要丢
    },
    # 2026-10-19：质量优先级 = 符合需求 > 内容新 > 尽量多 > 画质。
    # 这些权重给 rank.score_of 用；命令行/代码默认值在 rank.WEIGHTS。
    "quality": {
        # 2026-10-19 取消四方连续后：tile 权重 0，挪给来源/新鲜/分辨率。
        "source": 0.38, "fresh": 0.24, "tile": 0.00, "graphic": 0.10,
        "res": 0.14, "provider": 0.07, "engage": 0.07,
        "fresh_days": 0,             # >0 时：发布时间早于 N 天的图直接不要（0=不过滤，只降权）
    },
    # 精选站搜索（需要 Serper key）：按意图取前 N 个域名；domains 留空 = 自动。
    "site_search": {
        "domains": [],               # 例如 ["etsy.com","creativemarket.com"]
        "limit": 4,
    },
}

_LOCK = threading.Lock()
_CACHE = {"path": None, "data": None, "warned": set()}


def path(env=None):
    """配置文件路径。`WSEARCH_CONFIG` 可覆盖，默认跟 run.py 同层。"""
    e = env if env is not None else os.environ
    if (e.get("WSEARCH_NO_CONFIG") or "").strip() not in ("", "0", "false"):
        return ""
    p = (e.get("WSEARCH_CONFIG") or "").strip()
    if p:
        return os.path.abspath(os.path.expanduser(p))
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "config.json")


def _merge(base, over):
    """两层的浅合并：段内逐键覆盖，段外整段替换（数组一律整段换，不拼）。"""
    out = {}
    for k, v in (base or {}).items():
        out[k] = dict(v) if isinstance(v, dict) else v
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


def load(force=False, file_path=None):
    """读配置并跟默认值合并。**永远回一个完整 dict**，读不到就是默认值。"""
    p = file_path if file_path is not None else path()
    if not p:
        return _merge(DEFAULTS, {})
    with _LOCK:
        if not force and _CACHE["data"] is not None and _CACHE["path"] == p:
            return _CACHE["data"]
        data = {}
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    data = raw
            except Exception as e:                           # noqa: BLE001
                if p not in _CACHE["warned"]:                # 只警告一次，别刷屏
                    _CACHE["warned"].add(p)
                    print("· 配置文件读不了（%s）：%s —— 按默认设置继续" 
                          % (os.path.basename(p), e))
        merged = _merge(DEFAULTS, data)
        merged["_path"] = p if os.path.exists(p) else ""
        _CACHE["path"], _CACHE["data"] = p, merged
        return merged


def section(name, file_path=None):
    """取一段（永远回 dict，字段齐全）。"""
    return dict(load(file_path=file_path).get(name) or {})


def ai(file_path=None):
    """对话模型（需求分析）的 {base, key, model, timeout}。

    key 的取法：`api_key` 直接写 > `api_key_env` 指的**环境变量**。
    环境变量优先于文件 —— 这样临时换 key 不用改文件，也不会把 key 落盘。
    """
    s = section("ai", file_path)
    env = os.environ
    key = (s.get("api_key") or "").strip()
    env_name = (s.get("api_key_env") or "").strip()
    if env_name and (env.get(env_name) or "").strip():
        key = env[env_name].strip()
    return {"base": (s.get("base_url") or "").strip(),
            "key": key,
            "model": (s.get("model") or "").strip(),
            "timeout": int(s.get("timeout") or 0)}


def vision(file_path=None):
    """视觉模型（语义甄别）。没单独配就**复用对话模型那套**（同一家 OpenAI 兼容服务）。"""
    a, s = ai(file_path), section("ai", file_path)
    base = (s.get("vision_base_url") or "").strip() or a["base"]
    model = (s.get("vision_model") or "").strip() or a["model"]
    key = (s.get("vision_api_key") or "").strip()
    env_name = (s.get("vision_api_key_env") or "").strip()
    if env_name and (os.environ.get(env_name) or "").strip():
        key = os.environ[env_name].strip()
    if not key and not s.get("vision_base_url"):
        key = a["key"]                    # 同一家服务，key 就那一个
    return {"base": base, "key": key, "model": model,
            "timeout": int(s.get("timeout") or 0)}


def filters(file_path=None):
    """筛选项。类型都在这里兜一次底 —— 手写的 JSON 很容易写成 "false" 字符串。"""
    s = section("filters", file_path)

    def flag(v, d):
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() not in ("false", "0", "no", "off", "")
        return d if v is None else bool(v)

    thr = s.get("similar_threshold")
    try:
        thr = float(thr)
    except Exception:                                        # noqa: BLE001
        thr = DEFAULTS["filters"]["similar_threshold"]
    try:
        min_side = int(s.get("min_side") or 0)
    except Exception:                                        # noqa: BLE001
        min_side = 0
    return {"quality_gate": flag(s.get("quality_gate"), True),
            "tile_check": flag(s.get("tile_check"), True),
            "tile_gate": flag(s.get("tile_gate"), False),
            "screen": flag(s.get("screen"), True),
            "similar": flag(s.get("similar"), True),
            "similar_threshold": thr,
            "min_side": max(0, min_side),
            "drop_flat": flag(s.get("drop_flat"), True)}


def crawlers(file_path=None):
    """爬虫目标：`disabled`（关掉哪几家）+ `custom`（加站，见 README 的字段表）。"""
    s = section("crawlers", file_path)
    dis = s.get("disabled") or []
    cus = s.get("custom") or []
    return {"disabled": [str(x).strip() for x in dis if str(x).strip()],
            "custom": [c for c in cus if isinstance(c, dict) and c.get("name")]}


def quality(file_path=None):
    """排序权重 + 新鲜度过滤天数。字段缺了用 rank 的默认值兜底。"""
    s = section("quality", file_path)
    out = {}
    for k in ("source", "fresh", "tile", "graphic", "res", "provider", "engage"):
        try:
            out[k] = float(s.get(k))
        except Exception:                                    # noqa: BLE001
            pass
    try:
        out["fresh_days"] = max(0, int(s.get("fresh_days") or 0))
    except Exception:                                        # noqa: BLE001
        out["fresh_days"] = 0
    return out


def site_search(file_path=None):
    """精选站搜索：domains（空=按意图自动）+ limit。"""
    s = section("site_search", file_path)
    doms = s.get("domains") or []
    if isinstance(doms, str):
        doms = [x.strip() for x in doms.replace("，", ",").split(",") if x.strip()]
    doms = [str(x).strip() for x in doms if str(x).strip()]
    try:
        limit = max(1, min(12, int(s.get("limit") or 4)))
    except Exception:                                        # noqa: BLE001
        limit = 4
    return {"domains": doms, "limit": limit}


def describe():
    """给 `run.py keys` 用的一行人话：配置文件在哪、三段各是什么状态。"""
    d = load()
    p = d.get("_path") or ""
    f = filters()
    c = crawlers()
    bits = ["配置：%s" % (p or "没有 config.json（全用默认值）")]
    if c["disabled"]:
        bits.append("关掉 %d 家（%s）" % (len(c["disabled"]), "、".join(c["disabled"])))
    if c["custom"]:
        live = [x for x in c["custom"] if x.get("enabled") is not False]
        if live:
            bits.append("自建 %d 家（%s）" % (len(live),
                                            "、".join(str(x["name"]) for x in live)))
    bits.append("接缝筛选=%s" % ("开" if f["tile_gate"] else "关"))
    bits.append("语义甄别=%s" % ("开" if f["screen"] else "关"))
    bits.append("相似去重=%s" % ("开" if f["similar"] else "关"))
    q = quality()
    bits.append("排序=来源%.2f/新鲜%.2f/平铺%.2f/画质%.2f"
                % (q.get("source", 0.30), q.get("fresh", 0.20),
                   q.get("tile", 0.22), q.get("res", 0.08)))
    return "；".join(bits)
