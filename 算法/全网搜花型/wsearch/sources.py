"""来源图（source graph）：把"从哪拿图"做成一张可配置的表。

为什么要单独一层
    以前来源只是 `providers.REGISTRY` 里的一个 `kind`（search/stock），
    排序时只能区分"搜索引擎 vs 图库"两档。Pinterest 那套供给里真正值钱的是
    **专业设计交易站 / 作品集 / 品牌壁纸 / 日本插画素材站**，它们和百度图片
    不在一个质量档位上；再加"来源是否可追溯、有没有授权"。

本模块只放**元数据**，不 import providers（避免循环）。providers 用它给每条
候选打 quality_prior/kind，rank 用它算来源分，run.py sources 用它列清单。

三层
    · CORE：已经接进 providers.REGISTRY 的来源（免 key 的、Spoonflower 等）
    · SITE：只能通过 Google/Bing `site:` 搜索代理访问的来源（Etsy 等）。
      需要 WSEARCH_SERPER_KEY；provider 名统一写成 `site:<domain>`。
    · 意图：business（找可印花素材）/ inspiration（找风格灵感）/ specific（按花名）
      同一来源在不同意图下权重不同。
"""
from __future__ import annotations

# 质量先验：0~1。不是"客观评分"，是"这个来源里花型密度的经验值"。
# 依据见 README 与 _rea 调研：交易平台过平铺闸门约七成，通用引擎 18~27%。
CORE = {
    "walan":       {"kind": "marketplace", "quality": 0.95, "lang": "zh", "intents": ("business", "pattern")},
    "dancecolor":  {"kind": "marketplace", "quality": 0.94, "lang": "zh", "intents": ("business", "pattern")},
    "tnc":         {"kind": "marketplace", "quality": 0.93, "lang": "zh", "intents": ("business", "pattern")},
    "spoonflower": {"kind": "marketplace", "quality": 0.95, "lang": "en", "intents": ("business", "pattern")},
    "patternbank": {"kind": "marketplace", "quality": 0.92, "lang": "en", "intents": ("business", "pattern")},
    "patternbulk": {"kind": "marketplace", "quality": 0.88, "lang": "en", "intents": ("business", "pattern")},
    "clip_studio": {"kind": "illustration", "quality": 0.82, "lang": "ja", "intents": ("inspiration", "business")},
    "patterndesigns": {"kind": "marketplace", "quality": 0.66, "lang": "en", "intents": ("business", "pattern")},
    "huaban":      {"kind": "community",   "quality": 0.65, "lang": "zh", "intents": ("inspiration",)},
    "duitang":     {"kind": "community",   "quality": 0.60, "lang": "zh", "intents": ("inspiration",)},
    "ypattern":    {"kind": "marketplace", "quality": 0.55, "lang": "zh", "intents": ("business",)},
    "baidu":       {"kind": "search",      "quality": 0.55, "lang": "zh", "intents": ("business", "inspiration")},
    "so360":       {"kind": "search",      "quality": 0.55, "lang": "zh", "intents": ("business", "inspiration")},
    "bing":        {"kind": "search",      "quality": 0.55, "lang": "en", "intents": ("business", "inspiration")},
    "serper":      {"kind": "search",      "quality": 0.55, "lang": "en", "intents": ("business", "inspiration")},
    "serpapi":     {"kind": "search",      "quality": 0.55, "lang": "en", "intents": ("business", "inspiration")},
    "brave":       {"kind": "search",      "quality": 0.55, "lang": "en", "intents": ("business", "inspiration")},
    "pixabay":     {"kind": "stock",       "quality": 0.40, "lang": "en", "intents": ("inspiration",)},
    "pexels":      {"kind": "stock",       "quality": 0.40, "lang": "en", "intents": ("inspiration",)},
    "unsplash":    {"kind": "stock",       "quality": 0.40, "lang": "en", "intents": ("inspiration",)},
    # 2026-10-20 可直爬来源
    "designspiration": {"kind": "community", "quality": 0.62, "lang": "en", "intents": ("inspiration", "business")},
    "openclipart":     {"kind": "illustration", "quality": 0.52, "lang": "en", "intents": ("inspiration", "specific")},
    "irasutoya":       {"kind": "illustration", "quality": 0.55, "lang": "ja", "intents": ("inspiration", "specific")},
    "printpattern":    {"kind": "community", "quality": 0.68, "lang": "en", "intents": ("inspiration", "business")},
    "patternobserver": {"kind": "community", "quality": 0.62, "lang": "en", "intents": ("inspiration", "business")},
    "poptnc":          {"kind": "marketplace", "quality": 0.80, "lang": "zh", "intents": ("business", "pattern")},
    "publicdomainpictures": {"kind": "stock", "quality": 0.50, "lang": "en", "intents": ("inspiration", "specific")},
    "pxhere":          {"kind": "stock", "quality": 0.42, "lang": "en", "intents": ("inspiration",)},
    "stocksnap":       {"kind": "stock", "quality": 0.42, "lang": "en", "intents": ("inspiration",)},
}

