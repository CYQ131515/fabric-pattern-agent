#!/usr/bin/env python3
"""整条产线的端到端测试（**不联网**）。

为什么要桩掉网络
    · 搜索要 key，测试里不能依赖用户掏钱买的东西；
    · 图床随时会变，真下载会让测试随机失败 —— 那是最糟的一种测试；
    所以这里自己造一个"假搜索源"，把合成图按 URL 喂给下载层。
    这样测的还是**真实代码路径**（同样的 fetch/rank/store/service），
    只是把"外面那个世界"换成了确定的。
"""
from __future__ import annotations

import io
import json
import math
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# **测试绝不碰真网络**：service 会去读本机后端已配的对话模型（`后端/config`），
# 那是便利功能，但在测试里会变成"跑一遍测试就给 DeepSeek 打一次请求"。
# 这一行必须在 import service 之前设好（_llm_from_args 是调用时读，稳妥起见提前）。
os.environ["WSEARCH_NO_HOST_CONFIG"] = "1"
# 测试绝不碰外部视觉 API（config.json 里配了 APIMart，也不能让单测联网）
os.environ.setdefault("WSEARCH_VLM_BASE", "http://127.0.0.1:9")
os.environ.setdefault("WSEARCH_VLM_KEY", "test")
# 同理：**测试不看 config.json**。否则用户把 filters.screen 关掉、或把自己的 key
# 写进配置文件，跑一遍测试就全变了（甚至真去打一次云端）。配置本身单独测。
os.environ["WSEARCH_NO_CONFIG"] = "1"

from PIL import Image, ImageDraw                                          # noqa: E402
from wsearch import analyze, config, fetch, providers, rank, screen, store, tile  # noqa: E402
from wsearch import service                                               # noqa: E402

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


def _png(im):
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


# 每个种子一种色 —— 保证 10 个种子出来 10 张**看着不一样**的图
INKS = [(46, 74, 140), (150, 60, 70), (60, 120, 90), (180, 130, 40),
        (80, 70, 150), (30, 90, 110), (200, 90, 120), (90, 90, 90),
        (120, 60, 160), (60, 140, 140)]


def make_tile(seed=0):
    """造一张**真**平铺图。

    花体必须放在格子的中心和四个角上 —— 角上的花被边界切开，切开的两半
    在铺开之后正好拼回一朵；中心的花整朵都在格子内部。这两种位置都是
    天然的平铺单元。（第一版把花随便丢在格子里的偏移位置，结果被自己
    的判定器全部拒绝了 —— 因为它确实不是平铺图。判定器是对的。）
    """
    im = Image.new("RGB", (S, S), (242, 238, 228))
    d = ImageDraw.Draw(im)
    n = (2, 4, 5, 8)[seed % 4]               # 都能整除 320，周期才严丝合缝
    c = S // n
    ink = INKS[seed % len(INKS)]
    r = c * (0.10 + 0.03 * (seed % 4))
    for i in range(n + 1):                   # 到 n 为止：右/下边界也要有半朵
        for j in range(n + 1):
            d.ellipse([i * c - r, j * c - r, i * c + r, j * c + r], fill=ink)
    for i in range(n):
        for j in range(n):
            cx, cy = i * c + c / 2, j * c + c / 2
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=ink)
    return im


def make_photo(seed=0):
    """造一张"不是平铺图"的图（左右两半毫不相干）"""
    im = Image.new("RGB", (S, S))
    px = im.load()
    for y in range(S):
        for x in range(S):
            v = (x * 7 + y * 13 + seed * 31) % 256
            px[x, y] = (v, (v * 3) % 256, (x * 5 + seed) % 256)
    return im


# ============================================================ [1] 需求分析兜底
print("[1] 需求分析：没有大模型时的兜底")
plan, note = analyze.plan("国风 花卉 蓝色 连衣裙", llm=None, n_queries=6)
ok(plan["engine"] == "template", "没配 key 时退化成模板（engine=%s）" % plan["engine"])
ok(len(plan["queries"]) >= 2, "至少给出 2 条检索词（%d 条）" % len(plan["queries"]))
ok(any("无缝" in q or "平铺" in q for q in plan["queries"]), "检索词里带了行业词（平铺/无缝）")
ok(any(q.strip().endswith("print") or "pattern" in q for q in plan["queries"]),
   "有英文检索词（捞 Pinterest/素材站）")
ok(any("连衣裙" in n for n in plan["negative"]), "会招来成品照的词进了 negative")
ok(analyze.plan("", llm=None)[0]["queries"], "空需求也不崩，回通用检索词")

bad_plan, _ = analyze.plan("x", llm={"base": "http://127.0.0.1:9", "key": "k",
                                     "model": "m"}, n_queries=4, timeout=2)
ok(bad_plan["engine"] == "template", "对话模型连不上时自动退回模板，不抛异常")
ok(len(analyze.to_keywords("蓝白青花，花卉 连衣裙")) >= 2, "关键词粗切能用")

# ===================================== [1b] 免 key 的国内引擎（百度 / 360）
print("\n[1b] 免 key 来源：百度图片 / 360 图片")
ok(providers.key_of("baidu") == "" and providers.enabled("baidu"),
   "百度图片不需要 key，而且一直是可用的")
ok(providers.key_of("so360") == "" and providers.enabled("so360"),
   "360 图片不需要 key，而且一直是可用的")
ok("serper" not in providers.available({}), "要 key 的来源没配就还是不算可用")
ok({"baidu", "so360"} <= set(providers.available({})),
   "一个 key 都不配时，默认能用的就是这两家（这才叫开箱即用）")

# 百度：原图必须取 replaceUrl[0].ObjURL —— 顶层那个 objURL 是加密串
# （ippr_z2C$qAzdH3F...），直接拿来用会下到一堆废地址。
BAIDU_PAYLOAD = {"data": [
    {"thumbURL": "https://img0.baidu.com/it/u=1&f=JPEG?w=500",
     "middleURL": "https://img0.baidu.com/it/u=1&f=JPEG?w=500",
     "objURL": "ippr_z2C$qAzdH3FThisIsTheEncryptedOne",
     "width": "690", "height": "1496",
     "fromPageTitleEnc": "温柔<em>小碎花</em>平铺壁纸",
     "fromURLHost": "weibo.com",
     "replaceUrl": [{"ObjURL": "https://ww1.sinaimg.cn/mw690/a.jpg",
                     "FromURL": "http://weibo.com/1647496920/Q8"}]},
    {"thumbURL": "https://img1.baidu.com/it/u=2&f=JPEG?w=500",
     "width": "800", "height": "800",
     "fromPageTitleEnc": "绣球花无缝图案", "fromURLHost": "www.laixuexi.cc",
     "replaceUrl": []},
    {"adType": "0"},                       # 广告空条目：必须被跳过，不能崩
]}
SO360_PAYLOAD = {"list": [
    {"img": "https://c-ssl.dtstatic.com/uploads/a.thumb.1000_0.jpeg",
     "thumb": "https://p2.ssl.qhimgs1.com/t03.jpg",
     "thumbWidth": "148", "thumbHeight": "240",
     "width": "1000", "height": "1620", "title": "日系小清新花朵平铺",
     "link": "http://www.duitang.com/blog/?id=1", "site": "www.duitang.com"},
    {"thumb": "https://p2.ssl.qhimgs1.com/t04.jpg", "title": "只有缩略图的条目",
     "link": "http://x.example/1", "site": "x.example"},
]}


class _FakeSess:
    """假的百度会话。

    注意：真实的 net.Session.get_json 内部会先 self.warm()（不 warm 就没有
    BAIDUID，百度会直接回 Forbid spider access）。这里**不**去复刻那个行为 ——
    桩复刻了就等于在测桩自己。真正该钉的是"百度这条路径必须走带 cookie 的
    会话"，所以下面断言的是 net.session() 被调用过、而且用的是百度的站点。
    """

    def __init__(self, payload):
        self.payload = payload

    def get_json(self, url, *, params=None, headers=None, timeout=25):
        self.params = params
        return self.payload


_sess_bd = _FakeSess(BAIDU_PAYLOAD)
_sess_homes = []
_real_sess, _real_gj = providers.net.session, providers.net.get_json
providers.net.session = lambda home: (_sess_homes.append(home), _sess_bd)[1]
providers.net.get_json = lambda url, **kw: SO360_PAYLOAD
try:
    b = providers._baidu(["青花 平铺"], 60, "", {})
    ok(len(b) == 2, "百度：没图的空条目被跳过（拿到 %d 条）" % len(b))
    ok(b[0]["url"] == "https://ww1.sinaimg.cn/mw690/a.jpg",
       "百度：原图取 replaceUrl[0].ObjURL，不是那个加密的 objURL")
    ok(b[0]["title"] == "温柔小碎花平铺壁纸",
       "百度：标题里的 <em> 被剥掉了（%r）" % b[0]["title"])
    ok(b[0]["site"] == "weibo.com", "百度：来源站取 fromURLHost")
    ok(b[0]["page"].startswith("http://weibo.com/"), "百度：来源页取 FromURL")
    ok(b[1]["url"] == "https://img1.baidu.com/it/u=2&f=JPEG?w=500",
       "百度：没有 replaceUrl 时退回 middleURL/thumbURL")
    ok(b[0]["w"] == 690 and b[0]["h"] == 1496, "百度：尺寸是字符串也能转成数")

    s = providers._so360(["青花 平铺"], 30, "", {})
    ok(len(s) == 2, "360：两条都拿到了（%d）" % len(s))
    ok(s[0]["url"].startswith("https://c-ssl.dtstatic.com/"), "360：原图取 img")
    ok(s[0]["site"] == "www.duitang.com",
       "360：来源站取 site，不是图床域名（否则来源配额会误伤整批）")
    ok(s[0]["page"] == "http://www.duitang.com/blog/?id=1", "360：来源页取 link")
    ok(s[1]["url"] == "https://p2.ssl.qhimgs1.com/t04.jpg", "360：没 img 就退回 thumb")
    ok(_sess_homes == ["https://image.baidu.com/"],
       "百度：走的是带 cookie 的会话（net.session）—— 不先拿 BAIDUID 就会被挡")
finally:
    providers.net.session, providers.net.get_json = _real_sess, _real_gj

# ============================== [1c] 后加的三家免 key 源（必应 / 花瓣 / 堆糖）
print("\n[1c] 免 key 来源：必应图片 / 花瓣 / 堆糖")
ok(providers.key_of("bing") == "" and providers.enabled("bing"),
   "必应图片不需要 key")
ok(providers.key_of("huaban") == "" and providers.key_of("duitang") == "",
   "花瓣 / 堆糖不需要 key")
ok({"baidu", "so360", "huaban", "duitang"} <= set(providers.available({})),
   "一个 key 都不配时，默认能用四家（真的开箱即用）")
ok(providers._cjk("青花 平铺") and not providers._cjk("floral pattern"),
   "_cjk 认得出中英文检索词")
# 必应：适配器留着，但**默认不跑** —— 它限流时不是报错，而是安安静静回一整套
# 不相关的图（真图、能下），照样占掉 800 个候选名额，等于挤掉好来源的名额。
ok(providers.enabled("bing") and "bing" not in providers.available({}),
   "必应能用，但默认不跑（限流时会回不相关的图，占掉候选名额）")
ok("bing" in providers.on_demand(), "必应被列进「要手动开」的那几家")
ok(providers.REGISTRY["bing"].get("default") is False,
   "必应在注册表里标了 default=False（不是靠忘记加进 DEFAULT_ORDER）")


class _FakeBing:
    """假的必应会话。要点是**返回 HTML**（必应没有 JSON 接口）。"""

    def __init__(self, text):
        self.text = text
        self.urls = []

    def get_text(self, url, *, headers=None, timeout=25, max_bytes=0):
        self.urls.append(url)
        return self.text


# 注意第二条里嵌了一层 {}（"desc":{"a":1}）—— 用 \\{.*?\\} 那种正则会在
# 内层的 } 处截断，一条都解不出来。这条就是钉那个 bug 的。
BING_HTML = '''<div class="imgpt"><a m="{&quot;cid&quot;:&quot;1&quot;,&quot;murl&quot;:&quot;https://a.example/p1.jpg&quot;,&quot;turl&quot;:&quot;https://ts1.mm.bing.net/th?id=1&quot;,&quot;purl&quot;:&quot;https://huaban.com/pins/1&quot;,&quot;t&quot;:&quot;青花平铺素材&quot;}"></a></div>
<div class="imgpt"><a m="{&quot;cid&quot;:&quot;2&quot;,&quot;murl&quot;:&quot;https://a.example/p2.png&quot;,&quot;desc&quot;:{&quot;a&quot;:1},&quot;t&quot;:&quot;碎花 四方连续&quot;,&quot;purl&quot;:&quot;https://www.etsy.com/listing/2&quot;}"></a></div>
<div class="imgpt"><a m="{&quot;cid&quot;:&quot;3&quot;}"></a></div>
<div class="imgpt"><a m="not-json-at-all"></a></div>'''
_bing = _FakeBing(BING_HTML)
_real_sess2 = providers.net.session
providers.net.session = lambda home: _bing
try:
    rows = providers._bing(["青花 平铺"], 35, "", {"page": 1})
    ok(len(rows) == 2, "必应：没 murl 的、不是 JSON 的条目都被跳过（拿到 %d 条）" % len(rows))
    ok(rows[0]["url"] == "https://a.example/p1.jpg", "必应：原图取 murl")
    ok(rows[0]["thumb"].startswith("https://ts1.mm.bing.net/"), "必应：缩略图取 turl")
    ok(rows[0]["title"] == "青花平铺素材" and rows[0]["page"].endswith("/pins/1"),
       "必应：标题取 t、来源页取 purl")
    ok(rows[1]["url"].endswith(".png"),
       "必应：**嵌了内层 {} 的那条也要解出来**（正则不能停在第一个 }）")
    ok("mkt=zh-CN" in _bing.urls[0], "必应：中文检索词用 mkt=zh-CN（用 en-US 会只回 1 条）")
    _bing.urls.clear()
    providers._bing(["floral pattern"], 35, "", {"page": 3})
    ok("mkt=en-US" in _bing.urls[0], "必应：英文检索词换成 mkt=en-US")
    ok("first=71" in _bing.urls[0], "必应：翻页用 first（步长 35，第 3 页=71）")
