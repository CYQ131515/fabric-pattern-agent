"""看图规划分层：决定分几层、每层给 SAM3.1 什么提示词。

提示词规范（工作流要求）：
  · 多个对象必须用**英文逗号**分隔，中文逗号无效
  · 用**英文冒号 + 数字**指定数量，如 `花朵:12`
  · 可加描述性词做定位，如 `中间的瓶子:2`
  · 单次标记总数不得超过 32

关于视觉模型（VLM）：
原来只绑 api.apimart.ai，实测该域名已不可达（代理 CONNECT 502、直连 443 关闭），
所以这里改成"可插拔"：任意 OpenAI 兼容的 /chat/completions 网关都能用，
通过 --vlm-provider / --vlm-base-url / --vlm-model 或环境变量指定。
跑之前会先做一次 preflight 探活，网关不通就直接报清楚，
不要等到花了 RunningHub 的任务费才发现诊断根本跑不了。

⚠️ 网络出口统一走 net.urlopen —— **强制直连、不走代理**（见 net.py 顶部）。
   别改回裸 urllib.request.urlopen：那会自己读 HTTP_PROXY/系统代理，
   排错时看着像"网关挂了/key 不对"，实际是代理把请求拦了。
"""
from __future__ import annotations

import base64
import io
import json
import os
import threading
import time
import urllib.error
import urllib.request

import numpy as np
from PIL import Image

from . import net

# 预设网关：base_url + 该家推荐的支持图片输入的模型
PROVIDERS: dict[str, dict[str, str]] = {
    # 官方首选：DeepSeek-V4.1-Flash 官网 id 就叫 deepseek-flash，
    # input_modalities 含 image，是真能看图的（V4-Pro 反而只有 text）
    "deepseek": {
        "base": "https://api.deepseek.com/v1",
        "model": "deepseek-flash",
        "env": "DEEPSEEK_API_KEY",
        "note": "DeepSeek 官方，V4.1-Flash 支持图片输入",
    },
    "apimart": {
        "base": "https://api.apimart.ai/v1",
        "model": "deepseek-v4.1-flash",
        "env": "APIMART_API_KEY",
        "note": "实测已不可达，保留仅为兼容旧配置",
    },
    "dashscope": {
        "base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-vl-max-latest",
        "env": "DASHSCOPE_API_KEY",
        "note": "阿里通义，qwen-vl-max / qwen2.5-vl-72b-instruct 均可",
    },
    "bigmodel": {
        "base": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4v-plus",
        "env": "ZHIPU_API_KEY",
        "note": "智谱，glm-4v-plus / glm-4.5v",
    },
    "zai": {
        "base": "https://api.z.ai/api/paas/v4",
        "model": "glm-4.5v",
        "env": "ZAI_API_KEY",
        "note": "智谱国际版",
    },
    "siliconflow": {
        "base": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen2.5-VL-72B-Instruct",
        "env": "SILICONFLOW_API_KEY",
        "note": "硅基流动",
    },
    "moonshot": {
        "base": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k-vision-preview",
        "env": "MOONSHOT_API_KEY",
        "note": "Kimi",
    },
    "volc": {
        "base": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "doubao-vision-pro-32k",
        "env": "ARK_API_KEY",
        "note": "火山方舟，model 要填接入点 id（ep-xxx）",
    },
}

# 默认走 DeepSeek 官网：deepseek-flash（V4.1-Flash）支持图片输入，实测 2 秒出方案
DEFAULT_BASE = PROVIDERS["deepseek"]["base"]
MODEL = PROVIDERS["deepseek"]["model"]

# 没显式指定 provider 时，按顺序找 key
_FALLBACK_KEY_ENVS = ("VLM_API_KEY", "DEEPSEEK_API_KEY", "APIMART_API_KEY")

DEFAULT_PLAN = {
    "layers": [
        {"name": "花型", "prompt": "花朵:24"},
    ]
}

