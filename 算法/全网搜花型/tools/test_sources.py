#!/usr/bin/env python3
"""来源图 / 质量排序 / 溯源字段的离线测试（**不联网**）。

2026-10-19 加：用户要求"快速、精准、高质量，优先级=符合需求>新>多>画质"。
这批改动最容易坏的是三件事，测试就盯这三件：

  ① 来源先验：专业交易平台 > 通用搜索；意图不同权重不同。
  ② 溯源字段：_item / store 必须带 domain/author/created_at/has_source/license。
  ③ 新来源解析：Clip Studio JSON / Serper site: 的字段映射（桩掉网络）。
"""
from __future__ import annotations

import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image                                                      # noqa: E402
from wsearch import analyze, config, providers, rank, sources, store       # noqa: E402

ok_n = 0
bad_n = 0


def ok(cond, msg, extra=""):
    global ok_n, bad_n
    if cond:
        ok_n += 1
        print("  \u2713 %s" % msg)
    else:
        bad_n += 1
        print("  \u2717 %s   %s" % (msg, extra))


# ------------------------------------------------------------------ 来源图
print("\n[1] 来源图：质量先验 / 意图 / 精选站")
ok(len(sources.CORE) >= 20, "CORE 来源登记齐全（%d 家）" % len(sources.CORE))
ok(len(sources.SITE) >= 20, "SITE 精选站登记齐全（%d 家）" % len(sources.SITE))
ok(sources.prior("walan") > sources.prior("baidu"),
   "交易平台先验高于通用搜索（%.2f > %.2f）" % (sources.prior("walan"), sources.prior("baidu")))
ok(sources.prior("clip_studio") > sources.prior("pixabay"),
   "日本插画素材站高于图库")
ok(sources.effective_prior("site:etsy.com", "business") >
   sources.effective_prior("site:etsy.com", "inspiration"),
   "Etsy 在 business 意图下权重更高")
ok(sources.effective_prior("site:behance.net", "inspiration") >
   sources.effective_prior("site:behance.net", "business"),
   "Behance 在 inspiration 意图下权重更高")
ok(len(sources.site_domains("business", limit=4)) == 4, "按意图取精选站能取到 4 个")
ok("etsy.com" in sources.site_domains("business", limit=8), "business 意图会取 Etsy")
ok("behance.net" in sources.site_domains("inspiration", limit=8), "inspiration 意图会取 Behance")

# ------------------------------------------------------------------ _item
print("\n[2] 候选行：溯源字段")
it = providers._item(
    "https://cdn.example.com/a.jpg", "", 800, 1200, "Floral",
    "https://www.etsy.com/listing/1", "etsy", "floral", domain="etsy.com",
    author="A. Designer", created_at="2026-10-01", kind="marketplace",
    quality=0.93, license="purchase_required", engagement=120, method="feed")
ok(it["domain"] == "etsy.com" and it["author"] == "A. Designer",
   "domain/author 保留")
ok(it["has_source"] is True, "有外链 = has_source True")
ok(it["quality_prior"] == 0.93 and it["kind"] == "marketplace", "质量先验/类型保留")
ok(it["created_at"] == "2026-10-01" and it["method"] == "feed", "时间/渠道保留")
up = providers._item("https://x/y.jpg", "", 100, 100, "up", "", "manual")
ok(up["has_source"] is False, "无外链 = has_source False")

# ------------------------------------------------------------------ rank
print("\n[3] 排序：来源 / 新鲜 / 画质权重")
fresh = rank.freshness_score("2026-10-01")
old = rank.freshness_score("2020-01-01")
unknown = rank.freshness_score("")
ok(fresh > old and fresh > unknown, "新图新鲜度 > 老图 / 未知")
ok(0.0 < old < 1.0 and unknown == 0.5, "老图不归零、未知按 0.5")

pro = {"tile": {"quality": 0.8, "graphic": 0.2}, "w": 1200, "h": 1200,
       "source": {"provider": "site:etsy.com", "created_at": "2026-10-01",
                  "engagement": 1000}}
search = dict(pro, source={"provider": "baidu", "created_at": "2020-01-01"})
ok(rank.score_of(pro, {}) > rank.score_of(search, {}),
   "专业+新 > 通用+老")
ok(rank.freshness_score("2026-10-01") == 1.0, "90 天内满分")
ok(rank.WEIGHTS["tile"] == 0,
   "取消四方连续后：平铺质量不再参与排序（权重 0）")
ok(rank.WEIGHTS["res"] > rank.WEIGHTS["tile"],
   "分辨率仍有权重（画质排最后但保留）")

# ------------------------------------------------------------------ analyze intent
print("\n[4] 意图识别")
ok(analyze.detect_intent("国风 青花 花卉 平铺 无缝 面料印花") == "business",
   "有面料/平铺词 = business")
