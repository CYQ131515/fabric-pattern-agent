"""生图 / 改图客户端。

设计原则（跟 vision.py 一致）：
    **模型只负责「想生成什么」，「真的去调接口、落盘、算不算成功」由本地程序定。**

provider 可插拔：
    - volcengine 火山方舟 Seedream（默认，中文好、支持参考图改图）
    - openai     OpenAI 兼容端点（硅基流动 / 本地 SD API 等）
    - mock       本地程序画占位图（仅用于链路验证，会在返回里标 mock:true，绝不冒充真图）

所有产出落盘到 generated/，通过 /api/file/generated/<name> 访问，
不依赖第三方图床（方舟返回的 url 只有 24 小时有效期，不能直接用）。
"""
import base64
import json
import os
import struct
import threading
import time
import urllib.request
import urllib.error
import zlib


class ImageGenError(Exception):
    pass


class ImageGenHTTP(ImageGenError):
    """带 HTTP 状态码与厂商错误码，用于区分「换模型重试」和「真失败」。"""
    def __init__(self, status, raw):
        self.status = status
        self.raw = raw or ''
        code = ''
        try:
            code = (json.loads(self.raw).get('error') or {}).get('code') or ''
        except Exception:
            pass
        self.code = code
        self.message = self._msg()
        # 额度触顶（火山「安全体验模式」）≠ 普通并发限流，前者重试多少次都没用
        self.quota_limit = (code == 'SetLimitExceeded'
                            or 'safe experience mode' in self.message.lower()
                            or 'usage limit' in self.message.lower())
        super().__init__(self.message)

    def _msg(self):
        try:
            return (json.loads(self.raw).get('error') or {}).get('message') or self.raw[:200]
        except Exception:
            return self.raw[:400]


# 这类错误说明「这个模型用不了」，可以换下一个候选模型再试
UNAVAILABLE_CODES = ('ModelNotOpen', 'InvalidEndpointOrModel.NotFound', 'AccessDenied')


def friendly_error(err):
    """把厂商错误码翻译成「下一步该干什么」，而不是甩一段英文 JSON。"""
    if not isinstance(err, ImageGenHTTP):
        return str(err)
    s, c = err.status, err.code
    if s == 401 or c in ('AuthenticationError', 'InvalidApiKey'):
        return '生图 Key 无效（401）。检查 config/secrets.json 里的 image_api_key 是否填对、是否过期。'
    if s == 402 or c in ('InsufficientBalance', 'Arrears'):
        return '火山方舟账户余额不足（402）。去控制台充值后重试。'
    if s == 403:
        return ('生图模型无访问权限（403 AccessDenied）。这个模型可能需要在方舟控制台创建并绑定'
                '「推理接入点」才能调用，或者换一个已开通的模型。')
    if s == 429 or c == 'SetLimitExceeded':
        if isinstance(err, ImageGenHTTP) and err.quota_limit:
            return ('生图额度已用满，模型服务被暂停（SetLimitExceeded）。\n'
                    '这是火山方舟的「安全体验模式」上限：去控制台 → 模型开通管理，'
                    '找到该模型，调整或关闭「安全体验模式」的额度限制后即可继续。'
                    '（这种情况重试没用，不用等。）')
        return '生图请求过于频繁（429），稍等一会儿再试。'
    if c == 'ModelNotOpen':
        return ('生图模型未开通（ModelNotOpen）。去火山方舟控制台 → 模型广场，'
                '找到该模型点「开通」，开通后不用改代码，直接重试即可。')
    if c == 'InvalidEndpointOrModel.NotFound':
        return ('生图模型不存在或未开通。去火山方舟控制台 → 模型广场开通一个 Seedream 模型。')
    return '生图接口返回 %s（%s）：%s' % (s, c or '无错误码', str(err)[:160])


# ---------------------------------------------------------------- 提示词构造
# 这里【刻意不写】四方连续 / seamless。
# 实测：把「无缝平铺、四方连续、边缘完美对接」写进提示词，扩散模型做不到，
# 接缝比值仍在 6~8（安全线 1.6），纯粹白占提示词权重，还会把构图压死。
# 四方连续是下游「织印师」机器人的活：周期偏移 + 补缝 + 接缝检测。
# 生图这一环只负责把画面交干净：视角正交、元素完整、边界清楚，方便织印师分色分层和抽取花回。
GEN_TAIL = ('俯视正交视角的平面花型稿，无透视、无褶皱、无模特、无光影渐变，'
            '主体元素完整不被画框切断，排布均匀，底色平整干净，'
            '元素轮廓清晰、与底色对比明确，便于后期分色与分层，'
            '不要出现人物、文字、水印、摄影棚、衣架、边框。')