# 只能通过 Google/Bing `site:` 访问的来源：抓取受反爬/ToS 限制，
# 但用搜索代理拿"这个站里被收录的图"是合规的（和直接抓站不是一回事）。
# `provider` 名 = `site:<domain>`；quality 是"这个站里花型/设计图的密度"。
SITE = {
    "etsy":            {"domain": "etsy.com",            "quality": 0.95, "kind": "marketplace", "lang": "en", "intents": ("business", "pattern")},
    "creativemarket":  {"domain": "creativemarket.com",  "quality": 0.93, "kind": "marketplace", "lang": "en", "intents": ("business", "pattern")},
    "creativefabrica": {"domain": "creativefabrica.com", "quality": 0.88, "kind": "marketplace", "lang": "en", "intents": ("business", "pattern")},
    "payhip":          {"domain": "payhip.com",          "quality": 0.75, "kind": "marketplace", "lang": "en", "intents": ("business", "pattern")},
    "behance":         {"domain": "behance.net",         "quality": 0.88, "kind": "portfolio",   "lang": "en", "intents": ("inspiration", "specific")},
    "dribbble":        {"domain": "dribbble.com",        "quality": 0.84, "kind": "portfolio",   "lang": "en", "intents": ("inspiration",)},
    "instagram":       {"domain": "instagram.com",       "quality": 0.68, "kind": "social",      "lang": "en", "intents": ("inspiration",)},
    "anthropologie":   {"domain": "anthropologie.com",   "quality": 0.82, "kind": "brand",       "lang": "en", "intents": ("business", "pattern")},
    "littlegreene":    {"domain": "littlegreene.us",     "quality": 0.82, "kind": "brand",       "lang": "en", "intents": ("business", "pattern")},
    "wallsrepublic":   {"domain": "wallsrepublic.com",   "quality": 0.82, "kind": "brand",       "lang": "en", "intents": ("business", "pattern")},
    "chasingpaper":    {"domain": "chasingpaper.com",    "quality": 0.82, "kind": "brand",       "lang": "en", "intents": ("business", "pattern")},
    "wallpaper70s":    {"domain": "wallpaperfromthe70s.com", "quality": 0.80, "kind": "brand",   "lang": "en", "intents": ("business", "pattern")},
    "decoratorsbest":  {"domain": "decoratorsbest.com",  "quality": 0.78, "kind": "brand",       "lang": "en", "intents": ("business", "pattern")},
    "fabricmegastore": {"domain": "fabricmegastore.com", "quality": 0.78, "kind": "brand",       "lang": "en", "intents": ("business", "pattern")},
    "shabbyfabrics":   {"domain": "shabbyfabrics.com",   "quality": 0.76, "kind": "brand",       "lang": "en", "intents": ("business", "pattern")},
    "ac_illust":       {"domain": "ac-illust.com",       "quality": 0.78, "kind": "illustration","lang": "ja", "intents": ("inspiration", "specific")},
    "garafactory":     {"domain": "garafactory.com",     "quality": 0.72, "kind": "illustration","lang": "ja", "intents": ("inspiration", "specific")},
    "yuzawaya":        {"domain": "yuzawaya.shop",       "quality": 0.68, "kind": "brand",      "lang": "ja", "intents": ("business",)},
    "tomoyasyugeiten": {"domain": "tomoyasyugeiten.jp",  "quality": 0.68, "kind": "brand",      "lang": "ja", "intents": ("business",)},
    "hansoku_create":  {"domain": "hansoku-create.jp",   "quality": 0.66, "kind": "brand",      "lang": "ja", "intents": ("business",)},
    "adobe_stock":     {"domain": "stock.adobe.com",     "quality": 0.60, "kind": "stock",      "lang": "en", "intents": ("specific",)},
    "123freevectors":  {"domain": "123freevectors.com",  "quality": 0.55, "kind": "stock",      "lang": "en", "intents": ("specific",)},
    "magnific":        {"domain": "magnific.com",        "quality": 0.55, "kind": "stock",      "lang": "en", "intents": ("specific",)},
}

