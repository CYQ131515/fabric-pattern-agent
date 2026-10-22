"""编排：算法搭骨架 → 大模型诊断 → 大模型补缝 → 算法强制还原 → 体检出图。

为什么是这个顺序
    1. 算法先动手（位移、mask、还原、对齐）—— 这些是确定性的，能算准，就不该交给模型；
    2. 模型只干算法干不了的那一小块：中央十字带的接续判断；
    3. 模型出图之后立刻用算法兜回来 —— mask 带外强制用原图像素。
       保真是架构保证的，不指望模型自觉（实测它会把整张图重画）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import metrics, seam
from .apimart import ANALYZE_MODEL, IMAGE_MODEL, APIMart, snap_size
from .io_utils import resize, side_by_side, tile_preview, working_size

# 兜底指令。刻意写得短：对编辑模型来说，"解释背景"是有害的，
# 它会照着描述重新生成整张图，而不再做局部修补。
BASE_PROMPT = (
    "Repair the discontinuity along the central cross of this image so the "
    "pattern continues seamlessly across it. Keep every motif, color and "
    "detail exactly identical everywhere else."
)


@dataclass
class Config:
    band_frac: float = 0.12        # 纯算法羽化带占边长比例
    align_frac: float = 0.03       # 收尾边界对齐带
    mask_band_frac: float = 0.07   # 中央十字带宽度（喂给模型的 mask）
    n_candidates: int = 1          # 模型不可复现，跑几个候选挑最优
    do_analyze: bool = True        # 是否让 deepseek 做接缝诊断
    analyze_model: str = ANALYZE_MODEL
    image_model: str = IMAGE_MODEL
    restore_original_size: bool = True   # 最终 tile 缩回原图尺寸
    sharp_band_frac: float = 0.08  # 保析度只看这条带（≈羽化/补缝实际动过的范围）


@dataclass
class Result:
    tile: np.ndarray
    preview: np.ndarray          # 3x3 平铺
    compare: np.ndarray          # 原图 / 纯算法 / 本次结果
    report: metrics.SeamReport
    baseline_report: metrics.SeamReport
    fidelity: metrics.FidelityReport | None
    route: str
    analysis: dict = field(default_factory=dict)
    prompt: str = ""
    intermediates: dict = field(default_factory=dict)
    log: list = field(default_factory=list)


def _undo_half_offset(arr: np.ndarray) -> np.ndarray:
    h, w = arr.shape[:2]
    return np.roll(np.roll(arr, -(h // 2), axis=0), -(w // 2), axis=1)


_GRADE_RANK = {"优": 3, "良": 2, "一般": 1, "差": 0}


def _sharp_penalty(v: float) -> float:
    """保析度偏离 1 的惩罚 —— **刻意做成不对称**。

    低于 1 = 接缝带的细节被抹掉了，这正是羽化重影的样子，最坏；
    高于 1 = 细节变多（模型给接缝补了真内容，也可能是噪点）。两件事语义
    相反，不该同罚。

    这不是拍脑袋：换到**不支持遮罩**的渠道后实测，模型会整图重绘、
    保析跑到 1.4~1.5，而纯算法因为羽化掉到 0.7。按原来的对称惩罚算，
    肉眼明显更干净的模型结果**反倒输给纯算法**、被退了回去
    （三联对比里能直接看到纯算法那版的规律重影）。
    所以"变多"只按 1/3 罚。
    """
    d = v - 1.0
    return d / 3.0 if d > 0 else -d


def _score(r: metrics.SeamReport) -> float:
    """越小越好：把各项指标压成一个可比较的分数。

    权重是踩坑踩出来的。第一版给 seam_visibility 压了 20 的权重，结果在深底
    密集花卉上把**肉眼明显更干净的模型结果判输了** ——

        纯算法   边界0.102 可见度0.814 重影0.935 保析度0.782
        模型     边界0.098 可见度1.262 重影1.022 保析度0.927

    根因：seam_visibility 是"接缝带梯度 ÷ 画面内部梯度"，分母跨了内容。
    密集花型里模型在接缝上补了几朵小花，带内梯度自然抬高 → 被判成"有硬边"；
    而纯算法把带内抹平，值掉到 0.81 → 反倒像是"很干净"。这条指标本身没法定量，
    所以权重压到 4，只用来兜"真有硬边"（>2）的情况。

    真正该主导的两个指标，分母都是**同一块区域自己的历史**，1.0 有绝对含义：
      · 重影 ghosting     —— 边缘带局部对比度 ÷ 内部（同图内相对量）
      · 保析度 sharp_keep —— 接缝带细节 ÷ 该区域修补前（内容逐像素对齐）
    后者按不对称罚，见 _sharp_penalty 的说明。
    """
    return (r.boundary_error * 2.0
            + abs(r.seam_visibility - 1.0) * 4.0
            + abs(r.ghosting - 1.0) * 30.0
            + _sharp_penalty(r.sharp_keep) * 30.0)


def _quality_key(r: metrics.SeamReport) -> tuple[int, float]:
    """先看评级，同级再比分数。比单看分数稳，也更符合人的判断顺序。"""
    return (_GRADE_RANK.get(r.grade, 0), -_score(r))


def build_prompt(analysis: dict | None, max_extra: int = 260) -> str:
    """把诊断结论拼成给图像模型的指令。

    为什么要截断：对编辑模型来说长指令是有害的 —— 它会照着描述重新生成整张图，
    而不是做局部修补。所以只取诊断结论里最关键的头部，并且尽量切在句子边界上。
    """
    extra = " ".join(((analysis or {}).get("repair_prompt") or "").split())
    if not extra or extra.lower() in ("none", "n/a"):
        return BASE_PROMPT
    if len(extra) > max_extra:
        cut = extra[:max_extra]
        for sep in (". ", "; ", ", "):
            k = cut.rfind(sep)
            if k > max_extra * 0.6:
                cut = cut[:k + 1]
                break
        extra = cut
    return f"{BASE_PROMPT} {extra}"


def run(image: np.ndarray, client: APIMart | None = None,
        cfg: Config | None = None) -> Result:
    cfg = cfg or Config()
    lo = image

    # 1) 规整到网关能接受的尺寸。原图往往不是 16 的倍数，直接送会被悄悄忽略。
    w0, h0 = lo.shape[1], lo.shape[0]
    ww, hh = working_size(w0, h0)
    log = [f"输入 {w0}x{h0} → 处理尺寸 {ww}x{hh}"]

    # 有些网关把出图尺寸锁死在白名单里，多一像素都回 400（实测 apimarket.site
    # 的 -1k 档：1280x1280、1152x896、1024x640 全被拒）。这种才需要在调模型前
    # 把处理尺寸吸附过去。原 APIMart 不锁尺寸，直接送就好 —— 吸附反而会平白
    # 拉伸一下，白白损失一点清晰度。
    # 判断由客户端给（`size_locked` 按网关主机名推断），不在这里写死网关名。
    # 纯算法路线永远不吸附：它是本机确定性计算，没道理受网关的尺寸规矩约束。
    # 吸附允许比例略微变化：模型只在中央十字带动笔，带外由 compose 强制还原，
    # 最后还会 resize 回原图尺寸，所以这点拉伸不会留在交付物上。
    if client is not None and getattr(client, "size_locked", False):
        sw, sh = snap_size(ww, hh)
        if (sw, sh) != (ww, hh):
            log.append(f"网关锁出图尺寸，处理尺寸吸附 {ww}x{hh} → {sw}x{sh}"
                       f"（交付前还原为 {w0}x{h0}）")
            ww, hh = sw, sh

    work = resize(lo, (ww, hh))

    inter: dict = {}

    # 保真度 / 保析度的比较坐标系：接缝居中姿态。
    # 交付的 tile 里，"被允许改动的那一条"在【画面边界】上（因为接缝被移回来了），
    # 而这类指标默认在画面【中心】找豁免区。直接比会把合法的修补算成"改了花型"。
    # 所以统一拿"接缝居中姿态的原图"当参考，指标内部负责把 tile 滚成同姿态。
    ref = seam.half_offset(work)

    # 2) 纯算法基线：永远可用，也用来当对照和兜底
    base_tile = seam.baseline(work, cfg.band_frac, cfg.align_frac)
    base_rep = metrics.evaluate(base_tile, ref, cfg.sharp_band_frac)
    inter["baseline"] = base_tile

    best_tile, best_rep, route = base_tile, base_rep, "pure-algorithm"
    analysis: dict = {}
    prompt = ""
    final_fid: metrics.FidelityReport | None = None

    if client is not None:
        # 3) 把接缝推到画面正中，得到"待修补图"
        to_edit = seam.half_offset(work)
        inter["to_edit"] = to_edit

        # 4) 大模型诊断：判断图案类型 + 中央十字坏在哪
        if cfg.do_analyze:
            try:
                analysis = client.analyze(work, to_edit, cfg.analyze_model)
                log.append("诊断完成：" + str(analysis.get("pattern_type")))
            except Exception as exc:                   # noqa: BLE001
                log.append(f"诊断失败，退回通用指令：{type(exc).__name__} {exc}")
                analysis = {}
        prompt = build_prompt(analysis)

        # 5) 只让模型改中央十字；带外像素由 compose 强制还原
        mask = seam.central_cross_mask(to_edit.shape, cfg.mask_band_frac)
        inter["mask"] = mask

        picked = None
        for i in range(max(1, cfg.n_candidates)):
            try:
                edited = client.edit(to_edit, prompt, (ww, hh), mask=mask,
                                     model=cfg.image_model)
            except Exception as exc:                   # noqa: BLE001
                log.append(f"候选{i + 1} 出图失败：{type(exc).__name__} {exc}")
                continue

            inter[f"model_output_{i + 1}"] = edited
            if edited.shape[:2] != to_edit.shape[:2]:
                edited = resize(edited, (ww, hh))

            # 诊断：模型有没有顺手把带外也重画了
            fid = metrics.fidelity(to_edit, edited)
            log.append(f"候选{i + 1}：带外改动 {fid.core_diff:.2f}，"
                       f"相关 {fid.correlation:.3f}")

            composed = seam.compose(to_edit, edited, mask)
            tile = seam.wrap_align(_undo_half_offset(composed), cfg.align_frac)
            rep = metrics.evaluate(tile, ref, cfg.sharp_band_frac)
            log.append(f"候选{i + 1}：边界 {rep.boundary_error:.3f} "
                       f"可见度 {rep.seam_visibility:.3f} "
                       f"重影 {rep.ghosting:.3f} "
                       f"保析 {rep.sharp_keep:.3f} → {rep.grade}")
            key = _quality_key(rep)
            if picked is None or key > picked[0]:
                picked = (key, tile, rep, fid, edited)

        if picked is not None:
            _, tile_m, rep_m, fid_m, edited_m = picked
            inter["model_output"] = edited_m
            inter["model_tile"] = tile_m
            if _quality_key(rep_m) > _quality_key(base_rep):
                best_tile, best_rep = tile_m, rep_m
                route = "model"
            else:
                log.append(f"模型结果（{rep_m.grade}）不如纯算法"
                           f"（{base_rep.grade}），已退回纯算法")
        else:
            log.append("模型一个候选都没成功，退回纯算法")

    # 交付 tile 的保真度：滚回接缝居中姿态后，和同姿态的原图比 ——
    # 这才是用户真正关心的"花还是不是那朵花"。
    # 必须在缩放回原尺寸【之前】比：缩放本身会制造全图像素差，那是重采样不是改花型。
    # 模型原始输出的保真只是诊断信息，不代表交付质量。
    final_fid = metrics.fidelity(ref, seam.half_offset(best_tile),
                                 band_frac=cfg.mask_band_frac)
    log.append(f"最终 tile 相对原图：主体差 {final_fid.core_diff:.2f}，"
               f"相关 {final_fid.correlation:.3f}，"
               f"改动 {final_fid.changed_ratio:.1%}")

    if cfg.restore_original_size:
        best_tile = resize(best_tile, (w0, h0))

    # 6) 出图：单元 + 3x3 平铺 + 三方对比
    base_show = resize(base_tile, (w0, h0)) if cfg.restore_original_size else base_tile
    preview = tile_preview(best_tile, 3)
    compare = side_by_side([tile_preview(lo, 2), tile_preview(base_show, 2),
                            tile_preview(best_tile, 2)], height=520,
                           labels=["原图直接平铺", "纯算法", f"本次结果（{route}）"])

    return Result(tile=best_tile, preview=preview, compare=compare,
                  report=best_rep, baseline_report=base_rep,
                  fidelity=final_fid, route=route, analysis=analysis,
                  prompt=prompt, intermediates=inter, log=log)
