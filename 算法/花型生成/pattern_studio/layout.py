"""
pattern_studio.layout —— 第 5 层：排版求解（这个算法的核心）

目标
----
把 N 个元素摆进一张 W×H 的 tile，使得：
  1. 任意两个元素**不重叠**（含"留白间距 gap"）；
  2. 视觉密度可控（覆盖率 / 元素数量）；
  3. 允许元素跨越 tile 边界（跨界部分由渲染层的 3×3 卫星接回对边）；
  4. 排布有"设计感"，不是随机撒点 —— 用预设骨架（网格/半错位/砖排/对角/环形/条纹/黄金角/散点）；
  5. 同一套元素能产出多版不同排法（seed + 预设 + 尺寸/角度分布不同）。

核心数据结构：**环面占据图**（torus occupancy）
---------------------------------------------
因为要无缝，元素的"位置"其实是在一个环面（torus）上：x 和 y 都是 mod W / mod H 的。
所以碰撞检测、最小间距、泊松采样全部必须用**环面距离**，否则边界处会重叠或被误判。

尺寸从密度反推（而不是拍脑袋）
------------------------------
给定目标覆盖率 coverage 与目标个数 N，元素平均边长 side 由面积守恒反推：

    N * (side² * fill) = W * H * coverage        （fill ≈ 元素掩膜在其 bbox 内的填充率）
    side = sqrt(W*H*coverage / (N*fill))

于是"密度"这一个旋钮同时改变**数量**与**单元素大小**，两者自动协调，
不会出现"高密度但只有 3 朵大花"或者"低密度但 80 朵芝麻"。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

COLLISION_RES = 480          # 碰撞检测网格边长（与 tile 尺寸无关，保证速度）


# ------------------------------------------------------------------ 环面工具

def _blit_wrap(canvas: np.ndarray, patch: np.ndarray, x0: int, y0: int, op: str = "or") -> None:
    """把 patch 以 (x0,y0) 为左上角贴到 canvas 上，越界部分从对边绕回（环面）。"""
    G = canvas.shape[0]
    h, w = patch.shape
    for dx in (-G, 0, G):
        for dy in (-G, 0, G):
            px, py = x0 + dx, y0 + dy
            sx0, sy0 = max(0, -px), max(0, -py)
            sx1, sy1 = min(w, G - px), min(h, G - py)
            if sx1 <= sx0 or sy1 <= sy0:
                continue
            sub = patch[sy0:sy1, sx0:sx1]
            target = canvas[py + sy0 : py + sy1, px + sx0 : px + sx1]
            if op == "or":
                target |= sub
            elif op == "clear":
                target &= ~sub
            else:  # "and"
                target &= sub


def _dilate_wrap(canvas: np.ndarray, r: int = 1) -> np.ndarray:
    """环面膨胀 r 像素（8 邻域），用于实现"元素之间至少留 gap"。"""
    out = canvas.copy()
    for _ in range(r):
        acc = out.copy()
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dx or dy:
                    acc |= np.roll(np.roll(out, dy, axis=0), dx, axis=1)
        out = acc
    return out


def torus_dist2(ax, ay, bx, by, w, h):
    dx = abs(ax - bx); dx = min(dx, w - dx)
    dy = abs(ay - by); dy = min(dy, h - dy)
    return dx * dx + dy * dy


# ------------------------------------------------------------------ 元素 → 掩膜

def render_mask(mask: np.ndarray, size_px: tuple[int, int], angle: float, flip: bool,
                grid: int, tile: tuple[float, float]) -> np.ndarray:
    """把元素掩膜按目标尺寸/旋转/镜像渲染到碰撞网格上，返回 bool 阵列。"""
    w_px, h_px = size_px
    tw, th = tile
    gw = max(1, int(round(w_px / tw * grid)))
    gh = max(1, int(round(h_px / th * grid)))
    m = Image.fromarray((mask.astype(np.uint8) * 255), "L").resize((gw, gh), Image.BILINEAR)
    if flip:
        m = m.transpose(Image.FLIP_LEFT_RIGHT)
    if abs(angle) > 0.01:
        m = m.rotate(angle, expand=True, resample=Image.BILINEAR)
    arr = np.asarray(m) > 96
    return arr


# ------------------------------------------------------------------ 预设骨架

def _phyllotaxis(n: int, w: float, h: float, jitter: float, rng: random.Random):
    """黄金角螺旋：面积均匀分布，天然有秩序感（花型排版的常青树）。"""
    pts = []
    golden = math.pi * (3 - math.sqrt(5))
    for i in range(n):
        t = (i + 0.5) / n
        r = math.sqrt(t) * 0.5 * min(w, h)
        a = i * golden
        pts.append(((w / 2 + math.cos(a) * r * (w / h) ** 0.5 + rng.uniform(-jitter, jitter) * w) % w,
                    (h / 2 + math.sin(a) * r * (h / w) ** 0.5 + rng.uniform(-jitter, jitter) * h) % h))
    return pts


def preset_positions(name: str, n: int, w: float, h: float, rng: random.Random,
                     jitter: float = 0.18, r_min: float | None = None) -> list[tuple[float, float]]:
    """按预设生成 n 个候选中心点（单位=tile 像素，已 mod 到环面内）。"""
    j = lambda: rng.uniform(-jitter, jitter)
    if name == "grid":
        cols = max(1, int(round(math.sqrt(n * w / h))))
        rows = max(1, math.ceil(n / cols))
        return [((c + 0.5) * w / cols + j() * w / cols, (r + 0.5) * h / rows + j() * h / rows)
                for r in range(rows) for c in range(cols)][:n]
    if name == "halfdrop":
        cols = max(1, int(round(math.sqrt(n * w / h))))
        rows = max(1, math.ceil(n / cols))
        out = []
        for r in range(rows):
            for c in range(cols):
                off = 0.5 * w / cols if r % 2 else 0.0
                out.append(((c + 0.5) * w / cols + off + j() * w / cols,
                            (r + 0.5) * h / rows + j() * h / rows))
        return out[:n]
    if name == "brick":
        cols = max(1, int(round(math.sqrt(n * w / h))))
        rows = max(1, math.ceil(n / cols))
        out = []
        for r in range(rows):
            for c in range(cols):
                off = (0.5 if r % 2 else 0.0) * w / cols
                out.append(((c + 0.5) * w / cols + off + j() * 0.10 * w / cols,
                            (r + 0.5) * h / rows))
        return out[:n]
    if name == "diagonal":
        cols = max(1, int(round(math.sqrt(n * w / h))))
        rows = max(1, math.ceil(n / cols))
        out = []
        for r in range(rows):
            for c in range(cols):
                out.append((((c + 0.5) / cols + 0.22 * r / max(1, rows)) % 1.0 * w,
                            (r + 0.5) / rows * h))
        return out[:n]
    if name == "radial":
        rings = max(2, int(round(math.sqrt(n / 3))))
        out = []
        placed = 0
        for ring in range(rings):
            rad = (ring + 1) / (rings + 1) * 0.5 * min(w, h)
            count = max(3, int(round(2 * math.pi * rad / (min(w, h) * 0.28))))
            for k in range(count):
                a = 2 * math.pi * k / count + ring * 0.5
                out.append(((w / 2 + math.cos(a) * rad) % w, (h / 2 + math.sin(a) * rad) % h))
                placed += 1
                if placed >= n:
                    return out
        return out
    if name == "stripes":
        cols = max(2, int(round(math.sqrt(n))))
        out = []
        per = max(1, math.ceil(n / cols))
        for c in range(cols):
            for k in range(per):
                out.append(((c + 0.5) / cols * w + j() * 0.04 * w, (k + 0.5) / per * h + j() * 0.3 * h / per))
        return out[:n]
    if name == "phyllotaxis":
        return _phyllotaxis(n, w, h, jitter, rng)
    # scatter / poisson（r_min 由元素尺寸给出，否则按数量反推）
    return poisson_torus(n, w, h, rng, r_min=r_min)


def poisson_torus(n: int, w: float, h: float, rng: random.Random,
                  r_min: float | None = None) -> list[tuple[float, float]]:
    """环面泊松盘采样（Bridson 变体）：点是"蓝噪声"，天然均匀且不聚堆。"""
    area = w * h
    r = r_min if r_min else math.sqrt(area / max(1, n) / math.pi) * 1.02
    cell = r / math.sqrt(2)
    gw, gh = max(1, int(w / cell)), max(1, int(h / cell))
    grid = [[None] * gh for _ in range(gw)]
    pts: list[tuple[float, float]] = []
    active: list[tuple[float, float]] = []

    def add(p):
        gx = int(p[0] / w * gw) % gw
        gy = int(p[1] / h * gh) % gh
        grid[gx][gy] = p
        pts.append(p)
        active.append(p)

    def ok(p):
        gx = int(p[0] / w * gw) % gw
        gy = int(p[1] / h * gh) % gh
        for i in range(-2, 3):
            for jx in range(-2, 3):
                q = grid[(gx + i) % gw][(gy + jx) % gh]
                if q is not None and torus_dist2(p[0], p[1], q[0], q[1], w, h) < r * r:
                    return False
        return True

    add((rng.uniform(0, w), rng.uniform(0, h)))
    while active and len(pts) < n * 3:
        idx = rng.randrange(len(active))
        base = active[idx]
        found = False
        for _ in range(24):
            a = rng.uniform(0, 2 * math.pi)
            rad = rng.uniform(r, 2 * r)
            p = ((base[0] + math.cos(a) * rad) % w, (base[1] + math.sin(a) * rad) % h)
            if ok(p):
                add(p)
                found = True
                break
        if not found:
            active.pop(idx)
        if len(pts) >= n:
            break
    rng.shuffle(pts)
    return pts


PRESETS = ["grid", "halfdrop", "brick", "diagonal", "radial", "stripes", "phyllotaxis", "scatter"]


# ------------------------------------------------------------------ 求解结果

@dataclass
class Placement:
    element_id: str
    name: str
    cx: float
    cy: float
    angle: float
    flip: bool
    size_px: tuple[int, int]
    mask_res: np.ndarray = field(repr=False, default=None)
    area_frac: float = 0.0


# ------------------------------------------------------------------ 求解器

def block_coverage(occ: np.ndarray, k: int = 10) -> np.ndarray:
    """把占据图切成 k×k（环面）块，返回每块覆盖率。"""
    bs = max(1, occ.shape[0] // k)
    out = np.zeros((k, k), float)
    for by in range(k):
        for bx in range(k):
            out[by, bx] = occ[by * bs : (by + 1) * bs, bx * bs : (bx + 1) * bs].mean()
    return out


def evenness_stats(occ: np.ndarray, k: int = 10) -> dict:
    """均匀度：块覆盖率的变异系数 CV 与最差比值（越小越均匀）。"""
    bc = block_coverage(occ, k)
    mean = float(bc.mean())
    sd = float(bc.std())
    nz = bc[bc > 0]
    return {
        "cv": round(sd / mean, 4) if mean > 1e-9 else 0.0,
        "min_cov": round(float(bc.min()), 4),
        "max_cov": round(float(bc.max()), 4),
        "empty_blocks": int((bc < mean * 0.15).sum()),
        "nonempty_mean": round(float(nz.mean()), 4) if nz.size else 0.0,
    }


def _nearest_neighbour(positions: list[tuple[float, float]], W: float, H: float) -> list[float]:
    n = len(positions)
    out = [float("inf")] * n
    for i in range(n):
        ax, ay = positions[i]
        for j in range(n):
            if i == j:
                continue
            bx, by = positions[j]
            d = math.sqrt(torus_dist2(ax, ay, bx, by, W, H))
            if d < out[i]:
                out[i] = d
    return out


def spacing_stats(placements: list, tile: tuple[int, int]) -> dict:
    """间距均匀度：最近邻中心距的变异系数（越接近 0，元素之间越"等距"）。

    比"块覆盖率 CV"更能反映"排得平均不平均"——低密度时块 CV 天然偏高，
    但一个理想的蓝噪声/格排仍然有很小的间距 CV。
    """
    W, H = tile
    pts = [(p.cx, p.cy) for p in placements]
    if len(pts) < 3:
        return {"nn_cv": 0.0, "nn_min": 0.0, "nn_mean": 0.0}
    ds = []
    for i, (ax, ay) in enumerate(pts):
        best = float("inf")
        for j, (bx, by) in enumerate(pts):
            if i == j:
                continue
            d = math.sqrt(torus_dist2(ax, ay, bx, by, W, H))
            if d < best:
                best = d
        ds.append(best)
    m = float(np.mean(ds))
    return {
        "nn_cv": round(float(np.std(ds)) / m, 4) if m > 1e-9 else 0.0,
        "nn_min": round(float(np.min(ds)), 1),
        "nn_mean": round(m, 1),
    }


def relax_torus(points: list[tuple[float, float]], W: float, H: float,
                iters: int = 40, step: float = 0.55) -> list[tuple[float, float]]:
    """环面上的斥力松弛（简化 Lloyd）：让点尽量等距，消除聚堆与空洞。

    每次迭代把每个点朝"远离其它点"的方向推一点：F_i = Σ (p_i-p_j)/|p_i-p_j|²，
    再按最近邻距离归一化步长。点少时非常快（N≤80 → 80² 次距离）。
    """
    pts = [list(p) for p in points]
    n = len(pts)
    if n < 3:
        return [tuple(p) for p in pts]
    for _ in range(iters):
        disp = [[0.0, 0.0] for _ in range(n)]
        for i in range(n):
            for j in range(i + 1, n):
                dx = pts[i][0] - pts[j][0]
                dy = pts[i][1] - pts[j][1]
                if dx > W / 2: dx -= W
                if dx < -W / 2: dx += W
                if dy > H / 2: dy -= H
                if dy < -H / 2: dy += H
                d2 = dx * dx + dy * dy
                if d2 < 1e-6:
                    # 完全重合：随机推开
                    ang = 1.0 + i * 2.399
                    dx, dy, d2 = math.cos(ang), math.sin(ang), 1.0
                f = 1.0 / d2
                disp[i][0] += dx * f; disp[i][1] += dy * f
                disp[j][0] -= dx * f; disp[j][1] -= dy * f
        # 步长按"平均最近邻距离"缩放，避免震荡
        nn = _nearest_neighbour([tuple(p) for p in pts], W, H)
        scale = float(np.mean([d for d in nn if d < float("inf")] or [1.0])) * step * 0.5
        for i in range(n):
            mag = math.hypot(disp[i][0], disp[i][1])
            if mag < 1e-9:
                continue
            pts[i][0] = (pts[i][0] + disp[i][0] / mag * min(scale, mag * 0.35)) % W
            pts[i][1] = (pts[i][1] + disp[i][1] / mag * min(scale, mag * 0.35)) % H
    return [(float(x), float(y)) for x, y in pts]


def _farthest_point_order(points: list[tuple[float, float]], W: float, H: float,
                          rng: random.Random) -> list[int]:
    """最远点遍历：让"大元素"在空间上尽量分散，而不是挤在一侧。"""
    n = len(points)
    if n <= 2:
        return list(range(n))
    order = [rng.randrange(n)]
    seen = {order[0]}
    mind = [torus_dist2(points[i][0], points[i][1], points[0][0], points[0][1], W, H) for i in range(n)]
    for _ in range(n - 1):
        idx = max((i for i in range(n) if i not in seen), key=lambda i: mind[i])
        order.append(idx)
        seen.add(idx)
        px, py = points[idx]
        for i in range(n):
            d = torus_dist2(points[i][0], points[i][1], px, py, W, H)
            if d < mind[i]:
                mind[i] = d
    return order


def _lattice_positions(preset: str, cols: int, rows: int, W: float, H: float,
                       jitter: float, rng: random.Random) -> list[tuple[float, float]]:
    """规则骨架：网格/半错位/砖排/对角/条纹，按格位给点（环面内）。"""
    cw, ch = W / cols, H / rows
    pts = []
    for r in range(rows):
        for c in range(cols):
            x, y = (c + 0.5) * cw, (r + 0.5) * ch
            if preset == "halfdrop" and r % 2 == 1:
                x += cw * 0.5
            elif preset == "brick" and r % 2 == 1:
                x += cw * 0.5
            elif preset == "diagonal":
                x += cw * (r / max(1, rows))
            elif preset == "stripes":
                x = cw * (c + 0.5)
            x = (x + rng.uniform(-jitter, jitter) * cw) % W
            y = (y + rng.uniform(-jitter, jitter) * ch) % H
            pts.append((x, y))
    return pts


def element_layer(el) -> int:
    """元素的叠层：0=大主花（最底）、1=中花、2=小点缀/墨线叶（最上）。"""
    l = getattr(el, "layer", -1)
    if l is not None and int(l) >= 0:
        return int(l)
    rel = float(getattr(el, "rel_size", 1.0) or 1.0)
    return 0 if rel >= 1.4 else (1 if rel >= 0.75 else 2)


LAYER_ALLOW = {0: 0.0, 1: 0.28, 2: 0.62}


def solve(elements: list, tile: tuple[int, int], preset: str, density: float,
          rng: random.Random, gap_ratio: float = 0.12, count: int | None = None,
          angle_range: float = 0.0, mirror_prob: float = 0.0,
          scale_jitter: tuple[float, float] = (0.88, 1.12),
          grid: int = COLLISION_RES, max_attempts: int = 40,
          evenness: float = 0.8, overlap_mode: str = "none",
          overlap_allow: dict | None = None, element_scale: float = 1.0) -> dict:
    """把元素摆进 tile（尽量均匀），返回 {placements, coverage, evenness, ...}。

    均匀性来自三件事：
      1. **位置先均匀**：规则骨架按格位给点；不规则用环面泊松盘（蓝噪声）。
      2. **尺寸服从间距**：每个位置的元素直径上限由"到最近邻的距离"决定，
         大元素自动落在空旷处，小元素填缝。
      3. **事后均衡**：把过密块里的元素搬到最空的块，再补空块，直到块覆盖率的
         变异系数不再下降（CV 是均匀度的量化指标）。
    """
    W, H = float(tile[0]), float(tile[1])
    area = W * H
    d01 = max(0.0, min(1.0, density))
    even = max(0.0, min(1.0, evenness))

    # 密度 → (覆盖率, 数量)
    target_cov = 0.06 + 0.32 * d01
    n_target = count or int(round(10 + 50 * d01))

    fill = 0.55
    side = math.sqrt(area * target_cov / (max(1, n_target) * fill))
    side = max(0.045 * min(W, H), min(0.40 * min(W, H), side)) * max(0.3, float(element_scale))

    rels = [float(getattr(e, "rel_size", 1.0) or 1.0) for e in elements]
    mean_rel = max(1e-3, float(np.mean(rels)))
    rel_norm = [r / mean_rel for r in rels]
    weights = [max(1, int(getattr(e, "weight", 1) or 1)) for e in elements]

    # 实例表：按权重分配数量（至少 1 个），并按"有效尺寸"降序
    total_w = sum(weights)
    quota = [max(1, int(round(w / total_w * n_target))) for w in weights]
    while sum(quota) > n_target and max(quota) > 1:
        quota[quota.index(max(quota))] -= 1
    while sum(quota) < n_target:
        quota[int(np.argmax(weights))] += 1
    instances = []
    for ei, q in enumerate(quota):
        for _ in range(q):
            instances.append(ei)
    rng.shuffle(instances)
    if overlap_mode == "layered":
        # 叠层模式：先摆底层大花，再叠中层，最后叠小元素（渲染顺序=摆放顺序）
        instances.sort(key=lambda ei: (element_layer(elements[ei]), -rel_norm[ei]))
    else:
        instances.sort(key=lambda ei: -rel_norm[ei])

    # ---- 1. 位置骨架（每次尝试都重新生成，便于按覆盖率校准尺寸）
    regular = preset in ("grid", "halfdrop", "brick", "diagonal", "stripes", "radial")
    jitter = (1.0 - even) * 0.22

    def build_positions(n_use: int) -> tuple[list[tuple[float, float]], list[float]]:
        n_use = max(1, int(n_use))
        if preset == "radial":
            pos = preset_positions("radial", n_use, W, H, rng, jitter=jitter * 0.5)
        elif regular:
            cols = max(1, int(round(math.sqrt(n_use * W / H))))
            rows = max(1, int(math.ceil(n_use / cols)))
            pos = _lattice_positions(preset, cols, rows, W, H, jitter, rng)
        elif preset == "phyllotaxis":
            pos = preset_positions("phyllotaxis", n_use, W, H, rng, jitter=jitter * 0.6)
        else:  # scatter：蓝噪声
            r_min = math.sqrt(area / max(1, n_use) / math.pi) * (1.0 + 0.10 * even)
            pos = poisson_torus(int(n_use * 1.6), W, H, rng, r_min=r_min)[:n_use]
        if not pos:
            pos = [(rng.uniform(0, W), rng.uniform(0, H)) for _ in range(n_use)]
        return pos, _nearest_neighbour(pos, W, H)

    def attempt(side_v: float) -> dict:
        base_idx = [ei for ei in instances if element_layer(elements[ei]) < 2] if overlap_mode == "layered" else list(instances)
        over_idx = [ei for ei in instances if element_layer(elements[ei]) >= 2] if overlap_mode == "layered" else []
        positions, nn = build_positions(len(base_idx))
        if not regular and even > 0.2:
            positions = relax_torus(positions, W, H, iters=int(20 + 30 * even))
            nn = _nearest_neighbour(positions, W, H)
        slot_cap = [min(0.95 * (d if d < float("inf") else min(W, H)), 0.42 * min(W, H)) for d in nn]
        # 大元素 → 最空旷的格位（按可用直径降序配对），保证主花分散且不被压小
        slot_order = sorted(range(len(positions)), key=lambda i: -slot_cap[i])
        order = slot_order
        occ = np.zeros((grid, grid), bool)
        placements_local: list[Placement] = []
        zero = [0.0]

        def try_place(ei: int, cx: float, cy: float, cap: float, shrink: float = 1.0):
            el = elements[ei]
            d = min(side_v * rel_norm[ei] * rng.uniform(*scale_jitter), cap) * shrink
            if d <= 1:
                return None
            sw = min(d * math.sqrt(max(0.2, el.aspect)), 0.95 * W)
            sh = min(d / math.sqrt(max(0.2, el.aspect)), 0.95 * H)
            ang = rng.uniform(-angle_range, angle_range) if angle_range else 0.0
            flip = rng.random() < mirror_prob
            gap_px = max(1, int(round(sw * gap_ratio / W * grid)))
            lay = element_layer(el) if overlap_mode == "layered" else 0
            allow = 0.0 if overlap_mode != "layered" else (overlap_allow or LAYER_ALLOW).get(lay, 0.0)
            for _try in range(4):
                patch = render_mask(el.mask, (sw, sh), ang, flip, grid, (W, H))
                if patch.sum() < 4:
                    return None
                ph, pw = patch.shape
                x0 = int(round(cx / W * grid - pw / 2)); y0 = int(round(cy / H * grid - ph / 2))
                probe = np.zeros((grid, grid), bool)
                _blit_wrap(probe, patch, x0, y0, "or")
                ok = False
                if allow <= 0:
                    ok = not np.any(probe & _dilate_wrap(occ, gap_px))
                else:
                    inter = float((probe & occ).sum()) / max(1.0, float(probe.sum()))
                    ok = inter <= allow
                if ok:
                    _blit_wrap(occ, patch, x0, y0, "or")
                    p = Placement(element_id=el.id, name=el.name, cx=round(cx, 3), cy=round(cy, 3),
                                  angle=round(ang, 2), flip=flip, size_px=(int(round(sw)), int(round(sh))),
                                  mask_res=patch, area_frac=float(patch.sum()) / (grid * grid))
                    placements_local.append(p)
                    zero[0] += p.area_frac
                    return p
                sw *= 0.9; sh *= 0.9
            return None

        # 按"最远点顺序"分配大元素，保证主花分散
        for k, si in enumerate(order[: len(base_idx)]):
            try_place(base_idx[k], positions[si][0], positions[si][1], slot_cap[si])

        # 叠层模式的第二段：小元素/墨线叶贴着已有元素放（这才是"手绘层叠"的样子）
        overlay_count: dict[int, int] = {}
        for ei in over_idx:
            placed = False
            base = None
            for _t in range(26):
                cands = [q for q in placements_local if overlay_count.get(id(q), 0) < 2] or placements_local
                if cands and rng.random() < 0.72:
                    base = rng.choices(cands, weights=[max(1e-6, q.area_frac) for q in cands])[0]
                    rad = max(8.0, base.size_px[0] / 2.0)
                    dst = rng.uniform(0.30, 0.95) * rad
                    a = rng.uniform(0, 2 * math.pi)
                    cx = (base.cx + math.cos(a) * dst) % W
                    cy = (base.cy + math.sin(a) * dst) % H
                else:
                    cx, cy = rng.uniform(0, W), rng.uniform(0, H)
                got = try_place(ei, cx, cy, min(0.34 * min(W, H), side_v * 1.3))
                if got is not None:
                    if base is not None:
                        overlay_count[id(base)] = overlay_count.get(id(base), 0) + 1
                    placed = True
                    break

        # 补位：填最空的块
        if zero[0] < target_cov:
            K = max(3, min(8, int(round(min(W, H) / (side_v * 2.4)))))
            tries = 0
            while zero[0] < target_cov and tries < n_target * 12:
                tries += 1
                bc = block_coverage(occ, K)
                order_blocks = np.dstack(np.unravel_index(np.argsort(bc.ravel()), bc.shape))[0]
                added = False
                for by, bx in order_blocks[:3]:
                    for _t in range(6):
                        cx = (bx + rng.random()) / K * W
                        cy = (by + rng.random()) / K * H
                        cap = min(0.34 * min(W, H), side_v * 1.15)
                        pool = ([i for i in range(len(elements)) if element_layer(elements[i]) >= 2]
                                if overlap_mode == "layered" else list(range(len(elements))))
                        if not pool:
                            pool = list(range(len(elements)))
                        ei = rng.choices(pool, weights=[weights[i] for i in pool])[0]
                        if try_place(ei, cx, cy, cap, shrink=rng.uniform(0.8, 1.0)) is not None:
                            added = True
                            break
                    if added:
                        break
                if not added:
                    break
        return {"occ": occ, "placements": placements_local, "coverage": float(occ.sum()) / (grid * grid),
                "side": side_v}

    # ---- 2. 覆盖率校准：尺寸大一点/小一点，让实际覆盖率逼近目标（最多 3 次）
    best = None
    for _it in range(3):
        cand = attempt(side)
        if best is None or abs(cand["coverage"] - target_cov) < abs(best["coverage"] - target_cov):
            best = cand
        ratio = target_cov / max(1e-6, cand["coverage"])
        if 0.92 <= ratio <= 1.08:
            break
        side = max(0.03 * min(W, H), min(0.45 * min(W, H), side * max(0.7, min(1.4, math.sqrt(ratio)))))
    occ = best["occ"]
    placements = best["placements"]

    # ---- 4. 均衡：把"过密块"里的元素搬到"最空块"，只在块极差变小时接受
    if even > 0.15 and placements:
        K = max(3, min(8, int(round(min(W, H) / (side * 2.2)))))
        for _round in range(3):
            bc = block_coverage(occ, K)
            spread = float(bc.max() - bc.min())
            improved = False
            # 先搬小元素（挪动代价低、对视觉影响小），再搬中等元素
            movable = [q for q in placements
                       if element_layer(elements[[e.id for e in elements].index(q.element_id)]) < 2]
            for p in sorted(movable, key=lambda q: q.area_frac):
                by, bx = min(K - 1, int(p.cy / H * K)), min(K - 1, int(p.cx / W * K))
                if bc[by, bx] < float(bc.mean()) * 1.30:
                    continue
                tmp = erase_placement(occ, p, grid, (W, H))
                bc2 = block_coverage(tmp, K)
                for _ in range(12):
                    ty, tx = np.unravel_index(int(np.argmin(bc2)), bc2.shape)
                    cx = (tx + rng.random()) / K * W
                    cy = (ty + rng.random()) / K * H
                    if not fits_at(tmp, p, cx, cy, grid, (W, H)):
                        bc2[ty, tx] = 1.0          # 该块试不通，换下一空块
                        continue
                    stamped = tmp.copy()
                    stamp_placement(stamped, p, cx, cy, grid, (W, H))
                    if float(block_coverage(stamped, K).max() - block_coverage(stamped, K).min()) < spread * 0.997:
                        occ = stamped
                        p.cx, p.cy = round(cx, 3), round(cy, 3)
                        improved = True
                    break
                if improved:
                    break
            if not improved:
                break

    coverage = float(occ.sum()) / (grid * grid)
    K_STAT = max(3, min(8, int(round(min(W, H) / (side * 2.2)))))
    stats = evenness_stats(occ, K_STAT)
    stat_pl = ([q for q in placements
                if element_layer(elements[[e.id for e in elements].index(q.element_id)]) < 2]
               if overlap_mode == "layered" else placements)
    stats.update(spacing_stats(stat_pl or placements, (W, H)))
    return {
        "preset": preset,
        "density": density,
        "coverage_target": round(target_cov, 4),
        "density_target": density,
        "coverage": coverage,
        "evenness": stats,
        "n": len(placements),
        "side": side,
        "gap_ratio": gap_ratio,
        "angle_range": angle_range,
        "mirror_prob": mirror_prob,
        "placements": placements,
        "occupied": occ,
    }


def placement_box(p, grid: int, tile: tuple[int, int]):
    """placement 的足迹在碰撞网格上的左上角（可用于擦除/试探）。"""
    W, H = tile
    patch = p.mask_res
    ph, pw = patch.shape
    return int(round(p.cx / W * grid - pw / 2)), int(round(p.cy / H * grid - ph / 2)), ph, pw


def erase_placement(occ: np.ndarray, p, grid: int, tile: tuple[int, int]) -> np.ndarray:
    """把 placement 的足迹从占据图上抹掉（返回新图，便于"先抹后试"）。"""
    out = occ.copy()
    x0, y0, _, _ = placement_box(p, grid, tile)
    _blit_wrap(out, p.mask_res, x0, y0, "clear")
    return out


def fits_at(occ: np.ndarray, p, cx: float, cy: float, grid: int, tile: tuple[int, int]) -> bool:
    W, H = tile
    ph, pw = p.mask_res.shape
    x0 = int(round(cx / W * grid - pw / 2)); y0 = int(round(cy / H * grid - ph / 2))
    probe = np.zeros((grid, grid), bool)
    _blit_wrap(probe, p.mask_res, x0, y0, "or")
    return not bool(np.any(probe & occ))


def stamp_placement(occ: np.ndarray, p, cx: float, cy: float, grid: int, tile: tuple[int, int]) -> None:
    W, H = tile
    ph, pw = p.mask_res.shape
    x0 = int(round(cx / W * grid - pw / 2)); y0 = int(round(cy / H * grid - ph / 2))
    _blit_wrap(occ, p.mask_res, x0, y0, "or")


def summarize(result: dict) -> dict:
    return {k: v for k, v in result.items() if k not in ("placements", "occupied")}
