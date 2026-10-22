"""全网搜花型 · 网络出口（本项目里**唯一**该 import socket/urllib 的文件）。

为什么单开一层
    这个算法要连两类完全不同的服务：
      ① 图片搜索网关（Google/Bing 图片搜索的第三方代理，如 Serper、SerpAPI）
      ② 各种图床的 CDN（ Pinterest、Etsy、花朵素材站…… 域名不可预测）
    两类的网络脾气都不一样，但**出口策略必须只有一套** —— 否则以后想加
    "超时统一调短""全部走某个代理"，得满工程找散落的 urlopen。

两条硬策略（和 算法/psd分层/rhpsd/net.py 保持一致，全项目一个口径）
    1) **强制直连，不认系统代理。** 走 ProxyHandler({}) 把 urllib 的代理
       探测整个关掉。本机开发环境里系统代理常年开着，而图床 CDN 经代理
       往往直接 403/超时 —— 这是排查过很多次的坑。后端起子进程时还会
       再摘一遍代理环境变量（见 core/psdlayer.py 的同名注释）。
    2) **IPv4 优先。** 有的站点 AAAA 记录通但极慢（本机实测几十秒才握手），
       而 A 记录飞快。做法是把 getaddrinfo 的结果**排序**，不是禁用 IPv6 ——
       纯 IPv6 的站点照样连得上。
    3) **带浏览器 UA。** 没有 UA 的请求会被不少 CDN 直接挡掉（Cloudflare
       会回 403 + code 1010）。这不是伪装，是很多站点的基本门槛。
"""
from __future__ import annotations

import http.cookiejar
import json
import socket
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request

# 很多图床/CDN 没 UA 直接 403。用一个正常的桌面浏览器 UA 就够。
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# 单张图最大允许多少字节。花型图 2048 边长 JPEG 也就 1~2 MB；
# 给到 12 MB 是留余量给 PNG 大图，超过的基本是"点进去是整页 HTML"或压缩包。
MAX_IMAGE_BYTES = 12 * 1024 * 1024


class NetError(Exception):
    """网络层错误。message 一律是**人话**，可以直接进 note 给用户看。"""


_v4_patched = False


def _prefer_ipv4():
    """把 getaddrinfo 的结果改成 IPv4 排前面。只补一次，幂等。

    只排序不禁用：纯 IPv6 的站点（很少，但存在）仍然连得上，
    只是排在后面 —— 前面的都失败了才会轮到它。
    """
    global _v4_patched
    if _v4_patched:
        return
    _v4_patched = True
    real = socket.getaddrinfo

    def sorted_gai(*args, **kw):
        try:
            res = real(*args, **kw)
        except Exception:
            raise
        if not isinstance(res, list):
            return res
        return sorted(res, key=lambda r: 0 if r[0] == socket.AF_INET else 1)

    socket.getaddrinfo = sorted_gai


# 少数老服务器只认 OpenSSL 3 默认禁掉的那套套件。SECLEVEL=1 能放开它们，
# **证书校验照旧开着**（只是不再拒绝 SHA1/MAC 这一类老参数）。
_LOOSE_CIPHERS = "DEFAULT@SECLEVEL=1"


def opener(*, loose_tls=False):
    """一个不认代理、IPv4 优先的 urllib opener。每次新建，避免状态串。

    `loose_tls=True` 只给**服务器只支持老套件**的站用（目前只有
    hx.tnc.com.cn / 全球纺织网）。那台服务器只回 `TLS1.2 + AES128-SHA`
    （RSA 交换、没有前向保密、SHA1 做 MAC），Python 3.13 的默认 context 在
    SECLEVEL=2 下把这套全禁了 —— 于是同一台机器上 **curl 连得上、urllib
    握手直接 `SSLV3_ALERT_HANDSHAKE_FAILURE`**。排查半天才发现是这里。

    ⚠️ **别把它设成默认**：默认放宽等于拿整条产线的 TLS 强度去陪一台老服务器。
       只有调用方显式点名（`request(..., loose_tls=True)`）这一条链路才降。
    """
    _prefer_ipv4()
    handlers = [
        urllib.request.ProxyHandler({}),          # 直连：不读系统代理
    ]
    if loose_tls:
        ctx = ssl.create_default_context()
        ctx.set_ciphers(_LOOSE_CIPHERS)
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    handlers.append(urllib.request.HTTPRedirectHandler())
    return urllib.request.build_opener(*handlers)