finally:
    providers.net.session = _real_sess2

# 花瓣：网页版是 JS 渲染的（0 个 pin），只能走 api.huaban.com
HUABAN_PAYLOAD = {"pins": [
    {"pin_id": 11, "raw_text": "青花平铺素材",
     "file": {"key": "abc111", "url": "https://gd-hbimg.huaban.com/abc111?auth_key=x",
              "width": 1200, "height": 900},
     "board": {"title": "蓝染"}, "link": ""},
    # raw_text 就是那个 key —— 等于没标题，必须退回画板名
    {"pin_id": 12, "raw_text": "def222",
     "file": {"key": "def222", "url": "https://gd-hbimg.huaban.com/def222?auth_key=x"},
     "board": {"title": "碎花"}},
    # 连画板名都没有 → 用标签拼
    {"pin_id": 13, "raw_text": "ghi333",
     "file": {"key": "ghi333", "url": "https://gd-hbimg.huaban.com/ghi333?auth_key=x"},
     "tags": ["花卉", "平铺"]},
    # 没有 file.url 的坏条目：跳过，不能崩
    {"pin_id": 14, "raw_text": "bad", "file": {"key": "z"}},
]}
DUITANG_PAYLOAD = {"data": {"total": 720, "next_start": 24, "object_list": [
    {"id": 1, "msg": "碎花壁纸/碎花平铺", "tags": ["花卉"],
     "photo": {"path": "https://a-ho-img.dtstatic.com/uploads/a.jpeg",
               "width": 1170, "height": 2532}},
    {"id": 2, "msg": "", "tags": ["青花", "平铺"],
     "photo": {"path": "https://a-ho-img.dtstatic.com/uploads/b.jpeg"}},
]}}

_p, _a = providers._hb_urls("https://gd-hbimg-edge.huaban.com/KEY123?auth_key=x")
ok(_p == "https://hbimg.huaban.com/KEY123",
   "花瓣：主链换到 hbimg **且去掉 auth_key**（它不校验，少个过期变量）—— %s" % _p)
ok(_a.startswith("https://gd-hbimg-edge.huaban.com/"),
   "花瓣：备用留 API 原样那条 edge 直链（hbimg 万一挂了它是唯一退路）—— %s" % _a)
ok(providers._hb_urls("https://x.example/a.jpg") == ("https://x.example/a.jpg", ""),
   "花瓣：不是花瓣的域名别乱动")

# 这两家是**按空格做"与"匹配**：analyze 生成的七八个词的组合词喂进去一律
# 0 条（实测"法式小碎花 平铺 无缝图案 水彩手绘 粉彩花卉 四方连续 印花素材"
# → 0；截到 2 个词才有）。所以喂给它们的必须是截短后的词。
_trim = providers._trim_for_zh(
    "法式小碎花 平铺 无缝图案 水彩手绘 粉彩花卉 四方连续 印花素材")
ok(_trim[0] == "法式小碎花 平铺",
   "长词截成「题材 + 行业词」（%s）—— 不截的话这两家一条都不给" % _trim[0])
ok(len(_trim) >= 2 and _trim[1] == "法式小碎花",
   "还搜不到就退到最宽的一档（只留题材词）：%s" % (_trim[1:] or "无"))
ok(providers._trim_for_zh("田园碎花 矢量线描 平铺印花 小花卉")[0] == "田园碎花 平铺印花",
   "行业词不在第二个时也要挑出来（不然回来的是插画不是素材）")
ok(providers._trim_for_zh("碎花 平铺") == ["碎花 平铺"],
   "本来就只有两个词就别动它")

_calls = []
_real_gj2 = providers.net.get_json


def _fake_gj(url, **kw):
    _calls.append((url, dict(kw)))
    return HUABAN_PAYLOAD if "huaban" in url else DUITANG_PAYLOAD


providers.net.get_json = _fake_gj
try:
    h = providers._huaban(["青花 平铺"], 20, "", {"page": 1})
    ok(len(h) == 3, "花瓣：没有 file.url 的坏条目被跳过（拿到 %d 条）" % len(h))
    ok(h[0]["url"].startswith("https://hbimg.huaban.com/"),
       "花瓣：直链换到能下的域名（gd-hbimg-edge / gd-hbimg 现在都 567）")
    ok(h[0].get("ref") == "https://huaban.com/",
       "花瓣：下载 Referer 钉成站点根（带具体页 567，不带也 567）")
    ok("auth_key=" not in h[0]["url"],
       "花瓣：主链**不带** auth_key（hbimg 不校验；带了反而多一个会过期的变量）")
    ok(h[0]["thumb"] == "https://gd-hbimg.huaban.com/abc111?auth_key=x",
       "花瓣：API 原样那条直链塞进 thumb，当第二域名退路")
    ok(h[0]["title"] == "青花平铺素材", "花瓣：标题取 raw_text")
    ok(h[1]["title"] == "碎花", "花瓣：raw_text 就是 key 时退回画板名（不留哈希）")
    ok(h[2]["title"] == "花卉 平铺", "花瓣：连画板名都没有时用标签拼")
    ok(h[0]["page"] == "https://huaban.com/pins/11", "花瓣：来源页按 pin_id 拼")
    ok(h[0]["w"] == 1200, "花瓣：尺寸跟着 file 走")
    ok(providers._huaban(["floral pattern"], 20, "", {}) == [],
       "花瓣：英文检索词直接跳过（省一次请求，不然基本是空的）")

    d = providers._duitang(["青花 平铺"], 24, "", {"page": 1})
    ok(len(d) == 2, "堆糖：两条都拿到了（%d）" % len(d))
    ok(d[0]["url"] == "https://a-ho-img.dtstatic.com/uploads/a.jpeg", "堆糖：直链取 photo.path")
    ok(d[0]["title"] == "碎花壁纸/碎花平铺", "堆糖：标题取 msg")
    ok(d[1]["title"] == "青花 平铺", "堆糖：msg 为空时用标签拼")
    ok(d[0]["page"] == "https://www.duitang.com/blog/?id=1", "堆糖：来源页按 id 拼")
    ok(providers._duitang(["floral pattern"], 24, "", {}) == [],
       "堆糖：英文检索词同样跳过")

    # 长检索词必须**截短了再发**（不然这两家一条都不给）
    _calls.clear()
    providers._huaban(["法式小碎花 平铺 无缝图案 水彩手绘 粉彩花卉 四方连续"], 20, "", {})
    ok(_calls and _calls[0][1].get("params", {}).get("q") == "法式小碎花 平铺",
       "花瓣：发出去的是截短后的词（%s）"
       % (_calls[0][1].get("params", {}).get("q") if _calls else "没发"))
    _calls.clear()
    providers._duitang(["法式小碎花 平铺 无缝图案 水彩手绘"], 24, "", {})
    ok(_calls and _calls[0][1].get("params", {}).get("kw") == "法式小碎花 平铺",
       "堆糖：同样发截短后的词（%s）"
       % (_calls[0][1].get("params", {}).get("kw") if _calls else "没发"))
finally:
    providers.net.get_json = _real_gj2

# 结果必须**按来源轮流取**拼起来：下游会把候选截到 800 条再下载，
# 按来源顺序拼的话，排在后面的家一条都进不了下载队列（加了等于白加）。
_a = [{"provider": "a", "i": i} for i in range(4)]
_b = [{"provider": "b", "i": i} for i in range(2)]
mix = providers._interleave([_a, _b])
ok(len(mix) == 6, "轮流取不改变总条数（%d）" % len(mix))
ok([r["provider"] for r in mix] == ["a", "b", "a", "b", "a", "a"],
   "轮流取：两家的结果交替出现（%s）" % [r["provider"] for r in mix])
ok([r["provider"] for r in mix[:4]].count("a") == 2,
   "截断到前 4 条时，两家各占一半 —— 新来源不会被前面的家挤掉")

# 偶发被挡时的重试（百度和 360 都会在连打几次后临时挡人）
_tries = [0]


def _flaky():
    _tries[0] += 1
    if _tries[0] < 2:
        raise RuntimeError("第一次被挡")
    return "ok"


ok(providers._with_retry(_flaky, tries=2, delay=0) == "ok" and _tries[0] == 2,
   "接口偶发被挡时重试一次能救回来")


def _always_fail():
    raise RuntimeError("一直挡")


try:
    providers._with_retry(_always_fail, tries=2, delay=0)
    ok(False, "两次都挂时应该把错误抛出去")
except RuntimeError:
    ok(True, "两次都挂就把错误抛出去（让 search 记成一条来源错误，不吞掉）")

# ========================= [1d] 专业花型站（Scrapling 爬取层，可选依赖）
print("\n[1d] 专业花型站：Patternbank（走 Scrapling，可选依赖）")
ok(providers.REGISTRY["patternbank"].get("needs") == "scrapling",
   "Patternbank 在注册表里标了 needs=scrapling（不是靠注释约定）")
ok(providers.key_of("patternbank") == "",
   "Patternbank 不需要 key（它要走的是浏览器指纹，不是 key）")
# 装没装 Scrapling 决定它跑不跑：没装就**根本不该出现在可用列表里**，
# 否则跑起来才一个个 403，用户分不清是缺依赖还是站挂了。
_real_have = providers.scrape.have
_real_avail = providers.REGISTRY["patternbank"]
providers.scrape.have = lambda: False
ok(not providers.enabled("patternbank"),
   "没装 Scrapling 时 Patternbank 不算可用")
ok("patternbank" not in providers.available({}),
   "没装 Scrapling 时它不在默认跑的几家里")
ok(providers._patternbank(["floral"], 10, "", {}) == [],
   "没装依赖时整家跳过、返回空（不抛、不把别家拖下水）")
providers.scrape.have = _real_have

# 检索词收拾：这条决定了它能不能搜到东西
ok(providers._pb_word("floral seamless pattern") == "floral seamless pattern",
   "_pb_word 原样放行英文词")
ok(providers._pb_word("chinoiserie floral site:pinterest.com")
   == "chinoiserie floral",
   "_pb_word 剥掉 site: 限定（那是给通用搜索引擎用的，站内搜索会搜空）")
ok(providers._pb_word("水彩花卉 平铺 印花素材") == "",
   "_pb_word 丢掉中文词（这站中文词不过滤，只会掺无关图）")
ok(len(providers._pb_word("a b c d e f g h i").split()) == 6,
   "_pb_word 长词截到 6 个（词太多这站也是 0 条）")


class _FakeEl:
    """假的元素。attrib 是它的属性，_kids 是它的子元素。"""


def _el(attrib, kids=()):
    e = _FakeEl()
    e.attrib = attrib
    e._kids = kids
    e.css = lambda sel: [_el(k[0], k[1]) for k in kids] if sel == "img" else []
    return e


class _FakePage:
    """够用的 Scrapling Selector 替身 —— 让解析逻辑**在没装 Scrapling 的
    机器上也能测**（后端那套 venv 就不装它）。只实现 _pb_cards 用到的
    两层：css('a') 和 a.css('img')。"""

    def __init__(self, links):
        self._links = links          # [(href, [img属性dict]), ...]

    def css(self, sel):
        if sel != "a":
            return []
        return [_el({"href": href}, [(d, ()) for d in imgs])
                for href, imgs in self._links]


CDN = "https://production-cdn2.patternbank.com/uploads/uploaded_files/x/"
PB_PAGE = _FakePage([
    # 一张正常的设计卡片
    ("/OrangeTheStudio/designs/464455300-soft-daisy-floral-on-deep-brown",
     [{"src": CDN + "1.jpg", "width": "1200", "height": "1200"}]),
    # 同一张卡重复出现一次（页面里真有），不该收两遍
    ("/OrangeTheStudio/designs/464455300-soft-daisy-floral-on-deep-brown",
     [{"src": CDN + "1.jpg"}]),
    # 有 alt 的，标题该用 alt
    ("/bethanialima/designs/718455305-elegant-green-foliage",
     [{"data-src": CDN + "2.jpg", "alt": "Elegant green foliage"}]),
    # 不是设计卡片（站里"猜你喜欢"的图），不该收
    ("/trends/ss28-print-pattern-forecast", [{"src": CDN + "3.jpg"}]),
    # 是卡片但图不是站里的 uploaded_files（占位图 / logo），不该收
    ("/pans/designs/356387021-texture-leaf",
     [{"src": "https://patternbank.com/assets/logo.svg"}]),
])
_pb = providers._pb_cards(PB_PAGE, "floral", 0)
ok(len(_pb) == 2, "只收 /designs/ 的真卡片，重复的丢掉（%d 条）" % len(_pb))
ok(all(r["provider"] == "patternbank" for r in _pb),
   "provider 标成 patternbank")
ok(all(r["site"] == "patternbank.com" for r in _pb),
   "site 显式写成 patternbank.com（图在三个 CDN 上，按直链取域名会散开）")
