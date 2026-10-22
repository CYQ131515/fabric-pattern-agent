"""全网搜花型 · 爬取层（**可选依赖**：Scrapling）。

为什么要有这一层
    前面几家（百度 / 360 / 花瓣 / 堆糖 / 必应）都是"打公开 JSON 或 HTML 接口"，
    net.py 那一套（urllib + 浏览器 UA + 强制直连）够用。但**专业花型站**不一样：

      · patternbank.com 用 net.py 直连是 **403** —— 它在按 TLS 指纹挡非浏览器
        客户端，光换个 UA 没用（UA 是应用层，指纹在握手层）；
      · thepatternlibrary.com / subtlepatterns 整页是 JS 渲染的，urllib 拿到的
        HTML 里一个 img 都没有。

    要拿下这些站需要**真浏览器的 TLS 握手特征**，这正是 Scrapling 的 Fetcher
    层做的事（底层 curl_cffi，模拟 Chrome 的 TLS/HTTP2 指纹）。实测同一个 URL：
    **net.py 403 / Scrapling 200**。

为什么只用它的 Fetcher，不用浏览器
    Scrapling 还有 StealthyFetcher / DynamicFetcher（真开 Chromium），能过
    Cloudflare Turnstile。但那两个要 `scrapling install` 再下几百 MB 浏览器，
    而且一页 3~8 秒 —— 我们一轮要打十几条检索词，慢不起。
    Fetcher 层是纯 HTTP，**1~2 秒一页**，已经够拿下 patternbank。
    真碰到非开浏览器不可的站（Cloudflare 拦截页）再说，见 README「下一步」。

为什么单独一个文件
    它是**可选依赖**：没装 scrapling 这个项目照样能跑，只是爬取类的来源自动
    跳过。把 import 散进 providers.py 会让"没装"变成**启动期崩溃**。
    所以全项目只有这一层 import scrapling，而且是**懒加载**（首次 get 时才
    import，import 失败就记下来，不抛）。

和 net.py 的关系（这条别搞反）
    net.py 仍是"本项目唯一该 import socket/urllib 的文件"，那条规矩没破 ——
    **下载图片一律走 net.fetch_bytes()**，这一层只负责"取页面 / 取直链"。
    但 Scrapling 自己的网络栈不吃 net.py 的直连设置，所以这里**手动把代理
    环境变量摘掉**（和后端起子进程时摘 6 个代理变量是同一个道理，见下）。
"""
from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
import urllib.parse

# 同一个站两次请求之间至少隔多久。
# 实测连打（间隔 <2s）打到第 4~5 次就开始回 403，隔 3 秒以上是稳的。
# 一轮英文检索词通常 3~5 条，总耗时 +10~15 秒，可以接受。
GAP = 3.0

_lock = threading.Lock()
_last = {}          # host -> 上次请求的时间戳
_fetcher = None     # 懒加载塞这儿（Scrapling 的 Fetcher 类）
_tried = False      # 只试一次 import，失败就别反复试


def have():
    """装了 Scrapling 吗？没装返回 False，**不抛异常**。

    providers 那边靠这个判断"这一家现在能不能用"（见 available()），
    所以这里必须老实回 False，不能让 ImportError 冒出去。
    """
    global _fetcher, _tried
    if _tried:
        return _fetcher is not None
    _tried = True
    try:
        from scrapling.fetchers import Fetcher              # noqa: PLC0415
        _fetcher = Fetcher
        # 它默认往 stderr 打 "INFO: Fetched (200) <GET ...>"，会混进 run.py
        # 的进度输出里，看着像报错。只留 CRITICAL。
        # （日志对象是它自己包的一层 LoggerProxy，名字叫 "scrapling"，
        #   直接 setLevel 就行 —— 试过 logging.disable()，对它没用。）
        try:
            from scrapling.core.utils import log            # noqa: PLC0415
            log.setLevel(logging.CRITICAL)
        except Exception:                                   # noqa: BLE001
            pass
    except Exception:                                       # noqa: BLE001
        _fetcher = None
    return _fetcher is not None


def why_not():
    """没装时给人看的一句话（会进 probe / 报错里）。"""
    return ("没装 Scrapling（专业花型站要靠它绕过 TLS 指纹拦截）。装："
            "./.venv/bin/pip install \"scrapling[fetchers]\"")


