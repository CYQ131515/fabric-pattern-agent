"""分层 PSD 引擎 —— 调新算法 `steps/04-layers-color.py`。

和老实现的区别
    老实现把图发给 RunningHub 跑 SAM3.1 工作流，按**语义**拆层（大花/枝叶……），
    还要一次视觉模型调用决定分几层。新算法走的是**色层**：k-means 调色板 →
    每个主色一层（`Color01 #a6b5d3` 这种），结构固定为
        Background（根层）+ Colors（分组）
    每层保留原始像素的蒙版，合成起来和源图逐像素相等；色层数 = k-1。
    好处是不再依赖 RunningHub、不再有一次「看图定层」的模型调用，秒级出图。

对外契约（和旧引擎一致，skill handler 不用改）
    plan  提案：不跑算法，直接报「会分成几层色层」
    run   完整执行；产物在 <临时目录>/out.psd，调用方读完负责删临时目录
"""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time

from .algo import Algo, AlgoError
from .imagegen import sniff


class PsdLayerError(Exception):
    pass


DEFAULT_CONF = {
    "enabled": True,
    "cost": 45,             # 点数额度沿用老口径，后台可改
    "max_layers": 7,        # 色层数（不含根层 Background），对齐算法 k=8
    "mode": "color",        # color=按颜色分色层（印刷分色）；elements=前景/背景元素分层
    "timeout_run": 900,
}