ok(_pb[0]["title"] == "soft daisy floral on deep brown",
   "alt 为空时标题从 URL 的 slug 来（'soft daisy floral on deep brown'）")
ok(_pb[1]["title"] == "Elegant green foliage",
   "有 alt 就用 alt")
ok(_pb[0]["page"].startswith("https://patternbank.com/"),
   "page 是补全成绝对地址的详情页链接")
ok(_pb_capped := providers._pb_cards(PB_PAGE, "floral", 1),
   "cap=1 时按 per_query 截断")
ok(len(_pb_capped) == 1, "per_query 真的生效（只留 1 条）")

# 打桩 scrape.get，测整家：中文词跳过、英文词出结果
_gets = []
_real_get = providers.scrape.get


def _fake_get(url, *, timeout=25, gap=0):
    _gets.append(url)
    return PB_PAGE


providers.scrape.get = _fake_get
providers.scrape.have = lambda: True
try:
    _rows = providers._patternbank(
        ["chinoiserie floral site:pinterest.com", "水彩 花卉 平铺", "floral"],
        10, "", {})
    ok(len(_gets) == 2, "三条词里中文那条被跳过，只发了 2 次请求（%d）" % len(_gets))
    ok(all("q=" in u for u in _gets) and "floral" in _gets[0],
       "英文词进了 URL 的 q=，且 site: 已被剥掉")
    ok(len(_rows) == 4, "两家词各出 2 条（去重后共 %d 条）" % len(_rows))
    ok(providers._patternbank(["floral"], 10, "", {"page": 3}),
       "翻页参数能传进去")
    ok("&page=3" in _gets[-1], "第 3 页的 page 参数拼进了 URL")
finally:
    providers.scrape.get = _real_get
    providers.scrape.have = _real_have

ok(providers.scrape.GAP > 0,
   "爬取层有限速（GAP=%s 秒）—— 连打会被这站 403" % providers.scrape.GAP)
# 和 net.py 同一口径：**强制直连**。Scrapling 底层是 libcurl，会读
# http_proxy/https_proxy；本机系统代理常年开着，有的站经代理会被重定向到
# 127.0.0.1 然后被 curl 的 SSRF 保护拒掉。所以发请求前必须摘掉，发完放回。
_old_proxy = os.environ.get("HTTP_PROXY")
os.environ["HTTP_PROXY"] = "http://127.0.0.1:9/"
with providers.scrape._no_proxy():
    _during = os.environ.get("HTTP_PROXY")
ok(_during is None, "发请求时把代理变量摘掉（不走系统代理）")
ok(os.environ.get("HTTP_PROXY") == "http://127.0.0.1:9/",
   "发完请求把代理变量放回去（不能把进程环境改坏）")
if _old_proxy is None:
    os.environ.pop("HTTP_PROXY", None)
else:
    os.environ["HTTP_PROXY"] = _old_proxy


# ========================= [1e] 瓦栏（公开搜索页，免登录，在售花型）
print("\n[1e] 瓦栏：公开搜索页解析（在售花型，只取公开缩略图）")

# 按真实页面结构造的假 HTML：卡片是 `<div class="design_grid rel">`，
# 里面 a[data-photoid] + img[alt][src]。第三块故意重复 1001，验去重。
_WALAN_HTML = """
<div class="row row10">
 <div class="col-xs-6">
  <div class="design_grid rel">
   <div class="design_grid_img">
    <a class="design_grid_link" target="_blank" name="design1001" href="/designs/1001/" data-photoid="1001">
     <img class="img-responsive" alt="豹纹印花花型图片" title="豹纹印花花型图片"
          src="https://www.walanwalan.com/wldata/cacheimgs/photos/1001_son/aaa.jpg">
    </a>
   </div>
  </div>
 </div>
 <div class="col-xs-6">
  <div class="design_grid rel">
   <a class="design_grid_link" href="/designs/1002/" data-photoid="1002">
    <img class="img-responsive" alt="中国风水墨印花花型图片"
         src="/wldata/cacheimgs/photos/1002_son/bbb.jpg">
   </a>
  </div>
  <div class="design_grid rel">
   <a class="design_grid_link" href="/designs/1001/" data-photoid="1001">
    <img class="img-responsive" alt="重复卡片" src="https://x/dup.jpg">
   </a>
  </div>
 </div>
</div>
"""


class _FakeWalan:
    def __init__(self):
        self.urls = []

    def get_text(self, url, **kw):
        self.urls.append(url)
        return _WALAN_HTML


_wsess = _FakeWalan()
_real_wsess = providers.net.session
providers.net.session = lambda home=None, **kw: _wsess
try:
    _wr = providers._walan(["水墨"], 100, "", {"page": 2})
finally:
    providers.net.session = _real_wsess

ok(len(_wr) == 2, "解析出 2 张（同 photoid 的重复卡片被去掉，实际 %d）" % len(_wr))
ok([r["title"] for r in _wr] == ["豹纹", "中国风水墨"],
   "标题剥掉「印花花型图片」尾巴：%s" % [r["title"] for r in _wr])
ok(_wr[1]["url"].startswith("https://www.walanwalan.com/wldata/"),
   "相对路径的图补成绝对地址")
ok(_wr[0]["page"].endswith("/designs/1001/"), "详情页链接拼对了（用户去买正版的那条路）")
ok(_wr[0]["site"] == "walanwalan.com", "site 钉成瓦栏，不是 CDN 域名")
ok(_wsess.urls and "/search/patterns/" in _wsess.urls[0]
   and "/page2/" in _wsess.urls[0], "URL 形状：/search/patterns/{词}/pageN/")
ok(providers._walan_title("") == "" and providers._walan_title("花卉") == "花卉",
   "标题清洗不把短标签吃掉（「花卉」不能被剥空）")

# 截词：这站按空格做与匹配，长首词只剩个位数（"青花 四方连续" 实测 3 张）
_w = providers._walan_words("水墨画元素 国风 花卉 平铺 印花")
ok("水墨" in _w, "长首词会一路退到两字（%s）" % _w)
ok(_w[-1] == "水墨", "最宽的一档排在最后（从具体到宽）")
_we = providers._walan_words("floral seamless pattern")
ok(_we == ["floral seamless", "floral"], "英文词不乱截：%s" % _we)
ok(providers.enabled("walan", {}) and "walan" in providers.available({}),
   "瓦栏是免 key 源，一个 key 都不配也能用")


# ==================== [1f] Spoonflower（浏览器层，2026-10-18 用户要求开成默认）
print("\n[1f] Spoonflower：Cloudflare 站的解析与门禁")

ok(providers.enabled("spoonflower", {}) == providers.scrape.have_browser(),
   "能不能用取决于浏览器层装没装（没装就从可用列表消失，不报 403）")
ok("spoonflower" not in providers.on_demand(),
   "已经进默认了，就不许再出现在「要手动开」清单里（两边都占会让人以为要手动开）")
ok("spoonflower" in providers.DEFAULT_ORDER,
   "**已进默认**（用户 2026-10-18 明确要求打开）：代价是一轮多 1.5~2 分钟")

# 中文词直接跳过（英文站搜不到），而且**一次请求都不该发**
_sent = []


class _Img:
    def __init__(self, attrib):
        self.attrib = attrib


class _Page:
    def __init__(self, imgs):
        self._imgs = imgs

    def css(self, sel):
        return self._imgs if sel == "img" else []


class _Sess:
    def fetch(self, url, **kw):
        _sent.append(url)
        return _Page([
            _Img({"src": "https://www.spoonflower.com/img/logo.svg", "alt": "logo"}),
            _Img({"src": "https://img.spoonflower.com/c/777/p/f/m/ABCDEF/777.png",
                  "alt": "Among Flowers"}),
            _Img({"src": "https://img.spoonflower.com/c/888/p/f/s/GHIJKL/888.png",
                  "alt": ""}),
        ])


class _Ctx:
    def __enter__(self):
        return _Sess()

    def __exit__(self, *a):
        return False


_real_sess = providers.scrape.stealth_session
_real_hb = providers.scrape.have_browser
providers.scrape.stealth_session = lambda **kw: _Ctx()
providers.scrape.have_browser = lambda: True
try:
    ok(providers._spoonflower(["水墨 花卉 平铺"], 10, "", {}) == [] and not _sent,
       "中文词直接跳过，一次请求都不发")
    _sr = providers._spoonflower(["chinoiserie floral"], 10, "", {})
    ok(len(_sr) == 2, "只收 img.spoonflower.com 的图（logo 被丢），拿到 %d 条" % len(_sr))
    ok(_sr[0]["url"] == "https://img.spoonflower.com/c/777/p/f/l/ABCDEF/777.png",
       "m 档换成 l 档（400px，最大就只有这个）：%s" % _sr[0]["url"][-26:])
    ok(_sr[1]["url"].endswith("/888/p/f/l/GHIJKL/888.png") if "/p/f/l/" in _sr[1]["url"]
       else False, "s 档也换成 l 档")
    ok(_sr[0]["title"] == "Among Flowers", "标题取 alt（真语义标题，不是编号）")
    ok(_sr[1]["title"].startswith("spoonflower "),
       "alt 为空时用 designId 兜底（别留空标题）")
    ok(_sr[0]["page"] == "https://www.spoonflower.com/en/designs/777",
       "来源页按 designId 拼，用户能点回去看")

    # `site:` 必须先剥掉再截词 —— analyze 的英文词常带 site:pinterest.com，
    # 带着它去站内搜索等于 0 条，而且**不报错**（看着像"这家没货"）。
    _sent.clear()
    _sr2 = providers._spoonflower(
        ["site:pinterest.com French romantic ditsy floral seamless pattern"], 10, "", {})
    ok(_sent and "site%3A" not in _sent[0] and "site:" not in _sent[0],
       "**剥掉 site: 再搜**（否则 Spoonflower 每轮静默交白卷）：%s" % (_sent[0][-60:]))
    ok(len(_sr2) == 2, "剥完照样出图（%d 条）" % len(_sr2))
finally:
    providers.scrape.stealth_session = _real_sess
    providers.scrape.have_browser = _real_hb


# ============ [1g] 色彩跳动（公开 JSON 接口，免登录，在售花型）
print("\n[1g] 色彩跳动：公开列表接口解析（免登录，只取预览图）")

ok(providers.enabled("dancecolor", {}) and "dancecolor" in providers.available({}),
   "免 key 免登录源，一个 key 都不配也能用")
ok("dancecolor" in providers.DEFAULT_ORDER,
   "**进默认**：一个 POST 回 100 条、0.5 秒，不像 Spoonflower 那样拖时间")
ok("dancecolor" not in providers.on_demand(),
   "既然是默认跑的，就不该出现在「要手动开」的清单里")

# —— 预览图：必须走站方那套 BOS 图片处理，**绝不能原样下原图** ——
_dc_raw = "https://su.bcebos.com/v1/annuoqi-prod/20230825112400024125.png"
_dc_pv = providers._dc_preview(_dc_raw)
ok("x-bce-process" in _dc_pv and "w_480,h_480" in _dc_pv,
   "原图直链换成站方图片处理的预览（长边钉 480，跟瓦栏那档一个量级）")
ok(_dc_pv.startswith(_dc_raw + "?"), "在原始直链后面挂参数，不换域名")
ok("ignore-error,i_1" in _dc_pv,
   "`ignore-error,i_1` 是原厂参数，删了老图会 400")
ok(providers._dc_preview(_dc_pv) == _dc_pv, "已经是预览就不再套一层（别叠两个 query）")
ok(providers._dc_preview("") == "", "空直链回空串，别拼出个 `?x-bce-process`")

# —— 标题：worksTitle 常是「DC170185027151453 标签1,标签2…」这种垃圾 ——
ok(providers._dc_title({"worksId": "DC170185027151453",
                        "worksName": "水墨中国风生肖龙",
                        "worksTitle": "DC170185027151453 时尚创意,节日,装饰纹样,动物"})
   == "水墨中国风生肖龙",
   "标题被系统拿 id+标签拼过时，退回 worksName")
ok(providers._dc_title({"worksId": "DC169294078996050",
                        "worksName": "", "worksTitle": "叶子与山水墨风国风"})
   == "叶子与山水墨风国风", "worksName 为空就用 worksTitle")

# —— 检索词：左边截不够，这站主语在尾巴上 ——
_wd = providers._dc_words("法式小碎花 平铺 无缝 水彩 粉彩 花卉 四方连续")
ok("小碎花" in _wd and "碎花" in _wd,
   "**尾部截断**：法式小碎花 → 小碎花 / 碎花（实测 法式 只有 5 条、小碎花 1838 条）")
ok("四方连续" in _wd, "行业词单独试一条（这站有 85 条标题明写「四方连续」）")
ok(_wd.index("法式") < _wd.index("小碎花"), "还是从具体到宽，别一上来就撒大网")

# —— 解析：假接口回两页，第二页故意重复第一条 ——
_dc_calls = []


def _fake_post_json(url, payload, **kw):
    _dc_calls.append((url, payload, kw))
    cur = payload["page"]["current"]
    if cur == 1:
        return {"code": "200", "data": {"total": 3, "pages": 2, "records": [
            {"worksId": "DC1001", "fileUrl": _dc_raw,
             "worksName": "水彩晕染花卉", "worksTitle": "水彩晕染花卉",
             "fileWidth": 896.0, "fileHeight": 1344.0},
            {"worksId": "DC1002", "fileUrl": "https://su.bcebos.com/v1/x/2.jpg",
             "worksName": "", "worksTitle": "DC1002 标签,标签",
             "fileWidth": 1200, "fileHeight": 1200},
        ]}}
    return {"code": "200", "data": {"total": 3, "pages": 2, "records": [
        {"worksId": "DC1001", "fileUrl": _dc_raw, "worksName": "重复卡片",
         "fileWidth": 896.0, "fileHeight": 1344.0},
        {"worksId": "DC1003", "fileUrl": "https://su.bcebos.com/v1/x/3.jpg",
         "worksName": "青花瓷缠枝莲", "fileWidth": 800.0, "fileHeight": 800.0},
    ]}}


