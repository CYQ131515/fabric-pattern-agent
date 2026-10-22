"""视觉模型客户端：一个 OpenAI 兼容接口，通吃本地和云端。

为什么只写一个客户端就够
    Ollama、LM Studio、llama.cpp server、vLLM、以及各家云端，全都提供
    `/v1/chat/completions` 这个 OpenAI 兼容端点，图片都用
    `{"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}` 传。
    所以不需要为每个运行时写适配层，改 base_url 和 model 就行。

    默认指向**本项目自带的本地引擎**（http://127.0.0.1:11435/v1，见
    ../本地模型/）—— 对"给花型图批量打标签"这件事，本地跑是最划算的：
    不按张计费、不用把客户图传出去、批几千张也不用看账单。

    自带引擎走 Apple 原生 MLX（mlx-vlm），在 M 系列芯片上比通用运行时快、
    内存也省。但它不是唯一选择：任何 OpenAI 兼容端点都能用，换 base_url 就行 ——
      · Ollama        --base-url http://127.0.0.1:11434/v1
      · LM Studio     --base-url http://127.0.0.1:1234/v1
      · llama.cpp     --base-url http://127.0.0.1:8080/v1
      · 云端（通义 / 智谱 / OpenAI 等）  --base-url https://... --api-key sk-xxx

为什么不用 Python 的模型库（transformers / mlx-vlm）
    这是刻意的取舍，跟 算法/psd分层 把分割丢给云端是同一个思路：
    重的推理放在外面托管，算法本身保持零依赖。这样环境坏了只影响这一个
    功能，不会把整个产品拖下水。

一个必须做的动作：直连，不走代理
    本地服务走系统代理会以各种奇怪的方式失败（超时、连接被重置），
    报出来的错还像"模型没起来"，很容易查错方向。
    所以这里显式把代理清空 —— 跟 算法/psd分层 的 rhpsd/net.py 同一条铁律。
"""
import base64
import json
import os
import socket
import urllib.error
import urllib.request

DEFAULT_BASE = 'http://127.0.0.1:11435/v1'
DEFAULT_MODEL = 'mlx-community/Qwen3.5-4B-MLX-4bit'

# 输出预算。给得比"一个 JSON"多不少 —— 因为 Qwen3.5 这类模型默认带
# thinking 模式，思考内容也吃预算，给少了 JSON 会被截断在中间。
# 截断时我们会加倍重试一次，所以这里不用一次给到顶。
MAX_TOKENS = 2000
RETRY_MAX_TOKENS = 5000


class VisionError(Exception):
    """带人话说明的错误。调用方直接把 str(e) 给用户看就行。"""