STYLE_EN = {
    '法式浪漫': 'French romantic, soft watercolor florals',
    '新中式国风': 'modern Chinese national style, ink-wash elegance',
    '复古波普': 'retro pop art, bold flat colors',
    '现代极简': 'modern minimalist, clean lines',
    '童趣插画': 'playful children illustration',
    '波西米亚': 'bohemian, ethnic ornamental',
    'Y2K未来': 'Y2K futuristic, glossy metallic',
    '田园草木': 'pastoral botanical, natural wildflower',
}
ELEMENT_EN = {
    '花卉': 'floral flowers',
    '叶片藤蔓': 'leaves and vines',
    '几何': 'geometric shapes',
    '动物纹': 'animal print',
    '条纹格纹': 'stripes and checks',
    '波点': 'polka dots',
    '抽象笔触': 'abstract brush strokes',
    '海洋贝壳': 'ocean shells',
}
COLOR_EN = {
    '莫兰迪灰调': 'Morandi muted grey palette',
    '马卡龙甜彩': 'macaron pastel candy colors',
    '黑白灰': 'black white grey monochrome',
    '高饱和撞色': 'high saturation contrasting colors',
    '大地色系': 'earthy brown tones',
    '蓝白青花': 'blue and white porcelain palette',
    '粉紫梦幻': 'dreamy pink and purple',
    '暖橙焦糖': 'warm orange caramel tones',
    # 常用但原先不在档位里的说法。加这一批是为了让「淡红」「雾霾蓝」这类
    # 用户口语能直接命中，而不是逼模型把它换成"最接近的粉紫梦幻"。
    '淡红': 'pale red, soft rose',
    '浅粉': 'light pink, blush',
    '樱花粉': 'sakura pink',
    '藕荷粉': 'lotus pink, dusty rose',
    '雾霾蓝': 'hazy dusty blue',
    '藏青': 'navy blue',
    '薄荷绿': 'mint green',
    '苔绿': 'moss green',
    '橄榄绿': 'olive green',
    '奶油白': 'creamy off-white',
    '燕麦米': 'oatmeal beige',
    '酒红': 'burgundy wine red',
    '芥末黄': 'mustard yellow',
    '灰紫': 'muted purple grey',
    '焦糖棕': 'caramel brown',
}

# 词表外的配色说法（用户顺口说的颜色）→ 英文提示。
# 用户说「淡红」，我们就该照「淡红」出图，而不是替他挑一个"最接近的档位"。
# 这里按「深浅修饰 + 色相」拆开拼英文；拼不出来就返回空串，
# 中文原样进提示词模型也认得，不影响可用性。
_COLOR_TONE = (
    ('淡', 'pale'), ('浅', 'light'), ('深', 'deep'), ('浓', 'rich'), ('暗', 'dark'),
    ('亮', 'bright'), ('柔', 'soft'), ('雾', 'misty'), ('灰', 'muted'), ('奶', 'creamy'),
    ('荧光', 'neon'),
)
# 顺序有讲究：两个字的色名要排在单字前面，否则「藕荷粉」会先命中「粉」，
# 拼出 'pink lotus pink' 这种重复词。
_COLOR_HUE = (
    ('藕荷', 'lotus pink'), ('豆沙', 'muted bean-paste red'), ('雾霾', 'dusty'),
    ('薄荷', 'mint'), ('橄榄', 'olive'), ('松石', 'turquoise'), ('珊瑚', 'coral'),
    ('香槟', 'champagne'), ('奶咖', 'milky coffee'),
    ('红', 'red'), ('粉', 'pink'), ('橙', 'orange'), ('黄', 'yellow'), ('绿', 'green'),
    ('青', 'cyan'), ('蓝', 'blue'), ('紫', 'purple'), ('棕', 'brown'), ('咖', 'coffee brown'),
    ('褐', 'brown'), ('灰', 'grey'), ('白', 'white'), ('黑', 'black'), ('米', 'beige'),
    ('杏', 'apricot'), ('藕', 'lotus pink'), ('茶', 'tea brown'), ('金', 'gold'),
    ('银', 'silver'), ('驼', 'camel'), ('酒', 'wine'), ('玫', 'rose'), ('砖', 'brick'),
)


def free_color_en(val):
    """自由配色 → 英文提示。拼不出来返回空串（中文原样进提示词即可）。"""
    s = (val or '').strip()
    if not s:
        return ''
    if all(ord(c) < 128 for c in s):
        return s                       # 用户直接写了英文，原样用
    rest, tones = s, []
    for zh, en in _COLOR_TONE:
        if zh in rest:
            tones.append(en)
            rest = rest.replace(zh, '', 1)
    hues = []
    for zh, en in _COLOR_HUE:
        if zh in rest and en not in hues and not any(en in h for h in hues):
            hues.append(en)          # '藕荷粉' 只该出一次 pink，别拼成 lotus pink pink
            rest = rest.replace(zh, '', 1)
    if not hues:
        return ''
    return ('%s %s palette' % (' '.join(tones), ' '.join(hues))).strip()
