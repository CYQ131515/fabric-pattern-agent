"""
pattern_studio.imagegen —— 第 3 层：生图后端适配

三种 provider，接口一致，CLI 用 `--provider mock|openai|comfy` 切换：

  mock    : 程序化代笔（mockgen），不需要任何模型/网络，用于跑通与调参
  openai  : 任意 OpenAI 兼容的 /v1/images/generations（OpenAI、硅基流动、DashScope 兼容模式、
            火山 Ark、自建 one-api 等），只用到 prompt / size / n
  comfy   : 本地 ComfyUI HTTP API（默认 127.0.0.1:8188），用内置的 SD txt2img workflow 模板

统一约定：**生图模型只负责出图，不做任何裁剪/去背**；拆格与去背在 sheet.py。
"""
from __future__ import annotations

import base64
import io
import json
import re
import os
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from PIL import Image

from . import mockgen, prompts


# ------------------------------------------------------------------ 基类

class Provider:
    name = "base"

    def sheet(self, spec: dict, cells: list[dict], index: int, size: int = 1024, seed: int = 0) -> Image.Image:
        raise NotImplementedError

    def background(self, spec: dict, variant: int, size: int = 1024, seed: int = 0) -> Image.Image:
        raise NotImplementedError

    def element(self, spec: dict, el: dict, variant: int, size: int = 1024, seed: int = 0) -> Image.Image:
        raise NotImplementedError


# ------------------------------------------------------------------ mock

class MockProvider(Provider):
    name = "mock"

    def sheet(self, spec, cells, index, size=1024, seed=0):
        return mockgen.make_sheet(spec, cells, size=size, sheet_index=index, seed=seed)

    def background(self, spec, variant, size=1024, seed=0):
        return mockgen.make_background(spec, size=size, variant=variant, seed=seed)

    def element(self, spec, el, variant, size=1024, seed=0):
        cells = [{"element": el, "variant": variant, "element_index": 0}] * 9
        return mockgen.make_sheet(spec, cells, size=size, sheet_index=variant, seed=seed)


# ------------------------------------------------------------------ OpenAI 兼容

