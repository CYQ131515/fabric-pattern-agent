"""候选图落地：并发下载 → 去重 → **平铺闸门** → 留下合格的原图。

为什么这一层必须并发
    项目里有过教训（见 .workbuddy/memory 里"逐个探活必须并发"那条）：
    一次搜索能出 600~1000 个候选，串行下载每张 1~3 秒就是十几二十分钟，
    用户只会以为程序卡死了。这里用线程池并发下载，同时**限制每个图床的
    并发数** —— 全冲一个域名会被限速甚至拉黑，那样整批都下不来。

下载失败是常态，不是异常
    图床会：挡盗链（要 Referer）、直接 403、Content-Type 撒谎、图已经删了、
    回一个"图片已下架"的占位图。所以这里**不抛错**，逐张记录失败原因，
    最后统计出来。原图下不动就退一步拿缩略图 —— 缩略图是同一张图的
    降采样，平铺判定照样成立（判定本来就是缩到 256 长边做的）。

去重做两层
    · URL 原文去重（同一家网关的同一张图出现多次）
    · **图像内容 md5 去重**（不同站转载同一张图，或者图床给不同尺寸的
      同一张图）—— 这一层才是真正省配额的那层，实测能砍掉两成重复。
"""
from __future__ import annotations

import hashlib
import io
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from PIL import Image, ImageStat

from . import net, tile
from . import config

# 认图片靠**魔法字节**，不看扩展名也不信 Content-Type —— 图床这两样都经常撒谎。
_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
    (b"RIFF", "image/webp", ".webp"),        # 还要再看第 8-12 字节是 WEBP
    (b"BM", "image/bmp", ".bmp"),
)


def sniff(blob):
    """从字节头认图片。回 (mime, ext)，认不出回 ('', '')。"""
    if not blob or len(blob) < 12:
        return "", ""
    for magic, mime, ext in _MAGIC:
        if blob.startswith(magic):
            if magic == b"RIFF" and blob[8:12] != b"WEBP":
                continue
            return mime, ext
    return "", ""


def _grab(row, timeout, max_bytes):
    """拿一张图的字节。原图不行就退缩略图。回 (blob, url, how) 或 None。"""
    tries = [(row.get("url"), "full")]
    if row.get("thumb") and row.get("thumb") != row.get("url"):
        tries.append((row.get("thumb"), "thumb"))
    last = "没有可用的图片地址"
    for url, how in tries:
        if not url:
            continue
        try:
            blob, _ct, _final = net.fetch_bytes(url, timeout=timeout,
                                                max_bytes=max_bytes,
                                                referer=(row.get("ref")
                                                         or row.get("page") or ""))
        except Exception as e:                               # noqa: BLE001
            last = "%s：%s" % (how, e)
            continue
        mime, _ext = sniff(blob)
        if not mime:
            last = "%s：拿到的不是图片" % how
            continue
        return blob, url, how
    return {"error": last}


