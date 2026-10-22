"""
pattern_studio.sheet —— 第 4 层：九宫格拆格 / 去背 / 元素归一化

输入：一张 3×3 元素图（白底或近似纯色底）
输出：9 个（或更少）RGBA 元素 + 碰撞掩膜

处理链（每一步都对应一类真实失败模式）：
  1. 切格（内缩 gutter）：避免元素压线时被切成两半
  2. 背景色估计：取单元格最外圈像素的中位色（比"假设白色"稳，AI 常出米白/浅灰）
  3. 软阈值 alpha：alpha = smoothstep(dist, t0, t1) —— 保住水彩的柔边
  4. 反预乘去白边：半透明像素含底色，直接叠到深色背景会出现白边，必须还原前景色
  5. 连通域清理：只保留最大色块，去掉背景噪点/散落笔触
  6. bbox 裁切 + 长边归一化 + 生成低分辨率碰撞掩膜
  7. 质量门：覆盖率过低（空格/没画出来）或过高（背景没去干净）直接判废
"""
from __future__ import annotations

import math
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

ELEMENT_LONG_SIDE = 512      # 元素归一化后的长边
MASK_RES = 96                # 碰撞掩膜边长
ALPHA_SOLID = 40             # 判定"实体像素"的 alpha 阈值


@dataclass
class Element:
    id: str
    name: str
    element_index: int
    variant: int
    image: Image.Image            # RGBA
    mask: np.ndarray              # bool[MASK_RES, MASK_RES]（按长边等比缩放的占据图）
    coverage: float               # alpha>ALPHA_SOLID 的像素占 bbox 比例
    aspect: float                 # w/h
    rel_size: float = 1.0         # 相对尺寸（来自 spec.elements[].rel_size）
    weight: int = 1               # 出现权重
    layer: int = -1               # 叠层（-1=按 rel_size 自动：大0/中1/小2）
    source: str = ""

    def save(self, path: Path) -> None:
        self.image.save(path)


# ------------------------------------------------------------------ 基础工具

def _median_border_color(arr: np.ndarray, ring: int = 6) -> np.ndarray:
    h, w = arr.shape[:2]
    parts = [arr[:ring, :, :3].reshape(-1, 3), arr[-ring:, :, :3].reshape(-1, 3),
             arr[:, :ring, :3].reshape(-1, 3), arr[:, -ring:, :3].reshape(-1, 3)]
    px = np.concatenate(parts, axis=0)
    return np.median(px, axis=0)


