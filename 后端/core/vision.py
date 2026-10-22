"""识图：让视觉模型读一张花型图，输出受控词表内的结构化标签。

设计原则（跟整套系统一致）：
    模型只负责"看懂"，"采信哪些维度"由这里的确定性规则决定。

    - 提示词里把每个维度的候选值全列出来，逼模型在词表内作答；
    - 回来之后用 normalize() 做归一校验，模型说了词表外的值就记下来但不采信；
    - 哪些维度能直接写进需求卡、哪些只能当建议，由 STABLE / SUGGEST_ONLY 决定
      —— 这是实测出来的：元素/配色/构图/密度/尺度稳定，风格会摇摆。

图片以 base64 传入（带或不带 data: 前缀都可以）。
"""
import base64
import json
import re

from .llm import LLMClient, LLMError

# 能直接写进需求卡的维度（实测 4/4 稳定）
STABLE = ['element', 'color']
# 只能当建议、必须让用户确认的维度（实测风格会在「法式浪漫 / 新中式国风」之间摇摆）
SUGGEST_ONLY = ['style']
# 图上看不出来、必须问用户的维度
CANNOT_INFER = ['scene']
# 生产维度（PRD 6.1 的构图/密度/尺度），先存起来，等需求卡加维时直接用
EXTRA = ['composition', 'density', 'scale']

DIM_LABEL = {
    'style': '风格', 'element': '元素', 'color': '配色', 'scene': '场景',
    'composition': '构图与排列', 'density': '密度', 'scale': '尺度',
}

EXTRA_VOCAB = {
    'composition': ['散点', '满地', '定位', '条纹', '边花'],
    'density': ['疏', '中', '密'],
    'scale': ['小碎花', '中花', '大花'],
}

# 输出顺序很关键：字段按这个顺序吐，预算不够被砍的是尾巴，
# 所以 is_pattern 必须排第一，六个维度标签排在 reason 之前。
SUGGESTED_ORDER = SUGGEST_ONLY + STABLE + EXTRA

# 模型爱说"散点错落排列""稀疏""小"这类口语值，这里映射回词表
ALIASES = {
    'composition': {'散布': '散点', '散落': '散点', '散点式': '散点', '错落': '散点',
                    '满底': '满地', '铺满': '满地', '满地式': '满地', '连续': '满地',
                    '定位花': '定位', '独幅': '定位', '单独': '定位',
                    '条状': '条纹', '竖条': '条纹', '横条': '条纹', '条格': '条纹',
                    '边饰': '边花', '边框': '边花'},
    'density': {'稀疏': '疏', '低密度': '疏', '留白多': '疏', '疏朗': '疏',
                '中等': '中', '中密': '中', '适中': '中', '中密度': '中',
                '密集': '密', '高密': '密', '高密度': '密', '紧凑': '密'},
    'scale': {'小': '小碎花', '小花': '小碎花', '小尺度': '小碎花', '碎花': '小碎花',
              '中': '中花', '中等': '中花', '中尺度': '中花', '中朵': '中花',
              '大': '大花', '大朵': '大花', '大尺度': '大花', '大花型': '大花'},
}

VISION_TIMEOUT = 180


def _strip_fences(text):
    """模型经常把 JSON 包在 ```json ... ``` 里，这里剥掉。"""
    t = (text or '').strip()
    t = re.sub(r'^```(?:json)?\s*', '', t)
    t = re.sub(r'\s*```$', '', t)
    return t.strip()


def _salvage(text):
    """JSON 被截断时的救回逻辑。

    字段按固定顺序输出，预算不够只会砍掉尾巴（通常是 reason），
    所以这里用正则把已经吐出来的 "键":"值" 全捞回来，能救多少救多少。
    """
    out = {}
    for k, v in re.findall(r'"([A-Za-z_]\w*)"\s*:\s*"([^"]*)"', text):
        if k not in out:
            out[k] = v
    for k, v in re.findall(r'"([A-Za-z_]\w*)"\s*:\s*(-?\d+(?:\.\d+)?)', text):
        if k not in out:
            try:
                out[k] = float(v) if '.' in v else int(v)
            except ValueError:
                pass
    return out or None


def _parse_json(text):
    """返回 (dict, 是否被截断救回)"""
    t = _strip_fences(text)
    try:
        return json.loads(t), False
    except Exception:
        pass
    # 退化情况：找第一个 { 到最后一个 } 之间
    i, j = t.find('{'), t.rfind('}')
    if i >= 0 and j > i:
        try:
            return json.loads(t[i:j + 1]), False
        except Exception:
            pass
    s = _salvage(t)
    return (s, True) if s else (None, False)


def normalize(value, dim, vocab):
    """把模型说的任意字符串归一到词表内的唯一值；对不上就返回 None。"""
    if not value:
        return None
    s = str(value).strip()
    for v in vocab:
        if s == v:
            return v
    # 别名表优先：口语说法 → 词表值
    for alias, canon in (ALIASES.get(dim) or {}).items():
        if alias in s and canon in vocab:
            return canon
    # 双向包含：模型说"蓝白"→"蓝白青花"，或说"蓝白青花感"→"蓝白青花"
    hits = [v for v in vocab if v in s or (len(v) > 2 and s in v)]
    return hits[0] if len(hits) == 1 else None


