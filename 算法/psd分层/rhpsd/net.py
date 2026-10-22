"""网络出口 —— 强制直连，不走代理（PSD 分层算法的唯一出口）。

为什么必须自己管这件事
    urllib 会**自动**读三处代理配置，不看代码看不出来：
      1. 环境变量 HTTP_PROXY / HTTPS_PROXY / ALL_PROXY（大小写都读）
      2. macOS 的系统网络设置（`scutil --proxy`）
      3. 显式设置的 agent 环境（这条机器上就有）

    这台机器上躺着一个本地代理（HTTP_PROXY=http://127.0.0.1:1082/）。
    RunningHub 和视觉网关**走它一律网络错误**：代理 CONNECT 失败，
    但报出来的样子特别像"key 不对""网关挂了"—— 排查半天方向全错。

    所以要的不是"优先直连、不行再退代理"，而是**永远直连**：
    无论用户的电脑开不开 VPN、系统代理开不开、shell 里导出了什么
    环境变量，算法都走正常网络。这也是产品要求（用户明确提过）。

怎么做到
    显式给一个 `ProxyHandler({})` —— 空字典 = 一个代理都不用。
    它**优先于环境变量、也优先于系统设置**，是最强的一层约束。
    再配合 `sanitize_env()` 把代理变量从子进程环境里摘掉，
    从"谁都不许配"变成"根本没得配"。

顺带把 IPv4 排到前面（和四方连续的 apimart.py 同一个理由）
    结果图的 CDN 常把 AAAA 排在 A 前面，而 urllib 没有 Happy Eyeballs
    （不像 curl，不会并发探两族），会老实按 getaddrinfo 的顺序连第一个。
    某些网络下 IPv6 是"连得上但极慢"—— 不报错、状态码正常、字节数也对，
    只看"通不通"根本发现不了（实测同一张图 152 秒 vs 2.6 秒）。
    所以排序时把 IPv4 提前。注意是**只排序、不禁用**：同一个地址族内部
    保持原顺序，IPv4 全失败时仍会顺着列表退到 IPv6，纯 v6 环境照样能用。

自检
    python -m rhpsd.net --check         # 打印代理现状 + 真实连通性
"""
from __future__ import annotations

import http.client
import os
import socket
import sys
import time
import urllib.error
import urllib.request

__all__ = [
    "PROXY_ENV_KEYS", "opener", "urlopen", "sanitize_env",
    "proxy_env_in", "describe",
]

# 会被 urllib / requests / http.client 读取的代理变量。
# 大小写两份都要摘：urllib 两份都读，而且不同工具读的那份不一样。
PROXY_ENV_KEYS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
)


# ---------------------------------------------------------------- IPv4 优先

def _v4_first_socket(address, timeout, source_address=None):
    """create_connection 的替身：按「IPv4 优先」的顺序逐个试地址。

    与 socket.create_connection 的差别只有一处 —— 排序。IPv4 全失败时
    仍会继续试 IPv6，所以不会把纯 v6 网络弄坏。
    """
    host, port = address
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    infos.sort(key=lambda i: i[0] != socket.AF_INET)
    last: OSError | None = None
    for fam, stype, proto, _cname, sa in infos:
        sock = None
        try:
            sock = socket.socket(fam, stype, proto)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sa)
            return sock
        except OSError as exc:
            last = exc
            if sock is not None:
                try:
                    sock.close()
                except Exception:                    # noqa: BLE001
                    pass
    raise last or OSError("连不上 %s" % (host,))


class _V4FirstHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        self.sock = _v4_first_socket((self.host, self.port), self.timeout,
                                     self.source_address)
        if self._tunnel_host:                        # pragma: no cover
            self._tunnel()
        self.sock = self._context.wrap_socket(self.sock,
                                              server_hostname=self.host)


class _V4FirstHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = _v4_first_socket((self.host, self.port), self.timeout,
                                     self.source_address)
        if self._tunnel_host:                        # pragma: no cover
            self._tunnel()


class _V4FirstHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_V4FirstHTTPSConnection, req,
                            context=self._context)


class _V4FirstHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_V4FirstHTTPConnection, req)


# ---------------------------------------------------------------- 直连出口

_OPENER: urllib.request.OpenerDirector | None = None


def opener() -> urllib.request.OpenerDirector:
    """全局直连 opener（建一次复用，线程安全：urlopen 本身是可重入的）。

    关键就是 `ProxyHandler({})` —— 传入空字典等于"不做任何代理"。
    不传参数的话它会自己 `getproxies()` 去读环境变量和系统设置，
    那就又回到我们不想要的老路了。
    """
    global _OPENER
    if _OPENER is None:
        _OPENER = urllib.request.build_opener(
            _V4FirstHTTPSHandler(), _V4FirstHTTPHandler(),
            urllib.request.ProxyHandler({}))        # ← 空 = 禁用一切代理
    return _OPENER