def collect(rows, *, outdir, want=100, workers=12, per_host=4, timeout=25,
            max_bytes=None, log=None, progress=None, min_keep=None,
            tile_gate=False):
    """把候选链接变成本地图片，按闸门策略留下。

    回 (合格记录列表, 统计 dict)。记录形状：
        {"path":..., "md5":..., "ext":..., "mime":..., "w":..., "h":...,
         "tile": <tile.analyze 的结果>, "source": <候选行>, "via": full|thumb,
         "tile_gate": <是否真过了接缝判定>}

    参数
        want       想要多少张的。到数就停（不再提交新任务）
        min_keep   低于这个数就算"搜得不够"，调用方可以再翻页（默认 = want)
        progress   回调 (done, total, kept, phase)，给 --progress-out 用
        **tile_gate  接缝要不要当筛选条件。默认 False —— 用户 2026-10-18
            明确说"接缝对不上不用做筛选"，所以**接缝不再拦图**，
            但 `tile.analyze` 照跑，`tile_gate` 字段照记（True=真能铺开）。
            想要旧行为就 `tile_gate=True`（CLI `--tile-gate`）。

    两种"不合格"仍然拦，因为它们跟接缝无关、收了也没法用：
        · `too_small`：最短边 < 200px（缩略图统计样本太少）
        · `flat`：整张纯色，没有花型内容

    ⚠️ 关掉接缝筛选的代价：结果里会出现**铺开会露缝**的图。索引里
       `tile_gate: "off"` 会标出来，排序上也排在后面（quality=0），
       但**别把它们当可印花素材**。
    """
    log = log or (lambda *_a: None)
    max_bytes = max_bytes or net.MAX_IMAGE_BYTES
    want = max(1, int(want))
    min_keep = want if min_keep is None else int(min_keep)
    # 筛选项从 config.json 读（"纯色要不要丢""最小边多少"这种）。
    # 读不到就用默认，不影响任何既有行为。
    try:
        _f = config.filters()
    except Exception:                                        # noqa: BLE001
        _f = {}
    min_side = int(_f.get("min_side") or 0)
    drop_flat = bool(_f.get("drop_flat", True))
    # 2026-10-19 用户要求：**取消四方连续的检测标准**。
    # tile_check=False 时完全不跑 tile.analyze（不判接缝、不判纯色），
    # 只保留"能不能解码"和（可选）最小边；索引里 tile_quality=None、
    # tile_gate="skipped"。平铺判定仍可用 `run.py tile` / `one` 单独跑。
    tile_check = bool(_f.get("tile_check", True))
    quality_gate = bool(_f.get("quality_gate", True))
    _TITLE_BAD = ("合集", "拼图", "九宫格", "多格", "色卡", "预览", "展示",
                  "目录", "素材包", "花型大全", "海报", "壁纸合集", "裁剪图")

    def _dims(blob):
        try:
            with Image.open(io.BytesIO(blob)) as im:
                im.load()
                return im.size
        except Exception:                                    # noqa: BLE001
            return None

    def _bad_border(blob):
        """大块白边/底部留白 = 截图/展示图，不是整块花型。

        只看"底部 20% 几乎纯白、顶部不是"这种明显不对称留白，
        避免误伤白底花型（真花型四周会碰到花/叶）。
        """
        try:
            with Image.open(io.BytesIO(blob)) as im:
                g = im.convert("L")
                w, h = g.size
                if h < 80 or w < 80:
                    return False
                top = ImageStat.Stat(g.crop((0, 0, w, max(1, int(h * 0.10)))))
                bot = ImageStat.Stat(g.crop((0, int(h * 0.80), w, h)))
                tm, bm = top.mean[0], bot.mean[0]
                return bm > 248 and bot.stddev[0] < 3 and tm < 245
        except Exception:                                    # noqa: BLE001
            return False

    # ① 链接去重（保序，先来的优先 —— 它来自排在前面的检索词）
    uniq, seen = [], set()
    for r in rows:
        u = (r.get("url") or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        uniq.append(r)

    stats = {"候选": len(rows), "去重后": len(uniq), "下载失败": 0, "不是图片": 0,
             "内容重复": 0, "判定不通过": 0, "太小的": 0, "留下": 0,
             "接缝不符但收了": 0, "退用缩略图": 0, "放宽闸门": 0,
             "未检测平铺": 0, "错误样本": []}

    if not uniq:
        return [], stats

    os.makedirs(outdir, exist_ok=True)
    gates = {}
    gate_guard = threading.Lock()
    by_md5 = set()
    lock = threading.Lock()
    kept = []
    done = [0]
    t0 = time.time()

    def gate_for(host):
        with gate_guard:
            if host not in gates:
                gates[host] = threading.Semaphore(max(1, per_host))
            return gates[host]

    def one(row):
        """处理一个候选。回记录或 None。"""
        host = row.get("site") or "?"
        if quality_gate:
            title = str(row.get("title") or "")
            if any(w in title for w in _TITLE_BAD):
                return {"_fail": "tile", "_why": "标题像合集/展示图：%s" % title[:40],
                        "_verdict": "not_pattern"}
        sem = gate_for(host)
        with sem:
            got = _grab(row, timeout, max_bytes)
        if isinstance(got, dict):
            return {"_fail": "download", "_why": got["error"]}
        blob, url, how = got

        md5 = hashlib.md5(blob).hexdigest()
        with lock:
            first = md5 not in by_md5
            by_md5.add(md5)
        if not first:
            return {"_fail": "dup"}

        if not tile_check:
            size = _dims(blob)
            if not size:
                return {"_fail": "broken", "_why": "图片解不开"}
            if min(size) < max(200, int(min_side or 0)):
                return {"_fail": "tile", "_why": "图太小（%d×%d）"
                        % (size[0], size[1]), "_verdict": "too_small"}
            if quality_gate and _bad_border(blob):
                return {"_fail": "tile", "_why": "大块白边/底部留白，像截图/展示图",
                        "_verdict": "not_pattern"}
            info = {"ok": True, "seamless": None, "verdict": "unchecked",
                    "size": size, "quality": None, "graphic": None,
                    "seam_h": None, "seam_v": None, "flags": ["unchecked"]}
        else:
            info = tile.analyze(blob)
        if not info.get("ok"):
            return {"_fail": "broken", "_why": info.get("note")}
        verdict = info.get("verdict")
        # 这两类是"图本身没法用"，跟接缝无关 —— 不管闸门开不开都拦。
        # 例外：config.json 里把 `drop_flat` 关了，纯色图也放行（有人拿它当底布）。
        if tile_check and verdict in ("too_small", "flat"):
            if verdict == "flat" and not drop_flat:
                pass
            else:
                return {"_fail": "tile", "_why": info.get("note"),
                        "_verdict": verdict}
        # config.json 的 `min_side`：比 tile.MIN_SIDE 更严的那道（0 = 不额外要求）
        if tile_check and min_side and min(info["size"][0], info["size"][1]) < min_side:
            return {"_fail": "tile", "_why": "图太小（%d×%d，低于设置的 %d）"
                    % (info["size"][0], info["size"][1], min_side),
                    "_verdict": "too_small"}
        # 接缝对不上（not_tile）：
        #   · tile_gate=True（旧行为 / --tile-gate）→ 拦；
        #   · 默认（False）→ **收**，但记录上标 tile_gate=False。
        # 用户 2026-10-18 明确说"接缝对不上不用做筛选"，所以默认不拦。
        # ⚠️ 收下来的图铺开会露缝，索引里必须标出来（tile_gate: off），
        #    否则有人拿它去印花才发现，那是我们骗了他。
        if tile_check and (not info.get("seamless")) and tile_gate and not row.get("tile_opt"):
            return {"_fail": "tile", "_why": info.get("note"),
                    "_verdict": verdict}

        _mime, ext = sniff(blob)
        path = os.path.join(outdir, "raw_%s%s" % (md5[:16], ext or ".img"))
        try:
            with open(path, "wb") as f:
                f.write(blob)
        except Exception as e:                               # noqa: BLE001
            return {"_fail": "broken", "_why": "写文件失败：%s" % e}
        return {"path": path, "md5": md5, "ext": ext or ".img", "mime": _mime,
                "w": info["size"][0], "h": info["size"][1],
                "tile": info, "source": row, "via": how,
                # True=过了接缝闸门；False=放宽收下；None=这次没跑检测
                "tile_gate": (bool(info.get("seamless"))
                              if tile_check else None)}

    def tick():
        with lock:
            done[0] += 1
            n, k = done[0], len(kept)
        if progress and (n % 5 == 0 or n == len(uniq)):
            progress(n, len(uniq), k, "fetch")

    max_workers = max(1, min(int(workers), len(uniq)))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = []
        it = iter(uniq)
        # 先塞满池子；每完成一个再补一个 —— 这样"够了就停"能立刻生效，
        # 不会把 1000 个任务一次性全排上队、白下几百张。
        for _ in range(max_workers):
            try:
                futures.append(pool.submit(one, next(it)))
            except StopIteration:
                break
        while futures:
            f = futures.pop(0)
            try:
                rec = f.result()
            except Exception as e:                           # noqa: BLE001
                rec = {"_fail": "broken", "_why": "%s：%s" % (type(e).__name__, e)}
            if rec is None:
                pass
            elif rec.get("_fail"):
                kind = rec["_fail"]
                if kind == "download":
                    stats["下载失败"] += 1
                elif kind == "dup":
                    stats["内容重复"] += 1
                elif kind == "tile":
                    stats["判定不通过"] += 1
                    if rec.get("_verdict") == "too_small":
                        stats["太小的"] += 1
                elif kind == "broken":
                    stats["不是图片"] += 1
                if rec.get("_why") and len(stats["错误样本"]) < 6:
                    stats["错误样本"].append(rec["_why"])
            else:
                kept.append(rec)
                if rec.get("via") == "thumb":
                    stats["退用缩略图"] += 1
                if rec.get("tile_gate") is False:
                    stats["放宽闸门"] += 1
                    stats["接缝不符但收了"] += 1
                elif rec.get("tile_gate") is None:
                    stats["未检测平铺"] = stats.get("未检测平铺", 0) + 1
            tick()
            if len(kept) >= want:
                break                                        # 够了，不再补任务
            try:
                futures.append(pool.submit(one, next(it)))
            except StopIteration:
                pass

    stats["留下"] = len(kept)
    stats["耗时秒"] = round(time.time() - t0, 1)
    log("下载 %d 个候选，留下 %d 张平铺图（%.1fs）"
        % (len(uniq), len(kept), stats["耗时秒"]))
    return kept, stats
