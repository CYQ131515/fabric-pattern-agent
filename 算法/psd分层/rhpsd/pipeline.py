"""编排：上传 → deepseek 规划 → 逐层跑 SAM3.1 → 补底布 → 写 PSD。

关于"合成为什么无损"：
SAM3.1 是**分割**模型，输出的 RGB 就是原图那几个像素，不重画。
所以每层内容直接取原图，无论 alpha 是什么，合成结果都恒等于原图 —— 不需要
任何收敛修复，也不需要模型去"想象"被遮挡的内容（旧算法那套全都不需要了）。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

from . import analyze as analyze_mod
from . import fill as fill_mod
from .client import (IMAGE_FIELD, IMAGE_NODE, OUT_TARGET, TEXT_FIELD,
                     TEXT_NODE, RunningHub, WORKFLOW_ID)
from .psd import composite

# 蒙版退化判据：超过这个比例说明"整幅都选了"，低于那个比例说明"什么都没选"
DEGENERATE_HIGH = 0.85
DEGENERATE_LOW = 0.004

# 新层和已有层的 IoU 超过这个值，就认为是同一批像素，不要重复建层。
# 实测踩过：第 3 层所有提示词都退化，最后退到默认提示词，结果和「主体花」
# 一模一样（都是 30.2%），PSD 里凭空多一个完全重复的图层。
DUPLICATE_IOU = 0.6

# SAM3.1 单次标记总数的硬上限。超过就退化成"整幅全选"，见 _prompt_total。
PROMPT_TOTAL_LIMIT = 32

# 连续蔓延的纹理（枝叶底纹）SAM3 会整幅全选，这类层挪到最后跑。
# 否则它先退化、落到兜底的通用"花朵"蒙版，把后面的主体花挤成 2.5%。
CONTINUOUS_HINTS = ("底纹", "叶", "枝", "藤", "蔓", "茎", "背景", "纹理")


def _run_order(layers: list[dict]) -> list[int]:
    """跑工作流的顺序（≠ PSD 层序）。可分割性优先，连续纹理垫底。"""
    def late(i: int) -> int:
        blob = layers[i]["name"] + layers[i]["prompt"]
        return 1 if any(k in blob for k in CONTINUOUS_HINTS) else 0
    return sorted(range(len(layers)), key=late)   # sorted 稳定，其余保持原序


@dataclass
class Config:
    workflow_id: str = WORKFLOW_ID
    max_layers: int = 4
    max_side: int = 2048          # 超过就缩，省上传/下载时间和 RH 币
    instance_type: str = ""
    poll: float = 5.0
    # **单个云端任务**的轮询上限。正常一个任务约 30 秒；给到 900 秒的后果是
    # 「云端一堵，用户就盯着转圈满 15 分钟」，最后还被主程序的进程级上限直接
    # kill —— 白等一场、连"换下一条提示词"的机会都没有（真踩过，见 MEMORY）。
    # 180 秒 = 正常耗时 6 倍余量：卡住就赶紧失败，把机会让给下一条候选提示词。
    timeout: float = 180.0
    do_analyze: bool = True
    # 实测：数量给到 32（上限）时 SAM3 会退化成"整幅全选"，24 就正常。别用 32。
    fallback_prompt: str = "花朵:24"
    manual_layers: list[dict] | None = None   # 手写了就跳过诊断
    # 视觉模型（分层规划用）。留空 = 用 analyze.resolve 的默认值。
    vlm_base_url: str = ""
    vlm_model: str = ""
    vlm_key: str = ""
    vlm_provider: str = ""
    # 链式分割：下一层拿"上一层扣完后"的图去分，避免重复检出同一批像素。
    # 注意只换**分割输入**，每层像素仍取原图，否则合成不再等于原图。
    chain: bool = False
    chain_fill: str = "ground"    # ground=按图案周期补全 / white=填白
    max_retry: int = 3            # 蒙版退化时重试几次（含同提示词重试）
    keep_raw: bool = False
    # 进度落盘的位置（主程序传进来的绝对路径）。留空 = 不写。
    # 分层是"逐层跑云端"，一层几十秒起步，界面必须能回答"跑到第几层了、
    # 等了多久"，否则用户分不清"在跑"和"死了"。
    progress_out: str = ""


def _progress(cfg: Config, **fields) -> None:
    """把当前进度原子写进 cfg.progress_out（读方永远看不到半截文件）。

    纯旁观信息：写失败绝不影响分层本身，所以只吞 OSError。
    """
    if not cfg.progress_out:
        return
    fields["ts"] = time.time()
    try:
        tmp = cfg.progress_out + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(fields, f, ensure_ascii=False)
        os.replace(tmp, cfg.progress_out)
    except OSError:
        pass


@dataclass
class Result:
    rgbs: list[np.ndarray]
    alphas: list[np.ndarray]
    names: list[str]
    composite: np.ndarray
    ground: np.ndarray
    plan: dict
    log: list[str] = field(default_factory=list)
    checks: dict = field(default_factory=dict)
    used_prompts: list[str] = field(default_factory=list)


def _fit(img: np.ndarray, max_side: int) -> np.ndarray:
    h, w = img.shape[:2]
    m = max(h, w)
    if m <= max_side:
        return img
    s = max_side / m
    return np.asarray(Image.fromarray(img).resize(
        (max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS))


def _run_workflow(rh: RunningHub, cfg: Config, file_name: str, prompt: str,
                  size: tuple[int, int], dst: str) -> np.ndarray:
    """跑一次工作流，把目标层下载到 dst 并返回 alpha。"""
    nodes = [
        {"nodeId": IMAGE_NODE, "fieldName": IMAGE_FIELD, "fieldValue": file_name},
        {"nodeId": TEXT_NODE, "fieldName": TEXT_FIELD, "fieldValue": prompt},
    ]
    _, outs = rh.run(cfg.workflow_id, nodes,
                     instance_type=cfg.instance_type or None,
                     poll=cfg.poll, timeout=cfg.timeout)
    pick = next((o for o in outs if str(o.get("nodeId")) == OUT_TARGET), None)
    if pick is None or not pick.get("fileUrl"):
        raise RuntimeError(f"没拿到节点 {OUT_TARGET} 的输出（拿到 "
                           f"{[o.get('nodeId') for o in outs]}）")
    rh.download(pick["fileUrl"], dst)
    a = _alpha_of(dst, size)
    if a is None:
        raise RuntimeError("输出没有透明通道，可能这一层没抠出来")
    return a


def _alpha_of(path: str, size: tuple[int, int]) -> np.ndarray | None:
    im = Image.open(path)
    if im.mode not in ("RGBA", "LA", "PA"):
        return None
    a = np.asarray(im.convert("RGBA"), dtype=np.float64)[..., 3] / 255.0
    if a.shape != size:
        a = np.asarray(Image.fromarray((a * 255).astype(np.uint8)).resize(
            (size[1], size[0]), Image.LANCZOS), dtype=np.float64) / 255.0
    return a


def _is_duplicate(a: np.ndarray, accepted: list[np.ndarray]
                  ) -> tuple[bool, float]:
    m = a > 0.5
    for b in accepted:
        n = b > 0.5
        union = float((m | n).sum())
        if union == 0:
            continue
        iou = float((m & n).sum()) / union
        if iou > DUPLICATE_IOU:
            return True, iou
    return False, 0.0


def _prompt_total(prompt: str) -> int:
    """提示词里所有对象的数量之和。没写数量的按 1 算。

    SAM3.1 的标记总数上限是 32，超了就会退化成"整幅全选"。
    实测 `深蓝色叶子:20,细枝:12,黄色小穗:8`（和 = 40）→ 100% 全选，
    白烧 3 次工作流调用。所以超过上限的提示词不原样试，先砍成单对象。
    """
    total = 0
    for part in prompt.split(","):
        part = part.strip()
        if not part:
            continue
        n = 1
        if ":" in part:
            try:
                n = int(float(part.rsplit(":", 1)[1].strip()))
            except ValueError:
                n = 1
        total += max(1, n)
    return total


def _candidates(prompt: str, cfg: Config, is_first: bool = False) -> list[str]:
    """跑工作流时的提示词序列，越靠前越先试。

    实测 SAM3.1 **同一个提示词逐次结果会抖**（同一张图上 花朵:24 出现过 30% 和 100%），
    所以先原样重试一次，再换数量。

    超标（数量之和 > 32）的提示词排到最后，优先试砍成单对象的版本。
    通用兜底提示词只给第一层用：给后面的层用等于复制第一层，白白多花一次调用。
    """
    out: list[str] = []
    head = prompt.split(",")[0].split(":")[0].strip()
    if _prompt_total(prompt) > PROMPT_TOTAL_LIMIT and head:
        out.append(f"{head}:24")            # 砍成单对象，别去撞上限
    out += [prompt, prompt]
    if head:
        alt = f"{head}:24"
        if alt not in out:
            out.append(alt)
    if is_first and cfg.fallback_prompt not in out:
        out.append(cfg.fallback_prompt)
    seen: list[str] = []
    for p in out:
        if p not in seen:
            seen.append(p)
    return seen[: cfg.max_retry + 1]


def _next_input(orig: np.ndarray, hole: np.ndarray, mode: str) -> np.ndarray:
    """链式分割的下一张输入图：把已抠走的区域抹掉。

    hole 区域怎么处理有两种：
    - ground：按图案周期借真纹理补全，画面连贯，但补全处可能"长"出假元素被误检
    - white：直接填白，背景变干净，但会引入强边缘
    无论哪种，**只用于喂给 SAM3 决定 alpha**，每层像素仍然取原图。
    """
    if mode == "white":
        out = orig.copy()
        out[hole] = 255.0
        return out
    return fill_mod.fill_holes(orig, hole)


def run(img: np.ndarray, rh: RunningHub, api_key: str = "",
        cfg: Config | None = None, workdir: str = "") -> Result:
    cfg = cfg or Config()
    log: list[str] = []
    img = img[..., :3]
    orig = _fit(img, cfg.max_side).astype(np.float64)
    h, w = orig.shape[:2]
    log.append(f"工作尺寸 {w}x{h}")

    tmp = tempfile.mkdtemp(prefix="rhpsd_")
    raw_dir = ""
    if cfg.keep_raw:
        raw_dir = os.path.join(workdir or "out", "raw")
        os.makedirs(raw_dir, exist_ok=True)

    # ---- 1. 分层规划（放在上传/跑工作流之前：诊断不通就别花 RH 的钱）----
    default_plan = {"layers": [{"name": "花型", "prompt": cfg.fallback_prompt}]}
    base, model, key = analyze_mod.resolve(
        provider=cfg.vlm_provider, base_url=cfg.vlm_base_url,
        model=cfg.vlm_model, api_key=cfg.vlm_key or api_key)
    if cfg.manual_layers:
        plan = {"layers": list(cfg.manual_layers)}
        log.append("手动指定层：" + " / ".join(
            f"{x['name']}({x['prompt']})" for x in plan["layers"]))
    elif cfg.do_analyze and key:
        plan = default_plan
        log.append(f"诊断用视觉模型 {model} @ {base}")
        try:
            plan = analyze_mod.analyze(orig.astype(np.uint8), key,
                                       max_layers=cfg.max_layers,
                                       model=model, base_url=base)
            log.append("诊断：" + " / ".join(
                f"{x['name']}({x['prompt']})" for x in plan["layers"]))
        except Exception as exc:                            # noqa: BLE001
            log.append(f"诊断失败（{type(exc).__name__}: {exc}），用默认单层")
            log.append("提示：换网关用 --vlm-provider / --vlm-base-url，"
                       "或先跑 python -m rhpsd.analyze 看哪些网关现在可达")
    else:
        plan = default_plan
        log.append(f"跳过诊断，用默认提示词 {cfg.fallback_prompt}")

    total_layers = len(plan["layers"])
    _progress(cfg, phase="upload", total=total_layers)
    src = os.path.join(tmp, "input.png")
    Image.fromarray(orig.astype(np.uint8)).save(src)
    file_name = rh.upload(src)
    log.append(f"图片已上传（{os.path.getsize(src) // 1024} KB）")

    # ---- 2. 逐层跑工作流 ----
    alphas: list[np.ndarray] = []
    names: list[str] = []
    used: list[str] = []
    done: list[tuple[int, np.ndarray, str]] = []   # (PSD 层序, alpha, 实际提示词)
    cur_file = file_name
    order = _run_order(plan["layers"])
    for step, idx in enumerate(order):
        item = plan["layers"][idx]
        a = None
        p_used = ""
        _progress(cfg, phase="layer", index=step + 1, total=len(order),
                  name=item["name"], done=len(done))
        for attempt, p in enumerate(_candidates(item["prompt"], cfg,
                                                is_first=(not done))):
            dst = os.path.join(tmp, f"L{idx}_{len(used)}.png")
            _progress(cfg, phase="seeking", index=step + 1, total=len(order),
                      name=item["name"], done=len(done), attempt=attempt + 1)
            try:
                cand = _run_workflow(rh, cfg, cur_file, p, (h, w), dst)
            except Exception as exc:                        # noqa: BLE001
                log.append(f"层「{item['name']}」提示词「{p}」失败"
                           f"（{type(exc).__name__}: {exc}）")
                continue
            area = float((cand > 0.5).mean())
            used.append(p)
            if raw_dir:
                shutil.copy(dst, os.path.join(raw_dir, f"L{idx}_{p.replace(':','_')}.png"
                                              .replace("/", "_")))
            if not (DEGENERATE_LOW < area < DEGENERATE_HIGH):
                why = "整幅全选" if area >= DEGENERATE_HIGH else "几乎为空"
                log.append(f"层「{item['name']}」提示词「{p}」退化（{why} "
                           f"{area:.1%}），换提示词重试")
                continue
            dup, iou = _is_duplicate(cand, [d[1] for d in done])
            if dup:
                log.append(f"层「{item['name']}」提示词「{p}」和已有层重复"
                           f"（IoU {iou:.0%}），换提示词重试")
                continue
            a = cand
            p_used = p
            log.append(f"层「{item['name']}」提示词「{p}」ok  "
                       f"面积 {area:.1%}  节点 {OUT_TARGET}")
            break
        if a is None:
            log.append(f"层「{item['name']}」所有提示词都拿不到可用蒙版，跳过")
            continue
        done.append((idx, a, p_used))
        _progress(cfg, phase="layer_done", index=step + 1, total=len(order),
                  done=len(done), name=item["name"])

        # 链式：把这一层抹掉后的图作为下一层的分割输入
        if cfg.chain and step < len(order) - 1:
            nxt = _next_input(orig, a > 0.5, cfg.chain_fill)
            chain_path = os.path.join(tmp, f"chain_{step}.png")
            Image.fromarray(np.clip(nxt, 0, 255).astype(np.uint8)).save(chain_path)
            cur_file = rh.upload(chain_path)
            log.append(f"链式：第 {step + 1} 层已抠掉，输入图更新为"
                       f"「{cfg.chain_fill}」填充版")

    # 跑的顺序按可分割性打乱过，这里还原成 PSD 该有的从底到顶层序
    done.sort(key=lambda d: d[0])
    alphas = []
    names = []
    for idx, a, p_used in done:
        name = plan["layers"][idx]["name"]
        if p_used == cfg.fallback_prompt and p_used != plan["layers"][idx]["prompt"]:
            name = "花型"        # 退到兜底提示词了，原名已经名不副实
            log.append(f"层「{plan['layers'][idx]['name']}」最终用的是兜底提示词"
                       f"「{p_used}」，已改名为「花型」")
        alphas.append(a)
        names.append(name)

    if not alphas:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError("所有层都没拿到可用蒙版：" + "；".join(log))

    # ---- 3. 底布：把蒙版盖住的地方挖掉再补上 ----
    _progress(cfg, phase="assemble", total=len(order), done=len(done))
    union = np.max(np.stack(alphas, 0), 0)
    hole = union > 0.5
    ground = fill_mod.fill_holes(orig, hole)
    log.append(f"底布：挖掉 {hole.mean():.1%} 的面积并补全")

    # 关键点：合成必须严格等于原图。洞里填的是"编"出来的颜色，
    # 所以它只能在"必定被上层完全盖住"的地方露出来。洞的边界上有些像素
    # union 略小于 1，会漏一点填充色 —— 把这些像素的 alpha 抬到 1 就没了。
    leak = hole & (union < 1.0)
    if leak.any():
        owner = np.argmax(np.stack(alphas, 0)[:, leak], 0)
        rows, cols = np.nonzero(leak)
        for i, (r, c) in enumerate(zip(rows, cols)):
            alphas[owner[i]][r, c] = 1.0
        log.append(f"边缘 {int(leak.sum())} 个像素 alpha 抬到 1，保证合成无损")

    # ---- 4. 组装 ----
    rgbs = [ground] + [orig] * len(alphas)
    als = [np.ones((h, w), dtype=np.float64)] + alphas
    names_all = ["底布"] + names
    comp = composite(rgbs, als)
    diff = np.abs(comp - orig)
    checks = {
        "stack_max_diff": round(float(diff.max()), 3),
        "stack_mean_diff": round(float(diff.mean()), 4),
        "ground_ratio": round(float(hole.mean()), 4),
        "n_layers": len(names_all),
        "n_tasks": rh.n_tasks,
    }
    log.append(f"合成 ≡ 原图 最大差 {checks['stack_max_diff']} 灰阶")

    shutil.rmtree(tmp, ignore_errors=True)
    return Result(rgbs=rgbs, alphas=als, names=names_all, composite=comp,
                  ground=ground, plan=plan, log=log, checks=checks,
                  used_prompts=used)
