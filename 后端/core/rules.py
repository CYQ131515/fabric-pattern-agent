"""离线规则引擎：没有 api_key 时的兜底，也是零成本回归基线。

它刻意复刻原型 v0.3 的决策逻辑，但把「最多追问 2 轮」按 PRD 5.3 / 19.1 修正为 2。
"""
import re

DIMS = ('style', 'element', 'color', 'scene')
DIM_LABEL = {'style': '风格', 'element': '元素', 'color': '配色', 'scene': '场景'}
UNCLEAR = ('不知道', '不清楚', '没想好', '随便', '都可以', '你决定', '没概念', '不确定', '没偏好')

# 「用户这一轮是不是在明确要求出图」——**全站唯一的词表**。
# 生成技能的闸门（要放行）、规则兜底（要照办）、人设与系统提示（要照着走）
# 都认这一份。以前这套词在 handler 里单独抄了一份，两处一旦不同步，
# 就会出现"闸门放行了、兜底还在把用户往库里带"这种自相矛盾的行为。
#
# 只认动作词：「我想要一张卡通图案」不算 —— 实测模型就是把它当生成指令用，
# 用户为这个抱怨过两次（先是"怎么都只会生成了"，后是"怎么又不根据需求卡问用户了"）。
GEN_WORDS = ('生成', '出图', '出一张', '出张', '来一张', '来一块', '做一块',
             '画一张', '画个', '画一块', '开跑', '打样', '就这样出', '按这个出')

# 用户把这几个维度"交给"机器人的说法。说了就不该再问下去。
CEDE_WORDS = ('随便', '你决定', '你来定', '你定', '你看着办', '看着办', '听你的',
              '都行', '都可以', '不用问', '别问了', '帮我挑')


def hits(text, words):
    """text 里有没有 words 里的词。忽略空格（用户常打「生成 一张」）。"""
    t = (text or '').replace(' ', '')
    return any(w in t for w in words)


def wants_generate(text):
    """这一轮用户是不是明确要出图（或把选择权交回来了）。"""
    return hits(text, GEN_WORDS) or hits(text, CEDE_WORDS)


KEYMAP = {
    'style': {'法式': '法式浪漫', '国风': '新中式国风', '中式': '新中式国风', '复古': '复古波普',
              '波普': '复古波普', '极简': '现代极简', '简约': '现代极简', '童趣': '童趣插画',
              '卡通': '童趣插画', '儿童': '童趣插画', '波西米亚': '波西米亚', '民族': '波西米亚',
              'y2k': 'Y2K未来', '未来': 'Y2K未来', '科技': 'Y2K未来', '田园': '田园草木',
              '草木': '田园草木', '植物': '田园草木'},
    'element': {'碎花': '花卉', '玫瑰': '花卉', '花': '花卉', '叶': '叶片藤蔓', '藤': '叶片藤蔓',
                '树枝': '叶片藤蔓', '几何': '几何', '格': '条纹格纹', '条纹': '条纹格纹',
                '格子': '条纹格纹', '动物': '动物纹', '豹': '动物纹', '波点': '波点',
                '圆点': '波点', '海洋': '海洋贝壳', '贝壳': '海洋贝壳', '抽象': '抽象笔触',
                '水墨': '抽象笔触', '笔触': '抽象笔触'},
    'color': {'莫兰迪': '莫兰迪灰调', '马卡龙': '马卡龙甜彩', '黑白': '黑白灰', '撞色': '高饱和撞色',
              '鲜艳': '高饱和撞色', '大地': '大地色系', '棕': '大地色系', '蓝白': '蓝白青花',
              '青花': '蓝白青花', '粉': '粉紫梦幻', '紫': '粉紫梦幻', '橙': '暖橙焦糖',
              '焦糖': '暖橙焦糖'},
    'scene': {'连衣裙': '女装连衣裙', '女装': '女装连衣裙', '裙': '女装连衣裙', '衬衫': '衬衫',
              '衬衣': '衬衫', '童装': '童装', '家居': '家居布艺', '家纺': '家居布艺',
              '布艺': '家居布艺', '窗帘': '家居布艺', '箱包': '箱包配饰', '包': '箱包配饰',
              '鞋': '箱包配饰', '泳装': '泳装', '泳衣': '泳装', '丝巾': '丝巾',
              '运动': '运动服', '瑜伽': '运动服'},
}


def extract(text, vocab=None):
    """关键词抽取；只允许落在受控词表内，抽不出就不填，避免脏标签。"""
    t = (text or '').lower()
    got = {}
    for dim in DIMS:
        for kw, val in KEYMAP[dim].items():
            if kw.lower() in t:
                if not vocab or val in vocab.get(dim, []):
                    got[dim] = val
                break
    return got


def filled(brief):
    return [k for k in DIMS if brief.get(k)]


def missing(brief):
    return [k for k in DIMS if not brief.get(k)]


