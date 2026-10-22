"""搜索适配器：把"一段检索词"变成"一堆图片直链"。

为什么要适配器
    图片搜索**没有官方免费入口**。Google 自家没有公开的图片搜索 API，
    Bing Image Search API 已经下线，所以只能走第三方代理或图库站自己的
    API。每一家的请求形状/返回结构都不一样，而且**用户手里有哪把 key 是
    未知数** —— 所以这里把它们收成一个注册表：

        加一家新网关 = 加一个函数 + 在 REGISTRY 里添一行，别处不用动。

三类来源（都留着，互补）
    · free（**不需要 key 的公开接口**：百度图片 / 360 图片 / 必应图片 /
      花瓣 / 堆糖）
      优点：**零成本、零申请、开箱即用**。而且中文花型素材（花瓣、千图、
            站酷、图行天下、素材站…）这几家搜得比 Google 还全 ——
            花型这行本来就是国内素材站最丰富。**默认首选**。
      缺点：**没有承诺**，随时可能改接口或反爬（搜狗就是这么没的：
            它的 napi 现在要签名参数，带了 cookie 也回 "forbid"）。
            所以它们是"默认跑、单家挂掉只记一条错误继续"的一路，
            不是唯一一路。
      五家各有各的脾气，坑都在各自的函数注释里写了（百度要先拿 cookie；
            360 的 pn 是"滚动位置"不是页号；必应要解 HTML 且 mkt 得跟
            着词的语言走；花瓣的直链带时效；堆糖只认中文词）。

    · search（境外图片搜索代理：Serper / SerpAPI / Brave）
      优点：真的"全网"，能捞到 Pinterest、Etsy、英文素材站。
      缺点：要申请 key；结果里混着"穿着这件花裙子的模特照""色卡"
            "包装盒"，全靠 tile.py 的平铺判定筛掉。

    · stock（图库站官方 API：Pexels / Pixabay / Unsplash）
      优点：直出原图直链、结构干净、免费额度大、不用猜图床防盗链。
      缺点：**是图库不是全网**，而且绝大多数是**摄影作品**（一朵花、
            一块布的特写），真正的"可平铺单元"占比很低。

    ⚠️ 默认排序偏向 free/search 类，stock 类当补充（见 rank.KIND_W）。
       想只跑某一家，用 --provider 指定。

一个必须知道的现实
    Google Images 的结果里，`imageUrl` 指向的图**经常不是**那个"平铺单元"，
    而是它在某个页面里的展示图（带边距、带阴影、拼在布料上）。用户明确
    说了"不管水印和清晰度"，但"必须是真正的平铺图"是硬要求 —— 所以
    平铺判定（tile.py）不是可选优化，是这条产线的**主过滤器**。
"""
from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.parse

from . import net
from . import scrape
from . import config as _config
from . import sources as _sources

# 各家的环境变量名。主程序注入时也用这些名字（见 service.py 顶部）。
# **免 key 的来源这里写空串** —— 它们没有 key 这回事，但字段要齐
# （run.py 的 keys 命令会把这一列打出来，probe 也会逐条列）。
ENV = {
    "baidu": "",
    "so360": "",
    "bing": "",
    "huaban": "",
    "duitang": "",
    "walan": "",
    "dancecolor": "",
    "tnc": "",
    "patterndesigns": "",
    "ypattern": "",
    "patternbulk": "",
    "patternbank": "",
    "spoonflower": "",
    "clip_studio": "",
    "site_search": "WSEARCH_SERPER_KEY",
    "serper": "WSEARCH_SERPER_KEY",
    "serpapi": "WSEARCH_SERPAPI_KEY",
    "brave": "WSEARCH_BRAVE_KEY",
    "pexels": "WSEARCH_PEXELS_KEY",
    "pixabay": "WSEARCH_PIXABAY_KEY",
    "unsplash": "WSEARCH_UNSPLASH_KEY",
}


def _domain(url):
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except Exception:                                        # noqa: BLE001
        return ""


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _cjk(s):
    """这条检索词里有没有汉字。

    两个用途（都是实测逼出来的，不是拍脑袋）：
      · 必应的 `mkt` 必须跟着词的语言走 —— 英文词配 mkt=zh-CN 只回 1 条；
      · 花瓣 / 堆糖是中文素材站，英文词上去基本搜不到东西，不如直接跳过，
        省掉一次请求（一轮十来条词，省下来的是真时间）。
    """
    return any("一" <= ch <= "鿿" for ch in (s or ""))


# 中文素材站（花瓣 / 堆糖）截词时要优先保留的"行业词"。
# 留着它们的理由：这两家截到两个词才有结果，而"平铺/印花/四方连续"这类词
# 决定了回来的是不是**平铺素材** —— 只留题材词（"法式小碎花"）会捞回一堆
# 成品照和插画，白白占掉候选名额。
_SEAM_HINT = ("平铺", "无缝", "四方连续", "印花", "图案", "素材", "重复",
              "花型", "面料", "连续", "底纹", "壁纸")


def _trim_for_zh(q):
    """给中文素材站用的检索词：**长词一律截短**，回一串备选（从具体到宽）。

    为什么必须截：这两家是把检索词按空格拆开做**与**匹配的，词一多就一条
    都不给。实测"法式小碎花 平铺 无缝图案 水彩手绘 粉彩花卉 四方连续 印花
    素材"→ 0 条；截到 5 个词 → 0；截到 3 个词 → 堆糖有、花瓣还是 0；
    **截到 2 个词 → 两家都有**。而 analyze 生成的词本来是给搜索引擎用的
    （越长越准），直接喂给这两家等于白跑一趟。

    截法：留**第一个词（题材）+ 第一个行业词**；没有行业词就退回前两个词。
    再往后还搜不到，就只用第一个词（最宽的一档）。
    """
    toks = [t for t in (q or "").split() if t]
    if len(toks) <= 2:
        return [" ".join(toks)] if toks else []
    head = toks[0]
    hint = next((t for t in toks[1:] if any(h in t for h in _SEAM_HINT)), toks[1])
    out = ["%s %s" % (head, hint)]
    if head != out[0] and head not in out:
        out.append(head)
    return out


def _hb_urls(url):
    """花瓣的图直链**换个域名就能下**，回 (主, 备)。

    实测（同一批 20 条、同一个 key 和 query）：
        `gd-hbimg-edge.huaban.com`（API 给的）  →  **0/20**，全 HTTP 567
        `gd-hbimg.huaban.com`                   →  20/20（**2026-10 之后变 567**）
        `hbimg.huaban.com`                      →  20/20（**带不带 auth_key 都行**）

    567 不是标准状态码。`gd-hbimg` 现在连"带 auth_key"都回
    `567 auth failed`（换了参数、换了 UA 都一样）—— 说明它那一支的校验
    变了，别指望它。**老域名 `hbimg` 反而不校验 auth_key**，带不带都 200，
    所以主链换成它，并且**把 query 去掉**（少个变量，也省得 key 过期）。

    备用留 API 原样那条 edge 直链：它现在基本下不动，但万一哪天
    `hbimg` 也挂了，它就是唯一退路 —— 别因为"现在没用"就删掉。
    fetch 在直链下不动时会自动退用 thumb，正好当第二个域名用。
    """
    try:
        s = urllib.parse.urlsplit(url or "")
    except Exception:                                        # noqa: BLE001
        return url or "", ""
    host = (s.hostname or "").lower()
    if not host.endswith("huaban.com") or not s.path:
        return url or "", ""
    tail = s.path.lstrip("/")
    prim = "https://hbimg.huaban.com/%s" % tail
    alt = url if url.startswith("http") and url != prim else ""
    return prim, alt


def _with_retry(fn, *, tries=2, delay=1.5, what=""):
    """跑一次外部接口，失败就再来一次（最多 tries 次）。

    为什么要有这个：免 key 的百度和 360 都是**没有承诺**的公开接口 ——
    同一分钟内连打几次，它们就会当爬虫挡回来。实测：连探几轮之后，百度
    会回一页 HTML 而不是 JSON（`接口返回的不是 JSON`）。这不是"接口坏了"，
    等一会儿再来一次就好了。所以：

      · 只重试一次，间隔 1.5 秒 —— 真挂了也只是多等 1.5 秒，
        不会把"这家挂了"拖成"整条产线卡住"；
      · **按检索词重试**（不是整家重试）：一个词被挡不影响别的词；
      · 两次都失败才把错误抛出去，让 search() 记成该来源的一条错误。
    """
    last = None
    for i in range(max(1, tries)):
        try:
            return fn()
        except Exception as e:                               # noqa: BLE001
            last = e
            if i + 1 < tries:
                time.sleep(delay)
    raise last


def _strip_tags(s):
    """剥掉标题里的 <em> 高亮标签和实体。

    百度返回的 `fromPageTitleEnc` 长这样：`温柔浪漫<em>小碎花</em>平铺壁纸`
    —— 不剥的话花型名里会带着尖括号进索引。
    """
    if not s:
        return ""
    out, depth = [], 0
    for ch in str(s):
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out).replace("&nbsp;", " ").replace("&amp;", "&").strip()


def _item(url, thumb="", w=0, h=0, title="", page="", provider="", query="",
          site=None, ref="", tile_opt=False, created_at="", author="",
          domain="", kind="", quality=None, license="", engagement=None,
          method=""):
    """统一的结果形状。后面所有环节（下载/平铺判定/排序/落库）只认这几个键。

    site 可以显式给：百度/360 的图一律走它们自家的 CDN，按图直链取域名
    会把所有图算成同一个站，rank 的"单站配额"会误伤一大片 —— 所以这两家
    都显式传来源站。

    ref 也是显式给：下载时用的 Referer，默认用 page（来源页）。
    **个别站不吃来源页、只认站点根**（花瓣就是这样：带具体页 referer 回 567，
    带 `https://huaban.com/` 才 200），那种情况用 ref 覆盖掉。

    tile_opt 是**给"预览图被站方裁过、但题材对得上"的来源**开的口子
    （目前只有源创花样网）：置 True 后 fetch 层不会因为它铺不开就丢掉，
    索引里会标 `tile_gate=off`，跟"真能铺"的那批分开。默认 False ——
    **别顺手给别家打开**，那等于把平铺闸门废了。

    溯源字段（2026-10-19 新增）
        created_at  原站发布时间（能拿就拿，拿不到留空，别编）
        author      原站作者/设计师（Behance/Etsy/Clip Studio 能拿到）
        domain      原站域名（不是图床域名）
        kind        sources.py 里的来源类型（marketplace/portfolio/brand/...）
        quality     来源质量先验 0~1（不给就按 sources.prior 取）
        license     授权提示（unknown/purchase_required/...）
        engagement  商家侧互动信号（销量/收藏等），没有就 None
        method      入库方式/渠道（upload/save/feed/...），能拿就拿
    """
    dom = (domain or _domain(page or url) or "").lower()
    # 上传图/无外链的图：has_source=False 表示"只能当灵感，不能当可授权素材"
    has_source = bool(page) and bool(dom) and dom not in ("uploaded by user",)
    if quality is None:
        try:
            quality = _sources.prior(provider)
        except Exception:                                    # noqa: BLE001
            quality = 0.5
    if not kind:
        try:
            kind = _sources.kind(provider)
        except Exception:                                    # noqa: BLE001
            kind = ""
    return {
        "url": url or "",
        "thumb": thumb or "",
        "w": _int(w), "h": _int(h),
        "title": _strip_tags(title)[:120],
        "page": page or "",
        "ref": ref or "",
        "site": (site or dom or _domain(url)),
        "provider": provider,
        "query": query,
        "tile_opt": bool(tile_opt),
        # ---- 溯源/质量 ----
        "created_at": str(created_at or ""),
        "author": str(author or ""),
        "domain": dom,
        "kind": kind,
        "quality_prior": float(quality),
        "license": str(license or "unknown"),
        "engagement": engagement,
        "method": str(method or ""),
        "has_source": has_source,
    }


def _clean(rows):
    """丢掉没直链的、直链明显不是图的（有些网关会塞 html 落地页进来）。"""
    out = []
    for r in rows:
        u = r.get("url") or ""
        if not u.startswith("http"):
            continue
        low = u.split("?")[0].lower()
        if low.endswith((".html", ".htm", ".php", ".asp", ".aspx")):
            continue
        out.append(r)
    return out


# ----------------------------------------------------------------- free（免 key）
def _baidu(queries, per_query, key, opts):
    """百度图片 —— **不需要 key**，中文花型素材最全的一家。

    实测要点（都是打出来的，不是从文档抄的）：
      · 直接用裸请求打 `image.baidu.com/search/acjson`，它会回
        `{"antiFlag": 1, "message": "Forbid spider access"}`，一个字的结果
        都没有。**必须先去一次站点首页把 BAIDUID 收进 cookie jar**，
        那一步在 net.session() 里做（进程内只做一次，见 net.Session.warm）。
      · `rn` **最多 60**（传 100 也只给 60），翻页用 `pn`（真正的偏移量）。
        一次请求 60 条，比 Serper 还大方，所以默认整条产线都在用它。
      · **原图直链在 `replaceUrl[0].ObjURL`**。注意两件事：
          ① `replaceUrl` 是个 **list**，不是字符串（踩过，取出来是空）；
          ② 顶层那个 `objURL` 字段是**加密串**（`ippr_z2C$qAzdH3F...`），
             直接用会拿到一堆 404 —— 别贪方便。
        `replaceUrl[0].FromURL` 才是"这张图来自哪个页面"。
      · ObjURL 走 `gimg2.baidu.com` 代理，**不怕原站的防盗链**；
        `thumbURL`（img0.baidu.com）几乎每条都有，当兜底。
    """
    rows = []
    rn = min(60, max(10, int(per_query or 60)))
    pn = max(0, (max(1, int(opts.get("page") or 1)) - 1) * rn)
    sess = net.session("https://image.baidu.com/")
    rows, blocked, last = [], 0, None
    for q in queries:
        try:
            j = _with_retry(lambda q=q: sess.get_json(
                "https://image.baidu.com/search/acjson",
                params={"tn": "resultjson_com", "ipn": "rj", "word": q,
                        "pn": pn, "rn": rn, "nc": 1, "ie": "utf-8", "oe": "utf-8"},
                headers={"Referer": "https://image.baidu.com/"},
                timeout=opts.get("timeout", 25)), what="百度图片")
            if j.get("antiFlag"):
                raise net.NetError("百度图片把我们当爬虫挡了（antiFlag）")
            data = j.get("data")
            if not isinstance(data, list):
                raise net.NetError("百度图片这次的返回里没有结果列表（接口可能改了）")
        except Exception as e:                               # noqa: BLE001
            # **按检索词容错**：8 条词里被挡了 1 条，不该把另外 7 条的
            # 结果一起丢掉（那是"一家失败=整批归零"，最亏的一种写法）。
            blocked += 1
            last = e
            continue
        for it in data:
            if not isinstance(it, dict):
                continue
            thumb = it.get("thumbURL") or it.get("middleURL") or ""
            orig, page = "", ""
            ru = it.get("replaceUrl")
            if isinstance(ru, list) and ru and isinstance(ru[0], dict):
                cand = ru[0].get("ObjURL") or ""
                if cand.startswith("http"):
                    orig = cand
                pg = ru[0].get("FromURL") or ru[0].get("FromUrl") or ""
                if pg.startswith("http"):
                    page = pg
            host = it.get("fromURLHost") or ""
            if not page and host:
                page = "https://" + host
            orig = orig or it.get("middleURL") or thumb
            if not (orig or thumb):
                continue
            rows.append(_item(orig, thumb, it.get("width"), it.get("height"),
                              it.get("fromPageTitleEnc"), page, "baidu", q,
                              site=host or None))
    if not rows and blocked:
        raise net.NetError("百度图片这次一条都没给（%d 条检索词全被挡）：%s"
                           % (blocked, last))
    return rows


