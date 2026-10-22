"""解析与归一：把模型吐出来的东西变成能落盘的干净标签。

这一层的职责边界跟主程序一致 —— **模型只负责"看"，采信与否在这里定**。

处理的现实问题（都是视觉模型打标签时的常态）：
  - JSON 被 ``` 围起来，或者前后带一句"好的，我看到了..."；
  - 数组被写成字符串（"花卉、叶片"而不是 ["花卉","叶片"]）；
  - 四维里有一维它想不到，于是硬填一个词凑数 —— 所以空数组是**允许且正常**的；
  - 输出被 token 预算截断，只吐了一半 —— 得把已经吐出来的捞回来；
  - 图上有文字，模型把文字当指令去执行了 —— 输出里会留痕迹，标记出来等人看。
"""
import json
import re

from .taxonomy import (
    DIMS, MAX_PER_DIM, MAX_COLORS, MAX_KEYWORDS,
    normalize_many, _as_list,
)

MAX_DESC = 80
MAX_REASON = 40
MAX_COLORS_N = MAX_COLORS
MAX_KEYWORDS_N = MAX_KEYWORDS

# 模型把 JSON 包在 ``` 里
_FENCE = re.compile(r'^```(?:json)?\s*|\s*```$')

# 指令式措辞。真在打标签的模型不会说这些；出现了多半是它把图上文字
# 当成了要执行的命令。命中就标 suspect，不阻断落盘 —— 交给人看。
_INJECTION = re.compile(
    r'忽略(以上|之前|前面|上述)|无视(以上|之前|前面)|你现在是|你是一个|'
    r'请(执行|立刻|立即)|system\s*[:：]|assistant\s*[:：]|'
    r'ignore\s+(the\s+)?(above|previous)|you\s+are\s+now|disregard',
    re.I,
)


def _strip_fences(text):
    t = (text or '').strip()
    t = _FENCE.sub('', t)
    return t.strip()


def _salvage(text):
    """JSON 被截断时把已经吐出来的字段捞回来。

    字段是按固定顺序写的，预算不够只会砍掉尾巴（通常是 reason/desc）。
    所以先捞完整的数组和字符串，再处理"数组开了头没闭合"的情况。
    """
    out = {}

    # 1) 完整数组："style": ["a", "b"]
    for k, body in re.findall(r'"([A-Za-z_]\w*)"\s*:\s*\[([^\]]*)\]', text):
        if k in out:
            continue
        out[k] = [s for s in re.findall(r'"([^"]*)"', body)] or \
                 [s for s in re.findall(r'([^",\[\]]+)', body) if s.strip()]

    # 2) 未闭合的数组（被截断在数组中间）：从 "[ 后面一直捞到结尾
    for k, body in re.findall(r'"([A-Za-z_]\w*)"\s*:\s*\[([^\[\]]*)$', text):
        if k in out:
            continue
        vals = [s for s in re.findall(r'"([^"]*)"', body)]
        if not vals:
            vals = [s.strip() for s in body.split(',') if s.strip()]
        if vals:
            out[k] = vals

    # 3) 标量字段："desc": "..."
    for k, v in re.findall(r'"([A-Za-z_]\w*)"\s*:\s*"([^"]*)"', text):
        if k not in out:
            out[k] = v

    # 4) 布尔与数字
    if 'is_pattern' not in out:
        m = re.search(r'"is_pattern"\s*:\s*(true|false)', text, re.I)
        if m:
            out['is_pattern'] = m.group(1).lower() == 'true'
    for k in ('confidence',):
        if k not in out:
            m = re.search(r'"%s"\s*:\s*(-?[\d.]+)' % k, text)
            if m:
                try:
                    out[k] = float(m.group(1))
                except ValueError:
                    pass
    return out or None


def parse_json(text):
    """返回 (dict, 是否被截断救回)。"""
    t = _strip_fences(text)
    if not t:
        return None, False
    try:
        d = json.loads(t)
        return (d, False) if isinstance(d, dict) else (None, False)
    except Exception:                                     # noqa: BLE001
        pass
    i, j = t.find('{'), t.rfind('}')
    if i >= 0 and j > i:
        try:
            d = json.loads(t[i:j + 1])
            if isinstance(d, dict):
                return d, False
        except Exception:                                 # noqa: BLE001
            pass
    s = _salvage(t)
    return (s, True) if s else (None, False)


def _truthy(v):
    """is_pattern 可能是 true / "true" / "True" / 1 / "是"。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return bool(v)
    s = str(v).strip().lower()
    if s in ('true', '1', 'yes', '是', 'y'):
        return True
    if s in ('false', '0', 'no', '否', 'n'):
        return False
    return None


def _clean_text(v, limit):
    if v is None:
        return ''
    s = str(v).strip().strip('"\'')
    s = re.sub(r'\s+', ' ', s)
    return s[:limit]


def _confidence(v):
    """模型给"80%"、"0.8"、0.8 都要吃下。"""
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        c = float(v)
    else:
        s = str(v).strip().replace('%', '')
        try:
            c = float(s)
        except ValueError:
            return None
    # 数字和字符串走**同一套换算**：模型有时给 0.8、有时给 80（当成百分数），
    # 两条路径必须一致，否则 "80" 会被 min() 夹成 1.0，看板上就成了"100% 把握"。
    if c > 1:
        c = c / 100.0
    if c < 0:
        return None
    return round(min(c, 1.0), 3)


def structure(parsed, vocab, truncated=False):
    """把模型返回的 dict 变成落盘用的结构。

    返回：
        ok          解析到了东西没有
        is_pattern  是不是花型图（None = 模型没说清）
        tags        四维里**归到词表内**的值 {dim: [值...]}
        free        四维里**没归上**的原话 {dim: [原话...]}，留着做新词发现
        colors      自由色名
        keywords    检索词
        desc/reason/confidence
        suspect     输出里有没有指令式措辞（图上有字被当指令了）
    """
    if not parsed:
        return {'ok': False}

    is_pat = _truthy(parsed.get('is_pattern'))
    suspect = bool(_INJECTION.search(json.dumps(parsed, ensure_ascii=False)))

    out = {
        'ok': True,
        'is_pattern': is_pat,
        'tags': {},
        'free': {},
        'colors': _as_list(parsed.get('colors'))[:MAX_COLORS_N],
        'keywords': _as_list(parsed.get('keywords'))[:MAX_KEYWORDS_N],
        'desc': _clean_text(parsed.get('desc'), MAX_DESC),
        'reason': _clean_text(parsed.get('reason'), MAX_REASON),
        'confidence': _confidence(parsed.get('confidence')),
        'truncated': bool(truncated),
        'suspect': suspect,
        # 只留白名单字段，模型自己加的一律丢掉（防注入的一部分：
        # 别让它往结构里塞"action"这类能影响下游的键）
        'raw': {k: v for k, v in parsed.items()
                if k in ('is_pattern', 'confidence', 'desc', 'reason')},
    }

    if is_pat is False:
        # 不是花型图，四维全空 —— 不硬套
        for d in DIMS:
            out['tags'][d] = []
            out['free'][d] = []
        return out

    for d in DIMS:
        hit, free = normalize_many(parsed.get(d), d, vocab, MAX_PER_DIM)
        out['tags'][d] = hit
        out['free'][d] = free
    return out


def tag_any(tags):
    """四个维度加起来有没有标签。用来判断"这张图到底打上标没有"。"""
    return any((tags or {}).get(d) for d in DIMS)
