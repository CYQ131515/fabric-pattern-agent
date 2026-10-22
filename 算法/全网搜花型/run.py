#!/usr/bin/env python3
"""全网搜花型 · 命令行（给人用的）。

    python run.py probe                 看看环境齐不齐（Pillow 在不在）
    python run.py keys                  各家搜索源的地址；默认十家不用申请 key
    python run.py search "国风 花卉 连衣裙"   【路线 1】凑满 100 张（不够就翻页再捞）
    python run.py pick  "国风 花卉 水彩"     【路线 2】不凑数，剩多少交多少
    python run.py tile a.jpg b.png      只判几张本地图能不能平铺
    python run.py one https://.../x.jpg 只判一张网上的图
    python run.py summary               看上次搜出来多少、来自哪些站

**两条路线怎么选**
    路线 1（search）：点名要"给我 100 张"。合格图不够就一路翻页去补，
        翻到第 7~9 页以后基本只剩"颜色像、做法不对"的图 —— 凑满 100 张
        实测要 20~50 分钟，而且最后那几十张质量明显掉一档。
    路线 2（pick）：只在乎"每张都得对"。**只搜一轮**，过完平铺闸门和语义
        甄别剩 30 张就交 30 张，不翻页、不重判、不凑数。一轮 8~13 分钟，
        质量齐整。--want 在这儿是上限（0 = 不限），不是任务。

结果默认落在 ./out，形状和本地花型库一致：
    out/orig/       原图
    out/thumb/      缩略图（480 长边）
    out/index.json  索引（字段和 数据/library/index.json 对齐）
    out/manifest.json 这次搜索的完整记录（需求/检索词/统计）

**开箱即用的顺序就是：probe → search。** 搜索用的是百度图片和 360 图片的
公开接口，不需要任何 key（境外那几家是可选的补充，不配也能跑）。
唯一值得配的是对话模型——配了以后检索词是模型按需求拟的，比模板准不少。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from wsearch import __version__, providers, screen, sources, store, tile     # noqa: E402
from wsearch import service                                             # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

# **所有检索产物都在 产物/ 一个文件夹里**。以前是 run.py 在哪个目录就落哪儿，
# 跑过几轮之后算法根目录会堆出 out、out_rabbit、out_watercolor2… 一片。
OUT_ROOT = os.path.join(HERE, "产物")
DEFAULT_OUT = os.path.join(OUT_ROOT, "out")


def _out(name):
    """把 `--outdir` 解析成真实路径。

    只写名字（out_1017_1546）→ 自动放进 `产物/`；
    写绝对路径、或用 `../x`、`/tmp/x` 这种带目录的 → 原样用（兼容老用法和
    临时目录）。这样命令行里还是只管起个名字，不用每次敲一长串路径。
    """
    name = (name or "").strip()
    if not name:
        return DEFAULT_OUT
    if os.path.isabs(name) or "/" in name or name.startswith("."):
        return name
    return os.path.join(OUT_ROOT, name)


def _hr(t):
    print("\n" + "=" * 62)
    print(t)
    print("=" * 62)


def cmd_probe(a):
    """环境自检。把"还差点什么"直接说清楚，别让人对着 ok=false 猜。"""
    r = service.cmd_probe(argparse.Namespace(json_out="", redact=False,
                                             llm_base="", llm_key="", llm_model=""))
    del r
    have = providers.available()
    _hr("搜索来源")
    for name in providers.DEFAULT_ORDER:
        spec = providers.REGISTRY[name]
        if spec.get("keyless"):
            mark = "✓ 免 key"
        else:
            mark = "✓ 已配" if name in have else "·  未配"
        print("  %-10s %-22s %s" % (mark, spec["label"], spec["home"]))
        print("             %s" % spec["note"])
    if not [n for n in have if providers.REGISTRY[n].get("keyless")]:
        print("\n  ⚠️ 免 key 的国内引擎也不可用了（一般是被临时反爬）。")
        print("     可以先配一把境外搜索的 key 顶着："
              "https://serper.dev（免费 2500 次）")
        print("     拿到 key 后设环境变量：" + providers.REGISTRY["serper"]["env"] + "=xxx")
    try:
        import PIL                                                       # noqa: F401
        print("\n  ✓ Pillow 在")
    except Exception:                                                    # noqa: BLE001
        print("\n  ✗ 缺 Pillow —— 先双击「安装.command」")
    # 注意别只看环境变量：service 会回落到本机后端已经配好的那个对话模型
    llm = service._llm_from_args(argparse.Namespace(
        llm_base="", llm_key="", llm_model=""))
    if llm.get("from") == "host":
        print("  ✓ 需求分析用本机后端已配的对话模型（%s）" % (llm.get("model") or "?"))
        print("     它会抽出工艺词和「这种做法长什么样」的正反例，"
              "甄别按做法判而不是按颜色")
    elif llm.get("key"):
        print("  ✓ 需求分析用的对话模型（已配 %s）" % (llm.get("model") or "?"))
    else:
        print("  · 需求分析没配模型 —— 检索词里只有「原话 + 无缝平铺」，缺工艺词，"
              "「颜色像、做法不对」的图会占多数")
        print("     配法：设环境变量 WSEARCH_LLM_KEY（或用后端已配的那个）")

    print("\n" + "=" * 62)
    print("语义甄别（把实物照、贴卡纸的展示图挑出来）")
    print("=" * 62)
    vlm = screen.VlmClient()
    if vlm.available():
        print("  ✓ 本地视觉模型在跑 —— 这一道会生效")
        print("     模型：%s" % vlm.model)
    else:
        print("  · 没跑：只有几何判定把关，「深底排一排实物 / 贴卡纸的展示图」")
        print("     这一类会混进来（几何特征和真花型重叠，分不开）。")
        print("     要开就先双击：算法/自动打标签/本地模型/启动本地模型.command")


def cmd_keys(a):
    _hr("各家搜索源（默认十一家不用申请 key；必应/源创/Patternbulk/精选站要 --provider 手动开）")
    # 先把"配置文件现在是什么状态"打一行 —— 用户改完 config.json 用这条确认生效。
    try:
        from wsearch import config as _cfg                     # noqa: PLC0415
        print("  " + _cfg.describe())
        print()
    except Exception:                                        # noqa: BLE001
        pass
    # 默认不跑的那几家也要列出来 —— 不写的话用户不知道还有这一路能开。
    todo = list(providers.DEFAULT_ORDER)
    todo += [n for n in providers.REGISTRY if n not in todo]
    for i, name in enumerate(todo, 1):
        spec = providers.REGISTRY[name]
        tag = "" if spec.get("default", True) else "（**默认不跑**，--provider %s 开）" % name
        print("  %d) %s%s" % (i, spec["label"], tag))
        print("     申请：%s" % spec["home"])
        # 要装依赖的那几家：光看"不需要 key"会以为直接就能跑，得说明白
        if spec.get("needs") == "scrapling":
            from wsearch import scrape                       # noqa: PLC0415
            print("     变量：**不需要 key**，但要装 Scrapling —— %s"
                  % ("已装，默认在跑" if scrape.have() else
                     "**没装，这家会跳过**（./.venv/bin/pip install "
                     "\"scrapling[fetchers]\"）"))
        else:
            print("     变量：%s" % (spec["env"] or "**不需要 key**"))
        print("     说明：%s" % spec["note"])
        print()


def _hunt_ns(a, outdir, want):
    """search / refill 共用的那组字段（两个命令的管线是同一套）。"""
    provider = a.provider
    if getattr(a, "all_keyless", False):
        provider = ",".join(
            n for n, spec in providers.REGISTRY.items()
            if spec.get("keyless") and n != "bing"  # 必应默认不跑：限速后回不相关结果
            and providers.enabled(n) and not providers.is_disabled(n))
    return dict(
        outdir=outdir, want=want, candidates=a.candidates, provider=provider,
        per_query=a.per_query, queries=a.queries, page=a.page, rounds=a.rounds,
        intent=getattr(a, "intent", ""), sites=getattr(a, "sites", ""),
        fresh_days=getattr(a, "fresh_days", 0),
        workers=a.workers, per_host=a.per_host, timeout=a.timeout,
        vocab=a.vocab or os.path.join(HERE, "..", "..", "后端", "data", "vocab.json"),
        progress_out="", no_screen=a.no_screen, loose=getattr(a, "loose", False),
        vlm_base=a.vlm_base, vlm_model=a.vlm_model, vlm_key=a.vlm_key,
        vlm_timeout=a.vlm_timeout, vlm_max_side=a.vlm_max_side,
        vlm_batch=a.vlm_batch, vlm_workers=a.vlm_workers,
        craft=a.craft, style_pos=a.style_pos, style_neg=a.style_neg,
        tile_gate=getattr(a, "tile_gate", False),
        llm_base=os.environ.get("WSEARCH_LLM_BASE", ""),
        llm_key=os.environ.get("WSEARCH_LLM_KEY", ""),
        llm_model=os.environ.get("WSEARCH_LLM_MODEL", ""), json_out="")


def _show(outdir):
    if not os.path.exists(os.path.join(outdir, "index.json")):
        return
    s = store.summary(outdir)
    _hr("结果")
    _q = s.get("平均平铺质量")
    print("  共 %d 张，%s" % (s["count"],
                             ("平均平铺质量 %s" % _q) if _q is not None
                             else "平铺检测：未检测（已取消四方连续标准）"))
    print("  来源分布（前 10）：")
    for site, n in (s.get("来源分布") or {}).items():
        print("    %-28s %d" % (site, n))
    print("\n  目录：%s" % outdir)


def cmd_search(a):
    """路线 1：凑满 --want 张（不够就翻页再捞）。"""
    ns = argparse.Namespace(need=" ".join(a.need), brief="",
                            **_hunt_ns(a, _out(a.outdir), a.want))
    code = service.cmd_search(ns)
    _show(ns.outdir)
    return code


def cmd_pick(a):
    """路线 2：检索 → 甄别 → 删除 → **剩多少交多少**（不凑数、不翻页）。"""
    ns = argparse.Namespace(need=" ".join(a.need), brief="",
                            **_hunt_ns(a, _out(a.outdir), a.want))
    code = service.cmd_pick(ns)
    _show(ns.outdir)
    return code


def cmd_refill(a):
    """补满到 --want 张：先把不合格的甄别再删一遍，再出去补齐。"""
    ns = argparse.Namespace(need=" ".join(a.need), brief="", no_rescreen=a.no_rescreen,
                            **_hunt_ns(a, _out(a.outdir), a.want))
    code = service.cmd_refill(ns)
    _show(ns.outdir)
    return code


def cmd_screen(a):
    """只做甄别：把已有产物里不合格的删掉（不重新搜）。"""
    ns = argparse.Namespace(
        need=" ".join(a.need), outdir=_out(a.outdir), progress_out="",
        no_screen=a.no_screen, vlm_base=a.vlm_base, vlm_model=a.vlm_model,
        vlm_key=a.vlm_key, vlm_timeout=a.vlm_timeout, vlm_max_side=a.vlm_max_side,
        vlm_batch=a.vlm_batch, vlm_workers=a.vlm_workers, loose=a.loose,
        craft=a.craft, style_pos=a.style_pos, style_neg=a.style_neg, json_out="")
    return service.cmd_screen(ns)


def cmd_tile(a):
    bad = 0
    for p in a.files:
        r = tile.analyze(p)
        flag = "能铺" if r["seamless"] else "不能铺"
        bad += 0 if r["seamless"] else 1
        print("%-34s %-8s %-9s 接缝=%s/%s  说明=%s" % (
            os.path.basename(p)[:34], flag, r["verdict"],
            r["seam_h"], r["seam_v"], r["note"]))
    return 0 if bad == 0 else 1


def cmd_one(a):
    ns = argparse.Namespace(url=a.url, title="", page="", outdir=_out(a.outdir),
                            timeout=25, json_out="")
    return service.cmd_one(ns)


def cmd_summary(a):
    return service.cmd_summary(argparse.Namespace(outdir=_out(a.outdir),
                                                  json_out=""))


def _add_hunt(p, default_rounds=3, want_default=100,
              want_help="要多少张（默认 100）"):
    """检索这一组参数（search / refill / pick 共用）。

    pick 是路线 2：不凑数，所以 --rounds 默认 0（不翻页）、--want 是上限
    （0 = 不限）。
    """
    p.add_argument("--want", type=int, default=want_default, help=want_help)
    p.add_argument("--candidates", type=int, default=0,
                   help="下载多少候选去筛（默认 want×8；路线 2 默认 300）")
    p.add_argument("--provider", default="", help="只用这几家，逗号分隔")
    p.add_argument("--all-keyless", action="store_true",
                   help="打开所有已接入、免 key 的来源（自动排除需要 key 的）")
    p.add_argument("--per-query", type=int, default=100)
    p.add_argument("--queries", type=int, default=10, help="生成几条检索词（默认 10）")
    p.add_argument("--intent", default="",
                   choices=("", "business", "inspiration", "specific"),
                   help="意图：business=找可印花素材 / inspiration=找风格 / specific=按花名")
    p.add_argument("--sites", default="",
                   help="site_search 的站点（逗号分隔，如 etsy,creativemarket）")
    p.add_argument("--fresh-days", type=int, default=0,
                   help="只收最近 N 天的图（0=不过滤，只按新鲜度排序）")
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--rounds", type=int, default=default_rounds,
                   help="合格图不够时最多再翻几轮页去补（路线 1 默认 3，"
                        "路线 2 默认 0；0 = 不补）")
    p.add_argument("--workers", type=int, default=12)
    p.add_argument("--per-host", type=int, default=4)
    p.add_argument("--timeout", type=int, default=25)
    p.add_argument("--vocab", default="")
    p.add_argument("--outdir", default="")
    p.add_argument("--tile-gate", action="store_true",
                   help="恢复「接缝必须对得上」的硬筛选。**默认不筛**（用户 "
                        "2026-10-18 的要求）：接缝对不上的图照样收，只在索引里"
                        "标 tile_gate=off、排序往后放。加了这个参数才回到旧行为")


def _add_vlm(p):
    """语义甄别这一组参数。"""
    p.add_argument("--sim", type=float, default=0,
                   help="相似去重阈值 0~1。**不给就按 config.json 的 "
                        "filters.similar_threshold**（默认 0.995）：同一张图的变体、"
                        "同一套素材包只留质量最高的一张；0 = 这一次关掉这层")
    p.add_argument("--no-screen", action="store_true",
                   help="跳过语义甄别（快很多，但实物照/展示图会混进来）")
    p.add_argument("--loose", action="store_true",
                   help="语义甄别用宽松档：不按工艺严判，只剔非图案/明显跑题")
    p.add_argument("--vlm-base", default="",
                   help="视觉端点。不填就走后端已配的云端识图模型；"
                        "填 http://127.0.0.1:11435/v1 可退回本机模型")
    p.add_argument("--vlm-model", default="", help="视觉模型名（默认跟后端配置）")
    p.add_argument("--vlm-key", default="")
    p.add_argument("--vlm-timeout", type=int, default=0)
    p.add_argument("--vlm-max-side", type=int, default=0,
                   help="送进模型的图缩到多小（默认 768；别再往上调，1024 反而更差）")
    p.add_argument("--vlm-batch", type=int, default=0,
                   help="一次判几张（默认跟引擎走：云端 10、本机 6）")
    p.add_argument("--vlm-workers", type=int, default=0,
                   help="并发几批（默认跟引擎走：云端 8、本机 1）")
    # 风格判据。不给就由需求分析自动生成（绝大多数情况够用），
    # 手动给是为了"机器判偏了，我自己写死几条"这种补救场景。
    p.add_argument("--craft", default="",
                   help="要的工艺/风格，如 扎染、蜡染、水彩。给了就按做法严判")
    p.add_argument("--style-pos", default="",
                   help="逗号分隔：这种做法画面上长什么样（正例）")
    p.add_argument("--style-neg", default="",
                   help="逗号分隔：看着像但做法不对的样子（反例）")


def cmd_sources(a):
    _hr("来源图（质量先验 = 这个来源里花型密度的经验值；site=需 Serper key 的精选站）")
    print("  推荐顺序：专业交易平台 > 专业花型站 > 作品集/品牌 > 通用搜索 > 图库")
    print()
    print(sources.summary())
    print()
    print("  用法：")
    print("    python run.py pick \"国风 花卉\" --provider site_search --sites etsy,creativemarket")
    print("    python run.py pick \"国风 花卉\" --provider site_search --intent business")
    print("  注意：site_search 需要 Serper/SerpAPI/Brave 任意一把 key；不配就只跑免 key 的来源。")
    return 0


def main():
    p = argparse.ArgumentParser(prog="run.py", description="全网搜花型 v" + __version__)
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("probe", help="环境自检：Pillow 在不在、哪些搜索来源能用")
    sub.add_parser("keys", help="各家搜索源的地址与要配的环境变量（默认十家不用申请 key）")
    sub.add_parser("sources", help="来源图：质量先验/意图/访问方式（含精选站）")

    s = sub.add_parser("search",
                       help="【路线 1】按需求全网搜平铺花型，凑满 N 张（不够就翻页再捞）")
    s.add_argument("need", nargs="+", help="用户需求，比如：国风 花卉 蓝白 连衣裙")
    _add_hunt(s)
    _add_vlm(s)

    k = sub.add_parser("pick",
                       help="【路线 2】检索→甄别→删除，剩多少张就交多少张（不凑数）")
    k.add_argument("need", nargs="+", help="用户需求，比如：国风 花卉 水彩")
    _add_hunt(k, default_rounds=0, want_default=0,
              want_help="**上限**，不是任务：0 = 不限（默认），有多少合格收多少")
    _add_vlm(k)

    r = sub.add_parser("refill",
                       help="补齐到 N 张：先甄别再删，再出去搜（用户删完图就跑这个）")
    r.add_argument("need", nargs="*", help="需求；留空就用上次记在目录里的")
    r.add_argument("--no-rescreen", action="store_true",
                   help="不重新甄别现有的，只补缺的那几张")
    _add_hunt(r)
    _add_vlm(r)

    c = sub.add_parser("screen", help="只对已有产物做语义甄别：不合格的删掉")
    c.add_argument("need", nargs="*", help="需求；留空就用上次记在目录里的")
    _add_vlm(c)
    c.add_argument("--outdir", default="")

    t = sub.add_parser("tile", help="判本地图能不能平铺")
    t.add_argument("files", nargs="+")

    o = sub.add_parser("one", help="判一张网图")
    o.add_argument("url")
    o.add_argument("--outdir", default="")

    m = sub.add_parser("summary", help="看上次搜出来多少张、来自哪些站")
    m.add_argument("--outdir", default="")

    a = p.parse_args()
    if not a.cmd:
        p.print_help()
        return 2
    return {"probe": cmd_probe, "keys": cmd_keys, "search": cmd_search,
            "refill": cmd_refill, "screen": cmd_screen, "pick": cmd_pick,
            "tile": cmd_tile, "one": cmd_one, "summary": cmd_summary,
            "sources": cmd_sources}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