def _so360(queries, per_query, key, opts):
    """360 图片（so.com）—— **不需要 key，也不用 cookie**。

    实测要点（这条最反直觉，必须写下来）：
      · `pn` **不是页号，是"滚动位置"**，而且返回的是 **0..pn 的累积列表**：
            pn=0   → 2 条      pn=30  → 59 条
            pn=60  → 110 条    pn=150 → 180 条（到顶，再多给也是 180）
        所以"pn=30 拿到的 59 条"是"pn=60 拿到的 110 条"的**子集**，不是
        下一批。传 pn=30 当第一页用最划算；我们按 `30 × 页号` 取。
        多出来的重叠不用在这里去重 —— 下游 fetch/rank 本来就有
        URL 与内容两级去重。
      · 单次 `rn` 几乎不起作用（pn=0 时 rn=10/30/60 都只回 2 条），
        真正决定条数的是 pn。
      · `list[]` 里：`img` 是**原图**（原站 CDN 直链）、`thumb` 是 360 自家
        CDN 的缩略图（`p*.qhimg.com`，实测只有 240 长边，低于 tile.MIN_SIDE，
        拿它兜底多半会被判"太小"—— 留着，但别指望它）、
        `link` 才是来源页、`site`/`dspurl` 是来源域名。
      · 搜狗图片我们试过：`pic.sogou.com/napi/pc/searchList` 即使带 cookie
        也回 `{"status":1,"info":"forbid","data":null}`，还要签名参数。
        **没成，所以没有搜狗适配器** —— 不留半成品。
    """
    rows = []
    pn = 30 * max(1, int(opts.get("page") or 1))
    rows, blocked, last = [], 0, None
    for q in queries:
        try:
            j = _with_retry(lambda q=q: net.get_json(
                "https://image.so.com/j",
                params={"q": q, "pn": pn, "rn": 30, "src": "srp"},
                headers={"Referer": "https://image.so.com/"},
                timeout=opts.get("timeout", 25)), what="360 图片")
            lst = j.get("list")
            if not isinstance(lst, list):
                raise net.NetError("360 图片这次的返回里没有结果列表（接口可能改了）")
        except Exception as e:                               # noqa: BLE001
            blocked += 1                                     # 按词容错，同 _baidu
            last = e
            continue
        for it in lst:
            if not isinstance(it, dict):
                continue
            orig = it.get("img") or ""
            if not orig.startswith("http"):
                orig = it.get("thumb") or ""
            thumb = it.get("thumb") or it.get("thumb_bak") or ""
            if not (orig or thumb):
                continue
            rows.append(_item(orig, thumb, it.get("width"), it.get("height"),
                              it.get("title"), it.get("link") or "", "so360", q,
                              site=it.get("site") or it.get("dspurl") or None))
    if not rows and blocked:
        raise net.NetError("360 图片这次一条都没给（%d 条检索词全被挡）：%s"
                           % (blocked, last))
    return rows


def _bing(queries, per_query, key, opts):
    """必应图片（cn.bing.com 的 async 接口）—— **不需要 key**，但要自己解 HTML。

    ⚠️ **默认不跑**（REGISTRY 里 default=False）：它是"免 key 又真的全网"的
    唯一一家，但**会限流到"回一整套不相关的图"** —— 不是报错，是安安静静
    给你 35 张跟检索词毫无关系的图。实测：一次跑十条词之后，搜"碎花 平铺
    素材"回来的是电动绿篱机，搜"青花 花卉 平铺"回来的是三原县地图和硬笔
    书法。这些图是真图、能下，于是实实在在占掉候选名额（候选会被截到 800
    条），等于**挤掉了好来源的名额**。所以留着适配器、能手动开（
    `--provider bing`），但不进默认。哪天它稳定了再放回 DEFAULT_ORDER。

    实测要点（全是打出来的）：
      · **没有 JSON 接口**。`/images/async` 回的是一段 HTML，每张图是一个
        `m="{...}"` 属性（JSON，里面的引号被转义成 `&quot;`），要先
        html.unescape 再 json.loads。正则必须是 `m="([^"]+)"` ——
        用 `\\{.*?\\}` 会在嵌套括号处截断，实测只能解出 1~3 条（踩过）。
      · **mkt 必须跟着检索词的语言走**：中文词 + zh-CN 一次给 35 条，
        中文词 + en-US 只回 1 条，英文词 + zh-CN 也只回 1 条 ——
        **不报错，只是静默变少**，最容易误判成"这家不能用"。
      · 翻页用 `first`（1 / 36 / 71 …，步长 = count=35），是真的偏移量：
        实测 35 → 25 → 7 → 1 递减。
      · 字段：`murl` 原图、`turl` 缩略图（mm.bing.net，长边小，只当兜底）、
        `purl` 来源页、`t` 标题。blob 里**没有宽高**。
      · 偶发回空页（0 字节），重试一次就有 —— 所以走 _with_retry。
      · 部分原图直链本身就是**带防盗链的**（花瓣的 gd-hbimg 不带 auth_key
        时 403），下不动就只能用缩略图，跟别家一样交给 fetch 兜底。
    """
    rows = []
    count = 35
    page = max(1, int(opts.get("page") or 1))
    first = 1 + (page - 1) * count
    sess = net.session("https://cn.bing.com/images/search")
    blocked, last = 0, None
    for q in queries:
        mkt = "zh-CN" if _cjk(q) else "en-US"
        url = "https://cn.bing.com/images/async?" + urllib.parse.urlencode(
            {"q": q, "first": first, "count": count, "mkt": mkt, "mmasync": 1,
             "relp": count, "tsc": "ImageBasicHover", "datsrc": "I",
             "layout": "RowBased"})
        try:
            text = _with_retry(
                lambda url=url: sess.get_text(
                    url, headers={"Referer": "https://cn.bing.com/images/search"},
                    timeout=opts.get("timeout", 25),
                    max_bytes=6 * 1024 * 1024),
                tries=3, delay=1.2, what="必应图片")
        except Exception as e:                               # noqa: BLE001
            blocked += 1                                     # 按词容错，同 _baidu
            last = e
            continue
        for blob in re.findall(r'm="([^"]+)"', text):
            try:
                d = json.loads(html.unescape(blob))
            except Exception:                                # noqa: BLE001
                continue
            if not isinstance(d, dict) or not d.get("murl"):
                continue
            rows.append(_item(d.get("murl"), d.get("turl"), 0, 0,
                              d.get("t"), d.get("purl") or "", "bing", q))
    if not rows and blocked:
        raise net.NetError("必应图片这次一条都没给（%d 条检索词都失败了）：%s"
                           % (blocked, last))
    return rows


def _huaban(queries, per_query, key, opts):
    """花瓣（huaban.com）—— **不需要 key**，国内设计素材最对路的一家。

    为什么值得加它：花瓣是国内设计师采风的主阵地，"平铺/四方连续/印花"
    这类词在它上面比在搜索引擎上准得多（搜出来的就是素材本身，不是落地页）。

    实测要点：
      · **网页版不能用**：`huaban.com/search?q=` 是 JS 渲染的，实测 986 字节、
        0 个 pin。**必须走 `api.huaban.com/search`**（`q` / `page` /
        `per_page`），回 JSON，`pins[]` 里就是结果。
      · 图直链在 `file.url`，**带 auth_key、有时效**（实测约一天），
        所以**别自己拿 key 去拼**（不带 auth_key 一律 403）。但**要换域名**：
        API 给的 `gd-hbimg-edge` 实测 20 条里 0 条能下（HTTP 567），
        换成同 key 同参数的 `gd-hbimg` 就是 20/20（见 _hb_urls）。
      · `raw_text` 经常就是那个 key 本身（等于没标题），这时退回画板名；
        再没有就用标签拼一下 —— 标题后面要进甄别和索引，别留一堆哈希。
      · **只认中文词**（见 _cjk）：英文词在花瓣上几乎是空的，跳过省一次请求。
      · **长检索词必须截短**（见 _trim_for_zh）：它按空格做"与"匹配，
        analyze 生成的七八个词的组合词在这儿一律 0 条。
    """
    rows = []
    per_page = max(10, min(50, int(per_query or 20)))
    page = max(1, int(opts.get("page") or 1))

    def _one(q):
        j = _with_retry(lambda q=q: net.get_json(
            "https://api.huaban.com/search",
            params={"q": q, "page": page, "per_page": per_page},
            headers={"Referer": "https://huaban.com/"},
            timeout=opts.get("timeout", 25)), what="花瓣")
        got = []
        for p in (j.get("pins") or []):
            if not isinstance(p, dict):
                continue
            f = p.get("file") or {}
            url = f.get("url") or ""
            if not url.startswith("http"):
                continue
            title = (p.get("raw_text") or "").strip()
            if not title or title == f.get("key"):
                title = ((p.get("board") or {}).get("title") or "")
            if not title:
                title = " ".join((p.get("tags") or [])[:6])
            page_url = p.get("link") or ""
            if not page_url and p.get("pin_id"):
                page_url = "https://huaban.com/pins/%s" % p["pin_id"]
            prim, alt = _hb_urls(url)                        # 换域名 + 备一个域名
            got.append(_item(prim, alt, f.get("width"), f.get("height"),
                             title, page_url, "huaban", q, site="huaban.com",
                             # ⚠️ 必须站点根：实测 hbimg 带**具体页** referer
                             # 回 567，不带也 567，只有 `https://huaban.com/` 才 200
                             ref="https://huaban.com/"))
        return got

    for q in queries:
        if not _cjk(q):
            continue
        for cand in _trim_for_zh(q):                         # 从截短到最宽，谁先出结果用谁
            try:
                got = _one(cand)
            except Exception:                                # noqa: BLE001
                continue                                     # 按词容错，不整批归零
            if got:
                rows.extend(got)
                break
    return rows


def _duitang(queries, per_query, key, opts):
    """堆糖（duitang.com）—— **不需要 key**，壁纸/素材社区，量很大。

    实测要点：
      · napi：`/napi/blog/list/by_search/?kw=&start=&limit=`，回 JSON，
        `data.object_list[]` 是结果，`data.next_start` 是下一页的 start。
      · `start` 是**真偏移**（实测 0 / 24 / 200 三条的起头各不相同），
        `limit` 能给到 100（实测一次回 100 条）。
      · 图直链 `photo.path`（dtstatic CDN），**不防盗链**，裸 GET 就能下
        （实测 2.4 MB jpeg、200）。
      · 标题在 `msg`，`tags` 是标签列表；**没有来源页字段**，用博客页拼一个
        （`https://www.duitang.com/blog/?id=<id>`）—— 索引里有个能点回去的
        链接就行，不是主字段。
      · 同花瓣：**只认中文词**，长词也要**截短**（见 _trim_for_zh）。
    """
    rows = []
    limit = max(10, min(100, int(per_query or 24)))
    page = max(1, int(opts.get("page") or 1))
    start = (page - 1) * limit

    def _one(q):
        j = _with_retry(lambda q=q: net.get_json(
            "https://www.duitang.com/napi/blog/list/by_search/",
            params={"kw": q, "start": start, "limit": limit},
            headers={"Referer": "https://www.duitang.com/"},
            timeout=opts.get("timeout", 25)), what="堆糖")
        got = []
        data = j.get("data") or {}
        for o in (data.get("object_list") or []):
            if not isinstance(o, dict):
                continue
            ph = o.get("photo") or {}
            url = ph.get("path") or ""
            if not url.startswith("http"):
                continue
            tags = o.get("tags") or []
            title = o.get("msg") or ""
            if not title and isinstance(tags, list):
                title = " ".join(str(t) for t in tags[:6])
            page_url = ("https://www.duitang.com/blog/?id=%s" % o.get("id")
                        if o.get("id") else "")
            got.append(_item(url, "", ph.get("width"), ph.get("height"),
                             title, page_url, "duitang", q, site="duitang.com"))
        return got

    for q in queries:
        if not _cjk(q):
            continue
        for cand in _trim_for_zh(q):                         # 同花瓣：从截短到最宽
            try:
                got = _one(cand)
            except Exception:                                # noqa: BLE001
                continue
            if got:
                rows.extend(got)
                break
    return rows


# 瓦栏的 alt 是模板拼出来的（`<分类标签>印花花型图片`），尾巴必须剥掉。
# 不剥的话一页 100 张里九十张都叫"年轻女装印花花型图片"，标题进索引等于没有。
_WALAN_TAIL = ("印花花型图片", "花型图片", "印花图案", "印花花型", "花型",
               "印花", "图片")


def _walan_title(alt):
    """`豹纹印花花型图片` -> `豹纹`；剥不出来就原样返回。"""
    s = (alt or "").strip()
    for tail in _WALAN_TAIL:
        if s.endswith(tail) and len(s) > len(tail):
            return s[: -len(tail)].strip() or s
    return s


def _walan_words(q):
    """瓦栏能吃的检索词，**从具体到宽**排好。

    比 _trim_for_zh 多退两档：analyze 生成的首词经常是一长串
    （"水墨牡丹花卉无缝印花图案"），瓦栏按与匹配，整串上去只有个位数；
    截成"水墨牡丹"就有一批，"水墨"有整整 100 张。
    所以首词超过 4 个字时，再补上前 4 / 前 3 / 前 2 字当退路。
    """
    out = []
    for c in _trim_for_zh(q):
        if c and c not in out:
            out.append(c)
    toks = [t for t in (q or "").split() if t]
    if toks and _cjk(toks[0]):
        head = toks[0]
        for n in (4, 3, 2):
            w = head[:n]
            if len(head) > n and w not in out:
                out.append(w)
    return out


def _walan(queries, per_query, key, opts):
    """瓦栏（walanwalan.com）—— 国内印花花型交易平台，**搜索页免登录**。

    和上面几家的根本差别，也是加它的理由：**整个站就是平铺花型**。
    搜索页 / 分类页是服务端渲染的公开页面，不登录就能拿到一页 100 张；
    详情页才要登录。所以取列表页的公开缩略图，够做检索线索和视觉甄别。

    ⚠️ 边界（写在这里，改代码的人别越线）：
      · 这些图是**平台在售的花型商品**（￥99~299/张）。只取**列表页公开的
        缩略图**（300×450），够看够筛；**不登录、不碰原图**。
      · 索引里保留详情页链接（`src_page`）—— 用户看中哪张，去详情页正版下单。
      · 别拿缩略图直接投产：300px 做不了印花，它只是"线索"。

    实测要点（都是打出来的）：
      · URL 形状：`/search/patterns/{词}/page{N}/`，一页**固定 100 张**，
        实测翻到第 7 页还是 100 张，没限流。
      · **中英文都认**（"floral" 也回 100 张），所以不像花瓣/堆糖那样按
        语言跳过。
      · 搜索是**按空格做与匹配**，跟花瓣/堆糖一个脾气：
        "青花 四方连续" 只剩 3 张，"水墨 印花" 100 张，乱词 0 张。
        所以按 _trim_for_zh 从具体到宽地试，**但别见好就收** —— 双词给
        3 张时要退到单词（100 张），见下面 `_MIN` 的判断。
      · 卡片结构：
            <div class="design_grid rel">
              <a class="design_grid_link" href="/designs/<id>/" data-photoid="<id>">
                <img class="img-responsive" alt="…" src="https://…/cacheimgs/photos/<id>_son/<md5>.jpg">
      · 缩略图**必须带 .jpg**：去掉后缀是 404（踩过）。
      · 搜索确认真实：`水墨` 和 `几何` 各 100 张，photo id **交集 0**。
    """
    rows = []
    base = "https://www.walanwalan.com"
    cap = max(10, min(100, int(per_query or 100)))
    page = max(1, int(opts.get("page") or 1))
    sess = net.session(base + "/")

    def _one(word):
        u = "%s/search/patterns/%s/page%d/" % (
            base, urllib.parse.quote(word, safe=""), page)
        txt = _with_retry(lambda: sess.get_text(
            u, headers={"Referer": base + "/"}, timeout=opts.get("timeout", 30)),
            what="瓦栏")
        if not txt:
            return []
        got, seen = [], set()
        for blk in re.split(r'<div class="design_grid[ "]', txt)[1:]:
            mid = re.search(r'href="/designs/(\d+)/"', blk)
            mt = re.search(r"<img[^>]*>", blk)
            if not (mid and mt):
                continue
            pid = mid.group(1)
            if pid in seen:
                continue
            tag = mt.group(0)
            src = re.search(r'src="([^"]+)"', tag)
            alt = re.search(r'alt="([^"]*)"', tag)
            if not src:
                continue
            url = html.unescape(src.group(1))
            if not url.startswith("http"):
                url = base + url
            seen.add(pid)
            got.append(_item(url, "", 0, 0,
                             _walan_title(html.unescape(alt.group(1)) if alt else ""),
                             "%s/designs/%s/" % (base, pid), "walan", word,
                             site="walanwalan.com"))
            if len(got) >= cap:
                break
        return got

    # 双词命中就停的话，像"青花 四方连续"这种只剩 3 张 —— 太少。
    # 所以按"从具体到宽"试，**留够 20 张才收手**，否则继续退到更宽的词。
    for q in queries:
        best = []
        for cand in _walan_words(q):
            try:
                got = _one(cand)
            except Exception:                                # noqa: BLE001
                continue
            if len(got) > len(best):
                best = got
            if len(best) >= 20:
                break
        rows.extend(best)
    return rows