def score(p, brief):
    known = filled(brief)
    if not known:
        return min(96, int(62 + p.get('heat', 0) * 0.2))
    hit = sum(1 for k in known if p.get(k) == brief.get(k))
    return max(46, min(99, int(52 + hit / len(known) * 42)))


def search(patterns, brief, query='', limit=6):
    scored = []
    for p in patterns:
        known = filled(brief)
        hit = sum(1 for k in known if p.get(k) == brief.get(k))
        s = score(p, brief)
        if query and (query in p['name'] or query in p.get('desc', '')):
            s += 8
        scored.append((hit, s, p))
    scored.sort(key=lambda x: (-x[0], -x[1], -x[2].get('heat', 0)))
    return [p for _, _, p in scored[:limit]]


def _line(items):
    return '、'.join(items)


def _gen_offer(gen):
    """「库里没有，要不要 AI 生成」的报价文案。张数与单价都从生图配置取，别写死。"""
    n = max(1, int(getattr(gen, 'variants', 1) or 1)) if gen is not None else 1
    unit = int(getattr(gen, 'cost', 0) or 0) if gen is not None else 0
    if n <= 1:
        return ('要不要我直接用 AI 生成一版？一块 %d 点，我先报方案，你点头才出图。'
                % unit if unit else '要不要我直接用 AI 生成一版？我先报方案，你点头才出图。')
    tail = '（%d 张一共 %d 点）' % (n, unit * n) if unit else ''
    return ('要不要我直接用 AI 生成一版？一次给你 %d 张不一样的%s，你挑合意的用。'
            '我先报方案，你点头才出图。' % (n, tail))


def _match_verdict(brief, res, limit=4):
    """库里有没有真正对得上的款。判定口径跟 search_patterns 技能保持一致：
    用户只给一个维度时命中 1 个就算对上，给了多个维度时至少要命中 2 个。"""
    known = filled(brief)
    if not known:
        return False, 0
    best = max([sum(1 for k in known if p.get(k) == brief.get(k))
                for p in res[:limit]] or [0])
    return best >= (1 if len(known) <= 1 else 2), best


def reply_designer(text, sess, patterns, gen=None):
    got = extract(text)
    sess.brief.update({k: v for k, v in got.items() if v})
    sess.rounds += 1
    miss = missing(sess.brief)

    # 用户**这一轮真说出口**的维度。兜底文案只能拿它当"你说的"——
    # 需求卡里还躺着前几轮定下的值（配色/场景往往是几轮前补的），
    # 把它们一并说成"你给的条件"就是在替用户编话。真出过这个 bug：
    # 用户只说了「国风 + 玫瑰菊花牡丹」，回复却写「按你给的条件
    # （新中式国风、花卉、莫兰迪灰调、女装连衣裙）」，用户直接来问"我什么时候说过"。
    said = _line([v for v in got.values() if v])

    # (1) 卡上一个维度都没有、用户却明说要出图：照搬"从库里筛到 N 款"毫无意义
    # （等于把整个库倒给他），又落回"我要生成、它却让我去库里挑"那个毛病。
    # 先要一样最要紧的，拿到就能画。
    if wants_generate(text) and not filled(sess.brief):
        return ('想生成没问题，先给我一样就够——**画面里要有花还是走几何**？'
                '或者直接给个风格（国风 / 法式 / 卡通…），我按它先把方案摆出来，'
                '其余的我替你补，卡片上随时能改。', None)

    # (2) 用户这一轮明说要出图 —— 直接照办，**不再走"卡不齐就先问"那一套**。
    # 他要的是图，不是问卷。真正的方案卡由 runtime 在服务端确定性地挂出来
    # （模型那条路没走通时补位），这里只负责把话说对：先应下来，再顺带提一句
    # 库里有没有接近的，决定权仍然给他。
    #
    # 这一条必须放在下面那个 if 外面：那个 if 只在「卡齐了 / 轮次到了 / 用户说随便」
    # 时才进，卡不齐时压根走不到 —— 而"卡不齐"恰恰是最常见的场景。
    if wants_generate(text) and filled(sess.brief):
        res = search(patterns, sess.brief, text)
        matched, _hit = _match_verdict(sess.brief, res)
        near = _line([p['name'] for p in res[:2]])
        tail = ('库里 %s 这几款比较接近，你想先看也行。\n' % near) if (near and matched) else ''
        return ('按你说的「%s」这就给你出图。%s%s'
                % (said or '这版需求', tail, _gen_offer(gen)), None)

    if any(u in text for u in UNCLEAR) or sess.rounds >= 3 or not miss:
        res = search(patterns, sess.brief, text)
        known = filled(sess.brief)
        matched, _hit = _match_verdict(sess.brief, res)

        # 库里没有真正对得上的 → 别把近似款当答案推，直接引导 AI 生成
        if known and not matched:
            why = _line([sess.brief.get(k) for k in known if sess.brief.get(k)])
            near = _line([p['name'] for p in res[:2]])
            head = ('按你给的条件（%s）翻了库，说实话：现成的没有。'
                    % (why or '暂无明确条件'))
            if near:
                head += '最接近的只有 %s 两款，只是个方向，配色和元素都不是你要的。' % near
            return head + '\n' + _gen_offer(gen), [p['id'] for p in res[:2]]
        names = _line([p['name'] for p in res[:4]])
        why = _line([v for v in [sess.brief.get(k) for k in known] if v])
        tail = '四个维度都对齐了。' if not miss else '还差 %s，匹配度会有浮动，你可以先看着。' % _line(
            [DIM_LABEL[k] for k in miss])
        return ('按你给的条件（%s），从库里筛到 %d 款：%s。\n%s'
                '打开花型选择页可以只看图案，多选后我一张一张发给你。'
                % (why or '暂无明确条件', len(res), names, tail), [p['id'] for p in res])

    nxt = 'element' if 'element' in miss else miss[0]
    q = {'style': '这个花型主要想表达什么风格？',
         'element': '画面里希望出现什么元素？',
         'color': '配色上有偏好吗？',
         'scene': '最后用在什么品类上？'}[nxt]
    return q + '\n（不确定可以直接说「不知道」，我按已知条件先出候选。）', None