SCENE_EN = {
    '女装连衣裙': "women's dress fabric",
    '童装': 'childrenswear fabric',
    '家居布艺': 'home textile upholstery',
    '丝巾配饰': 'silk scarf accessory',
    '衬衫正装': 'shirting formalwear',
    '运动休闲': 'sportswear casual',
    '箱包鞋帽': 'bags shoes hats',
}
COMPOSITION_EN = {
    '散点': 'scattered scattered-layout',
    '满地': 'all-over dense coverage',
    '定位': 'engineered placement print',
    '条纹': 'stripe arrangement',
    '边花': 'border floral edge',
}
DENSITY_EN = {'疏': 'sparse spacing', '中': 'medium spacing', '密': 'dense spacing'}
SCALE_EN = {'小碎花': 'small ditsy scale', '中花': 'medium scale', '大花': 'large scale'}

# 改图指令模板：只改一处，其余全部"保持原样"——这是改图不跑偏的关键
EDIT_TEMPLATES = {
    'color': '把配色改成{v}，保持花型的造型、排布、疏密、尺度完全不变。',
    'element': '把主体元素改成{v}，保持配色、排布、疏密、尺度完全不变。',
    'density': '把花型排布的疏密度改成{v}，保持元素造型、配色、尺度完全不变。',
    'scale': '把花型的尺度改成{v}，保持元素造型、配色、排布疏密完全不变。',
    'composition': '把构图排列改成{v}，保持元素造型、配色、尺度完全不变。',
    'style': '把整体风格改成{v}，保持元素、配色、排布与尺度完全不变。',
}
EDIT_LABELS = {'color': '改配色', 'element': '改元素', 'density': '改疏密度',
               'scale': '改尺度', 'composition': '改构图', 'style': '改风格'}


def build_prompt(brief, extra=None):
    """把需求卡（四维 + 识图出来的构图/密度/尺度）拼成生图提示词。"""
    extra = extra or {}
    zh, en = [], []
    for key, table in (('style', STYLE_EN), ('element', ELEMENT_EN),
                       ('color', COLOR_EN), ('scene', SCENE_EN),
                       ('composition', COMPOSITION_EN), ('density', DENSITY_EN),
                       ('scale', SCALE_EN)):
        val = brief.get(key) or extra.get(key)
        if val:
            zh.append(val)
            if val in table:
                en.append(table[val])
            elif key == 'color':
                # 配色是用户最容易有具体想法的维度（"要淡红不要粉紫"）。
                # 词表里没有就现拼英文，而不是把它替换成最接近的档位。
                e = free_color_en(val)
                if e:
                    en.append(e)
    head = '面料印花花型设计，' + '，'.join(zh) if zh else '面料印花花型设计'
    if brief.get('scene'):
        head += '，用于' + brief['scene']
    prompt = head + '。' + ((' ' + ', '.join(en) + '. ') if en else ' ') + GEN_TAIL
    return prompt.strip()


def build_edit_prompt(base_prompt, changes, brief=None):
    """改图：把「改什么」翻译成一条明确的指令，附在原始提示词上。"""
    clauses = []
    for kind, value in (changes or {}).items():
        if not value:
            continue
        tpl = EDIT_TEMPLATES.get(kind)
        if tpl:
            clauses.append(tpl.format(v=value))
        else:
            clauses.append('把%s改成%s。' % (EDIT_LABELS.get(kind, kind), value))
    if not clauses:
        raise ImageGenError('没有指定要改什么（color/element/density/scale/composition/style）')
    tail = ('参考图是上一版花型，只按要求改动，其余保持不变。' + GEN_TAIL
            if base_prompt else GEN_TAIL)
    return (' '.join(clauses) + ' ' + tail).strip()