# --------------------------------------------------------------- 浏览器层
# 有些站不是"看 TLS 指纹"，而是整页 JS 渲染 + Cloudflare 挑战页（403 但页面写着
# "Just a moment..."）。Fetcher 层（纯 HTTP）拿不下来，必须真开浏览器。
#
# **重要区分**（踩过这个坑）：Cloudflare 403 **不等于**站方禁止抓取。
# spoonflower 的 robots 明写 "Everyone else: crawlable" + `Crawl-delay: 1`，
# 只是把闸门放在网络边缘防滥用。判断"能不能爬"看 robots，不看状态码。
#
# 代价很大，所以只给少数站用（default=False）：
#   · 要下载 Chromium（`python -m playwright install chromium`，约 100MB）
#   · 一次 CF 挑战要 ~80 秒，一页 ~96 秒（Fetcher 层是 1~2 秒）
_browser_tried = False
_browser_ok = False
_session_cls = None


def have_browser():
    """浏览器层能不能用（Scrapling 的 StealthySession + 已下载的 Chromium）。"""
    global _browser_tried, _browser_ok, _session_cls
    if _browser_tried:
        return _browser_ok
    _browser_tried = True
    try:
        from scrapling.fetchers import StealthySession     # noqa: PLC0415
        _session_cls = StealthySession
        _browser_ok = True
    except Exception:                                       # noqa: BLE001
        _browser_ok = False
    return _browser_ok


def why_no_browser():
    return ("浏览器层没装好（爬 Cloudflare 站要用）。装："
            "./.venv/bin/python -m playwright install chromium")


@contextlib.contextmanager
def stealth_session(**kw):
    """开一个**能过 Cloudflare** 的持久会话；不可用时 yield None（不抛）。

    为什么必须可复用一个 session：
      · 每次 fetch 都会重新过一遍 CF 挑战，`solve_cloudflare` 一次 ~80 秒；
      · 一个 session 里连续取多个页面，比每页新开省掉若干次挑战。

    两条实测出来的硬规矩：
      1. **不要传 `network_idle=True`** —— 这类站的请求永远不空闲，会一直等到
         超时，实测整个进程被 SIGKILL(137)。用 `wait_selector` 等元素出现。
      2. 这个会话里**必须带着 `_no_proxy()`** —— 浏览器也会读系统代理，
         本机代理常年开着，走过代理会遇到 CF 判定异常。
    """
    if not have_browser():
        yield None
        return
    kw.setdefault("headless", True)
    kw.setdefault("solve_cloudflare", True)
    kw.setdefault("timeout", 90000)
    with _no_proxy():
        sess = _session_cls(**kw)
        try:
            with sess:
                yield sess
        finally:
            pass


@contextlib.contextmanager
def _no_proxy():
    """临时摘掉 *_proxy 环境变量。

    和 net.py「强制直连」同一口径：本机系统代理常年开着，而 Scrapling 底层
    是 libcurl，**会读 http_proxy / https_proxy**。实测有的站经代理会被重定向
    到 127.0.0.1，然后被 curl 的 SSRF 保护拒掉（freepik 就是这样，报
    "Redirect to internal IP 127.0.0.1 rejected"）。
    """
    keys = [k for k in os.environ if k.lower().endswith("_proxy")]
    saved = {k: os.environ.pop(k) for k in keys}
    try:
        yield
    finally:
        os.environ.update(saved)


def _domain(url):
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except Exception:                                       # noqa: BLE001
        return ""


def get(url, *, timeout=25, gap=GAP):
    """取一个页面，返回 Scrapling 的 Selector（能 .css() / .xpath()）；失败回 None。

    **失败不抛**：调用方（providers 各家）是按"一家挂了记一条错误、其他家继续"
    写的，这里抛上去会被当成整批失败，把别的来源一起赔进去。
    """
    if not have():
        return None
    if gap > 0:
        with _lock:
            wait = gap - (time.time() - _last.get(_domain(url), 0.0))
            if wait > 0:
                time.sleep(wait)
            _last[_domain(url)] = time.time()
    try:
        with _no_proxy():
            # impersonate="chrome"：模拟 Chrome 的 TLS + HTTP2 指纹。
            # 这是能不能过 403 的关键，换成普通 requests 就又是 403 了。
            return _fetcher.get(url, impersonate="chrome",
                                timeout=timeout, stealthy_headers=True)
    except Exception:                                       # noqa: BLE001
        return None