def urlopen(url, timeout=None, headers=None, data=None, method=None):
    """发一个**不走代理**的请求。用法与 urllib.request.urlopen 一致。

    既接受字符串 URL，也接受现成的 Request 对象（后者直接透传，
    调用方自己加好了 header/body）。
    """
    if isinstance(url, urllib.request.Request):
        req = url
    else:
        req = urllib.request.Request(url, data=data, method=method)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    return opener().open(req, timeout=timeout)


# ---------------------------------------------------------------- 环境清理

def sanitize_env(env: dict | None = None) -> dict:
    """把代理相关变量从一份环境字典里摘掉，返回新的字典（不改原对象）。

    为什么要动环境，而不只靠 ProxyHandler({})：
    出口只保证"我们自己的请求"直连。算法里还可能间接用到网络的东西
    （第三方库自带的 HTTP 客户端、PIL 拉取远程资源等），它们各自
    读环境变量的时机和行为都不一样。把变量摘掉 = 从源头上没得配。
    """
    src = dict(os.environ if env is None else env)
    for k in PROXY_ENV_KEYS:
        src.pop(k, None)
    return src


def proxy_env_in(env: dict | None = None) -> dict:
    """列出当前环境里还残留的代理变量（自检用）。"""
    src = os.environ if env is None else env
    return {k: src[k] for k in PROXY_ENV_KEYS if src.get(k)}


# ---------------------------------------------------------------- 自检

def describe() -> dict:
    """把当前网络出口的现状说清楚，给 --check 和排错用。"""
    op = opener()
    return {
        # 环境变量里有什么（这是"污染源"）
        "env_proxies": proxy_env_in(),
        # urllib 自己会认为要用哪些代理（空 = 干净）
        "urllib_proxies": urllib.request.getproxies(),
        # 我们实际用的出口。注意这里**不应该**有 ProxyHandler：
        # ProxyHandler({}) 生效的方式就是"不注册任何 *_open 方法"，
        # 于是 build_opener 连默认那个读环境的 ProxyHandler 都不会建。
        # 所以判断标准是"没有代理处理器"，而不是"有一个空代理处理器"。
        "outlet_has_proxy_handler": any(
            isinstance(h, urllib.request.ProxyHandler) for h in op.handlers),
        "outlet_handlers": [type(h).__name__ for h in op.handlers],
    }


def _probe(url: str, timeout: float = 15.0, op=None) -> tuple[bool, str]:
    """对 URL 做一次真实请求，返回 (是否通, 说明)。任何 HTTP 码都算"通"。

    op 不传就用我们的直连出口；传别的 opener 是为了做对照（见 _selftest）。
    ⚠️ `op.open(req, timeout)` 的第二个位置参数是 **data 不是 timeout**，
    写成位置参数会把秒数当成请求体发出去 —— 必须用关键字。
    """
    req = urllib.request.Request(url, method="GET",
                                 headers={"User-Agent": "rhpsd-netcheck/1.0"})
    t0 = time.time()
    try:
        if op is not None:
            ctx = op.open(req, timeout=timeout)
        else:
            ctx = urlopen(req, timeout=timeout)
        with ctx as resp:
            dt = time.time() - t0
            return True, "HTTP %s，%.2fs" % (resp.status, dt)
    except urllib.error.HTTPError as exc:
        dt = time.time() - t0
        # 4xx/5xx 都说明**网络是通的**，只是这个地址本身不欢迎裸请求
        return True, "HTTP %s（能连上），%.2fs" % (exc.code, dt)
    except Exception as exc:                          # noqa: BLE001
        dt = time.time() - t0
        return False, "%s: %s（%.2fs）" % (type(exc).__name__, exc, dt)