_real_post = providers.net.post_json
providers.net.post_json = _fake_post_json
try:
    _dr = providers._dancecolor(["水墨"], 50, "", {"dc_pages": 2})
finally:
    providers.net.post_json = _real_post

ok(len(_dr) == 3, "两页 4 条、worksId 去重后剩 3 条（实际 %d）" % len(_dr))
ok([r["title"] for r in _dr] == ["水彩晕染花卉", "DC1002 标签,标签", "青花瓷缠枝莲"],
   "标题按记录走：%s" % [r["title"] for r in _dr])
ok(_dr[0]["url"] == providers._dc_preview(_dc_raw),
   "主链是预览图，不是 fileUrl 原图")
ok(_dr[0]["page"] == "https://www.dancecolor.net/#/detail?worksId=DC1001",
   "来源页拼成详情页（用户去买正版的那条路）：%s" % _dr[0]["page"][-30:])
ok(_dr[0]["site"] == "dancecolor.net", "site 钉成色彩跳动，不是 bcebos 的 CDN 域名")
ok((_dr[0]["w"], _dr[0]["h"]) == (896, 1344),
   "w/h 取 fileWidth/fileHeight（原图尺寸，不是预览尺寸）—— 好判够不够用")
ok(_dc_calls and _dc_calls[0][0].endswith("/pb/crms/api/pageHome/getHomeInfoPage"),
   "接口路径带 `/pb/crms` 微服务前缀（少了这段就是 404）")
_p0 = _dc_calls[0][1]
ok(_p0["customQueryParams"]["worksTitle"] == "水墨"
   and _p0["customQueryParams"]["type"] == "1",
   "搜索参数：worksTitle + type=1（商城在售那一类，跟站内一致）")
ok(_p0["page"]["size"] == 100, "一页要满 100 条（实测 size:100 有效）")
_hk = {k.lower() for k in (_dc_calls[0][2].get("headers") or {})}
ok("authorization" not in _hk and "token" not in _hk,
   "**不发任何鉴权头**：这个接口本来就是匿名公开的，别去带账号")


# ============ [1h] 全球纺织网·花样画稿市场（SSR + GBK 关键词，在售花型）
print("\n[1h] 全球纺织网·花样画稿：GBK 搜索 + 600×900 缩略图")

ok(providers.enabled("tnc", {}) and "tnc" in providers.available({}),
   "免 key 免登录源，一个 key 都不配也能用")
ok("tnc" in providers.DEFAULT_ORDER, "**进默认**：SSR 一页 24 条、4 秒回 60 张")
ok("tnc" not in providers.on_demand(), "默认跑的就不该出现在「要手动开」清单里")

# —— 检索词编码：这站只认 GBK，UTF-8 上去会被静默忽略 ——
ok(providers._hx_word("水墨") == "%CB%AE%C4%AB",
   "检索词按 **GBK** 百分号编码（UTF-8 编码会回 200 但 k 参数被整段忽略）")
ok(providers._hx_word("floral") == "floral", "纯英文词原样透传")
ok(providers._hx_word("") == "", "空词不炸")

# —— 标题：价格和编号不是描述 ——
_hx_card = ('<li class="item"><a href="/product-detail-13656064.html" target="_blank">'
            '<img class="item-image" src="https://imgqfc.tnccdn.com/upload/102/outer/86/45/x.jpeg" />'
            '<div class="title"><span class="price">￥199 / ￥299</span>'
            '<span class="wl-orange color">( 0017696 )</span><br> '
            '<div>中国风-水墨</div></div></a></li>')
ok(providers._hx_title(_hx_card) == "中国风-水墨",
   "标题只留结构化标签，价格和作品编号剥掉：%r" % providers._hx_title(_hx_card))
ok(providers._hx_title('<li><div class="title"><span>￥99</span><br><div>四方连续-花卉</div></div></li>')
   == "四方连续-花卉", "单档价（只有一个 ￥）也要剥干净")

# —— 解析：假页面两页，第二页故意重复第一条 ——
_hx_calls = []


def _fake_hx_request(url, **kw):
    _hx_calls.append((url, kw))
    if "-p2.html" in url:
        body = ('<li class="item"><a href="/product-detail-1001.html">'
                '<img src="https://imgqfc.tnccdn.com/a/dup.jpeg" />'
                '<div class="title"><span>￥99</span><br><div>重复卡片</div></div></a></li>'
                '<li class="item"><a href="/product-detail-1003.html">'
                '<img src="https://imgqfc.tnccdn.com/a/3.jpeg" />'
                '<div class="title"><span>￥158</span><br><div>中国风-香云</div></div></a></li>')
    else:
        body = ('<li class="item"><a href="/product-detail-1001.html">'
                '<img src="https://imgqfc.tnccdn.com/a/1.jpeg" />'
                '<div class="title"><span>￥128</span><br><div>四方连续-花卉</div></div></a></li>'
                '<li class="item"><a href="/product-detail-1002.html">'
                '<img src="https://imgqfc.tnccdn.com/a/2.jpeg" />'
                '<div class="title"><span>￥199 / ￥299</span><br><div>中国风-水墨</div></div></a></li>')
    return 200, {}, body.encode("gbk")


_real_req = providers.net.request
providers.net.request = _fake_hx_request
try:
    _hr = providers._hx(["水墨"], 50, "", {"hx_pages": 2})
finally:
    providers.net.request = _real_req

ok(len(_hr) == 3, "两页 4 条、作品 id 去重后剩 3 条（实际 %d）" % len(_hr))
ok([r["title"] for r in _hr] == ["四方连续-花卉", "中国风-水墨", "中国风-香云"],
   "标题按卡片走：%s" % [r["title"] for r in _hr])
ok(_hr[0]["url"] == "https://imgqfc.tnccdn.com/a/1.jpeg", "主链是列表页那张缩略图")
ok(_hr[0]["page"] == "https://hx.tnc.com.cn/product-detail-1001.html",
   "来源页拼成详情页（正版下单那条路）：%s" % _hr[0]["page"][-26:])
ok(_hr[0]["site"] == "hx.tnc.com.cn", "site 钉成 hx 站，不是 imgqfc 的 CDN 域名")
ok((_hr[0]["w"], _hr[0]["h"]) == (600, 900), "w/h 钉死 600×900（实测这一档是恒定的）")
ok(_hx_calls and "/search/products-c-13773-k-%CB%AE%C4%AB-p1.html" in _hx_calls[0][0],
   "URL 形状：/search/products-c-<全部分类>-k-<GBK词>-p<页>.html")
ok(all(c[1].get("loose_tls") for c in _hx_calls),
   "**必须带 loose_tls**：这站只回 TLS1.2 + AES128-SHA，默认 context 握手直接失败")
ok(all("hx.tnc.com.cn" in (c[1].get("headers") or {}).get("Referer", "") for c in _hx_calls),
   "带本站 Referer")

# 分词太宽的后果：长词不该被"退让"成更宽的东西，_hx_words 只给两档
ok(providers._hx_words("水墨牡丹 四方连续 印花 图案 国风")
   == ["水墨牡丹 四方连续", "水墨牡丹"],
   "检索词不做额外退让（这站是分词 OR，退让只会更宽）：%s"
   % providers._hx_words("水墨牡丹 四方连续 印花 图案 国风"))


# ============ [1i] Pattern Designs（公开 JSON 分页接口，矢量无缝图案）
print("\n[1i] Pattern Designs：ajaxLoadMoreDesigns 解析（offset/limit 翻页）")

ok(providers.enabled("patterndesigns", {}),
   "免 key 源：不配 key 也 enabled")
ok("patterndesigns" in providers.DEFAULT_ORDER
   and "patterndesigns" not in providers.on_demand(),
   "**已进默认**（用户 2026-10-18 明确要求打开）；水印/硬边矢量的代价靠甄别兜")

# —— 检索词：**行业词必须丢掉**，跟瓦栏/色彩跳动正好相反 ——
ok(providers._pd_words("国风 水墨 花卉 四方连续") == ["国风", "水墨"],
   "只取题材词（这站每条标题都带「无缝矢量模式设计」，行业词=全库）：%s"
   % providers._pd_words("国风 水墨 花卉 四方连续"))
ok(providers._pd_words("四方连续") == [],
   "全是行业词就返回空，让这家跳过，别拿「四方连续」把全库捞回来")
ok(providers._pd_words("牡丹 花卉 印花") == ["牡丹", "花卉"],
   "具体花名原样保留（实测 牡丹/玫瑰/樱花/竹 命中很准）")

_r = {"designs": []}
_ok = {"designs": [
    {"id": 10094, "title": "花卉缩影 无缝矢量模式设计",
     "galleryImageUrl": "https://d1biw2rz2h5h5w.cloudfront.net/images/patterns/thumbs/gallery-m/10094/A.webp",
     "detailImageUrl": "https://d1biw2rz2h5h5w.cloudfront.net/images/patterns/thumbs/detail/10094/A.jpg",
     "designUrl": "/zh/design/10094", "designer": "Julia"},
    {"id": 10095, "title": '带 "引号" 的标题',
     "galleryImageUrl": "https://d1biw2rz2h5h5w.cloudfront.net/images/patterns/thumbs/gallery-m/10095/B.webp",
     "designUrl": "/zh/design/10095"},
]}
_pd_calls = []


def _fake_pd_request(url, **kw):
    _pd_calls.append((url, kw))
    off = int(url.split("offset=")[1].split("&")[0])
    if off == 0:
        return 200, {}, json.dumps(_ok).encode("utf-8")
    return 200, {}, json.dumps(_r).encode("utf-8")     # 第二页空 = 到头了


_real_req2 = providers.net.request
providers.net.request = _fake_pd_request
try:
    _pr = providers._patterndesigns(["花卉"], 50, "", {})
finally:
    providers.net.request = _real_req2

ok(len(_pr) == 2, "第一页 2 条、第二页空就停（实际 %d）" % len(_pr))
ok(_pr[0]["url"].endswith("10094/A.webp"), "主链取 gallery-m 那档（700×700）")
ok("/thumbs/detail/" not in _pr[0]["url"],
   "**不取 detailImageUrl**：那档只有 400×400，比 gallery-m 还小")
ok((_pr[0]["w"], _pr[0]["h"]) == (700, 700), "w/h 钉 700×700（实测恒定）")
ok(_pr[0]["page"] == "https://www.patterndesigns.com/zh/design/10094",
   "来源页拼成设计详情页：%s" % _pr[0]["page"])
ok(_pr[0]["site"] == "patterndesigns.com", "site 钉成站点，不是 cloudfront 域名")
ok(_pr[0]["title"] == "花卉缩影 无缝矢量模式设计", "标题原样用它的（机翻的中文也能看懂）")
_pd_u = _pd_calls[0][0]
ok("/zh/search/ajaxLoadMoreDesigns" in _pd_u,
   "端点来自分类页 `.designs[data-ajaxloadmore]` 的明文，别自己猜")
ok("text=%E8%8A%B1%E5%8D%89" in _pd_u,
   "**关键词走 text=**（q= / searchText= 都无效，会静默回默认列表）")
ok("limit=60" in _pd_u and "offset=0" in _pd_u, "翻页靠 offset/limit（?page=2 无效）")
ok(len(_pd_calls) == 1,
   "第一页只有 2 条（不满 limit 60）就收手，不白翻一次")

# 满页才该往后翻：补一个 60 条满页的场景
_pd_calls.clear()


def _fake_pd_full(url, **kw):
    _pd_calls.append((url, kw))
    if "offset=0" in url:
        ds = [{"id": 20000 + i, "title": "t%d" % i,
               "galleryImageUrl": "https://x/%d.webp" % (20000 + i),
               "designUrl": "/zh/design/%d" % (20000 + i)} for i in range(60)]
        return 200, {}, json.dumps({"designs": ds}).encode("utf-8")
    return 200, {}, json.dumps({"designs": []}).encode("utf-8")


providers.net.request = _fake_pd_full
try:
    _pr2 = providers._patterndesigns(["花卉"], 120, "", {})
finally:
    providers.net.request = _real_req2

ok(len(_pd_calls) == 2 and "offset=60" in _pd_calls[1][0],
   "满页才带着 offset=60 往后翻（?page=2 在这站是无效的）")
ok(len(_pr2) == 60, "第二页空就停，最后 60 条（实际 %d）" % len(_pr2))


# ============ [1j] 源创花样网（WebForms PostBack 翻页 + **放宽平铺闸门**）
print("\n[1j] 源创花样网：cbp-item 卡片 + PostBack 翻页 + tile_opt 线索图")

ok(providers.enabled("ypattern", {}), "免 key 源：不配 key 也 enabled")
ok("ypattern" not in providers.DEFAULT_ORDER and "ypattern" in providers.on_demand(),
   "**不进默认**：预览被站方裁过（4:3），只能当线索，要 --provider 手动开")

ok(providers._yp_words("法式小碎花 水彩 粉彩 花卉 四方连续")
   == ["法式小碎花 四方连续", "法式小碎花"],
   "检索词只给两档（搜索很宽，长词不会 0 条）：%s"
   % providers._yp_words("法式小碎花 水彩 粉彩 花卉 四方连续"))

