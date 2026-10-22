"""RunningHub 开放接口客户端（零第三方依赖，只用标准库）。

⚠️ 两个反直觉的实测结论（都踩过）：
1. `/task/openapi/status` 的 `data` 是**纯字符串**（"RUNNING"），不是对象
2. 目标层的输出节点是 **432 不是 440** —— SAM3_Detect 的原始蒙版是反的


五个接口跑一个 ComfyUI 工作流：

    POST /api/openapi/getJsonApiFormat   取工作流节点定义
    POST /task/openapi/upload            上传图片 → fileName（multipart）
    POST /task/openapi/create            建任务 → taskId
    POST /task/openapi/status            轮询 → QUEUED/RUNNING/SUCCESS/FAILED
    POST /task/openapi/outputs           取结果 → [{fileUrl, fileType, nodeId}]

坑：
1. 认证两处都给：body 的 apiKey ＋ 头 Authorization: Bearer
2. 结果 URL 约 24 小时过期，拿到立刻落盘
3. nodeId/fieldName 不能猜，用 getJsonApiFormat 查

⚠️ 网络出口统一走 net.urlopen —— **强制直连、不走代理**。
   别改回裸 urllib.request.urlopen：那会自己读 HTTP_PROXY/系统代理，
   这台机器上的本地代理会让 RunningHub 直接 Connection refused，
   而错误信息看着像"key 不对"，排查方向全错。详见 net.py 顶部。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid

from . import net

DEFAULT_BASE = "https://www.runninghub.cn"

# 本工作流的节点（来自「自用抠图_api.json」）
WORKFLOW_ID = "2109041802081820673"
IMAGE_NODE = "431"        # LoadImage
IMAGE_FIELD = "image"
TEXT_NODE = "434"         # CR Text
TEXT_FIELD = "text"
# ⚠️ 实测（不是照抄节点名）：SAM3_Detect 吐出来的原始蒙版是**反的**，
# 经过 433「Mask Invert」之后才变成"目标=不透明"。
# 所以流程图看着像反的：432 才是目标层，440 是"抠掉目标后剩下的背景"。
# 别按节点命名猜，拿 tools/probe_repeat.py 出来的覆盖率验证过。
OUT_TARGET = "432"        # SaveImage ← 目标层（透明底）
OUT_BACKGROUND = "440"    # SaveImage ← 抠掉目标后剩下的（目前用不到）


class RunningHubError(RuntimeError):
    pass


def _multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    boundary = "----rhpsd" + uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
        out += f"{v}\r\n".encode()
    for k, (name, data) in files.items():
        out += f"--{boundary}\r\n".encode()
        out += (f'Content-Disposition: form-data; name="{k}"; '
                f'filename="{name}"\r\n').encode()
        out += b"Content-Type: application/octet-stream\r\n\r\n"
        out += data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


class RunningHub:
    def __init__(self, api_key: str | None = None, base: str = DEFAULT_BASE,
                 timeout: float = 60.0, retries: int = 2):
        self.api_key = api_key or os.environ.get("RUNNINGHUB_API_KEY", "")
        if not self.api_key:
            raise RunningHubError(
                "缺 RunningHub API Key：传 api_key 或设 RUNNINGHUB_API_KEY")
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.log: list[str] = []
        self.n_tasks = 0

    def _post(self, path: str, payload: dict | None = None,
              body: bytes | None = None, ctype: str | None = None,
              timeout: float | None = None) -> dict:
        if body is None:
            body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
            ctype = "application/json"
        req = urllib.request.Request(self.base + path, data=body, method="POST")
        req.add_header("Content-Type", ctype)
        req.add_header("Authorization", f"Bearer {self.api_key}")
        req.add_header("Accept", "application/json")

        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                with net.urlopen(req, timeout=timeout or self.timeout) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    raise RunningHubError(f"{path} 返回非 JSON：{raw[:200]}")
                code = obj.get("code", 0)
                if code not in (0, "0"):
                    raise RunningHubError(
                        f"{path} code={code} msg={obj.get('msg')}")
                return obj
            except (urllib.error.HTTPError, urllib.error.URLError,
                    TimeoutError, OSError) as exc:
                last = exc
                if isinstance(exc, urllib.error.HTTPError) and exc.code < 500:
                    raise RunningHubError(
                        f"{path} HTTP {exc.code}: "
                        f"{exc.read().decode('utf-8', 'replace')[:300]}")
                if attempt < self.retries:
                    time.sleep(2 ** attempt)
        raise RunningHubError(f"{path} 网络失败：{last}")

    def workflow_json(self, workflow_id: str) -> dict:
        obj = self._post("/api/openapi/getJsonApiFormat",
                         {"apiKey": self.api_key, "workflowId": workflow_id})
        data = obj.get("data") or {}
        p = data.get("prompt")
        if isinstance(p, str):
            return json.loads(p)
        if isinstance(p, dict):
            return p
        raise RunningHubError(f"没拿到 prompt：{str(data)[:200]}")

    def describe_nodes(self, workflow_id: str) -> list[dict]:
        out = []
        for nid, node in self.workflow_json(workflow_id).items():
            if not isinstance(node, dict):
                continue
            inputs = node.get("inputs") or {}
            out.append({
                "nodeId": str(nid),
                "class_type": node.get("class_type", "?"),
                "title": (node.get("_meta") or {}).get("title", ""),
                # 值是 list 的表示连线，不能通过 API 改
                "fields": {k: v for k, v in inputs.items()
                           if not isinstance(v, list)},
            })
        return out

    def upload(self, file_path: str) -> str:
        with open(file_path, "rb") as fh:
            raw = fh.read()
        body, ctype = _multipart({"apiKey": self.api_key},
                                 {"file": (os.path.basename(file_path), raw)})
        obj = self._post("/task/openapi/upload", body=body, ctype=ctype,
                         timeout=max(self.timeout, 180))
        data = obj.get("data") or {}
        fn = data.get("fileName")
        if not fn:
            raise RunningHubError(f"upload 没返回 fileName：{str(data)[:200]}")
        self.log.append(f"上传 {os.path.basename(file_path)} → {fn}")
        return fn

    def create(self, workflow_id: str, node_info_list: list[dict],
               instance_type: str | None = None) -> str:
        payload = {"apiKey": self.api_key, "workflowId": str(workflow_id),
                   "nodeInfoList": node_info_list}
        if instance_type:
            payload["instanceType"] = instance_type
        obj = self._post("/task/openapi/create", payload)
        data = obj.get("data") or {}
        tid = data.get("taskId")
        if not tid:
            raise RunningHubError(f"create 没返回 taskId：{str(data)[:300]}")
        tips = (data.get("promptTips") or "").replace(" ", "")
        if tips and '"result":false' in tips:
            raise RunningHubError(f"工作流校验不通过：{data.get('promptTips')[:300]}")
        return str(tid)

    def status(self, task_id: str) -> tuple[str, str]:
        """注意：这个接口的 data 是**纯字符串**（"QUEUED"/"RUNNING"/"SUCCESS"/"FAILED"），
        不是包了 status 字段的对象 —— 按对象解会直接 AttributeError。"""
        obj = self._post("/task/openapi/status",
                         {"apiKey": self.api_key, "taskId": str(task_id)})
        data = obj.get("data")
        if isinstance(data, str):
            return data, str(obj.get("msg") or "")
        data = data or {}
        return str(data.get("status", "")), str(data.get("msg", ""))

    def outputs(self, task_id: str) -> list[dict]:
        obj = self._post("/task/openapi/outputs",
                         {"apiKey": self.api_key, "taskId": str(task_id)})
        data = obj.get("data")
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            # 任务刚 SUCCESS 的一瞬间还会返回 804 + 一个含 wss 的对象
            return []
        return []

    def run(self, workflow_id: str, node_info_list: list[dict],
            instance_type: str | None = None, poll: float = 5.0,
            timeout: float = 180.0) -> tuple[str, list[dict]]:
        """建任务 + 轮询到出结果。

        timeout 是**这一个任务**的等待上限，不是整批的上限。正常一个任务约
        30 秒；给太大（曾经是 900）会让"云端堵了"变成"用户干等 15 分钟"，
        而且外层进程上限一到就被直接 kill，连换提示词重试的机会都没有。
        """
        tid = self.create(workflow_id, node_info_list, instance_type)
        t0 = time.time()
        while True:
            st, msg = self.status(tid)
            if st in ("SUCCESS", "FAILED"):
                break
            if time.time() - t0 > timeout:
                raise RunningHubError(f"任务 {tid} 超时，最后状态 {st}")
            time.sleep(poll)
        if st == "FAILED":
            raise RunningHubError(f"任务 {tid} 失败：{msg}")
        self.n_tasks += 1
        outs: list[dict] = []
        for _ in range(6):                  # 刚 SUCCESS 时 outputs 可能还是 804
            outs = self.outputs(tid)
            if outs:
                break
            time.sleep(poll)
        self.log.append(
            f"任务 {tid} 完成 {time.time() - t0:.0f}s，输出 {len(outs)} 个")
        return tid, outs

    def download(self, url: str, dest: str) -> str:
        os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
        # 走直连出口（禁用代理，且 IPv4 优先）—— 结果图 CDN 对这两点都敏感
        with net.urlopen(url, timeout=max(self.timeout, 180)) as r:
            raw = r.read()
        with open(dest, "wb") as fh:
            fh.write(raw)
        return dest