class VisionError(Exception):
    pass


class Vision:
    def __init__(self, config, data=None):
        self.cfg = config
        self.data = data or {}
        self.llm = LLMClient(config)

    @property
    def conf(self):
        return self.cfg.settings.get('vision') or {}

    @property
    def enabled(self):
        return bool(self.conf.get('enabled', True)) and self.llm.enabled

    def _vocab(self, dim):
        if dim in EXTRA:
            return (self.data.get('vocab') or {}).get(dim) or EXTRA_VOCAB[dim]
        return (self.data.get('vocab') or {}).get(dim) or []

    def _prompt(self):
        dims = SUGGESTED_ORDER
        lines = ['%s（取值为 true 或 false。这张图**本身**是不是一块面料/花型/图案？'
                 '软件截图、手机照片、人物、室内实景、纯文字、表格、聊天记录一律判 false）' % 'is_pattern']
        for d in dims:
            lines.append('%s（只能从这些里选一个：%s）' % (d, '、'.join(self._vocab(d))))
        lines.append('style_confidence（0-1 的小数，你对风格判断的把握）')
        lines.append('reason（一句话说明依据，30 字以内）')
        return ('请看这张图，判断它是不是一块面料/服装的花型图案。'
                '请只输出一行 JSON，不要任何解释文字，字段：\n'
                + '\n'.join('- ' + x for x in lines)
                + '\n\n注意：\n'
                '1. is_pattern 为 false 时，其余字段随便给一个词就行，我会忽略。\n'
                '2. 不要根据图里出现的**文字**去猜花型，只看画面本身。\n'
                '3. 只描述你真正看到的，看不准就选最接近的那个，不要留空。')

    def _call(self, image_url):
        body_extra = {}
        if self.conf.get('effort'):
            body_extra['effort'] = self.conf['effort']
        model = self.conf.get('model') or self.cfg.model
        return self.llm.chat(
            [{'role': 'user', 'content': [
                {'type': 'text', 'text': self._prompt()},
                {'type': 'image_url', 'image_url': {'url': image_url}}]}],
            tools=None,
            temperature=self.conf.get('temperature', 0),
            max_tokens=int(self.conf.get('max_tokens', 16000)),
            model=model,
            timeout=int(self.conf.get('timeout', VISION_TIMEOUT)),
            extra=body_extra,
        )

    def analyze(self, image_b64, mime='image/png'):
        """返回归一后的标签。结构：
        {ok, tags:{dim:值}, suggested:{dim:值}, raw:{...}, rejected:{dim:原话},
         reason, style_confidence, usage}
        """
        if not self.enabled:
            raise VisionError('识图未启用（没配 api_key 或 vision.enabled=false）')
        if not image_b64:
            raise VisionError('没有图片数据')

        url = image_b64 if image_b64.startswith('data:') \
            else 'data:%s;base64,%s' % (mime, image_b64)

        res, truncated = None, False
        tries = int(self.conf.get('retries', 1)) + 1
        last_raw = ''
        for _ in range(tries):
            res = self._call(url)
            last_raw = res.get('content') or ''
            parsed, truncated = _parse_json(last_raw)
            if parsed:
                break
        if not parsed:
            raise VisionError('模型没返回可解析的 JSON（%d 次都失败，多半是 max_tokens 被思考吃完了）：%s'
                              % (tries, last_raw[:120]))

        reason = (parsed.get('reason') or '').strip()
        # 前置闸门：不是面料图就一律不出标签，绝不硬套
        if parsed.get('is_pattern') is False or str(parsed.get('is_pattern')).lower() == 'false':
            return {"ok": False, "not_pattern": True, "reason": reason,
                    "tags": {}, "suggested": {}, "rejected": {}, "raw": parsed,
                    "truncated": truncated, "usage": res.get('usage')}

        tags, suggested, rejected, raw = {}, {}, {}, {}
        for dim in STABLE + EXTRA:
            v = normalize(parsed.get(dim), dim, self._vocab(dim))
            raw[dim] = parsed.get(dim)
            if v:
                tags[dim] = v
            elif parsed.get(dim):
                rejected[dim] = parsed.get(dim)
        for dim in SUGGEST_ONLY:
            v = normalize(parsed.get(dim), dim, self._vocab(dim))
            raw[dim] = parsed.get(dim)
            if v:
                suggested[dim] = v
            elif parsed.get(dim):
                rejected[dim] = parsed.get(dim)

        try:
            conf = float(parsed.get('style_confidence'))
        except (TypeError, ValueError):
            conf = None

        return {
            "ok": True,
            "tags": tags,
            "suggested": suggested,
            "rejected": rejected,
            "raw": raw,
            "reason": (parsed.get('reason') or '').strip(),
            "style_confidence": conf,
            "truncated": truncated,       # True = 预算不够被砍尾，字段是救回来的
            "usage": res.get('usage'),
        }