_YP_HTML = """<ul>
<li class="cbp-item designerli">
  <a href="/preview.aspx?code=AA11BB22"><img src="/Preview/20230320/AA11BB22/AA11BB22.jpg" /></a>
</li>
<li class="cbp-item designerli">
  <a href="/preview.aspx?code=CC33DD44"><img src="/Preview/0812016/XX99YY88/XX99YY88.jpg" /></a>
</li>
<li class="cbp-item designerli">
  <a href="/preview.aspx?code=EE55FF66"><img src="/Preview/0812016/EE55FF66/EE55FF66.jpg" /></a>
</li>
<div id="yourlike"><ul class="jcarousel">
  <li class="column-four"><a href="/preview.aspx?code=ZZ99"><img src="/Preview/0130018/ZZ99/ZZ99.jpg" /></a></li>
</ul></div></ul>"""

_ypc = providers._yp_cards(_YP_HTML)
ok(len(_ypc) == 2,
   "只认 `cbp-item designerli`：坏卡被剔、`#yourlike` 推荐块不进来（实际 %d）"
   % len(_ypc))
ok([c["code"] for c in _ypc] == ["AA11BB22", "EE55FF66"], "编号按页面顺序")
ok(_ypc[0]["img"] == "https://www.ypattern.com/Preview/20230320/AA11BB22/AA11BB22.jpg",
   "图片直链**连日期段一起**拼（少那段就 404）")
ok(_ypc[1]["img"].endswith("/Preview/0812016/EE55FF66/EE55FF66.jpg"),
   "日期段是脏值（7 位）也照用，别拿它做判断")
ok(providers._yp_fields('<input type="hidden" id="__VIEWSTATE" value="a&amp;b+c" />')
   ["__VIEWSTATE"] == "a&b+c", "PostBack 隐藏域要 html.unescape")

_yp_calls = []
_yp_gap = providers._YP_GAP
providers._YP_GAP = 0


def _yp_page(tag):
    return ('<li class="cbp-item designerli"><a href="/preview.aspx?code=%s">'
            '<img src="/Preview/20230320/%s/%s.jpg" /></a></li>' % (tag, tag, tag))


def _fake_yp(url, **kw):
    _yp_calls.append((url, kw))
    if kw.get("method") == "POST":
        return 200, {}, (_yp_page("P2A") + _yp_page("P2B")).encode("utf-8")
    return 200, {}, (_yp_page("P1A") + _yp_page("P1B")).encode("utf-8")


providers.net.request = _fake_yp
try:
    _yp = providers._ypattern(["水墨"], 40, "", {"yp_pages": 2})
finally:
    providers.net.request = _real_req2
    providers._YP_GAP = _yp_gap

ok(len(_yp) == 4, "两页各 2 条 = 4 条（实际 %d）" % len(_yp))
ok((_yp[0]["w"], _yp[0]["h"]) == (400, 300), "w/h 钉 400×300（实测就这个尺寸）")
ok(_yp[0]["title"] == "", "**标题留空**：卡片只有编号/花回尺寸/价格，不硬编名字")
ok(_yp[0]["page"] == "https://www.ypattern.com/preview.aspx?code=P1A",
   "来源页拼成详情页（看中了去站上买）")
ok(_yp[0]["site"] == "ypattern.com", "site 钉成站点")
ok(all(r["tile_opt"] is True for r in _yp),
   "**全部带 tile_opt**：这是全项目唯一放宽平铺闸门的来源")
ok("kw=%E6%B0%B4%E5%A2%A8" in _yp_calls[0][0],
   "检索词走 **UTF-8**（跟全球纺织网正好相反）：%s" % _yp_calls[0][0])
ok("%CB%AE" not in _yp_calls[0][0], "**不能按 GBK 编**：这站 GBK 直接回 500")
_p1 = _yp_calls[1][1]
ok(_p1.get("method") == "POST", "第 2 页必须是表单 POST（`?page=2` 完全不认）")
_p1b = _p1["data"].decode("utf-8")
ok("__EVENTTARGET=AspNetPager1" in _p1b and "__EVENTARGUMENT=2" in _p1b,
   "PostBack 参数：__EVENTTARGET=AspNetPager1 / __EVENTARGUMENT=页号")
ok("__VIEWSTATE=" in _p1b and "__EVENTVALIDATION=" in _p1b,
   "上一页隐藏域原样回填（viewstate 必须在，少一个服务端就 500）")
ok(all((c[1].get("headers") or {}).get("Cookie") is None for c in _yp_calls),
   "**不带任何 Cookie/登录态**（只读公开列表页）")

_yp_calls.clear()


def _fake_yp_403(url, **kw):
    _yp_calls.append((url, kw))
    return 403, {}, b"<html>403</html>"


providers.net.request = _fake_yp_403
providers._YP_GAP = 0
try:
    _yp403 = providers._ypattern(["孔雀"], 40, "", {"yp_pages": 3})
finally:
    providers.net.request = _real_req2
    providers._YP_GAP = _yp_gap
ok(_yp403 == [] and len(_yp_calls) == 1,
   "403 的词直接放弃（「孔雀」实测稳定 403），不重试不翻页（请求数 %d）"
   % len(_yp_calls))


# ============ [1k] Patternbulk（RSC payload 里挖设计对象：标题 + 25 个标签）
print("\n[1k] Patternbulk：从 RSC payload 挖标题/标签，本地筛 + 交给模型判")

ok(providers.enabled("patternbulk", {}), "免 key 源：不配 key 也 enabled")
ok("patternbulk" not in providers.DEFAULT_ORDER
   and "patternbulk" in providers.on_demand(),
   "**不进默认**（266×266 + 没有服务端搜索），要 --provider 手动开")

ok(providers._pbk_tokens(
    "site:pinterest.com chinoiserie ink wash floral seamless repeat pattern")
   == ["chinoiserie", "ink", "wash", "floral"],
   "剥掉 site: 与行业词，只留题材英文词：%s"
   % providers._pbk_tokens("site:pinterest.com chinoiserie ink wash floral seamless"))
ok(providers._pbk_tokens("国风 水墨 花卉 四方连续") == [],
   "全中文 → 空（这站标题标签全英文，喂中文只会 0 命中，别硬凑）")

_PBK_HTML = (
    '<script>self.__next_f.push([1,"6:[\\"$\\",\\"$L25\\",null,'
    '{\\"designs\\":[{\\"id\\":\\"11111111-1111-1111-1111-111111111111\\",'
    '\\"title\\":\\"Chinoiserie Botanical Bird\\",\\"description\\":\\"$26\\",'
    '\\"tags\\":[\\"seamless\\",\\"chinoiserie\\",\\"botanical\\"],'
    '\\"image\\":\\"pattern_aaaaaaaa-0000-0000-0000-000000000000.jpg\\",'
    '\\"folderId\\":\\"22222222-2222-2222-2222-222222222222\\"},'
    '{\\"id\\":\\"33333333-3333-3333-3333-333333333333\\",'
    '\\"title\\":\\"Geometric Plaid\\",\\"description\\":\\"$27\\",'
    '\\"tags\\":[\\"seamless\\",\\"plaid\\"],'
    '\\"image\\":\\"pattern_bbbbbbbb-0000-0000-0000-000000000000.jpg\\",'
    '\\"folderId\\":\\"44444444-4444-4444-4444-444444444444\\"}]}"])</script>')

_pd = providers._pbk_designs(_PBK_HTML)
ok(len(_pd) == 2, "从转义的 RSC 里挖出 2 个设计对象（实际 %d）" % len(_pd))
ok(_pd[0]["title"] == "Chinoiserie Botanical Bird", "标题原样取（站方写的，不是机翻）")
ok(_pd[0]["tags"] == ["seamless", "chinoiserie", "botanical"], "标签原样取")
ok(_pd[0]["folder"] == "22222222-2222-2222-2222-222222222222", "folderId 留着拼图链")

_pbk_calls = []


def _fake_pbk(url, **kw):
    _pbk_calls.append((url, kw))
    return 200, {}, _PBK_HTML.encode("utf-8")


providers.net.request = _fake_pbk
try:
    _pbk = providers._patternbulk(["chinoiserie botanical"], 20, "", {})
finally:
    providers.net.request = _real_req2

ok(len(_pbk) == 2, "命中 1 条 + 兜底补到 12 以内的其余设计 = 2 条（实际 %d）"
   % len(_pbk))
ok(_pbk[0]["title"] == "Chinoiserie Botanical Bird", "命中的排前面（按标题/标签打分）")
ok(_pbk[0]["url"]
   == "https://patternbulk.com/api/fetchFile?fileName=thumb_pattern_aaaaaaaa-0000-0000-0000-000000000000.jpg&folderName=uploaded_Designs/22222222-2222-2222-2222-222222222222",
   "图链直取 fetchFile（**加 thumb_ 前缀**，原图 pattern_* 是 404）")
ok((_pbk[0]["w"], _pbk[0]["h"]) == (266, 266), "w/h 钉 266×266（缩略图真实尺寸）")
ok(_pbk[0]["page"].endswith("/studio?design=11111111-1111-1111-1111-111111111111"),
   "来源页带上设计 id：%s" % _pbk[0]["page"])
ok(_pbk[0]["site"] == "patternbulk.com", "site 钉成站点")
ok(len(_pbk_calls) == 1, "一个请求拿全量（`?q=` 无效，翻页参数也无效，别白试）")
ok(all(r["tile_opt"] is False for r in _pbk),
   "**不带走私口子**：它没过闸门就该被拦，别跟着 ypattern 一起放宽")


# ============================================================== [2] 假搜索源
print("\n[2] 检索层：多源并发 + 单源出错不影响别家")
FAKE = {}


def fake_provider_a(queries, per_query, key, opts):
    """假 A 站。**第 2 页故意给另一批图** —— 用来测"不够时自动翻页补一轮"。

    URL 里的第二个数是"画哪张图"的编号（见 _blob_for），所以第 2 页换成
    10+i，内容才真的是新的。第 1 页的 URL 格式和以前完全一样。
    """
    pg = int((opts or {}).get("page") or 1)
    rows = []
    for qi, q in enumerate(queries):
        for i in range(4):
            tag = i if pg <= 1 else 10 + i
            rows.append({"url": "https://a.example/%d-%d.png" % (pg if pg > 1 else qi, tag),
                         "thumb": "", "w": S, "h": S, "title": "Blue Flower %d" % i,
                         "page": "https://a.example/p/%d-%d" % (qi, i),
                         "site": "a.example", "provider": "fakea", "query": q})
    return rows


def fake_provider_b(queries, per_query, key, opts):
    raise RuntimeError("网关 500 了")


def fake_provider_c(queries, per_query, key, opts):
    """只有站点的网关：图和 a 站一模一样（用来测跨站内容去重）。

    第 2 页同样换一批（编号 12/13 会和 a 站第 2 页的撞上 —— 那正好继续
    测"跨站同一张图只留一份"）。
    """
    pg = int((opts or {}).get("page") or 1)
    rows = []
    for qi, q in enumerate(queries):
        for i in range(2):
            tag = i if pg <= 1 else 12 + i
            rows.append({"url": "https://c.example/%d-%d.png" % (pg if pg > 1 else qi, tag),
                         "thumb": "", "w": S, "h": S, "title": "copy %d" % i,
                         "page": "https://c.example/p/%d" % i,
                         "site": "c.example", "provider": "fakec", "query": q})
    return rows


providers.REGISTRY["fakea"] = {"label": "假站A", "kind": "search", "fn": fake_provider_a,
                               "home": "http://a.example", "env": "WSEARCH_FAKEA",
                               "note": "测试用"}
providers.REGISTRY["fakeb"] = {"label": "假站B", "kind": "search", "fn": fake_provider_b,
                               "home": "http://b.example", "env": "WSEARCH_FAKEB",
                               "note": "测试用（永远失败）"}
providers.REGISTRY["fakec"] = {"label": "假站C", "kind": "stock", "fn": fake_provider_c,
                               "home": "http://c.example", "env": "WSEARCH_FAKEC",
                               "note": "测试用"}
ENV = {"WSEARCH_FAKEA": "k1", "WSEARCH_FAKEB": "k2", "WSEARCH_FAKEC": "k3"}

ok(set(providers.available(ENV, only=["fakea", "fakeb", "fakec"])) ==
   {"fakea", "fakeb", "fakec"}, "配了 key 的来源都被认出来")
ok(providers.available({}, only=["fakea"]) == [], "没配 key 的来源不会被用")
try:
    providers.search(["q"], providers=["nope"], env=ENV)
    ok(False, "不认识的名字该报错")
except Exception as e:                                                     # noqa: BLE001
    ok("没有可用的搜索来源" in str(e), "没有可用来源时给出人话（不抛技术异常）")

rows, errs = providers.search(["测试词"], providers=["fakea", "fakeb", "fakec"],
                             env=ENV, per_query=10)
ok(len(rows) == 4 + 2, "两家的结果都收齐了（A 4 条 + C 2 条 = %d）" % len(rows))
ok(len(errs) == 1 and "假站B" in errs[0], "B 站挂掉被记成错误，不影响别家")

# ============================================== [3] 下载 + 平铺闸门（桩网络）
print("\n[3] 下载层：平铺闸门 + 两层去重")
BL = {}


