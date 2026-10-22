"""需求分析：把用户的一句人话变成**一组能命中平铺图的检索词**。

这一段是用户点名的核心诉求
    "通过 ai 分析用户需求后针对用户的需求进行一个全网的搜索" —— 需求分析
    就是这里。它不只是把话拆成四个维度，更要紧的是**换成搜索站的语法**。

为什么不能直接拿用户原话去搜
    用户会说"想要国风的花，做连衣裙"。直接把这句话丢进图片搜索，
    回来的多半是**穿着国风连衣裙的模特照** —— 那是成品服装图，不是花型。
    要拿到花型，检索词里必须出现图案行业的词（无缝、平铺、四方连续、
    tileable、seamless pattern、repeat print…），还要**主动排掉**会招来
    成品照的词（连衣裙、模特、成衣、海报、壁纸效果图）。

两层兜底（没有大模型也能跑）
    没有配对话模型的 key 时，退化成"原话 + 行业词模板"拼几条检索词。
    质量差不少，但整条产线照样能出图 —— 这是刻意的：算法不该因为
    一个可选依赖没配就完全不可用。

维度的用法
    分析出的 style/element/color/scene 不只是给检索用，后面落库时会写进
    和本地花型库**同样的字段**（见 store.py），所以客户在库里筛"蓝白青花"
    能筛到全网搜回来的这些图。词表（--vocab，默认用后端的
    后端/data/vocab.json）的作用和自动打标签一致：**能归一就归一，
    归不上就留原话，绝不硬套最近档**。
"""
from __future__ import annotations

import json
import os
import re

from . import net

# 检索词里必须带的"行业词"。这些词是平铺图和成品照的分水岭 ——
# 带上了，搜索站才会把"图案素材"端出来，而不是"穿着这件料子的模特"。
SEAM_WORDS_EN = ("seamless pattern", "tileable pattern repeat", "textile print pattern")
SEAM_WORDS_CN = ("无缝 平铺 图案", "四方连续 花型 面料", "印花 平铺 素材")

# 主动排掉：这些词是成品照/效果图的入口
DEFAULT_NEGATIVE = ("连衣裙", "模特", "成衣", "服装效果图", "lookbook",
                    "mockup", "model wearing", "poster", "t-shirt print")

# 没有大模型时的兜底模板
FALLBACK_SUFFIX = {
    "en": " seamless pattern tileable repeat textile print",
    "cn": " 无缝平铺 四方连续 花型 图案 素材",
}


def _extract_json(text):
    """从模型回复里抠出 JSON。模型爱加 ```json 围栏和前后废话。"""
    if not text:
        return None
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.+?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except Exception:                                        # noqa: BLE001
        pass
    # 退一步：抓最外层的一对花括号
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(t[i:j + 1])
        except Exception:                                    # noqa: BLE001
            return None
    return None


def _dedupe(seq, cap=None):
    out, seen = [], set()
    for s in seq or []:
        s = (s or "").strip()
        if not s or s.lower() in seen:
            continue
        seen.add(s.lower())
        out.append(s)
    return out[:cap] if cap else out


