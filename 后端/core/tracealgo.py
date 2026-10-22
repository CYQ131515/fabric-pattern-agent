"""描稿引擎 —— 调新算法 `steps/01-clean.mjs`（AI 清洗：照片 → 平整印花稿）。

和老实现的区别
    老实现走通用生图接口（core/imagegen.ImageGen），提示词里带「还原度三档」，
    产出的清晰度/风格取决于生图模型。新算法用 2.5 世代模型做**保花型清洗**：
    默认提示词就是「逐元素忠实复刻 + 去掉褶皱/光影/背景」，一次一张。
    所以这里不再有 fidelity 三档（2026-10-20 产品确认去掉）。

对 skill handler 的契约（保持不变）
    .enabled / .api_key / .cost / .variants / .describe()
    .generate(prompt, image=<b64>, image_mime=..., tag='trace')
        → {"files": [...], "cost": 点数, "provider": ..., "model": ...}
    prompt 在这里的含义是「用户的额外要求」（比如“改成蓝紫色”），
    引擎会把它作为 --note 追加到算法默认提示词后面，默认行为仍然是忠实复刻。
"""
from __future__ import annotations

import base64
import os
import shutil
import tempfile

from .algo import Algo, AlgoError
from .imagegen import ImageGen, ImageGenError, sniff

DEFAULT_COST = 60

# 第一步「想象平铺还原」的提示词（对应 R1 / lessons L30-L31）。
# 真实面料照片经常是挂拍/堆叠/带透视的，默认的「忠实复刻」提示词在重褶皱上
# 会让模型把褶皱也一起"忠实"下来（实测：产品里出的稿子褶皱、抽褶、阴影全在）。
# 所以描稿固定先跑一遍这个提示词把几何拉直，再跑默认清洗 + 高清。
UNWARP_PROMPT = """The photo shows a PRINTED FABRIC that is heavily draped, folded and shot at an angle, \
with big wrinkles and perspective distortion. Reconstruct the ORIGINAL FLAT PRINTED TEXTILE DESIGN, \
as if the cloth were laid perfectly flat and scanned from directly above.

Undo the draping, folds and perspective: straighten the pattern geometry so flower heads are round \
and evenly arranged, stems/leaves follow the original direction, and the motif spacing becomes regular. \
Keep every motif identical in shape, size, colour and style. Do NOT redesign, add, remove or rearrange \
motifs; do not invent new colours.

Fill the whole frame with the reconstructed flat pattern. Flat 2D textile print, no folds, no shadows, \
no highlights, no fabric sheen, no garment edges, no background objects. High resolution, sharp edges."""

# 描稿路线二（对标 uniseven 的单遍生成）：一次生成就把图案画清楚、画完整。
# 依据 2026-10-21 对照分析：
#   - 两遍级联（平铺还原→清洗）会把细节磨软（锐度比单遍低约 24%）；
#   - 第二遍提示词只写"填满画面"会鼓励模型在四边切断元素（贴边残缺 49 处，
#     单遍+构图约束后 25 处）；
#   - 他们的描稿偏"保留浮雕/暗部"（低频 std 17.4 vs 我们 12.3），观感更清晰。
# 所以路线二 = 单遍 2048，强调「元素完整 + 保留印花自身的浮雕/色彩层次」。
ROUTE2_PROMPT = """Reproduce this printed fabric as a high-resolution flat textile design - \
like a clean, sharp scan of the printed fabric itself.

Keep the same motifs, palette, brushwork and the embossed relief / colour depth of the print. \
Remove only the folds, wrinkles, perspective, background objects and camera shadows; do NOT flatten \
away the print's own relief, texture and shading.

COMPOSITION RULE (important): every flower, leaf and branch must be COMPLETE inside the frame - \
do not cut any motif at the image edges. Recompose the layout slightly if necessary so that all \
motifs are fully visible, while keeping the same style, motif scale and density.

Square 1:1 composition, flat 2D textile print, no border, no watermark, high resolution, crisp edges."""