# ---------------------------------------------------------------- 描稿仿稿
# 描稿仿稿跟文生图看着像（都是"给个输入出张花型图"），但难点完全不同：
#   文生图——用户给的是文字，模型自由发挥，只要好看就行；
#   描稿   ——用户给的是一张随手拍的照片，模型的任务是「忠实提取」，
#            而不是「创作」。所以提示词的重点全在"忽略什么"上：
#            褶皱、光影、透视、背景、人体……这些必须明确排除，
#            否则模型会把照片的拍摄条件一起画进结果，用户拿到一张
#            "印在皱裙子上的图案"而不是"一张花型稿"。
# 与 GEN_TAIL 刻意分开、不共用：两者的约束方向是相反的。
TRACE_FIDELITY = {
    'strict': ('严格复刻', '照着照片里的图案原样复刻，元素造型、位置、配色、疏密都与照片保持一致，'
                          '不做任何美化或重设计'),
    'clean': ('清晰化重绘', '提取照片里的图案并重新绘制成干净规整的平面设计稿，'
                           '保留元素的造型特征与配色关系，线条清晰、边缘锐利、细节完整'),
    'vector': ('简化描摹', '把照片里的图案描摹成简洁的平面风格，合并细碎细节，'
                          '保留主要轮廓与配色关系，色块分明，适合印花分色'),
}
DEFAULT_FIDELITY = 'clean'

TRACE_TAIL = ('输入是一张随手拍的照片，可能是穿在身上的裙子、铺开的布料或样衣局部，'
              '上面带有褶皱、布料织纹、光影不均、透视变形、背景杂物或人体轮廓。'
              '这些拍摄条件一律不要出现在结果里——只提取衣服上的印花图案本身，'
              '把它还原成一张平整的平面花型设计稿：'
              '俯视正交视角，无透视、无褶皱、无阴影、无布料质感、无模特、无背景，'
              '图案完整不被画框切断，底色干净平整，元素轮廓清晰便于后期分色与分层。'
              '不要出现人物、文字、水印、衣架、价签、挂绳、拍摄场景。')

TRACE_LABELS = {'style': '风格', 'element': '元素', 'color': '配色',
                'scene': '用途', 'composition': '构图',
                'density': '疏密度', 'scale': '尺度'}


def build_trace_prompt(brief=None, fidelity=None, note=None):
    """描稿仿稿提示词：照片 → 平面花型设计稿。

    brief 里的维度是「用户**这一轮额外说出来的**要求」（例如"这块布的花，配色改成粉紫"）。
    图案本身以照片为准，brief 只覆盖用户明确说出来的那几项。

    ⚠️ 调用方**不要**把会话需求卡（designer 那张）传进来。
    描稿是织印师的活，照片是唯一依据；把花型师那边的配色带过来，
    用户会看到"织印师照着设计师的配色描稿"，那是错的。
    额外要求只能来自用户本轮的原话（trace_pattern 的 prompt 参数）。
    """
    brief = {k: v for k, v in (brief or {}).items() if v}
    key = fidelity if fidelity in TRACE_FIDELITY else DEFAULT_FIDELITY
    label, desc = TRACE_FIDELITY[key]

    parts = ['把这张照片上的印花图案提取出来，做成面料花型设计稿。',
             '还原度要求（%s）：%s。' % (label, desc)]

    if brief:
        stated = '、'.join('%s%s' % (TRACE_LABELS.get(k, k), v)
                          for k, v in brief.items())
        parts.append('用户对结果的额外要求：%s；图案仍以照片为准，只改这几项。' % stated)

    extra = (note or '').strip()
    if extra:
        parts.append(extra + '。')

    parts.append(TRACE_TAIL)
    return ' '.join(parts)


def last_image_record(f, prompt=None, origin='generated'):
    """把一张出图的产出记成「改图底图」的会话记录。

    ⚠️ `path` 是必须的：会话落盘时**只存 path 不存图片本体**（jsonl 会炸），
    重启后靠它把图读回来。少写这个字段，重载后底图就凭空消失，
    用户会觉得"刚生成的图，怎么改不了"。（真出过这个事故，见 session._locate）
    """
    with open(f['path'], 'rb') as fh:
        b64 = base64.b64encode(fh.read()).decode()
    return {"b64": b64, "mime": f.get('mime') or 'image/png',
            "url": f.get('url'), "path": f.get('path'),
            "origin": origin, "prompt": prompt}


# ---------------------------------------------------------------- 占位图（mock）
def _png(width, height, painter):
    """零依赖 PNG 编码器：painter(x, y) -> (r,g,b)。"""
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            rows += bytes(painter(x, y))
    def chunk(tag, data):
        return (struct.pack('>I', len(data)) + tag + data
                + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff))
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(bytes(rows), 9))
            + chunk(b'IEND', b''))