def _no_proxy_opener():
    """直连的 opener。显式清空代理，理由见模块头。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _friendly_http_error(code, body):
    """把 HTTP 错误翻成能指导下一步的人话。"""
    low = (body or '')[:400].lower()
    if code == 401 or code == 403:
        return '视觉服务说密钥不对（%d）。本地 Ollama 一般不用密钥，' \
               '如果你连的是云端，检查一下 api key。' % code
    if code == 404:
        if 'model' in low or 'not found' in low:
            return '视觉服务里没有这个模型（404）。先把它拉下来再试。'
        return '视觉服务的地址不对（404）。检查一下 base_url 有没有写错。'
    if code == 429:
        return '视觉服务在限流（429），等一会儿再跑，或者把并发调小。'
    if code >= 500:
        return '视觉服务自己出错了（%d）。' % code
    return '视觉服务返回了 %d。' % code


class Client:
    """一个视觉模型端点。构造后 available 为假也照样能建 —— 由调用方决定怎么兜。"""

    def __init__(self, base_url=None, model=None, api_key=None,
                 timeout=None, max_tokens=None, temperature=0.0,
                 retries=1, no_think=True):
        self.base_url = (base_url
                         or os.environ.get('ATAG_BASE_URL')
                         or _ollama_from_env()
                         or DEFAULT_BASE).rstrip('/')
        self.model = model or os.environ.get('ATAG_MODEL') or DEFAULT_MODEL
        self.api_key = api_key if api_key is not None \
            else (os.environ.get('ATAG_API_KEY') or '')
        self.timeout = float(timeout or os.environ.get('ATAG_TIMEOUT') or 180)
        self.max_tokens = int(max_tokens or os.environ.get('ATAG_MAX_TOKENS')
                              or MAX_TOKENS)
        self.temperature = float(temperature)
        self.retries = max(0, int(retries))
        # 关掉思考模式。打标签不需要推理链，开着只是让每张图慢好几倍。
        # 不支持这个参数的服务会忽略它，不会报错。
        self.no_think = bool(no_think)
        self._opener = _no_proxy_opener()
        self.n_calls = 0
        # 实际要写在请求里的模型名。起点是 self.model，第一次调用时问服务确认。
        self._resolved = None

    # ---------- 探活 ----------
    def list_models(self):
        """列出端点上有哪些模型。失败抛 VisionError。"""
        req = urllib.request.Request(self.base_url + '/models',
                                     headers=self._headers())
        try:
            raw = self._opener.open(req, timeout=min(self.timeout, 15)).read()
        except urllib.error.HTTPError as e:
            raise VisionError(_friendly_http_error(
                e.code, e.read().decode('utf-8', 'replace')))
        except Exception as e:                            # noqa: BLE001
            raise VisionError('连不上视觉服务（%s）：%s'
                              % (self.base_url, _conn_hint(e)))
        try:
            data = json.loads(raw.decode('utf-8', 'replace'))
        except ValueError:
            raise VisionError('视觉服务的返回读不出模型列表。')
        items = data.get('data') if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        out = []
        for it in items:
            name = (it or {}).get('id') if isinstance(it, dict) else None
            if name:
                out.append(str(name))
        return out

    @property
    def available(self):
        try:
            return bool(self.list_models())
        except VisionError:
            return False

    @staticmethod
    def _match(names, want, allow_single=True):
        """在服务回的模型名里找 want 对应哪一个，找不到给 None。

        为什么要这么多层：Ollama 的 tag 可能带量化后缀（qwen3.5:4b-q4_K_M），
        自带引擎是用本地路径加载的、/v1/models 会把整个路径当名字回过来
        （…/模型/Qwen3.5-4B-MLX-4bit），而我们配的是 HuggingFace 仓库名
        （mlx-community/Qwen3.5-4B-MLX-4bit）。所以按
        「精确 → 目录名对得上 → 前缀」的顺序认。

        allow_single 控制最后一档兜底：服务上**只有一个**模型时算它匹配。
        请求时（resolve）该开 —— 单模型服务很常见，名字奇葩也该把活干了；
        自检时（has_model）必须关 —— 否则用户要 9B、服务只有 4B，
        probe 会谎报"就绪"，真跑起来才发现货不对板。
        """
        want = (want or '').strip()
        if not want:
            return None
        for n in names:
            if n == want:
                return n
        base = os.path.basename(want.rstrip('/'))
        if base:
            for n in names:
                if base in n:
                    return n
        for n in names:
            if n.startswith(want + ':') or n.startswith(want + '-'):
                return n
        if allow_single and len(names) == 1:
            return names[0]
        return None

    def has_model(self, name=None):
        """端点上有没有这个模型。匹配规则见 _match（不开单模型兜底）。"""
        want = (name or self.model or '').strip()
        if not want:
            return False
        try:
            names = self.list_models()
        except VisionError:
            return False
        return self._match(names, want, allow_single=False) is not None

    def resolve(self):
        """确认请求里到底该写哪个模型名，只问一次。

        为什么需要这一步：自带引擎是用**本地路径**加载模型的，它在
        /v1/models 里很可能把那个路径当模型名回给我们。我们配的是
        HuggingFace 仓库名，两者对不上就会被服务判成"没有这个模型"。
        所以用 _match 认一次，认出来之后所有请求都写它。
        """
        if self._resolved is not None:
            return self._resolved
        try:
            names = self.list_models()
        except VisionError:
            names = []
        pick = self._match(names, self.model) or (self.model or '')
        self._resolved = pick
        return self._resolved

    # ---------- 主调用 ----------
    def _headers(self):
        h = {'Content-Type': 'application/json'}
        if self.api_key:
            h['Authorization'] = 'Bearer ' + self.api_key
        return h

    def _payload(self, prompt, image_b64, mime, max_tokens):
        url = image_b64 if str(image_b64).startswith('data:') \
            else 'data:%s;base64,%s' % (mime or 'image/png', image_b64)
        body = {
            'model': self._resolved or self.model,
            'messages': [{'role': 'user', 'content': [
                {'type': 'text', 'text': prompt},
                {'type': 'image_url', 'image_url': {'url': url}},
            ]}],
            'temperature': self.temperature,
            'max_tokens': int(max_tokens),
            'stream': False,
        }
        if self.no_think:
            # vLLM / 部分 OpenAI 兼容服务认这个；Ollama 会忽略
            body['chat_template_kwargs'] = {'enable_thinking': False}
        return body

    def _post(self, body):
        req = urllib.request.Request(
            self.base_url + '/chat/completions',
            data=json.dumps(body).encode('utf-8'),
            headers=self._headers())
        try:
            raw = self._opener.open(req, timeout=self.timeout).read()
        except urllib.error.HTTPError as e:
            raise VisionError(_friendly_http_error(
                e.code, e.read().decode('utf-8', 'replace')))
        except socket.timeout:
            raise VisionError('视觉模型超时了（%d 秒没回）。'
                              '图小一点、模型小一点，或者把超时调大。'
                              % int(self.timeout))
        except Exception as e:                            # noqa: BLE001
            raise VisionError('调视觉模型失败：%s'
                              % _conn_hint(e))
        try:
            data = json.loads(raw.decode('utf-8', 'replace'))
        except ValueError:
            raise VisionError('视觉模型返回的不是 JSON。')
        return data

    def see(self, prompt, image_b64, mime='image/png'):
        """发一张图 + 提示词，返回 (文本, 用量)。

        截断（finish_reason=length）时把预算翻倍重试一次 —— 跟主程序
        处理推理模型截断的做法一致：思考也算 token，一次给足反而更省。
        """
        if not image_b64:
            raise VisionError('没有图片数据。')

        self.resolve()          # 第一次调用时确认服务认的模型名，见 resolve()
        budget = self.max_tokens
        last = ''
        tries = self.retries + 1
        for i in range(tries):
            body = self._payload(prompt, image_b64, mime, budget)
            self.n_calls += 1
            data = self._post(body)

            choices = data.get('choices') or []
            if not choices:
                raise VisionError('视觉模型没返回任何内容。')
            ch = choices[0] or {}
            msg = ch.get('message') or {}
            text = msg.get('content')
            if isinstance(text, list):
                text = ''.join(seg.get('text', '') for seg in text
                               if isinstance(seg, dict))
            text = (text or '').strip()
            if text:
                return text, (data.get('usage') or {})

            last = str(ch.get('finish_reason') or '')
            # 空内容 + 因为长度被砍 → 加倍预算再来一次
            if ch.get('finish_reason') == 'length' and budget < RETRY_MAX_TOKENS:
                budget = min(RETRY_MAX_TOKENS, budget * 2)
                continue
            if i == tries - 1:
                break

        raise VisionError('视觉模型没吐出内容（finish_reason=%s）。'
                          '换个模型或把 max_tokens 调大再试。' % (last or '?'))

    def see_file(self, prompt, path):
        from .image import load_for_model
        b64, mime = load_for_model(path)
        return self.see(prompt, b64, mime)

    def describe(self):
        return {'base_url': self.base_url, 'model': self.model,
                'resolved_model': self._resolved or self.model,
                'no_think': self.no_think, 'max_tokens': self.max_tokens,
                'timeout': self.timeout, 'calls': self.n_calls}


def _ollama_from_env():
    """Ollama 装好后常见于 127.0.0.1:11434，用户也可能自定义 OLLAMA_HOST。"""
    host = (os.environ.get('OLLAMA_HOST') or '').strip()
    if not host:
        return None
    if not host.startswith('http'):
        host = 'http://' + host
    host = host.rstrip('/')
    return host if host.endswith('/v1') else host + '/v1'


def _conn_hint(e):
    """连接类错误的提示。分两种：服务没起来 vs 起了但不是这个口。"""
    msg = str(e)
    if isinstance(e, urllib.error.URLError):
        reason = getattr(e, 'reason', None)
        msg = str(reason) if reason else msg
    if 'Connection refused' in msg or 'Connect call failed' in msg \
            or 'actively refused' in msg:
        return '端口上没人监听 —— 视觉模型服务没起来。'
    if 'Name or service not known' in msg or 'nodename' in msg:
        return '这个地址解析不了，检查一下 base_url。'
    return msg
