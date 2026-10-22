"""近似去重：同一张图的变体、同一套素材包，只留一张。

问题长什么样
    用户搜"浪漫 3d 花卉 ai 图"，回来 9 张**同一个素材系列**的纸雕花卉 ——
    同一位作者、同一套底纹语言，只是裁切位置和底色不同。还有更常见的：
    同一张图在 5 个站转载、图床给 3 种尺寸、加了水印或裁掉一条边。
    用户看到的观感是"你在拿同一张图糊弄我"。

为什么 rank.py 的 aHash 不够
    它把图缩到 8×8 取均值、比 64 位指纹，阈值 6 位。这套只能抓
    "同尺寸、同裁切、轻微压缩"。**换尺寸或裁掉一条边就能翻转 10~20 位** ——
    实测 out_watercolor2 里 37 对肉眼完全一样的图，aHash 全放行了。
    8×8 太粗：整张图的"观感"（底色、明暗分布、纹理密度、色相构成）
    根本不在那 64 位里。

一把尺子：**风格签名**（sig）
    HSV 直方图 8×4×4 + 灰度 16 档 + 边缘密度，归一化后算余弦。
    在真实产物上量出来的分布：

        同一张图（换尺寸/水印/裁边）   0.995 ~ 1.000
        白底水彩但设计不同             0.985 ~ 0.993   ← 不能砍
        不同风格                       0.40 ~ 0.90

    中间那档是关键：0.985 会误杀（实测把"仙鹤粉花"和"粉紫牡丹"判成
    一对），0.995 干净。所以默认 NEAR = 0.995。

    想过、也试过再加一条"同来源包"（图片 URL 的目录段一样，比如
    `/2019/05/16/EPS-190516_33/`，说明是同一套素材被几个站转载）：
    理论上它能在相似度低一些时也合并。实测两种阈值都误杀 ——
    0.90 会把"红樱桃小花 / 粉牡丹大花"（0.984）并掉，0.97 仍会并掉
    "两种粉牡丹"（0.993）。同站的图配色构图本来就接近，光看直方图
    分不开。所以这条路整条删了，只留 0.995 一条线 ——
    **宁可漏（带大水印、被重裁切的那种认不出来），也不误杀**。

保留谁
    一组里留 `tile.quality` 最高的那张（同分比像素总数）——
    用户是拿它去铺面料的，接缝好看比什么都重要。
"""
from __future__ import annotations

import math
import os

from PIL import Image, ImageFilter

# 风格签名余弦阈值。0.995 是实测划出来的线，别轻易往下调：
# 0.985 就会把"同为白底水彩、但设计完全不同"的图并成一张。
#
# **试过、放弃的两条降阈值思路**（都实测误杀过，别再走一遍）：
#   ① 同来源包 + 0.90：同一素材站的图配色构图接近但设计不同
#      （红樱桃小花 / 粉牡丹大花 判成 0.984），一批全被并掉。
#   ② 同来源包 + 0.97：好一些，但 0.993 那对"两种粉牡丹"仍是不同设计。
# 宁可漏（带大水印、被重裁切的转载图认不出来），也不误杀 ——
# 少给几张是遗憾，把好图删掉是事故。
NEAR = 0.995

_SIZE = 48          # 签名用的采样边长
_EDGE_W = 3.0       # 边缘密度在向量里的权重（它本身是个 0~1 的标量）


def _flat(im):
    """Pillow 12 起 getdata 弃用、给的是 get_flattened_data，两个都认。"""
    f = getattr(im, "get_flattened_data", None)
    return list(f()) if callable(f) else list(im.getdata())