def _headers(extra=None):
    hdrs = {"User-Agent": BROWSER_UA, "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    hdrs.update(extra or {})
    return hdrs


class Session:
    """带 cookie 的会话（百度和搜狗的图片接口**必须**先用它）。

    实测：直接用裸请求打百度的 `image.baidu.com/search/acjson`，它会回

        {"antiFlag": 1, "message": "Forbid spider access"}

    一个字的结果都没有。先去一次站点首页把 `Set-Cookie`（主要是 BAIDUID）
    收进 jar，再带着它请求就正常了（实测能拿到 60 条）。搜狗同理，
    不过搜狗**即使带了 cookie 也回 "forbid"** —— 它现在还要一个签名参数，
    所以适配器里没有搜狗（试过，不留半成品）。

    cookie 按域名缓存在进程内（见下面的 session()）：一次搜索会打好几个
    检索词，预热只做一次，不会每个词都去敲一遍首页。
    """

    def __init__(self, home, *, timeout=15):
        self.home = home
        self.jar = http.cookiejar.CookieJar()
        self._op = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),                 # 直连
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPRedirectHandler(),
        )
        self._warmed = False
        self._lock = threading.Lock()

    def warm(self, timeout=15):
        """去首页拿 cookie。拿不到也返回（有的站点本来就不给 cookie）。"""
        _prefer_ipv4()
        with self._lock:
            if self._warmed:
                return True
            try:
                req = urllib.request.Request(self.home, headers=_headers())
                with self._op.open(req, timeout=timeout) as r:
                    r.read(4096)                 # 必须读一下，才会走完 Set-Cookie
            except Exception:                                # noqa: BLE001
                pass
            self._warmed = True
            return True

    def _open(self, url, *, headers=None, timeout=25, max_bytes=4 * 1024 * 1024):
        self.warm()
        req = urllib.request.Request(url, headers=_headers(headers))
        try:
            with self._op.open(req, timeout=timeout) as r:
                return r.status, dict(r.headers), r.read(max_bytes)
        except urllib.error.HTTPError as e:
            raise NetError("对方拒绝了这次请求（HTTP %s）" % e.code)
        except urllib.error.URLError as e:
            raise NetError("连不上：%s" % (getattr(e, "reason", None) or e))
        except socket.timeout:
            raise NetError("请求超时（超过 %d 秒）" % timeout)
        except Exception as e:                               # noqa: BLE001
            raise NetError("%s：%s" % (type(e).__name__, e))

    def get_json(self, url, *, params=None, headers=None, timeout=25):
        if params:
            sep = "&" if ("?" in url) else "?"
            url = url + sep + urllib.parse.urlencode(params)
        status, _hd, body = self._open(url, headers=headers, timeout=timeout)
        if status < 200 or status >= 300:
            raise NetError("接口返回 HTTP %s" % status)
        text = body.decode("utf-8", "replace")
        try:
            return json.loads(text)
        except Exception:                                    # noqa: BLE001
            # 有的接口会把 JSON 包一层回调，或者在前面塞几个字节
            i, j = text.find("{"), text.rfind("}")
            if i >= 0 and j > i:
                try:
                    return json.loads(text[i:j + 1])
                except Exception:                            # noqa: BLE001
                    pass
            raise NetError("接口返回的不是 JSON（可能被网关拦了）")

    def get_text(self, url, *, headers=None, timeout=25, max_bytes=3 * 1024 * 1024):
        status, _hd, body = self._open(url, headers=headers, timeout=timeout,
                                       max_bytes=max_bytes)
        if status < 200 or status >= 300:
            raise NetError("页面返回 HTTP %s" % status)
        return body.decode("utf-8", "replace")


