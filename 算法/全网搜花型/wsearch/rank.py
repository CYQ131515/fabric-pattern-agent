"""排序与挑选：把"所有合格的平铺图"排成"最该给用户看的 100 张"。

排序看四件事（都是被真实搜索结果逼出来的）

  ① **平铺质量**（tile.quality）—— 接缝越看不出来越靠前。这是主项：
     用户要的就是能直接铺的图。

  ② **去重到"看起来不是同一张"**。搜索结果的重复率高得离谱：同一张图
     在 5 个站上转载、图床给 3 种尺寸、还有一堆只差一点点的变体。
     按 URL 和 md5 去重只能干掉完全相同的字节，**拦不住"差一点点的变体"**。
     所以这里加一层**感知哈希（aHash）**：把图缩到 8×8 灰度取均值，
     得到 64 位指纹，两张图指纹的汉明距离小于阈值就认为"看着是同一张"。
     不这么干的话，前 100 张里可能有 30 张是同一朵花的不同尺寸。

  ③ **来源权重**。图片搜索（Serper/SerpAPI/Brave）是真全网，图库站
     （Pexels/Pixabay）是补充 —— 同分时全网来的排前面。同时也是"别让
     单一来源塞满整个列表"：同站来源超过一定比例就降权。

  ④ **分辨率**。用户说了暂时不管清晰度，但**太小的图铺开就是马赛克**。
     所以不是硬门槛，而是打分项：边长大的一路加分，到 1600 封顶
     （超过之后对"看花型"没有增量，再大只是下载慢）。

另外两处刻意的不作为
    · **不做"和需求的相关性打分"**。检索词是模型给的、搜索站按自己的
      相关性排过序了，再自己算一遍相似度只会引入噪声。相关性靠
      **检索词的质量**（analyze.py）和**来源顺序**来体现。
    · **不因为"看着像照片"就丢掉**。tile.graphic 只是打分项。合成标定
      证明了纯几何特征分不开"贴卡纸的展示图"和"素底上的一朵大花"，
      真要判得靠语义复核（见 README 的"还差什么"一节）。
"""
from __future__ import annotations

import email.utils
import math
import os
import re
import time
from datetime import datetime, timezone

from PIL import Image

from . import sources as _sources

# 感知哈希的汉明距离阈值：小于它就算"同一张"。64 位里差 6 位以内，
# 实测能把"同一张图的不同尺寸/轻微压缩"归到一起，又不会误伤同系列
# 但不同的花型（那种通常差 15 位以上）。
HASH_NEAR = 6

# 单一来源（域名）最多占多少比例，超出的降权。搜索站前排经常被
# 一两个站霸榜（比如全是 Pinterest），不压一下列表会很难看。
SITE_QUOTA = 0.28

# 2026-10-19：单一 provider（瓦栏/色彩跳动/tnc/patternbulk…）也不能霸榜。
# 用户要"从更多来源检索"，如果高先验的四家把 30 个名额全占了，结果看起来
# 还是只有四个站。provider 配额先压到 22%，再用第二/第三轮补满。
PROVIDER_QUOTA = 0.22

# 来源类型配额：交易平台最多占 70%，剩下的名额留给搜索/社区/插画/品牌。
# 不这么做的话，5 家高先验交易平台 ×6 = 30 个名额，结果看起来还是"只有那几个站"，
# 用户要的"从更多来源检索"在最终列表里体现不出来。
KIND_QUOTA = {"marketplace": 0.70}

# 分辨率加分的封顶边长
RES_FULL = 1600

# 来源类型权重（兼容旧配置；新排序用 sources.effective_prior + WEIGHTS）
KIND_W = {"search": 1.0, "stock": 0.86}

# 2026-10-19 新排序权重。用户给的优先级：
#   符合需求 > 内容新 > 尽量多 > 画质尽量高
# "符合需求"主要由 screen 的 match 硬闸门保证，排序里用来源/意图先验；
# "内容新"给 0.20；"画质"只给 0.08（用户明确说画质不是第一位）；
# "尽量多"不是排序项，靠候选量/来源数/配额控制。
WEIGHTS = {
    # 2026-10-19 用户明确"取消四方连续检测标准"：tile 不再参与排序。
    # 原来 tile 的 0.22 挪给来源/新鲜/分辨率；画质仍排最后。
    "source": 0.38,     # 来源类型 + 意图匹配（marketplace/portfolio/brand…）
    "fresh": 0.24,      # 发布时间新鲜度
    "tile": 0.00,       # 不再按平铺质量排序（tile_check 默认关）
    "graphic": 0.10,    # 越像图形越好
    "res": 0.14,        # 分辨率
    "provider": 0.07,   # 检索词顺序
    "engage": 0.07,     # 商家侧热度（销量/收藏；拿不到按 0.5）
}

# 新鲜度：90 天内满分，之后衰减；未知按 0.5，不因为"没日期"就判死。
FRESH_FULL_DAYS = 90
FRESH_ZERO_DAYS = 1095


