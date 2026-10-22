"""网关客户端。

两个能力分别走两条**独立的**线（网关和 key 都可以不一样）：
    analyze()  → deepseek-flash          看图，判断图案类型与接缝问题（只出文字）
                                          默认走 DeepSeek 官方 api.deepseek.com/v1
    edit()     → gpt-image-2.5-flare-1k  按诊断结果重绘中央十字带（出图）
                                          默认走 ApiMarket apimarket.site/v1

为什么要把两条线拆开
    "能看图而且便宜"和"画得好"不是同一件事的最优解。现在就是分开的：
    诊断走 DeepSeek 官方（原生支持图片输入、按 token 计费、便宜），
    出图走聚合网关。所以两边各给一套凭据；**只给一套时自动落回单线**，
    单网关的老用法照样能用。

接口契约（都是实测确认过的，别想当然）
    出图有两种版本的契约，按响应形状自动识别，两种都支持：
      A 版（ApiMarket 现在这套）
          POST /images/generations -> 顶层 {"id": "image_xxx", "status": "queued"}
          GET  /images/{id}        -> {"status": "completed",
                                       "result": {"image_url": "https://..."}}
      B 版（原 APIMart）
          POST /images/generations -> {"data": [{"task_id": "xxx"}]}
          GET  /tasks/{task_id}    -> data.status / data.result.images[0].url[0]

    带 -1k 后缀的出图模型**尺寸锁死在白名单**（见 ALLOWED_SIZES）：
    多一像素都不行 —— 1280x1280、1152x896、1024x640 全被 400 拒掉。
    所以进模型前必须吸附，见 snap_size()。

    这个渠道（Nexhina）**不支持 mask_url**，会回 400「暂不支持遮罩编辑」。
    这时自动降级为"只用指令约束"。注意这不影响保真：
    带外像素本来就**不靠模型自觉**，而是由 compose() 强制还原原图，
    所以遮罩只是给模型的一个提示，少了它最差是接缝补得糙一点。

    对话接口还是 OpenAI 那套；但两个网关都有坑：
      · 必须显式传 "stream": false，否则返回 SSE 流、解析会炸
      · 图片按 {"type":"image_url","image_url":{"url":"data:image/jpeg;base64,..."}}
      · deepseek-flash 是推理模型，reasoning_content 也吃 max_tokens，要给足
"""
from __future__ import annotations

import http.client
import io
import json
import math
import socket
import time
import urllib.error
import urllib.request

import numpy as np
from PIL import Image

from .io_utils import from_pil, mask_to_data_url, to_data_url, to_pil

# 出图线（APIMart）。诊断线默认落回这里 —— 同一个网关也代理 deepseek。
DEFAULT_BASE = "https://api.apimart.ai/v1"
# 诊断线的默认网关。默认与出图一致；要走自己的网关时显式传
# analyze_base= / --analyze-base（并给 analyze_key，才会真的分线）。
DEFAULT_ANALYZE_BASE = "https://api.apimart.ai/v1"

ANALYZE_MODEL = "deepseek-v4.1-flash"
IMAGE_MODEL = "gpt-image-2.5-flare"

# 哪些网关**锁死出图尺寸**，必须先把处理尺寸吸附到 ALLOWED_SIZES 才发得出去。
#   apimarket.site —— 实测 -1k 档只吃那七档，多一像素就 400
#   原 APIMart     —— 不锁，只要求"16 的倍数 / 总像素够 / 比例 ≤ 3:1"，
#                     832x816 这类直接能用，吸附反而会平白拉伸一下
# 所以吸附只在锁尺寸的网关上做，别一刀切（见 pipeline 里的 sizelock 判断）。
SIZE_LOCKED_HOSTS = ("apimarket.site", "apimarket.fun")

# 图片 CDN 挂在 Cloudflare 后面，会按 UA 拦人：Python 默认的
# "Python-urllib/3.x" 直接吃 403「error code: 1010」（浏览器完整性检查），
# 换成浏览器 UA 就正常 200。这不是可选优化 —— 不加就永远下不到图，
# 而且 403 看起来特别像"key 没权限"，能白查半天。
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")

# -1k 档模型只吃这几个尺寸（逐个实测过）。
# 规律是"必须有一条边正好是 1024"，另一边只能是标准档位之一；
# 1280x1280 / 1536x1536 / 1152x896 / 1024x640 / 1024x2048 全部被 400 拒。
ALLOWED_SIZES = ((1024, 1024), (1280, 1024), (1536, 1024),
                 (1024, 1280), (1024, 1536), (1024, 768), (768, 1024))