def scan_prompt(prompt):
    """mock 专用：从提示词里反查维度值，让占位图也能体现"改了什么"。

    先找「改成X」这种明确的编辑指令，找不到再退化成整体包含匹配。
    """
    import re
    out = {}
    for key, table in (('color', COLOR_EN), ('element', ELEMENT_EN),
                       ('composition', COMPOSITION_EN), ('density', DENSITY_EN),
                       ('scale', SCALE_EN)):
        if not table:
            continue
        pat = '改成(' + '|'.join(re.escape(v) for v in table) + ')'
        hit = re.findall(pat, prompt or '')
        if hit:
            out[key] = hit[-1]
            continue
        # 先把「保持…不变」的从句剥掉，否则"疏密""尺度"这些词会污染匹配
        base = re.sub(r'保持[^。]*不变', '', prompt or '')
        hit2 = [v for v in table if v in base]
        # '条纹' 是 '条纹格纹' 的子串，元素已命中就别再算成构图
        if key == 'composition' and '条纹格纹' in base:
            hit2 = [v for v in hit2 if v != '条纹']
        if hit2:
            out[key] = hit2[-1]
    return out


def make_mock_image(brief, seed=0):
    """本地画一张示意花型（散点小花 + 网格），只为验证链路，不冒充模型产出。

    seed 用来让「一次四张」的占位图彼此看得出区别，不然四张一模一样，
    验完也不知道前端到底摆了几张。
    """
    palette = {'蓝白青花': ((246, 243, 232), (58, 92, 148)),
               '粉紫梦幻': ((250, 240, 246), (188, 120, 176)),
               '大地色系': ((242, 234, 220), (150, 110, 70)),
               '莫兰迪灰调': ((236, 232, 226), (140, 138, 132)),
               '暖橙焦糖': ((248, 236, 222), (198, 116, 52)),
               '黑白灰': ((248, 248, 248), (60, 60, 60)),
               '马卡龙甜彩': ((250, 246, 250), (128, 190, 200)),
               '高饱和撞色': ((255, 250, 240), (226, 60, 90))}
    bg, fg = palette.get(brief.get('color'), ((245, 239, 226), (58, 92, 148)))
    if seed:
        # 每多一版就把颜色往旁边挪一点，肉眼能分出来是四张不同的
        fg = tuple(max(0, min(255, c + (seed * 37 if i == 0 else -seed * 29)))
                   for i, c in enumerate(fg))
    element = brief.get('element') or '花卉'
    density = {'疏': 150, '中': 90, '密': 55}.get(brief.get('density'), 90)
    scale = {'小碎花': 9, '中花': 16, '大花': 26}.get(brief.get('scale'), 13)
    phase = (seed * 23) % 96
    W = H = 384

    def painter(x, y):
        if x % 96 == 0 or y % 96 == 0:            # 网格：提示这是 tile
            return (228, 220, 206)
        cx, cy = ((x + phase) % density) - density // 2, (y % density) - density // 2
        d2 = cx * cx + cy * cy
        if element in ('波点', '几何'):
            return fg if d2 <= (scale * 0.7) ** 2 else bg
        if d2 <= 9:
            return fg
        # 四瓣花
        for dx, dy in ((scale, 0), (-scale, 0), (0, scale), (0, -scale)):
            if (cx - dx) ** 2 + (cy - dy) ** 2 <= (scale * 0.62) ** 2:
                return fg
        return bg
    return _png(W, H, painter)


def sniff(blob):
    """按文件头判断真实格式。

    踩过的坑：方舟 response_format=b64_json 返回的其实是 **JPEG**（JFIF 头），
    一律存成 .png 会让改图时把 JPEG 当 PNG 传给模型。必须按真实字节定后缀和 mime。
    """
    head = blob[:12]
    if head[:8] == b'\x89PNG\r\n\x1a\n':
        return '.png', 'image/png'
    if head[:3] == b'\xff\xd8\xff':
        return '.jpg', 'image/jpeg'
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return '.webp', 'image/webp'
    if head[:4] == b'GIF8':
        return '.gif', 'image/gif'
    # PSD 也得认，否则分层 PSD 的产物会被存成假的 .png，用户下下来打不开
    if head[:4] == b'8BPS':
        return '.psd', 'image/vnd.adobe.photoshop'
    return '.png', 'image/png'      # 认不出来就当 PNG，至少不会错得更离谱


# ---------------------------------------------------------------- 客户端
# 「配置段 → 它自己的密钥字段」。生图（image）用 image_api_key；
# 描稿仿稿（trace）可以单独配一把 trace_api_key，**留空就落回生图那把**。
# 以后再有能独立出图的机器人，在这里加一行即可（后台 KEY_FIELDS 也要同步加）。
KEY_FIELD = {'image': 'image_api_key', 'trace': 'trace_api_key'}


def _env(section, name):
    """按配置段读环境变量：先找段专属的，没有就回落到 IMAGE_* 那套。

    回落很重要：测试 / 部署只要设一把 `PATTERNFLOW_IMAGE_PROVIDER=mock`，
    生图和描稿就都走 mock。不回落的话描稿会漏到真接口上 ——
    离线回归变成真花钱，还可能因为网络不通整片红。
    """
    v = os.environ.get('PATTERNFLOW_%s_%s' % (section.upper(), name))
    if v is None and section != 'image':
        v = os.environ.get('PATTERNFLOW_IMAGE_%s' % name)
    return v