SYSTEM = """你是纺织面料花型的分层规划器。看一张平铺花型图，把它拆成若干语义图层。

只输出 JSON，不要解释、不要 markdown 代码块：
{"layers":[{"name":"主体花","prompt":"花朵:12"},{"name":"叶片","prompt":"叶子:20"}]}

规则：
1. name：中文，不超过 6 个字，会直接作为 PSD 图层名。
2. prompt：喂给 SAM3.1 的检测提示词，必须遵守：
   - 多个对象之间用**英文逗号**分隔（中文逗号无效）
   - 用**英文冒号加数字**指定数量，例如 花朵:12
   - **一层里所有对象的数量"之和"不得超过 32**。这是硬上限：实测
     `深蓝色叶子:20,细枝:12,黄色小穗:8`（和为 40）直接退化成整幅全选。
     **建议总和控制在 8 到 24**，不要顶格。
   - **每层最多写 1 到 2 个对象词**。堆 3 个以上几乎必退化，宁可少写。
   - 需要定位时加描述性词，例如 中间的瓶子:2
3. layers 按**从底到顶**排列：面积大、铺在底下的排前面；细小的点缀排后面。
4. 不要单独列"底布/背景"，底布由算法处理。
5. 整幅平铺图案里同类元素通常很多，数量要给足（满幅小花给 20~30），
   宁可多标也不要只标一两朵 —— 标少了图层会缺一大片。
6. 层数控制在 2 到 __MAX__ 之间。语义上区分不开就不要硬分。
7. **不要把"连续蔓延的枝叶底纹"单独列成层**。SAM3 对连成一片的纹理只会
   整幅全选（实测 深蓝色叶子:24 → 100% 全选），列了也白列还多花钱。
   只列"一朵一朵、一簇一簇数得清"的元素。
8. 顺序仍按从底到顶写。连续底纹如果实在要写就放最后，流水线会自动把它
   挪到最后再跑。"""

# 注意：SYSTEM 里有 JSON 示例（花括号），不能用 str.format()，
# 否则 {"layers" 会被当成占位符，直接 KeyError: '"layers"'
SYSTEM_TEMPLATE = SYSTEM


# ---------------------------------------------------------------- 配置解析

def resolve(provider: str = "", base_url: str = "", model: str = "",
            api_key: str = "") -> tuple[str, str, str]:
    """把 provider / base_url / model / key（含环境变量）解析成 (base, model, key)。

    优先级：显式参数 > VLM_* 环境变量 > provider 预设 > provider 专属 key 环境变量。
    """
    provider = (provider or os.environ.get("VLM_PROVIDER", "")).strip().lower()
    preset = PROVIDERS.get(provider, {})
    base = (base_url or os.environ.get("VLM_BASE_URL", "")
            or preset.get("base", "") or DEFAULT_BASE).rstrip("/")
    model = (model or os.environ.get("VLM_MODEL", "")
             or preset.get("model", "") or MODEL)
    key = api_key
    if not key and preset.get("env"):
        key = os.environ.get(preset["env"], "")
    if not key:
        for name in _FALLBACK_KEY_ENVS:
            key = os.environ.get(name, "")
            if key:
                break
    return base, model, key


def preflight(base_url: str, api_key: str, timeout: int = 12) -> tuple[bool, str]:
    """探活：网关能不能连上、key 收不收。返回 (ok, 说明)。

    只看 HTTP 状态码：401/403 说明**能连上**只是 key 不对或没权限；
    000 / 502 说明根本连不通，换网关。
    """
    url = base_url.rstrip("/") + "/models"
    req = urllib.request.Request(
        url, method="GET", headers={"Authorization": f"Bearer {api_key}"})
    try:
        with net.urlopen(req, timeout=timeout) as resp:
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        # 401/403 = 通了，key 的问题；404 = 通了，只是这网关没 /models
        return True, f"HTTP {exc.code}"
    except Exception as exc:                                # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


_REACH_CACHE: dict[int, list[str]] = {}
# 同一次扫描只跑一遍：多个线程同时踩进来，后面的等第一个的结果。
# 用普通 dict + Lock 而不是别的，是因为要能在**没有第三方库**的前提下
# 也保证「一次扫描只有一个在跑」。
_REACH_LOCK = threading.Lock()


def _probe_provider(name: str, cfg: dict, timeout: int) -> tuple[str, bool]:
    try:
        good, _ = preflight(cfg["base"], "", timeout=timeout)
    except Exception:                                    # noqa: BLE001
        good = False
    return name, good