def _prompt(text, brief, n_queries, vocab):
    dims = ""
    if vocab:
        for k, label in (("style", "风格"), ("element", "元素"),
                         ("color", "配色"), ("scene", "适用场景")):
            vals = vocab.get(k) or []
            if vals:
                dims += "\n  %s（优先从这些里选，选不中就用你自己的说法）：%s" % (
                    label, "、".join(vals))
    ctx = ""
    if brief:
        got = {k: v for k, v in brief.items() if v}
        if got:
            ctx = "\n【已经聊出来的条件】%s" % json.dumps(got, ensure_ascii=False)

    return """你在帮一个面料花型平台做**图片检索**。用户想要的是"花型图案本身"，
不是穿着这个花型的衣服、也不是印着它的海报。你的任务是把他的需求变成
%s 组可以直接丢进图片搜索的检索词。

【用户说的话】
%s%s

【硬要求】
1. 每组检索词都必须带上"图案/平铺"这类行业词（seamless、tileable、repeat、
   无缝、平铺、四方连续、印花素材…），否则搜回来的是成品服装照。
2. 中英文各占一半：中文词捞国内站（花瓣、千图、素材站），英文词捞
   Pinterest / Etsy / 素材站。英文那几条可以带 site: 限定，例如
   site:pinterest.com、site:etsy.com、site:freepik.com。
3. **不要**出现这些词（它们招来的都是成品照）：连衣裙、模特、成衣、效果图、
   mockup、model wearing、poster。把该排除的写进 negative，不要写进 queries。
4. 检索词要**具体**：把颜色、元素、风格、工艺（水彩/线描/矢量/复古）落到词里，
   不要只写"花型"两个字。
5. **工艺 / 风格要单独抽出来**。用户说"扎染""蜡染""蓝印花布""水彩""刺绣"这类
   是**做法**，跟"蓝白""花卉"这类题材不是一回事 —— 同一个题材可以是很多种做法，
   光靠颜色分不开。检索词里必须有做法本身的词（扎染还要带 shibori / tie dye /
   indigo resist 这类英文行话），否则搜回来全是"颜色像、做法不对"的图。
6. **文化圈风格要落到那个圈子的具体元素**。用户说"国风""中式""新中式"时，只写
   "水彩花卉"会捞回一堆**欧美复古水彩**（图库里通用水彩占绝对多数）。国风的检索词
   必须带：国画、水墨、工笔、白描、青花、折枝留白、团花缠枝，题材落到牡丹、梅、兰、
   竹、菊、荷、海棠、玉兰、山茶；英文用 chinoiserie / oriental / asian ink wash。
   并且 **brief.style 要写成"国风"或"新中式"** —— 后面看图甄别靠这个字段挡西式图。

【craft / style_pos / style_neg 怎么写】（这一段是给后面的看图甄别用的）
craft：用户指定的工艺或风格，一个词到四个字，抽不出来就留空。
style_pos：**这种做法的画面上长什么样**，2~5 条，每条都得是眼睛能看见的特征
  （比如扎染：晕染开的不规则圈纹、颜色互相渗开的软边、绑扎留下的放射折痕、
  靛蓝底上留白）。**不许写"有国风感""有艺术气息"这种看不出来的空话。**
style_neg：**同一个题材、同一个色系，但做法明显不对的样子**，2~5 条
  （比如扎染的反面：规整的格纹菱形、佩斯利腰果花、曼陀罗对称放射、
  蓝印花布那种硬边漏印、电脑矢量插画、水彩手绘花卉）。
  这一条最要紧：用户最常抱怨的就是"颜色对、工艺不对"。
craft 抽不出来（用户只说了题材没说做法）时，三个字段都给空。

只输出 JSON，不要别的话：
{
  "brief": {"style": "", "element": "", "color": "", "scene": ""},%s
  "craft": "",
  "style_pos": ["...", "..."],
  "style_neg": ["...", "..."],
  "queries": ["...", "..."],
  "negative": ["...", "..."]
}""" % (n_queries, text, ctx, dims)