# 给诊断模型的系统指令。要求它输出结构化 JSON，方便程序消费、也方便人复核。
ANALYZE_SYSTEM = """你是纺织印花的四方连续（无缝平铺）工艺工程师。
用户会给你两张图：
  图A = 原始花型；
  图B = 同一张图平移半幅后的"待修补图"，四处接缝被集中到了画面正中央的十字位置。
你的任务：判断这张花型该怎么平铺、中央十字处现在坏在哪里。

要求：直接输出 JSON，不要长篇推理，每条描述不超过 30 字。
只输出一个 JSON 对象，不要任何多余文字、不要 markdown 代码块：
{
  "pattern_type": "满地散点 / 连缀骨架 / 几何规整 / 定位独幅 / 其他",
  "density": "清地 / 混地 / 满地",
  "main_seam_problems": ["最多 3 条，每条 30 字以内"],
  "repair_strategy": "一句话，40 字以内",
  "repair_prompt": "给图像编辑模型的英文指令，60 词以内；只说明中央十字处如何接续，不要解释图被平移过",
  "suitable": true
}
说明：suitable 表示这张图是否适合做满地平接。定位独幅、大面积留白、长笔触抽象图通常为 false。"""


class APIMartError(RuntimeError):
    pass


# ---------------------------------------------------------------- IPv4 优先
# 结果图的 CDN 挂在 Cloudflare 后面，解析出来 AAAA 排在 A 前面。这台机器到
# Cloudflare 的 **IPv6 是「连得上但极慢」**（不是不通，所以不会报错）：
#
#     同一张 562 KB 的图   走 IPv6 →  4 KB/s，152 秒
#                          走 IPv4 → 209 KB/s， 2.6 秒
#
# urllib 不像 curl 有 Happy Eyeballs（并发探两族、先通先用），它老老实实
# 按 getaddrinfo 的顺序连第一个 —— 于是每张图白等两分半，一次四方连续要下
# 两三张图，全程能被这一项拖掉 5 分钟。跟代理毫无关系（这台机器根本没开代理）。
#
# 所以这里把 IPv4 排到前面。注意不是"禁用 IPv6"：同一个地址族内部保持原顺序，
# IPv4 全失败时仍会顺着列表退到 IPv6，专网/纯 v6 环境照样能用。

def _v4_first_socket(address, timeout, source_address=None):
    """create_connection 的替身：按「IPv4 优先」的顺序逐个试地址。"""
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
                except Exception:            # noqa: BLE001
                    pass
    raise last or OSError("连不上 %s" % (host,))


class _V4FirstHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        self.sock = _v4_first_socket((self.host, self.port), self.timeout,
                                     self.source_address)
        if self._tunnel_host:
            self._tunnel()
        self.sock = self._context.wrap_socket(self.sock,
                                              server_hostname=self.host)


class _V4FirstHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = _v4_first_socket((self.host, self.port), self.timeout,
                                     self.source_address)
        if self._tunnel_host:
            self._tunnel()


class _V4FirstHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_V4FirstHTTPSConnection, req, context=self._context)


class _V4FirstHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_V4FirstHTTPConnection, req)


def _opener(no_proxy: bool):
    """带 IPv4 优先的 opener。

    no_proxy=True  → 完全不碰代理（默认路径）
    no_proxy=False → 读环境/系统代理（兜底路径，见 APIMart._open）
    """
    return urllib.request.build_opener(
        _V4FirstHTTPSHandler(), _V4FirstHTTPHandler(),
        urllib.request.ProxyHandler({} if no_proxy else None))


def snap_size(w: int, h: int, allowed: tuple = ALLOWED_SIZES) -> tuple[int, int]:
    """把任意处理尺寸吸附到白名单里**比例最接近**的一档。

    为什么允许改比例：模型只在中央十字带里动笔，带外像素随后由 compose()
    强制还原成原图，交付前还会 resize 回原图尺寸。所以中间过程的轻微拉伸
    不会留在最终产物上 —— 换来的是"一次就发合法请求"，不用拿 400 试错。

    用 log 比值排序，是为了让 1.5 和 0.667 这种互为倒数的比例对称
    （直接比差值会把 0.667 算得比 1.5 更远，横竖图就会选得不一致）。
    """
    if not allowed:
        return w, h
    target = math.log(w / float(h))
    return min(allowed, key=lambda s: abs(math.log(s[0] / float(s[1])) - target))