def ahash(path, size=8):
    """8×8 均值哈希，回一个整数（64 位）。读不出来回 None。"""
    try:
        with Image.open(path) as im:
            g = im.convert("L").resize((size, size), Image.LANCZOS)
            px = list(g.getdata())
    except Exception:                                        # noqa: BLE001
        return None
    avg = sum(px) / float(len(px))
    bits = 0
    for i, v in enumerate(px):
        if v >= avg:
            bits |= (1 << i)
    return bits


def _hamming(a, b):
    if a is None or b is None:
        return 64
    return bin(a ^ b).count("1")


def _res_score(w, h):
    side = min(int(w or 0), int(h or 0))
    if side <= 0:
        return 0.30
    return min(1.0, (side / float(RES_FULL)) ** 0.5)


def parse_time(v):
    """把各种来源的日期/时间戳解析成 datetime（UTC）。解析不了回 None。

    见过的格式：
      · Pinterest：`Thu, 01 Oct 2026 14:47:33 +0000`
      · Clip Studio 缩略图 URL 上的秒级时间戳：`1674267854`
      · ISO：`2026-10-01T12:00:00Z` / `2026-10-01`
      · 秒/毫秒时间戳
    解析不了就回 None —— **不要拿"现在"冒充发布时间**，那是造假。
    """
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        ts = float(v)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except Exception:                                    # noqa: BLE001
            return None
    s = str(v).strip()
    if not s:
        return None
    if re.fullmatch(r"\d{10}", s):
        return parse_time(int(s))
    if re.fullmatch(r"\d{13}", s):
        return parse_time(int(s))
    try:
        dt = email.utils.parsedate_to_datetime(s)
        if dt is not None:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:                                        # noqa: BLE001
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:                                    # noqa: BLE001
            continue
    return None


def freshness_score(created_at, now=None):
    """0~1 的新鲜度。越新越高；未知 0.5；不因为"老"就归零。"""
    dt = parse_time(created_at)
    if dt is None:
        return 0.5
    now = now or datetime.now(timezone.utc)
    try:
        age = (now - dt).total_seconds() / 86400.0
    except Exception:                                        # noqa: BLE001
        return 0.5
    if age < 0:
        return 0.5                                           # 未来时间不可信
    if age <= FRESH_FULL_DAYS:
        return 1.0
    if age >= FRESH_ZERO_DAYS:
        return 0.35
    # 90 天 -> 3 年，线性从 1.0 降到 0.35
    return round(1.0 - 0.65 * (age - FRESH_FULL_DAYS) / (FRESH_ZERO_DAYS - FRESH_FULL_DAYS), 4)


def _engage_score(v):
    """商家侧热度归一到 0~1。没有信号时给 0.5，别让"没数据"变成惩罚。"""
    if v is None or v == "":
        return 0.5
    try:
        n = float(v)
    except Exception:                                        # noqa: BLE001
        return 0.5
    if n <= 0:
        return 0.0
    # 10 万销量/收藏 ≈ 1.0；对数压缩，避免大店一家独大
    return min(1.0, math.log1p(n) / math.log1p(100000.0))


def score_of(rec, order_of_provider, cfg=None):
    """算一条记录的分数（0~1，越大越靠前）。纯函数，方便测试。

    2026-10-19 改：从"平铺质量一家独大"改成
        来源/意图 0.30 + 新鲜 0.20 + 平铺 0.22 + 图形 0.08
        + 分辨率 0.08 + 检索顺序 0.06 + 热度 0.06
    这样专业来源（marketplace/portfolio/brand）和新图会排前面；画质只占 0.08。
    """
    cfg = cfg or {}
    t = rec.get("tile") or {}
    src = rec.get("source") or {}

    q = float(t.get("quality") or 0.0)                      # 平铺质量
    g = float(t.get("graphic") or 0.5)                      # 越像图形越好
    graphic = max(0.0, min(1.0, 1.0 - g))
    res = _res_score(rec.get("w"), rec.get("h"))
    provider = src.get("provider") or ""
    intent = cfg.get("intent") or ""
    # 来源先验：优先用 sources 的"类型+意图"；自定义来源回落到 _item 带的 quality_prior
    try:
        prior = _sources.effective_prior(provider, intent)
    except Exception:                                        # noqa: BLE001
        prior = float(src.get("quality_prior") or 0.5)
    fresh = freshness_score(src.get("created_at"), cfg.get("now"))
    engage = _engage_score(src.get("engagement"))
    pos = order_of_provider.get(provider, 0)
    prov_rank = 1.0 / (1.0 + 0.15 * pos)

    w = dict(WEIGHTS)
    w.update(cfg.get("weights") or {})
    s = (w["source"] * prior + w["fresh"] * fresh + w["tile"] * q
         + w["graphic"] * graphic + w["res"] * res
         + w["provider"] * prov_rank + w["engage"] * engage)
    return round(max(0.0, min(1.0, s)), 5)


