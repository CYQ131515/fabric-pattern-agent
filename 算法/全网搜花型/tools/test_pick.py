#!/usr/bin/env python3
"""路线 2（pick）的端到端测试（**不联网**）。

测的是这条路线**和路线 1 到底差在哪**，不是把产线再测一遍（那是
test_pipeline.py 的事）。所以这里每一条断言都盯着"不凑数"这四个字：

    · 只搜一轮（page 永远是 1，不去翻第二页）
    · 过完两道闸门剩几张就交几张，**一句"还差 N 张"都不说**
    · --want 是上限不是任务
    · 一张都没剩下时要说清楚是被哪道闸门砍光的，别留个空目录让人猜

顺带钉住一条回归：路线 1（search）没有因为共用 `_land()` 被带偏 ——
它照样翻页、照样在不够时报差多少。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 测试绝不碰真网络，也绝不给后端已配的 DeepSeek 打请求（_llm_from_args 会读）
os.environ["WSEARCH_NO_HOST_CONFIG"] = "1"
# 测试绝不碰外部视觉 API（config.json 里配了 APIMart，也不能让单测联网）
os.environ.setdefault("WSEARCH_VLM_BASE", "http://127.0.0.1:9")
os.environ.setdefault("WSEARCH_VLM_KEY", "test")

from PIL import Image, ImageDraw                                          # noqa: E402
from wsearch import fetch, providers, service, store                       # noqa: E402

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


S = 320
INKS = [(46, 74, 140), (150, 60, 70), (60, 120, 90), (180, 130, 40),
        (80, 70, 150), (30, 90, 110), (200, 90, 120), (90, 90, 90)]


def _png(im):
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def make_tile(seed=0):
    """造一张**真**平铺图（花放在格心和四角，切开的两半能拼回去）。"""
    im = Image.new("RGB", (S, S), (242, 238, 228))
    d = ImageDraw.Draw(im)
    n = (2, 4, 5, 8)[seed % 4]
    c = S // n
    ink = INKS[seed % len(INKS)]
    r = c * (0.10 + 0.03 * (seed % 4))
    for i in range(n + 1):
        for j in range(n + 1):
            d.ellipse([i * c - r, j * c - r, i * c + r, j * c + r], fill=ink)
    for i in range(n):
        for j in range(n):
            cx, cy = i * c + c / 2, j * c + c / 2
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=ink)
    return im


def make_photo(seed=0):
    """造一张不是平铺图的图。"""
    im = Image.new("RGB", (S, S))
    px = im.load()
    for y in range(S):
        for x in range(S):
            v = (x * 7 + y * 13 + seed * 31) % 256
            px[x, y] = (v, (v * 3) % 256, (x * 5 + seed) % 256)
    return im


# ============================================================ 桩：搜索源
print("[1] 装桩搜索源 / 桩网络 / 桩视觉模型")
PAGES = []            # 记录每次检索要的是第几页 —— 路线 2 必须永远是 [1]


def fake_provider_a(queries, per_query, key, opts):
    pg = int((opts or {}).get("page") or 1)
    PAGES.append(pg)
    rows = []
    for qi, q in enumerate(queries):
        for i in range(4):
            tag = i if pg <= 1 else 10 + i
            rows.append({"url": "https://a.example/%d-%d.png" % (qi, tag),
                         "thumb": "", "w": S, "h": S, "title": "t%d" % tag,
                         "page": "https://a.example/p/%d" % tag,
                         "site": "a.example", "provider": "fakea", "query": q})
    return rows


providers.REGISTRY["fakea"] = {"label": "假站A", "kind": "search",
                               "fn": fake_provider_a, "home": "http://a.example",
                               "env": "WSEARCH_FAKEA", "note": "测试用"}
os.environ["WSEARCH_FAKEA"] = "k1"
providers.DEFAULT_ORDER = ("fakea",)


def _blob_for(url):
    if "photo" in url:
        return _png(make_photo(1))
    if url.startswith("https://a.example/"):
        idx = int(url.rsplit("/", 1)[-1].split("-")[1].split(".")[0])
        return _png(make_tile(idx))
    return b"<html>not an image</html>"


def fake_fetch(url, *, timeout=25, max_bytes=None, referer=""):
    blob = _blob_for(url)
    if not blob.startswith(b"\x89PNG"):
        raise fetch.net.NetError("拿到的不是图片")
    return blob, "image/png", url


real_fetch = fetch.net.fetch_bytes
tmp = tempfile.mkdtemp(prefix="wsearch_pick_")


def ns(**kw):
    base = {"json_out": "", "outdir": "", "need": "", "brief": "", "want": 0,
            "candidates": 0, "provider": "", "per_query": 100, "queries": 1,
            "page": 1, "workers": 4, "per_host": 2, "timeout": 5, "vocab": "",
            "progress_out": "", "llm_base": "", "llm_key": "", "llm_model": "",
            # 默认关甄别：只在想测"删"的那几条里打开，其余用例保持老行为
            "no_screen": True, "no_rescreen": False, "rounds": 0,
            "vlm_base": "", "vlm_model": "", "vlm_key": "", "vlm_timeout": 0,
            "vlm_max_side": 0, "vlm_batch": 1, "vlm_workers": 1,
            "craft": "", "style_pos": "", "style_neg": ""}
    base.update(kw)
    return argparse.Namespace(**base)


def run_svc(fn):
    """跑一个 service 命令，把 stdout 抓成 JSON（_emit 写的是多行 JSON）。"""
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        rc = fn()
    finally:
        sys.stdout = old
    return rc, json.loads(buf.getvalue().strip())


# ============================================================ [2] 只搜一轮
print("\n[2] 路线 2：只搜一轮、不翻页")
PAGES.clear()


def _pick_plain():
    fetch.net.fetch_bytes = fake_fetch
    try:
        return service.cmd_pick(ns(
            need="国风 花卉 水彩", outdir=os.path.join(tmp, "p1"),
            want=0, provider="fakea", queries=1))
    finally:
        fetch.net.fetch_bytes = real_fetch


rc, res = run_svc(_pick_plain)
ok(rc == 0 and res["ok"], "pick 跑通了（退出码 %s）" % rc)
ok(PAGES == [1], "**一次都没翻页**（实际要过的页：%s）" % PAGES)
ok(res["route"] == "pick", "结果里标了这是路线 2（route=%s）" % res["route"])
ok(res["count"] >= 1, "落库 %d 张" % res["count"])
out1 = os.path.join(tmp, "p1")
ok(sorted(os.listdir(out1)) == ["index.json", "manifest.json", "orig", "thumb"],
   "产物目录干净、没留下载碎片（%s）" % sorted(os.listdir(out1)))
mani = json.load(open(os.path.join(out1, "manifest.json"), encoding="utf-8"))
idx1 = json.load(open(os.path.join(out1, "index.json"), encoding="utf-8"))
ok(mani.get("route") == "pick" and (idx1.get("need") or {}).get("route") == "pick",
   "manifest 和 index 里都记了 route=pick（事后能分清是哪条路线落的）")
ok("路线 2" in res["note"] and "还差" not in res["note"],
   "note 说明了这是不凑数的路线（%s）" % res["note"][:40])

# ============================================================ [3] 剩多少交多少
print("\n[3] 路线 2：删掉不合格的，剩几张交几张")
_real_vlm = service._vlm


class _FakeVlm:
    """桩视觉端点：按"整批里的第几张"决定判好判坏，顺序可预测。"""

    def __init__(self, bad_seq=()):
        self.bad = set(bad_seq)
        self.n_calls = 0
        self.model = "fake-vlm"
        self.prompts = []

    def available(self):
        return True

    def see(self, prompt, paths, max_tokens=None):
        self.n_calls += 1
        self.prompts.append(prompt)
        out = []
        for i, _p in enumerate(paths, 1):
            seq = (self.n_calls - 1) * len(paths) + i
            good = seq not in self.bad
            out.append('{"i":%d,"pattern":%s,"match":true,"why":"桩第%d张"}'
                       % (i, "true" if good else "false", seq))
        return "[" + ",".join(out) + "]"


def _pick_screened(bad_seq, want=0, outdir="p2"):
    fetch.net.fetch_bytes = fake_fetch
    service._vlm = lambda args: _FakeVlm(bad_seq=bad_seq)
    try:
        return service.cmd_pick(ns(
            need="国风 花卉 水彩", outdir=os.path.join(tmp, outdir),
            want=want, provider="fakea", queries=1, no_screen=False,
            vlm_batch=1))
    finally:
        fetch.net.fetch_bytes = real_fetch
        service._vlm = _real_vlm


rc2, res2 = run_svc(lambda: _pick_screened(bad_seq=(1, 3)))
ok(rc2 == 0 and res2["ok"], "带甄别的 pick 跑得通")
ok(res2["stats"]["甄别剔除"] == 2, "判坏的 2 张被剔掉了（%s）"
   % res2["stats"]["甄别剔除"])
ok(res2["count"] == res2["stats"]["留下平铺图"] - 2,
   "**剩多少交多少**：%d 张过平铺闸门、剔掉 2 张、交 %d 张"
   % (res2["stats"]["留下平铺图"], res2["count"]))
ok("还差" not in res2["note"] and "按你要的" not in res2["note"],
   "一句「还差 N 张 / 按你要的」都没有（%s）" % res2["note"][:40])
ok(os.path.exists(os.path.join(tmp, "p2", "rejected.json")),
   "被剔的留了档（rejected.json）")
rej = json.load(open(os.path.join(tmp, "p2", "rejected.json"), encoding="utf-8"))
ok(rej["count"] == 2 and all(x.get("thumb") for x in rej["items"]),
   "留档条数和缩略图都对得上（先删再留档就只剩空壳）")

# ============================================================ [4] want 是上限
print("\n[4] 路线 2：--want 是上限，不是任务")
rc3, res3 = run_svc(lambda: _pick_screened(bad_seq=(), want=2, outdir="p3"))
ok(rc3 == 0 and res3["count"] <= 2, "给了上限 2 就最多交 2 张（实际 %d）"
   % res3["count"])
ok(res3["count"] == 2, "合格的多于上限时按上限交（%d 张）" % res3["count"])

# ============================================================ [5] 一张都没剩
print("\n[5] 路线 2：一张都没剩下时要说清楚是被哪道闸门砍的")
rc4, res4 = run_svc(lambda: _pick_screened(
    bad_seq=(1, 2, 3, 4, 5, 6, 7, 8), outdir="p4"))
# 注意：**失败也是退出码 0**（_fail 走 _emit），只有 ok=false 才是信号。
ok(res4["ok"] is False, "全被判不合格时报 ok=false，而不是交个空目录")
ok(res4.get("stage") == "screen" and "rejected.json" in res4.get("error", ""),
   "说清了是**甄别**这道闸门砍的、理由在哪看（%s）" % (res4.get("error") or "")[:40])

# ============================================================ [6] 回归路线 1
print("\n[6] 回归：路线 1（search）没被共用的那段代码带偏")
PAGES.clear()


def _search_more_than_one_page():
    fetch.net.fetch_bytes = fake_fetch
    try:
        return service.cmd_search(ns(
            need="国风 花卉", outdir=os.path.join(tmp, "s1"),
            want=10, provider="fakea", queries=1, rounds=3))
    finally:
        fetch.net.fetch_bytes = real_fetch


rc5, res5 = run_svc(_search_more_than_one_page)
ok(rc5 == 0 and res5["ok"], "search 照样跑得通")
ok(len(PAGES) > 1, "路线 1 **会翻页**去凑数（要过的页：%s）" % PAGES)
ok(res5.get("route") in (None, "full"), "路线 1 的结果不带 pick 标记")
mani5 = json.load(open(os.path.join(tmp, "s1", "manifest.json"), encoding="utf-8"))
ok(mani5.get("route") == "full", "manifest 里记的是 route=full")
ok("按你要的" in res5["note"] or res5["count"] >= 10,
   "路线 1 仍然按交差数字报账（%s）" % res5["note"][:40])

# 两条路线的入口都得在命令行里能找到
p = service.build_parser()
ok("pick" in str(p.format_usage()) + str([a for a in p._subparsers._group_actions]),
   "service 的命令行里注册了 pick 子命令")
_cli_out = os.path.join(tmp, "cli")
fetch.net.fetch_bytes = fake_fetch
try:
    code = service.main(["pick", "--need", "国风 花卉", "--outdir", _cli_out,
                         "--want", "1", "--provider", "fakea", "--queries", "1"])
finally:
    fetch.net.fetch_bytes = real_fetch
_cli_idx = os.path.join(_cli_out, "index.json")
ok(code == 0 and os.path.exists(_cli_idx),
   "service.main(['pick', ...]) 能直接调（退出码 %s）" % code)
_cli_d = json.load(open(_cli_idx, encoding="utf-8")) if os.path.exists(_cli_idx) else {}
ok(_cli_d.get("count") == 1 and (_cli_d.get("need") or {}).get("route") == "pick",
   "命令行走通了整条路线 2（落 %s 张）" % _cli_d.get("count"))

# 这行的格式是契约：后端/测试/test_wsearch.py 按 "结果：N 通过 / M 失败"
# 把分数并进全量回归，写成别的样子就并进去了。
print("\n结果：%d 通过 / %d 失败" % (ok_n, bad_n))
sys.exit(1 if bad_n else 0)