def size_plan(size: tuple, allowed: tuple = ALLOWED_SIZES, extra: int = 2) -> list:
    """排出"先试哪个尺寸、被拒了再试哪个"的顺序，按比例接近度。

    首选就是 snap_size 的结果；后面几档是保险 —— 万一网关的白名单
    比实测的更严（换了渠道、换了档位），也不至于整条链路硬失败。
    """
    first = snap_size(size[0], size[1], allowed)
    ranked = sorted(allowed,
                    key=lambda s: abs(math.log(s[0] / float(s[1]))
                                      - math.log(size[0] / float(size[1]))))
    plan = [first] + [s for s in ranked if s != first]
    return plan[:max(1, extra + 1)]


def _size_rejected(text: str) -> bool:
    return ("锁定" in text) or ("不接受" in text) or ("size" in text.lower()
                                                    and "invalid" in text.lower())


def _mask_unsupported(text: str) -> bool:
    low = text.lower()
    return ("遮罩" in text) or ("mask" in low) or ("暂不支持" in text)


class APIMart:
    """网关客户端。只依赖标准库 + Pillow + NumPy。

    构造
        APIMart(key, base_url)                     # 单网关：诊断和出图共用
        APIMart(key, base_url, analyze_key=...,     # 双线：诊断走自己的网关
                analyze_base=DEFAULT_ANALYZE_BASE)
    """

    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE, *,
                 analyze_key: str = "", analyze_base: str = "",
                 timeout: int = 180, poll_interval: float = 3.0,
                 max_wait: float = 420.0, retries: int = 3):
        self.image_key = (api_key or "").strip()
        if not self.image_key:
            raise APIMartError("缺少 API key")
        self.image_base = (base_url or DEFAULT_BASE).rstrip("/")

        # 诊断线：**显式给了自己的 key 才分线**，否则整条落回主线。
        # 这样"只配一个 key"的老用法不会因为默认 base 变了而把 key 发错网关。
        self.analyze_key = (analyze_key or "").strip()
        self.analyze_base = (analyze_base or DEFAULT_ANALYZE_BASE).rstrip("/")
        if not self.analyze_key:
            self.analyze_key = self.image_key
            self.analyze_base = self.image_base
        self.split_lines = (self.analyze_base != self.image_base
                            or self.analyze_key != self.image_key)

        # 这个网关锁不锁出图尺寸（锁了才需要在进模型前 snap_size 吸附）。
        self.size_locked = any(h in self.image_base for h in SIZE_LOCKED_HOSTS)

        self.timeout = timeout
        self.poll_interval = poll_interval
        self.max_wait = max_wait
        self.retries = retries
        self.cost_total = 0.0
        self.log: list[str] = []
        self._mask_ok: bool | None = None      # None = 还没试过
        self._poll_kind = ""                   # 出图契约：image / task
        self._plain = _opener(no_proxy=True)     # 直连（且 IPv4 优先）
        self._proxy = _opener(no_proxy=False)    # 读环境/系统代理，兜底用

    # ---------------------------------------------------------------- 底层
    @property
    def mask_supported(self):
        """渠道支不支持遮罩编辑。None = 还没试过（第一次出图时才探明）。"""
        return self._mask_ok

    def _open(self, req, timeout):
        """发请求。**优先直连**，直连真不动了才退回走系统代理。

        为什么优先直连：urllib 在 macOS 上会去读系统网络设置里的代理
        （_scutil --proxy）。这类开发机上的代理常常只对特定域名友好，
        走它反而会把本来能通的请求打回 403/502，而且伪装成业务错误
        （看着像 key 不对），极难查。所以默认直连；只有连接层真的异常，
        或拿到 403/407 这种"看着就像代理拦的"码，才退回走代理。
        """
        try:
            return self._plain.open(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code not in (403, 407):
                raise                     # 真业务错误，别拿代理再打一遍
            try:
                return self._proxy.open(req, timeout=timeout)
            except Exception:              # noqa: BLE001
                raise exc
        except Exception:                  # noqa: BLE001
            return self._proxy.open(req, timeout=timeout)

    def _request(self, method: str, path: str, body: dict | None = None,
                 base: str | None = None, key: str | None = None):
        url = (base or self.image_base).rstrip("/") + path
        auth = key or self.image_key
        last: Exception | None = None
        for attempt in range(self.retries):
            req = urllib.request.Request(
                url, method=method,
                data=json.dumps(body).encode() if body is not None else None,
                headers={"Authorization": f"Bearer {auth}",
                         "Content-Type": "application/json"})
            try:
                with self._open(req, self.timeout) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
                if exc.code < 500:            # 4xx 重试没意义，直接抛
                    raise APIMartError(f"HTTP {exc.code}: {detail}") from exc
                last = APIMartError(f"HTTP {exc.code}: {detail}")
            except Exception as exc:          # noqa: BLE001  网络抖动就重试
                last = exc
            if attempt < self.retries - 1:
                time.sleep(2 * (attempt + 1))
        raise APIMartError(f"{method} {path} 失败（已重试 {self.retries} 次）：{last}")

    def _fetch(self, url: str) -> bytes:
        """把出图结果下载回来。必须带浏览器 UA（见 BROWSER_UA 的说明）。

        CDN 抖动或限流（429/5xx）就重试；其它 4xx 是死结，重试没意义。
        """
        last: Exception | None = None
        for attempt in range(self.retries):
            req = urllib.request.Request(url, headers={
                "User-Agent": BROWSER_UA, "Accept": "image/*,*/*"})
            try:
                with self._open(req, self.timeout) as resp:
                    return resp.read()
            except Exception as exc:                   # noqa: BLE001
                last = exc
                code = getattr(exc, "code", None)
                if code is not None and not (code in (403, 429) or code >= 500):
                    break
                if attempt < self.retries - 1:
                    time.sleep(2 * (attempt + 1))
        raise APIMartError("下载出图结果失败（%s）：%s" % (type(last).__name__, last))

    def list_models(self, analyze: bool = False) -> list[str]:
        data = self._request("GET", "/models",
                             base=self.analyze_base if analyze else None,
                             key=self.analyze_key if analyze else None)
        return [m.get("id", "") for m in (data.get("data") or [])]

    # ------------------------------------------------------------ 文字诊断
    def chat(self, model: str, messages: list, max_tokens: int = 2000,
             temperature: float = 0.3, on_analyze_line: bool = True) -> str:
        body = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": False,          # 不传会返回 SSE，解析会炸
        }
        if temperature is not None:
            body["temperature"] = temperature
        data = self._request(
            "POST", "/chat/completions", body,
            base=self.analyze_base if on_analyze_line else None,
            key=self.analyze_key if on_analyze_line else None)
        choices = data.get("choices") or []
        if not choices:
            raise APIMartError("对话接口没有返回 choices：" + str(data)[:300])
        return (choices[0].get("message") or {}).get("content", "") or ""

    def analyze(self, original: np.ndarray, to_edit: np.ndarray,
                model: str = ANALYZE_MODEL) -> dict:
        """让视觉模型看图，输出结构化的接缝诊断。

        max_tokens 要给得很宽：deepseek 这代是推理模型，推理过程的 token
        也从这个额度里扣。额度给少了会**推理把额度吃光、content 直接为空**
        （解析出来是空字符串）。为空就抬额度重试一次。
        """
        msgs = [
            {"role": "system", "content": ANALYZE_SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text":
                    "图A（原始花型）：\n"
                    "图B（待修补图，接缝已集中到画面中央十字）：\n"
                    "请诊断并按要求输出 JSON。"},
                {"type": "image_url", "image_url": {"url": to_data_url(original)}},
                {"type": "image_url", "image_url": {"url": to_data_url(to_edit)}},
            ]},
        ]
        raw = ""
        for attempt, budget in enumerate((8000, 16000)):
            raw = self.chat(model, msgs, max_tokens=budget)
            if raw.strip():
                break
            self.log.append(f"诊断第 {attempt + 1} 次 content 为空，提高额度重试")
        self.log.append(f"诊断模型 {model} 返回 {len(raw)} 字符")
        return _parse_json(raw)

    # ------------------------------------------------------------ 图像编辑
    def _submit_image(self, body: dict) -> str:
        """提交出图，返回任务 id，并记下这次用的是哪版契约。"""
        data = self._request("POST", "/images/generations", body)
        tid = data.get("id")
        if isinstance(tid, str) and tid.startswith("image_"):
            self._poll_kind = "image"          # A 版：ApiMarket
            return tid
        items = data.get("data") or []
        tid = (items[0] if items else {}).get("task_id")
        if tid:
            self._poll_kind = "task"           # B 版：原 APIMart
            return tid
        raise APIMartError("出图接口没返回任务 id：" + str(data)[:300])

    def edit(self, image: np.ndarray, prompt: str, size: tuple[int, int],
             mask: np.ndarray | None = None, model: str = IMAGE_MODEL,
             extra: dict | None = None) -> np.ndarray:
        """局部重绘。mask 里 255 = 允许改，None 表示整图交给模型。

        两处自动降级（都是为了"别让渠道差异把整条链路打死"）：
          · 尺寸：先按比例吸附到白名单；仍被拒就换次接近的档位再试
          · 遮罩：渠道不支持 mask_url 时去掉它重试，并把结论缓存下来，
                  后续请求不再白发一次（实测降级后出图完全正常）
        """
        base_body = {
            "model": model,
            "prompt": prompt,
            "image_urls": [to_data_url(image)],
        }
        if extra:
            base_body.update(extra)

        want_mask = mask is not None and self._mask_ok is not False
        submitted: tuple[str, tuple] | None = None
        last_exc: Exception | None = None
        plan = size_plan(size)

        for idx, s in enumerate(plan):
            body = dict(base_body, size="%dx%d" % (s[0], s[1]))
            if want_mask:
                body["mask_url"] = mask_to_data_url(mask)
            try:
                submitted = (self._submit_image(body), s)
                if want_mask and self._mask_ok is None:
                    self._mask_ok = True
                break
            except APIMartError as exc:
                last_exc = exc
                text = str(exc)
                # 遮罩不支持 → 剥掉遮罩，尺寸不变再试（不算消耗尺寸档位）
                if want_mask and self._mask_ok is not False and _mask_unsupported(text):
                    self._mask_ok = False
                    want_mask = False
                    self.log.append("该渠道不支持遮罩编辑，改为只用指令约束"
                                    "（带外像素仍由算法强制还原，保真不受影响）")
                    body.pop("mask_url", None)
                    try:
                        submitted = (self._submit_image(body), s)
                        break
                    except APIMartError as exc2:
                        last_exc = exc2
                        text = str(exc2)
                if not _size_rejected(text) or idx == len(plan) - 1:
                    raise
                self.log.append("网关不接受 %dx%d，换 %dx%d 再试"
                                % (s[0], s[1], plan[idx + 1][0], plan[idx + 1][1]))
        if submitted is None:
            raise last_exc or APIMartError("出图没提交成功")

        tid, used = submitted
        if (used[0], used[1]) != (size[0], size[1]):
            self.log.append("出图尺寸 %dx%d → %dx%d（比例接近，交付前会还原）"
                            % (size[0], size[1], used[0], used[1]))

        result = self._poll(tid)
        url = _pick_url(result)
        if not url:
            raise APIMartError("任务完成但没有图片：" + str(result)[:300])

        out = from_pil(Image.open(io.BytesIO(self._fetch(url))))
        cost = result.get("cost")
        if isinstance(cost, (int, float)):
            self.cost_total += float(cost)
        self.log.append(f"出图 {model} {out.shape[1]}x{out.shape[0]} 成本 {cost}")
        return out

    def _poll(self, task_id: str) -> dict:
        """等任务完成。两种契约的终态形状不同，返回统一的 dict（含 result）。"""
        path = f"/images/{task_id}" if self._poll_kind == "image" else f"/tasks/{task_id}"
        deadline = time.monotonic() + self.max_wait
        while time.monotonic() < deadline:
            data = self._request("GET", path)
            d = data.get("data") if self._poll_kind == "task" else data
            d = d or {}
            status = d.get("status")
            if status in ("completed", "succeeded", "success"):
                return d
            if status in ("failed", "error", "canceled", "cancelled"):
                raise APIMartError("出图任务失败："
                                   + str(d.get("error") or d.get("fail_reason")
                                         or d)[:300])
            time.sleep(self.poll_interval)
        raise APIMartError(f"任务 {task_id} 超时（{self.max_wait:.0f}s）未完成")


def _pick_url(d: dict) -> str | None:
    """从终态里取图片地址。A 版在 result.image_url，B 版在 result.images[0].url[0]。"""
    res = d.get("result") or {}
    url = res.get("image_url")
    if isinstance(url, str) and url:
        return url
    imgs = res.get("images") or []
    if imgs:
        u = imgs[0].get("url")
        if isinstance(u, list):
            return u[0] if u else None
        if isinstance(u, str):
            return u
    u = d.get("image_url") or d.get("url")
    return u if isinstance(u, str) and u else None


def _parse_json(text: str) -> dict:
    """模型有时会裹一层 markdown 代码块或加解释，尽量把 JSON 抠出来。"""
    s = (text or "").strip()
    if s.startswith("```"):
        parts = s.split("```")
        if len(parts) > 1:
            s = parts[1]
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        s = s[i:j + 1]
    try:
        return json.loads(s)
    except Exception:                          # noqa: BLE001
        return {"_raw": text, "pattern_type": "未解析", "density": "未解析",
                "main_seam_problems": [], "repair_strategy": "",
                "repair_prompt": "", "suitable": None}