def _bg_candidates(arr: np.ndarray, quantum: int = 12, min_frac: float = 0.06,
                   min_borders: int = 2, max_cands: int = 2) -> list[np.ndarray]:
    """从单元格里猜"可能属于背景的平涂色"（白底 + 浅色纸片这类多底色场景）。"""
    q = (arr // quantum).astype(np.int64)
    key = q[..., 0] * 10000 + q[..., 1] * 100 + q[..., 2]
    vals, counts = np.unique(key, return_counts=True)
    total = float(key.size)
    out: list[np.ndarray] = []
    for idx in np.argsort(-counts)[:8]:
        frac = counts[idx] / total
        if frac < min_frac:
            continue
        mask = key == vals[idx]
        borders = int(mask[0].mean() > 0.04) + int(mask[-1].mean() > 0.04) + \
            int(mask[:, 0].mean() > 0.04) + int(mask[:, -1].mean() > 0.04)
        if borders < min_borders:
            continue
        out.append(np.median(arr[mask], axis=0))
        if len(out) >= max_cands:
            break
    return out


def _smoothstep(x: np.ndarray, e0: float, e1: float) -> np.ndarray:
    t = np.clip((x - e0) / max(1e-6, (e1 - e0)), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def _largest_component(mask: np.ndarray) -> np.ndarray:
    """保留最大连通域（4 邻域 BFS）。mask 尺寸很小（<=128²），Python 也够快。"""
    h, w = mask.shape
    seen = np.zeros((h, w), bool)
    best = np.zeros((h, w), bool)
    best_n = 0
    for sy in range(h):
        for sx in range(w):
            if not mask[sy, sx] or seen[sy, sx]:
                continue
            q = deque([(sy, sx)])
            seen[sy, sx] = True
            comp = []
            while q:
                y, x = q.popleft()
                comp.append((y, x))
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        q.append((ny, nx))
            if len(comp) > best_n:
                best_n = len(comp)
                best = np.zeros((h, w), bool)
                ys, xs = zip(*comp)
                best[list(ys), list(xs)] = True
    return best


def _unpremultiply(rgb: np.ndarray, alpha: np.ndarray, bg: np.ndarray) -> np.ndarray:
    """半透明像素反预乘：F = bg + (P - bg) / a（裁到 0..255），去掉白底/纯色底残留的边。"""
    a = np.clip(alpha, 1e-3, 1.0)[..., None]
    fg = bg[None, None, :] + (rgb - bg[None, None, :]) / a
    # 只在有透明度的区域替换，全不透明区域保持原值
    out = np.where(alpha[..., None] < 0.995, np.clip(fg, 0, 255), rgb)
    return out


# ------------------------------------------------------------------ 核心：从单元格提取元素

def extract_cell(cell: Image.Image, gutter: float = 0.03,
                 t0: float = 10.0, t1: float = 46.0) -> tuple[Image.Image, np.ndarray, float] | None:
    """从一个单元格里抠出元素，返回 (RGBA 裁切图, 二值掩膜, 覆盖率)；失败返回 None。

    两种输入都支持：
      * **自带 alpha 的透明底图**（部分生图模型直接给透明 PNG —— 直接用 alpha 最准）
      * 白底/纯色底图（估底色 + 距离软阈值 + 反预乘 + 连通域清理）
    """
    cell = cell.convert("RGBA")
    raw = np.asarray(cell, dtype=np.uint8)
    w, h = cell.size
    gx, gy = int(w * gutter), int(h * gutter)

    # 真正的透明底 PNG：必须有相当比例 alpha≈0 的像素（半透明纸纹不算）
    if float((raw[..., 3] < 16).mean()) > 0.03:
        # ---------- 情况 A：图本身带透明背景
        rgba = raw[gy : h - gy, gx : w - gx]
        alpha = rgba[..., 3].astype(np.float32) / 255.0
        rgb = rgba[..., :3].astype(np.float32)
        # 透明区是黑的（预乘/铺黑）时反预乘，避免黑边
        dark = (rgba[..., 3] < 250) & (rgb.max(axis=2) < 8)
        if float(dark.mean()) > 0.02:
            a = np.clip(alpha, 1e-3, 1.0)[..., None]
            rgb = np.clip(rgb / a, 0, 255)
        if int((alpha > ALPHA_SOLID / 255.0).sum()) < 20:
            return None
        img = Image.fromarray(
            np.dstack([np.clip(rgb, 0, 255), np.clip(alpha * 255, 0, 255)]).astype(np.uint8), "RGBA")
    else:
        # ---------- 情况 B：不透明底
        inner = cell.convert("RGB").crop((gx, gy, w - gx, h - gy))
        arr = np.asarray(inner, dtype=np.float32)
        bg = _median_border_color(arr)

        # 多底色模型：有些模型会在白底上再垫一块浅色"纸片"（如米黄方块）。
        # 只认一个底色会把纸片当成前景。这里找出"频率高 + 至少贴着 2 条边"的颜色
        # 作为候选底色，取到最近候选色的距离。
        cands = [bg] + _bg_candidates(arr)
        dists = np.stack([np.sqrt(((arr - c[None, None, :]) ** 2).sum(axis=2)) for c in cands])
        dist = dists.min(axis=0)
        alpha = _smoothstep(dist, t0, t1)

        # 反例保护：有些模型会在每个元素下面垫一块纯色"纸片"。
        # 这时第一次抠出来的就是那个矩形垫片本身 —— 判断"掩膜是不是一块纯色矩形"，
        # 如果是，就把垫片颜色当真正的底色，重抠一次。
        solid = alpha > 0.6
        if solid.sum() > inner.width * inner.height * 0.25:
            ys0, xs0 = np.where(solid)
            bw = xs0.max() - xs0.min() + 1
            bh = ys0.max() - ys0.min() + 1
            fill = solid.sum() / float(bw * bh)
            patch = arr[solid]
            cstd = float(patch.std(axis=0).mean()) if patch.size else 99.0
            if fill > 0.80 and cstd < 10.0:
                bg = np.median(patch, axis=0)
                dist = np.sqrt(((arr - bg[None, None, :]) ** 2).sum(axis=2))
                alpha = _smoothstep(dist, t0, t1)

        small = np.asarray(Image.fromarray((alpha * 255).astype(np.uint8)).resize((128, 128), Image.BILINEAR))
        solid = small > 40
        if solid.sum() < 20:
            return None
        keep = _largest_component(solid)
        keep_full = np.asarray(Image.fromarray((keep * 255).astype(np.uint8)).resize(inner.size, Image.BILINEAR)) > 96
        alpha = np.where(keep_full, alpha, 0.0)

        rgb = _unpremultiply(arr, alpha, bg)
        img = Image.fromarray(
            np.dstack([np.clip(rgb, 0, 255), np.clip(alpha * 255, 0, 255)]).astype(np.uint8), "RGBA")

    # ---------- 统一收尾：bbox 裁切 + 覆盖率
    a = np.asarray(img, dtype=np.uint8)[..., 3]
    ys, xs = np.where(a > ALPHA_SOLID)
    if len(ys) == 0:
        return None
    pad = 2
    y0, y1 = max(0, ys.min() - pad), min(img.height, ys.max() + pad + 1)
    x0, x1 = max(0, xs.min() - pad), min(img.width, xs.max() + pad + 1)
    img = img.crop((x0, y0, x1, y1))
    a = np.asarray(img, dtype=np.uint8)[..., 3]
    coverage = float((a > ALPHA_SOLID).sum()) / float(max(1, a.size))
    return img, (a > ALPHA_SOLID), coverage


def normalize_element(img: Image.Image, long_side: int = ELEMENT_LONG_SIDE) -> Image.Image:
    w, h = img.size
    scale = long_side / max(w, h)
    if abs(scale - 1.0) > 1e-3:
        img = img.resize((max(1, int(round(w * scale))), max(1, int(round(h * scale)))), Image.LANCZOS)
    return img


def make_mask(img: Image.Image, res: int = MASK_RES) -> np.ndarray:
    """把 alpha 等比缩到 res 内的掩膜（bool），用于非重叠求解。"""
    a = img.split()[-1]
    w, h = img.size
    scale = res / max(w, h)
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    small = np.asarray(a.resize((nw, nh), Image.BILINEAR))
    return small > 24


def quality_ok(img: Image.Image, coverage: float) -> bool:
    return 0.004 <= coverage <= 0.96


# ------------------------------------------------------------------ 九宫格 → 元素列表

def split_sheet(sheet: Image.Image, cells_meta: list[dict], sheet_index: int = 0,
                gutter: float = 0.03, min_area: float = 0.004) -> list[Element]:
    """把一张 3×3 图拆成 Element 列表；失败的格子跳过并打印原因。"""
    W, H = sheet.size
    cw, ch = W // 3, H // 3
    out: list[Element] = []
    for i in range(9):
        row, col = divmod(i, 3)
        cell = sheet.crop((col * cw, row * ch, (col + 1) * cw, (row + 1) * ch))
        got = extract_cell(cell, gutter=gutter)
        meta = cells_meta[i] if i < len(cells_meta) else {}
        raw_el = meta.get("element")
        if isinstance(raw_el, dict):          # assign_cells() 输出的原始结构
            name = raw_el.get("name") or f"cell-{i + 1}"
            eidx = int(meta.get("element_index", 0) or 0)
        else:                                  # sheet_prompt() 输出的扁平结构
            name = raw_el or f"cell-{i + 1}"
            eidx = int(meta.get("element_index", 0) or 0)
        if got is None:
            print(f"  [sheet{sheet_index}] cell {i + 1} ({name}) 提取失败：几乎空白")
            continue
        img, _mask, coverage = got
        if coverage < min_area:
            print(f"  [sheet{sheet_index}] cell {i + 1} ({name}) 覆盖过小 {coverage:.3%}，丢弃")
            continue
        if coverage > 0.96:
            print(f"  [sheet{sheet_index}] cell {i + 1} ({name}) 覆盖过大 {coverage:.1%}（背景没去干净？），丢弃")
            continue
        img = normalize_element(img)
        el = Element(
            id=f"s{sheet_index}c{i + 1}",
            name=str(name),
            element_index=eidx,
            rel_size=float((raw_el or {}).get("rel_size", 1.0)) if isinstance(raw_el, dict) else float(meta.get("rel_size", 1.0) or 1.0),
            weight=int((raw_el or {}).get("weight", 1)) if isinstance(raw_el, dict) else int(meta.get("weight", 1) or 1),
            layer=int((raw_el or {}).get("layer", -1)) if isinstance(raw_el, dict) else int(meta.get("layer", -1) or -1),
            variant=int(meta.get("variant", 0) or 0),
            image=img,
            mask=make_mask(img),
            coverage=coverage,
            aspect=img.width / max(1, img.height),
            source=f"sheet{sheet_index}:cell{i + 1}",
        )
        if not quality_ok(img, coverage):
            continue
        out.append(el)
    return out


def load_elements_dir(path: Path) -> list[Element]:
    """外部素材模式：直接读透明底 PNG；若同级有 elements.json，沿用 rel_size / weight。"""
    import json as _json

    meta: dict[str, dict] = {}
    for cand in (Path(path).parent / "elements.json", Path(path) / "elements.json"):
        if cand.exists():
            try:
                for r in _json.loads(cand.read_text(encoding="utf-8")).get("elements", []):
                    meta[Path(r.get("file", "")).name] = r
            except Exception:  # noqa: BLE001
                pass
            break
    out: list[Element] = []
    for p in sorted(Path(path).glob("*.png")):
        img = Image.open(p).convert("RGBA")
        a = np.asarray(img)[..., 3]
        if (a > ALPHA_SOLID).sum() == 0:
            print(f"  [dir] {p.name} 全透明，跳过")
            continue
        ys, xs = np.where(a > ALPHA_SOLID)
        img = img.crop((xs.min(), ys.min(), xs.max() + 1, ys.max() + 1))
        img = normalize_element(img)
        cov = float((np.asarray(img)[..., 3] > ALPHA_SOLID).sum()) / (img.width * img.height)
        name = re.sub(r"^\d+[-_.]", "", p.stem)
        rec = meta.get(p.name, {})
        out.append(Element(id=p.stem, name=rec.get("name", name), element_index=0, variant=0, image=img,
                           mask=make_mask(img), coverage=cov, aspect=img.width / max(1, img.height),
                           rel_size=float(rec.get("rel_size", 1.0) or 1.0),
                           weight=int(rec.get("weight", 1) or 1),
                           layer=int(rec.get("layer", -1)),
                           source=f"file:{p.name}"))
    return out


def save_elements(elements: list[Element], outdir: Path) -> list[dict]:
    outdir.mkdir(parents=True, exist_ok=True)
    records = []
    for el in elements:
        fn = f"{el.id}.png"
        el.save(outdir / fn)
        records.append({"id": el.id, "name": el.name, "element_index": el.element_index,
                        "variant": el.variant, "file": str(outdir / fn),
                        "coverage": round(el.coverage, 4), "aspect": round(el.aspect, 3),
                        "size": list(el.image.size), "source": el.source})
    return records