# -------------------------------------------------------------- 色彩跳动
# 上海尚乎彩链的「色彩跳动」—— 花型图案**版权登记 / 侵权检测 / 交易**平台。
# 和瓦栏同类（都是国内印花花型在售平台），但**接口是公开的 JSON**，
# 不用爬 HTML，也不用浏览器，比瓦栏更省事。
_DC_HOME = "https://www.dancecolor.net"
_DC_API = _DC_HOME + "/pb/crms/api/pageHome/getHomeInfoPage"


def _dc_preview(url, w=480):
    """把原图直链换成一版**站方自己的图片处理预览**。

    为什么不直接下 `fileUrl`：那是**原图**（实测 2.3MB / 2362×3543），
    而这是版权交易平台在售的作品 —— 下原图就越线了，跟瓦栏"不碰原图"
    是同一条规矩。它自家前端本来就用百度 BOS 的图片处理接口出缩略图：

        `${url}?x-bce-process=image/quality,q_20/format,f_webp/resize,m_lfit,w_50,h_50/ignore-error,i_1`

    我们照用同一个接口，只把尺寸放大、质量提高、格式换 jpg。
    **长边钉在 480**：正好跟瓦栏列表页那档（300×450）一个量级 ——
    够看清平铺结构、够喂视觉甄别，但**当不了生产素材**，它就是"线索"。
    `ignore-error,i_1` 是原厂参数，别删 —— 删了老图会 400。
    """
    if not url:
        return ""
    if "x-bce-process" in url:                     # 已经是预览了，别套两层
        return url
    return url + ("?x-bce-process=image/quality,q_80/format,f_jpg"
                  "/resize,m_lfit,w_%d,h_%d/ignore-error,i_1" % (w, w))


def _dc_title(rec):
    """标题。**`worksTitle` 经常是一坨标签串，不能直接用。**

    实测两条相邻记录的差别：
        worksName  : 叶子与山水墨风国风
        worksTitle : 叶子与山水墨风国风            ← 正常
        worksName  : 水墨中国风生肖龙
        worksTitle : DC170185027151453 时尚创意,节日,装饰纹样,动物,中国风 一支画笔/水…
                                                   ← 作者没填标题，系统拿 id+标签拼的

    那串东西进索引等于没标题（跟瓦栏"年轻女装印花花型图片"一个病）。
    判断办法：**开头就是自己的 worksId / 带 `DC1` 前缀**的当垃圾丢掉。
    """
    wid = (rec.get("worksId") or "").strip()
    cands = [(rec.get("worksName") or "").strip(),
             (rec.get("worksTitle") or "").strip()]
    for s in cands:
        if s and not s.startswith(wid) and "DC1" not in s[:24]:
            return s
    return cands[0] or cands[1]


def _dc_words(q):
    """检索词退让策略**跟瓦栏同一套**（也是按空格做与匹配），但多两档。

    实测命中量（这站的搜索是整串 LIKE，不是分词）：

        水墨 198 | 水墨 中国风 14 | 四方连续 85 | 青花瓷 6 | 乱词 0
        法式小碎花 0 | 法式 5 | 小碎花 **1838** | 碎花 2159 | 花卉 3907

    看出问题了：`_walan_words` 只会从**左边**截（法式小碎花 → 法式小碎 →
    法式小 → 法式），而这里**主语在尾巴上** —— 退到"法式"只剩 5 条，
    旁边"小碎花"有 1838 条，白白错过。所以补两档：

      · **尾部截断** head[-3:] / head[-2:]（法式小碎花 → 小碎花 → 碎花）。
        中文修饰语在前、主体在后，这条规律在这站特别灵。
      · **行业词单独试**（四方连续 / 平铺 / 印花 …）。这站有 85 条标题里
        明写"四方连续"，是唯一能把折枝/单只筛掉的词，值一条请求。
    """
    out = list(_walan_words(q))
    toks = [t for t in (q or "").split() if t]
    if toks and _cjk(toks[0]):
        head = toks[0]
        for n in (3, 2):
            w = head[-n:]
            if len(head) > n and w not in out:
                out.append(w)
    for t in toks[1:]:
        if any(h in t for h in _SEAM_HINT) and t not in out:
            out.append(t)
    return out


def _dancecolor(queries, per_query, key, opts):
    """色彩跳动（dancecolor.net）—— 国内花型版权交易平台，**接口免登录**。

    加它的理由：**这是目前找到的最对路的一家**。
      · 整个站就是**印花花型**（不是素材站、不是图片搜索），平铺闸门通过率最高；
      · **免 key、免登录、免浏览器**，一个 POST 回 100 条 JSON，一次 0.5 秒；
      · **中文检索词原生就吃**（不像 patternbank/Spoonflower 只认英文）；
      · 单张就带 `fileWidth/fileHeight` 和适用面料（`suitArea`），能直接判尺寸。

    怎么找到的（记一下，以后前端换版本还用得上）：
      这是个 Vue SPA，`robots.txt`/`sitemap.xml` 都回落到 index.html（等于没有），
      页面里只有一个入口包。接口藏在**路由分包**里：
        `assets/js/index-*.js` → 路由表写死 `./mallIndex-*.js`
        → `mallIndex` 引用 `./works-*.js`，那里才是 api 定义：
              getHomeInfoPage: e=>o.post("/api/pageHome/getHomeInfoPage", e)
              getHomeWorksList: e=>o.get("/api/pageHome/getHomeWorksList", e)
              getPage: e=>o.post("/api/worksInfo/page", e)
      而 `o` / `t` 这两个 axios 实例的 baseURL 在入口包里：
          `x` = `/pb/crms`、`y` = `/pb/crbasems`、`R` = `/pb/cmms`
      **所以完整路径要加 `/pb/<微服务>` 前缀** —— 少了这段就是 404。
      列表**不需要任何 token**，Anonymous 直接 200。

    ⚠️ 边界（跟瓦栏那条一样，改代码的人别越线）：
      · 这些是**平台在售的花型作品**，且这个平台的主营业务就是**版权登记
        和侵权检测**。只取**站方自己那套图片处理接口出的预览图**（长边 480），
        **不下原图**；索引里保留详情页链接（`src_page`），看中哪张去正版买。
      · **不登录、不带账号、不碰 `/api/worksInfo/*` 那些读写接口**
        （`add`/`update`/`deleteById`/`vipDownloadFile` 一个都别调）——
        只读 `/api/pageHome/getHomeInfoPage` 这一个搜索接口。
      · 尺寸钉在 480 是**故意的**，别顺手改大：改大了这批图就"能用"了，
        那就不叫线索了。要看大图/要生产用图，去详情页走正版授权。

    实测要点：
      · 搜索是**按空格做与匹配**，`水墨` 198 / `水墨 中国风` 14 / `四方连续` 85
        / 乱词 0；`法式碎花` 这种四字生造词 0 条 —— 必须往短退（见 _dc_words）。
      · 一页**最多 100 条**（`size:100` 实测有效，`pages` 会算对）。
      · 必须带 `customQueryParams.type:"1"`：那是"商城在售"这一类，
        跟站内商城页一致（不加也能回，但排序不一样）。
      · 翻页靠 `page.current`，第 2 页回 98 条（总 198）。
    """
    rows = []
    cap = max(10, min(100, int(per_query or 100)))
    page_from = max(1, int(opts.get("page") or 1))
    pages = max(1, int(opts.get("dc_pages") or 1))   # 一页 100 张，默认只翻一页
    hdrs = {"Referer": _DC_HOME + "/", "Origin": _DC_HOME}

    def _one(word):
        got, seen = [], set()
        for pno in range(page_from, page_from + pages):
            payload = {"customQueryParams": {"worksTitle": word, "type": "1"},
                       "page": {"current": pno, "size": 100}}
            js = _with_retry(
                lambda p=payload: net.post_json(
                    _DC_API, p, headers=hdrs, timeout=opts.get("timeout", 30)),
                what="色彩跳动")
            dd = (js or {}).get("data") or {}
            recs = dd.get("records") or []
            if not recs:
                break
            for rec in recs:
                wid = (rec.get("worksId") or "").strip()
                if not wid or wid in seen:
                    continue
                seen.add(wid)
                got.append(_item(
                    _dc_preview(rec.get("fileUrl")), "", 
                    _int(rec.get("fileWidth")), _int(rec.get("fileHeight")),
                    _dc_title(rec),
                    "%s/#/detail?worksId=%s" % (_DC_HOME, wid),
                    "dancecolor", word, site="dancecolor.net"))
                if len(got) >= cap:
                    return got
        return got

    # 跟瓦栏同一套：从具体试到宽，**留够 20 张才收手**（不然"青花瓷"给 6 张就停）
    for q in queries:
        best = []
        for cand in _dc_words(q):
            try:
                got = _one(cand)
            except Exception:                                # noqa: BLE001
                continue
            if len(got) > len(best):
                best = got
            if len(best) >= 20:
                break
        rows.extend(best)
    return rows


# -------------------------------------------------------- 全球纺织网·花样画稿
# 全球纺织网（tnc.com.cn / qfc.cn）的**花样画稿市场** —— 国内面料印花的
# 花型交易平台，服务端渲染、免登录。这一批候选站里**最对路的一家**：
# 600×900 的无水印缩略图，比瓦栏（300×450）和色彩跳动（长边 480）都大一倍。
_HX_HOME = "https://hx.tnc.com.cn"
_HX_CAT_ALL = "13773"        # 分类「全部」。13774 女装 / 13800 碎花 / 13801 旗袍
_HX_PER_PAGE = 24            # 一页固定 24 条（实测，不随参数变）
_HX_GAP = 1.2                # 站方扛不住并发：同时打 4 个直接回 500（踩过）


def _hx_word(word):
    """hx 的检索词**必须按 GBK 百分号编码**。

    按 UTF-8 编上去也回 200，但 `k` 参数被整段忽略 —— 拿到的是"全部 24 条"，
    看着像成功、其实一个字都没筛（连 `<title>` 都还是"全部花样画稿"）。
    这个坑只从"有没有报错"分辨不出来，**必须比对卡片内容**才发现。
    整个站是 GBK 的（`<meta charset="GBK">`），所以 k 段也按 GBK 走。
    """
    try:
        raw = word.encode("gbk")
    except UnicodeEncodeError:               # 生僻字/emoji 编不出来就丢掉
        raw = word.encode("gbk", "ignore")
    return urllib.parse.quote(raw, safe="")


def _hx_words(q):
    """hx 跟瓦栏/色彩跳动**不是一个脾气，别照抄那两家的退让策略**。

    它的搜索是**分词 OR**：`水墨牡丹` 回 32 页，比 `水墨` 的 20 页**还多**
    （"牡丹"那一支也算了进来）；`碎花` 和 `花卉` 更是回**同一个 24 条**
    （都命中"花"）。所以：
      · 长词不会 0 条 → 不需要往短了退；
      · 但**长词更宽泛**，不是更精准 → 也别指望靠加词收敛。
    结论：_trim_for_zh 那两档（首词+行业词、首词）就够，别再退。
    """
    return _trim_for_zh(q)


def _hx_title(blk):
    """从卡片里剥出**结构化标签**，价格和编号不是描述。

        <div class="title">
          <span class="price">￥199 / ￥299</span>
          <span class="wl-orange color">( 0017696 )</span>
          <br> <div>中国风-水墨</div>
        </div>

    → `中国风-水墨`。这站的标签是整个库里最有价值的东西：它自己就写着
    「四方连续-花卉」「中国风-水墨」「花卉-水彩」，比任何搜索结果的 alt 都准，
    而且**直接就是我们要的维度**（工艺 + 题材）。
    """
    m = re.search(r'class="title"[^>]*>(.*?)</div>\s*</a>', blk, re.S)
    seg = m.group(1) if m else blk
    tags = re.findall(r"<div[^>]*>(.*?)</div>", seg, re.S)
    txt = _strip_tags(tags[-1]) if tags else _strip_tags(seg)
    txt = re.sub(r"￥\s*[\d.]+\s*(?:/\s*￥\s*[\d.]+)?", "", txt)
    txt = re.sub(r"\(\s*\d+\s*\)", "", txt)
    return re.sub(r"\s+", " ", txt).strip()