def _ask(url, payload, headers, timeout, budget, retry=32000, log=None):
    """发一次 chat/completions，**被截断就加预算重试一次**。

    为什么必须处理这个：本机后端配的是推理模型（deepseek-flash 这类），
    思考过程也吃 max_tokens。给 1600 的时候 finish_reason 直接是
    `length`、content 是空字符串 —— 表现就是"需求分析没跑成，退回模板"，
    而且**不报错**，静默降级。整条产线于是从"AI 分析需求"退回"原话 +
    无缝平铺"，扎染的工艺词一个都没有。这是项目里踩过的老坑，别再犯。
    """
    payload = dict(payload)
    payload["max_tokens"] = int(budget)
    j = net.post_json(url, payload, headers=headers, timeout=timeout)
    ch = (j.get("choices") or [{}])[0] or {}
    content = ((ch.get("message") or {}).get("content") or "").strip()
    if not content and retry and int(budget) < retry:
        if log:
            log("· 需求分析输出被截断（思考占满预算），加到 %d 重试一次" % retry)
        payload["max_tokens"] = int(retry)
        j = net.post_json(url, payload, headers=headers,
                          timeout=max(timeout, timeout * retry // max(int(budget), 1)))
        ch = (j.get("choices") or [{}])[0] or {}
        content = ((ch.get("message") or {}).get("content") or "").strip()
    return content


# ---- 意图识别（2026-10-19）------------------------------------------------
# 用来给"来源先验/排序"分流：找可印花素材、找风格灵感、按具体花名找，
# 三类需求对应的优质来源不一样（Pinterest 的供给结构也是随查询类型变的）。
BUSINESS_HINTS = ("面料", "印花", "花型", "平铺", "四方连续", "无缝", "打样",
                  "开版", "生产", "客户", "订单", "图案库", "素材", "可印",
                  "批量", "稿子", "花稿", "版", "印染", "纺织")
INSPIRATION_HINTS = ("灵感", "风格", "参考", "好看", "壁纸", "穿搭", "氛围",
                     "设计感", "配色", "pinterest", "ins", "mood", "参考图")
SPECIFIC_HINTS = ("牡丹", "玫瑰", "樱花", "百合", "梅花", "兰花", "竹子", "竹",
                  "菊花", "荷花", "郁金香", "向日葵", "山茶", "玉兰", "海棠",
                  "花名", "具体")


def detect_intent(text, brief=None):
    """粗判意图：business / inspiration / specific。

    这是**排序用的软信号**，不是硬门槛；判错最多影响来源权重，不会丢图。
    默认 business：这个平台的主用户是来找可印花花型的。
    """
    s = (text or "") + " " + " ".join(str(v) for v in (brief or {}).values())
    biz = sum(1 for w in BUSINESS_HINTS if w in s)
    ins = sum(1 for w in INSPIRATION_HINTS if w in s)
    spec = sum(1 for w in SPECIFIC_HINTS if w in s)
    if spec >= 1 and biz == 0:
        return "specific"
    if ins > biz:
        return "inspiration"
    return "business"


def plan(text, *, brief=None, vocab=None, llm=None, n_queries=8, timeout=60,
         log=None):
    """把需求变成检索计划。回 (计划 dict, note 字符串)。

    计划形状
        {"brief": {...4 维...}, "queries": [...], "negative": [...],
         "craft": "扎染", "style_pos": [...], "style_neg": [...],
         "engine": "llm" | "template"}

    llm 是 {"base":..., "key":..., "model":...}；key 为空就退化成模板拼接。
    永不抛异常 —— 检索计划失败不该让整次搜索失败。
    """
    text = (text or "").strip()
    queries, negative, got, engine = [], list(DEFAULT_NEGATIVE), {}, "template"
    craft, style_pos, style_neg = "", [], []

    base = dict(brief or {})
    for k in ("style", "element", "color", "scene"):
        if base.get(k):
            got[k] = base[k]

    if text and llm and llm.get("key"):
        try:
            url = (llm.get("base") or "").rstrip("/") + "/chat/completions"
            payload = {
                "model": llm.get("model") or "deepseek-chat",
                "messages": [{"role": "user",
                              "content": _prompt(text, brief, n_queries, vocab)}],
                "temperature": 0.3,
            }
            if llm.get("thinking") is False:
                payload["thinking"] = {"type": "disabled"}
            content = _ask(url, payload,
                           headers={"Authorization": "Bearer %s" % llm["key"]},
                           timeout=timeout,
                           budget=llm.get("max_tokens") or 4000, log=log)
            data = _extract_json(content)
            if data:
                queries = _dedupe(data.get("queries"))
                neg2 = _dedupe(data.get("negative"))
                b = data.get("brief") or {}
                for k in ("style", "element", "color", "scene"):
                    if b.get(k):
                        got[k] = b[k]
                if neg2:
                    negative = _dedupe(negative + neg2)
                # 工艺与正反例：给看图甄别当判据（见 screen.build_prompt）
                craft = str(data.get("craft") or "").strip()[:12]
                style_pos = _dedupe(data.get("style_pos"), cap=5)
                style_neg = _dedupe(data.get("style_neg"), cap=5)
                if queries:
                    engine = "llm"
        except Exception as e:                               # noqa: BLE001
            engine = "template"
            neg2 = None
            note_err = str(e)
        else:
            note_err = None
    else:
        note_err = None if text else "没有给出需求，按通用花型搜"

    # 兜底 / 补足：不管模型给没给，最后再拼一条"原话 + 行业词"的保险词
    if text:
        queries.append(text + FALLBACK_SUFFIX["cn"])
        queries.append(text + FALLBACK_SUFFIX["en"])
        if engine == "template":
            # 模板模式只拼一条原话的话，一次搜索最多也就百来个候选 ——
            # 而平铺闸门会筛掉七成以上，**凑不到用户要的 100 张**。
            # 所以这里把关键词和几条行业词分别组合，多开几路：
            # 一条检索词在一家引擎上只能捞 60 条，八条才有得挑。
            core = " ".join(to_keywords(text)[:3]) or text
            for w in SEAM_WORDS_CN + SEAM_WORDS_EN:
                queries.append("%s %s" % (core, w))
    if not queries:
        queries = list(SEAM_WORDS_CN) + list(SEAM_WORDS_EN)
    queries = _dedupe(queries, cap=max(n_queries, 2) + 2)

    note = ""
    if engine == "template":
        if note_err:
            note = "需求分析没跑成（%s），先用关键词直接检索。" % note_err
        elif not (llm and llm.get("key")):
            note = ("没配对话模型的 key，先用关键词直接检索 —— 检索词里只有"
                    "「原话 + 行业词」，缺工艺词，命中率会明显偏低。")
    elif not craft:
        note = ("需求里没听出具体工艺/做法（只说了题材），甄别只能按题材对，"
                "「颜色对、做法不对」那类图会漏进来。")
    intent = detect_intent(text, got)
    return ({"brief": got, "queries": queries, "negative": negative,
             "craft": craft, "style_pos": style_pos, "style_neg": style_neg,
             "intent": intent, "engine": engine}, note)


def to_keywords(text):
    """把一句话粗略切成关键词（兜底排序用，不追求分词质量）。"""
    t = re.sub(r"[，,。.、；;：:!！?？\s]+", " ", text or "")
    return [w for w in t.split(" ") if len(w) >= 2][:8]


def load_vocab(path):
    """读词表。读不到就返回 {}（不报错 —— 词表只是"能归一就归一"）。"""
    if not path:
        return {}
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:                                        # noqa: BLE001
        return {}
