"""AI 打标签：让视觉模型给一张待入库的图建档。

跟 core/vision.py 的分工：
    vision.py 服务于**对话里的识图**——用户传张图，读出能写进需求卡的标签，
             词表外的值一律不采信（怕污染需求卡）。
    tagger.py 服务于**入库建档**——要的是一条完整档案：名字、描述、六维标签、
             主色、关键词、适合面料。词表外的颜色名**照收**（"淡红""藕荷粉"
             这些客户真会说的词必须留住），只是标记出来。

两条链路共用一件事：**模型只负责"看"，采信与否由这里的确定性规则定。**
模型给的值先在词表里归一，归不上就留原话，绝不硬套成"最接近的那一档"。
"""
import re

from .llm import LLMClient, LLMError
from .vision import normalize, _parse_json, EXTRA_VOCAB

# 受控维度 → 归一用的词表（词表外的值进 rejected，不写进标签）
CONTROLLED = ('element', 'color', 'style', 'scene')
PROD = ('composition', 'density', 'scale')

DIM_LABEL = {
    'element': '元素', 'color': '配色', 'style': '风格', 'scene': '场景',
    'composition': '构图', 'density': '密度', 'scale': '尺度',
}

# 字段输出顺序：预算不够被砍的是尾巴，所以判断性的字段必须排最前
FIELD_ORDER = ('is_pattern', 'name', 'element', 'color', 'style', 'scene',
               'composition', 'density', 'scale', 'colors', 'keywords',
               'fabric', 'desc', 'reason')

MAX_NAME = 10
MAX_DESC = 80
MAX_KEYWORDS = 6
MAX_COLORS = 4

# 模型爱把数组写成字符串（"蓝、白"或"蓝,白"），统一拆开
_SPLIT = re.compile(r'[、,，/|;；\s]+')


def _as_list(val, limit):
    if val is None:
        return []
    if isinstance(val, str):
        parts = [x.strip() for x in _SPLIT.split(val)]
    elif isinstance(val, (list, tuple)):
        parts = []
        for x in val:
            parts.extend(_SPLIT.split(str(x)))
    else:
        parts = [str(val)]
    out = []
    for p in parts:
        p = p.strip().strip('"\'·。')
        if p and p not in out:
            out.append(p)
    return out[:limit]


def _clean_text(val, limit):
    if val is None:
        return ''
    s = str(val).strip().strip('"\'')
    s = s.replace('\n', ' ')
    return s[:limit]