def reachable(timeout: int = 6) -> list[str]:
    """扫一遍预设网关，返回**当前网络能连上**的 provider 名（结果缓存）。

    ⚠️ 必须并发扫（原来是一个一个串着探）。
    串行时总耗时是 8 家 × timeout 的**累加**：网络稍微不配合（某几家被墙、
    不返回 RST 只是干挂着），用户就要面对 30~60 秒的纯静默 —— 界面既没有
    进度也没有错误，只能干等，看起来就是"卡死了"。
    实测这台机器串行 17.1 秒；在更差的网络下能到 60 秒以上（真的出现过）。

    改成并发之后，总耗时≈**单个** timeout（谁慢谁自己超时，不拖累别人）。
    顺序仍按 PROVIDERS 的声明顺序返回 —— 调用方拿它做的是"报给用户看
    有哪些备选"，稳定顺序比"谁先回来谁在前"更好读。
    """
    if timeout in _REACH_CACHE:
        return _REACH_CACHE[timeout]
    with _REACH_LOCK:
        # 等锁的时候可能已经被别人填好了，再查一次
        if timeout in _REACH_CACHE:
            return _REACH_CACHE[timeout]
        items = list(PROVIDERS.items())
        got: dict[str, bool] = {}
        threads = []
        for name, cfg in items:
            t = threading.Thread(target=lambda n=name, c=cfg: got.__setitem__(
                n, _probe_provider(n, c, timeout)[1]), daemon=True)
            t.start()
            threads.append(t)
        # join 带上限：哪怕某个线程因为奇怪的原因卡住，也不能把调用方拖死。
        # 已经探测完的照常收下，没收到的按"不通"处理 —— 宁可少报一个备选，
        # 也不能让用户继续等。
        deadline = time.time() + timeout + 3
        for t in threads:
            t.join(max(0.0, deadline - time.time()))
        ok = [name for name, _cfg in items if got.get(name)]
        _REACH_CACHE[timeout] = ok
        return ok


# ---------------------------------------------------------------- 调用

def _data_url(img: np.ndarray) -> str:
    im = Image.fromarray(img)
    if im.mode != "RGB":
        im = im.convert("RGB")
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def analyze(img: np.ndarray, api_key: str, max_layers: int = 4,
            model: str = MODEL, base_url: str = DEFAULT_BASE,
            timeout: int = 120, retries: int = 3) -> dict:
    """返回 {"layers": [{"name","prompt"}, ...]}。失败时抛异常，由上层决定退路。

    retries 只在"网关连得上但这一次没成功"时才有意义；如果 preflight 就失败，
    直接抛，不浪费时间空转。
    """
    ok, detail = preflight(base_url, api_key)
    if not ok:
        avail = reachable()
        hint = "、".join(avail) if avail else "（一个都连不上，检查代理）"
        raise RuntimeError(
            f"视觉网关连不上：{base_url}（{detail}）。"
            f"当前可用的有：{hint}。"
            f"换一个：--vlm-provider 名字 --vlm-key key，"
            f"或 --vlm-base-url 任意 OpenAI 兼容地址")

    last: Exception | None = None
    for attempt in range(retries):
        try:
            return _analyze_once(img, api_key, max_layers, model, base_url, timeout)
        except Exception as exc:                            # noqa: BLE001
            last = exc
            if attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"诊断重试 {retries} 次仍失败：{last}")


def _analyze_once(img: np.ndarray, api_key: str, max_layers: int = 4,
                  model: str = MODEL, base_url: str = DEFAULT_BASE,
                  timeout: int = 120) -> dict:
    body = {
        "model": model,
        "messages": [
            {"role": "system",
             "content": SYSTEM.replace("__MAX__", str(max_layers))},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": _data_url(img)}},
                {"type": "text",
                 "text": f"把这张图拆成 2~{max_layers} 个图层，按规范输出 JSON。"},
            ]},
        ],
        # deepseek 是推理模型，reasoning_content 也吃 max_tokens，给少了 content 会空
        "max_tokens": 8000,
        "temperature": 0.3,
        "stream": False,
    }
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"})
    with net.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("对话接口没返回 choices：" + str(data)[:300])
    text = (choices[0].get("message") or {}).get("content") or ""
    if not text.strip():
        raise RuntimeError("模型返回空 content（可能是 max_tokens 被 reasoning 吃光）")
    return _parse(text, max_layers)


def _parse(text: str, max_layers: int) -> dict:
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```")[1]
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        s = s[i:j + 1]
    try:
        obj = json.loads(s)
    except Exception:                                       # noqa: BLE001
        return {**DEFAULT_PLAN, "_raw": text[:300]}
    layers = obj.get("layers") or []
    clean = []
    for it in layers[:max_layers]:
        name = str(it.get("name", "")).strip()
        prompt = str(it.get("prompt", "")).strip()
        if name and prompt:
            # 中文逗号是无效的，顺手替掉，避免模型手滑
            prompt = prompt.replace("，", ",").replace("：", ":")
            clean.append({"name": name, "prompt": prompt})
    if not clean:
        return {**DEFAULT_PLAN, "_raw": text[:300]}
    return {"layers": clean}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="探活视觉网关")
    ap.add_argument("--key", default="")
    ap.add_argument("--timeout", type=int, default=8)
    a = ap.parse_args()
    print("当前可达的预设网关：")
    for name, cfg in PROVIDERS.items():
        ok, detail = preflight(cfg["base"], a.key, timeout=a.timeout)
        flag = "可用" if ok else "不可达"
        print(f"  {name:<12} {flag:<7} {detail:<28} {cfg['base']}  ({cfg['note']})")
