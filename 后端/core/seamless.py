"""四方连续引擎 —— 调新算法 `算法/描稿-四方连续-psd分层` 的接缝步骤。

和老实现的区别
    老实现把「体检 / 纯算法 / 模型补缝」交给 `算法/四方连续` 的 Python 引擎，
    路线由用户在卡片上手动选（纯算法 or 模型补缝）。
    新算法把选择权收进算法自己：先做**零成本体检**（特征 + 经验规则/planner
    给出候选顺序），然后按顺序跑候选，**每一步都过整图接缝审查**，不过就删产物
    换下一条路线，通过才交付。所以这里不再有「路线开关」，只有自动策略。

为什么不直接跑 pipeline.mjs
    `pipeline.mjs` 是研发用的一键流程：它会顺带生成 9382px 的色层 PSD（几百 MB）
    并归档进算法目录的 deliverables/ —— 那是研发交付目录，产品每次调用往里堆
    不合适。所以产品侧只复用它的**体检**（--dry-run，零成本），接缝候选按
    pipeline 里同样的规则自己编排，只出「无缝单元图」。

对外契约（和旧引擎一致，skill handler 不用改）
    probe   输入体检，零成本、秒级，不调模型
    convert 完整执行；产物在 <临时目录>/02-seamless.png，调用方读完负责删临时目录
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time

from .algo import Algo, AlgoError, DEFAULT_CONF as ALGO_DEFAULT
from .imagegen import sniff

# 候选路线的一句话说明（给日志、给交付话术用）
KIND_LABEL = {
    "fast": "纯算法直出",
    "ai2": "AI 补缝（带后处理）",
    "det": "清洗 + 算法对齐",
    "ai": "AI 补缝",
    "tile-multi": "多次采样选优",
    "regenerate": "原生重生成",
}

DEFAULT_CONF = {
    "enabled": True,
    "cost": 30,                 # 点数额度沿用老口径，后台可改
    "timeout_probe": 180,
    "timeout_run": 900,
    # 产品侧最多尝试几条候选（det → ai → regenerate 足够覆盖绝大多数图）
    "max_attempts": 3,
    # tile-multi 要 4 次采样、能跑 5~10 分钟，默认关；R&D 需要时打开
    "tile_multi": False,
    # AI 补缝后是否跑「修补带后处理」（wrap 强制还原 + 带均值匹配，见 steps/09）。
    # 开：密集大花/高对比图的 03/03b 通过率明显更高（L39）；关：退回原 02-seam-ai。
    "ai_postfix": True,
    "postfix": {"k": 1.0, "border": 16, "band": 180, "rounds": 5,
                "ring": 40, "gain": 1.0, "ramp": 20},
    # 修缝前把底图缩到这个长边（算法参数是按 1024 调的；大图会改变 band 相对比例）
    "base_max_side": 1024,
    # 所有候选都过不了 A 级审查时，是否挑缺陷最小的按 B 级（接缝轻微可见）交付。
    # 2026-10-20 产品确认：允许 B 级，但话术里必须如实说"接缝可能轻微可见"。
    "allow_grade_b": True,
    "review": True,          # False = 关闭 03/03b 接缝审查（用户要求时用，交付时标注"未审"）
}


class SeamlessError(Exception):
    pass


class SeamlessEngine:
    def __init__(self, config):
        conf = dict(DEFAULT_CONF)
        conf.update(config.settings.get("seamless") or {})
        self.conf = conf
        self.enabled = bool(conf.get("enabled"))
        self.cost = int(conf.get("cost") or 0)
        self.timeout_probe = int(conf.get("timeout_probe") or 180)
        self.timeout_run = int(conf.get("timeout_run") or 900)
        self.algo = Algo(config)
        # 后端显式配的 key 优先（secrets / 环境变量）；没配就看算法目录的 .env
        self.key = ((os.environ.get("PATTERNFLOW_SEAMLESS_KEY")
                     or config.secrets.get("seamless_api_key") or "").strip()
                    or self.algo.key)
        # 最近一次体检结果（按图内容缓存）：提案时算过，执行时直接复用，
        # 省一次特征提取，也保证「卡片上写的路线」和「实际尝试的顺序」一致。
        self._probe_cache = {}
        self._current = None

    # ---------- 可用性 ----------
    @property
    def installed(self):
        return self.algo.installed

    @property
    def available(self):
        return self.enabled and self.installed

    @property
    def missing_note(self):
        if not self.enabled:
            return "四方连续功能没启用（settings.json → seamless.enabled）。"
        return self.algo.missing_note

    def describe(self):
        d = self.algo.describe()
        d.update({"enabled": self.enabled, "available": self.available,
                  "cost": self.cost, "has_key": bool(self.key),
                  "max_attempts": int(self.conf.get("max_attempts") or 3),
                  "tile_multi": bool(self.conf.get("tile_multi")),
                  "allow_grade_b": bool(self.conf.get("allow_grade_b", True)),
                  "review": bool(self.conf.get("review", True)),
                  "ai_postfix": bool(self.conf.get("ai_postfix", True)),
                  "base_max_side": int(self.conf.get("base_max_side") or 0),
                  "direct_net": True})
        return d

    # ---------- 路线 ----------
    def _allowed(self, attempts):
        """候选顺序的产品侧收口。

        - 没有生图 key：只保留不花钱的快速路径（fast）
        - tile-multi 默认不跑：它要 4 次模型采样，实测一张图能吃掉 5~10 分钟，
          产品侧等不起；研发需要时把 settings.seamless.tile_multi 打开
        - 最多跑 max_attempts 条（默认 3），其余留给 R&D
        """
        max_n = max(1, int(self.conf.get("max_attempts") or 3))
        attempts = [a for a in (attempts or []) if a in KIND_LABEL]
        if self.conf.get("ai_postfix", True):
            attempts = ["ai2" if a == "ai" else a for a in attempts]
        if not self.conf.get("tile_multi"):
            attempts = [a for a in attempts if a != "tile-multi"]
        if not self.key:
            attempts = [a for a in attempts if a == "fast"]
        return attempts[:max_n]

    def _probe_report(self, blob, ext, timeout):
        key = hashlib.sha1(blob).hexdigest()
        info = self._probe_cache.get(key)
        if info:
            return info
        rep = self.algo.dry_run(blob, ext, timeout=timeout)
        attempts = self._allowed(rep.get("attempts"))
        feats = rep.get("features") or {}
        abort = (rep.get("planner") or {}).get("abort")
        if not attempts:
            if abort:
                why = (rep.get("planner") or {}).get("rationale") \
                      or rep.get("error") or "算法判断现有方法都不适用"
                info = {"ok": True, "route_hint": "reject",
                        "warnings": ["算法拒单：%s" % why],
                        "attempts": [], "features": feats,
                        "summary": "算法判断这张图不适合做四方连续"}
            else:
                info = {"ok": True, "route_hint": "reject",
                        "warnings": ["这张图找不到可用的无缝路线（缺生图 key 且纯算法不适用）"],
                        "attempts": [], "features": feats,
                        "summary": "没有可用路线"}
        else:
            first = attempts[0]
            # 卡片/对话只给「这张图能不能做」的结论；特征数字和路线留在
            # features/attempts 里给排查用，不再往用户脸上摆。
            flat = feats.get("lowGradFrac")
            try:
                flat = float(flat)
            except (TypeError, ValueError):
                flat = None
            if flat is None:
                verdict = "可以做：图案适合做四方连续。"
            elif flat >= 0.34:
                verdict = "可以做，但留白偏多，接缝处能对齐的内容少，效果可能一般。"
            else:
                verdict = "可以做：图案内容够，接缝能对齐。"
            info = {
                "ok": True,
                "route_hint": ("pure-algorithm" if first in ("fast", "det") else "model"),
                "attempts": attempts,
                "features": feats,
                "suggested": first,
                "summary": verdict,
                "planner": (rep.get("planner") or {}).get("source"),
                "warnings": ([] if self.key else ["没配生图 key：只能用零成本的算法路线。"]),
            }
        self._probe_cache[key] = info
        return info

    # ---------- 对外：体检 ----------
    def probe(self, blob, ext=".png"):
        """输入体检：零成本，只跑特征 + 候选顺序，不调任何模型。"""
        if not self.available:
            raise SeamlessError(self.missing_note)
        self._begin("probe")
        try:
            info = self._probe_report(blob, ext, self.timeout_probe)
        except AlgoError as e:
            raise SeamlessError(str(e))
        finally:
            self._end()
        return info

    # ---------- 对外：执行 ----------
    def convert(self, blob, ext=".png", engine="auto"):
        """完整执行：按候选顺序逐条跑 + 审查，第一条通过就交付。

        `engine` 参数保留旧接口（auto/pure/model），新算法统一走自动策略，
        这里**有意忽略**它 —— 前端已经不再给用户选路线了。
        """
        if not self.available:
            raise SeamlessError(self.missing_note)
        workdir = tempfile.mkdtemp(prefix="seamless_")
        src = os.path.join(workdir, "input" + ext)
        with open(src, "wb") as f:
            f.write(blob)
        # 算法参数按 1024 调参；输入更大时先缩到 base_max_side，保证 band 相对比例一致
        max_side = int(self.conf.get("base_max_side") or 0)
        if max_side > 0:
            work_src = os.path.join(workdir, "input-seam" + ext)
            try:
                self.algo.shrink(src, work_src, max_width=max_side,
                                 timeout=self._budget(time.time() + self.timeout_run, 120))
            except Exception:                            # noqa: BLE001
                work_src = src                           # 缩图失败就用原图，别挡住
            src = work_src

        t0 = time.time()
        log = []
        self._begin("convert")
        try:
            info = self._probe_report(blob, ext, self.timeout_probe)
            attempts = info.get("attempts") or []
            if not self.conf.get("review", True):
                # 审查关掉后不能"先跑先赢"：按画质优先级排队，避免 det/fast 的
                # 直出交叉淡化被当成最终交付（2026-10-22 实测：det 会在中线留可见缝）。
                _order = {"ai2": 0, "ai": 1, "det": 2, "regenerate": 3,
                          "tile-multi": 4, "fast": 5}
                attempts = sorted(attempts, key=lambda k: _order.get(k, 9))
            if not attempts:
                raise SeamlessError(
                    "这张图没有可用的无缝路线。换一张有明确图案的花型图，"
                    "或者让花型师按需求出一张。")
            chosen = None
            failed = []
            deadline = t0 + self.timeout_run
            # AI 清洗只做一次，所有非 fast 候选共用同一张底图。
            # 放在候选循环**外面**：网络/模型失败时立刻报真因，而不是把同一次
            # 清洗失败重复三遍，最后误报成「三条路线都没过接缝审查」。
            if any(k != "fast" for k in attempts):
                self._ensure_base(src, os.path.join(workdir, "01-base.png"),
                                  [], deadline)
            for kind in attempts:
                if time.time() > deadline - 15:
                    raise SeamlessError(
                        "四方连续跑超时了（超过 %d 分钟）。稍等一会儿再试，"
                        "或者换一张更简单的图案。" % (self.timeout_run // 60))
                cand = self._candidate(kind, src, workdir, deadline)
                log.extend(cand.get("log") or [])
                if cand["ok"]:
                    chosen = cand
                    break
                failed.append(cand)
            if chosen is None and not self.conf.get("review", True):
                raise SeamlessError(
                    "这几条路线都没出图（%s）。检查一下网络/生图网关，或者换一张图再试。"
                    % "；".join(log[-4:]))
            if chosen is None:
                # A 级全灭：按产品口径（2026-10-20）挑缺陷最小的做 B 级交付。
                # 只有「wrap 本身就是断的」那种候选直接淘汰 —— 那不叫轻微可见，
                # 那是根本不无缝，不能给用户。
                chosen = self._best_b(failed) if self.conf.get("allow_grade_b", True) else None
                if chosen is None:
                    raise SeamlessError(
                        "这张图三条路线都没通过接缝审查（%s）。"
                        "换一张图案更规整、留白更少的花型图再试。"
                        % "；".join(log[-4:]))
                chosen["grade_b"] = True
                log.append("按 B 级交付：%s（%s）" % (
                    KIND_LABEL.get(chosen["kind"], chosen["kind"]),
                    chosen.get("reason") or "修复带轻微可见"))
        except AlgoError as e:
            raise SeamlessError(str(e))
        except Exception:
            shutil.rmtree(workdir, ignore_errors=True)
            raise
        finally:
            self._end()

        rep = chosen.get("review") or {}
        detail = rep.get("detail") or {}
        ratio = detail.get("wrap") or {}
        # 审查已通过（03 无线 + 03b 带内一致）即 A 级；只有 B 级兜底才标 B。
        # 不再用原始 wrap 比值二次降级 —— 高方差图上这个比值本来就偏大（L40），
        # 会把自己的 A 级交付标成 B。
        grade_b = bool(chosen.get("grade_b"))
        reviewed = not bool(rep.get("skipped"))
        grade = ("B" if grade_b else "A") if reviewed else "未审"
        warnings = []
        if not reviewed:
            warnings.append("按用户要求已关闭接缝审查：本次交付未验证无缝性")
        elif grade_b:
            warnings.append("接缝带亮度/颗粒轻微不一致，按 B 级交付（%s）"
                            % (chosen.get("reason") or "修复带轻微可见"))
        cost = round(sum((m or {}).get("cost_usd") or 0 for m in chosen.get("metas") or []), 5)
        result = {
            "ok": True,
            "route": chosen["kind"],
            "route_label": KIND_LABEL.get(chosen["kind"]),
            "outputs": {"tile": chosen["flat"]},
            "seam_report": {"grade": grade, "ok": True,
                            "degraded": grade_b, "reviewed": reviewed,
                            "reason": (chosen.get("reason") or "") if grade_b else "",
                            "detail": detail,
                            "ratio": {"x": ratio.get("ratioX"), "y": ratio.get("ratioY")}},
            "warnings": warnings,
            "fidelity": {},
            "probe": {"attempts": attempts, "features": info.get("features")},
            "log": log,
            "cost_usd": cost,
            "seconds": round(time.time() - t0, 1),
            "input": src,
        }
        return result, workdir

    # ---------- B 级挑选 ----------
    @staticmethod
    def _defect_score(review):
        """B 级候选评分：越小越好；None = 不能按 B 级交付。

        2026-10-20 修正（用户实测打回）：**B 级只允许「软带」**——
        接缝审查（03）通过、只是修复带的亮度/颗粒轻微不一致（03b 判 fail）。
        一旦 03 检出贯穿直线，或者 wrap 本身就是断的，那是硬缺陷：
        平铺时会看到一条条对不上的缝，不能拿"B 级"糊过去。
        """
        d = (review or {}).get("detail") or {}
        wrap = d.get("wrap") or {}
        if wrap.get("fail"):
            return None
        if d.get("suspicious_lines"):
            return None
        reason = d.get("reason") or ""
        if not reason.startswith("修复带可见"):
            return None
        rep = d.get("repair") or {}
        score = 0.0
        try:
            if rep.get("ok") is False:
                score += 1.0
            score += abs(float(rep.get("hp_ratio") or 1.0) - 1.0) * 3.0
            score += abs(float(rep.get("d_lp") or 0.0))
        except (TypeError, ValueError):
            score += 2.0
        return score

    def _best_b(self, failed):
        """从 A 级没过的候选里挑缺陷最小的那个做 B 级交付。"""
        best, best_score = None, None
        for c in failed or []:
            sc = self._defect_score(c.get("review"))
            if sc is None:
                continue
            if best_score is None or sc < best_score:
                best, best_score = c, sc
        return best

    # ---------- 候选执行 ----------
    def _candidate(self, kind, src, workdir, deadline=None):
        """跑一条候选路线。返回 {ok, kind, flat, review, metas, log, reason}。

        deadline 是整次 convert 的硬截止（time.time() 秒）。每一步的 timeout
        都取「剩余预算」和单步上限的小值 —— 否则某一步（比如 tile-multi 的
        4 个样本）能把整次调用拖到远超用户能接受的时间。
        """
        lines = []
        base = os.path.join(workdir, "01-base.png")
        flat = os.path.join(workdir, "02-seamless.png")
        metas = []
        try:
            if kind == "fast":
                self._phase("clean")
                self.algo.shrink(src, base, timeout=self._budget(deadline, 120))
            else:
                self._ensure_base(src, base, metas, deadline)
            self._phase("seam")
            self._seam(kind, base, flat, metas, deadline)
            if not self.conf.get("review", True):
                # 用户要求关闭接缝审查：出图即交付，不再跑 03/03b，交付结果标注"未审"
                review = {"ok": True, "skipped": True,
                          "detail": {"reason": "接缝审查已关闭（按用户要求）"}}
                lines.append("%s：已出图（接缝审查已关闭）" % KIND_LABEL.get(kind, kind))
            else:
                self._phase("review")
                review = self._review_seam(flat, deadline)
                if review["ok"] and kind in ("ai", "ai2"):
                    review = self._review_repair(flat, review, deadline)
                if review["ok"]:
                    ratio = (review.get("detail") or {}).get("wrap") or {}
                    line = "%s：通过（wrapX=%s / wrapY=%s）" % (
                        KIND_LABEL.get(kind, kind), ratio.get("ratioX"), ratio.get("ratioY"))
                else:
                    line = "%s：审查没过（%s）" % (
                        KIND_LABEL.get(kind, kind),
                        (review.get("detail") or {}).get("reason") or "接缝可见")
                lines.append(line)
            # 注意：失败的候选**不再删产物** —— 全部 A 级失败时要拿它做 B 级交付。
            # 临时目录本来就会在 handler 交付后整目录删掉，不会长期占盘。
            return {"ok": review["ok"], "kind": kind, "flat": flat,
                    "review": review, "metas": metas, "log": lines,
                    "reason": (review.get("detail") or {}).get("reason")
                              or ("接缝可见" if not review["ok"] else "")}
        except AlgoError as e:
            return {"ok": False, "kind": kind, "flat": flat, "metas": metas,
                    "log": ["%s：失败（%s）" % (KIND_LABEL.get(kind, kind), e)],
                    "reason": str(e)}

    def _budget(self, deadline, cap):
        """这一步最多能等多久：剩余预算和单步上限取小，且不少于 30 秒。"""
        if not deadline:
            return cap
        return max(30, min(cap, int(deadline - time.time())))

    def _ensure_base(self, src, base, metas, deadline=None):
        """01-clean：AI 清洗（保花型）。同一轮里只洗一次，多条候选共用。"""
        if os.path.exists(base):
            # 已经洗过（convert 会先统一洗一次）：只补成本，不重复调模型
            metas.append(self.algo.meta(base))
            return
        self._phase("clean")
        code, out, err = self.algo.run_node(
            "steps/01-clean.mjs",
            ["--input", src, "--out", base, "--model", self.algo.model_clean],
            timeout=self._budget(deadline, self.algo.timeout_step))
        if code != 0:
            raise AlgoError("AI 清洗失败了。%s"
                            % (self.algo.last_json(out) or self.algo.error_summary(out, err)))
        metas.append(self.algo.meta(base))

    def _seam(self, kind, base, flat, metas, deadline=None):
        args = ["--base", base, "--out", flat]
        if kind in ("fast", "det"):
            script = "steps/02-seam-deterministic.mjs"
        elif kind in ("ai", "ai2"):
            script = "steps/02-seam-ai.mjs"
            args += ["--model", self.algo.model_seam,
                     "--band", self.algo.band, "--feather", self.algo.feather]
        elif kind == "tile-multi":
            script = "steps/02-seam-tile-multi.mjs"
            args += ["--model", self.algo.model_seam, "--samples", "4"]
        elif kind == "regenerate":
            script = "steps/02-seam-regenerate.mjs"
            args += ["--model", self.algo.model_seam, "--size", "1:1"]
        else:
            raise AlgoError("未知路线：%s" % kind)
        code, out, err = self.algo.run_node(
            script, args, timeout=self._budget(deadline, self.algo.timeout_step))
        if code != 0:
            raise AlgoError("%s 没跑出来。%s"
                            % (os.path.basename(script), self.algo.error_summary(out, err)))
        metas.append(self.algo.meta(flat))
        if kind == "ai2":
            # 修补带后处理：wrap 强制还原 + AI 贡献收敛 + 带均值匹配（L39）
            self._phase("review")
            pf = self.conf.get("postfix") or {}
            repair_w = int(pf.get("band") or
                           (self.algo.band + 2 * self.algo.feather + 2))
            code2, out2, err2 = self.algo.run_py(
                "steps/09-seam-postfix.py",
                ["--base", base, "--ai", flat, "--out", flat,
                 "--k", pf.get("k", 1.0), "--border", pf.get("border", 16),
                 "--band", repair_w, "--rounds", pf.get("rounds", 5),
                 "--ring", pf.get("ring", 40), "--gain", pf.get("gain", 1.0),
                 "--ramp", pf.get("ramp", 20)],
                timeout=self._budget(deadline, 300))
            if code2 != 0:
                raise AlgoError("修补带后处理失败：%s" % self.algo.error_summary(out, err2))

    def _review_seam(self, flat, deadline=None):
        code, out, err = self.algo.run_py(
            "steps/03-verify-seam.py", ["--image", flat],
            timeout=self._budget(deadline, 300))
        detail = self.algo.last_json(out) or {}
        ok = (code == 0 and detail.get("ok") is True)
        if not detail:
            detail = {"reason": "审查器没输出结果：%s" % self.algo.error_summary(out, err)}
        return {"ok": ok, "detail": detail}

    def _review_repair(self, flat, review, deadline=None):
        """AI 补缝还要查修复带的亮度/颗粒连续性（带内和周围差太多就是可见补丁）。"""
        meta = self.algo.meta(flat)
        size = meta.get("size") or []
        if len(size) != 2:
            return review
        w, h = int(size[0]), int(size[1])
        band = self.algo.band + 2 * self.algo.feather + 2
        bands = "v:%d,%d;h:%d,%d" % (w // 2, band, h // 2, band)
        code, out, err = self.algo.run_py(
            "steps/03b-verify-repair.py", ["--image", flat, "--bands", bands],
            timeout=self._budget(deadline, 300))
        detail = self.algo.last_json(out) or {}
        if code != 0 or detail.get("ok") is not True:
            merged = dict(review)
            merged["ok"] = False
            merged["detail"] = {"reason": "修复带可见（亮度/颗粒不连续）",
                                "repair": detail, "seam": review.get("detail")}
            return merged
        return review

    # ---------- 进度 / 终止 ----------
    LABELS = {
        "probe": "正在体检这张图",
        "clean": "正在清洗底图（保花型）",
        "seam": "正在对齐接缝、补好再拼（一两分钟）",
        "review": "正在审查接缝",
    }

    def _begin(self, phase):
        self._current = {"phase": phase, "t0": time.time(),
                         "label": self.LABELS.get(phase) or "正在处理"}

    def _phase(self, phase):
        if self._current is not None:
            self._current["phase"] = phase
            self._current["label"] = self.LABELS.get(phase) or "正在处理"

    def _end(self):
        self._current = None

    def progress(self):
        cur = self._current
        if not cur:
            return {"ok": True, "running": False}
        return {"ok": True, "running": True, "cmd": cur["phase"],
                "label": cur.get("label") or "正在处理",
                "elapsed": round(time.time() - cur["t0"], 1)}

    def cancel(self):
        """用户点了「终止」：杀掉算法子进程（含它的子进程）。"""
        ok = self.algo.cancel()
        if ok:
            print("[seamless] 已终止当前算法步骤（用户点了终止）")
        return ok