def _blob_for(url):
    """按 URL 造图：a.example 是真平铺，c.example 是"和 a 一样的内容"，
    含 photo 字样的不是平铺。"""
    if "photo" in url:
        return _png(make_photo(1))
    if url.startswith("https://a.example/"):
        k = url.rsplit("/", 1)[-1]
        idx = int(k.split("-")[1].split(".")[0])
        return _png(make_tile(idx))
    if url.startswith("https://c.example/"):
        k = url.rsplit("/", 1)[-1]
        idx = int(k.split("-")[1].split(".")[0])
        return _png(make_tile(idx))            # 与 a 站同 idx 的图**字节完全相同**
    return b"<html>not a image</html>"


def fake_fetch(url, *, timeout=25, max_bytes=None, referer=""):
    if "gone.example" in url:
        raise fetch.net.NetError("下载失败（HTTP 404）")
    blob = _blob_for(url)
    if not blob.startswith(b"\x89PNG"):
        raise fetch.net.NetError("拿到的不是图片")
    return blob, "image/png", url


real_fetch = fetch.net.fetch_bytes
fetch.net.fetch_bytes = fake_fetch
tmp = tempfile.mkdtemp(prefix="wsearch_test_")
try:
    cand = []
    for qi in range(4):
        for i in range(3):
            cand.append({"url": "https://a.example/%d-%d.png" % (qi, i), "thumb": "",
                         "w": S, "h": S, "title": "t%d%d" % (qi, i),
                         "page": "https://a.example/p", "site": "a.example",
                         "provider": "fakea", "query": "q%d" % qi})
            cand.append({"url": "https://c.example/%d-%d.png" % (qi, i), "thumb": "",
                         "w": S, "h": S, "title": "c%d%d" % (qi, i),
                         "page": "https://c.example/p", "site": "c.example",
                         "provider": "fakec", "query": "q%d" % qi})
    cand.append({"url": "https://a.example/photo-1.png", "thumb": "", "w": S, "h": S,
                 "title": "model wearing it", "page": "https://a.example/p",
                 "site": "a.example", "provider": "fakea", "query": "q"})
    cand.append({"url": "https://gone.example/1.png", "thumb": "", "w": S, "h": S,
                 "title": "", "page": "https://gone.example/p", "site": "gone.example",
                 "provider": "fakea", "query": "q"})
    cand.append({"url": "https://a.example/0-0.png", "thumb": "", "w": S, "h": S,
                 "title": "", "page": "https://a.example/p", "site": "a.example",
                 "provider": "fakea", "query": "q"})       # 和上面重复的 URL

    # —— 默认策略：**接缝不筛**（用户 2026-10-18 的要求）——
    kept, fstats = fetch.collect(cand, outdir=tmp, want=50, workers=4, per_host=4)
    ok(fstats["候选"] == len(cand), "候选数记对了（%d）" % fstats["候选"])
    ok(fstats["留下"] == len(kept), "统计里留下的张数和实际一致")
    ok(any(not k["tile"]["seamless"] for k in kept),
       "**默认不筛接缝**：铺不开的图也留下了（这张 photo 就是）")
    ok(fstats["判定不通过"] == 0, "默认不筛，所以「被接缝筛掉」是 0（%d）"
       % fstats["判定不通过"])
    ok(fstats["接缝不符但收了"] >= 1,
       "但**如实记账**：接缝不符却收下 %d 张" % fstats["接缝不符但收了"])
    ok(fstats["下载失败"] >= 1, "下载失败单独计数（%d）" % fstats["下载失败"])
    ok(fstats["内容重复"] >= 1,
       "跨站同一张图被 md5 去重（%d）" % fstats["内容重复"])
    ok(len({k["md5"] for k in kept}) == len(kept), "留下的 md5 互不相同")
    ok(all(os.path.exists(k["path"]) for k in kept), "留下的图都真写到磁盘上了")
    ok(all(k["path"].endswith(".png") for k in kept), "扩展名按魔法字节认的（.png）")
    ok(fstats["留下"] <= 14, "去重后数量少于原始候选（%d ≤ 14）" % fstats["留下"])

    # —— 老行为还在：--tile-gate 一开，接缝就又当硬门槛 ——
    strict, sstrict = fetch.collect(cand, outdir=tmp, want=50, workers=4,
                                    per_host=4, tile_gate=True)
    ok(all(k["tile"]["seamless"] for k in strict),
       "`tile_gate=True`（--tile-gate）：留下的全部是真平铺")
    ok(sstrict["判定不通过"] >= 1,
       "开关打开时非平铺照样被拦并计数（%d）" % sstrict["判定不通过"])
    ok(len(strict) < len(kept), "严格模式收得比默认少（%d < %d）"
       % (len(strict), len(kept)))

    # want 到了就停：要 1 张时不该下完所有候选
    kept2, s2 = fetch.collect(cand, outdir=tmp, want=1, workers=4)
    ok(s2["留下"] == 1, "want=1 时只留 1 张（%d）" % s2["留下"])
    ok(s2["候选"] > 1 and len(kept2) == 1, "且没有把候选全下完（省配额）")

    # —— 站方裁剪过的来源（tile_opt）：连"接缝"这一步都不该判死它 ——
    opt_row = {"url": "https://a.example/photo-9.png", "thumb": "", "w": S, "h": S,
               "title": "", "page": "https://a.example/p", "site": "a.example",
               "provider": "ypattern", "query": "q", "tile_opt": True}
    kept3, s3 = fetch.collect([opt_row], outdir=tmp, want=1, workers=1,
                              tile_gate=True)
    ok(len(kept3) == 1 and kept3[0]["tile_gate"] is False,
       "tile_opt=True 的非平铺图在**严格模式**下也收，但记 tile_gate=False")
    ok(s3["放宽闸门"] == 1, "统计里单独计「放宽闸门」（%d）" % s3["放宽闸门"])
    no_opt = dict(opt_row)
    no_opt.pop("tile_opt")
    kept4, s4 = fetch.collect([no_opt], outdir=tmp, want=1, workers=1,
                              tile_gate=True)
    ok(kept4 == [] and s4["判定不通过"] == 1,
       "严格模式下同一个 URL 不带 tile_opt 就被拦")
finally:
    fetch.net.fetch_bytes = real_fetch

# ==================================================== [4] 感知去重与排序
print("\n[4] 排序层：感知哈希去重 + 来源配额")
same = []
for i in range(6):
    p = os.path.join(tmp, "same_%d.png" % i)
    make_tile(0).save(p)                      # 六张**几乎一样**的图
    same.append({"path": p, "w": S, "h": S, "tile": tile.analyze(p),
                 "source": {"site": "x.example", "provider": "fakea", "query": "q"},
                 "id": "S%d" % i})
picked, rstats = rank.pick(same, want=6)
ok(len(picked) == 1, "六张几乎一样的图只留一张（留下 %d）" % len(picked))

mix = []
for i in range(10):
    p = os.path.join(tmp, "mix_%d.png" % i)
    make_tile(i).save(p)
    mix.append({"path": p, "w": S, "h": S, "tile": tile.analyze(p),
                "source": {"site": "a.example" if i % 2 else "b.example",
                           "provider": "fakea", "query": "q",
                           "url": "https://%s.example/m%d.png" % (
                               "a" if i % 2 else "b", i),
                           "page": "https://a.example/p/%d" % i},
                "id": "M%d" % i, "score": 0, "hash": None})
distinct = len({make_tile(i).tobytes() for i in range(10)})
picked2, _ = rank.pick(mix, want=10)
ok(distinct == 10, "测试素材本身是 10 张不同的图（%d）" % distinct)
ok(len(picked2) >= 6, "同族但不同的图大部分留下了（%d/10）" % len(picked2))
# 留下来的两两之间必须"看着不是同一张" —— 这才是这层去重的契约。
# （不要求全部 10 张都留：这 10 张本来就是同一种"圆点格子"的变体，
#   把其中最像的几张合并掉是**对的**行为。）
worst = 64
for i in range(len(picked2)):
    for j in range(i + 1, len(picked2)):
        worst = min(worst, abs(rank._hamming(picked2[i].get("hash"),
                                             picked2[j].get("hash"))))
ok(worst >= rank.HASH_NEAR,
   "留下来的两两之间都不算同一张（最小距离 %d ≥ %d）" % (worst, rank.HASH_NEAR))
ok(all(p.get("score", 0) > 0 for p in picked2), "每条都算了分数")
scores = [p["score"] for p in picked2]
ok(scores == sorted(scores, reverse=True), "结果按分数从高到低")

try:
    rank.ahash(os.path.join(tmp, "mix_0.png"))
    a, b = rank.ahash(os.path.join(tmp, "mix_0.png")), rank.ahash(os.path.join(tmp, "mix_0.png"))
    ok(a == b, "同一张图的感知哈希稳定")
    ok(rank.ahash("/tmp/不存在_%d.png" % 1) is None, "哈希算不出来回 None，不抛异常")
except Exception as e:                                                     # noqa: BLE001
    ok(False, "感知哈希不该抛异常", str(e))

ok(rank.clean_name("Blue Floral Seamless Pattern | Etsy | 20% off") == "Blue Floral Se",
   "标题洗干净了（去掉站名/价格），得 %r"
   % rank.clean_name("Blue Floral Seamless Pattern | Etsy | 20% off"))
ok(rank.clean_name("") == "" and rank.clean_name("|  |") == "", "洗不出来就回空，不硬编")

# ===================================================== [5] 落库形状
print("\n[5] 落库：形状要和本地花型库对得上")
libdir = os.path.join(tmp, "lib")
os.makedirs(libdir, exist_ok=True)
take = picked2[:4]
index, paths = store.build(take, libdir, meta={"need": "国风 花卉"})
ok(index["count"] == len(take), "索引条数和喂进去的一致（%d）" % index["count"])
ok(os.path.isdir(os.path.join(libdir, "orig")) and os.path.isdir(os.path.join(libdir, "thumb")),
   "orig/ 和 thumb/ 都建了")
it = index["items"][0]
LIB_FIELDS = ("id", "name", "file", "w", "h", "md5", "source", "source_label",
              "status", "status_label", "added", "updated", "tag_mode", "in_use",
              "style", "element", "color", "scene", "keywords", "desc", "image", "full")
missing = [f for f in LIB_FIELDS if f not in it]
ok(not missing, "库该有的字段都在（缺：%s）" % (missing or "无"))
ok(it["source"] == "web" and it["status"] == "untagged", "来源标成 web、状态是待打标签")
ok(it["tag_mode"] == "web", "标了 tag_mode=web（不假装是模型打出来的标签）")
ok(it["style"] == "" and it["element"] == "", "四维不硬编（真打过标才写）")
ok(it["id"].startswith("PF-WEB-"), "id 用 PF-WEB- 前缀，和库里的 PF-LIB- 不撞")
ok(it["src_url"] and it["src_page"], "原始出处留下来了（将来追溯/避版权要用）")
ok(it["seam"] and it["seam"]["quality"] is not None, "平铺指标写进索引了")
ok(it["image"] and os.path.exists(os.path.join(libdir, it["image"])), "缩略图真存在")
ok(os.path.exists(os.path.join(libdir, it["full"])), "原图真存在")
with Image.open(os.path.join(libdir, it["image"])) as th:
    ok(max(th.size) <= 480, "缩略图长边 ≤480（%s）" % (th.size,))
ok(os.path.exists(paths["index"]), "index.json 写出来了")
ok(os.path.exists(os.path.join(libdir, "manifest.json")), "manifest.json 也写了（可复盘）")
try:
    json.load(open(paths["index"], encoding="utf-8"))
    ok(True, "index.json 是合法 JSON")
except Exception as e:                                                     # noqa: BLE001
    ok(False, "index.json 该是合法 JSON", str(e))
ok(not os.path.exists(os.path.join(libdir, "_raw")), "下载暂存目录已清掉")
# 上面那条只有"确实建过 _raw 又清掉了"才有意义 —— 光看目录不存在会假通过
# （真出过这个 bug：scratch 变量定义了却没传给 fetch.collect，下载碎片全堆在
#  产物目录里，而清理代码一直在一个根本不存在的位置上空转）。
leftovers = sorted(f for f in os.listdir(libdir) if f.startswith("raw_"))
ok(not leftovers, "产物目录里没有下载碎片（%s）" % (leftovers[:3] or "无"))
ok(sorted(os.listdir(libdir)) == ["index.json", "manifest.json", "orig", "thumb"],
   "产物目录里只有该有的四样：%s" % sorted(os.listdir(libdir)))
s = store.summary(libdir)
ok(s["count"] == len(take) and s["平均平铺质量"] > 0, "summary 能读回来（%s 条）" % s["count"])
ok(store.summary(os.path.join(tmp, "nope"))["count"] == 0, "没搜过的目录回 count=0 不报错")

# ============================================== [6] 子进程契约（service）
print("\n[6] 子进程契约：probe / search / summary")
import argparse                                                           # noqa: E402


def ns(**kw):
    base = {"json_out": "", "redact": False, "outdir": "", "need": "", "brief": "",
            "want": 100, "candidates": 0, "provider": "", "per_query": 100,
            "queries": 4, "page": 1, "workers": 4, "per_host": 2, "timeout": 5,
            "vocab": "", "progress_out": "", "llm_base": "", "llm_key": "",
            "llm_model": "", "url": "", "title": "",
            # 语义甄别那组。**默认在这里打开 no_screen**，让没提这事的用例
            # 保持"只过平铺闸门"的老行为 —— 否则跑测试会去连本机的视觉模型，
            # 装了模型的机器和没装的机器跑出两种结果。
            "no_screen": True, "no_rescreen": False, "rounds": 3,
            "vlm_base": "", "vlm_model": "", "vlm_key": "", "vlm_timeout": 0,
            "vlm_max_side": 0, "vlm_batch": 0, "vlm_workers": 1,
            # 风格判据（空 = 没指定工艺，甄别按"只防明显跑题"的宽松版）
            "craft": "", "style_pos": "", "style_neg": ""}
    base.update(kw)
    return argparse.Namespace(**base)