def reply_assistant(text, sess=None):
    t = text.lower()
    if re.search(r'充值|充点|买点|续费|付费|付款|余额不足|点数不够|计费|点数|多少钱|价格', t):
        # 充值系统先用模拟的形式，但对话里**卡片是真的**：金额档位 + 微信/支付宝 +
        # 二维码由前端挂出来。所以这里要把"去扫码"说清楚，别再说转人工。
        return ('前台统一用「点数」结算：四方连续 30 点/张、分层 PSD 45 点/张、'
                '描稿仿稿 60 点/张、全套交付包 120 点/套；客服问答与花型检索免费，'
                '任务失败自动退还。\n'
                '要充点数直接扫下面的码就行，微信和支付宝都可以，充多少自己选，'
                '付完点数立刻到账；需要发票随时跟我说。')
    if re.search(r'发票|开票|抬头|税号', t):
        return '可以开发票。需要抬头、税号、开票金额范围和接收邮箱，提交后由财务实际开票，进度我同步给你。'
    if re.search(r'表格|清单|整理|导出|项目册', t):
        # 清单是服务端确定性做好的（见 runtime.chat 的导出前置步骤），
        # 这里只要把结果说对：有几款就报几款，一款没选就照实说。
        hint = getattr(sess, 'export_hint', None) or {}
        n = len(hint.get('items') or [])
        if n:
            names = '、'.join((i.get('name') or i.get('id') or '') for i in
                              (hint.get('items') or [])[:4])
            return ('已经按你选中的 %d 款（%s%s）整理好了，带编号、四维标签、'
                    '设计说明和卖点，点下面的按钮就能下载表格。'
                    % (n, names, ' 等' if n > 4 else ''))
        return (hint.get('note') or
                '你还没在花型选择里确认过花型，所以这边没有清单可以整理。'
                '先去花型设计师那边选几款，我马上给你出一张表。')
    return ('我可以解答平台功能、点数计费、发票申请，也能整理花型清单，'
            '或把需求转给花型设计师和织印师。')


def reply_engineer(text, sess, patterns):
    t = text.lower()
    if re.search(r'psd|分层', t):
        return ('可以做分层 PSD：按主色聚类生成命名图层，输出 PSD + 色板表，45 点/张。'
                '确认花型后我跟你核对色数和尺寸再执行。')
    # 用户拿了照片来说描稿：先把话接上（"你的图我收到了"），
    # 别答成一段跟照片无关的功能清单——那看起来就像没认真看他发的东西。
    if getattr(sess, 'last_photo', None) and re.search(
            r'描|照片|图片|这张|仿|复刻|提取|做成|花纹|花型', t):
        return ('照片我收到了。描稿就是把照片上的印花提取成一张平整的平面花型稿，'
                '60 点/张；出来的是花型稿本身，不含四方连续（那要再走一步）。'
                '你说一声「就照这张描」，我先把照片体检结果给你，再决定要不要动手。')
    if re.search(r'描稿|照片|上传|仿稿', t):
        return ('描稿仿稿流程：输入质量检查 → 去噪超分 → 主体分割 → 矢量描摹，输出 SVG + PNG，60 点/张。'
                '低清、反光、严重遮挡的图我会先告诉你可恢复等级，不会承诺百分百还原。')
    if re.search(r'连续|四方', t):
        return ('四方连续：周期偏移补缝 + 接缝检测 + 3×3 平铺预览，输出 PNG/TIFF，30 点/张。'
                '花回尺寸有要求的话现在告诉我。')
    return ('我可以把花型做成可生产的文件：四方连续、分层 PSD、描稿仿稿。'
            '先告诉我处理哪一款、什么工艺规格。')


def reply(agent, text, sess, patterns, gen=None):
    if agent == 'assistant':
        return reply_assistant(text, sess), None
    if agent == 'engineer':
        return reply_engineer(text, sess, patterns), None
    return reply_designer(text, sess, patterns, gen)