class TraceEngine:
    def __init__(self, config):
        self.cfg = config
        # 继承生图段的默认值（outdir / cost / timeout 老口径一致），trace 段显式
        # 填了的项才覆盖 —— 和 core/imagegen.py 里 section 的处理方式一样。
        conf = dict(config.settings.get("image") or {})
        for k, v in (config.settings.get("trace") or {}).items():
            if v not in (None, "", [], {}):
                conf[k] = v
        self.conf = conf
        self.algo = Algo(config)
        # 模型默认用**算法自己的**清洗模型（APIMART 上的 2.5 世代），
        # 不继承生图段的模型 —— 那是火山方舟的模型 id，算法网关不认。
        # 后台 trace.model 显式填了才覆盖。
        trace_conf = config.settings.get("trace") or {}
        self.model = (str(trace_conf.get("model") or "")).strip() or self.algo.model_clean
        self.cost = int(trace_conf.get("cost") or conf.get("cost") or DEFAULT_COST)
        self.variants = 1                    # 新算法一次一张（2026-10-20 产品确认）
        self.provider = "algo-01-clean"
        self.timeout = int(conf.get("timeout") or 300)
        # 描稿路线：① 平铺还原（可关）→ ② 清洗+高清（最终交付）
        self.unwarp = bool(conf.get("unwarp", True))
        self.size_restore = str(conf.get("size_restore") or "1:1")
        self.size_final = str(conf.get("size_final") or "2048x2048")
        # 描稿路线：route1 = 两遍（平铺还原→清洗，默认，抗扭曲强）；
        #           route2 = 单遍（构图保完整 + 保留浮雕，更锐、更快、更便宜）。
        self.route = str(conf.get("route") or "route1").strip().lower()
        if self.route not in ("route1", "route2"):
            self.route = "route1"
        self._saver = None

    # ---------- 可用性（admin 面板直接读 .enabled / .api_key） ----------
    @property
    def enabled(self):
        return self.algo.installed and bool(self.algo.key)

    @property
    def api_key(self):
        return self.algo.key

    def describe(self):
        d = self.algo.describe()
        d.update({"enabled": self.enabled, "has_key": bool(self.api_key),
                  "cost": self.cost, "model": self.model,
                  "variants": self.variants, "provider": self.provider,
                  "unwarp": self.unwarp, "size_restore": self.size_restore,
                  "size_final": self.size_final,
                  "route": self.route,
                  "routes": ["route1", "route2"],
                  "passes": (2 if self.unwarp else 1) if self.route == "route1" else 1})
        return d

    @property
    def saver(self):
        """落盘用：沿用 imagegen 的保存逻辑（写到 数据/generated 并给出可访问地址）。"""
        if self._saver is None:
            self._saver = ImageGen(self.cfg, section="trace")
        return self._saver

    # ---------- 生成 ----------
    def generate(self, prompt, image=None, image_mime="image/png", n=1, tag="trace",
                 route=None):
        if not self.enabled:
            raise ImageGenError("描稿算法没配好：%s" % self.algo.missing_note)
        if not image:
            raise ImageGenError("描稿要有一张照片。")
        try:
            blob = base64.b64decode(image)
        except Exception:                                # noqa: BLE001
            raise ImageGenError("照片数据读不出来，换一张重新上传试试。")
        _ext, _mime = sniff(blob)
        ext = _ext or ".png"

        workdir = tempfile.mkdtemp(prefix="trace_")
        try:
            src = os.path.join(workdir, "input" + ext)
            with open(src, "wb") as f:
                f.write(blob)
            note = (prompt or "").strip()
            cost_usd = 0.0
            route = str(route or self.route or "route1").strip().lower()
            if route not in ("route1", "route2"):
                route = "route1"

            if route == "route2":
                # 单遍：构图约束 + 保留浮雕（对标 uniseven 的描稿路线）
                out = os.path.join(workdir, "clean.png")
                pf = os.path.join(workdir, "route2-prompt.txt")
                with open(pf, "w", encoding="utf-8") as f:
                    f.write(ROUTE2_PROMPT)
                args = ["--input", src, "--out", out, "--model", self.model,
                        "--size", self.size_final, "--prompt-file", pf]
                if note:
                    args += ["--note", note]
                code, stdout, stderr = self.algo.run_node(
                    "steps/01-clean.mjs", args, timeout=self.timeout)
                if code != 0 or not os.path.exists(out):
                    raise ImageGenError("描稿路线二失败了。%s"
                                        % self.algo.error_summary(stdout, stderr))
                with open(out, "rb") as f:
                    png = f.read()
                info = self.algo.meta(out)
                cost_usd += float(info.get("cost_usd") or 0)
                try:
                    files = list(self.saver.save([png], tag=tag).get("files") or [])
                except Exception as e:                       # noqa: BLE001
                    raise ImageGenError("描稿出图了，但产物没存下来（%s: %s）"
                                        % (type(e).__name__, e))
                if not files:
                    raise ImageGenError("描稿没产出可用的图。")
                return {"files": files, "count": len(files), "cost": self.cost,
                        "mock": False, "provider": self.provider,
                        "model": info.get("model") or self.model,
                        "size": self.size_final, "passes": 1, "route": route,
                        "cost_usd": round(cost_usd, 5)}

            # ① 想象平铺还原：把褶皱/透视/抽褶先拉直成平版（R1 / L31）。
            #    这一步不接用户附加要求：先保几何，再谈风格。
            cur = src
            if self.unwarp:
                flat = os.path.join(workdir, "flat.png")
                pf = os.path.join(workdir, "unwarp-prompt.txt")
                with open(pf, "w", encoding="utf-8") as f:
                    f.write(UNWARP_PROMPT)
                code, stdout, stderr = self.algo.run_node(
                    "steps/01-clean.mjs",
                    ["--input", cur, "--out", flat, "--model", self.model,
                     "--size", self.size_restore, "--prompt-file", pf],
                    timeout=self.timeout)
                if code != 0 or not os.path.exists(flat):
                    raise ImageGenError("描稿的「平铺还原」这一步失败了。%s"
                                        % self.algo.error_summary(stdout, stderr))
                cost_usd += float(self.algo.meta(flat).get("cost_usd") or 0)
                cur = flat

            # ② 清洗 + 高清（默认提示词 = 忠实复刻；用户额外要求走 --note）
            out = os.path.join(workdir, "clean.png")
            args = ["--input", cur, "--out", out, "--model", self.model,
                    "--size", self.size_final]
            if note:
                args += ["--note", note]
            code, stdout, stderr = self.algo.run_node(
                "steps/01-clean.mjs", args, timeout=self.timeout)
            if code != 0 or not os.path.exists(out):
                raise ImageGenError("描稿清洗失败了。%s"
                                    % self.algo.error_summary(stdout, stderr))
            with open(out, "rb") as f:
                png = f.read()
            info = self.algo.meta(out)
            cost_usd += float(info.get("cost_usd") or 0)
        except AlgoError as e:
            raise ImageGenError(str(e))
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        try:
            files = list(self.saver.save([png], tag=tag).get("files") or [])
        except Exception as e:                           # noqa: BLE001
            raise ImageGenError("描稿出图了，但产物没存下来（%s: %s）"
                                % (type(e).__name__, e))
        if not files:
            raise ImageGenError("描稿没产出可用的图。")
        return {"files": files, "count": len(files), "cost": self.cost,
                "mock": False, "provider": self.provider,
                "model": info.get("model") or self.model,
                "size": self.size_final,
                "passes": 2 if self.unwarp else 1,
                "route": route,
                "cost_usd": round(cost_usd, 5)}