class OpenAIProvider(Provider):
    name = "openai"

    def __init__(self):
        self.base = (os.environ.get("PATTERN_STUDIO_IMAGE_BASE")
                     or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.key = os.environ.get("OPENAI_API_KEY", "")
        self.model = os.environ.get("PATTERN_STUDIO_IMAGE_MODEL", "gpt-image-1")
        self.quality = os.environ.get("PATTERN_STUDIO_IMAGE_QUALITY", "high")

    def _call(self, prompt: str, size: int) -> Image.Image:
        body = {"model": self.model, "prompt": prompt, "n": 1, "size": f"{size}x{size}"}
        if self.model.startswith("gpt-image"):
            body["quality"] = self.quality
        else:
            body["response_format"] = "b64_json"
        req = urllib.request.Request(
            self.base + "/images/generations",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {self.key}"} if self.key else {})},
        )
        with urllib.request.urlopen(req, timeout=300) as r:
            data = json.loads(r.read().decode())
        item = data["data"][0]
        if item.get("b64_json"):
            raw = base64.b64decode(item["b64_json"])
        else:
            with urllib.request.urlopen(item["url"], timeout=120) as rr:
                raw = rr.read()
        return Image.open(io.BytesIO(raw)).convert("RGBA")

    def sheet(self, spec, cells, index, size=1024, seed=0):
        p = prompts.sheet_prompt(spec, cells, index)
        return self._call(p["prompt"] + "\n\nAvoid: " + p["negative_prompt"], size)

    def background(self, spec, variant, size=1024, seed=0):
        p = prompts.background_variant_prompt(spec, variant)
        return self._call(p["prompt"] + "\n\nAvoid: " + p["negative_prompt"], size)

    def element(self, spec, el, variant, size=1024, seed=0):
        p = prompts.element_prompt(spec, el, variant)
        return self._call(p["prompt"] + "\n\nAvoid: " + p["negative_prompt"], size)


# ------------------------------------------------------------------ ComfyUI

def _comfy_workflow(prompt: str, negative: str, size: int, seed: int, ckpt: str) -> dict:
    """标准 SD txt2img（API 格式），节点 id 固定，便于替换。"""
    return {
        "3": {"class_type": "KSampler", "inputs": {
            "seed": seed, "steps": 28, "cfg": 7.0, "sampler_name": "dpmpp_2m",
            "scheduler": "karras", "denoise": 1.0, "model": ["4", 0],
            "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": size, "height": size, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["4", 1]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "pattern_studio", "images": ["8", 0]}},
    }


class ComfyProvider(Provider):
    name = "comfy"

    def __init__(self):
        self.host = os.environ.get("PATTERN_STUDIO_COMFY", "http://127.0.0.1:8188").rstrip("/")
        self.ckpt = os.environ.get("PATTERN_STUDIO_COMFY_CKPT", "")

    def _checkpoint(self) -> str:
        if self.ckpt:
            return self.ckpt
        with urllib.request.urlopen(self.host + "/object_info/CheckpointLoaderSimple", timeout=10) as r:
            info = json.loads(r.read().decode())
        ckpts = info["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
        if not ckpts:
            raise RuntimeError("ComfyUI 里没有 checkpoint")
        return ckpts[0]

    def _call(self, prompt: str, negative: str, size: int, seed: int) -> Image.Image:
        wf = _comfy_workflow(prompt, negative, size, seed, self._checkpoint())
        body = json.dumps({"prompt": wf, "client_id": str(uuid.uuid4())}).encode()
        req = urllib.request.Request(self.host + "/prompt", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            pid = json.loads(r.read().decode())["prompt_id"]
        for _ in range(600):
            time.sleep(1.0)
            with urllib.request.urlopen(f"{self.host}/history/{pid}", timeout=30) as r:
                hist = json.loads(r.read().decode())
            if pid in hist:
                outs = hist[pid]["outputs"]
                for node in outs.values():
                    for img in node.get("images", []):
                        q = urllib.parse.urlencode({"filename": img["filename"],
                                                    "subfolder": img.get("subfolder", ""),
                                                    "type": img.get("type", "output")})
                        with urllib.request.urlopen(f"{self.host}/view?{q}", timeout=60) as rr:
                            return Image.open(io.BytesIO(rr.read())).convert("RGBA")
        raise RuntimeError("ComfyUI 生成超时")

    def sheet(self, spec, cells, index, size=1024, seed=0):
        p = prompts.sheet_prompt(spec, cells, index)
        return self._call(p["prompt"], p["negative_prompt"], size, seed)

    def background(self, spec, variant, size=1024, seed=0):
        p = prompts.background_variant_prompt(spec, variant)
        return self._call(p["prompt"], p["negative_prompt"], size, seed)

    def element(self, spec, el, variant, size=1024, seed=0):
        p = prompts.element_prompt(spec, el, variant)
        return self._call(p["prompt"], p["negative_prompt"], size, seed)


# ------------------------------------------------------------------ 工厂

def make_provider(name: str = "auto") -> Provider:
    if name == "mock":
        return MockProvider()
    if name == "apimart":
        return ApimartProvider()
    if name == "openai":
        return OpenAIProvider()
    if name == "comfy":
        return ComfyProvider()
    # auto
    if os.environ.get("PATTERN_STUDIO_APIMART_KEY") or os.environ.get("APIMART_KEY"):
        print("[imagegen] 检测到 apimart key → 使用 apimart provider")
        return ApimartProvider()
    if os.environ.get("OPENAI_API_KEY") or os.environ.get("PATTERN_STUDIO_IMAGE_BASE"):
        print("[imagegen] 检测到 API Key → 使用 openai 兼容 provider")
        return OpenAIProvider()
    if os.environ.get("PATTERN_STUDIO_COMFY"):
        print("[imagegen] 检测到 PATTERN_STUDIO_COMFY → 使用 ComfyUI provider")
        return ComfyProvider()
    print("[imagegen] 未配置生图模型 → 使用 mock provider（程序化代笔，用于跑通流程）")
    return MockProvider()


# ------------------------------------------------------------------ Apimart（异步任务 + SSE 对话）

class ApimartProvider(Provider):
    """api.apimart.ai：图像走"提交任务 → 轮询 → 下载 URL"，对话走 SSE 流式。

    模型按角色配置（可用环境变量或 CLI 覆盖）：
      sheet_model  九宫格元素图  默认 gpt-image-2      $0.0085/张
      bg_model     背景图        默认 z-image-turbo    $0.01/张
      llm_model    需求/提示词   默认 deepseek-v4-flash（token 成本可忽略）
    """

    name = "apimart"

    def __init__(self, base: str | None = None, key: str | None = None,
                 sheet_model: str | None = None, bg_model: str | None = None,
                 llm_model: str | None = None, quality: str | None = "medium"):
        self.base = (base or os.environ.get("PATTERN_STUDIO_APIMART_BASE")
                     or os.environ.get("APIMART_BASE") or "https://api.apimart.ai/v1").rstrip("/")
        self.key = key or os.environ.get("PATTERN_STUDIO_APIMART_KEY") or os.environ.get("APIMART_KEY") or os.environ.get("OPENAI_API_KEY", "")
        self.sheet_model = sheet_model or os.environ.get("PATTERN_STUDIO_SHEET_MODEL", "gpt-image-2")
        self.bg_model = bg_model or os.environ.get("PATTERN_STUDIO_BG_MODEL", "z-image-turbo")
        self.llm_model = llm_model or os.environ.get("PATTERN_STUDIO_LLM_MODEL", "deepseek-v4-flash")
        self.quality = quality
        self.last_cost: dict[str, float] = {}
        self.cost_log: list[dict] = []

    # ---------- 基础 HTTP

    def _req(self, path: str, body: dict | None = None, timeout: int = 90, retries: int = 2):
        """HTTP 请求 + 瞬时错误重试（503/502/504/超时/隧道断开）。"""
        last: Exception | None = None
        for attempt in range(retries):
            try:
                return self._req_once(path, body, timeout)
            except Exception as e:  # noqa: BLE001
                last = e
                msg = str(e)
                # 连不上/超时：重试也只是再等一个 timeout，直接抛出
                if any(k in msg for k in ("timed out", "timeout",
                                          "Connection refused", "Network is unreachable",
                                          "Name or service not known")):
                    raise
                transient = any(k in msg for k in (
                    "503", "502", "504",
                    "Tunnel connection", "Connection reset", "Remote end closed"))
                if attempt >= retries - 1 or not transient:
                    raise
                time.sleep(1.5 * (attempt + 1))
        if last is not None:
            raise last

    def _req_once(self, path: str, body: dict | None, timeout: int) -> dict:
        url = self.base + path
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
            method="POST" if body is not None else "GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            ctype = r.headers.get("Content-Type", "")
        if "event-stream" in ctype or raw.lstrip().startswith("data:"):
            return {"__sse__": raw}
        return json.loads(raw)

    # ---------- 对话（SSE）

    def chat(self, system: str, user: str, temperature: float = 0.4,
             images: list | None = None, model: str | None = None) -> str:
        """对话（apimart 强制 SSE）。images=PIL 图列表时按 OpenAI vision 格式编码。"""
        if images:
            import base64 as _b64
            import io as _io

            content: list = [{"type": "text", "text": user}]
            for im in images:
                buf = _io.BytesIO()
                im.convert("RGB").save(buf, format="JPEG", quality=88)
                uri = "data:image/jpeg;base64," + _b64.b64encode(buf.getvalue()).decode()
                content.append({"type": "image_url", "image_url": {"url": uri}})
            msgs = [{"role": "system", "content": system}, {"role": "user", "content": content}]
        else:
            msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        body = {"model": model or self.llm_model, "messages": msgs, "temperature": temperature}
        res = self._req("/chat/completions", body, timeout=120)
        if "__sse__" in res:
            parts = []
            usage = None
            for line in res["__sse__"].split("\n"):
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload in ("[DONE]", ""):
                    continue
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if chunk.get("usage"):
                    usage = chunk["usage"]
                for ch in chunk.get("choices", []):
                    delta = ch.get("delta") or {}
                    parts.append(delta.get("content") or "")
            self.last_usage = usage
            return "".join(parts)
        self.last_usage = res.get("usage")
        return res["choices"][0]["message"]["content"]

    # ---------- 图像（异步任务）

    def _submit(self, model: str, prompt: str, size: str, quality: str | None) -> str:
        body = {"model": model, "prompt": prompt, "n": 1, "size": size}
        if quality:
            body["quality"] = quality
        for _ in range(6):
            try:
                res = self._req("/images/generations", body)
                break
            except urllib.error.HTTPError as e:
                msg = e.read().decode("utf-8", "replace")
                low = msg.lower()
                if "aspect ratio" in low:
                    m = re.search(r"supported ratios:\s*([^\"]+)", msg)
                    ratios = [x.strip() for x in m.group(1).split(",")] if m else []
                    body["size"] = "1:1" if ("1:1" in ratios or not ratios) else ratios[0]
                elif "not supported for asynchronous" in low or 'parameter "size"' in low:
                    body.pop("size", None)
                elif "quality" in low:
                    body.pop("quality", None)
                elif "response format" in low or "response_format" in low:
                    body.pop("response_format", None)
                else:
                    raise RuntimeError(msg[:300])
        else:
            raise RuntimeError("apimart: 参数降级失败")
        data = res.get("data")
        if isinstance(data, list) and data and data[0].get("task_id"):
            return data[0]["task_id"]
        raise RuntimeError("apimart: 未返回 task_id: " + json.dumps(res)[:200])

    def _poll(self, task_id: str, timeout: int = 420) -> tuple[str, float | None]:
        t0 = time.time()
        while time.time() - t0 < timeout:
            d = (self._req(f"/tasks/{task_id}", timeout=60) or {}).get("data") or {}
            if d.get("status") == "completed":
                images = (d.get("result") or {}).get("images") or []
                if not images:
                    raise RuntimeError("apimart: 任务完成但没有图片")
                u = images[0].get("url")
                url = u[0] if isinstance(u, list) else u
                return url, d.get("cost")
            if d.get("status") in ("failed", "error", "cancelled"):
                raise RuntimeError("apimart: 任务失败 " + json.dumps(d)[:300])
            time.sleep(1.5)
        raise TimeoutError(f"apimart: 任务 {task_id} 超时")

    def _generate(self, model: str, prompt: str, size: str, role: str,
                  quality: str | None = None) -> Image.Image:
        tid = self._submit(model, prompt, size, quality if quality is not None else self.quality)
        url, cost = self._poll(tid)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=180) as r:
            raw = r.read()
        self.last_cost[role] = float(cost or 0.0)
        self.cost_log.append({"role": role, "model": model, "cost": float(cost or 0.0)})
        return Image.open(io.BytesIO(raw)).convert("RGBA")

    # ---------- Provider 接口

    def sheet(self, spec, cells, index, size=1024, seed=0):
        p = prompts.sheet_prompt(spec, cells, index)
        return self._generate(self.sheet_model, p["prompt"] + "\n\nAvoid: " + p["negative_prompt"],
                              f"{size}x{size}", "sheet")

    def background(self, spec, variant, size=1024, seed=0):
        p = prompts.background_variant_prompt(spec, variant)
        return self._generate(self.bg_model, p["prompt"] + "\n\nAvoid: " + p["negative_prompt"],
                              f"{size}x{size}", "background")

    def element(self, spec, el, variant, size=1024, seed=0):
        p = prompts.element_prompt(spec, el, variant)
        return self._generate(self.sheet_model, p["prompt"] + "\n\nAvoid: " + p["negative_prompt"],
                              f"{size}x{size}", "element")