def pick(records, *, want=100, near=HASH_NEAR, site_quota=SITE_QUOTA, cfg=None):
    """排序 + 感知去重 + 来源配额，挑出前 want 条。

    回 (挑选结果, 统计)。每条记录会补上 score / hash / near_of（和谁撞了）。
    """
    cfg = cfg or {}
    order_of_provider = {p: i for i, p in enumerate(cfg.get("provider_order") or [])}

    fresh_days = int(cfg.get("fresh_days") or 0)
    for r in records:
        r["score"] = score_of(r, order_of_provider, cfg)
        r["hash"] = ahash(r.get("path"))
        if fresh_days > 0:
            dt = parse_time((r.get("source") or {}).get("created_at"))
            if dt is not None:
                age = (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
                if age > fresh_days:
                    r["_stale"] = True

    ranked = sorted(records, key=lambda r: (-r["score"], r.get("path") or ""))

    picked, dropped_near, site_count = [], 0, {}
    prov_count, kind_count = {}, {}
    stale_dropped = 0
    cap = max(1, int(want * site_quota))
    cap_prov = max(1, int(want * float(cfg.get("provider_quota") or PROVIDER_QUOTA)))
    kind_q = dict(KIND_QUOTA)
    kind_q.update(cfg.get("kind_quota") or {})
    # 三轮：strict=站点+provider 双配额；soft=只守 provider 配额（站点可放宽）；
    # fill=不设配额补满。这样既不让四家交易平台霸榜，也不会因为配额凑不满。
    for mode in ("strict", "soft", "fill"):
        for r in ranked:
            if len(picked) >= want or r in picked:
                continue
            if r.get("_stale"):
                stale_dropped += 1
                continue
            src = r.get("source") or {}
            site = src.get("site") or "?"
            prov = src.get("provider") or "?"
            kind = _sources.kind(prov) or "?"
            if mode == "strict" and site_count.get(site, 0) >= cap:
                continue
            if mode in ("strict", "soft") and prov_count.get(prov, 0) >= cap_prov:
                continue
            if mode == "strict" and kind_q.get(kind) is not None:
                kcap = max(1, int(want * float(kind_q[kind])))
                if kind_count.get(kind, 0) >= kcap:
                    continue
            hit = None
            for p in picked:
                if _hamming(r.get("hash"), p.get("hash")) < near:
                    hit = p
                    break
            if hit is not None:
                dropped_near += 1
                continue
            r["near_of"] = None if hit is None else hit.get("id")
            picked.append(r)
            site_count[site] = site_count.get(site, 0) + 1
            prov_count[prov] = prov_count.get(prov, 0) + 1
            kind_count[kind] = kind_count.get(kind, 0) + 1
        if len(picked) >= want:
            break

    # **选完之后再按分数重排一次**。配额是"选谁"的规则，不该顺手把
    # "谁排前面"也改掉 —— 两轮选择会把第二轮补进来的塞到队尾，
    # 结果分数最高的那张可能排在中间。这个 bug 是测试逮到的。
    picked.sort(key=lambda r: -(r.get("score") or 0.0))

    stats = {"参评": len(records), "选中": len(picked), "近似重复丢掉": dropped_near,
             "过期丢掉": stale_dropped, "来源分布": {}, "来源类型分布": {}}
    for r in picked:
        site = ((r.get("source") or {}).get("site") or "?")
        stats["来源分布"][site] = stats["来源分布"].get(site, 0) + 1
        k = _sources.kind((r.get("source") or {}).get("provider") or "")
        stats["来源类型分布"][k or "?"] = stats["来源类型分布"].get(k or "?", 0) + 1
    return picked, stats


def clean_name(title, fallback="网络花型"):
    """把网页标题洗成一个人看得下去的花型名。

    搜索结果标题长这样："Blue Floral Seamless Pattern | Etsy | 20% off"
    —— 站名、价格、尺寸都是噪声。洗不干净就退回"网络花型 001"这种默认名，
    **不硬编**一个看似有意义的名字（那是编话，犯不着）。
    """
    t = (title or "").strip()
    if not t:
        return ""
    for sep in ("|", "｜", "-", "—", "–", "_", "·", "  "):
        if sep in t:
            parts = [p.strip() for p in t.split(sep) if p.strip()]
            if parts:
                # 取最长的一段：站名/价格通常最短
                t = max(parts, key=len)
            break
    t = t.replace("http://", "").replace("https://", "")
    for junk in ("High Quality", "Free", "Download", "免费", "下载", "高清",
                 "素材", "矢量图", "图片", "库存照片", "Stock Photo"):
        t = t.replace(junk, "")
    t = t.strip(" .·-—_|/\\")
    t = " ".join(t.split())
    if len(t) > 14:
        t = t[:14]
    # 只剩数字/符号、或者太短的，当没洗出来
    if len([c for c in t if c.isalnum() or "\u4e00" <= c <= "\u9fff"]) < 2:
        return ""
    return t