def run_svc(fn):
    """跑一个 service 命令，把 stdout 抓成 JSON 再解析。

    注意是解析**整段**输出，不是最后一行 —— _emit 写的是 indent=1 的多行
    JSON，最后一行只有个 `}`。（踩过：拿 splitlines()[-1] 去 json.loads。）
    """
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        rc = fn()
    finally:
        sys.stdout = old
    return rc, json.loads(buf.getvalue().strip())


_rc, probe = run_svc(lambda: service.cmd_probe(ns()))
ok("providers" in probe and isinstance(probe["providers"], list), "probe 列出了各家来源")
ok(all("configured" in p for p in probe["providers"]), "每家都标了配没配")
ok(any(p.get("keyless") for p in probe["providers"]), "免 key 的来源被标出来了")
_bingrow = [p for p in probe["providers"] if p.get("name") == "bing"]
ok(bool(_bingrow), "probe 也列出了默认不跑的必应（不列出来用户不知道有这一路）")
ok(bool(_bingrow) and _bingrow[0].get("default") is False,
   "必应在 probe 里标了 default=False，界面能把它跟默认在跑的区分开")
raw = json.dumps(probe, ensure_ascii=False)
# 免 key 的来源 env 是空串 —— 而**空串在任何字符串里都找得到**，
# 不排掉的话下面这条断言会永远失败（而且失败得莫名其妙）。
env_keys = [providers.REGISTRY[n]["env"] for n in providers.DEFAULT_ORDER
            if providers.REGISTRY[n].get("env")]
ok(not any(k in raw for k in env_keys), "probe 结果里**没有**环境变量名")
ok("sk-" not in raw, "probe 结果里没有 key 的影子")
_rc2, _p2 = run_svc(lambda: service.cmd_probe(ns(redact=True)))
ok(_rc2 == 0 and "note" in _p2, "probe 能跑通（--redact 路径）")

# 真跑一次 search：全程用假源 + 假网络
providers.DEFAULT_ORDER = ("fakea", "fakec")
os.environ["WSEARCH_FAKEA"] = "k1"
os.environ["WSEARCH_FAKEC"] = "k3"


def _search():
    fetch.net.fetch_bytes = fake_fetch
    try:
        return service.cmd_search(ns(
            need="国风 花卉 连衣裙", outdir=os.path.join(tmp, "svc"),
            want=6, provider="fakea,fakec", queries=3,
            progress_out=os.path.join(tmp, "prog.json")))
    finally:
        fetch.net.fetch_bytes = real_fetch


rc, res = run_svc(_search)
ok(rc == 0 and res["ok"], "search 跑通了（退出码 %s）")
ok(res["count"] >= 1, "搜到并落库了 %d 张" % res["count"])
ok(res["queries"] and len(res["queries"]) >= 2,
   "回复里带了用过的检索词（%d 条）" % len(res["queries"]))
ok(os.path.exists(res["index"]), "index.json 在")
_svcout = os.path.join(tmp, "svc")
# 目录里允许有 `_rejected/` + `rejected.json`（相似去重也会留档），
# 要钉的是**没有下载碎片**（`_raw/`）—— 那才是会越堆越大的东西。
_left = [x for x in sorted(os.listdir(_svcout))
         if x not in ("_rejected", "rejected.json")]
ok(_left == ["index.json", "manifest.json", "orig", "thumb"],
   "service 的产物目录干净、没留下载碎片（%s）" % sorted(os.listdir(_svcout)))
ok("候选" in res["stats"] and "接缝不符(下载时未筛)" in res["stats"]
   and "接缝不符(最终交付)" in res["stats"] and "接缝不符(被筛掉)" in res["stats"],
   "统计把三种接缝口径分开：下载时没筛 / 最终交付里有多少 / 被筛掉多少")
ok(json.load(open(os.path.join(tmp, "prog.json")))["phase"] == "done", "进度文件最后是 done")

_rc3, ss = run_svc(lambda: service.cmd_summary(ns(outdir=os.path.join(tmp, "svc"))))
ok(ss["ok"] and ss["count"] == res["count"], "summary 和 search 的条数一致")


def _search_short():
    """要的张数比第一页能给的多 —— 应该自动翻一页再捞。"""
    fetch.net.fetch_bytes = fake_fetch
    try:
        return service.cmd_search(ns(need="国风 花卉", outdir=os.path.join(tmp, "svc2"),
                                     want=10, provider="fakea,fakec", queries=1,
                                     candidates=60))
    finally:
        fetch.net.fetch_bytes = real_fetch


rc8, res8 = run_svc(_search_short)
ok(rc8 == 0 and res8["ok"], "要的比第一页能给的多时也能跑通")
# 一轮只有 4 + 2 = 6 个候选，要 10 张就必须翻页
ok(res8["stats"]["候选"] >= 10,
   "自动翻页补了候选（两轮合计 %d 个，单轮只有 6 个）" % res8["stats"]["候选"])
# 用**下载层的统计**（留下平铺图）而不是最终条数：最终条数还要过一遍感知
# 去重，而这批合成图里有几对是"几何一样、只换个颜色"的，会被当成近似重复
# 合并掉 —— 那是去重层该有的行为，不该拿来当"翻页有没有生效"的证据。
ok(res8["stats"]["留下平铺图"] >= 8,
   "翻页补进来的图真的通过了平铺闸门（合计 %d 张，单轮只有 4 张）"
   % res8["stats"]["留下平铺图"])


# ============================================== [7] 语义甄别与补齐
print("\n[7] 语义甄别：不合格的剔掉、理由留档、不够的补回来")

_real_vlm = service._vlm


class _FakeVlm:
    """桩视觉端点：按「这是整批里的第几张」判好判坏，顺序可预测。

    为什么要能指定"第几张坏"：光测"有没有调过"没意义。要测的是实事 ——
    被判坏的图**真的没进索引**、被剔的理由**真的留了档**、剔完不够
    **真的会去翻页补**。
    """

    def __init__(self, bad_seq=(), batch=2, alive=True):
        self.bad = set(bad_seq)
        self.batch = batch
        self.alive = alive
        self.n_calls = 0
        self.model = "fake-vlm"
        self.prompts = []            # 方便断言"判据真的传到模型了"

    def available(self):
        return self.alive

    def see(self, prompt, paths, max_tokens=None):
        self.n_calls += 1
        self.prompts.append(prompt)
        out = []
        for i, _p in enumerate(paths, 1):
            seq = (self.n_calls - 1) * self.batch + i
            good = seq not in self.bad
            out.append('{"i":%d,"pattern":%s,"match":true,"why":"桩第%d张"}'
                       % (i, "true" if good else "false", seq))
        return "[" + ",".join(out) + "]"


def _search_screened():
    fetch.net.fetch_bytes = fake_fetch
    service._vlm = lambda args: _FakeVlm(bad_seq=(2, 5), batch=1)
    try:
        return service.cmd_search(ns(
            need="国风 花卉", outdir=os.path.join(tmp, "svc3"), want=4,
            provider="fakea,fakec", queries=3, no_screen=False, vlm_batch=1,
            rounds=2, progress_out=os.path.join(tmp, "prog3.json")))
    finally:
        fetch.net.fetch_bytes = real_fetch
        service._vlm = _real_vlm


rc9, res9 = run_svc(_search_screened)
ok(rc9 == 0 and res9["ok"], "带甄别的 search 跑得通")
ok(res9["screen"]["不是图案"] >= 1,
   "桩判出的「不是图案」被记进结果（%s 张）" % res9["screen"]["不是图案"])
ok(res9["rejected"] == res9["screen"]["不是图案"] + res9["screen"]["不符需求"]
   + res9["stats"].get("相似只留一张", 0),
   "剔除数 = 不是图案 + 不符需求 + 相似只留一张")
ok(res9["stats"]["甄别剔除"] >= 1, "统计里报出了甄别剔除数")
svc3 = os.path.join(tmp, "svc3")
ok(os.path.exists(os.path.join(svc3, "rejected.json")),
   "被剔的理由留了档（rejected.json）")
ok(os.path.isdir(os.path.join(svc3, "_rejected")), "被剔的图也留了缩略图（_rejected/）")
rejd = json.load(open(os.path.join(svc3, "rejected.json"), encoding="utf-8"))
ok(rejd["count"] == res9["rejected"] and rejd["items"], "留档条数跟结果对得上")
ok(all(x["thumb"] for x in rejd["items"]),
   "留档的缩略图**真的复制出来了**（先删再留档就只剩空壳 —— 踩过）")
ok(all(x["why"] for x in rejd["items"]), "每条都带理由，用户能核对机器判得对不对")
idx9 = json.load(open(os.path.join(svc3, "index.json"), encoding="utf-8"))
ok(all((it.get("screen") or {}).get("pattern") for it in idx9["items"]),
   "索引里每张都是甄别通过的（被判不合格的没混进来）")
ok(all((it.get("screen") or {}).get("why") for it in idx9["items"]),
   "索引里留了判词，事后能核对")
ok(json.load(open(os.path.join(tmp, "prog3.json")))["phase"] == "done",
   "进度文件最后是 done")
ok(res9["count"] >= 1, "剔完之后还有图交出来（%d 张）" % res9["count"])

# 端点没起来时要如实说、并且**不剔除任何图**
service._vlm = lambda args: _FakeVlm(alive=False)


def _search_novlm():
    fetch.net.fetch_bytes = fake_fetch
    try:
        return service.cmd_search(ns(
            need="国风 花卉", outdir=os.path.join(tmp, "svc5"), want=3,
            provider="fakea,fakec", queries=3, no_screen=False, vlm_batch=2))
    finally:
        fetch.net.fetch_bytes = real_fetch
        service._vlm = _real_vlm


rc12, res12 = run_svc(_search_novlm)
ok(rc12 == 0 and res12["ok"], "视觉模型没起来时 search 照跑（只是少一道把关）")
ok(res12["stats"]["甄别剔除"] == 0, "没模型就不剔除任何图")
ok("视觉模型没起来" in (res12["screen"]["note"] or ""),
   "结果里如实写明这一层没跑", res12["screen"]["note"])
ok(res12["count"] >= 1, "照样交图")

# ---- refill：用户删掉不满意的，再补齐 ----
service._vlm = _real_vlm


def _search_plain():
    fetch.net.fetch_bytes = fake_fetch
    try:
        return service.cmd_search(ns(
            need="国风 花卉", outdir=os.path.join(tmp, "svc4"), want=5,
            provider="fakea,fakec", queries=3))
    finally:
        fetch.net.fetch_bytes = real_fetch


rc10, res10 = run_svc(_search_plain)
svc4 = os.path.join(tmp, "svc4")
idx10 = json.load(open(os.path.join(svc4, "index.json"), encoding="utf-8"))
ids_before = [it["id"] for it in idx10["items"]]
n_before = len(ids_before)
ok(n_before >= 2, "先落一批（%d 张）" % n_before)

victim = idx10["items"][-1]


def _refill():
    # 两边都**先删文件再补**：模拟用户自己在 Finder 里把不满意的图扔了
    for key in ("file", "thumb"):
        try:
            os.remove(os.path.join(svc4, victim[key]))
        except OSError:
            pass
    fetch.net.fetch_bytes = fake_fetch
    # 桩顺手判掉第一张 —— 一次把"用户删的"和"甄别剔的"两条路径都走到
    service._vlm = lambda args: _FakeVlm(bad_seq=(1,), batch=2)
    try:
        return service.cmd_refill(ns(need="国风 花卉", outdir=svc4, want=n_before,
                                     provider="fakea,fakec", queries=3,
                                     no_screen=False, vlm_batch=2, rounds=2))
    finally:
        fetch.net.fetch_bytes = real_fetch
        service._vlm = _real_vlm


rc11, res11 = run_svc(_refill)
ok(rc11 == 0 and res11["ok"], "refill 跑得通")
idx11 = json.load(open(os.path.join(svc4, "index.json"), encoding="utf-8"))
ids_after = [it["id"] for it in idx11["items"]]
ok(victim["id"] not in ids_after, "用户删掉的那条从索引里消失了")
killed = ids_before[0]                     # 桩按批次序判掉的第一张
ok(killed not in ids_after, "被甄别剔掉的那张也从索引里消失了")
ok(res11["removed"] == 1, "结果里报了甄别剔掉 1 张（%s）" % res11["removed"])
ok(all(i in ids_after for i in ids_before if i not in (victim["id"], killed)),
   "**活下来的图编号一个都没变**（用户手上引用过那些编号）", str(ids_after))
ok(idx11["count"] == len(ids_after), "count 跟 items 对得上")
ok(res11["count"] == len(ids_after),
   "结果里的 count 等于索引实际条数（踩过：先数后删 → 报 100 实际只有 85）")
ok(len(ids_after) >= max(1, n_before - 2), "refill 不会把总数搞少（%d → %d）"
   % (n_before, len(ids_after)))
ok(res11["have"] == n_before - 2,
   "「补之前还剩几张」是**删完之后**数的：%d（原本 %d：用户删 1、甄别剔 1）"
   % (res11["have"], n_before))
