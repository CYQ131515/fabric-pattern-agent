"""落库：把选中的图整理成**和本地花型库一模一样的形状**。

形状对齐到什么程度
    直接照 数据/library/ 的目录约定来：orig/ 放原图、thumb/ 放缩略图、
    index.json 是索引，字段名也一一对上（id / name / file / w / h / md5 /
    source / source_label / status / style / element / color / scene /
    keywords / desc / image / full …）。这样后端**不需要改一行代码**就能
    把这份产物当成一个花型库来展示、筛选、选中（前端认的就是这些字段）。

和库不重合的地方（都标清楚了）
    · id 前缀用 `PF-WEB-<日期>-####`，和库里的 `PF-LIB-` 分开 —— 全网搜
      回来的图**还没入库**，只是"候选池"，ID 撞了以后会很难收拾。
    · 多写了 src_url / src_page / src_site / src_provider / src_query，
      原始出处必须留着：这批图是从别人站上搜来的，将来要追溯、要避版权
      全靠这几个字段。
    · status 一律是 `untagged`（待打标签）—— **不假装打过标**。
      四维标签要靠视觉模型逐张看（算法/自动打标签 那个服务就能干），
      这里只在 keywords 里放"检索时用的词"和"标题洗出来的词"，
      并明确标 tag_mode='web'，绝不当成模型打出来的标签。
      库里本来就是这么设计"未打标"状态的，所以这个形状是合法的。

缩略图
    库里用 sips，这里用 Pillow —— 算法自带 Pillow，不再起外部进程。
    统一存 JPEG（库里也是 thumb/<id>.jpg），长边 480、质量 72，和库一致。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time

from PIL import Image

from . import rank

THUMB_PX = 480
THUMB_Q = 72

SOURCE_LABEL = "全网检索"

# 和 library.py 同一套来源标记（这批是新增的一类，后端要做展示映射的话加一项）
SOURCE_KEY = "web"


def _thumb(src, dst, max_px=THUMB_PX, q=THUMB_Q, bg=(255, 255, 255)):
    """生成缩略图。带透明通道的合成到白底（JPEG 不支持透明）。"""
    with Image.open(src) as im:
        im.load()
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            flat = Image.new("RGB", im.size, bg)
            flat.paste(im, mask=im.split()[-1])
            im = flat
        else:
            im = im.convert("RGB")
        w, h = im.size
        k = min(1.0, max_px / float(max(w, h)))
        if k < 1.0:
            im = im.resize((max(1, int(round(w * k))), max(1, int(round(h * k)))),
                           Image.LANCZOS)
        im.save(dst, "JPEG", quality=q, optimize=True)
    return dst


def _keywords(rec):
    """关键词只用**有出处的东西**：检索词 + 标题里洗出来的词。

    刻意不做的事：不从需求里搬四维标签当每张图的标签。那是"替图编标签"，
    和自动打标签那套"绝不硬套"的立场冲突。
    """
    src = rec.get("source") or {}
    words = []
    for s in (src.get("query") or "", rank.clean_name(src.get("title")) or ""):
        for w in s.replace("/", " ").replace("|", " ").split():
            w = w.strip(" ,，。.()（）[]")
            if len(w) >= 2 and w not in words:
                words.append(w)
    return words[:8]


def _clean_dir(path, log=None, keep=False):
    """清空一个目录（orig / thumb / _raw）。**删完要数一遍**，删不干净必须吭声。

    `keep=True` 时清完把空目录留下（orig/thumb 要接着往里放图）；
    `keep=False` 时整个目录都不要（_raw 是暂存，用完即弃）。

    `shutil.rmtree(ignore_errors=True)` 有两个坑，都会让"清空重建"变成
    "新旧混在同一个目录里"：
      · 它只吞 OSError —— 外部环境（沙箱守卫、安全软件、被占用的文件）拦下
        删除时若抛的是别的异常，会直接冒到调用方，落库半途中止；
      · 它可能只删掉一部分（部分文件被锁）就返回，不报错。
    两种情况的后果一样：索引里只有新图，旧图变成谁也管不到的垃圾 —— 而且
    没有任何提示，用户只会看到"图比上次多了"。所以这里删完必数。
    """
    log = log or (lambda *_a: None)
    name = os.path.basename(path)
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception as e:                                   # noqa: BLE001
        log("清 %s 时出错（继续，下面会数还剩多少）：%s" % (name, e))
    left = []
    if os.path.isdir(path):
        for fn in os.listdir(path):
            p = os.path.join(path, fn)
            try:
                if os.path.isdir(p) and not os.path.islink(p):
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    os.remove(p)
            except Exception:                                # noqa: BLE001
                pass
            if os.path.exists(p):
                left.append(fn)
    if left:
        log("⚠︎ %s 没清干净，还剩 %d 个（权限或外部限制）：%s —— 它们不在索引里，"
            "但会留在目录中" % (name, len(left), "、".join(sorted(left)[:3])))
        os.makedirs(path, exist_ok=True)
    elif keep:
        os.makedirs(path, exist_ok=True)
    else:
        try:
            os.rmdir(path)
        except OSError:
            pass
    return len(left)


def build(records, outdir, *, meta=None, log=None):
    """把记录写成花型库形状。回 (index dict, 路径 dict)。会先清掉旧的产物。

    records 是 rank.pick 的输出（已排序、已去重）。目录里的 raw_* 是下载
    暂存，这里只搬选中的那些进 orig/，其余的随 _raw 一起删掉。
    """
    log = log or (lambda *_a: None)
    orig = os.path.join(outdir, "orig")
    thumb = os.path.join(outdir, "thumb")
    raw = os.path.join(outdir, "_raw")
    for d in (orig, thumb):
        _clean_dir(d, log, keep=True)
    # 甄别的留档（rejected.json / _rejected/）**不在这里清** —— 它由
    # screen.dump_rejected() 自己重置，而且必须在原图还在的时候先跑。
    # 这里清的话，就会出现"先留档再被这里清掉"的空壳。

    stamp = time.strftime("%Y%m%d")
    now = time.time()
    items = []
    for i, rec in enumerate(records, 1):
        it = _make_item(rec, outdir, "PF-WEB-%s-%04d" % (stamp, i), now, log)
        if it:
            items.append(it)

    index, paths = _write(outdir, items, meta, now, seq=len(items))
    _clean_dir(raw, log)                                     # 暂存目录用完就清
    log("落库 %d 张 → %s" % (len(items), outdir))
    return index, paths


def _make_item(rec, outdir, pid, now, log):
    """一条记录 → 一个索引条目（拷原图、生成缩略图）。失败回 None。"""
    orig = os.path.join(outdir, "orig")
    thumb = os.path.join(outdir, "thumb")
    src = rec.get("source") or {}
    ext = rec.get("ext") or ".jpg"
    dst = os.path.join(orig, "%s%s" % (pid, ext))
    try:
        shutil.copyfile(rec["path"], dst)
    except Exception as e:                                   # noqa: BLE001
        log("%s 搬不进 orig：%s" % (pid, e))
        return None
    tp = os.path.join(thumb, "%s.jpg" % pid)
    try:
        _thumb(dst, tp)
    except Exception as e:                                   # noqa: BLE001
        log("%s 缩略图失败：%s" % (pid, e))
        tp = ""
    t = rec.get("tile") or {}
    name = rank.clean_name(src.get("title")) or ""
    scr = rec.get("screen") or {}
    return {
        "id": pid,
        "lib": False,                 # 不是本地库条目（是全网候选池）
        "name": name or pid,
        "file": os.path.relpath(dst, outdir),
        "thumb": os.path.relpath(tp, outdir) if tp else "",
        "w": rec.get("w"), "h": rec.get("h"),
        "md5": rec.get("md5"),
        "source": SOURCE_KEY,
        "source_label": SOURCE_LABEL,
        "src_url": src.get("url"),
        "src_page": src.get("page"),
        "src_site": src.get("site"),
        "src_provider": src.get("provider"),
        "src_query": src.get("query"),
        # ---- 溯源/质量（2026-10-19）----
        # 和 Pinterest 的 Pin 字段对齐：原站域名、作者、来源类型、质量先验、
        # 发布时间、有没有出处、授权提示、入库渠道。下游做"可授权/灵感"分流、
        # 排序、客户回原站购买，都靠这几个字段。
        "src_domain": src.get("domain") or src.get("site") or "",
        "src_author": src.get("author") or "",
        "src_kind": src.get("kind") or "",
        "src_quality": src.get("quality_prior"),
        "src_created_at": src.get("created_at") or "",
        "src_method": src.get("method") or "",
        "src_engagement": src.get("engagement"),
        "has_source": bool(src.get("has_source")),
        "license": src.get("license") or "unknown",
        "status": "untagged",
        "status_label": "待打标签",
        "added": now, "updated": now,
        "tag_mode": "web",
        "in_use": 0,
        # 平铺指标：这是这批图和普通图库最大的不同，必须留在索引里，
        # 页面/后台才能按"平铺质量"排，也便于排错。
        "seam": {"h": t.get("seam_h"), "v": t.get("seam_v"),
                 "quality": t.get("quality")},
        "tile_quality": t.get("quality"),
        # **闸门状态**：on=过了平铺闸门（真能铺）；off=放宽收下的线索图
        # （站方把预览裁过，铺开会露缝 —— 目前只有源创花样网）。
        # 这一条不能省：下游/后台要能一眼分出"能直接印花"和"只能当参考"。
        "tile_gate": ("on" if rec.get("tile_gate") is True else
                      ("off" if rec.get("tile_gate") is False else "skipped")),
        "graphic": t.get("graphic"),
        # 语义甄别的判词（没有模型时是空 dict）。留着是为了事后核对
        # "机器凭什么把它判合格"—— 甄别删错比漏删更难发现。
        "screen": ({"pattern": scr.get("pattern"), "match": scr.get("match"),
                    "why": scr.get("why")} if scr else {}),
        "via": rec.get("via"),
        "score": rec.get("score"),
        "keywords": _keywords(rec),
        "desc": (src.get("title") or "").strip()[:200],
        # 四维标签留空（真打过标才写），只把"检索时的需求"记在 need 里
        "style": "", "element": "", "color": "", "scene": "",
        "image": os.path.relpath(tp, outdir) if tp else os.path.relpath(dst, outdir),
        "full": os.path.relpath(dst, outdir),
    }


def _write(outdir, items, meta, now, seq=0):
    index = {"version": 1, "kind": "web-search", "updated": now,
             # 已发出去的最大编号。**编号只增不复用** —— 用户删掉 005 之后，
             # 补进来的新图必须是 006：复用会让"说好的 005"指向另一张图。
             "seq": int(seq or len(items)),
             "count": len(items), "need": meta or {}, "items": items}
    with open(os.path.join(outdir, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=1)

    # 搜索过程本身也留一份：出了问题时能复盘"当时用的是哪几条检索词"
    man = dict(meta or {})
    man.update({"built": now, "kept": len(items)})
    with open(os.path.join(outdir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=1)
    return index, {"orig": os.path.join(outdir, "orig"),
                   "thumb": os.path.join(outdir, "thumb"),
                   "index": os.path.join(outdir, "index.json")}


def append(records, outdir, *, meta=None, updates=None, log=None):
    """把新搜到的图**并进已有的产物目录**（补齐用）。回 (index, 路径, 统计)。

    和 build() 的区别：build 是"清空重建"，id 从 0001 重排；这里保留目录里
    已有的图（连同 id 和文件名），只管新增的。为什么必须这样 —— 用户是
    **先手工删掉不要的图，再来补齐**的；重建会把活下来那些图的编号全打乱，
    用户手上、对话里已经引用过的编号就全废了。

    `updates` 是 `{id: {字段: 值}}`，套在留下来的条目上。给"甄别之后把判词
    写回索引"用的 —— 不写回的话，索引里那张图永远没有"机器凭什么判它合格"
    的记录（老条目是上一版写的，那时还没有甄别这一步）。

    顺带做两件事（都在这一步做，别处就不用了）：
      · **存活检查**：索引里有、文件却不在的条目剔掉（用户手工删的就是这种），
        并清掉它留下的孤儿缩略图；
      · 孤儿缩略图清理：thumb/ 里有、索引里没有的也删掉。
    """
    log = log or (lambda *_a: None)
    outdir = os.path.abspath(outdir)
    for d in ("orig", "thumb"):
        os.makedirs(os.path.join(outdir, d), exist_ok=True)

    old_p = os.path.join(outdir, "index.json")
    old_items, old_meta, old_seq = [], {}, 0
    if os.path.exists(old_p):
        try:
            with open(old_p, encoding="utf-8") as f:
                old = json.load(f)
            old_items = list(old.get("items") or [])
            old_meta = old.get("need") or {}
            old_seq = int(old.get("seq") or 0)
        except Exception as e:                               # noqa: BLE001
            log("读旧索引失败（按空的处理）：%s" % e)

    alive = []
    for it in old_items:
        f = it.get("file") or ""
        if f and os.path.exists(os.path.join(outdir, f)):
            alive.append(it)
        else:
            th = it.get("thumb") or ""
            if th:
                try:
                    os.remove(os.path.join(outdir, th))
                except OSError:
                    pass
    removed = len(old_items) - len(alive)

    # 把调用方给的字段更新套到活下来的条目上（甄别判词就是这么写回去的）
    if updates:
        for it in alive:
            u = updates.get(it.get("id"))
            if u:
                it.update(u)

    # 新条目**续着编号**，不跟已有的撞，也不复用已经删掉的那些号
    max_n = max(old_seq, 0)
    for it in alive:
        m = re.search(r"-(\d+)$", str(it.get("id") or ""))
        if m:
            max_n = max(max_n, int(m.group(1)))
    stamp = time.strftime("%Y%m%d")
    now = time.time()
    added = []
    for rec in records:
        max_n += 1
        it = _make_item(rec, outdir, "PF-WEB-%s-%04d" % (stamp, max_n), now, log)
        if it:
            added.append(it)

    # 孤儿缩略图（索引里没有的）清掉
    known = {os.path.basename(i.get("thumb") or "") for i in alive + added}
    td = os.path.join(outdir, "thumb")
    for fn in os.listdir(td):
        if fn not in known:
            try:
                os.remove(os.path.join(td, fn))
            except OSError:
                pass

    items = alive + added
    m = dict(old_meta) if old_meta else dict(meta or {})
    if meta:
        for k, v in meta.items():
            if k in ("refill",) or k not in m:
                m[k] = v
    index, paths = _write(outdir, items, m, now, seq=max_n)
    _clean_dir(os.path.join(outdir, "_raw"), log)
    log("补齐落库：原有 %d 张、删掉 %d 张、新增 %d 张 → 共 %d 张"
        % (len(alive), removed, len(added), len(items)))
    return index, paths, {"原有": len(alive), "已删": removed, "新增": len(added)}


def summary(outdir):
    """读一遍已有产物，给统计。没跑过就回 ok=True + count=0。"""
    p = os.path.join(outdir, "index.json")
    if not os.path.exists(p):
        return {"ok": True, "count": 0, "note": "这个目录还没搜过"}
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:                                   # noqa: BLE001
        return {"ok": False, "error": "读索引失败：%s" % e}
    items = d.get("items") or []
    sites = {}
    for it in items:
        s = it.get("src_site") or "?"
        sites[s] = sites.get(s, 0) + 1
    q = [it.get("tile_quality") for it in items if it.get("tile_quality") is not None]
    return {"ok": True, "count": len(items),
            "need": d.get("need") or {},
            "来源分布": dict(sorted(sites.items(), key=lambda kv: -kv[1])[:10]),
            "平均平铺质量": round(sum(q) / len(q), 3) if q else None,
            "dir": outdir}
