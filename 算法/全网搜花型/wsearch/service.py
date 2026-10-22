#!/usr/bin/env python3
"""全网搜花型 · 对外服务入口（给主程序调用，不是给人用的）。

契约（主程序只依赖这几条，改动必须向后兼容）
    命令
        python -m wsearch.service probe   --json-out <结果.json>
                      自检：哪些搜索来源配了 key、对话模型能不能用、Pillow 在不在。
                      **只回 ok + 一句人话，不回 key**（--redact 时连端点地址也不回）

        python -m wsearch.service search  --need "<用户需求>" --outdir <目录>
                                          --json-out <结果.json>
                      整条产线：分析需求 → 全网检索 → 下载 → 平铺判定 →
                      排序去重 → 落成花型库形状。

        python -m wsearch.service one     --url <图片直链> --outdir <目录>
                                          --json-out <结果.json>
                      只判一张网上的图：下下来、跑平铺判定，通过就存进去。
                      用户在对话里丢一个图片链接时用。

        python -m wsearch.service summary --outdir <目录> --json-out <结果.json>
                      读一遍已搜到的结果，给统计

    通用参数
        --want N            想要多少张**合格**的平铺图（默认 100）
        --candidates N      下载多少个候选去筛（默认 want × 8，下限 200）
        --provider a,b      只用这几家搜索来源
                            （默认 = 免 key 的国内引擎 + 已配 key 的，按优先级）
        --page N            翻页（同一批检索词要更深的结果）
        --workers N         下载并发（默认 12）；--per-host N 单域名并发（默认 4）
        --min-side N        小于这个边长的图不收（默认 200，判定同理）
        --vocab <path>      词表，用于归一四维（默认用 后端/data/vocab.json）
        --progress-out <p>  进度原子写到这里，主程序边跑边读；
                            形状 {phase, done, total, kept, elapsed, ts}
        --redact            自检结果里隐去端点地址（页面展示场景用）

    环境（由主程序注入，不落盘、不进配置文件）
        WSEARCH_SERPER_KEY / WSEARCH_SERPAPI_KEY / WSEARCH_BRAVE_KEY
        WSEARCH_PEXELS_KEY / WSEARCH_PIXABAY_KEY / WSEARCH_UNSPLASH_KEY
            图片搜索来源的 key，**至少配一把**（推荐 Serper）
        WSEARCH_LLM_BASE / WSEARCH_LLM_KEY / WSEARCH_LLM_MODEL
            对话模型（做需求分析、生成检索词）。**留空也能跑** ——
            会退化成"关键词 + 行业词模板"，只是检索词质量差一截。
            这个复用主程序已有的对话模型，不需要另配。

    输出
        --json-out 里永远有 "ok"；失败时 {"ok": false, "error": "人话", "stage": "..."}。
        stdout 也打一份同样的 JSON，日志一律走 stderr。

    退出码
        0 = 跑完了（业务失败也算，看 JSON 的 ok）
        2 = 参数错误
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wsearch import __version__, analyze, fetch, providers, rank, screen, store, tile  # noqa: E402
from wsearch import sources as _sources  # noqa: E402
from wsearch import net                                                       # noqa: E402
from wsearch import similar                                                   # noqa: E402
from wsearch import config                                                    # noqa: E402


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _outdir(name):
    """把 `--outdir` 解析成真实路径，**产物统一落 `产物/` 一个文件夹**。

    只给名字（out_1017_1546）→ 放进 `产物/`；给绝对路径或带目录的
    （/tmp/x、../y）→ 原样用。run.py 里有一份同样的规则：那边是给人看的
    命令行，这边是子进程契约 —— 两边都得认，否则从后台直接调 service
    又会落回算法根目录。
    """
    name = (name or "").strip()
    if not name:
        return os.path.join(_ROOT, "产物", "out")
    if os.path.isabs(name) or "/" in name or name.startswith("."):
        return os.path.abspath(name)
    return os.path.join(_ROOT, "产物", name)


def _log(*a):
    print(*a, file=sys.stderr, flush=True)


def _emit(obj, json_out):
    """把结果写文件 + 打 stdout。默认值都补齐，调用方少踩 None 的坑。"""
    obj.setdefault("ok", True)
    obj.setdefault("version", __version__)
    text = json.dumps(obj, ensure_ascii=False, indent=1)
    if json_out:
        try:
            d = os.path.dirname(os.path.abspath(json_out))
            os.makedirs(d, exist_ok=True)
            tmp = json_out + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, json_out)          # 原子写：主程序可能正在读
        except Exception as e:                               # noqa: BLE001
            _log("写结果文件失败：%s" % e)
    print(text, flush=True)
    return 0


def _fail(json_out, msg, stage=""):
    return _emit({"ok": False, "error": msg, "stage": stage}, json_out)


def _progress(path, **kw):
    """进度原子写。写不动就算了 —— 进度是给别人看的，不该影响主流程。"""
    if not path:
        return
    try:
        kw["ts"] = time.time()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(kw, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:                                        # noqa: BLE001
        pass


_HOST_CFG_FILES = ("settings.json", "secrets.json")


def _host_llm():
    """复用本机后端已经配好的对话模型（`后端/config/{settings,secrets}.json`）。

    为什么非要有这条：这一层原本只认 `--llm-key`，真机上跑出来
    `engine: "template"` —— 检索词里只有"原话 + 无缝平铺"，一点扎染的工艺词
    （shibori / tie dye / indigo resist）都没有，搜回来的全是**颜色像、做法
    不对**的图，风格正反例也就无从生成。而项目自己早就配好了对话模型，
    白放着不用。同一台机器、同一个项目，复用它的配置比让用户再填一遍 key
    合理；只读不改。不想要就设 `WSEARCH_NO_HOST_CONFIG=1`。
    """
    if str(os.environ.get("WSEARCH_NO_HOST_CONFIG", "")).strip().lower() \
            in ("1", "true", "yes", "on"):
        return {}
    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))
    cfg = os.path.join(root, "后端", "config")
    try:
        with open(os.path.join(cfg, "settings.json"), encoding="utf-8") as f:
            s = (json.load(f) or {}).get("llm") or {}
        with open(os.path.join(cfg, "secrets.json"), encoding="utf-8") as f:
            key = (json.load(f) or {}).get("api_key") or ""
    except Exception:                                        # noqa: BLE001
        return {}
    if not (s.get("base_url") and key):
        return {}
    return {"base": s.get("base_url"), "key": key,
            "model": s.get("model") or "",
            "max_tokens": int(s.get("max_tokens") or 0) or 0}


def _host_vision():
    """复用本机后端已配的**识图**模型（和 `_host_llm` 同一份配置，只读）。

    为什么甄别也要走云端：本机那个 4B 判 6 张要 20~40 秒，`deepseek-flash`
    实测 **1.7 秒**，而且判词质量明显更好（4B 分不开"硬边漏印"和真扎染，
    大模型能分开）。判 1000 张候选的成本也就几毛钱，省下来的是几十分钟。

    取值口径跟后端一致：端点用对话那个 `llm.base_url`（后端注释写着
    "视觉和对话同一家"），模型优先 `vision.model`，没配就退 `llm.model`
    （`deepseek-flash` 本身就支持图片输入，不用另开一个模型）。
    """
    if str(os.environ.get("WSEARCH_NO_HOST_CONFIG", "")).strip().lower() \
            in ("1", "true", "yes", "on"):
        return {}
    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))
    cfg = os.path.join(root, "后端", "config")
    try:
        with open(os.path.join(cfg, "settings.json"), encoding="utf-8") as f:
            s = json.load(f) or {}
        with open(os.path.join(cfg, "secrets.json"), encoding="utf-8") as f:
            sec = json.load(f) or {}
    except Exception:                                        # noqa: BLE001
        return {}
    llm = s.get("llm") or {}
    vis = s.get("vision") or {}
    base = vis.get("base_url") or llm.get("base_url") or ""
    key = sec.get("vision_api_key") or sec.get("api_key") or ""
    model = vis.get("model") or llm.get("model") or ""
    if not (base and key and model):
        return {}
    return {"base": base, "key": key, "model": model}


def _llm_from_args(args):
    """命令行 > 环境变量 > 本机后端已配的。回 {base,key,model,from}。"""
    base = _arg(args, "llm_base", "") or os.environ.get("WSEARCH_LLM_BASE", "")
    key = _arg(args, "llm_key", "") or os.environ.get("WSEARCH_LLM_KEY", "")
    model = _arg(args, "llm_model", "") or os.environ.get("WSEARCH_LLM_MODEL", "")
    src = "args" if (base or key or model) else ""
    # config.json 的 ai 段：优先级是 命令行 > 环境变量 > config.json > 本机后端配置
    try:
        c = config.ai()
    except Exception:                                        # noqa: BLE001
        c = {}
    if c.get("base") and not base:
        base, src = c["base"], src or "config"
    if c.get("model") and not model:
        model = c["model"]
    if c.get("key") and not key:
        key = c["key"]
    mx = 0
    if not (base and key):
        h = _host_llm()
        if h:
            base = base or h["base"]
            key = key or h["key"]
            model = model or h["model"]
            mx = int(h.get("max_tokens") or 0)
            src = src or "host"
    return {"base": base, "key": key, "model": model, "from": src,
            "max_tokens": mx or 4000}


# ------------------------------------------------------------------- probe
def cmd_probe(args) -> int:
    """自检。**绝不回 key**；--redact 时连端点地址都不回。

    和后台其它自检一个规矩（见 .workbuddy/memory 里那条硬规矩）：
    这个结果会上了后台页面被截图，所以只回 ok + 一句人话。
    """
    have = providers.available()
    # 全部列出来，缺的给申请地址。**包括默认不跑的那几家**（必应）——
    # 不列出来的话用户根本不知道还有这一路能开。
    todo = [p for p in providers.DEFAULT_ORDER]
    todo += [p for p in providers.REGISTRY if p not in todo]
    rows = []
    for name in todo:
        spec = providers.REGISTRY[name]
        rows.append({"name": name,
                     "label": spec["label"],
                     "home": "" if args.redact else spec["home"],
                     "configured": name in have,
                     "keyless": bool(spec.get("keyless")),
                     "default": bool(spec.get("default", True)),
                     "kind": spec["kind"],
                     "note": spec["note"]})

    llm = _llm_from_args(args)
    llm_ok = bool(llm.get("key"))
    try:
        import PIL                                       # noqa: F401
        pil = True
    except Exception:                                    # noqa: BLE001
        pil = False

    # 语义甄别能不能跑：本地视觉模型（算法/自动打标签/本地模型）起没起。
    # 起没起只影响"多不多一道把关"，不影响能不能搜 —— 所以它不算 ok 的一部分。
    vlm = screen.VlmClient(base=_arg(args, "vlm_base", "") or None,
                           model=_arg(args, "vlm_model", "") or None)
    vlm_ok = vlm.available()
    vlm_note = ("语义甄别可用" if vlm_ok else
                "语义甄别没跑（视觉模型没起来）—— 只有几何判定把关，"
                "混进来的实物照/展示图剔不掉；要开就先双击 "
                "算法/自动打标签/本地模型/启动本地模型.command")

    free = [n for n in have if providers.REGISTRY[n].get("keyless")]
    if not rows:
        note = "没有任何搜索来源可用"
    elif not have:
        note = ("没有可用的搜索来源 —— 连免 key 的国内引擎都被排除了，"
                "配置方法见 算法/全网搜花型/README.md")
    else:
        note = ("可用搜索来源：%s%s；需求分析%s；%s"
                % ("、".join(providers.REGISTRY[n]["label"] for n in have),
                   ("（其中 %s 免 key，开箱即用）"
                    % "、".join(providers.REGISTRY[n]["label"] for n in free))
                   if free else "（都要 key）",
                   ("可用（本机后端已配的 %s）" % (llm.get("model") or "对话模型"))
                   if llm.get("from") == "host" else
                   ("可用" if llm_ok else "未配（会退化成关键词直搜）"),
                   vlm_note))
    if not pil:
        note += "；缺 Pillow，先跑 安装.command"

    return _emit({
        "ok": bool(have) and pil,
        "ready": bool(have) and pil,
        "providers": rows,
        "llm": {"configured": llm_ok,
                "model": (llm.get("model") or "") if not args.redact else ""},
        "vlm": {"ready": vlm_ok,
                "model": (vlm.model or "") if not args.redact else "",
                "note": vlm_note},
        "pillow": pil,
        "note": note,
    }, args.json_out)


# ------------------------------------------------------------------ search
def _merge_fstats(a, b):
    """把两轮下载的统计并起来（键几乎全是计数，只有"错误样本"是列表）。"""
    out = dict(a)
    for k, v in (b or {}).items():
        if k == "错误样本":
            out[k] = ((out.get(k) or []) + (v or []))[:6]
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = (out.get(k) or 0) + v
        else:
            out[k] = v
    return out


def _merge_sstats(a, b):
    """合并两轮甄别的统计。note 是文字，其余是计数。"""
    out = dict(a or {})
    for k, v in (b or {}).items():
        if k == "note":
            if v:
                out[k] = "；".join(x for x in [str(out.get(k) or ""), str(v)] if x)
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = (out.get(k) or 0) + v
        else:
            out[k] = v
    return out


def _arg(args, name, default=None):
    """取参数。不同调用方给的 Namespace 字段不齐（比如补齐那条路径
    不需要 --candidates），统一在这儿兜一下，别处就不用写 getattr 了。"""
    v = getattr(args, name, None)
    return default if v is None else v


def _vlm(args):
    """建视觉客户端。三级回落：命令行 > 环境变量 > 本机后端已配的云端模型。

    最后才退到本机 11435 —— 那条路要用户先双击"启动本地模型.command"，
    忘了开就等于没甄别；而云端是配好的、随叫随到、还快一个数量级。
    """
    base = _arg(args, "vlm_base", "") or os.environ.get("WSEARCH_VLM_BASE", "")
    model = _arg(args, "vlm_model", "") or os.environ.get("WSEARCH_VLM_MODEL", "")
    key = _arg(args, "vlm_key", "") or os.environ.get("WSEARCH_VLM_KEY", "")
    src = "args" if (base or model or key) else ""
    # config.json 的 ai 段插在"环境变量"和"本机后端配置"之间。
    # 只补空的，别盖掉命令行/环境变量（优先级见 wsearch/config.py 文件头）。
    try:
        c = config.vision()
    except Exception:                                        # noqa: BLE001
        c = {}
    if c.get("base") and not base:
        base, src = c["base"], src or "config"
    if c.get("model") and not model:
        model = c["model"]
    if c.get("key") and not key:
        key = c["key"]
    if not (base and key):
        h = _host_vision()
        if h:
            base = base or h["base"]
            key = key or h["key"]
            model = model or h["model"]
            src = src or "host"
    cli = screen.VlmClient(
        base=base or None, model=model or None, api_key=key or None,
        timeout=_arg(args, "vlm_timeout", 0) or None,
        max_side=_arg(args, "vlm_max_side", 0) or None)
    cli.from_ = src or "local"
    return cli


def _need_text(need, plan):
    """甄别时给模型看的需求：用户原话 +（分析出来的维度）。

    为什么要带上维度：用户那句"国风 青花 花卉"里，"花卉"这种词模型能从图上
    看出来，但"面料印花"这种用途词得说清楚，否则它会把家纺画稿也判不符。
    """
    bits = []
    for k, v in ((plan or {}).get("brief") or {}).items():
        if isinstance(v, (list, tuple, set)):
            v = "、".join(str(x) for x in v if x)
        v = str(v or "").strip()
        if v:
            bits.append("%s %s" % (k, v))
    extra = "；".join(bits[:6])
    if not need:
        return extra
    return ("%s（%s）" % (need, extra)) if extra else need


def _style(args, plan):
    """这次甄别用什么风格判据。回 (craft, 正例, 反例)。

    命令行给了就用命令行的（方便临时试词），否则用需求分析生成的那套。
    **三个都空 = 用户没提工艺**，那时 match 退回"只防明显跑题"的宽松版。
    """
    def _split(s):
        return [x.strip() for x in str(s or "").split(",") if x.strip()]

    pl = plan or {}
    # --loose：这次不按工艺严判（只判"是不是图案 + 是否明显跑题"）。
    # 适合"卡通 水彩 童装"这种既要多、又不想被单一工艺词砍光的场景。
    if _arg(args, "loose", False):
        return "", [], []
    craft = (_arg(args, "craft", "") or pl.get("craft") or "").strip()
    pos = _split(_arg(args, "style_pos", "")) or list(pl.get("style_pos") or [])
    neg = _split(_arg(args, "style_neg", "")) or list(pl.get("style_neg") or [])
    return craft, pos, neg


def _screen(records, args, need_text, plan, log, progress=None):
    """跑语义甄别。回 (通过, 被剔, 统计)。

    判不出来的（模型没吐、超时、整批挂了）**算通过** —— 甄别是"多一道把关"，
    不是"少了判词就当垃圾"。理由见 screen.accepts()。
    """
    empty = {"送审": 0, "通过": 0, "不是图案": 0, "不符需求": 0, "没判出来": 0,
             "调用次数": 0, "note": ""}
    if not records:
        return [], [], dict(empty)
    # `--no-screen` 是"这一次别判"；config.json 的 `filters.screen=false` 是
    # "我一直不想判"。两者都当跳过，但话术要分开 —— 不然用户会以为参数没生效。
    if _arg(args, "no_screen", False):
        for r in records:
            r["screen"] = None
        return list(records), [], dict(empty, **{"note": "按 --no-screen 跳过了语义甄别"})
    try:
        _cfg_off = not config.filters().get("screen", True)
    except Exception:                                        # noqa: BLE001
        _cfg_off = False
    if _cfg_off:
        for r in records:
            r["screen"] = None
        return list(records), [], dict(
            empty, **{"note": "config.json 里 filters.screen=false，跳过了语义甄别"})

    craft, pos, neg = _style(args, plan)
    if craft and not (pos or neg):
        log("· 风格判据：要「%s」，但没有正反例 —— 模型只能凭名字猜，"
            "命中率不会太稳（用 --style-pos/--style-neg 补几句更准）" % craft)

    # 传 dict 而不是纯字符串：判据要拿 brief 里的题材元素（"兔子动物"），
    # 只传字符串时 _element() 拿不到 brief，题材闸门等于没开 —— 真机因此
    # 放行了「九宫格柴犬」「几何卡通鼠」「卡通鼠」这类做法对、题材错的图。
    need_arg = dict(plan or {})
    if need_text:
        need_arg.setdefault("text", need_text)
    client = _vlm(args)
    # 并发和批大小都跟引擎走：云端单批 1~2 秒，串行就是白排队；本机是单 GPU，
    # 并发只会互相拖。命令行给了就听命令行的。
    engine = getattr(client, "engine", "cloud")
    w = int(_arg(args, "vlm_workers", 0) or 0)
    if not w:
        w = 8 if engine == "cloud" else 1
    b = int(_arg(args, "vlm_batch", 0) or 0)
    if not b:
        b = screen.CLOUD_BATCH if engine == "cloud" else screen.BATCH
    log("· 语义甄别：%s引擎 %s，每批 %d 张、%d 路并发"
        % ("云端" if engine == "cloud" else "本机",
           getattr(client, "model", "?"), b, w))
    # 文化圈（国风/中式）也当判据传进去：光判"是不是水彩"会把欧美复古
    # 花卉全放行 —— 做法对、味道不对（真机国风水彩那批 90% 是西式）。
    style = ((plan or {}).get("brief") or {}).get("style") or ""
    st = screen.judge_records(
        records, need_arg, client=client, batch=b,
        negative=(plan or {}).get("negative") or (),
        craft=craft, style_pos=pos, style_neg=neg, style=style,
        tile_checked=bool(config.filters().get("tile_check", True)),
        log=log, progress=progress, workers=w)
    good = [r for r in records if screen.accepts(r)]
    bad = [r for r in records if not screen.accepts(r)]
    return good, bad, st


def _screen_updates(good):
    """把甄别结果整理成 `{id: {"screen": 判词}}`，供 store.append 写回索引。

    为什么非要写回：索引里那张图得留下"机器凭什么判它合格"。老条目是上一版
    落的，那时还没有甄别这一步，不补写的话它永远是个空白 —— 而甄别**删错**
    比漏删更难发现，事后只能靠这份判词复盘。

    只收"真判过"的（判词是字典）。没跑甄别那几轮 `rec["screen"]` 是 None，
    拿它去覆盖会把上一次的判词抹掉。
    """
    up = {}
    for r in good or ():
        it = r.get("index_item") or {}
        pid, rec = it.get("id"), r.get("screen")
        if pid and isinstance(rec, dict):
            up[pid] = {"screen": {"pattern": rec.get("pattern"),
                                  "match": rec.get("match"),
                                  "why": rec.get("why") or ""}}
    return up


class _HuntError(Exception):
    """搜不下去时抛这个（带一句人话 + 出错在哪个阶段）。"""

    def __init__(self, msg, stage=""):
        Exception.__init__(self, msg)
        self.stage = stage


def _hunt(args, plan, need_text, outdir, *, want_good, t0, log, only=None,
          fetch_want=None):
    """检索 → 下载 → 平铺闸门 → 语义甄别，不够就翻页再捞一轮。

    为什么非要循环：两道闸门叠起来留下的比例很低（实测 774 个候选 →
    平铺闸门 116 张 → 语义甄别 ~90 张），而一家引擎**一条检索词一次
    只给 60 条**。与其给用户一句"只凑出 90 张"，不如多花一两分钟翻页 ——
    路线 1 点名要的就是"100 张合格的"。

    fetch_want 是给**路线 2** 用的：那一路线不按"要交多少张"反推下载量
    （它根本没有交差数字），而是"这一轮能捞多少就下多少"，所以调用方
    直接给个数，别拿 want 去乘倍数。

    回 (通过, 被剔, 下载统计, 错误, 甄别统计)。
    """
    want = max(1, int(want_good))
    # 命令行指定意图时覆盖需求分析的判断（临时试词/纠偏用）
    if _arg(args, "intent", ""):
        plan["intent"] = args.intent
    cand = int(_arg(args, "candidates", 0) or 0) or max(200, want * 8)
    per_query = int(_arg(args, "per_query", 100) or 100)
    timeout = int(_arg(args, "timeout", 25) or 25)
    workers = int(_arg(args, "workers", 12) or 12)
    per_host = int(_arg(args, "per_host", 4) or 4)
    max_rounds = max(0, int(_arg(args, "rounds", 3) or 0))
    page = max(1, int(_arg(args, "page", 1) or 1))
    # 接缝要不要当筛选条件。默认 **False**（不过滤）—— 用户 2026-10-18
    # 明确说"接缝对不上不用做筛选"，CLI 用 --tile-gate 才恢复旧行为。
    # config.json 的 `filters.tile_gate` 也能把它打开（任一为真就开）：
    # 命令行代表"这一次要严"，配置代表"我一直想严"。
    try:
        _f = config.filters()
    except Exception:                                        # noqa: BLE001
        _f = {}
    tile_gate = bool(_arg(args, "tile_gate", False)) or bool(_f.get("tile_gate"))
    scratch = os.path.join(outdir, "_raw")
    pout = _arg(args, "progress_out", "")

    # 下载目标给到 want 的好几倍。只过平铺闸门时留 1.17 倍就够，现在多了
    # 语义甄别这一道（实测再砍掉一成多），所以要得更宽 —— 多下图的成本
    # 远低于"再翻三页"的时间。
    need_fetch = int(fetch_want or 0) or max(want * 4 + 12,
                                            want + max(5, want // 6))

    def prog(done, total, kept, phase):
        _progress(pout, phase=phase, done=done, total=total, kept=kept,
                  elapsed=round(time.time() - t0, 1))

    errs = []
    # 2026-10-19：把意图/精选站/新鲜度带进 search 的 opts。
    # site_search 用 site_domains；其它 provider 忽略这几个键。
    intent = _arg(args, "intent", "") or (plan or {}).get("intent") or ""
    try:
        _ss = config.site_search()
    except Exception:                                        # noqa: BLE001
        _ss = {"domains": [], "limit": 4}
    sites_arg = (_arg(args, "sites", "") or "").strip()
    site_domains = _ss.get("domains") or []
    if sites_arg:
        site_domains = [x.strip() for x in sites_arg.replace("，", ",").split(",") if x.strip()]
    if not site_domains:
        try:
            site_domains = _sources.site_domains(intent, limit=_ss.get("limit") or 4,
                                                 names=None if not sites_arg else
                                                 [x.strip() for x in sites_arg.replace("，", ",").split(",") if x.strip()])
        except Exception:                                    # noqa: BLE001
            site_domains = []
    _opts = {"page": page, "timeout": timeout, "intent": intent,
             "site_domains": site_domains, "sites": sites_arg,
             "site_limit": _ss.get("limit") or 4}
    try:
        rows, e0 = providers.search(plan["queries"], per_query=per_query,
                                    providers=only or None, opts=_opts)
    except net.NetError as e:
        raise _HuntError(str(e), "search")
    except Exception as e:                                   # noqa: BLE001
        raise _HuntError("检索这一步没跑成：%s" % e, "search")
    errs.extend(e0)
    log("② 检索到 %d 个候选（%d 家来源，%d 个错误）"
        % (len(rows), len(providers.available(only=only or None)), len(errs)))
    if not rows:
        raise _HuntError("一条结果都没搜到。%s"
                         % ("；".join(errs) if errs else
                            "可能是检索词太窄，换个说法试试。"), "search")

    keep, fstats = fetch.collect(rows[:max(cand, need_fetch)], outdir=scratch,
                                 want=need_fetch, workers=workers, per_host=per_host,
                                 timeout=timeout, min_keep=want, log=log, progress=prog,
                                 tile_gate=tile_gate)
    good, bad, sstats = _screen(keep, args, need_text, plan, log,
                                progress=lambda d, t, p: prog(d, t, p, "screen"))

    seen_urls = {r.get("url") for r in rows}
    rnd = 0
    while len(good) < want and rnd < max_rounds:
        rnd += 1
        page += 1
        log("③ 通过甄别的只有 %d/%d 张，去第 %d 页再捞一轮…" % (len(good), want, page))
        _progress(pout, phase="search", done=0, total=0, kept=len(good))
        try:
            more, e2 = providers.search(plan["queries"], per_query=per_query,
                                        providers=only or None,
                                        opts=dict(_opts, page=page))
        except Exception as e:                               # noqa: BLE001
            errs.append("第 %d 页没捞成：%s" % (page, e))
            break
        errs.extend(e2)
        fresh, added = [], set()
        for r in more:
            u = r.get("url")
            if not u or u in seen_urls or u in added:
                continue
            added.add(u)
            fresh.append(r)
        seen_urls.update(added)
        log("   第 %d 页新增 %d 个候选" % (page, len(fresh)))
        if len(fresh) < 5:
            break
        k2, s2 = fetch.collect(fresh[:max(cand, need_fetch)], outdir=scratch,
                               want=max(8, need_fetch - len(keep)), workers=workers,
                               per_host=per_host, timeout=timeout, log=log, progress=prog,
                               tile_gate=tile_gate)
        fstats = _merge_fstats(fstats, s2)
        if not k2:
            break
        keep.extend(k2)
        g2, b2, s3 = _screen(k2, args, need_text, plan, log,
                             progress=lambda d, t, p: prog(d, t, p, "screen"))
        good.extend(g2)
        bad.extend(b2)
        sstats = _merge_sstats(sstats, s3)

    return good, bad, fstats, errs, sstats


def cmd_search(args) -> int:
    need = (args.need or "").strip()
    if not need and not args.brief:
        return _fail(args.json_out, "没给需求，不知道要搜什么。", "input")
    outdir = _outdir(args.outdir)
    os.makedirs(outdir, exist_ok=True)

    want = max(1, int(args.want or 100))
    t0 = time.time()
    _progress(args.progress_out, phase="start", total=0, done=0, kept=0)

    # ① 需求分析 → 检索词
    _progress(args.progress_out, phase="analyze", total=0, done=0, kept=0)
    brief = {}
    if args.brief:
        try:
            brief = json.loads(args.brief)
        except Exception:                                    # noqa: BLE001
            brief = {}
    vocab = analyze.load_vocab(args.vocab or os.environ.get("WSEARCH_VOCAB") or "")
    _log("① 分析需求…")
    llm = _llm_from_args(args)
    if llm.get("from") == "host":
        _log("   需求分析用本机后端已配的对话模型（%s）" % (llm.get("model") or "?"))

    # 甄别用的风格判据：命令行给了就盖住模型抽的
    c_args, p_args, n_args = _style(args, {})
    plan, note = analyze.plan(need, brief=brief, vocab=vocab,
                              llm=llm, n_queries=args.queries, log=_log)
    if c_args:
        plan["craft"] = c_args
    if p_args:
        plan["style_pos"] = p_args
    if n_args:
        plan["style_neg"] = n_args
    _log("   检索词 %d 条（%s）：%s" % (len(plan["queries"]), plan["engine"],
                                       " | ".join(plan["queries"][:4])))
    if plan.get("craft"):
        _log("   要的工艺/风格：「%s」—— 正例 %d 条、反例 %d 条（甄别按做法判，不按颜色）"
             % (plan["craft"], len(plan.get("style_pos") or []),
                len(plan.get("style_neg") or [])))

    need_text = _need_text(need, plan)

    # ② 全网检索 → ③ 下载过平铺闸门 → ③.5 语义甄别（一张不够就翻页再捞）
    _progress(args.progress_out, phase="search", total=0, done=0, kept=0)
    only = [s.strip() for s in (args.provider or "").split(",") if s.strip()]
    # 甄别前多要一点：后面 rank 还要按感知哈希合并"看着是同一张"的图
    want_good = want + max(5, want // 6)
    try:
        good, bad, fstats, errs, sstats = _hunt(
            args, plan, need_text, outdir, want_good=want_good, t0=t0,
            log=_log, only=only or None)
    except _HuntError as e:
        return _fail(args.json_out, str(e), e.stage)

    if not good:
        if bad:
            return _fail(args.json_out,
                         "平铺判定筛出 %d 张，但语义甄别把 %d 张全判不合格了。"
                         "把需求说得更具体一点再试（或者 --no-screen 先看看机器"
                         "判的理由对不对，理由在 rejected.json 里）。"
                         % (fstats.get("留下", 0), len(bad)), "screen")
        return _fail(args.json_out,
                     "搜到 %d 个候选，但一张真正的平铺图都没筛出来。"
                     "换一组说法再试试（比如把花型和用途说得更具体）。"
                     % fstats["候选"], "filter")

    return _land(args, good=good, bad=bad, fstats=fstats, sstats=sstats,
                 plan=plan, need=need, outdir=outdir, t0=t0, want=want,
                 route="full", only=only, errs=errs, note=note)


def _land(args, *, good, bad, fstats, sstats, plan, need, outdir, t0,
          want=0, route="full", only=None, errs=(), note=""):
    """排序 → 留档 → 落库 → 出结果。**两条路线共用这最后一段**。

    只有一处差别：路线 1（full）把 want 当"要交差的数字"，不够就在 note 里
    报差多少；路线 2（pick）把 want 当**上限**，0 = 不限，剩多少交多少、
    一句"还差"都不说 —— 它存在的意义就是不凑数。

    为什么非要共用：落库这段有"留档必须在 store.build 之前"这种顺序坑，
    抄成两份必然有一份先删后留档，事后对着空壳排查半天。
    """
    want = max(0, int(want or 0))
    # **相似去重**：同一张图换了尺寸/水印、同一套素材包被几个站转载 ——
    # 这些在 rank 的 aHash 那一层漏得很厉害（8×8 抓不住"观感"），
    # 用户的原话是"相似度很高的图只留一个"。剔掉的那几张照样进留档，
    # 理由写"和 PF-xxx 几乎是同一张（相似度 0.998）"，可以事后核对。
    # 阈值优先级：命令行 --sim > config.json 的 filters.similar_threshold > 代码默认。
    # `filters.similar=false` 就是"这一层别做"（等价 --sim 0）。
    try:
        _fcfg = config.filters()
    except Exception:                                        # noqa: BLE001
        _fcfg = {}
    _sim_arg = float(_arg(args, "sim", 0) or 0)
    sim_near = _sim_arg or (0.0 if not _fcfg.get("similar", True)
                            else float(_fcfg.get("similar_threshold")
                                       or similar.NEAR))
    near_drop = []
    if sim_near > 0 and good:
        good, near_drop = similar.dedup(good, near=sim_near, log=_log)
        if near_drop:
            bad = list(bad) + near_drop

    cap = want if (route == "full" or want > 0) else len(good)
    _progress(args.progress_out, phase="rank", total=len(good), done=len(good),
              kept=len(good))
    qcfg = config.quality()
    picked, rstats = rank.pick(good, want=cap, cfg={
        "kinds": {n: providers.REGISTRY[n]["kind"] for n in providers.REGISTRY},
        "provider_order": list(providers.DEFAULT_ORDER),
        "intent": (plan or {}).get("intent") or "",
        "weights": {k: v for k, v in qcfg.items() if k != "fresh_days"},
        "fresh_days": int(_arg(args, "fresh_days", 0) or 0) or (qcfg.get("fresh_days") or 0)})

    # 落成花型库形状
    meta = {"need": need, "queries": plan["queries"], "negative": plan["negative"],
            "brief": plan["brief"], "engine": plan["engine"],
            "intent": plan.get("intent") or "",
            "craft": plan.get("craft") or "",
            "style_pos": plan.get("style_pos") or [],
            "style_neg": plan.get("style_neg") or [],
            "route": route,
            "providers": providers.available(only=only or None),
            "fetched": fstats, "ranked": rstats, "screened": sstats,
            "elapsed": round(time.time() - t0, 1)}
    # **留档必须在落库之前**：被剔的图这会儿还在 `_raw/` 里，store.build
    # 会把 `_raw/` 整个删掉，之后再留档就只能复制到一堆空壳。
    rej = screen.dump_rejected(bad, outdir, log=_log)
    index, paths = store.build(picked, outdir, meta=meta, log=_log)
    _progress(args.progress_out, phase="done", total=len(good), done=len(good),
              kept=len(picked), elapsed=round(time.time() - t0, 1))

    notes = [n for n in [note] if n]
    if errs:
        notes.append("有 %d 家来源没跑成：%s" % (len(errs), "；".join(errs[:2])))
    if sstats.get("note"):
        notes.append(sstats["note"])
    if route == "full":
        if len(picked) < want:
            notes.append("按你要的 %d 张，只筛出 %d 张（候选里 %d 张是重复的、"
                         "%d 张图本身不合格、%d 张没过语义甄别）"
                         % (want, len(picked), fstats["内容重复"],
                            fstats["判定不通过"], len(bad)))
    else:
        notes.append("路线 2 剩多少交多少：%d 张（剔了 %d 张，理由在 rejected.json）"
                     % (len(picked), len(bad)))
    # 接缝口径的提示：**必须报"最终交出去的那批里有多少张铺不开"**，
    # 不是"下载时有多少张没筛"。这两个数差得远（下载 193 张里 102 张不符，
    # 但甄别把大部分不符的剔了，最终 108 张里只有 50 张）—— 一开始写错成
    # 前者，话说得像"交出去的全是破的"。
    off_n = sum(1 for r in picked if r.get("tile_gate") is False)
    if off_n:
        notes.append("这 %d 张里有 %d 张接缝对不上（按你的要求没筛掉，索引里标了 "
                     "tile_gate=off，铺开会露缝，别直接拿去印花）"
                     % (len(picked), off_n))

    return _emit({
        "ok": True,
        "route": route,
        "count": len(picked),
        "dir": outdir,
        "outdir": outdir,
        "index": paths["index"],
        "brief": plan["brief"],
        "queries": plan["queries"],
        "engine": plan["engine"],
        "sources": providers.available(only=only or None),
        "stats": {"候选": fstats["候选"], "留下平铺图": fstats["留下"],
                  "内容重复": fstats["内容重复"],
                  "接缝不符(下载时未筛)": fstats.get("接缝不符但收了", 0),
                  "接缝不符(最终交付)": off_n,
                  "接缝不符(被筛掉)": fstats["判定不通过"],
                  "下载失败": fstats["下载失败"], "甄别剔除": len(bad) - len(near_drop),
                  "相似只留一张": len(near_drop),
                  "耗时秒": round(time.time() - t0, 1)},
        "screen": {"通过": sstats.get("通过"), "不是图案": sstats.get("不是图案"),
                   "不符需求": sstats.get("不符需求"),
                   "没判出来": sstats.get("没判出来"),
                   "note": sstats.get("note") or ""},
        "rejected": rej.get("count", 0),
        "site_mix": rstats["来源分布"],
        "source_kinds": rstats.get("来源类型分布") or {},
        "note": "；".join(notes),
    }, args.json_out)


def cmd_pick(args) -> int:
    """**路线 2**：检索 → 甄别 → 删除 → 剩多少张就交多少张。

    和 cmd_search（路线 1）用的是同一条产线、同一套闸门，只差一件事：
    **不为了凑数去翻页补量**。路线 1 为了凑满 100 张会一路翻到第 7~9 页，
    而后面的页基本只剩"颜色像、做法不对"的图（实测国风水彩那轮，凑满 100 张
    总共花了 52 分钟，其中大半是翻页和重判）。路线 2 只搜一轮，过完两道闸门
    剩 30 张就交 30 张 —— 快、而且质量齐整。

    `--want` 在这儿是**上限**不是任务：0（默认）= 不限，有多少合格收多少。
    """
    need = (args.need or "").strip()
    if not need and not getattr(args, "brief", ""):
        return _fail(args.json_out, "没给需求，不知道要搜什么。", "input")
    outdir = _outdir(args.outdir)
    os.makedirs(outdir, exist_ok=True)

    # 上限：0 = 不限（这一轮能筛出多少就要多少）
    cap = max(0, int(_arg(args, "want", 0) or 0))
    cand = int(_arg(args, "candidates", 0) or 0) or 300
    t0 = time.time()
    _progress(args.progress_out, phase="start", total=0, done=0, kept=0)

    # ① 需求分析 → 检索词（和路线 1 完全一样）
    _progress(args.progress_out, phase="analyze", total=0, done=0, kept=0)
    brief = {}
    if getattr(args, "brief", ""):
        try:
            brief = json.loads(args.brief)
        except Exception:                                    # noqa: BLE001
            brief = {}
    vocab = analyze.load_vocab(_arg(args, "vocab", "") or os.environ.get("WSEARCH_VOCAB") or "")
    _log("① 分析需求…")
    llm = _llm_from_args(args)
    c_args, p_args, n_args = _style(args, {})
    plan, note = analyze.plan(need, brief=brief, vocab=vocab, llm=llm,
                              n_queries=_arg(args, "queries", 8) or 8, log=_log)
    if c_args:
        plan["craft"] = c_args
    if p_args:
        plan["style_pos"] = p_args
    if n_args:
        plan["style_neg"] = n_args
    _log("   检索词 %d 条（%s）：%s" % (len(plan["queries"]), plan["engine"],
                                       " | ".join(plan["queries"][:4])))
    if plan.get("craft"):
        _log("   要的工艺/风格：「%s」—— 正例 %d 条、反例 %d 条"
             % (plan["craft"], len(plan.get("style_pos") or []),
                len(plan.get("style_neg") or [])))

    need_text = _need_text(need, plan)

    # ② 检索一轮 → 平铺闸门 → 语义甄别。**不翻页**（rounds 默认 0）
    _progress(args.progress_out, phase="search", total=0, done=0, kept=0)
    only = [s.strip() for s in (_arg(args, "provider", "") or "").split(",") if s.strip()]
    try:
        good, bad, fstats, errs, sstats = _hunt(
            args, plan, need_text, outdir, want_good=cap or cand, t0=t0,
            log=_log, only=only or None, fetch_want=cand)
    except _HuntError as e:
        return _fail(args.json_out, str(e), e.stage)

    if not good:
        # 路线 2 允许"0 张"这个结果，但一张都没有十有八九是需求说法的问题，
        # 得说清楚是哪一道闸门把它全砍了，别让人对着空目录猜。
        if bad:
            return _fail(args.json_out,
                         "这一轮平铺判定留下 %d 张，但语义甄别把它们全判不合格了"
                         "（%d 张）。换个更具体的说法再试，理由在 rejected.json 里。"
                         % (fstats.get("留下", 0), len(bad)), "screen")
        return _fail(args.json_out,
                     "搜到 %d 个候选，但一张真正的平铺图都没筛出来。换一组说法再试试。"
                     % fstats["候选"], "filter")

    return _land(args, good=good, bad=bad, fstats=fstats, sstats=sstats,
                 plan=plan, need=need, outdir=outdir, t0=t0, want=cap,
                 route="pick", only=only, errs=errs, note=note)


# --------------------------------------------------------------------- one
def cmd_one(args) -> int:
    """只判一张图（用户在对话里丢链接时用）。"""
    if not args.url:
        return _fail(args.json_out, "没给图片地址。", "input")
    outdir = _outdir(args.outdir)
    os.makedirs(outdir, exist_ok=True)
    row = {"url": args.url, "thumb": "", "title": args.title or "",
           "page": args.page or args.url, "site": "", "provider": "manual",
           "query": args.title or ""}
    # `one` 是"你点一张图，我告诉你能不能铺"—— 这里**必须保留严格判定**，
    # 它是诊断命令，不是搜集流程（搜集流程默认已经不看接缝了）。
    rec, stats = fetch.collect([row], outdir=os.path.join(outdir, "_raw"),
                               want=1, min_keep=1, tile_gate=True,
                               workers=1, timeout=args.timeout, log=_log)
    if not rec:
        # 判定没过时，把"为什么没过"如实说出来（这是用户最需要的那句话）
        return _emit({"ok": False, "seamless": False, "count": 0,
                      "error": "这张图不能四方连续铺开（%s）" % (
                          "；".join(stats.get("错误样本") or []) or "接缝对不上"),
                      "stage": "filter"}, args.json_out)
    picked, _ = rank.pick(rec, want=1)
    index, paths = store.build(picked, outdir, meta={"need": args.title or "",
                                                     "engine": "manual"})
    it = index["items"][0]
    return _emit({"ok": True, "seamless": True, "count": 1,
                  "item": it, "dir": outdir, "index": paths["index"],
                  "note": "能四方连续铺开（接缝 %s/%s）"
                          % (it["seam"]["h"], it["seam"]["v"])}, args.json_out)


# ------------------------------------------------------- screen / refill
def _existing_records(outdir):
    """把目录里已有的图读成"记录"形状（甄别只要 path，另外带上原条目）。"""
    p = os.path.join(outdir, "index.json")
    if not os.path.exists(p):
        return [], {"meta": {}, "items": []}
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:                                   # noqa: BLE001
        _log("读索引失败：%s" % e)
        return [], {"meta": {}, "items": []}
    recs = []
    for it in (d.get("items") or []):
        rel = it.get("file") or ""
        fp = os.path.join(outdir, rel) if rel else ""
        if not fp or not os.path.exists(fp):
            continue                    # 用户手工删掉的，这里就不算了
        recs.append({"path": fp, "index_item": it,
                     "w": it.get("w"), "h": it.get("h"),
                     "source": {"site": it.get("src_site"),
                                "page": it.get("src_page"),
                                "title": it.get("desc") or it.get("name"),
                                "provider": it.get("src_provider") or "",
                                "domain": it.get("src_domain") or "",
                                "author": it.get("src_author") or "",
                                "kind": it.get("src_kind") or "",
                                "quality_prior": it.get("src_quality"),
                                "created_at": it.get("src_created_at") or "",
                                "method": it.get("src_method") or "",
                                "engagement": it.get("src_engagement"),
                                "has_source": it.get("has_source"),
                                "license": it.get("license") or "unknown"}})
    return recs, {"meta": d.get("need") or {}, "items": d.get("items") or []}


def _drop_bad(outdir, bad, log=None):
    """把甄别不合格的**物理删掉**（原图 + 缩略图）。回删掉的 id。

    删文件是有意为之：用户说的就是"不符合我的要求就删掉"。想留档的话
    理由和缩略图在 rejected.json / _rejected/ 里，可以事后翻。
    """
    gone = []
    for r in bad:
        it = r.get("index_item") or {}
        for key in ("file", "thumb"):
            rel = it.get(key) or ""
            if rel:
                try:
                    os.remove(os.path.join(outdir, rel))
                except OSError:
                    pass
        if it.get("id"):
            gone.append(it["id"])
    return gone


def cmd_screen(args) -> int:
    """对**已有产物**跑一遍语义甄别：不合格的删掉，合格的保持原编号。

    单独存在的意义：用户可能只是想"再帮我过一遍"，不想重新搜。
    补图那一步是 refill 的事。
    """
    outdir = _outdir(args.outdir)
    recs, info = _existing_records(outdir)
    if not recs:
        return _emit({"ok": True, "count": 0, "removed": 0,
                      "note": "这个目录里没有可甄别的图（先跑 search）"}, args.json_out)

    need = (args.need or "").strip() or str((info.get("meta") or {}).get("need") or "")
    # 上一次落库时记下的需求/工艺/正反例要接着用：不然"再帮我过一遍"会退回
    # 按题材判，上一轮好不容易抽出来的「扎染」判据全丢了。
    plan = dict(info.get("meta") or {})
    c_args, p_args, n_args = _style(args, {})
    if not plan.get("craft") and need and not c_args:
        # 旧产物（`engine: template` 那一版）索引里没有工艺判据。现场补一份 ——
        # 不然这次甄别只能按"题材是否跑题"宽松判，等于白跑一遍。
        _log("索引里没记工艺判据，现场分析一份…")
        fresh, _n = analyze.plan(need, llm=_llm_from_args(args), log=_log)
        plan["craft"] = fresh.get("craft") or ""
        plan["style_pos"] = fresh.get("style_pos") or []
        plan["style_neg"] = fresh.get("style_neg") or []
    plan["craft"] = c_args or plan.get("craft") or ""
    if p_args:
        plan["style_pos"] = p_args
    if n_args:
        plan["style_neg"] = n_args
    t0 = time.time()
    _log("甄别 %d 张（需求：%s%s）…"
         % (len(recs), need or "未记",
            "；要的工艺「%s」" % plan["craft"] if plan.get("craft") else ""))
    good, bad, sstats = _screen(recs, args, need, plan, _log,
                               progress=lambda d, t, p: _progress(
                                   args.progress_out, phase="screen", done=d,
                                   total=t, kept=p, elapsed=round(time.time() - t0, 1)))
    rej = {}
    if bad:
        # **先留档再删**：留档要读原图，删在前就只剩空壳了（踩过）。
        rej = screen.dump_rejected(bad, outdir, log=_log)
        _drop_bad(outdir, bad, _log)
    # 重写索引（顺带清掉孤儿），并把这一轮的判词写回还在的条目上
    index, paths, st = store.append([], outdir, updates=_screen_updates(good),
                                    log=_log)

    note = sstats.get("note") or ""
    if not note:
        note = ("甄别了 %d 张，删掉 %d 张、留下 %d 张%s"
                % (len(recs), len(bad), len(good),
                   ("；被删的理由在 rejected.json 里" if bad else "")))
    return _emit({"ok": True, "count": len(index.get("items") or []),
                  "removed": len(bad), "kept": len(good),
                  "dir": outdir, "index": paths["index"],
                  "rejected": rej.get("count", 0),
                  "screen": {"不是图案": sstats.get("不是图案"),
                             "不符需求": sstats.get("不符需求"),
                             "没判出来": sstats.get("没判出来")},
                  "note": note}, args.json_out)


def cmd_refill(args) -> int:
    """把产物补满到 want 张 —— 用户说的"删掉不合适的，再补齐"就是这条。

    一条命令里做完三件事：
      ① 先甄别现有产物（默认开）：能判的先判掉，不合格的直接删；
         （用户手工删过的图这里会自动发现 —— 文件不在就算没有，
           索引里的死条目会被 store.append 清掉。）
      ② 数一下还差几张；
      ③ 差就重新检索补齐，新图同样要过"平铺闸门 + 语义甄别"，
         然后**追加**落库（已有图的编号一个都不动）。
    """
    outdir = _outdir(args.outdir)
    os.makedirs(outdir, exist_ok=True)
    t0 = time.time()
    want = max(1, int(args.want or 100))

    # ① 先甄别现有的
    recs, info = _existing_records(outdir)
    need = (args.need or "").strip() or str((info.get("meta") or {}).get("need") or "")
    if not need:
        return _fail(args.json_out,
                     "不知道要搜什么：--need 没给，目录里也没记着上次的需求。",
                     "input")

    # **判据必须在甄别之前拿到**。以前这里先用索引里记着的旧判据甄别，
    # 再重新分析需求去补图 —— 结果是"老图按老判据留下、新图按新判据进来"。
    # 更糟的情形：旧产物是"没有工艺判据"那一版落的（engine=template），
    # 于是现有 100 张按宽松判据几乎全留，补进来的却按严判据，两边不同源。
    brief = {}
    if getattr(args, "brief", ""):
        try:
            brief = json.loads(args.brief)
        except Exception:                                    # noqa: BLE001
            brief = {}
    vocab = analyze.load_vocab(args.vocab or os.environ.get("WSEARCH_VOCAB") or "")
    old = dict(info.get("meta") or {})
    llm = _llm_from_args(args)
    if llm.get("from") == "host":
        _log("需求分析用本机后端已配的对话模型（%s）" % (llm.get("model") or "?"))
    c_args, p_args, n_args = _style(args, {})
    plan, note = analyze.plan(need, brief=brief, vocab=vocab,
                              llm=llm, n_queries=args.queries, log=_log)
    # 这一轮需求分析抽不出来（没 key / 模型抽空）就沿用上一轮记下的工艺判据
    if not plan.get("craft") and old.get("craft"):
        plan["craft"] = old["craft"]
        plan["style_pos"] = plan.get("style_pos") or (old.get("style_pos") or [])
        plan["style_neg"] = plan.get("style_neg") or (old.get("style_neg") or [])
        _log("沿用上次抽出来的工艺判据「%s」" % plan["craft"])
    if c_args:
        plan["craft"] = c_args
    if p_args:
        plan["style_pos"] = p_args
    if n_args:
        plan["style_neg"] = n_args
    if plan.get("craft"):
        _log("工艺「%s」：正例 %d 条、反例 %d 条（按做法判，不按颜色）"
             % (plan["craft"], len(plan.get("style_pos") or []),
                len(plan.get("style_neg") or [])))

    removed, s1, all_bad = 0, {}, []
    ups = {}                     # 第一拨甄别的判词，最后连同补图一起写回索引
    if recs and not args.no_rescreen:
        _log("① 先甄别现有 %d 张…" % len(recs))
        good0, bad0, s1 = _screen(
            recs, args, need, plan, _log,
            progress=lambda d, t, p: _progress(
                args.progress_out, phase="screen", done=d, total=t, kept=p,
                elapsed=round(time.time() - t0, 1)))
        ups.update(_screen_updates(good0))
        if bad0:
            all_bad.extend(bad0)
            removed = len(bad0)

    # 留档 → 删 → **再数**。顺序不能换：留档要读原图（删了就没了），
    # 而数必须在删之后（不然"现有的张数"把已经被判死的也算进去，
    # 会出现"已经有 100 张了不用补"，然后交出去只有 85 张）。
    if all_bad:
        screen.dump_rejected(all_bad, outdir, log=_log)
        _drop_bad(outdir, [r for r in all_bad if r.get("index_item")], _log)

    have = len(_existing_records(outdir)[0])
    lack = want - have
    _log("② 现有 %d 张、缺 %d 张" % (have, max(0, lack)))
    if lack <= 0:
        index, paths, st = store.append([], outdir, updates=ups, log=_log)
        return _emit({"ok": True, "count": have, "have": have, "added": 0,
                      "removed": removed, "dir": outdir, "index": paths["index"],
                      "note": "已经有 %d 张了（要 %d 张），不用补。"
                              "甄别删掉了 %d 张。" % (have, want, removed)},
                     args.json_out)

    # ③ 补齐
    need_text = _need_text(need, plan)
    only = [s.strip() for s in (args.provider or "").split(",") if s.strip()]
    try:
        good, bad, fstats, errs, sstats = _hunt(
            args, plan, need_text, outdir,
            want_good=lack + max(5, lack // 6), t0=t0, log=_log,
            only=only or None)
    except _HuntError as e:
        return _fail(args.json_out, str(e), e.stage)

    # **补图要跟库里已有的比一遍**：否则补进来的很容易是"早就收过的那张
    # 换了个尺寸/水印"，用户看到的是重复图。anchors 只当锚，已有的不动。
    near_drop = []
    if good:
        try:
            _fcfg = config.filters()
        except Exception:                                    # noqa: BLE001
            _fcfg = {}
        sim_near = float(_arg(args, "sim", 0) or 0) or (
            0.0 if not _fcfg.get("similar", True)
            else float(_fcfg.get("similar_threshold") or similar.NEAR))
        if sim_near > 0:
            anchors = _existing_records(outdir)[0]     # 上面可能刚删过，重新读
            good, near_drop = similar.dedup(good, anchors=anchors,
                                            near=sim_near, log=_log)
            if near_drop:
                bad = list(bad) + near_drop

    # 补图那批被剔的图这会儿还在 `_raw/` 里（store.append 会清掉），
    # 所以留档要在 append 之前，并且**接着上一拨的清单往下记**。
    rej = screen.dump_rejected(bad, outdir, append=True, log=_log) if bad else {}

    added = 0
    if good:
        picked, rstats = rank.pick(good, want=lack, cfg={
            "kinds": {n: providers.REGISTRY[n]["kind"] for n in providers.REGISTRY},
            "provider_order": list(providers.DEFAULT_ORDER)})
        meta = {"need": need, "queries": plan["queries"], "negative": plan["negative"],
                "brief": plan["brief"], "engine": plan["engine"],
                "craft": plan.get("craft") or "",
                "style_pos": plan.get("style_pos") or [],
                "style_neg": plan.get("style_neg") or [],
                "providers": providers.available(only=only or None),
                "fetched": fstats, "ranked": rstats, "screened": sstats,
                "refill": True, "elapsed": round(time.time() - t0, 1)}
        index, paths, st = store.append(picked, outdir, meta=meta, updates=ups,
                                        log=_log)
        added = st["新增"]
    else:
        index, paths, st = store.append([], outdir, updates=ups, log=_log)

    total = len(index.get("items") or [])
    notes = []
    if errs:
        notes.append("有 %d 家来源没跑成：%s" % (len(errs), "；".join(errs[:2])))
    if sstats.get("note"):
        notes.append(sstats["note"])
    if total < want:
        notes.append("还差 %d 张没补上（两个页面都翻过了，这次搜不出更多合格的平铺图）"
                     % (want - total))
    return _emit({"ok": True, "count": total, "have": have, "added": added,
                  "removed": removed, "dir": outdir, "index": paths["index"],
                  "rejected": rej.get("count", 0),
                  "stats": {"候选": fstats.get("候选"), "甄别剔除": len(bad),
                            "耗时秒": round(time.time() - t0, 1)},
                  "screen": {"通过": sstats.get("通过"), "不是图案": sstats.get("不是图案"),
                             "不符需求": sstats.get("不符需求"),
                             "没判出来": sstats.get("没判出来")},
                  "note": "；".join(n for n in [note] + notes if n)},
                 args.json_out)


# ----------------------------------------------------------------- summary
def cmd_summary(args) -> int:
    outdir = _outdir(args.outdir)
    return _emit(store.summary(outdir), args.json_out)


# -------------------------------------------------------------------- main
def _add_hunt(p, default_rounds=3):
    """检索/下载/翻页这一组参数（search / refill / pick 共用）。

    default_rounds 给路线 2 留了个口子：它不凑数，所以默认 **不翻页**（0），
    而路线 1 默认翻 3 轮去补够 100 张。
    """
    p.add_argument("--candidates", type=int, default=0,
                   help="下载多少个候选去筛（默认 want×8；路线 2 默认 300）")
    p.add_argument("--provider", default="", help="只用这几家，逗号分隔")
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
    p.add_argument("--progress-out", default="")


def _add_vlm(p):
    """语义甄别这组参数（search / refill / screen 共用）。"""
    p.add_argument("--sim", type=float, default=0,
                   help="相似去重阈值 0~1。不给就按 config.json 的 "
                        "filters.similar_threshold（默认 %.3f）；0 = 这一次关掉"
                        % similar.NEAR)
    p.add_argument("--no-screen", action="store_true",
                   help="跳过语义甄别：只过平铺闸门，快很多但会混进实物照/展示图")
    p.add_argument("--loose", action="store_true",
                   help="语义甄别用宽松档：不按工艺严判，只剔非图案/明显跑题")
    p.add_argument("--vlm-base", default="",
                   help="视觉端点（默认本机 11435 那个）")
    p.add_argument("--vlm-model", default="")
    p.add_argument("--vlm-key", default="")
    p.add_argument("--vlm-timeout", type=int, default=0)
    p.add_argument("--vlm-max-side", type=int, default=0)
    p.add_argument("--vlm-batch", type=int, default=0,
                   help="一次判几张（默认 6；实测 6~8 最划算）")
    p.add_argument("--vlm-workers", type=int, default=1,
                   help="并发几批（默认 1；本地单卡给 2 就够）")
    # 风格判据（不给就用需求分析自动生成的那套，见 analyze.plan）
    p.add_argument("--craft", default="",
                   help="要的工艺/风格，如 扎染、蜡染、水彩。给了就把 match 判严")
    p.add_argument("--style-pos", default="",
                   help="逗号分隔：这种做法画面上长什么样（正例）")
    p.add_argument("--style-neg", default="",
                   help="逗号分隔：看着像但做法不对的样子（反例）")


def build_parser():
    p = argparse.ArgumentParser(prog="wsearch.service",
                                description="全网搜花型（子进程契约入口）")
    p.add_argument("--json-out", default="")
    sub = p.add_subparsers(dest="cmd")

    q = sub.add_parser("probe")
    q.add_argument("--redact", action="store_true")
    q.add_argument("--json-out", default="")
    q.add_argument("--llm-base", default="")
    q.add_argument("--llm-key", default="")
    q.add_argument("--llm-model", default="")
    q.add_argument("--vlm-base", default="")
    q.add_argument("--vlm-model", default="")

    s = sub.add_parser("search")
    s.add_argument("--need", default="")
    s.add_argument("--brief", default="")
    s.add_argument("--outdir", default="")
    s.add_argument("--want", type=int, default=100)
    _add_hunt(s)
    _add_vlm(s)
    s.add_argument("--llm-base", default="")
    s.add_argument("--llm-key", default="")
    s.add_argument("--llm-model", default="")
    s.add_argument("--json-out", default="")

    r = sub.add_parser("refill")
    r.add_argument("--need", default="")
    r.add_argument("--brief", default="")
    r.add_argument("--outdir", default="")
    r.add_argument("--want", type=int, default=100)
    r.add_argument("--no-rescreen", action="store_true",
                   help="不对已有产物重跑甄别，只补缺的那几张")
    _add_hunt(r)
    _add_vlm(r)
    r.add_argument("--llm-base", default="")
    r.add_argument("--llm-key", default="")
    r.add_argument("--llm-model", default="")
    r.add_argument("--json-out", default="")

    k = sub.add_parser("pick", help="路线 2：检索→甄别→删除，剩多少交多少（不凑数）")
    k.add_argument("--need", default="")
    k.add_argument("--brief", default="")
    k.add_argument("--outdir", default="")
    k.add_argument("--want", type=int, default=0,
                   help="**上限**，不是任务（默认 0 = 不限，有多少合格收多少）")
    _add_hunt(k, default_rounds=0)
    _add_vlm(k)
    k.add_argument("--llm-base", default="")
    k.add_argument("--llm-key", default="")
    k.add_argument("--llm-model", default="")
    k.add_argument("--json-out", default="")

    c = sub.add_parser("screen")
    c.add_argument("--need", default="")
    c.add_argument("--outdir", default="")
    c.add_argument("--progress-out", default="")
    _add_vlm(c)
    c.add_argument("--json-out", default="")

    o = sub.add_parser("one")
    o.add_argument("--url", default="")
    o.add_argument("--title", default="")
    o.add_argument("--page", default="")
    o.add_argument("--outdir", default="")
    o.add_argument("--timeout", type=int, default=25)
    o.add_argument("--json-out", default="")

    m = sub.add_parser("summary")
    m.add_argument("--outdir", default="")
    m.add_argument("--json-out", default="")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not args.cmd:
        build_parser().print_help()
        return 2
    fn = {"probe": cmd_probe, "search": cmd_search, "one": cmd_one,
          "summary": cmd_summary, "screen": cmd_screen, "refill": cmd_refill,
          "pick": cmd_pick}[args.cmd]
    try:
        return fn(args)
    except KeyboardInterrupt:
        return _fail(getattr(args, "json_out", ""), "被中断了。", "interrupt")
    except Exception as e:                                   # noqa: BLE001
        _log(traceback.format_exc())
        return _fail(getattr(args, "json_out", ""),
                     "出错了（%s：%s）" % (type(e).__name__, e), "crash")


if __name__ == "__main__":
    sys.exit(main())