class Tagger:
    """给一张图打标签。模型不可用时 enabled=False，调用方自己决定怎么兜底。"""

    def __init__(self, config, vocab=None):
        self.cfg = config
        self.vocab = vocab or {}
        self.llm = LLMClient(config)

    @property
    def conf(self):
        return (self.cfg.settings.get('vision') or {}) if self.cfg is not None else {}

    @property
    def enabled(self):
        return bool(self.conf.get('enabled', True)) and self.llm.enabled

    def _vocab(self, dim):
        if dim in PROD:
            return (self.vocab or {}).get(dim) or EXTRA_VOCAB[dim]
        return (self.vocab or {}).get(dim) or []

    def describe(self):
        return {"enabled": self.enabled, "model": self.conf.get('model') or self.cfg.model,
                "max_tokens": int(self.conf.get('max_tokens', 16000))}

    # ---------- 提示词 ----------
    def _prompt(self):
        L = ['is_pattern（这张图本身是不是一块面料/花型/图案？'
             '软件截图、人物、室内实景、纯文字、表格一律 false）',
             'name（给这个花型起一个中文名，4~6 个字，像设计师给作品起名那样，'
             '要有画面感、不要"花卉图案一"这种流水号）']
        for d in ('element', 'color', 'style', 'scene'):
            L.append('%s（只能从这些里选一个：%s）' % (d, '、'.join(self._vocab(d))))
        for d in PROD:
            L.append('%s（只能从这些里选一个：%s）' % (d, '、'.join(self._vocab(d))))
        L.append('colors（画面主要用色 2~4 个中文色名，可以用"淡红""藕荷粉""雾霾蓝"'
                 '这类口语说法，不必迁就上面的分类）')
        L.append('keywords（3~6 个中文检索关键词，写客户可能会怎么描述它，'
                 '比如"小碎花""显瘦""通勤""对花""适合童装"）')
        L.append('fabric（适合的面料，一两个词，如"雪纺""棉麻"，看不出来留空）')
        L.append('desc（一句话 30~50 字，说清画面里有什么、怎么排的、什么调性）')
        L.append('reason（20 字以内，你为什么这么判断）')

        return ('这是一张准备收进面料花型库的图，请看图给它建一份档案。\n'
                '**只输出一行 JSON，不要任何解释文字**，字段如下：\n'
                + '\n'.join('- ' + x for x in L) +
                '\n\n注意：\n'
                '1. is_pattern 为 false 时其余字段随便给，我会忽略。\n'
                '2. 不要根据图里的**文字**去猜花型，只看画面本身。\n'
                '3. 名称和描述要基于你真的看到的东西，宁可朴素也别编。\n'
                '4. 不确定的维度选最接近的那一个，不要留空（fabric 除外）。')

    def _call(self, image_url):
        extra = {}
        if self.conf.get('effort'):
            extra['effort'] = self.conf['effort']
        return self.llm.chat(
            [{'role': 'user', 'content': [
                {'type': 'text', 'text': self._prompt()},
                {'type': 'image_url', 'image_url': {'url': image_url}}]}],
            tools=None,
            temperature=self.conf.get('temperature', 0),
            max_tokens=int(self.conf.get('max_tokens', 16000)),
            model=self.conf.get('model') or self.cfg.model,
            timeout=int(self.conf.get('timeout', 180)),
            extra=extra,
        )

    # ---------- 主入口 ----------
    def tag(self, image_b64, mime='image/png'):
        """返回结构化档案。抛异常交给调用方兜（库那边会把图照样存下）。"""
        if not self.enabled:
            raise RuntimeError('打标签不可用（没配 api_key 或 vision.enabled=false）')
        if not image_b64:
            raise RuntimeError('没有图片数据')

        url = image_b64 if image_b64.startswith('data:') \
            else 'data:%s;base64,%s' % (mime or 'image/png', image_b64)

        parsed, truncated, res = None, False, None
        tries = int(self.conf.get('retries', 1)) + 1
        last = ''
        for _ in range(tries):
            res = self._call(url)
            last = res.get('content') or ''
            parsed, truncated = _parse_json(last)
            if parsed:
                break
        if not parsed:
            raise RuntimeError('没返回可解析的 JSON（%d 次都失败）：%s' % (tries, last[:120]))

        reason = _clean_text(parsed.get('reason'), 40)
        if parsed.get('is_pattern') is False or str(parsed.get('is_pattern')).lower() == 'false':
            return {"ok": False, "not_pattern": True, "reason": reason,
                    "raw": parsed, "usage": res.get('usage')}

        out = {"ok": True, "reason": reason, "truncated": truncated,
               "raw": parsed, "usage": res.get('usage'), "rejected": {}}
        out['name'] = _clean_text(parsed.get('name'), MAX_NAME)
        out['desc'] = _clean_text(parsed.get('desc'), MAX_DESC)
        out['fabric'] = _clean_text(parsed.get('fabric'), 12)
        # 主色与关键词是**自由字段**：客户怎么说就怎么留，不做词表校验
        out['colors'] = _as_list(parsed.get('colors'), MAX_COLORS)
        out['keywords'] = _as_list(parsed.get('keywords'), MAX_KEYWORDS)

        for dim in CONTROLLED + PROD:
            raw_v = parsed.get(dim)
            v = normalize(raw_v, dim, self._vocab(dim))
            out[dim] = v
            # 词表外的值不硬套，但记下来（页面上能看到"AI 说的是什么"）
            if not v and raw_v and dim in CONTROLLED:
                out['rejected'][dim] = _clean_text(raw_v, 12)
        return out

    def tag_file(self, path):
        import base64
        import os
        from .imagegen import sniff
        with open(path, 'rb') as f:
            blob = f.read()
        return self.tag(base64.b64encode(blob).decode(), sniff(blob)[1])