DEFAULT_PRIOR = 0.50

# 意图对来源的加减。business=找可印花/可采购；inspiration=找风格；
# specific=按具体花名找。只调 rank 的 source_prior，不改变"能不能抓"。
INTENT_BONUS = {
    "business":    {"marketplace": 0.10, "brand": 0.06, "search": -0.04, "stock": -0.10, "community": -0.06, "illustration": -0.02},
    "inspiration": {"portfolio": 0.08, "community": 0.06, "illustration": 0.05, "marketplace": 0.02, "stock": -0.04},
    "specific":    {"illustration": 0.05, "marketplace": 0.04, "portfolio": 0.02, "search": -0.02},
}


def _norm_provider(name):
    return (name or "").strip()


def meta(name):
    """按 provider 名取来源元数据。`site:etsy.com` 也能反查。"""
    n = _norm_provider(name)
    if n.startswith("site:"):
        dom = n.split(":", 1)[1].lower()
        for k, v in SITE.items():
            if v.get("domain") == dom:
                return dict(v, name=k, provider=n)
        return {"name": dom, "kind": "search", "quality": DEFAULT_PRIOR,
                "lang": "", "intents": ()}
    if n in CORE:
        return dict(CORE[n], name=n, provider=n)
    # 兼容 providers.REGISTRY 里 kind 字段（没登记进 CORE 的 fake/custom）
    return {"name": n, "kind": "", "quality": DEFAULT_PRIOR, "lang": "", "intents": ()}


def kind(name):
    return meta(name).get("kind") or ""


def prior(name):
    return float(meta(name).get("quality") or DEFAULT_PRIOR)


def effective_prior(name, intent=None):
    """来源质量 + 意图匹配。回 0~1，给 rank 用。"""
    m = meta(name)
    base = float(m.get("quality") or DEFAULT_PRIOR)
    k = m.get("kind") or ""
    bonus = 0.0
    if intent:
        bonus = float((INTENT_BONUS.get(intent) or {}).get(k, 0.0))
        if intent in (m.get("intents") or ()):
            bonus += 0.03
    return max(0.0, min(1.0, base + bonus))


def site_domains(intent=None, limit=0, names=None):
    """给 site_search 用：按意图返回优先域名。"""
    out = []
    wanted = set(names or [])
    for k, v in SITE.items():
        if wanted and k not in wanted and v.get("domain") not in wanted:
            continue
        score = effective_prior("site:" + v["domain"], intent)
        out.append((score, v["domain"], k))
    out.sort(key=lambda x: (-x[0], x[1]))
    if limit:
        out = out[:limit]
    return [x[1] for x in out]


def site_names():
    return list(SITE)


def all_meta():
    rows = []
    for n, v in CORE.items():
        rows.append(dict(v, name=n, provider=n, access="direct"))
    for n, v in SITE.items():
        rows.append(dict(v, name=n, provider="site:" + v["domain"], access="site"))
    return rows


def summary():
    """run.py sources 用的一行行人话。"""
    lines = []
    for m in all_meta():
        lines.append("%-16s %-12s q=%.2f lang=%-2s %s  %s"
                     % (m.get("name"), m.get("kind") or "?", float(m.get("quality") or 0),
                        m.get("lang") or "-", m.get("access"),
                        m.get("domain") or ""))
    return "\n".join(lines)