class PsdLayerEngine:
    def __init__(self, config):
        conf = dict(DEFAULT_CONF)
        conf.update(config.settings.get("psdlayer") or {})
        self.conf = conf
        self.enabled = bool(conf.get("enabled"))
        self.cost = int(conf.get("cost") or 0)
        self.max_layers = max(1, min(int(conf.get("max_layers") or 7), 8))
        self.mode = str(conf.get("mode") or "color").strip().lower()
        if self.mode not in ("color", "elements"):
            self.mode = "color"
        # elements 模式的前景来源：flood=本地区域生长；sam3=RunningHub SAM3.1 语义蒙版
        self.matte = str(conf.get("matte") or "flood").strip().lower()
        if self.matte not in ("flood", "sam3"):
            self.matte = "flood"
        rh = dict(config.settings.get("runninghub") or {})
        self.rh_workflow = str(rh.get("workflow_id") or "").strip()
        self.rh_key_env = str(rh.get("api_key_env") or "RUNNINGHUB_API_KEY").strip()
        self.rh_prompts = str(rh.get("prompts") or "花朵:64,叶子:32,枝条:32,金色叶片:64")
        self.rh_threshold = float(rh.get("threshold") or 0.5)
        self.rh_timeout = int(rh.get("timeout") or 900)
        self._cfg = config
        self.timeout_run = int(conf.get("timeout_run") or 900)
        self.algo = Algo(config)
        self.height = self.algo.height
        self._current = None
        self._cached_stop = None

    # ---------- 可用性 ----------
    @property
    def installed(self):
        script = "05-layers-elements.py" if self.mode == "elements" else "04-layers-color.py"
        if self.mode == "elements" and self.matte == "sam3":
            if not (os.path.exists(self.algo.step("06-matte-sam3-rh.py")) and self.rh_key()):
                return False
        return self.algo.installed and os.path.exists(self.algo.step(script))

    def rh_key(self):
        key = (os.environ.get(self.rh_key_env) or '').strip()
        if key:
            return key
        try:
            return str((self._cfg.secrets or {}).get('runninghub_api_key') or '').strip()
        except Exception:                            # noqa: BLE001
            return ""

    @property
    def has_keys(self):
        # 色层 PSD 全程本地（uv + psd-tools），不需要任何外部 key
        return True

    @property
    def available(self):
        return self.enabled and self.installed

    @property
    def missing_note(self):
        if not self.enabled:
            return "分层 PSD 功能没启用（settings.json → psdlayer.enabled）。"
        return self.algo.missing_note

    def describe(self):
        d = self.algo.describe()
        d.update({"enabled": self.enabled, "available": self.available,
                  "cost": self.cost, "max_layers": self.max_layers,
                  "mode": self.mode,
                  "matte": self.matte,
                  "k": (0 if self.mode == "elements" else self.max_layers + 1),
                  "height": self.height,
                  "structure": "Background(root) + Colors(group)",
                  "layers_naming": "Color01 #rrggbb",
                  "has_runninghub_key": bool(self.rh_key()), "has_vlm_key": False,
                  "direct_net": True})
        return d

    # ---------- 进度 ----------
    def progress(self):
        cur = self._current
        if not cur:
            return {"ok": True, "running": False}
        return {"ok": True, "running": True, "cmd": "run",
                "label": cur.get("label") or "正在按主色分色层",
                "elapsed": round(time.time() - cur["t0"], 1)}

    def cancel(self):
        """终止：真跑动杀算法子进程；缓存伪装跑动只置停止事件。"""
        stop = self._cached_stop
        if stop is not None:
            stop.set()
        cur = self._current
        if not cur:
            return stop is not None
        cur["cancelled"] = True
        killed = self.algo.cancel()
        print("[psdlayer] 已终止当前分色层步骤（用户点了终止）")
        return killed or stop is not None

    # ---------- 缓存命中时的伪装跑动 ----------
    def begin_cached_run(self, total_layers, duration):
        """登记一次「假跑动」：缓存命中时界面上也要看起来在跑（产品要求）。

        新算法的真跑动是单步的（没有「第几层」），所以伪装也只报
        「正在按主色分色层 + 已等几秒」，和真跑动的观感保持一致。
        """
        self._cached_stop = threading.Event()
        self._current = {"label": "正在按主色分色层", "t0": time.time(),
                         "cached": True, "total": max(1, int(total_layers or 1))}

    def end_cached_run(self):
        stop = self._cached_stop
        self._cached_stop = None
        if stop is not None:
            stop.set()
        cur = self._current
        if cur and cur.get("cached"):
            self._current = None

    def wait_or_stop(self, secs):
        """缓存伪装期间的可中断等待：用户点终止就立刻返回 True。"""
        stop = self._cached_stop
        if stop is None:
            time.sleep(secs)
            return False
        return bool(stop.wait(secs))

    # ---------- 提案 ----------
    def plan(self, blob, max_layers=None):
        """提案：只报「会分成几层色层」，不跑算法、不调模型、不花钱。

        新算法的分层方案是确定性的（按主色数量），没有需要视觉模型判断的部分，
        所以这里不需要看图 —— 层数由 max_layers 决定。
        """
        if self.mode == "elements":
            return {"ok": True,
                    "plan": {"layers": [{"name": "Foreground"}, {"name": "Background"}]},
                    "note": "",
                    "k": 0,
                    "summary": "整组图案元素抠成一张软蒙版（前景）+ 重绘干净底布，另附隐藏原图/反相蒙版"}
        ml = self._layers(max_layers)
        names = ["Color%02d" % i for i in range(1, ml + 1)]
        return {"ok": True,
                "plan": {"layers": [{"name": n} for n in names]},
                "note": "",
                "k": ml + 1,
                "summary": "按主色自动分 %d 层色层（根层 Background + Colors 分组）" % ml}

    # ---------- 执行 ----------
    def run(self, blob, layers=None, max_layers=None):
        """把一张花型图做成色层 PSD。返回 (结果 dict, 临时目录)。"""
        if not self.available:
            raise PsdLayerError(self.missing_note)
        ml = self._layers(max_layers)
        k = ml + 1
        workdir = tempfile.mkdtemp(prefix="psdlayer_")
        ext = self._ext_of(blob)
        src = os.path.join(workdir, "input" + ext)
        with open(src, "wb") as f:
            f.write(blob)
        out = os.path.join(workdir, "out.psd")

        mask_path = None
        if self.mode == "elements":
            label = "正在分离图案元素与底布"
            script = "steps/05-layers-elements.py"
            args = [src, out, "--height", str(self.height), "--ppi", "300"]
            if self.matte == "sam3":
                label = "正在用 SAM3.1 语义分割（RunningHub）"
                mask_path = os.path.join(workdir, "matte.png")
                key = self.rh_key()
                if key:
                    os.environ[self.rh_key_env] = key
                code6, out6, err6 = self.algo.run_py(
                    "steps/06-matte-sam3-rh.py",
                    ["--input", src, "--out", mask_path,
                     "--prompts", self.rh_prompts,
                     "--threshold", str(self.rh_threshold),
                     "--workflow-id", self.rh_workflow,
                     "--api-key-env", self.rh_key_env,
                     "--raw-dir", os.path.join(workdir, "rh_raw"),
                     "--timeout", str(self.rh_timeout)],
                    timeout=self.rh_timeout + 300)
                ok6 = (code6 == 0 and os.path.exists(mask_path))
                if ok6:
                    args += ["--fg-mask", mask_path]
                else:
                    # 云端分割失败就退回区域生长，不能把 PSD 卡死
                    print("[psdlayer] SAM3 蒙版失败，退回区域生长：%s"
                          % self.algo.error_summary(out6, err6))
                    mask_path = None
        else:
            label = "正在按主色分色层"
            script = "steps/04-layers-color.py"
            args = [src, out, "--height", str(self.height), "--k", str(k),
                    "--group-name", "Colors"]
        self._current = {"label": label, "t0": time.time(), "cancelled": False}
        try:
            code, stdout, stderr = self.algo.run_py(script, args, timeout=self.timeout_run)
        except AlgoError as e:
            shutil.rmtree(workdir, ignore_errors=True)
            raise PsdLayerError(str(e))
        finally:
            cur, self._current = self._current, None
        cancelled = bool(cur and cur.get("cancelled"))
        if cancelled:
            shutil.rmtree(workdir, ignore_errors=True)
            raise PsdLayerError("这次已经停掉了。")

        detail = self.algo.last_json(stdout) or {}
        if code != 0 or not detail.get("out"):
            shutil.rmtree(workdir, ignore_errors=True)
            raise PsdLayerError("色层 PSD 没生成出来（退出码 %s）。%s"
                                % (code, self.algo.error_summary(stdout, stderr)))
        # 算法给的 coverage 是百分比（0~100），这里换算成 0~1：
        # skill handler 用 0~1 判断「这层是不是只逮到一角」，也按 0~1 显示。
        if self.mode == "elements":
            fg = detail.get("foreground_coverage")
            rows = [{"name": "Foreground", "hex": None,
                     "coverage": (round(float(fg), 4) if isinstance(fg, (int, float)) else None)},
                    {"name": "Background", "hex": None, "coverage": None}]
            result = {
                "ok": True,
                "outputs": {"psd": out},
                "layers": rows,
                "coverage_text": "前景元素覆盖约 %.0f%%" % ((fg or 0) * 100),
                "size": detail.get("size"),
                "cm": detail.get("cm"),
                "ppi": 300,
                "checks": {},
                "psd_check": {},
                "n_tasks": None,
                "seconds": detail.get("seconds"),
                "summary": "元素分层 PSD · 前景（原图+软蒙版）+ 背景底布（另有隐藏原图/反相蒙版）",
            }
            return result, workdir
        rows = []
        for it in detail.get("layers") or []:
            cov = it.get("coverage")
            rows.append({"name": it.get("name"),
                         "hex": it.get("hex"),
                         "coverage": (round(float(cov) / 100.0, 4)
                                      if isinstance(cov, (int, float)) else None)})
        cover = "、".join("%s %.0f%%" % (r["name"], (r.get("coverage") or 0) * 100)
                          for r in rows)
        result = {
            "ok": True,
            "outputs": {"psd": out},
            "layers": rows,
            "coverage_text": cover,
            "size": detail.get("size"),
            "cm": detail.get("cm"),
            "ppi": 300,
            "checks": {},
            "psd_check": {},
            "n_tasks": None,
            "seconds": detail.get("seconds"),
            "summary": "色层 PSD · %d 层（根层 Background + Colors 分组）" % len(rows),
        }
        return result, workdir

    # ---------- 小工具 ----------
    def _layers(self, max_layers):
        try:
            n = int(max_layers or self.max_layers)
        except (TypeError, ValueError):
            n = self.max_layers
        return max(1, min(n, 8))

    @staticmethod
    def _ext_of(blob):
        ext, _mime = sniff(blob)
        return ext or ".png"