_SESSIONS = {}
_SESSION_LOCK = threading.Lock()


def session(home):
    """按站点取一个共用的 Session（进程内缓存，只预热一次）。"""
    key = urllib.parse.urlsplit(home).hostname or home
    with _SESSION_LOCK:
        if key not in _SESSIONS:
            _SESSIONS[key] = Session(home)
        return _SESSIONS[key]


def request(url, *, method="GET", headers=None, data=None, timeout=20,
            max_bytes=MAX_IMAGE_BYTES, accept_any=False, loose_tls=False):
    """发一个请求，回 (status, resp_headers, body bytes)。

    accept_any=True 时不看 status，直接把响应体给调用方 —— 图片下载用：
    有些图床对 HEAD/GET 回 200 但 Content-Length 撒谎，也有些回 206，
    与其在这里判，不如把判断交给调用方（它能看字节）。

    loose_tls=True 只给只认老套件的服务器用，见 opener() 的注释。
    """
    hdrs = {"User-Agent": BROWSER_UA, "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with opener(loose_tls=loose_tls).open(req, timeout=timeout) as r:
            body = r.read(max_bytes) if max_bytes else r.read()
            return r.status, dict(r.headers), body
    except urllib.error.HTTPError as e:
        if accept_any:
            try:
                body = e.read(max_bytes) if max_bytes else e.read()
            except Exception:                                # noqa: BLE001
                body = b""
            return e.code, dict(e.headers or {}), body
        raise NetError("对方拒绝了这次请求（HTTP %s）" % e.code)
    except urllib.error.URLError as e:
        raise NetError("连不上：%s" % (getattr(e, "reason", None) or e))
    except socket.timeout:
        raise NetError("请求超时（超过 %d 秒）" % timeout)
    except Exception as e:                                   # noqa: BLE001
        raise NetError("%s：%s" % (type(e).__name__, e))


def get_json(url, *, headers=None, timeout=25, params=None):
    """GET 一个 JSON 接口。HTTP != 2xx 或不是 JSON 都当失败，错误信息带一句人话。"""
    if params:
        sep = "&" if ("?" in url) else "?"
        url = url + sep + urllib.parse.urlencode(params)
    status, _hd, body = request(url, headers=headers, timeout=timeout,
                                max_bytes=4 * 1024 * 1024)
    if status < 200 or status >= 300:
        raise NetError("接口返回 HTTP %s" % status)
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except Exception:                                        # noqa: BLE001
        raise NetError("接口返回的不是 JSON（可能是被网关拦了）")


def post_json(url, payload, *, headers=None, timeout=25):
    """POST 一个 JSON 接口。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    status, _hd, raw = request(url, method="POST", headers=hdrs, data=body,
                               timeout=timeout, max_bytes=4 * 1024 * 1024)
    if status < 200 or status >= 300:
        raise NetError("接口返回 HTTP %s" % status)
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:                                        # noqa: BLE001
        raise NetError("接口返回的不是 JSON")


def fetch_bytes(url, *, timeout=25, max_bytes=MAX_IMAGE_BYTES, referer=""):
    """下载一个二进制资源（图）。回 (bytes, content_type, 最终 URL)。

    referer 不是摆设：Pinterest / 一堆素材站对跨站盗链图会回 403 或一张
    "禁止盗链"占位图，带上来源页做 Referer 能多拿到一大批图。
    """
    hdrs = {"Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}
    if referer:
        hdrs["Referer"] = referer
    try:
        status, hd, body = request(url, headers=hdrs, timeout=timeout,
                                   max_bytes=max_bytes, accept_any=True)
    except NetError:
        raise
    if status < 200 or status >= 300:
        raise NetError("下载失败（HTTP %s）" % status)
    ctype = (hd.get("Content-Type") or "").split(";")[0].strip().lower()
    return body, ctype, url
