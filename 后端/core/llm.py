"""OpenAI 兼容 Chat Completions 客户端（仅标准库）。

任何 OpenAI 兼容端点都能用：DeepSeek、通义、 moonshot、硅基流动、本地 vLLM / Ollama(需 /v1) 等。
"""
import json
import urllib.error
import urllib.request


class LLMError(Exception):
    """truncated=True 表示这次不是真失败，而是思考把 token 预算吃光了——
    换个更大的预算重试一次通常就过了（见 chat()）。"""
    truncated = False
    raw = ''


class LLMClient:
    def __init__(self, config):
        self.cfg = config

    @property
    def enabled(self):
        return self.cfg.llm_enabled

    def _url(self):
        return self.cfg.base_url + '/chat/completions'

    def chat(self, messages, tools=None, temperature=None, max_tokens=None,
             model=None, timeout=None, extra=None):
        """正常调一次；如果只是「思考吃光了预算」，自动加大预算重试一次。

        推理模型把思考 token 也算进 max_tokens，预算不够时正文是空的、finish_reason=length。
        这种情况降级到规则引擎很可惜（回答会变得很笨），多给点预算基本都能过，
        所以在这里兜一层，而不是把难题丢给上层。
        """
        base = (self.cfg.settings['llm'].get('max_tokens', 4000)
                if max_tokens is None else max_tokens)
        try:
            return self._once(messages, tools, temperature, base, model, timeout, extra)
        except LLMError as e:
            ceiling = int(self.cfg.settings['llm'].get('max_tokens_retry') or 0)
            bigger = max(int(base) * 3, 16000)
            if not getattr(e, 'truncated', False) or ceiling <= int(base):
                raise
            bigger = min(bigger, ceiling)
            if bigger <= int(base):
                raise
            # 重试时明确要求「直接说结论」，别让它在思考上再烧一遍预算
            retry_msgs = list(messages) + [{
                "role": "user",
                "content": "（内部提示，不要复述这句）请直接给出结论，思考尽量简短，"
                           "需要调工具就立刻调。"}]
            try:
                return self._once(retry_msgs, tools, temperature, bigger, model, timeout, extra)
            except LLMError as e2:
                # 两次都被截断：第一次好歹留下过半截正文，给它总比空手强
                if getattr(e2, 'truncated', False) and getattr(e, 'raw', ''):
                    return {"content": e.raw, "tool_calls": [],
                            "finish_reason": "length", "usage": None}
                raise

    def _once(self, messages, tools, temperature, max_tokens, model, timeout, extra):
        if not self.enabled:
            raise LLMError('未配置 api_key')
        body = {
            "model": model or self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.settings['llm'].get('temperature') if temperature is None else temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        # 思考强度。DeepSeek V4 系推理模型支持 effort（low/high/max），
        # 思考 token 是从 max_tokens 里扣的，调低能明显省钱提速。其他端点不认这个字段，所以留空就不发。
        effort = self.cfg.settings['llm'].get('effort')
        if effort:
            body["effort"] = effort
        if extra:
            body.update(extra)
        if tools and self.cfg.settings['llm'].get('tools_enabled', True):
            body["tools"] = tools
            body["tool_choice"] = "auto"
        req = urllib.request.Request(
            self._url(),
            data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.cfg.api_key},
            method='POST',
        )
        try:
            with urllib.request.urlopen(
                    req, timeout=timeout or self.cfg.settings['llm'].get('timeout', 60)) as r:
                data = json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8', 'ignore')[:300]
            raise LLMError('HTTP %s：%s' % (e.code, detail))
        except Exception as e:
            raise LLMError('请求失败：%s' % e)

        try:
            choice = data['choices'][0]
        except (KeyError, IndexError):
            raise LLMError('返回结构异常：%s' % json.dumps(data, ensure_ascii=False)[:300])
        msg = choice.get('message') or {}
        content = msg.get('content') or ''
        calls = msg.get('tool_calls') or []
        finish = choice.get('finish_reason')

        # 推理模型会把思考 token 也算进 max_tokens。预算不够时正文是空的但 finish_reason=length，
        # 这种"半个回答"比报错更危险（前端会冒一个空气泡），所以判成可重试的截断。
        if finish == 'length' and not calls:
            err = LLMError('思考过程把 token 预算吃光了，这次没吐出正文')
            err.truncated = True
            err.raw = (content or '')[:2000]
            if not content:
                raise err
            # 有半截正文：先留着，重试若更差也不会比空手强，交给上层决定
            raise err

        return {
            "content": content,
            "tool_calls": calls,
            "finish_reason": finish,
            "usage": data.get('usage'),
        }