def sig(path):
    """回 (向量, 明暗均值, 边缘密度)。向量已归一化，点积即余弦。

    读不出来回 ((), 0, 0) —— 调用方把它当"和谁都不像"处理，
    不要因为一张坏图把整批去重搞崩。
    """
    try:
        with Image.open(path) as im:
            rgb = im.convert("RGB").resize((_SIZE, _SIZE), Image.LANCZOS)
            hsv = rgb.convert("HSV")
            g = rgb.convert("L")
            edge = g.filter(ImageFilter.FIND_EDGES)
            hpx, gpx, epx = _flat(hsv), _flat(g), _flat(edge)
    except Exception:                                        # noqa: BLE001
        return (), 0.0, 0.0

    n = float(len(hpx)) or 1.0
    hb, sb, vb = [0] * 8, [0] * 4, [0] * 4
    for a, b, c in hpx:
        hb[a * 8 // 256] += 1
        sb[b * 4 // 256] += 1
        vb[c * 4 // 256] += 1
    bright = sum(gpx) / (255.0 * n)
    ed = sum(epx) / (255.0 * n)

    gh = [0] * 16
    for v in gpx:
        gh[v * 16 // 256] += 1

    vec = ([x / n for x in hb] + [x / n for x in sb] + [x / n for x in vb]
           + [x / n * 0.7 for x in gh] + [ed * _EDGE_W])
    nrm = math.sqrt(sum(v * v for v in vec))
    if nrm <= 0:
        return (), bright, ed
    return tuple(v / nrm for v in vec), bright, ed


def _dot(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


def _quality(rec):
    q = (rec.get("tile") or {}).get("quality")
    if q is None:
        q = (rec.get("index_item") or {}).get("tile_quality")
    return float(q or 0)


def _size(rec):
    w = rec.get("w") or (rec.get("index_item") or {}).get("w") or 0
    h = rec.get("h") or (rec.get("index_item") or {}).get("h") or 0
    return int(w) * int(h)


def _label(rec, i):
    it = rec.get("index_item") or {}
    if it.get("id"):
        return it["id"]
    base = os.path.basename(rec.get("path") or "")
    return base or ("#" + str(i))


def _group(sigs, order, near):
    """贪心聚类：按 order 逐张和"已经立住的代表"比，像就归到它名下。

    **不用并查集**。并查集会传递：A~B 0.996、B~C 0.96、C~D 0.95，
    于是 A 和 D 也被算成一伙 —— 实测一批红花和粉花就这么被并成
    一条链，理由还写着"相似度 0.996"（那其实是 A~B 的）。贪心只跟
    **代表**比，链式效应就被切断了，代价是顺序有依赖（所以先排质量）。
    """
    parent = {i: i for i in order}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    reps = []                       # 每个代表：(下标, 签名)
    for i in order:
        vi, _bi, _ei = sigs[i]
        if not vi:
            continue
        hit = None
        for ri, rv in reps:
            if _dot(vi, rv) >= near:
                hit = ri
                break
        if hit is None:
            reps.append((i, vi))
        else:
            parent[i] = find(hit)
    return {i: find(i) for i in order}


def dedup(recs, anchors=(), near=NEAR, log=None):
    """把"看起来是同一张"的收成一张。

    回 (留下的, 剔掉的)。`anchors` 是**已经在库里的**记录：只当锚，
    和锚像的会被剔，锚自己不会被删（refill 补图时用，避免补进来的
    跟已有的撞车）。剔掉的记录上会写好 `screen.why` 与 `near_of`，
    直接就能进 `screen.dump_rejected()` 的留档。
    """
    log = log or (lambda *_a: None)
    recs = list(recs)
    anchors = list(anchors)
    if not recs:
        return [], []
    # near<=0 = 这一层关掉。**必须在这儿挡**：阈值 0 会让"任何两张"都
    # 过线，整批图会被并成一张 —— 命令行传 --sim 0 的意思是别管，
    # 不是"全部删掉"。
    if near <= 0:
        return recs, []

    n, m = len(recs), len(anchors)
    paths = [r.get("path") or "" for r in recs] + [a.get("path") or "" for a in anchors]
    sigs = [sig(p) if p else ((), 0.0, 0.0) for p in paths]
    # **质量高的先立住**（同分比像素总数）。anchors 排在所有人前面 ——
    # 它们已经在库里了，不能被新图顶掉位置。
    news_order = sorted(range(n), key=lambda i: (-_quality(recs[i]), -_size(recs[i])))
    order = list(range(n, n + m)) + news_order
    root = _group(sigs, order, near)

    # 按连通块聚合。含锚的块 → 新图全剔（库里已经有一张了）
    buckets = {}
    for idx in order:
        buckets.setdefault(root[idx], []).append(idx)

    kept, dropped = [], []
    for members in buckets.values():
        news = [i for i in members if i < n]
        if not news:
            continue
        old = [i for i in members if i >= n]
        if old:
            # 和已有的像：找最像的那张锚，报它的编号
            best = max(old, key=lambda i: _dot(sigs[news[0]][0], sigs[i][0]))
            sim = _dot(sigs[news[0]][0], sigs[best][0])
            for i in news:
                c = _dot(sigs[i][0], sigs[best][0])
                dropped.append((i, _label(anchors[best - n], best - n), c))
            continue
        if len(news) == 1:
            kept.append(news[0])
            continue
        # 同块里留质量最高的
        best = max(news, key=lambda i: (_quality(recs[i]), _size(recs[i])))
        kept.append(best)
        for i in news:
            if i == best:
                continue
            c = _dot(sigs[i][0], sigs[best][0])
            dropped.append((i, _label(recs[best], best), c))

    out_drop = []
    for i, near_id, c in dropped:
        r = recs[i]
        r["near_of"] = near_id
        r["sim"] = round(float(c), 4)
        r["screen"] = {"pattern": True, "match": True,
                       "why": "和 %s 几乎是同一张（相似度 %.3f），只留一张"
                              % (near_id, c)}
        out_drop.append(r)
    kept_recs = [recs[i] for i in sorted(kept)]
    if out_drop:
        log("· 相似去重：%d 张里 %d 张和别的太像，只留了 %d 张"
            % (n, len(out_drop), len(kept_recs)))
    return kept_recs, out_drop