ok(analyze.detect_intent("找点好看的壁纸灵感") == "inspiration",
   "灵感/壁纸 = inspiration")
ok(analyze.detect_intent("牡丹 玫瑰 花名") == "specific",
   "具体花名 = specific")

# ------------------------------------------------------------------ Clip Studio
print("\n[5] Clip Studio JSON 解析（桩网络）")
_real_gj = providers.net.get_json


def _fake_gj(url, **kw):
    assert "assets.clip-studio.com" in url
    return {"response_data": {
        "found": 2, "cursor": "", "isPageReaching": True,
        "data": [
            {"material_id": "123", "name": "花柄セット",
             "thumbnail_url": "https://s3.example.com/thumb.jpg?1674267854",
             "purchase_count": "12345", "clip_user_handle": "artist"},
            {"material_id": "456", "name": "和柄",
             "thumbnail_url": "https://s3.example.com/thumb2.jpg?1700000000",
             "purchase_count": "7", "clip_user_handle": "artist2"},
        ]}}


providers.net.get_json = _fake_gj
try:
    rows = providers._clip_studio(["花柄"], 10, "", {"pages": 1})
finally:
    providers.net.get_json = _real_gj
ok(len(rows) == 2, "两条都解析出来")
ok(rows[0]["provider"] == "clip_studio" and rows[0]["kind"] == "illustration",
   "provider/kind 正确")
ok(rows[0]["page"].endswith("id=123"), "详情页链接正确")
ok(rows[0]["engagement"] == 12345 and rows[0]["created_at"] == "1674267854",
   "热度/时间戳保留")
ok(rows[0]["has_source"] is True, "Clip Studio 有出处")

# ------------------------------------------------------------------ site_search
print("\n[6] Serper site: 解析（桩网络）")
_real_pj = providers.net.post_json


def _fake_pj(url, payload, **kw):
    assert "google.serper.dev" in url
    assert payload["q"].startswith("site:etsy.com ")
    return {"images": [{"imageUrl": "https://i.etsystatic.com/x.jpg",
                        "thumbnailUrl": "https://i.etsystatic.com/t.jpg",
                        "imageWidth": 1000, "imageHeight": 1000,
                        "title": "Etsy floral", "link": "https://etsy.com/listing/1"}]}


providers.net.post_json = _fake_pj
try:
    rows = providers._site_search(["floral"], 10, "KEY",
                                  {"site_domains": ["etsy.com"]})
finally:
    providers.net.post_json = _real_pj
ok(len(rows) == 1 and rows[0]["provider"] == "site:etsy.com", "provider 带站名")
ok(rows[0]["domain"] == "etsy.com" and rows[0]["has_source"], "域名/出处正确")
ok(rows[0]["kind"] == "marketplace" and rows[0]["quality_prior"] >= 0.9,
   "精选站质量先验生效")

# ------------------------------------------------------------------ store
print("\n[7] 落库：溯源字段进索引")
tmp = tempfile.mkdtemp(prefix="wsearch_src_")
try:
    img = Image.new("RGB", (400, 400), (240, 235, 225))
    px = img.load()
    for i in range(400):
        px[i, i] = (40, 60, 120)
    p = os.path.join(tmp, "a.png")
    img.save(p)
    rec = {"path": p, "md5": "x", "ext": ".png", "w": 400, "h": 400,
           "tile": {"quality": 0.8, "graphic": 0.2, "seam_h": 1.0, "seam_v": 1.0},
           "source": it, "tile_gate": True}
    index, _paths = store.build([rec], tmp, meta={"need": "x"})
    item = index["items"][0]
    ok(item["has_source"] is True and item["license"] == "purchase_required",
       "has_source/license 进索引")
    ok(item["src_domain"] == "etsy.com" and item["src_author"] == "A. Designer",
       "src_domain/src_author 进索引")
    ok(item["src_created_at"] == "2026-10-01" and item["src_method"] == "feed",
       "src_created_at/src_method 进索引")
finally:
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

# ------------------------------------------------------------------ config
print("\n[8] 配置：质量权重")
q = config.quality()
ok(abs(sum(q[k] for k in ("source", "fresh", "tile", "graphic", "res",
                          "provider", "engage")) - 1.0) < 1e-6,
   "七个权重加起来 = 1")
ok(q["source"] > q["res"], "来源权重大于画质")
ok(q["fresh"] > q["res"], "新鲜度权重大于画质")
ok(q["fresh_days"] >= 0, "fresh_days 不崩")

print("\n结果：%d 通过 / %d 失败" % (ok_n, bad_n))
sys.exit(0 if bad_n == 0 else 1)