def _hx(queries, per_query, key, opts):
    """全球纺织网「花样画稿市场」（hx.tnc.com.cn）—— 国内花型交易平台，免登录。

    加它的理由：
      · **整个站就是在售印花花型**（上千页货），不是素材站、不是图片搜索；
      · 缩略图 **600×900 且无水印** —— 瓦栏 300×450、色彩跳动长边 480，
        这家是这一批里最高清的一家，平铺结构和花型细节都看得清；
      · 卡片自带**结构化标签**（`四方连续-花卉`/`中国风-水墨`），
        等于站方替我们做了一次分类；
      · 中文检索词原生就吃，而且**搜索真实**（水墨∩几何=1、水墨∩条纹=0）；
      · 免 key、免登录、免浏览器 —— 纯 HTML，比 patternbank/Spoonflower 省事一个量级。

    URL 形状（换版时照这条路再走一遍）：
        /search/products-c-<分类ID>-k-<GBK百分号编码的词>-p<页>.html
      · `c` 是分类（13773=全部 / 13774 女装 / 13800 碎花 / 13801 旗袍）；
      · `k` 是关键词，**必须 GBK 编码**（见 _hx_word）；
      · `p` 是页码。首页 HTML 里 `urlforma` 字段直接就是
        `/search/products-c-13773-k-%CB%AE%C4%AB-p#.html` 这个模板。
      · 还有个 `?attr_1614=5350/5351/5352` = 四方连续/二方连续/独幅 的筛选，
        但实测**换个分类才生效**，单独加在 k 搜索上没反应，所以没用。

    ⚠️ 边界（跟瓦栏/色彩跳动同一条）：
      · 这些是**平台在售的花型画稿**（￥99~499/张）。只取**列表页公开的
        缩略图**（600×900），**不登录、不碰原图**、不进会员中心。
      · 索引里保留详情页链接（`src_page`）—— 看中哪张去详情页正版下单。
        600px 做不了印花稿，它只是"线索 + 甄别样本"。

    实测要点：
      · **一页固定 24 条**，翻页是独立的（p1∩p2 = 0），搜"水墨"共 20 页。
      · 库存量很大：`碎花`/`花卉` 各 270 页（那是分词的锅，见 _hx_words），
        `香云纱` 24 页、`中国风` 47 页、`扎染` 只有 18 条（一页不满）。
      · **并发会 500**：同时打 4 个请求，p1 直接 Internal Server Error，
        串行重试就好。所以加了 _HX_GAP 的间隔。
      · `robots.txt` / `sitemap.xml` 都回 404 兜底页（等于没有 robots）。
    """
    rows = []
    cap = max(10, min(100, int(per_query or 100)))
    page_from = max(1, int(opts.get("page") or 1))
    pages = max(1, int(opts.get("hx_pages")
                     or (cap + _HX_PER_PAGE - 1) // _HX_PER_PAGE))
    hdrs = {"Referer": _HX_HOME + "/"}
    timeout = opts.get("timeout", 30)

    def _one(word):
        got, seen = [], set()
        for i in range(pages):
            u = "%s/search/products-c-%s-k-%s-p%d.html" % (
                _HX_HOME, _HX_CAT_ALL, _hx_word(word), page_from + i)
            try:
                status, _h, body = net.request(u, headers=hdrs, timeout=timeout,
                                               loose_tls=True)
            except Exception:                                # noqa: BLE001
                break
            if status != 200 or not body:
                break
            txt = body.decode("gbk", "replace")
            before = len(got)
            for blk in txt.split('<li class="item"')[1:]:
                mh = re.search(r'href="/product-detail-(\d+)\.html"', blk)
                mi = re.search(r"<img[^>]+src=\"([^\"]+)\"", blk)
                if not (mh and mi):
                    continue
                pid = mh.group(1)
                if pid in seen:
                    continue
                seen.add(pid)
                got.append(_item(
                    html.unescape(mi.group(1)), "", 600, 900, _hx_title(blk),
                    "%s/product-detail-%s.html" % (_HX_HOME, pid),
                    "tnc", word, site="hx.tnc.com.cn"))
                if len(got) >= cap:
                    return got
            if len(got) == before:      # 这页一条新的都没有，后面更不会有
                break
            if i + 1 < pages:
                time.sleep(_HX_GAP)
        return got

    # 不做"留够 20 张才收手"那套（瓦栏要，因为它是与匹配）：
    # hx 分词太宽，"碎花"能给 6480 条，第一档就吃饱了，退让没有意义。
    for q in queries:
        for cand in _hx_words(q):
            try:
                got = _one(cand)
            except Exception:                                # noqa: BLE001
                continue
            if got:
                rows.extend(got)
                break
    return rows


# --------------------------------------------------------------- Pattern Designs
# 德国的免版税矢量图案店（patterndesigns.com）。**公开 JSON 分页接口**，
# 免 key 免登录免浏览器；缩略图 700×700 的 webp，无水印。
_PD_HOME = "https://www.patterndesigns.com"
_PD_API = _PD_HOME + "/zh/search/ajaxLoadMoreDesigns"
_PD_MAX = 60                  # `limit` 上限就是 60，给再大也只回 60


def _pd_words(q):
    """patterndesigns 的检索词：**只给题材词，行业词一个都别喂**。

    ⚠️ 这跟瓦栏/色彩跳动**正好相反**，别顺手"统一"掉：
    这站的每一条标题都是 `xxx 无缝矢量模式设计`，所以 `四方连续` / `无缝` /
    `平铺` 这类词在这里**命中全库**。实测：
        `四方连续`  → 虚线四足类 / 地理方块 / 连续树叶     （纯噪声）
        `国风 四方连续` → 连续的叶子 / 金花线条 / 虚线四足类 （"国风"被淹掉）
        `花卉`      → 盛开的花草甸 / 传统花卉 / 夏季花卉和草药  （干净）
    所以行业词在这里不是"锚定"，是"稀释"。只取前两个**非行业词**当题材。

    另：它的中文标题是站方机器翻的，`水墨` 会被它当成"墨水"（回 ink 类），
    语义已经偏了 —— 这个交给甄别层兜，别在词上硬修。
    """
    toks = [t for t in (q or "").split() if t]
    out = []
    for t in toks:
        if any(h in t for h in _SEAM_HINT):        # 行业词 = 这站的全库
            continue
        if t not in out:
            out.append(t)
        if len(out) >= 2:
            break
    # 全是行业词就返回空 —— 宁可这家跳过，也别拿"四方连续"去把全库捞回来
    return out


def _patterndesigns(queries, per_query, key, opts):
    """Pattern Designs（patterndesigns.com）—— 免版税**矢量**无缝图案库。

    **默认不跑**（不在 DEFAULT_ORDER 里）。原因是实跑出来的，不是拍脑袋：
      · `gallery-m` 那档**带满屏水印**（“patterndesigns” 大字横在画面中间），
        比瓦栏/色彩跳动那种"只是小一圈"差一档，**只能当线索**；
      · 整站是**硬边矢量平涂**，碰到「水墨/国风/扎染」这类工艺需求
        一条都对不上（实测 33 张过平铺闸门、甄别全剔）；
      · 中文标题是站方机翻，语义会漂：`水墨` 被当成"墨水"（回 ink 类）、
        `国风` 回来的是大嘴鸟和法国斗牛犬。
    **它的适用场景是"按具体花名找"**：牡丹/玫瑰/樱花/莲花/竹 实测都很准
    （`牡丹`→精致的牡丹/小鸟和牡丹/冬季牡丹）。要它跑就 `--provider patterndesigns`。

    技术上仍然值得留着（这也是一份"怎么从 Rails 无限滚动站里挖出 JSON 接口"
    的样本）：
      · **公开 JSON 分页接口**，免 key、免登录、免浏览器，一次 60 条；
      · 整站都是四方连续（2,100+ 一个分类），平铺闸门几乎全过；
      · 缩略图 700×700（`gallery-m` 那档），尺寸恒定。

    怎么找到接口的（这站是 Rails，前端是无限滚动）：
      1. 分类页 `/zh/gallery/<cid>/<名字>` 的 HTML 里就有分页端点的明文：
             <div class="designs" data-scrollsize=60 data-ajaxloadmore=
                  "/zh/search/ajaxLoadMoreDesigns/306?seoTitle=…&categoryId=306&sort=">
      2. `application.js` 里的 `context.loadMore` 读的正是这个
         `data('ajaxloadmore')`，再往后拼 `&offset=N&limit=60`；
      3. 返回 `{"designs":[{id, galleryImageUrl, designUrl, title, designer…}]}`。
      **`?page=2` 是无效的**（服务端照样回第一页），只有 offset/limit 能翻。

    搜索参数（试出来的，别猜）：
      · `text=<词>` **生效**（`floral` 27 条、`花卉` 和 `水墨` 各回不同结果）；
      · `q=` / `searchText=` **都无效**（静默回默认列表，看着像成功）；
      · 乱词回 `{"designs":[]}`，不报错。

    ⚠️ 边界（跟瓦栏/色彩跳动同一条）：
      · 这些是**在售设计**（按授权卖）。只取 `gallery-m` 那档缩略图（700×700，
        **本身带满屏水印**），**不碰原图、不碰 `/zh/cart`**；`src_page` 存
        `/zh/design/<id>` 供正版购买。
      · `detailImageUrl` 那档只有 **400×400**（比 gallery-m 还小），别用。

    实测要点：
      · 一页 60 条，offset 拉到头回 `{"designs":[]}`（不是报错）；
      · 搜索结果里 `metadata.recolorable` / `apiAvailable` 这类字段跟我们要的无关，没存；
      · 英文词命中数常常比中文少（它标题是英文原名、中文是机翻），
        所以**中文需求用中文词就行**，别为了"国际化"把词翻成英文。
    """
    rows = []
    cap = max(10, min(_PD_MAX * 3, int(per_query or 100)))
    off0 = max(0, int(opts.get("pd_offset") or 0))
    hdrs = {"Referer": _PD_HOME + "/zh/", "X-Requested-With": "XMLHttpRequest"}
    timeout = opts.get("timeout", 30)

    def _one(word):
        got, seen, off = [], set(), off0
        while len(got) < cap:
            qs = urllib.parse.urlencode({
                "categoryId": "", "sort": "", "text": word,
                "offset": off, "limit": _PD_MAX})
            try:
                status, _h, body = net.request(
                    _PD_API + "?" + qs, headers=hdrs, timeout=timeout)
            except Exception:                                # noqa: BLE001
                break
            if status != 200 or not body:
                break
            try:
                recs = json.loads(body.decode("utf-8", "replace")).get("designs") or []
            except Exception:                                # noqa: BLE001
                break
            if not recs:
                break
            for r in recs:
                did = str(r.get("id") or "")
                img = r.get("galleryImageUrl") or ""
                if not did or not img or did in seen:
                    continue
                seen.add(did)
                got.append(_item(
                    img, "", 700, 700, r.get("title") or "",
                    _PD_HOME + (r.get("designUrl") or "/zh/design/" + did),
                    "patterndesigns", word, site="patterndesigns.com"))
                if len(got) >= cap:
                    return got
            if len(recs) < _PD_MAX:      # 最后一页了，再翻也没东西
                break
            off += _PD_MAX
        return got

    for q in queries:
        for cand in _pd_words(q):
            try:
                got = _one(cand)
            except Exception:                                # noqa: BLE001
                continue
            if got:
                rows.extend(got)
                break
    return rows


# ----------------------------------------------------------------- 源创花样网
# ypattern.com：2015 年上线的**原创花样交易平台**，在售 10 万+ 款、服务端渲染、
# 免登录就能翻列表。**货多**（搜「水墨」自己 582 页、搜「几何」1837 页）。
#
# ⚠️ 它是本产线里**唯一"放宽平铺闸门"**的来源，原因见 _ypattern 的 docstring：
#    站方给的是**裁成 4:3 的预览**（花回多为 1:1），铺不开 —— 但用户要的
#    只是"题材对得上"，所以这一家按"线索图"收，索引里标 tile_gate=off。
_YP_HOME = "https://www.ypattern.com"
_YP_LIST = _YP_HOME + "/patterns.aspx"
_YP_PER = 20                 # 一页固定 20 条真结果（实测，不随参数变）
_YP_GAP = 1.2                # WebForms + 老 IIS：连打快了就 403（踩过）


def _yp_words(q):
    """ypattern 的检索词：**UTF-8 百分号编码**（见 _ypattern 第 1 条）。

    ⚠️ 它只认**自己标签库里有的词**，实测（页面上的「记录总数」）：
        `碎花`/`小碎花` 9889、`水墨` 11624、`玫瑰` 104、`复古` 33
        `牡丹`/`青花`/`缠枝`/`团花`/`花卉` **全部 0**
        `印花` **198564 = 全库**（行业词在这站是稀释，跟 Pattern Designs 同病）
    所以国画花名（牡丹、青花、缠枝）在这站等于没货，得用碎花/玫瑰/复古这类词。
    长词不会像瓦栏那样 0 条（AND 会 0，但 `_trim_for_zh` 第二档就退到首词了），
    所以还是那两档，不做额外退让。
    """
    return _trim_for_zh(q)


def _yp_cards(txt):
    """从列表页只取**真结果卡片**，跳过 `#yourlike` 的推荐块。

    一张卡片（节选）：
        <li class="cbp-item designerli">
          <a href="/preview.aspx?code=C360Q8E0"><img src="/Preview/20230320/C360Q8E0/C360Q8E0.jpg">

    回 `[{"code", "img"}, ...]`。**没有标题** —— 卡片上只有编号、花回尺寸、
    价格；详情页 302 到 `join.aspx` 会员注册，补不到标题。
    """
    out = []
    for blk in txt.split('<li class="cbp-item designerli"')[1:]:
        mh = re.search(r"/preview\.aspx\?code=([A-Za-z0-9]+)", blk)
        mi = re.search(r"/Preview/(\d+)/([A-Za-z0-9]+)/([A-Za-z0-9]+)\.jpg", blk)
        if not (mh and mi):
            continue
        code = mh.group(1)
        # **编号/目录名/文件名三者必须一致**：最后一张卡的 block 会延续到文件尾，
        # 不校验的话紧跟其后的 `#yourlike` 推荐图会被当成这张卡的图装进来。
        if mi.group(2) != code or mi.group(3) != code:
            continue
        out.append({"code": code,
                    "img": "%s/Preview/%s/%s/%s.jpg" % (_YP_HOME, mi.group(1),
                                                        code, code)})
    return out


def _yp_fields(txt):
    """抓 PostBack 要回填的三个隐藏域（少一个服务端就回 500 空页）。"""
    out = {}
    for name in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"):
        m = re.search(r'id="%s"[^>]*value="([^"]*)"' % name, txt)
        out[name] = html.unescape(m.group(1)) if m else ""
    return out


def _ypattern(queries, per_query, key, opts):
    """源创花样网（ypattern.com）—— 原创花样交易平台，免登录。

    **为什么它跳过平铺闸门**（全项目唯一一家，`tile_opt=True`）：
      · 站方给的是 **400×300（4:3）预览**，而卡片上写的花回是 1:1
        （20 张里 12 张 4488×4488）—— **必被裁过**，拿到的不是整幅花回；
      · 实测 60 张候选 **0 张**过闸门，接缝比值 2.8~4.6（真平铺 ≈1）；
      · 这是站方防抄，改代码救不了。用户明确说"只要题材对得上就行"，
        所以按**线索图**收：索引里标 `tile_gate=off`，不混进"可平铺"那批。

    三条技术细节（都跟别家不一样）：
      1. **检索词必须 UTF-8** —— 按 GBK 编直接 HTTP 500。跟全球纺织网**正好相反**
         （hx 按 GBK 才对、按 UTF-8 是"静默回全库"）。
      2. **翻页是 ASP.NET WebForms 的 PostBack**：`?page=2` 完全不认。要把上一页的
         `__VIEWSTATE/__VIEWSTATEGENERATOR/__EVENTVALIDATION` 原样 POST 回去，
         `__EVENTTARGET=AspNetPager1`、`__EVENTARGUMENT=<页号>`。
         **只能一页页走，不能跳页、不能并发**。
      3. 一页 20 条，但同一张 HTML 里还有 20 张"您可能感兴趣的花型"
         （`#yourlike`）—— 只按 `/Preview/` 抓会多抓一倍且全是噪声。

    另：有的词直接 403（`孔雀` 稳定 403，`水墨/几何/大理石/碎花` 正常），
    像关键词过滤不是限流 → 非 200 一律当"这词没结果"，**别重试**。
    """
    rows = []
    cap = max(_YP_PER, min(_YP_PER * 10, int(per_query or 100)))
    pages = max(1, int(opts.get("yp_pages") or (cap + _YP_PER - 1) // _YP_PER))
    hdrs = {"Referer": _YP_HOME + "/", "Accept-Language": "zh-CN,zh;q=0.9"}
    timeout = opts.get("timeout", 30)

    def _one(word):
        u = _YP_LIST + "?" + urllib.parse.urlencode({"kw": word})
        try:
            status, _h, body = net.request(u, headers=hdrs, timeout=timeout)
        except Exception:                                    # noqa: BLE001
            return []
        if status != 200 or not body:      # 403 的词当没结果，别重试
            return []
        txt = body.decode("utf-8", "replace")
        got, seen = [], set()
        for i in range(pages):
            if i:                          # 第 2 页起只能 PostBack
                time.sleep(_YP_GAP)
                f = _yp_fields(txt)
                payload = urllib.parse.urlencode({
                    "__EVENTTARGET": "AspNetPager1",
                    "__EVENTARGUMENT": str(i + 1),
                    "__VIEWSTATE": f["__VIEWSTATE"],
                    "__VIEWSTATEGENERATOR": f["__VIEWSTATEGENERATOR"],
                    "__EVENTVALIDATION": f["__EVENTVALIDATION"],
                }).encode()
                post_hdrs = dict(hdrs)
                post_hdrs["Content-Type"] = "application/x-www-form-urlencoded"
                try:
                    status, _h, body = net.request(
                        u, method="POST", data=payload, headers=post_hdrs,
                        timeout=timeout)
                except Exception:                            # noqa: BLE001
                    break
                if status != 200 or not body:
                    break
                txt = body.decode("utf-8", "replace")
            before = len(got)
            for c in _yp_cards(txt):
                if c["code"] in seen:
                    continue
                seen.add(c["code"])
                got.append(_item(
                    c["img"], "", 400, 300, "",
                    "%s/preview.aspx?code=%s" % (_YP_HOME, c["code"]),
                    "ypattern", word, site="ypattern.com", tile_opt=True))
                if len(got) >= cap:
                    return got
            if len(got) == before:         # 这页一条新的都没有，后面更不会有
                break
        return got

    for q in queries:
        for cand in _yp_words(q):
            try:
                got = _one(cand)
            except Exception:                                # noqa: BLE001
                continue
            if got:
                rows.extend(got)
                time.sleep(_YP_GAP)
                break
    return rows


# ----------------------------------------------------------------- Patternbulk
# patternbulk.com：欧洲的**免版税花型交易平台**（在售设计 4000px / 300dpi 起）。
# 列表页是 Next.js 的服务端渲染，**设计对象整个塞在 RSC payload 里** ——
# 标题、25 个标签、原图尺寸、颜色、适用品类全都有，比任何搜索结果都干净。
_PBK_HOME = "https://patternbulk.com"
_PBK_STUDIO = _PBK_HOME + "/studio"
_PBK_MAX = 100               # `/studio` 固定回 100 条，翻页参数一律无效


def _pbk_unescape(seg):
    """RSC 里是 JS 字符串，字段被转义成 `\\"`、`\\u0026`。还原成正常 JSON 文本。"""
    return (seg.replace('\\"', '"').replace("\\u0026", "&")
               .replace("\\/", "/").replace("\\\\", "\\"))


def _pbk_designs(txt):
    """从 `/studio` 的 HTML 里挖出设计对象。

    形状（还原转义后）：
        {"id":"<uuid>","title":"Chinoiserie Botanical Bird Illustration…",
         "tags":["seamless","repeat","chinoiserie",…],
         "image":"pattern_<uuid>.jpg","folderId":"<uuid>","width":4134,…}

    ⚠️ `?q=` / `?page=2` / `?offset=` / `?search=` **全部无效**（实测：
    换任何参数都回同一批 100 条）—— 这站没有服务端搜索可用，
    所以只能"取一批 + 按标题/标签本地筛 + 交给模型判"。
    """
    i = txt.find('\\"designs\\":[')
    if i < 0:
        return []
    seg = _pbk_unescape(txt[i:])
    out = []
    for chunk in seg.split('{"id":"')[1:]:
        def f(pat):
            m = re.search(pat, chunk)
            return m.group(1) if m else ""
        did = f(r'^([0-9a-f-]{36})')
        title = f(r'"title":"(.*?)","description"')
        image = f(r'"image":"(pattern_[^"]+)"')
        folder = f(r'"folderId":"([0-9a-f-]{36})"')
        if not (did and image and folder):
            continue
        mt = re.search(r'"tags":\[(.*?)\]', chunk)
        tags = re.findall(r'"([^"]+)"', mt.group(1)) if mt else []
        out.append({"id": did, "title": title, "tags": tags,
                    "image": image, "folder": folder})
    return out


def _pbk_tokens(q):
    """检索词收拾成**英文关键词**（这站的标题/标签全是英文）。

    跟 patternbank 一样要剥 `site:` 限定（那是给通用搜索引擎的）；
    中文词在这站没用 —— 标题和标签都是英文，喂中文只会 0 命中。
    """
    out = []
    for w in re.sub(r"site:\S+", " ", q or "").replace(",", " ").split():
        w = w.strip().lower()
        if len(w) < 3 or not re.match(r"^[a-z][a-z\-']*$", w):
            continue
        if w in ("the", "and", "for", "with", "pattern", "patterns", "seamless",
                 "tileable", "repeat"):
            continue
        if w not in out:
            out.append(w)
    return out


def _pbk_pool(opts):
    """取一次 `/studio` 的 100 条设计（进程内缓存，一轮只请求一次）。"""
    cache = opts.get("_pbk_cache")
    if cache is not None:
        return cache
    try:
        status, _h, body = net.request(_PBK_STUDIO, timeout=opts.get("timeout", 30),
                                       headers={"Referer": _PBK_HOME + "/"})
        recs = _pbk_designs(body.decode("utf-8", "replace")) if status == 200 else []
    except Exception:                                        # noqa: BLE001
        recs = []
    opts["_pbk_cache"] = recs
    return recs


def _patternbulk(queries, per_query, key, opts):
    """Patternbulk —— 免版税花型交易平台，**取一批 + 本地筛 + 交给模型判**。

    加它的理由：
      · 设计对象里**自带标题和 25 个标签**（`seamless`/`chinoiserie`/`watercolor`/
        `botanical`…），等于站方替我们做了一次分类，比 alt 文本准得多；
      · 每条都写着原图尺寸（实测 4134×4134 / 300dpi），是能直接印的货；
      · 纯 SSR HTML、免 key 免登录，一个请求拿 100 条。

    ⚠️ 两个硬限制（所以**默认不跑**，要 `--provider patternbulk`）：
      · 缩略图只有 **266×266**（`thumb_pattern_*.jpg`，原图要买）→ 只能当线索；
      · **没有服务端搜索**：`?q=`/`?page=2`/`?search=` 全部无效，永远是同一批
        100 条。所以这里只能"按标题+标签本地筛"，筛不满就**拿一批让模型判**
        （用户明确要的用法）。命中的排前面，没命中的补到 12 条兜底。

    用法：`pick "法式 中国风 花卉" --provider patternbulk`
    """
    rows = []
    cap = max(1, min(_PBK_MAX, int(opts.get("pbk_cap") or 40)))
    pool = _pbk_pool(opts)
    if not pool:
        return rows

    tokens = []
    for q in queries:
        for t in _pbk_tokens(q):
            if t not in tokens:
                tokens.append(t)
    if not tokens:                     # 全中文需求：这站没有中文索引，别硬凑
        return rows

    scored = []
    for d in pool:
        title = (d.get("title") or "").lower()
        tags = {t.lower() for t in d.get("tags") or []}
        s = 0
        for t in tokens:
            if t in title:
                s += 2
            if any(t in tg for tg in tags):
                s += 1
        scored.append((s, d))
    scored.sort(key=lambda x: -x[0])

    hits = [d for s, d in scored if s > 0][:cap]
    if len(hits) < 12:                 # 筛不满就补一批让模型自己判（别空手）
        have = {d["id"] for d in hits}
        for _s, d in scored:
            if len(hits) >= 12:
                break
            if d["id"] not in have:
                hits.append(d)
                have.add(d["id"])

    word = tokens[0] if tokens else ""
    for d in hits:
        u = "%s/api/fetchFile?fileName=thumb_%s&folderName=uploaded_Designs/%s" % (
            _PBK_HOME, d["image"], d["folder"])
        rows.append(_item(
            u, "", 266, 266, d.get("title") or "",
            "%s/studio?design=%s" % (_PBK_HOME, d["id"]),
            "patternbulk", word, site="patternbulk.com"))
    return rows


# ---------------------------------------------------------------- 专业花型站
# 类：直接爬专业花型站（走 scrape.py 的 Scrapling 层，**可选依赖**）
#
# 和上面几家的区别：上面是"通用图片搜索"（搜回来的图只有两成是真平铺），
# 这一类是**整个站就是平铺花型设计** —— 平铺闸门的通过率会高一个量级。
# 代价是每家都要按它的 DOM 写解析，站一改版就得修（Scrapling 的 adaptive
# 能扛一部分，但结构大改还是得人来看）。所以**一家一个函数**，别搞通用爬虫。
def _pb_word(q):
    """把一条检索词收拾成能喂给 patternbank 的样子；不能用的回 ""。

    两件事（都是实测逼出来的）：
      · **剥掉 `site:` 限定**。analyze 生成的英文词常带 `site:pinterest.com`
        这类（那是给通用搜索引擎用的），直接喂给站内搜索会得到空结果。
      · **只要英文词**。中文「花卉」和「碎花」在这站上返回的是**同一份**
        "最新上传"列表（实测交集 22/22，标题里混着 texture-leaf、zig-zag），
        根本没按词过滤 —— 留着只会掺一堆无关图。
    """
    q = re.sub(r"site:\S+", " ", q or "")
    q = re.sub(r"[^\w\s\-]", " ", q).strip()
    if not q or _cjk(q):
        return ""
    words = q.split()
    # 词太多也是 0 条（和花瓣/堆糖一个毛病，只是没那么敏感），留 6 个够用
    return " ".join(words[:6])


def _pb_title(href, im):
    """设计名。img 的 alt 经常是空的，退回 URL 末尾那段 slug。

    /OrangeTheStudio/designs/464455300-soft-daisy-floral-on-deep-brown
      -> "soft daisy floral on deep brown"
    标题后面要进甄别和索引，不能留一堆数字 id。
    """
    t = (im.attrib.get("alt") or "").strip()
    if t:
        return t
    slug = (href or "").rstrip("/").rsplit("/", 1)[-1]
    slug = re.sub(r"^\d+[-_]?", "", slug)
    return slug.replace("-", " ").replace("_", " ").strip()


def _pb_cards(page, q, cap):
    """从搜索页里抠出「设计卡片」：链接带 /designs/ 的那些。

    为什么认 `/designs/` 而不是直接抓所有 img：搜索页上还挂着一大批
    "猜你喜欢"的图（实测 img 有 319 个，真结果只有 15~22 个），
    全收会把无关设计掺进来。
    """
    out, seen = [], set()
    for a in page.css("a"):
        href = a.attrib.get("href") or ""
        if "/designs/" not in href or href in seen:
            continue
        for im in a.css("img"):
            u = im.attrib.get("src") or im.attrib.get("data-src") or ""
            if "uploaded_files" not in u:               # 站里的图都走这个路径
                continue
            seen.add(href)
            out.append(_item(
                u, "", im.attrib.get("width"), im.attrib.get("height"),
                _pb_title(href, im),
                urllib.parse.urljoin("https://patternbank.com", href),
                "patternbank", q, site="patternbank.com"))
            break
        if cap and len(out) >= cap:
            break
    return out


def _patternbank(queries, per_query, key, opts):
    """Patternbank —— 专业花型 / 印花素材库（royalty-free seamless repeat print）。

    站上约 23 万个设计、18 个分类（Floral / Geometric / Paisleys / Tropical /
    Traditional / Animal Skins …），**图本身就是四方连续的印花设计**。

    实测要点（这几个数决定了它怎么用）：
      · **只认英文词**：floral→15 张、leopard→16 张、geometric→3 张，
        三个集合**交集为 0**（真按词搜）；中文词一律回默认列表。
      · **量小**：英文词一页 4~15 张，第 2 页就只剩 3 张。
        所以它是"小而精的补充"，别指望它凑数。
      · **要限速**：间隔 <2 秒连打，第 4~5 次开始回 403（scrape.GAP=3）。
      · **net.py 直连是 403**，必须走 Scrapling 的 TLS 指纹（见 scrape.py）。
    """
    if not scrape.have():
        return []                                       # 没装依赖：整家跳过，不报错
    rows = []
    page = max(1, int(opts.get("page") or 1))
    cap = _int(per_query) or 0
    for q in queries:
        word = _pb_word(q)
        if not word:
            continue
        url = "https://patternbank.com/search?q=%s" % urllib.parse.quote(word)
        if page > 1:
            url += "&page=%d" % page
        p = scrape.get(url, timeout=opts.get("timeout", 25))
        if p is None:
            continue                                    # 一家按词容错，不整批归零
        rows.extend(_pb_cards(p, word, cap))
        if cap and len(rows) >= cap:
            break
    return rows


def _spoonflower(queries, per_query, key, opts):
    """Spoonflower（spoonflower.com）—— 全球最大的印花面料定制平台。

    **能爬，而且站方明说可以。** 这点必须掰开讲，因为它很容易被误判：

        那个 403 是 Cloudflare 的**防滥用闸门**，不是"禁止抓取"。
        robots.txt 里写着 "Everyone else: crawlable"：
            User-agent: *
            Content-Signal: search=yes, ai-input=yes, ai-train=no
            Crawl-delay: 1
        `search=yes` 允许建索引，`ai-input=yes` 允许喂给 AI 做检索 ——
        我们做视觉甄别正是 ai-input；只禁 ai-train，我们本来也不训练。
        **判断"能不能爬"看 robots，不看状态码。**

    唯一的硬约束是 `Crawl-delay: 1`，而且它注明了违反视为保留
    EU 2019/790 第 4 条权利 —— 所以这个 1 秒必须真的等。

    实测要点（全是打出来的）：
      · 搜索页 `/en/shop?on=fabric&q=<词>`，**一页 121 张**，图就是印花设计本身，
        不是成品照 —— 平铺闸门通过率比搜索引擎高。
      · **必须 `solve_cloudflare=True`**，否则永远 403 "Just a moment…"。
      · **不要 `network_idle=True`**：这类站的请求永不空闲，会一直等到超时，
        实测整个进程被 SIGKILL(137)。用 `wait_selector="img"`。
      · 图**不走 CF**：`img.spoonflower.com` 直连可下（5/5）。
      · 直链尾部必须有 `/<id>.png`；少这段全 404（拿过 115 字符的"看起来对"的
        版本，全部 404，找了半天）。
      · 尺寸档位 `/p/f/s|m|l/`：s=100、m=294、**l=400**（xl/o/f 都退回 294）。
        最大只有 400px，所以它跟瓦栏一样是**线索级**，不是生产级。
      · `alt` 是真语义标题（"Among Flowers" / "English Chintz Blue Pink"），
        比瓦栏的"年轻女装印花花型图片"好得多。
      · **一页约 96 秒**（CF 挑战占 80 秒）。所以这家 `default=False`。

    只吃英文词：英文站，中文词上去是空的。
    """
    if not scrape.have_browser():
        return []
    base = "https://www.spoonflower.com"
    cap = max(10, min(120, int(per_query or 100)))

    words = []
    for q in queries:
        if _cjk(q):
            continue
        # **先剥 `site:` 再截词**。analyze 生成的英文词常带
        # `site:pinterest.com`（那是给通用搜索引擎的），这站是站内搜索，
        # 带着它去搜等于搜「site:pinterest.com French…」→ 0 条。
        # 2026-10-18 实测：不剥的话 Spoonflower 每一轮都交白卷，而且**不报错**，
        # 看起来像"这家没货"。patternbank 早有 `_pb_word` 剥，这里漏了。
        q = re.sub(r"site:\S+", " ", q or "")
        w = " ".join(q.split()[:6])
        if w and w not in words:
            words.append(w)
    if not words:
        return []

    rows = []
    with scrape.stealth_session() as sess:
        if sess is None:
            return []
        for w in words:
            u = "%s/en/shop?on=fabric&q=%s" % (base, urllib.parse.quote_plus(w))
            try:
                page = sess.fetch(u, wait_selector="img")
            except Exception:                                # noqa: BLE001
                continue                                     # 一家按词容错
            if page is None:
                continue
            got = []
            for im in page.css("img"):
                src = im.attrib.get("src") or ""
                if "img.spoonflower.com" not in src:
                    continue
                pid = re.search(r"/c/(\d+)/", src)
                if not pid:
                    continue
                # 换最大的一档（l=400）
                big = src.replace("/p/f/s/", "/p/f/l/").replace("/p/f/m/", "/p/f/l/")
                title = (im.attrib.get("alt") or "").strip()
                got.append(_item(big, src, 0, 0,
                                 title or ("spoonflower %s" % pid.group(1)),
                                 "%s/en/designs/%s" % (base, pid.group(1)),
                                 "spoonflower", w, site="spoonflower.com"))
                if len(got) >= cap:
                    break
            rows.extend(got)
            if cap and len(rows) >= cap:
                break
            time.sleep(1.0)                                  # robots: Crawl-delay 1
    return rows


# ---------------------------------------------------------------- 可直爬来源
# 2026-10-20 用户要求：把"一二档允许爬的"来源都收进来。
#
# 这里的站都是**查过 robots / 实际响应**才加的；不是"看到有图就爬"：
#   · Designspiration：搜索页 200，robots 只禁 /image/browse/、/resource/*
#   · Openclipart：robots 只有 sitemap，页面 200，公共领域
#   · Irasutoya：robots 为空，Blogger feed 200；素材授权另有条款
#   · Print & Pattern：robots 禁 /search，用 Blogger feed（不是 /search）
#   · Pattern Observer：robots 全允许，WordPress REST 200
#   · PublicDomainPictures / PxHere / StockSnap：robots 允许；Cloudflare
#     直连 403，走 Scrapling（同 Spoonflower 的结论：403 不等于禁止）
#   · Rawpixel：Content-Signal 允许 search/use=reference，但页面是 JS/CF，
#     当前 Scrapling 拿不到，先**不收录**，免得每轮空跑。
#   · Savee / Cosmos：robots 允许页面/sitemap，但明确禁 /search、/api、
#     带 query 的 URL；**没有关键词搜索**，不适合按需求检索，先不接。
_EN_TERMS = (
    ("水墨画", "ink wash painting"), ("水墨", "ink painting"), ("国风", "chinoiserie"),
    ("中国风", "chinese style"), ("和风", "japanese style"), ("日式", "japanese"),
    ("花型", "pattern"), ("图案", "pattern"), ("纹样", "pattern"), ("底纹", "pattern"),
    ("无缝", "seamless"), ("平铺", "tileable"), ("四方连续", "repeat"),
    ("印花", "print"), ("面料", "fabric"), ("壁纸", "wallpaper"),
    ("花卉", "floral"), ("花朵", "flower"), ("植物", "botanical"),
    ("卡通", "cartoon"), ("水彩", "watercolor"), ("童装", "kids clothing"),
    ("儿童", "kids"), ("动物", "animal"), ("复古", "vintage"),
    ("牡丹", "peony"), ("梅花", "plum blossom"), ("兰花", "orchid"),
    ("竹子", "bamboo"), ("菊花", "chrysanthemum"), ("荷花", "lotus"),
)


def _en_query(q, limit=10):
    """给英文站用：把中文需求翻成几个英文核心词，再保留原英文词。

    为什么不是简单删中文：删完只剩 "seamless pattern"，所有需求都变成
    同一个搜索；Patternbank / Designspiration 这种站上会得到完全无关的图。

    limit 按站给：站点搜索多是 AND，10 个词上去直接 0 条。
    """
    q = re.sub(r"site:\S+", " ", q or "")
    bits = []
    for zh, en in _EN_TERMS:
        if zh in q and en not in bits:
            bits.append(en)
    for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-']*", q):
        if w.lower() not in {b.lower() for b in bits}:
            bits.append(w)
    return " ".join(bits[:limit])


def _html(url, timeout=25, max_bytes=3 * 1024 * 1024):
    try:
        _st, _hd, body = net.request(url, timeout=timeout, max_bytes=max_bytes)
        return body.decode("utf-8", "replace")
    except Exception:                                       # noqa: BLE001
        return ""


def _designspiration(queries, per_query, key, opts):
    """Pinterest-like 设计灵感站；搜索页是服务端渲染，图在 dspncdn.com。

    robots 禁的是 `/image/browse/` 和 `/resource/*`，`/search/` 没禁。
    图档只有 236x/472x 两档（736x 是 404），所以它是**线索级**。
    """
    rows, seen = [], set()
    for q in queries:
        en = _en_query(q, 5)
        if not en:
            continue
        url = "https://designspiration.net/search/?q=" + urllib.parse.quote_plus(en)
        text = _html(url, timeout=opts.get("timeout", 25))
        if not text:
            continue
        for tag in re.findall(r"<img[^>]+>", text):
            if "gridItemContent" not in tag:
                continue
            m = re.search(r'src="([^"]+)"', tag)
            if not m:
                continue
            u = html.unescape(m.group(1))
            if "dspncdn.com" not in u:
                continue
            u = u.replace("/236x/", "/472x/")
            if u in seen:
                continue
            seen.add(u)
            rows.append(_item(u, u, 472, 0, "", url, "designspiration", en,
                              site="designspiration.net", kind="community",
                              quality=0.62, license="unknown", method="html"))
            if per_query and len(rows) >= per_query:
                return rows
    return rows


def _openclipart(queries, per_query, key, opts):
    """Openclipart：公共领域 clipart，搜卡通/童装元素很对路。

    页面是服务端渲染的，图 `/image/200px/<id>`，升级到 800px。
    Openclipart 是 AND 搜索，词多了 0 条；3 个词空就退到第一个词。
    """
    rows, seen = [], set()
    for q in queries:
        en = _en_query(q, 3)
        if not en:
            continue
        variants = [en]
        first = en.split()[0]
        if first and first != en:
            variants.append(first)
        for variant in variants:
            url = "https://openclipart.org/search/?query=" + urllib.parse.quote_plus(variant)
            text = _html(url, timeout=opts.get("timeout", 25))
            for m in re.finditer(r'<img[^>]+src="(/image/(?:200|400)px/(\d+))"[^>]*alt="([^"]*)"', text):
                cid = m.group(2)
                if cid in seen:
                    continue
                seen.add(cid)
                src = "https://openclipart.org/image/800px/%s" % cid
                rows.append(_item(src, "https://openclipart.org/image/200px/%s" % cid,
                                  800, 0, html.unescape(m.group(3)),
                                  "https://openclipart.org/detail/%s" % cid,
                                  "openclipart", variant, site="openclipart.org",
                                  kind="illustration", quality=0.52,
                                  license="public_domain", method="html"))
                if per_query and len(rows) >= per_query:
                    return rows
            if len(rows) > 0:
                break
    return rows


def _blogger_feed(host, q, max_results=20, timeout=25):
    url = ("https://%s/feeds/posts/default?q=%s&alt=json&max-results=%d"
           % (host, urllib.parse.quote_plus(q), max_results))
    try:
        return net.get_json(url, timeout=timeout)
    except Exception:                                       # noqa: BLE001
        return {}


def _feed_rows(host, site, provider, queries, per_query, *, quality, kind,
               license="unknown", upgrade=("/s72-c/", "/s1600/"),
               timeout=25):
    rows, seen = [], set()
    for q in queries:
        en = _en_query(q, 2)
        if not en:
            continue
        d = _blogger_feed(host, en, timeout=timeout)
        entries = (d.get("feed") or {}).get("entry") or []
        if not entries and " " in en:
            d = _blogger_feed(host, en.split()[0], timeout=timeout)
            entries = (d.get("feed") or {}).get("entry") or []
        for e in entries:
            th = ((e.get("media$thumbnail") or {}).get("url") or "")
            if not th:
                continue
            u = th.replace(upgrade[0], upgrade[1])
            if u in seen:
                continue
            seen.add(u)
            page = ""
            for l in e.get("link") or []:
                if (l.get("rel") or "") == "alternate":
                    page = l.get("href") or ""
            title = (e.get("title") or {}).get("$t") or ""
            rows.append(_item(u, th, 0, 0, title, page or ("https://%s/" % host),
                              provider, en, site=site, kind=kind,
                              quality=quality, license=license, method="feed"))
            if per_query and len(rows) >= per_query:
                return rows
    return rows


def _irasutoya(queries, per_query, key, opts):
    """いらすとや：日系免费可爱插画；Blogger feed 搜索（robots 为空）。"""
    return _feed_rows("www.irasutoya.com", "irasutoya.com", "irasutoya",
                      queries, per_query, quality=0.55, kind="illustration",
                      license="irasutoya_terms", timeout=opts.get("timeout", 25))


def _printpattern(queries, per_query, key, opts):
    """Print & Pattern：多年印花/图案趋势博客；用 Blogger feed，不碰 /search。"""
    return _feed_rows("printpattern.blogspot.com", "printpattern.blogspot.com",
                      "printpattern", queries, per_query, quality=0.68,
                      kind="community", license="unknown",
                      timeout=opts.get("timeout", 25))


def _poptnc(queries, per_query, key, opts):
    """POP花型网（poptnc.com）—— WordPress，REST API 可搜、可分页、直出图。

    robots 只禁 `/wp-admin/`，REST/文章页都允许；比 fzthinking 好接。
    `wp-json/wp/v2/posts?search=...&_embed=1` 返回标题/日期/特色图，
    图在 img.poptnc.com，直链 200。
    """
    rows, seen = [], set()
    page = max(1, int(opts.get("page") or 1))
    n = max(1, min(100, int(per_query or 20)))
    for q in queries:
        q = re.sub(r"site:\S+", " ", q or "").strip()
        if not q:
            continue
        url = ("https://www.poptnc.com/wp-json/wp/v2/posts"
               "?search=%s&per_page=%d&page=%d&_embed=1" %
               (urllib.parse.quote_plus(q), n, page))
        try:
            d = net.get_json(url, timeout=opts.get("timeout", 25))
        except Exception:                                   # noqa: BLE001
            continue
        if not isinstance(d, list):
            continue
        for e in d:
            media = (((e.get("_embedded") or {}).get("wp:featuredmedia") or [{}])[0])
            u = media.get("source_url") or ""
            if not u:
                # 没有特色图就退回正文里的第一张
                m = re.search(r'https?://[^"\s]+?\.(?:jpg|jpeg|png|webp)(?:\?[^"\s]*)?',
                              (e.get("content") or {}).get("rendered") or "", re.I)
                u = m.group(0) if m else ""
            if not u or u in seen:
                continue
            seen.add(u)
            title = html.unescape(re.sub(r"<[^>]+>", "",
                                         (e.get("title") or {}).get("rendered") or ""))
            rows.append(_item(u, u, 0, 0, title, e.get("link") or "",
                              "poptnc", q, site="poptnc.com",
                              kind="marketplace", quality=0.80,
                              license="unknown", method="api",
                              created_at=e.get("date") or ""))
            if per_query and len(rows) >= per_query:
                return rows
    return rows


def _patternobserver(queries, per_query, key, opts):
    """Pattern Observer：WordPress REST 搜索；取 featured media。

    robots 全允许；比爬 HTML 稳，标题/日期/页面都结构化。
    """
    rows = []
    for q in queries:
        en = _en_query(q, 4)
        if not en:
            continue
        url = ("https://www.patternobserver.com/wp-json/wp/v2/posts"
               "?search=%s&per_page=%d&_embed=1" %
               (urllib.parse.quote_plus(en), min(50, int(per_query or 20))))
        try:
            d = net.get_json(url, timeout=opts.get("timeout", 25))
        except Exception:                                   # noqa: BLE001
            continue
        for e in d if isinstance(d, list) else []:
            media = (((e.get("_embedded") or {}).get("wp:featuredmedia") or [{}])[0])
            u = media.get("source_url") or ""
            if not u:
                continue
            rows.append(_item(u, u, 0, 0, html.unescape((e.get("title") or {}).get("rendered") or ""),
                              e.get("link") or "", "patternobserver", en,
                              site="patternobserver.com", kind="community",
                              quality=0.62, license="unknown",
                              created_at=e.get("date") or "", method="api"))
            if per_query and len(rows) >= per_query:
                return rows
    return rows


def _cloudflare_cards(url, host_filter, provider, site, q, quality, license,
                      timeout=25, cap=0, kind="stock"):
    """Scrapling 取 Cloudflare 站，抓 img。只留本站 CDN 的图，别把广告收进来。"""
    p = scrape.get(url, timeout=timeout)
    if p is None:
        return []
    out, seen = [], set()
    for im in p.css("img"):
        u = im.attrib.get("src") or im.attrib.get("data-src") or ""
        if not u:
            continue
        u = urllib.parse.urljoin("https://" + site, u)
        if host_filter not in u:
            continue
        if u in seen:
            continue
        seen.add(u)
        out.append(_item(u, u, 0, 0, (im.attrib.get("alt") or "").strip(),
                         url, provider, q, site=site, kind=kind,
                         quality=quality, license=license, method="html"))
        if cap and len(out) >= cap:
            break
    return out


def _publicdomainpictures(queries, per_query, key, opts):
    rows = []
    for q in queries:
        en = _en_query(q, 3)
        if not en:
            continue
        for variant in [en] + ([en.split()[0]] if " " in en else []):
            url = ("https://www.publicdomainpictures.net/en/hledej.php?hleda="
                   + urllib.parse.quote_plus(variant))
            got = _cloudflare_cards(url, "publicdomainpictures.net",
                                    "publicdomainpictures", "publicdomainpictures.net",
                                    variant, 0.50, "cc0",
                                    timeout=opts.get("timeout", 25),
                                    cap=per_query or 0)
            rows.extend(got)
            if got:
                break
        if per_query and len(rows) >= per_query:
            break
    return rows


def _pxhere(queries, per_query, key, opts):
    rows = []
    for q in queries:
        en = _en_query(q, 2)
        if not en:
            continue
        for variant in [en] + ([en.split()[0]] if " " in en else []):
            url = "https://pxhere.com/en/photos?q=" + urllib.parse.quote_plus(variant)
            got = _cloudflare_cards(url, "c.pxhere.com/photos/", "pxhere",
                                    "pxhere.com", variant, 0.42, "cc0",
                                    timeout=opts.get("timeout", 25),
                                    cap=per_query or 0)
            rows.extend(got)
            if got:
                break
        if per_query and len(rows) >= per_query:
            break
    return rows


def _stocksnap(queries, per_query, key, opts):
    rows = []
    for q in queries:
        en = _en_query(q, 2)
        if not en:
            continue
        for variant in [en] + ([en.split()[0]] if " " in en else []):
            url = "https://stocksnap.io/search/" + urllib.parse.quote_plus(variant)
            got = _cloudflare_cards(url, "cdn.stocksnap.io/", "stocksnap",
                                    "stocksnap.io", variant, 0.42, "cc0",
                                    timeout=opts.get("timeout", 25),
                                    cap=per_query or 0)
            rows.extend(got)
            if got:
                break
        if per_query and len(rows) >= per_query:
            break
    return rows


# --------------------------------------------------------------------- search
# 类：图片搜索代理 —— 真的全网
def _serper(queries, per_query, key, opts):
    """Serper.dev（Google 图片搜索代理）。POST，X-API-KEY 头。

    实测要点：
      · num 单次上限 100，一个 query 一次就能把 100 张捞齐（1 credit）；
      · gl/hl 决定区域和语言 —— 中文站点要 gl=cn & hl=zh-cn，
        否则搜"花型平铺"出来的全是英文站；
      · imageUrl 是原图直链，thumbnailUrl 是 Google 的缩略图代理
        （图床挡盗链时，缩略图反而更稳，所以两个都存）。
    """
    rows = []
    for q in queries:
        body = {"q": q, "num": min(100, per_query),
                "gl": opts.get("gl") or "cn", "hl": opts.get("hl") or "zh-cn"}
        if opts.get("page") and int(opts["page"]) > 1:
            body["page"] = int(opts["page"])
        j = net.post_json("https://google.serper.dev/images", body,
                          headers={"X-API-KEY": key}, timeout=opts.get("timeout", 25))
        for it in (j.get("images") or []):
            rows.append(_item(it.get("imageUrl"), it.get("thumbnailUrl"),
                              it.get("imageWidth"), it.get("imageHeight"),
                              it.get("title"), it.get("link"), "serper", q))
    return rows


def _serpapi(queries, per_query, key, opts):
    """SerpAPI（Google 图片搜索代理）。GET，key 走 query。

    要点：
      · engine=google_images；`original` 字段才是原图直链；
      · 翻页参数是 `ijn`（不是 page），每页 100；
      · 免费额度只有 100 次/月，比 Serper 贵很多，所以默认不选它。
    """
    rows = []
    for q in queries:
        params = {"engine": "google_images", "q": q, "api_key": key,
                  "num": min(100, per_query),
                  "hl": opts.get("hl") or "zh-cn", "gl": opts.get("gl") or "cn"}
        if opts.get("page") and int(opts["page"]) > 1:
            params["ijn"] = int(opts["page"])
        j = net.get_json("https://serpapi.com/search.json", params=params,
                         timeout=opts.get("timeout", 30))
        for it in (j.get("images_results") or []):
            rows.append(_item(it.get("original"), it.get("thumbnail"),
                              it.get("original_width"), it.get("original_height"),
                              it.get("title"), it.get("link"), "serpapi", q))
    return rows


def _brave(queries, per_query, key, opts):
    """Brave Search API 的图片接口。GET，X-Subscription-Token 头。

    要点：图直链藏在 properties.url 里；count 单次上限 100。
    免费档 1 次/秒、2000 次/月 —— 查询之间要有点间隔（下面 fetch.py 的
    限速是给图床用的，这家的节流交给调用方，我们不并发打它）。
    """
    rows = []
    for q in queries:
        params = {"q": q, "count": min(100, per_query),
                  "country": opts.get("country") or "cn",
                  "search_lang": opts.get("search_lang") or "zh-hans"}
        j = net.get_json("https://api.search.brave.com/res/v1/images/search",
                         headers={"X-Subscription-Token": key, "Accept": "application/json"},
                         params=params, timeout=opts.get("timeout", 25))
        for it in (j.get("results") or []):
            prop = it.get("properties") or {}
            th = it.get("thumbnail") or {}
            rows.append(_item(prop.get("url"),
                              prop.get("placeholder") or th.get("src"),
                              0, 0, it.get("title"), it.get("url"), "brave", q))
    return rows


# ---------------------------------------------------------------------- stock
# 类：图库站官方 API —— 干净但不是全网
def _pexels(queries, per_query, key, opts):
    """Pexels 官方 API。GET，Authorization 头。per_page 上限 80。"""
    rows = []
    for q in queries:
        j = net.get_json("https://api.pexels.com/v1/search",
                         headers={"Authorization": key},
                         params={"query": q, "per_page": min(80, per_query),
                                 "page": int(opts.get("page") or 1)},
                         timeout=opts.get("timeout", 25))
        for it in (j.get("photos") or []):
            src = it.get("src") or {}
            rows.append(_item(src.get("original"), src.get("medium"),
                              it.get("width"), it.get("height"),
                              it.get("alt") or "", it.get("url"), "pexels", q))
    return rows


def _pixabay(queries, per_query, key, opts):
    """Pixabay 官方 API。GET，key 走 query。per_page 上限 200。

    要点：largeImageURL 默认带水印？—— 不带，但**要求署名**（免费商用需
    注明来源）；largeImageWidth/Height 才是它的真实尺寸（不是 imageWidth）。
    """
    rows = []
    for q in queries:
        j = net.get_json("https://pixabay.com/api/",
                         params={"key": key, "q": q, "image_type": "photo",
                                 "per_page": min(200, per_query),
                                 "page": int(opts.get("page") or 1),
                                 "safesearch": "true"},
                         timeout=opts.get("timeout", 25))
        for it in (j.get("hits") or []):
            rows.append(_item(it.get("largeImageURL") or it.get("imageURL"),
                              it.get("webformatURL"),
                              it.get("largeImageWidth") or it.get("imageWidth"),
                              it.get("largeImageHeight") or it.get("imageHeight"),
                              it.get("tags"), it.get("pageURL"), "pixabay", q))
    return rows


def _unsplash(queries, per_query, key, opts):
    """Unsplash 官方 API。GET，Authorization: Client-ID 头。per_page 上限 30。

    要点：urls.raw 是**原图**（很大，几十 MB 级别），urls.regular 是
    1080 宽 —— 花型图用 regular 足够且下载快得多，所以这里取 regular。
    """
    rows = []
    for q in queries:
        j = net.get_json("https://api.unsplash.com/search/photos",
                         headers={"Authorization": "Client-ID %s" % key,
                                  "Accept-Version": "v1"},
                         params={"query": q, "per_page": min(30, per_query),
                                 "page": int(opts.get("page") or 1)},
                         timeout=opts.get("timeout", 25))
        for it in (j.get("results") or []):
            urls = it.get("urls") or {}
            rows.append(_item(urls.get("regular") or urls.get("full"),
                              urls.get("small"), it.get("width"), it.get("height"),
                              it.get("alt_description") or "",
                              (it.get("links") or {}).get("html"),
                              "unsplash", q))
    return rows


# 注册表。home = 申请 key 的地方（给人看的，probe 会把这一列打出来）。
# kind 决定默认排序权重：free/search 是真全网，stock 是补充。
# keyless=True 的来源**不需要 key、永远可用**（百度/360）。
# Clip Studio 是日文站：中文/英文检索词直接丢进去经常 0 条。
# 做一层小词表映射（不追求翻译质量，够把"花柄/パターン/水彩"这类核心词送进去）。
_JA_MAP = {
    "小碎花": "小花柄", "碎花": "小花柄", "花卉": "花柄", "花型": "パターン",
    "图案": "パターン", "平铺": "パターン", "无缝": "シームレス", "四方连续": "リピート",
    "水彩": "水彩", "复古": "レトロ", "国风": "和風", "中国风": "中華風",
    "青花": "染付", "牡丹": "牡丹", "玫瑰": "バラ", "樱花": "桜", "梅花": "梅",
    "兰花": "蘭", "竹子": "竹", "菊花": "菊", "荷花": "蓮", "郁金香": "チューリップ",
    "向日葵": "ひまわり", "山茶": "椿", "玉兰": "木蓮", "海棠": "海棠",
    "叶子": "葉", "植物": "植物", "线条": "線画", "手绘": "手描き",
    "矢量": "ベクター", "壁纸": "壁紙", "面料": "生地", "印花": "プリント",
    "卡通": "かわいい", "童装": "子供服", "儿童": "子供", "可爱": "かわいい",
    "动漫": "アニメ", "动物": "動物", "小熊": "くま", "兔子": "うさぎ",
    "floral": "花柄", "flower": "花", "pattern": "パターン", "seamless": "シームレス",
    "watercolor": "水彩", "botanical": "植物", "vintage": "レトロ", "leaf": "葉",
    "chinoiserie": "中華風", "ditsy": "小花柄", "repeat": "リピート",
}


def _ja_queries(q):
    """把一条中文/英文检索词尽量变成 Clip Studio 能命中的日文词。"""
    toks = [x for x in re.split(r"[\s,，、/|]+", (q or "").strip()) if x]
    out, seen = [], set()
    for tok in toks:
        ja = _JA_MAP.get(tok) or _JA_MAP.get(tok.lower())
        if ja:
            if ja not in seen:
                seen.add(ja); out.append(ja)
        elif re.search(r"[\u3040-\u30ff\u4e00-\u9fffA-Za-z]", tok):
            if tok not in seen:
                seen.add(tok); out.append(tok)
    if not out:
        out = ["花柄"]
    # 只取前 4 个词，长查询在日文站反而 0 条
    return [" ".join(out[:4])]


def _clip_studio(queries, per_query, key, opts):
    """CLIP STUDIO ASSETS（日本插画素材平台）的公开搜索 JSON。

    2026-10-19 逆向：搜索页用 `Search.SearchAPI` 打
        GET https://assets.clip-studio.com/ja-jp/search/ajax
            ?word=<词>&order=dl|cnew|new&cursor=<游标>
    返回 `response_data.data[]`：material_id / name / thumbnail_url /
    purchase_count / clip_user_handle / material_type_name…
    缩略图在 S3，直链可下（不需要 Referer）。原图要登录/购买，这里只取缩略图
    当线索；平铺闸门和语义甄别会再筛一道。

    为什么值得接：日本花型/图案素材密度高，且 `purchase_count` 是现成的
    质量/热度信号（rank 会用它当 engagement）。
    """
    rows = []
    base = "https://assets.clip-studio.com/ja-jp/search/ajax"
    order = str(opts.get("order") or "dl")
    pages = max(1, min(3, int(opts.get("pages") or 2)))
    per_query = max(1, min(200, int(per_query or 100)))
    timeout = int(opts.get("timeout") or 25)
    # 日文站：中文/英文词先过一遍小词表；只取前 8 条，别把一轮拖慢
    ja_queries = []
    for q in queries[:8]:
        for jq in _ja_queries(q):
            if jq and jq not in ja_queries:
                ja_queries.append(jq)
    for q in ja_queries:
        cursor = None
        got = 0
        for _p in range(pages):
            params = {"word": q, "order": order}
            if cursor:
                params["cursor"] = cursor
            try:
                j = net.get_json(base, params=params,
                                 headers={"X-Requested-With": "XMLHttpRequest",
                                          "Accept": "application/json"},
                                 timeout=timeout)
            except Exception:                                # noqa: BLE001
                break
            data = j.get("response_data") or {}
            for it in data.get("data") or []:
                mid = str(it.get("material_id") or "")
                thumb = it.get("thumbnail_url") or ""
                if not mid or not thumb:
                    continue
                # 缩略图 URL 上的 `?<ts>` 是站方生成时间，当 created_at 用（不精确）
                ts = ""
                m = re.search(r"[?&](\d{10})\b", thumb)
                if m:
                    ts = m.group(1)
                try:
                    eng = int(str(it.get("purchase_count") or "0"))
                except Exception:                            # noqa: BLE001
                    eng = None
                rows.append(_item(
                    thumb, thumb, 0, 0,
                    title=str(it.get("name") or it.get("translatedName") or ""),
                    page="https://assets.clip-studio.com/ja-jp/detail?id=%s" % mid,
                    provider="clip_studio", query=q,
                    site="assets.clip-studio.com",
                    ref="https://assets.clip-studio.com/",
                    created_at=ts, author=str(it.get("clip_user_handle") or ""),
                    domain="assets.clip-studio.com", kind="illustration",
                    quality=0.82, license="clip_studio_terms",
                    engagement=eng, method="feed"))
                got += 1
                if got >= per_query:
                    break
            cursor = data.get("cursor")
            if got >= per_query or not cursor or data.get("isPageReaching"):
                break
    return rows


def _site_search(queries, per_query, key, opts):
    """精选站搜索：用搜索代理的 `site:<domain>` 拿指定站里的图。

    支持 Serper（首选）/ SerpAPI / Brave，任意一把 key 都能用：
      · Serper: POST https://google.serper.dev/images，X-API-KEY
      · SerpAPI: GET https://serpapi.com/search.json?engine=google_images
      · Brave: GET https://api.search.brave.com/res/v1/images/search

    为什么不是直接抓 Etsy/Creative Market/Behance：
      这些站大多有 Cloudflare/DataDome/登录墙，直连 403；而且条款未必允许。
      用 Google 的 `site:` 索引拿"这个站里被收录的图"，和"自己爬站"不是一回事，
      也不碰目标站的 robots。代价是要一把搜索代理 key。

    默认不跑（REGISTRY default=False）。用法：
        --provider site_search --sites etsy,creativemarket,behance
    或按意图自动取：
        --provider site_search --intent business
    """
    names = None
    if opts.get("sites"):
        names = [x.strip() for x in str(opts["sites"]).replace("，", ",").split(",") if x.strip()]
    domains = opts.get("site_domains")
    if not domains:
        domains = _sources.site_domains(opts.get("intent"),
                                        limit=int(opts.get("site_limit") or 4),
                                        names=names)
    domains = [d for d in (domains or []) if d][:8]
    if not domains:
        return []
    e = os.environ
    engine = "serper"
    if not (e.get("WSEARCH_SERPER_KEY") or "").strip():
        if (e.get("WSEARCH_SERPAPI_KEY") or "").strip():
            engine = "serpapi"
        elif (e.get("WSEARCH_BRAVE_KEY") or "").strip():
            engine = "brave"
    rows = []
    per_domain = max(10, int(per_query or 100) // max(1, len(domains)))
    timeout = int(opts.get("timeout") or 25)
    for q in queries:
        q = (q or "").strip()
        if not q:
            continue
        zh = _cjk(q)
        for dom in domains:
            prov = "site:" + dom
            kind = _sources.kind(prov)
            quality = _sources.prior(prov)
            items = []
            try:
                if engine == "serpapi":
                    j = net.get_json(
                        "https://serpapi.com/search.json",
                        params={"engine": "google_images",
                                "q": "site:%s %s" % (dom, q),
                                "api_key": key, "num": min(100, per_domain),
                                "hl": "zh-cn" if zh else "en",
                                "gl": "cn" if zh else "us"},
                        timeout=timeout)
                    for it in (j.get("images_results") or []):
                        items.append((it.get("original"), it.get("thumbnail"),
                                      it.get("original_width"), it.get("original_height"),
                                      it.get("title"), it.get("link")))
                elif engine == "brave":
                    j = net.get_json(
                        "https://api.search.brave.com/res/v1/images/search",
                        headers={"X-Subscription-Token": key,
                                 "Accept": "application/json"},
                        params={"q": "site:%s %s" % (dom, q),
                                "count": min(100, per_domain),
                                "country": "cn" if zh else "us",
                                "search_lang": "zh-hans" if zh else "en"},
                        timeout=timeout)
                    for it in (j.get("results") or []):
                        prop = it.get("properties") or {}
                        items.append((prop.get("url"), it.get("thumbnail"),
                                      it.get("width"), it.get("height"),
                                      it.get("title"), it.get("page_url")))
                else:
                    body = {"q": "site:%s %s" % (dom, q), "num": min(100, per_domain),
                            "gl": "cn" if zh else "us",
                            "hl": "zh-cn" if zh else "en"}
                    j = net.post_json("https://google.serper.dev/images", body,
                                      headers={"X-API-KEY": key}, timeout=timeout)
                    for it in (j.get("images") or []):
                        items.append((it.get("imageUrl"), it.get("thumbnailUrl"),
                                      it.get("imageWidth"), it.get("imageHeight"),
                                      it.get("title"), it.get("link")))
            except Exception:                                # noqa: BLE001
                continue
            for img, thumb, w, h, title, page in items:
                if not img:
                    continue
                rows.append(_item(
                    img, thumb, w, h, title, page, prov, q,
                    site=dom, domain=dom, kind=kind, quality=quality,
                    license="unknown", method="search_index"))
    return rows


REGISTRY = {
    "baidu": {"label": "百度图片", "kind": "search", "fn": _baidu,
              "home": "https://image.baidu.com/", "env": ENV["baidu"],
              "keyless": True,
              "note": "**免 key**。中文花型素材最全，一次 60 条。默认首选。"},
    "so360": {"label": "360 图片", "kind": "search", "fn": _so360,
              "home": "https://image.so.com/", "env": ENV["so360"],
              "keyless": True,
              "note": "**免 key、免 cookie**，直出原图直链。量比百度小。"},
    "bing": {"label": "必应图片", "kind": "search", "fn": _bing,
             "home": "https://cn.bing.com/images/", "env": ENV["bing"],
             "keyless": True, "default": False,
             "note": "**免 key**，但**默认不跑**：连打十几次后会开始回不相关的结果集"
                     "（实测搜「碎花平铺素材」出来的是绿篱机/火车站），一次跑十条"
                     "词必然触发。想试就加 --provider bing。"},
    "huaban": {"label": "花瓣", "kind": "search", "fn": _huaban,
               "home": "https://huaban.com/", "env": ENV["huaban"],
               "keyless": True,
               "note": "**免 key**。国内设计素材最对路，但只认中文检索词。"},
    "duitang": {"label": "堆糖", "kind": "search", "fn": _duitang,
                "home": "https://www.duitang.com/", "env": ENV["duitang"],
                "keyless": True,
                "note": "**免 key**。壁纸/素材社区，量很大，只认中文检索词。"},
    "walan": {"label": "瓦栏（印花花型平台）", "kind": "search", "fn": _walan,
              "home": "https://www.walanwalan.com/", "env": ENV["walan"],
              "keyless": True,
              "note": "**免 key、免登录**。国内印花花型交易平台，搜的是**在售花型**，"
                      "一页 100 张。只取列表页公开缩略图（300×450）当线索，"
                      "原图要买 —— 索引里留了详情页链接。"},
    "dancecolor": {"label": "色彩跳动（花型版权平台）", "kind": "search",
                   "fn": _dancecolor, "home": "https://www.dancecolor.net/",
                   "env": ENV["dancecolor"], "keyless": True,
                   "note": "**免 key、免登录、免浏览器**。国内花型版权登记/交易平台，"
                           "搜的就是**在售印花花型**，一个接口回 100 条 JSON，"
                           "中文词原生就吃、单张带尺寸和适用面料。"
                           "只取站方图片接口出的预览图（长边 480）当线索，"
                           "原图要授权 —— 索引里留了详情页链接。"},
    "tnc": {"label": "全球纺织网·花样画稿市场", "kind": "search", "fn": _hx,
            "home": _HX_HOME + "/", "env": ENV["tnc"], "keyless": True,
            "note": "**免 key、免登录**。全球纺织网的花样画稿市场，在国内花型交易"
                    "平台里**图最清楚的一家：600×900 无水印**（瓦栏 300×450、"
                    "色彩跳动长边 480）。卡片自带结构化标签（四方连续-花卉／"
                    "中国风-水墨），一页 24 条、能一直翻。只取列表页公开缩略图"
                    "当线索，原图要买 —— 索引里留了详情页链接。"},
    "patterndesigns": {"label": "Pattern Designs（矢量无缝图案）", "kind": "search",
                       "fn": _patterndesigns, "home": _PD_HOME + "/zh",
                       "env": ENV["patterndesigns"], "keyless": True,
                       "note": "**免 key、免登录、免浏览器**，公开 JSON 分页接口一次 60 条、"
                               "700×700。**2026-10-18 用户要求开成默认**（本来因为"
                               "①`gallery-m` 那档**带满屏水印**；②整站是**硬边矢量**，"
                               "对「水墨/国风」一条都对不上；③中文标题是机翻，语义会漂"
                               "被放进了手动档）。开着跑就得接受这三条，"
                               "**按具体花名找**（牡丹/玫瑰/樱花/竹）命中很准。"},
    "ypattern": {"label": "源创花样网（原创花样交易）", "kind": "search",
                 "fn": _ypattern, "home": _YP_HOME + "/",
                 "env": ENV["ypattern"], "keyless": True, "default": False,
                 "note": "**免 key、免登录**。国内原创花样交易平台，`水墨` 自己就有"
                         "582 页，题材命中率高。但**默认不跑**：预览是站方裁过的"
                         "**400×300（4:3）**，花回多为 1:1 → **铺不开**，所以它是"
                         "全项目唯一 `tile_opt` 的来源（索引里标 `tile_gate=off`）。"
                         "取的是**线索图**，别当可平铺素材用。想用加 --provider ypattern。"},
    "patternbulk": {"label": "Patternbulk（免版税花型交易）", "kind": "search",
                    "fn": _patternbulk, "home": _PBK_HOME + "/studio",
                    "env": ENV["patternbulk"], "keyless": True, "default": False,
                    "note": "**免 key、免登录**，设计对象自带**标题 + 25 个标签**"
                            "（seamless/chinoiserie/watercolor…），原图 4000px/300dpi。"
                            "但两个硬限制：缩略图只有 **266×266**；**没有服务端搜索**"
                            "（换任何参数都是同一批 100 条）→ 只能按标题标签本地筛，"
                            "筛不满就拿一批交给模型判。想用加 --provider patternbulk。"},
    "patternbank": {"label": "Patternbank（专业花型）", "kind": "site",
                    "fn": _patternbank, "home": "https://patternbank.com/",
                    "env": ENV["patternbank"], "keyless": True,
                    "needs": "scrapling",
                    "note": "**免 key，但要装 Scrapling**（`pip install \"scrapling[fetchers]\"`）。"
                            "专业花型/印花素材库，23 万个设计，**图本身就是四方连续** —— "
                            "平铺闸门通过率比搜索引擎高一截。**只认英文词、量小**（一页 4~15 张）。"
                            "没装依赖就自动跳过。"},
    "spoonflower": {"label": "Spoonflower（印花面料平台）", "kind": "site",
                    "fn": _spoonflower, "home": "https://www.spoonflower.com/",
                    "env": ENV["spoonflower"], "keyless": True,
                    "needs": "scrapling_browser",
                    "note": "**免 key，站方允许爬**（robots 写着 Everyone else: crawlable，"
                            "只禁训练；Crawl-delay 1 已遵守）。全球最大印花面料定制平台，"
                            "**一页 121 张真印花设计**、alt 是真标题 —— 但要过 Cloudflare "
                            "挑战，**一页约 96 秒**，图只有 400px。"
                            "**2026-10-18 用户要求开成默认**：代价是默认一轮多 1.5~2 分钟。"},
    "clip_studio": {"label": "CLIP STUDIO ASSETS（日本插画素材）", "kind": "illustration",
                   "fn": _clip_studio, "home": "https://assets.clip-studio.com/ja-jp/",
                   "env": ENV["clip_studio"], "keyless": True,
                   "note": "**免 key、免登录**。日本插画/图案素材平台，公开 JSON 搜索接口，"
                           "一页返回数十条，带 purchase_count（热度）和作者 handle。"
                           "只取 S3 缩略图当线索，原图要登录/购买。"},
    "site_search": {"label": "精选站搜索（Etsy/Creative Market/Behance…）", "kind": "search",
                    "fn": _site_search, "home": "https://serper.dev",
                    "env": "WSEARCH_SERPER_KEY / WSEARCH_SERPAPI_KEY / WSEARCH_BRAVE_KEY",
                    "default": False,
                    "note": "**要搜索代理 key（Serper/SerpAPI/Brave 任意一把）**。用 Google Images 的 "
                            "`site:<domain>` 拿精选站里的图，不直接爬目标站。默认不跑；用 "
                            "--provider site_search 手动开，配 --sites etsy,creativemarket 或 "
                            "--intent business 选站。"},
    # ---- 2026-10-20 可直爬来源（默认不跑，--provider 点名才跑）----
    "designspiration": {"label": "Designspiration（Pinterest 式设计灵感）", "kind": "community",
                        "fn": _designspiration, "home": "https://designspiration.net/",
                        "env": "", "keyless": True, "default": False,
                        "note": "免 key。搜索页服务端渲染，robots 只禁 /image/browse/ 与 /resource/*；"
                                "图最高 472px，是**线索级**。最接近 Pinterest 的可爬替代。"},
    "openclipart": {"label": "Openclipart（公共领域 clipart）", "kind": "illustration",
                    "fn": _openclipart, "home": "https://openclipart.org/",
                    "env": "", "keyless": True, "default": False,
                    "note": "免 key。公共领域 clipart，robots 只有 sitemap；"
                            "适合卡通/童装元素；图是矢量/PNG，不是印花稿。"},
    "irasutoya": {"label": "いらすとや（日系免费插画）", "kind": "illustration",
                  "fn": _irasutoya, "home": "https://www.irasutoya.com/",
                  "env": "", "keyless": True, "default": False,
                  "note": "免 key。Blogger feed 搜索；日系可爱插画，适合卡通/儿童；"
                          "**授权另有条款**（索引标 irasutoya_terms），不是公共领域。"},
    "printpattern": {"label": "Print & Pattern（印花趋势博客）", "kind": "community",
                     "fn": _printpattern, "home": "https://printpattern.blogspot.com/",
                     "env": "", "keyless": True, "default": False,
                     "note": "免 key。用 Blogger feed（不碰 robots 禁的 /search）；"
                             "多年印花/图案趋势，适合发现品牌与来源；图有版权。"},
    "patternobserver": {"label": "Pattern Observer（纺织图案博客）", "kind": "community",
                        "fn": _patternobserver, "home": "https://www.patternobserver.com/",
                        "env": "", "keyless": True, "default": False,
                        "note": "免 key。WordPress REST 搜索（robots 全允许）；"
                                "偏纺织设计文章/趋势，不是大批量图库。"},
    "poptnc": {"label": "POP花型网（WordPress REST）", "kind": "marketplace",
               "fn": _poptnc, "home": "https://www.poptnc.com/",
               "env": "", "keyless": True, "default": False,
               "note": "免 key。robots 只禁 /wp-admin/；`wp-json/wp/v2/posts?search=` "
                       "可搜、可分页，特色图直链 200。花型/印花素材站，量很大；"
                       "图有版权，取链接/线索用。"},
    "publicdomainpictures": {"label": "PublicDomainPictures（CC0）", "kind": "stock",
                             "fn": _publicdomainpictures,
                             "home": "https://www.publicdomainpictures.net/",
                             "env": "", "keyless": True, "default": False,
                             "needs": "scrapling",
                             "note": "免 key。robots 允许搜索页；Cloudflare 直连 403，"
                                     "走 Scrapling。CC0 公版图，含历史纹样/植物。"},
    "pxhere": {"label": "PxHere（CC0 照片）", "kind": "stock", "fn": _pxhere,
               "home": "https://pxhere.com/", "env": "", "keyless": True,
               "default": False, "needs": "scrapling",
               "note": "免 key。robots Allow: /；Cloudflare 直连要 Scrapling。"
                       "CC0 照片为主，花型密度低，适合补真实材质/水彩参考。"},
    "stocksnap": {"label": "StockSnap（CC0 照片）", "kind": "stock", "fn": _stocksnap,
                  "home": "https://stocksnap.io/", "env": "", "keyless": True,
                  "default": False, "needs": "scrapling",
                  "note": "免 key。robots 只禁 shutterstock 图；Cloudflare 走 Scrapling。"
                          "CC0 照片，做材质/水彩参考，不是印花稿。"},
    "serper": {"label": "Serper（Google 图片）", "kind": "search", "fn": _serper,
               "home": "https://serper.dev", "env": ENV["serper"],
               "note": "要 key。免 key 那几家不够用时的补充，免费 2500 次。"},
    "serpapi": {"label": "SerpAPI（Google 图片）", "kind": "search", "fn": _serpapi,
                "home": "https://serpapi.com", "env": ENV["serpapi"],
                "note": "要 key。结构最规范，但免费额度只有 100 次/月。"},
    "brave": {"label": "Brave 图片搜索", "kind": "search", "fn": _brave,
              "home": "https://brave.com/search/api/", "env": ENV["brave"],
              "note": "要 key。额度大（2000 次/月），但 1 次/秒、偏英文站。"},
    "pexels": {"label": "Pexels 图库", "kind": "stock", "fn": _pexels,
               "home": "https://www.pexels.com/api/", "env": ENV["pexels"],
               "note": "要 key。免费、无限额、直出原图。但摄影图多，平铺单元少。"},
    "pixabay": {"label": "Pixabay 图库", "kind": "stock", "fn": _pixabay,
                "home": "https://pixabay.com/api/docs/", "env": ENV["pixabay"],
                "note": "要 key。同上，量比 Pexels 大，图片偏素材感。"},
    "unsplash": {"label": "Unsplash 图库", "kind": "stock", "fn": _unsplash,
                 "home": "https://unsplash.com/developers", "env": ENV["unsplash"],
                 "note": "要 key。画质最好，但免费档 50 次/小时。"},
}

# 默认跑哪些（按优先级）。**免 key 的排最前** —— 它们随时可用，
# 而且中文花型素材最对路；要 key 的排在后面当补充，没配就自动跳过。
# 顺序只决定"报数时谁先谁后"，不决定谁吃满配额 —— 结果在 search() 里
# 按来源轮流取（见 _interleave），后面的家不会被前面挤掉。
#
# 两个特例（都在 REGISTRY / enabled() 里判定，不是靠这张表）：
#   · 必应**不在默认里**（default=False）：它会限流到"回不相关的结果集"，
#     默认打开会污染候选池。用 --provider bing 开。
#   · patternbank 在表里，但**装没装 Scrapling 决定它跑不跑**（needs 字段）。
#
# ⚠️ 注意：`default: False` **只是给 on_demand() 看的标签**，真正决定
#    "默认跑不跑"的是**在不在这张表里**。
#
# 用户 2026-10-18 明确要求"patternbank / Spoonflower / Pattern Designs 都开"，
# 于是后两家从 on_demand 挪进了这张表（patternbank 本来就在）。
# 代价要记住：**Spoonflower 一页约 96 秒**，默认跑一轮会明显变慢；
# 它和图库/搜索引擎并发的，所以是"整体时长 +1.5~2 分钟"，不是"卡住"。
# 仍在手动一档的只剩三家：必应、源创花样网（tile_opt 线索图）、Patternbulk。
DEFAULT_ORDER = ("baidu", "so360", "huaban", "duitang", "walan", "dancecolor",
                 "tnc", "patterndesigns", "patternbank", "clip_studio", "spoonflower",
                 "serper", "serpapi", "brave", "pixabay", "pexels", "unsplash")


# ------------------------------------------------------- config.json 里的"加站"
# 用户想在 config.json 里加一家站，不该逼他去改代码 —— 所以这里有一个
# **完全由配置驱动**的通用适配器：给个列表页 URL 模板 + 三条正则就够了。
#
# 它只适合"服务端渲染 + 图直链在 HTML 里"的站（国内素材站大多是这种）。
# JS 渲染、要登录、要签名的站它抓不到，那种还是得写专门的函数。
def _generic(spec):
    """把 config.json 的一条 `crawlers.custom` 变成搜索适配器。"""
    name = spec["name"]
    base = ""
    try:
        u = urllib.parse.urlsplit(spec.get("list") or "")
        base = "%s://%s" % (u.scheme, u.netloc)
    except Exception:                                        # noqa: BLE001
        pass

    def fn(queries, per_query, key, opts):
        rows = []
        pages = max(1, int(spec.get("pages") or 1))
        per_page = max(1, int(spec.get("per_page") or 30))
        cap = max(per_page, min(per_page * pages, int(per_query or 100)))
        try:
            img_re = re.compile(spec["img"])
            # **卡片与标题按跨行匹配（DOTALL）**：HTML 里一个卡片几乎必然跨多行，
            # 不给 DOTALL 的话 `(.*?)` 一个都匹配不上 —— 用户会以为"正则写错了"，
            # 其实是少了这个 flag（这里替他加上）。
            item_re = re.compile(spec["item"], re.S) if spec.get("item") else None
            title_re = re.compile(spec["title"], re.S) if spec.get("title") else None
        except Exception as e:                               # noqa: BLE001
            # 正则写错是用户手滑，说清楚是哪一条，别让整轮搜索挂掉
            raise net.NetError("config.json 里 %s 的正则写错了：%s" % (name, e))
        hdrs = dict(spec.get("headers") or {})
        if spec.get("referer"):
            hdrs["Referer"] = spec["referer"]
        timeout = int(spec.get("timeout") or opts.get("timeout") or 25)

        for q in queries:
            for page in range(1, pages + 1):
                url = (spec.get("list") or "").replace(
                    "{q}", urllib.parse.quote(q or "")).replace("{page}", str(page))
                try:
                    status, _h, body = net.request(url, headers=hdrs, timeout=timeout)
                except Exception:                            # noqa: BLE001
                    continue
                if status != 200 or not body:
                    continue
                txt = body.decode(spec.get("encoding") or "utf-8", "replace")
                blocks = ([m.group(1) for m in item_re.finditer(txt)]
                          if item_re else [txt])
                for blk in blocks:
                    mi = img_re.search(blk)
                    if not mi:
                        continue
                    img = html.unescape(mi.group(1))
                    if img.startswith("//"):
                        img = "https:" + img
                    elif img.startswith("/"):
                        img = base + img
                    if not img.startswith("http"):
                        continue
                    title = ""
                    if title_re:
                        mt = title_re.search(blk)
                        title = _strip_tags(mt.group(1)) if mt else ""
                    rows.append(_item(img, "", 0, 0, title, url, name, q,
                                      site=spec.get("site") or None))
                    if len(rows) >= cap:
                        return rows
        return rows

    return fn


def load_custom():
    """把 config.json 里的自建站注册进来（幂等，重复调用无副作用）。

    `crawlers.disabled` 也在这里**读一次**并缓起来 —— 关站是"今天不想用它"，
    每轮都读文件没必要，而且中途改文件只影响下一轮，更好预期。
    """
    global _DISABLED, DEFAULT_ORDER
    try:
        cfg = _config.crawlers()
    except Exception:                                        # noqa: BLE001
        return []
    _DISABLED = set(cfg["disabled"])
    added = []
    for spec in cfg["custom"]:
        name = str(spec["name"]).strip()
        if not name or name in REGISTRY:
            continue
        # `"enabled": false` = 先留着当模板、这轮不跑（config.json 里的示例就是这种）
        if spec.get("enabled") is False:
            continue
        REGISTRY[name] = {
            "label": spec.get("label") or ("%s（配置文件加的）" % name),
            "kind": "custom", "fn": _generic(spec),
            "home": spec.get("referer") or spec.get("list") or "",
            "env": "", "keyless": True,
            "note": spec.get("note") or "来自 config.json 的自建站（列表页 + 正则）。",
        }
        DEFAULT_ORDER = DEFAULT_ORDER + (name,)
        added.append(name)
    return added


_DISABLED = set()


def is_disabled(name):
    """这家是不是被 config.json 关掉了。"""
    return name in _DISABLED


load_custom()


def on_demand():
    """哪些家**免 key / 已配 key，但默认不跑**（要 --provider 手动开）。"""
    return [n for n in REGISTRY if not REGISTRY[n].get("default", True)
            and enabled(n)]


def key_of(name, env=None):
    """取某家的 key。env 传 None 就读真实环境变量。

    免 key 的来源**永远回空串** —— 不是"没配"，是它压根没有 key 这回事。
    判断"这家现在能不能用"请用 enabled()。

    site_search 特殊：Serper / SerpAPI / Brave 任意一把都能用，按这个顺序取。
    """
    spec = REGISTRY[name]
    if spec.get("keyless"):
        return ""
    e = env if env is not None else os.environ
    if name == "site_search":
        for envn in ("WSEARCH_SERPER_KEY", "WSEARCH_SERPAPI_KEY", "WSEARCH_BRAVE_KEY"):
            v = (e.get(envn) or "").strip()
            if v:
                return v
        return ""
    return (e.get(spec["env"]) or "").strip()


def enabled(name, env=None):
    """这家现在能不能用：免 key 的永远能用，要 key 的得真配了 key。

    多一层：**要爬取的来源还得真装了 Scrapling**（`needs` 字段）。
    这一层不能省 —— 没装就说"能用"，跑起来才一个个 403/空结果，
    用户根本看不出是缺依赖还是站挂了。宁可一开始就把它从可用列表里去掉。
    """
    spec = REGISTRY[name]
    if is_disabled(name):          # config.json 里被关掉的那几家
        return False
    if spec.get("needs") == "scrapling" and not scrape.have():
        return False
    if spec.get("needs") == "scrapling_browser" and not scrape.have_browser():
        return False
    if spec.get("keyless"):
        return True
    return bool(key_of(name, env))


def available(env=None, only=None):
    """哪些家现在能用（免 key 的永远在里面）。only 可指定只看这几家。"""
    names = [n for n in (only or DEFAULT_ORDER) if n in REGISTRY]
    return [n for n in names if enabled(n, env)]


def _interleave(chunks):
    """把各家给的结果**轮流取**拼成一条，而不是"排第一的家全部在前"。

    为什么必须这样：下游 `_hunt` 会把候选截到 800 条（`rows[:800]`）再下载。
    按来源顺序拼的话，百度一家十条词就是 600 条，后面几家**连一条都进不
    了下载队列** —— 加了新来源等于白加。轮流取之后，800 个名额是五家平分
    的，"扩大搜索范围"才真的生效。

    保持不变的是：单家内部的相对顺序（谁先给谁在前），以及总条数。
    """
    out, idx = [], [0] * len(chunks)
    left = True
    while left:
        left = False
        for i, c in enumerate(chunks):
            if idx[i] < len(c):
                out.append(c[idx[i]])
                idx[i] += 1
                left = True
    return out


def search(queries, *, per_query=100, providers=None, env=None, opts=None):
    """并发跑所有可用来源，回 (结果列表, 错误列表)。

    **并发是硬要求**（见项目记忆里"逐个试的写法必须并发"那条）：串行跑
    8 家、每家一次请求 20 秒，光搜就要等两分半，用户只会以为卡死了。
    并发跑 40 秒，而且某一家的错误不影响别家。

    错误不抛出、收集起来返回 —— 搜索是"尽力而为"：百度挂了，360 给的
    几十张照样能出结果，只要在 note 里如实说清楚。
    """
    from concurrent.futures import ThreadPoolExecutor

    opts = dict(opts or {})
    names = [n for n in (providers or DEFAULT_ORDER) if n in REGISTRY]
    names = [n for n in names if enabled(n, env)]
    if not names:
        raise net.NetError(
            "没有可用的搜索来源。免 key 的「百度图片 / 360 图片 / 花瓣 / 堆糖 / "
            "瓦栏 / 色彩跳动 / 全球纺织网 / Pattern Designs / CLIP STUDIO ASSETS」"
            "默认一直可用（必应要 --provider bing；Patternbank 还要装 "
            "Scrapling）；`site_search`（Etsy/Creative Market/Behance…）要 "
            "WSEARCH_SERPER_KEY。这里为空，说明这次只指定了要 key 的来源"
            "（--provider）而 key 还没配。配置方法见 算法/全网搜花型/README.md。")

    rows, errors = [], []

    def one(name):
        spec = REGISTRY[name]
        try:
            got = spec["fn"](queries, per_query, key_of(name, env), opts)
            return name, _clean(got), None
        except Exception as e:                               # noqa: BLE001
            msg = str(e)
            # key 失效是最常见的一类，单独给一句人话，别让用户对着一串英文猜
            if "401" in msg or "403" in msg or "invalid" in msg.lower():
                msg = "key 可能不对或已过期（%s）" % msg
            return name, [], "%s：%s" % (spec["label"], msg)

    chunks = []
    with ThreadPoolExecutor(max_workers=min(8, len(names))) as pool:
        for name, got, err in pool.map(one, names):
            if got:
                chunks.append(got)                           # 按来源轮流，见 _interleave
            if err:
                errors.append(err)
    return _interleave(chunks), errors