# 老条目是上一版落的，那时还没有甄别这一步 → 索引里 screen 是空的。
# refill 甄别完必须把判词补写回去，否则那张图永远没有"机器凭什么判它合格"
# 的记录（真机上踩过：`每条都有判词? False`，100 张里 22 张是空白）。
ok(all((it.get("screen") or {}).get("why") for it in idx11["items"]),
   "**老条目的判词也补写回索引了**（上一版落的图不补写就永远是空白）",
   str([it["id"] for it in idx11["items"] if not (it.get("screen") or {}).get("why")]))
rej_file = os.path.join(svc4, "rejected.json")
if os.path.exists(rej_file):
    rj = json.load(open(rej_file, encoding="utf-8"))
    ok(rj["count"] == res11["rejected"], "补图时新剔的也追加进同一份留档（%d 条）"
       % rj["count"])
    ok(all(x["thumb"] for x in rj["items"]), "追加的留档也有缩略图，不是空壳")

# 只补不甄别（--no-rescreen）：现有图一张都不该被动
service._vlm = _real_vlm
rc13, res13 = run_svc(lambda: service.cmd_refill(ns(
    need="国风 花卉", outdir=svc4, want=len(ids_after), no_rescreen=True,
    provider="fakea,fakec", queries=3, no_screen=True)))
idx13 = json.load(open(os.path.join(svc4, "index.json"), encoding="utf-8"))
ok(rc13 == 0 and res13["ok"], "--no-rescreen 路径跑得通")
ok([it["id"] for it in idx13["items"]] == ids_after,
   "已经够数时一张都不动（--no-rescreen 不重新甄别）")


# ---- 指定了工艺/风格：判据必须一路传到模型，还要落进索引 ----
# 真机上第一次跑「国风扎染」时，需求分析根本没跑（engine=template，检索词只有
# "原话 + 无缝平铺"），模型只能按题材判 —— 100 张里一多半是"颜色像、做法不对"
# 的蓝印花布/佩斯利/曼陀罗。这里钉住：工艺判据真的进了提示词、也真的记进了索引。
def _search_styled():
    fetch.net.fetch_bytes = fake_fetch

    def mk(_args):
        holder["v"] = _FakeVlm(alive=True, batch=3)
        return holder["v"]

    service._vlm = mk
    try:
        return service.cmd_search(ns(
            need="国风 扎染 风格的印花图案", outdir=os.path.join(tmp, "svc5"),
            want=2, provider="fakea,fakec", queries=2, no_screen=False,
            vlm_batch=3, rounds=0, craft="扎染",
            style_pos="不规则晕开的同心圈纹,颜色互相渗开的柔和软边",
            style_neg="佩斯利腰果花,曼陀罗对称放射"))
    finally:
        fetch.net.fetch_bytes = real_fetch
        service._vlm = _real_vlm


holder = {}
rc14, res14 = run_svc(_search_styled)
ok(rc14 == 0 and res14["ok"], "带工艺判据的 search 跑得通")
_p5 = holder["v"].prompts[0]
ok("是不是「扎染」这种做法" in _p5, "工艺进了提示词：判的是做法，不是题材")
ok("佩斯利腰果花" in _p5 and "命中任何一条就判 false" in _p5,
   "反例以硬规则的形式进了提示词（先排除再确认）")
ok("拿不准就判 true" not in _p5,
   "严判时不能再给「拿不准就判 true」这个免死金牌")
idx5 = json.load(open(os.path.join(tmp, "svc5", "index.json"), encoding="utf-8"))
ok((idx5.get("need") or {}).get("craft") == "扎染",
   "工艺判据记进了索引（下次 refill/screen 直接用，不用重抽）")
ok((idx5.get("need") or {}).get("style_neg") == ["佩斯利腰果花", "曼陀罗对称放射"],
   "反例也记进了索引", str((idx5.get("need") or {}).get("style_neg")))


def _one(url, sub):
    fetch.net.fetch_bytes = fake_fetch
    try:
        return service.cmd_one(ns(url=url, outdir=os.path.join(tmp, sub)))
    finally:
        fetch.net.fetch_bytes = real_fetch


_rc4, one = run_svc(lambda: _one("https://a.example/0-0.png", "one"))
ok(one["ok"] and one["seamless"], "one 判一张网图：平铺（%s）" % one.get("note"))

_rc5, one2 = run_svc(lambda: _one("https://a.example/photo-1.png", "one2"))
ok(not one2["ok"] and "铺开" in one2.get("error", ""),
   "one 判非平铺图：如实说不行的原因（%s）" % one2.get("error"))

# 参数错误也必须是结构化 JSON，不能抛
rc6, e6 = run_svc(lambda: service.cmd_search(ns(need="", outdir=os.path.join(tmp, "empty"))))
ok(rc6 == 0 and e6["ok"] is False and e6["error"], "没给需求时回结构化错误（%s）" % e6["error"])
ok(e6.get("stage") == "input", "错误带了 stage，便于定位")

# 没有可用来源时的错误必须是给人看的
saved = dict(os.environ)
for n in providers.DEFAULT_ORDER:
    os.environ.pop(providers.REGISTRY[n]["env"], None)
rc7, e7 = run_svc(lambda: service.cmd_search(ns(need="任意", outdir=os.path.join(tmp, "no"))))
ok(e7["ok"] is False and "搜索" in e7["error"] and "README" in e7["error"],
   "一把 key 都没有时，错误话术直接指到 README（%s…）" % e7["error"][:40])
os.environ.update(saved)

# ======================================================== [7] config.json
# 这个文件是给用户手改的，所以**手滑的写法必须兜得住**：
# 缺字段、写 "false" 字符串、JSON 语法错、正则写错 —— 一个都不许把整轮搜索搞崩。
print("\n[7] 配置文件：模型 / 爬虫目标 / 筛选项")

_cdir = tempfile.mkdtemp(prefix="wsearch_cfg_")


def _cfg(name, obj):
    p = os.path.join(_cdir, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False) if isinstance(obj, dict) \
            else f.write(obj)
    return p


# ① 没有文件 = 全默认（不能因为找不到文件就炸）
_d = config.load(file_path=os.path.join(_cdir, "nope.json"))
ok(_d["filters"]["tile_gate"] is False and _d["filters"]["screen"] is True
   and _d["filters"]["similar_threshold"] == 0.995,
   "没有配置文件时用默认值：接缝不筛、甄别开、阈值 0.995")
ok(_d["ai"]["api_key_env"] == "WSEARCH_LLM_KEY", "模型那段也有默认值（指环境变量）")

# ② 只写想改的字段，别的照默认
_p1 = _cfg("a.json", {"filters": {"tile_gate": True}})
_f1 = config.filters(file_path=_p1)
ok(_f1["tile_gate"] is True and _f1["screen"] is True,
   "只写一行也能用：tile_gate 改了，其它字段仍是默认")

# ③ 手滑写成字符串 "false" —— 必须按布尔认，不能当"有值 = 真"
_p2 = _cfg("b.json", {"filters": {"screen": "false", "drop_flat": "0"}})
_f2 = config.filters(file_path=_p2)
ok(_f2["screen"] is False and _f2["drop_flat"] is False,
   '写成字符串 "false"/"0" 也认成关（手写的 JSON 很容易这样）')

# ④ 阈值写成字符串 / 乱写 —— 不许抛
_p3 = _cfg("c.json", {"filters": {"similar_threshold": "0.98", "min_side": "800"}})
_f3 = config.filters(file_path=_p3)
ok(abs(_f3["similar_threshold"] - 0.98) < 1e-9 and _f3["min_side"] == 800,
   "数字写成字符串也能认（0.98 / 800）")
_p4 = _cfg("d.json", {"filters": {"similar_threshold": "很严"}})
ok(abs(config.filters(file_path=_p4)["similar_threshold"] - 0.995) < 1e-9,
   "数字写不成数字就退回默认，不抛异常")

# ⑤ JSON 语法错 —— 只警告，回默认（这是用户最常见的失误）
_p5 = _cfg("broken.json", '{"filters": {"screen": false,},}')
_b5 = config.load(file_path=_p5)
ok(_b5["filters"]["screen"] is True, "JSON 写坏了只当没有配置，不崩")

# ⑥ 模型：文件里的 key 能用；环境变量优先于文件（临时换 key 不用改文件）
_p6 = _cfg("e.json", {"ai": {"base_url": "https://api.example.com/v1",
                             "api_key": "sk-from-file", "model": "m1"}})
_a6 = config.ai(file_path=_p6)
ok(_a6["base"] == "https://api.example.com/v1" and _a6["key"] == "sk-from-file",
   "ai 段读得到 base/key/model")
_saved_key = os.environ.get("WSEARCH_LLM_KEY")
os.environ["WSEARCH_LLM_KEY"] = "sk-from-env"
try:
    ok(config.ai(file_path=_p6)["key"] == "sk-from-env",
       "**环境变量优先于文件**（临时换 key 不用改文件）")
finally:
    if _saved_key is None:
        os.environ.pop("WSEARCH_LLM_KEY", None)
    else:
        os.environ["WSEARCH_LLM_KEY"] = _saved_key
ok(config.vision(file_path=_p6)["model"] == "m1",
   "视觉模型没单独配就复用对话模型那套（同一家服务）")

# ⑦ 关掉某几家：disabled 一写，enabled()/available() 立刻不认它
_real_crawlers = providers._config.crawlers
providers._config.crawlers = lambda *a, **kw: {"disabled": ["baidu", "so360"],
                                               "custom": []}
try:
    providers.load_custom()
    ok(providers.enabled("baidu") is False and "baidu" not in providers.available(),
       "config 里 disabled 的站不再参与（enabled/available 都认）")
    ok(providers.enabled("huaban") is True, "没点名的站照旧能用")
finally:
    providers._config.crawlers = _real_crawlers
    providers.load_custom()
ok(providers.enabled("baidu") is True, "恢复配置后 baidu 又回来了（load_custom 幂等）")

# ⑧ 加站：配置里写正则就能抓，不用改代码
_GEN_HTML = """<ul>
<li class="card"><a href="/p/1"><img src="//cdn.example.com/1.jpg" /></a>
  <h3>法式 小碎花 一</h3></li>
<li class="card"><a href="/p/2"><img src="/img/2.jpg" /></a>
  <h3>法式 小碎花 二</h3></li>
<li class="nocard">没有图</li></ul>"""
_SPEC = {"name": "demo_site", "label": "示例站",
         "list": "https://demo.example.com/search?q={q}&page={page}",
         "item": r'<li class="card">(.*?)</li>', "img": r'<img[^>]+src="([^"]+)"',
         "title": r"<h3>(.*?)</h3>", "pages": 1, "per_page": 10,
         "referer": "https://demo.example.com/"}
_gen_calls = []
_real_req3 = providers.net.request


def _fake_gen(url, **kw):
    _gen_calls.append((url, kw))
    return 200, {}, _GEN_HTML.encode("utf-8")


providers.net.request = _fake_gen
try:
    _gr = providers._generic(_SPEC)(["水墨 花卉"], 10, "", {})
finally:
    providers.net.request = _real_req3
ok(len(_gr) == 2, "自建站按正则抓到 2 条（实际 %d）" % len(_gr))
ok(_gr[0]["url"] == "https://cdn.example.com/1.jpg",
   "协议相对的图链补成 https：%s" % _gr[0]["url"])
ok(_gr[1]["url"] == "https://demo.example.com/img/2.jpg",
   "站内相对路径按 list 的域名补全：%s" % _gr[1]["url"])
ok(_gr[0]["title"] == "法式 小碎花 一", "标题按 title 正则取")
ok(_gr[0]["provider"] == "demo_site" and _gr[0]["site"] == "demo.example.com",
   "来源名与站点自动填好（排序/配额要用）")
ok("q=%E6%B0%B4%E5%A2%A8" in _gen_calls[0][0] and "page=1" in _gen_calls[0][0],
   "URL 模板里的 {q}/{page} 都替换了：%s" % _gen_calls[0][0])

# 正则写错：要说清是哪一家，别让整轮搜索挂掉
try:
    providers._generic({"name": "bad_site", "list": "https://x/", "img": "([unclosed"})(
        ["q"], 5, "", {})
    _bad_ok = False
except Exception as e:                                       # noqa: BLE001
    _bad_ok = "bad_site" in str(e)
ok(_bad_ok, "正则写错时错误信息点名是哪家站（用户才知道去改哪一行）")

# ⑨ 模板条目（enabled:false）不许注册成真站点
providers._config.crawlers = lambda *a, **kw: {
    "disabled": [], "custom": [dict(_SPEC, enabled=False, name="tmpl_only")]}
try:
    providers.load_custom()
    ok("tmpl_only" not in providers.REGISTRY,
       '`"enabled": false` 的模板条目只当模板，不会被当成真站点去抓')
finally:
    providers._config.crawlers = _real_crawlers
    providers.load_custom()

# ⑩ 自建站注册后应立即出现在默认清单里（否则用户加了站却不跑，等于白加）
providers._config.crawlers = lambda *a, **kw: {"disabled": [], "custom": [_SPEC]}
try:
    providers.load_custom()
    ok("demo_site" in providers.REGISTRY and "demo_site" in providers.DEFAULT_ORDER,
       "配置里加的站自动进 REGISTRY 和默认清单")
finally:
    providers._config.crawlers = _real_crawlers
    providers.load_custom()
    providers.REGISTRY.pop("demo_site", None)
    providers.DEFAULT_ORDER = tuple(n for n in providers.DEFAULT_ORDER
                                    if n != "demo_site")
shutil.rmtree(_cdir, ignore_errors=True)

shutil.rmtree(tmp, ignore_errors=True)

print("\n结果：%d 通过 / %d 失败" % (ok_n, bad_n))
sys.exit(1 if bad_n else 0)