def _selftest(url: str, timeout: float) -> int:
    """在不碰系统设置的前提下，现场证明"代理拦不住我们"。

    做两组对照实验：

    ① 环境变量代理 —— 临时往环境里塞一个**必然连不通**的代理
       (127.0.0.1:1)，对比"读环境代理的老写法"和 net.urlopen。

    ② 系统代理 —— VPN 客户端（ClashX / Surge 之类）通常改的是 macOS
       **系统网络设置**，而不是环境变量。所以把 urllib 读系统设置的那个
       函数临时换掉，模拟"系统代理开着且连不上"，再比一次。

    两组都只改**本进程内存里的东西**，不动任何系统设置，结束后还原。
    """
    dead = "http://127.0.0.1:1/"
    saved = {k: os.environ.get(k) for k in PROXY_ENV_KEYS}
    results = []

    def compare(label, make_old_opener):
        old_ok, old_why = _probe(url, timeout, op=make_old_opener())
        new_ok, new_why = _probe(url, timeout)
        print("  ① 老写法（%s）：%s  %s"
              % (label, "通" if old_ok else "不通", old_why))
        print("  ② 本模块出口（强制直连）：%s  %s"
              % ("通" if new_ok else "不通", new_why))
        results.append((old_ok, new_ok))
        return new_ok

    # ---- ① 环境变量里的（死）代理 ----
    print("\n=== 对照一：环境变量里的代理（植入 %s）===" % dead)
    try:
        for k in PROXY_ENV_KEYS:
            os.environ[k] = dead
        print("  当前环境: %s" % (proxy_env_in() or "（无）"))

        def old_env_opener():
            # 不给参数 = 默认行为，会去 getproxies() 读环境变量/系统设置
            return urllib.request.build_opener(
                urllib.request.ProxyHandler(),
                urllib.request.HTTPSHandler(), urllib.request.HTTPHandler())

        ok_env = compare("读环境代理", old_env_opener)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ---- ② macOS 系统设置里的（死）代理 ----
    ok_sys = None
    reader = getattr(urllib.request, "getproxies_macosx_sysconf", None)
    print("\n=== 对照二：macOS 系统设置里的代理（模拟成 %s）===" % dead)
    if reader is None:
        print("  这台机器不是 macOS，跳过（系统代理这条路只在 macOS 上有）")
    else:
        real_reader = reader
        # ⚠️ 必须连 NO_PROXY 一起清。urllib 的逻辑是
        #     getproxies() = getproxies_environment() or getproxies_macosx_sysconf()
        #   而 getproxies_environment() 会把 NO_PROXY 也收进去，于是哪怕只设了
        #   `NO_PROXY=localhost`，返回的字典也**非空（真值）**，`or` 直接短路，
        #   系统设置那条路根本不会被走到。不清它就测不到系统代理。
        #   （这也解释了为什么有些人"明明开了系统代理，脚本却当没看见"。）
        no_proxy_keys = ("NO_PROXY", "no_proxy")
        saved2 = {k: os.environ.get(k) for k in PROXY_ENV_KEYS + no_proxy_keys}
        try:
            for k in PROXY_ENV_KEYS + no_proxy_keys:
                os.environ.pop(k, None)
            urllib.request.getproxies_macosx_sysconf = \
                lambda: {"http": dead, "https": dead}
            print("  当前环境: %s（环境变量已清空，只让系统代理生效）"
                  % (proxy_env_in() or "（无环境变量）"))

            def old_sys_opener():
                # getproxies() = 环境优先，环境为空才读系统设置；这里环境为空
                return urllib.request.build_opener(
                    urllib.request.ProxyHandler(),
                    urllib.request.HTTPSHandler(), urllib.request.HTTPHandler())

            ok_sys = compare("读系统代理", old_sys_opener)
        finally:
            urllib.request.getproxies_macosx_sysconf = real_reader
            for k, v in saved2.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    print("\n环境已还原：%s" % (proxy_env_in() or "（无代理变量）"))

    hard = [r for r in results if not r[1]]
    if hard:
        print("➜ 结论：直连本身就不通 —— 问题在这台机器到 %s 的网络，"
              "与代理无关，换网络或稍后再试。" % url)
        return 1
    if all(r[0] for r in results):
        print("➜ 注：两种代理在这台机器上碰巧都能通，"
              "但至少证明了本模块不依赖代理。")
        return 0
    print("➜ 结论：代理确实会拦死请求，而本模块两条路都不受影响 —— "
          "直连生效 ✓")
    return 0


def _main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="rhpsd.net",
                                description="PSD 分层算法网络出口自检")
    p.add_argument("--check", action="store_true",
                   help="打印代理现状并做一次真实连通性探测")
    p.add_argument("--selftest", action="store_true",
                   help="植入死代理做对照实验，证明请求不受代理影响")
    p.add_argument("--url", default="https://www.runninghub.cn",
                   help="探测目标（默认 RunningHub 主页）")
    p.add_argument("--timeout", type=float, default=15.0)
    args = p.parse_args(argv)

    info = describe()
    print("=== 网络出口自检 ===")
    print("环境变量里的代理：%s" % (
        "、".join("%s=%s" % kv for kv in info["env_proxies"].items())
        or "（无）"))
    print("urllib 默认会读到的代理：%s" % (info["urllib_proxies"] or "（无）"))
    print("本模块出口是否挂了代理处理器：%s（False = 直连）"
          % info["outlet_has_proxy_handler"])
    if info["env_proxies"] and not info["outlet_has_proxy_handler"]:
        print("→ 环境里确实有代理变量，但已被本模块挡在门外；"
              "子进程环境还会经 sanitize_env() 摘掉。")

    if args.selftest:
        return _selftest(args.url, args.timeout)

    if not args.check:
        return 0
    ok, detail = _probe(args.url, args.timeout)
    print("\n探测 %s" % args.url)
    print("  结果：%s  %s" % ("通" if ok else "不通", detail))
    if not ok:
        print("  → 直连都不通，说明是这台机器到目标的网络问题，"
              "不是代理造成的；换网络或稍后再试。")
    return 0 if ok else 1


if __name__ == "__main__":                            # pragma: no cover
    sys.exit(_main())