class ImageGen:
    def __init__(self, config, section='image'):
        """section='image' → 花型设计师的生图配置；
        section='trace' → 描稿仿稿的配置。

        描稿**默认沿用生图那套**：只有在 settings.<section> 里显式填了的项才覆盖，
        留空的项一律继承生图。所以默认零配置即成链路，后台想单独换模型/网关/Key
        再填即可（见 core/runtime.py 里两个实例的建法）。
        """
        self.cfg = config
        self.section = section
        conf = dict(config.settings.get('image') or {})
        if section != 'image':
            # **只覆盖非空项**。空值代表"沿用生图那套"，写进去会把生图的
            # 网关/模型一起抹掉 —— 后台留空保存必踩，所以这里先过滤掉。
            for k, v in (config.settings.get(section) or {}).items():
                if v not in (None, '', [], {}):
                    conf[k] = v
        self.enabled = bool(conf.get('enabled'))
        self.provider = (_env(section, 'PROVIDER')
                         or conf.get('provider') or 'volcengine')
        self.model = conf.get('model') or 'doubao-seedream-4-5-251128'
        self.fallback = conf.get('fallback_models') or []
        self.model_used = None      # 实际调通的那个（可能跟 self.model 不同）
        self.size = conf.get('size') or '2048x2048'
        self.watermark = bool(conf.get('watermark', True))
        self.timeout = int(conf.get('timeout') or 180)
        self.retry_429 = int(conf.get('retry_429', 2))     # 429 限流重试次数
        self.retry_net = int(conf.get('retry_net', 2))     # 传输中断重试次数
        self.retry_wait = int(conf.get('retry_wait', 6))   # 退避秒数（逐次翻倍）
        self.base_url = (_env(section, 'BASE_URL')
                         or conf.get('base_url')
                         or 'https://ark.cn-beijing.volces.com/api/v3').rstrip('/')
        self.api_key = (_env(section, 'API_KEY')
                        or (config.secrets.get(
                            KEY_FIELD.get(section, 'image_api_key')) or '')).strip()
        # 独立段没配自己的 Key = 用生图那把（默认就是共用的）
        if not self.api_key and section != 'image':
            self.api_key = (config.secrets.get('image_api_key') or '').strip()
        self.extra = conf.get('extra') or {}
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        # 测试 / 冒烟脚本用 PATTERNFLOW_IMAGE_OUTDIR 把产物挪去临时目录 ——
        # 不设的话就是配置里那个 generated/。跑一次回归往里堆几十张 mock 图，
        # 混在用户的真图里根本分不出来。
        # 这个变量对**所有**出图段都生效（描稿也写同一个 generated/），
        # 所以刻意不按 section 取变量名 —— 否则测试里描稿会漏到真库去。
        self.outdir = os.path.normpath(
            os.environ.get('PATTERNFLOW_IMAGE_OUTDIR')
            or os.path.join(root, conf.get('outdir') or 'generated'))
        os.makedirs(self.outdir, exist_ok=True)
        self.cost = int(conf.get('cost') or 0)
        # 「一次给几张」：用户要换着挑的时候比一张一张快得多。
        # 默认 4（settings.json → image.variants），改配置就能调，不用动代码。
        self.variants = max(1, int(conf.get('variants') or 1))

    # ---------- 对外：生成 ----------
    def generate(self, prompt, image=None, image_mime='image/png', n=1, tag='gen'):
        """image 为空 → 文生图；有 image → 图生图（改图）。返回落盘后的图片信息。

        n>1 时尽量「一次请求出多张」：用户要换着挑的时候，等 4 次单张太慢。
        厂商不支持组图（或只给回一部分）就自动逐张补齐，保证张数对得上。
        """
        if not self.enabled:
            raise ImageGenError('生图功能未启用（settings.json → image.enabled）')
        n = max(1, int(n or 1))
        if self.provider == 'mock':
            brief = scan_prompt(prompt)
            return self._save([make_mock_image(brief, seed=i) for i in range(n)],
                              tag, mock=True)
        if not self.api_key:
            raise ImageGenError('未配置生图 Key：请在 config/secrets.json 填 image_api_key，'
                                '或设置环境变量 PATTERNFLOW_IMAGE_API_KEY')
        if self.provider not in ('volcengine', 'openai'):
            raise ImageGenError('未知的 image provider：%s' % self.provider)

        if n == 1:
            blobs = (self._call_volcengine(prompt, image, image_mime, 1)
                     if self.provider == 'volcengine'
                     else self._call_openai(prompt, image, image_mime, 1))
        else:
            blobs = self._batch(prompt, image, image_mime, n)

        blobs = list(blobs or [])
        # 兜底补足：有的厂商只认单张，组图参数被忽略时只会回一张
        if len(blobs) < n:
            blobs += self._top_up(prompt, image, image_mime, n - len(blobs))
        return self._save(blobs[:n], tag)

    def _batch(self, prompt, image, image_mime, n):
        """一次要 n 张：并发跑 n 个小请求，每张加一句「和其他版区分开」。

        这里**刻意不用**厂商的「组图」参数。原因：一张 2048 的图 b64 回来约 1MB，
        4 张塞在同一个响应里就是 4MB+，实测这种大响应经常中途断流（IncompleteRead）；
        拆成 n 个独立小请求并发跑，总耗时接近单张，链路风险和单张一样可控。
        能出几张算几张，缺的由 generate() 逐张补齐。
        """
        slots = [None] * n

        def worker(i):
            one = prompt if i == 0 else prompt + (
                '（这是同一套需求的第 %d 版，请在配色与元素排布上与其他版本明显区分）' % (i + 1))
            try:
                blobs = (self._call_volcengine(one, image, image_mime, 1)
                         if self.provider == 'volcengine'
                         else self._call_openai(one, image, image_mime, 1))
                blobs = list(blobs or [])
                slots[i] = blobs[0] if blobs else None
            except Exception as e:                      # 单张失败不影响其他张
                slots[i] = e

        threads = [threading.Thread(target=worker, args=(i,), daemon=True)
                   for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        ok = [b for b in slots if isinstance(b, (bytes, bytearray))]
        if ok:
            return ok
        for s in slots:
            if isinstance(s, Exception):
                raise s
        raise ImageGenError('生图没有返回任何图片')

    def _top_up(self, prompt, image, image_mime, k):
        """逐张补齐剩下的 k 张，每张加一句「和其他版区分开」，避免几版长得一样。"""
        out = []
        for i in range(k):
            one = prompt + ('（这是同一套需求的补第 %d 版，请在配色与元素排布上'
                            '与其他版本明显区分）' % (i + 2))
            got = (self._call_volcengine(one, image, image_mime, 1)
                   if self.provider == 'volcengine'
                   else self._call_openai(one, image, image_mime, 1))
            out += list(got or [])
        return out

    def save(self, blobs, tag='upload'):
        """只落盘不调模型——用户上传的图也要存下来，才能当作改图底图。"""
        return self._save(blobs if isinstance(blobs, list) else [blobs], tag)

    # ---------- 各家接口 ----------
    def _post(self, url, body):
        """发一次请求。2048 大图的 b64 响应有 1MB 上下，链路偶尔会提前断
        （IncompleteRead / 连接重置），这类错误重试通常就能过，所以单独包一层。
        HTTPError（4xx/5xx）不在这里重试，交给上层按错误码决定。"""
        payload = json.dumps(body, ensure_ascii=False).encode('utf-8')
        last = None
        for attempt in range(self.retry_net + 1):
            req = urllib.request.Request(
                url, data=payload,
                headers={'Content-Type': 'application/json',
                         'Authorization': 'Bearer ' + self.api_key},
                method='POST')
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read().decode('utf-8'))
            except urllib.error.HTTPError as e:
                raise ImageGenHTTP(e.code, e.read().decode('utf-8', 'ignore'))
            except ImageGenError:
                raise
            except Exception as e:
                last = e
                if attempt < self.retry_net:
                    time.sleep(self.retry_wait * (attempt + 1))
                    continue
                raise ImageGenError('生图请求失败（%s：%s）。已重试 %d 次仍失败，'
                                    '可能是网络不稳或图太大，可把 image.size 调小再试。'
                                    % (type(e).__name__, e, self.retry_net))
        raise ImageGenError('生图请求失败（%s）' % last)

    def _models_chain(self):
        """主模型 + 备选模型。主模型「未开通/无权限」时依次往下试，
        这样账号只要开通了其中任意一个，都无需改配置。"""
        chain = [self.model]
        for m in (self.fallback or []):
            if m and m not in chain:
                chain.append(m)
        return chain

    def _call_volcengine(self, prompt, image, image_mime, n):
        tried, last = [], None
        for model in self._models_chain():
            body = {"model": model, "prompt": prompt, "size": self.size,
                    "response_format": "b64_json", "watermark": self.watermark,
                    "sequential_image_generation": "disabled"}
            if image:
                body["image"] = 'data:%s;base64,%s' % (image_mime or 'image/png', image)
            body.update(self.extra)
            # 429 是免费额度/并发限制，等一下往往就过了，先重试再换模型
            for attempt in range(self.retry_429 + 1):
                try:
                    data = self._post(self.base_url + '/images/generations', body)
                    self.model_used = model
                    return self._extract(data)
                except ImageGenHTTP as e:
                    last = e
                    # 只有并发限流才值得等；额度触顶等也没用，直接报
                    if e.status == 429 and not e.quota_limit and attempt < self.retry_429:
                        time.sleep(self.retry_wait * (attempt + 1))
                        continue
                    tried.append((model, e.code or 'HTTP%s' % e.status))
                    # 只有「这个模型用不了」才换下一个；参数错/余额/限流直接失败
                    if e.code in UNAVAILABLE_CODES or e.status == 404:
                        break
                    raise ImageGenError(friendly_error(e))
        detail = '；'.join('%s(%s)' % (m, c) for m, c in tried)
        raise ImageGenError(
            '没有可用的生图模型（试过：%s）。\n%s\n'
            '开通后不用改代码也不用重启服务，直接重试就行。'
            % (detail, friendly_error(last) if last else ''))

    def _call_openai(self, prompt, image, image_mime, n):
        body = {"model": self.model, "prompt": prompt, "n": max(1, int(n or 1)),
                "size": self.size, "response_format": "b64_json"}
        if image:
            body["image"] = 'data:%s;base64,%s' % (image_mime or 'image/png', image)
        body.update(self.extra)
        data = self._post(self.base_url + '/images/generations', body)
        if isinstance(data.get('data'), list) and 'url' in (data['data'][0] if data['data'] else {}):
            # 有些兼容端点只给 url，下载回来再落盘
            return [self._download(d['url']) for d in data['data'] if d.get('url')]
        return self._extract(data)

    def _download(self, url):
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as r:
                return r.read()
        except Exception as e:
            raise ImageGenError('下载生成图失败（%s：%s）' % (type(e).__name__, e))

    def _extract(self, data):
        if data.get('error'):
            raise ImageGenError('生图接口报错：%s' % json.dumps(
                data['error'], ensure_ascii=False)[:200])
        items = data.get('data') or []
        out = []
        for it in items:
            if it.get('b64_json'):
                out.append(base64.b64decode(it['b64_json']))
            elif it.get('url'):
                out.append(self._download(it['url']))
        if not out:
            raise ImageGenError('生图接口没返回图片：%s' % json.dumps(data, ensure_ascii=False)[:300])
        return out

    # ---------- 落盘 ----------
    @staticmethod
    def _unique_path(path):
        """要落盘的路径若已存在，就在扩展名前加 `-2`、`-3`… 换一个没人用的。

        不做加锁+重试那套：出图目录是「一次落一张」的追加式目录，
        名字里已经带秒级时间戳，撞名只在同一秒并发时发生 ——
        这时候抢到哪个序号都不影响正确性，只要不覆盖就行。
        """
        if not os.path.exists(path):
            return path
        stem, ext = os.path.splitext(path)
        k = 2
        while os.path.exists('%s-%d%s' % (stem, k, ext)):
            k += 1
        return '%s-%d%s' % (stem, k, ext)

    def _save(self, blobs, tag, mock=False):
        files = []
        for i, blob in enumerate(blobs):
            ext, mime = sniff(blob)
            name = '%s-%s-%s%d%s' % (time.strftime('%Y%m%d-%H%M%S'),
                                     (tag or 'gen')[:24],
                                     os.getpid() % 1000, i, ext)
            # 名字里可能带中文，统一转义掉，避免 URL 取图时出问题
            name = ''.join(ch if (ch.isalnum() or ch in '-_.') else '_' for ch in name)
            path = os.path.join(self.outdir, name)
            # **绝不覆盖已有文件**：名字只精确到秒，同一秒里连落两张（tag 相同、
            # 序号 i 相同）就会撞名，直接 open(...,'wb') 会把前一张抹掉。
            # 宁可后面加个序号，也不能丢图 —— 用户明确要求过"不要覆盖"。
            path = self._unique_path(path)
            name = os.path.basename(path)
            with open(path, 'wb') as f:
                f.write(blob)
            files.append({"name": name, "path": path, "url": '/api/file/' + name,
                          "mime": mime, "bytes": len(blob)})
        return {"ok": True, "files": files, "count": len(files),
                "provider": self.provider, "model": self.model_used or self.model,
                "mock": mock, "cost": self.cost * len(files)}

    def describe(self):
        return {"enabled": self.enabled, "section": self.section,
                "provider": self.provider, "base_url": self.base_url,
                "model": self.model, "fallback_models": self.fallback,
                "model_used": self.model_used, "size": self.size,
                "has_key": bool(self.api_key), "outdir": self.outdir,
                "cost": self.cost, "variants": self.variants}
