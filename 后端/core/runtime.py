"""Agent 运行时：把「人设 + 技能 + 会话记忆 + 大模型」串成一轮对话。

流程：
    用户消息 → 组装系统提示（人设 + 需求卡 + 工具）→ 调模型
    → 模型要调工具就本地执行（最多 max_tool_rounds 轮）→ 回写结果再问模型
    → 产出最终回复 + 命中的技能 + 更新后的需求卡

没有 api_key 或调用失败时，自动降级到 core/rules.py 的规则引擎，保证原型永远有话说。
"""
import base64
import json
import os
import re
import threading
import time

from .imagegen import sniff
from .llm import LLMClient, LLMError
from .vision import Vision, VisionError
from .imagegen import ImageGen, ImageGenError
from .patgen import PatternEngine
from .seamless import SeamlessEngine
from .psdlayer import PsdLayerEngine
from .tracealgo import TraceEngine
from .psdcache import PsdCache
from .atag import AtagEngine
from .telemetry import Telemetry, ref_host as telemetry_ref_host
from . import atag as atag_gate
from . import rules

# 有副作用、要花钱的出图类技能：确认闸门只管它们，也是「重新生成」允许重放的集合
GEN_ACTIONS = ('generate_pattern', 'edit_pattern', 'trace_pattern',
               'seamless_pattern', 'layer_psd')

# 出图产出入库时的来源标记：库页面上要能看出来"这张图哪来的"。
# 描稿仿稿和四方连续的**成品**都进库（用户 2026-10-12 定的规矩：库里存的是
# "处理好的图"，不是客户上传来做处理的照片）—— 两者都带来源标记。
INGEST_SOURCE = {'generate_pattern': 'ai-gen', 'edit_pattern': 'ai-edit',
                 'trace_pattern': 'trace', 'seamless_pattern': 'seamless'}

# 这几个来源的产物入库前必须先过自动打标签算法（core/atag.py 的闸门），
# 和后台导入同一条规矩。AI 生成/改图不走闸门（老路：云端 tagger），
# 它们本来就是按花型提示词出的，没必要再被"是不是花型"卡一道。
GATED_SOURCES = ('trace', 'seamless')

# 提案卡片要带给前端的附加字段。前端靠它们渲染体检结论、路线切换这些控件；
# 只存 action/summary/cost 的话界面上就只剩一句干巴巴的摘要。
PENDING_EXTRA = ('qc', 'count', 'probe', 'engine', 'route')

# 「整理/导出花型清单」的意图词（只对客服生效）。
# 命中就走**确定性导出**：先按会话里记着的"已选中花型"把表做出来，再把结果
# 塞进系统提示，模型只剩"报个结果"的活。用户明确抱怨过以前客服会反问
# 「哪个项目」「算已下载还是候选」，最后还说「下载记录查不到，得转人工」——
# 那些问题在这条链路上全是多余的，因为数据本来就在本地。
_EXPORT_RE = re.compile(r'导出|清单|表格|项目册|整理')

# 「充值/买点数」的意图词。命中时前端会在他回复下方挂出充值卡片
# （金额档位 + 微信/支付宝 + 二维码），所以这里只要拦住模型别再说
# 「充值渠道我手里没资料、得转人工」—— 那是充值系统没接通时的老话术。
_RECHARGE_RE = re.compile(r'充值|充点|买点|续费|付费|付款|扫码支付|余额不足|点数不够')

# 失败归类的关键词表（顺序即优先级）。看板上要能一眼看出"是配错了、被限流了、
# 还是网络问题" —— 全都混成一句"失败"的话，运营根本不知道下一步该干什么。
_FAIL_PATTERNS = (
    (('超时', 'timeout', 'timed out', 'time out'), 'timeout'),
    (('限流', '限速', '429', 'rate limit', 'too many request'), 'rate_limit'),
    (('401', '403', '鉴权', '未授权', 'unauthorized', 'forbidden',
      'api key', 'api_key', '密钥', 'token'), 'auth'),
    (('连接', '网络', 'connection', 'resolve', 'dns', 'ssl', 'refused'), 'network'),
    # 「没说清要改什么」也是输入问题：改图/描稿没给方向时就是这句。
    # 不单列一类的话它会掉进 other，失败原因那栏就剩一串「其它」，等于白埋。
    (('太大', '过大', '尺寸', '分辨率', 'too large',
      '没说要改', '没说要生成', '没说清'), 'input'),
    (('没配', '未配', '没有配', '未启用', '不可用', '不存在', '找不到',
      'not found', 'disabled'), 'not_ready'),
)


def _fail_reason(text):
    low = (text or '').lower()
    for keys, name in _FAIL_PATTERNS:
        for k in keys:
            if k in low:
                return name
    return 'other'


def gen_outcome(res):
    """一次出图算成功还是失败，失败的话归到哪一类。

    口径：技能说 ok **且真的产出了东西**才算成功（图或可下载文件）。
    分层 PSD 只回一个 PSD 下载、没有预览图，所以不能只看 images ——
    那样做好的 PSD 会被统计成"失败"。
    """
    if not isinstance(res, dict):
        return False, 'bad_result'
    if res.get('ok') and (res.get('images') or res.get('downloads')):
        return True, ''
    if res.get('error'):
        return False, _fail_reason(str(res['error']))
    if res.get('ok'):
        return False, 'empty'          # 说成功却什么都没产出
    return False, 'unknown'



class AgentRuntime:
    def __init__(self, config, personas, registry, sessions, data=None, library=None,
                 kb=None):
        self.cfg = config
        self.personas = personas
        self.registry = registry
        self.sessions = sessions
        self.data = data or {}
        self.library = library
        self.kb = kb
        self.llm = LLMClient(config)
        self.vision = Vision(config, self.data)
        self.imagegen = ImageGen(config)
        # 花型生成：走新算法 pattern_studio（九宫格元素 + 自动排版 + 无缝校验），
        # 见 core/patgen.py。imagegen 仍负责改图与落盘。
        self.patgen = PatternEngine(config)
        # 描稿仿稿：走新算法 01-clean（AI 清洗保花型），见 core/tracealgo.py。
        # 技能侧用 ctx['tracegen']，见 skills/trace_pattern。
        self.tracegen = TraceEngine(config)
        # 四方连续引擎是起子进程跑的（算法要 numpy/Pillow，主程序刻意零依赖），
        # 所以这里拿到的只是个"遥控器"，不会把依赖引进来。见 core/seamless.py。
        self.seamless = SeamlessEngine(config)
        # 分层 PSD 同样是子进程（算法要 numpy/Pillow/psd-tools），
        # 这里拿到的也只是个"遥控器"。见 core/psdlayer.py。
        self.psdlayer = PsdLayerEngine(config)
        # 分层 PSD 的结果缓存：同一张图（同层数）只真跑一次，之后直接调出来。
        # 见 core/psdcache.py（含"命中也要伪装 40~60 秒"的产品要求）。
        self.psdcache = PsdCache(config)
        # 自动打标签引擎：入库闸门（和后台导入共用 core/atag.py 底部那套），
        # 管描稿/四方连续成品的自动入库。同样是只读配置、不起子进程。
        self.atag = AtagEngine(config)
        # 埋点：出图成败/耗时、提案转化、知识库命中、前端点击与来路。
        # 只追加落盘、失败不影响主流程，见 core/telemetry.py。
        self.tel = Telemetry.from_config(config)

    # ---------- 埋点 ----------
    def _tel(self, kind, **fields):
        """写一条埋点。**再保险一层**：统计坏了也绝不能影响用户拿到图。"""
        try:
            self.tel.record(kind, **fields)
        except Exception:                            # noqa: BLE001
            pass

    # ---------- 知识库 ----------
    def _kb_lookup(self, agent, message, sess=None):
        """这一轮要不要查知识库、查到了什么。返回 None = 该角色不吃知识库。

        注意：**空库也返回 None**。库是空的时候还下「不许编」的指令，
        机器人就只会说"没有资料"，那是最差的第一印象。
        """
        if not self.kb or not getattr(self.kb, 'enabled', False):
            return None
        if not (self.kb.docs or self.kb.qa):
            return None
        if self.kb.agents and agent not in self.kb.agents:
            return None
        try:
            res = self.kb.lookup(message, agent=agent)
        except Exception as e:
            print('[kb] 检索异常，本轮不注入：%s: %s' % (type(e).__name__, e))
            return None
        # 命中率直接反映"知识库够不够用"：问了多少次、几次命中、
        # 几次只能回「资料里没有」。未命中的问题原样记下来（截断到 80 字）——
        # 那张表就是下一步该补的资料清单。
        self._tel('kb', agent=agent, sid=getattr(sess, 'id', None),
                  hit=1 if res.get('hit') else 0,
                  best=res.get('best') or None,
                  qa=len(res.get('qa') or []),
                  chunks=len(res.get('chunks') or []),
                  q=(message or '').strip()[:80] or None)
        return res


    # ---------- 上下文 ----------
    def _ctx(self, sess):
        return {
            "patterns": self.data.get('patterns', []),
            "library": self.library,
            "trends": self.data.get('trends', []),
            "vocab": self.data.get('vocab', {}),
            "brief": dict(sess.brief),
            "session": sess,
            "imagegen": self.imagegen,
            # 花型生成的新发动机（关键词 → 无缝 tile），见 core/patgen.py
            "patgen": self.patgen,
            # 描稿仿稿能用独立配置（默认跟 imagegen 同一套，见 core/imagegen.py）
            "tracegen": self.tracegen,
            "seamless": self.seamless,
            "psdlayer": self.psdlayer,
            "psdcache": self.psdcache,
            "config": self.cfg.settings,
        }

    def _has(self, agent, skill):
        return skill in (self.cfg.agent_conf(agent).get('skills') or [])

    def _system(self, agent, sess, kb_res=None):
        persona = self.personas.get(agent) or {}
        conf = self.cfg.agent_conf(agent)
        # 顺手把角色钉住：_system 里要读 sess.last_image（分槽存的），
        # 不绑定的话读到的是"上一个说话的角色"那一槽，提示词就编错了。
        sess.act_as(agent)
        parts = [persona.get('body', '你是 PatternFlow 的智能助手。')]
        # 说话风格单独一层，压在人设后面：
        # 人设写"这个角色干什么"，风格写"怎么说话"，后台改档位不用动人设文件。
        # 冲突时以这一节为准，否则老的人设里的语气描述会和档位打架。
        st = self.cfg.style_of(agent)
        if st and st.get('body'):
            parts.append('\n## 说话风格（以此为准）\n' + st['body'])
        # 知识库（严格模式）放在最前面，紧跟人设：
        # 放在后面容易被工具说明和识图结论冲淡，实测模型会跳过它去自由发挥
        if kb_res is not None:
            parts.append(self.kb.render_for_prompt(kb_res))
        if not self.cfg.settings['llm'].get('tools_enabled', True):
            parts.append('\n## 约束\n当前未开启工具调用，不要编造花型编号、价格与任务状态；'
                         '需要检索时直接请用户补充条件。')
        # 需求卡只给「能写卡」的角色看（花型师/路由器）。
        # 织印师看不到：它描的是照片，把花型师那边的配色摆给它，
        # 它就会拿设计师的需求卡去指导描稿（实测真的会这么干）。
        if self._has(agent, 'update_brief'):
            parts.append('\n## 当前需求卡\n' + json.dumps(
                {k: v for k, v in sess.brief.items() if v}, ensure_ascii=False))
        parts.append('\n## 你当前可调用的技能\n' + ', '.join(conf.get('skills', [])))
        if agent == 'assistant' and _RECHARGE_RE.search(
                (getattr(sess, 'last_user_text', '') or '').lower()):
            parts.append(self._recharge_block())
        if getattr(sess, 'export_hint', None) and agent == 'assistant':
            parts.append(self._export_block(sess.export_hint))
        if (self.patgen.available or self.imagegen.enabled) and (
                self._has(agent, 'generate_pattern')
                or self._has(agent, 'edit_pattern')):
            parts.append(self._gen_block(sess))
        if sess.image_tags:
            # 只有"能写卡"的角色才谈需求卡 —— 客服/织印师没有卡，
            # 跟它们说"已写进需求卡"等于让模型引用一个不存在的东西。
            parts.append(self._vision_block(
                sess.image_tags, brief_writable=self._has(agent, 'update_brief')))
        return '\n'.join(parts)

    def _recharge_block(self):
        """充值这一轮要怎么说。

        充值系统还没真正接通（先用模拟的形式），但**前端会把充值卡片挂出来**
        （金额档位 + 微信/支付宝 + 二维码），所以模型不该再讲老话术
        「充值渠道我手里没资料，得转人工」—— 用户看到的是卡片，嘴上却被推给人工，
        两边对不上，看起来像客服不干活。
        """
        return ('\n## 这一轮用户要充值\n'
                '- 系统已经在你回复下方挂出了充值卡片：**金额档位 + 支付方式'
                '（微信支付 / 支付宝）+ 二维码**，用户扫码就能充，点数实时到账。\n'
                '- 你就说：扫下面的码就能充，微信和支付宝都可以，充多少自己选，'
                '付完点数立刻到账，需要发票随时找我。\n'
                '- **不要**说"充值渠道我查不到""帮你转人工""联系客服处理"。\n'
                '- 计费口径照人设念（四方连续 30 点/张、分层 PSD 45 点/张、'
                '描稿仿稿 60 点/张、全套交付包 120 点/套；客服问答与花型检索免费）。')

    def _export_block(self, res):
        """清单已经生成好了 —— 把事实喂给模型，它只负责报结果。"""
        items = res.get('items') or []
        if not items:
            return ('\n## 这一轮用户要导出花型清单，但他**还没选过花型**\n'
                    '- 照实说：还没在花型选择里确认过花型，先去花型设计师那边选几款，'
                    '选完回来我马上给你出一张表。\n'
                    '- **不要**反问他项目名/范围，不要说"下载记录查不到"，不要转人工。')
        names = '、'.join('%s（%s）' % (i.get('name'), i.get('id')) for i in items[:8])
        more = '' if len(items) <= 8 else ' 等 %d 款' % len(items)
        return ('\n## 这一轮用户要导出花型清单，**表格已经生成好了**\n'
                '- 已按他选中的 %d 款做好：%s%s。\n'
                '- 回复下方的下载按钮是系统挂的，**不要**再问他项目名、范围，'
                '也不要提"下载记录""台账""转人工"。\n'
                '- 用一两句话报结果就行：已经按你选中的这几款整理好了，'
                '每款带一张花型缩略图，还有编号、四维标签、设计说明和卖点，'
                '点下面的按钮下载。'
                % (len(items), names, more))

    def _gen_block(self, sess):
        """生图/改图的调用规则。重点：模型只能提方案，永远不能自己按下确认键。

        这一块是唯一的规则来源 —— 人设里不要再抄一份，两处措辞稍有出入就会
        互相打架（曾经人设抄了一份，模型读到的是"卡不齐不许提生成"的旧口径，
        而服务端的闸门其实已经对"用户明说要生成"放行了，结果用户说「给我生成
        一张」被一路推去花型库挑选）。
        """
        # 张数以当前发动机为准：新版花型生成（pattern_studio）一次出 N 版
        # 可直接平铺的无缝 tile；mock/回滚时退回老生图的 variants。
        gen_engine = self.patgen if self.patgen.available else self.imagegen
        n = max(1, int(getattr(gen_engine, 'variants', 1) or 1))
        lines = ['\n## 生图与改图']
        lines.append('- **第一件事：分清用户是要「找」还是要「画」。**')
        lines.append('  - 说「**生成** / 画一张 / 做一张 / 出图 / 来一张花型」——'
                     '**就是要画**，直接调 `generate_pattern` 提方案。'
                     '**不要**把他推去花型库挑选，服务端也不会再要求先查库。')
        lines.append('  - 说「我想要 XX 图案 / 帮我找 XX / 有没有 XX 的款」——'
                     '是想看库里有什么，先调 `search_patterns`。'
                     '出图要花点数，能查到就不该画。')
        lines.append('- 用户明说要生成时，可以顺手查一次库；查到接近的就顺带提一句'
                     '「库里也有这几款，想先看也行」，但**方案照样挂出来**，'
                     '决定权留给他，别替他拍板"先看库"。')
        lines.append('- **需求卡差维度怎么办，看用户有没有明说要生成：**')
        lines.append('  - 没明说 → 先把缺的那一项问出来（**一次只问一个**），别报价。')
        lines.append('  - **明说了**（「生成」「画一张」「出图」…）→ **直接用现有条件提方案**，'
                     '缺的维度在回复里点一句「另外两项我先按 X 定了，卡片上可以改」，'
                     '**不要把他挡在追问里**——他要的是图，不是问卷。')
        lines.append('- 用户说「改配色 / 改疏密 / 换元素 / 改构图 / 改尺度」→ 调 `edit_pattern`，'
                     '一次只改一处最清楚。')
        lines.append('- 这两个技能**不会立刻出图**，会先返回一份待确认方案（含点数）。'
                     '你要把「要做什么、花多少点」说清楚，请用户确认。')
        lines.append('- 没明说要生成、按上面走了检索的：看返回里的 `matched` ——'
                     '`false` 说明库里没真正对得上的，挑最多 2 款讲清「哪项对上、'
                     '哪项不是你要的」，然后问他**要不要我用 AI 生成一版**；'
                     '`true` 就正常给候选，别主动往生成上带。')
        if n > 1:
            lines.append('- 张数：用户没提就默认一次出 %d 张不一样的花型给他挑，'
                         '报价要按总价说（%d 张 = %d×单张点数）；'
                         '**用户说了张数就按用户的**（「一张 / 两张 / 三张」→ 传 `n=1/2/3`），'
                         '不要劝他多出，也不要自作主张改回 %d 张。'
                         % (n, n, n, n))
        lines.append('- `search_patterns` 会同时查内置款和**本地图库**。返回里 `lib=true` 的是'
                     '库里的真图，系统会自动把图展示给用户 —— 你只要说清哪几款对得上、'
                     '对在哪一项，不要用文字去描述画面。')
        lines.append('- **绝对不许**自己传 confirmed=true，也不许说「已经生成好了」——'
                     '真正执行是用户点了确认之后由系统做的。')
        if sess.last_image:
            origin = sess.last_image.get('origin')
            label = {'generated': '刚生成的', 'library': '刚从花型库选的',
                     'handoff': '刚从别的机器人手上接过来的'}.get(origin, '用户上传的')
            lines.append('- 你手上有一张%s底图，可以直接调 `edit_pattern` 改它。' % label)
        else:
            lines.append('- 你手上没有底图，改图前需要用户先上传一张或先生成一张。')
        # 底图是**按角色分槽**存的：这里读到的只可能是你自己这条线上的图。
        # 但模型有时会从"刚刚聊了什么"里脑补出一张别的机器人的图（比如织印师
        # 刚做的四方连续），所以这条边界要写死，省得它开口就说"在现有花型上改"。
        lines.append('- **底图只认你自己这条线**。别的机器人（织印师）做的四方连续、'
                     '分层 PSD、描稿稿**不在你的会话里**，不要拿它们当"现有花型"，'
                     '也不要在回复里提它们。用户要新花型就从他给的需求走检索 / 生成。')
        return '\n'.join(lines)

    def _vision_block(self, vt, brief_writable=True):
        """把识图结果写进系统提示。稳定维度已经确定性写入需求卡了，
           这里只交代"看到了什么"和"风格是猜的、要跟用户确认"。

        brief_writable=False 的的角色（客服/织印师，没有 update_brief）：
           不说"写进需求卡"——它们没有卡。标签照样给（客服现在能拿它
           直接检索，见 personas/assistant.md），只是换个说法。
        """
        def fmt(d):
            return '、'.join('%s=%s' % (k, v) for k, v in (d or {}).items()) or '无'
        tags = vt.get('tags') or {}
        extra = {k: v for k, v in tags.items() if k not in ('element', 'color')}
        if not tags and not vt.get('suggested'):
            return ('\n## 用户上传了一张花型图，但识图没读出受控词表内的任何标签。\n'
                    '- 不要编造画面内容，直接告诉用户没看懂，请他用文字描述，或换一张更清晰的图。')
        lines = ['\n## 用户上传了一张花型图，识图结果（已由本地程序按受控词表校验过，不是你编的）']
        lines.append(('- 已直接写入需求卡：' if brief_writable else
                      '- 图上看出的标签（可以直接拿去检索）：') +
                     fmt({k: v for k, v in tags.items() if k in ('element', 'color')}))
        if extra:
            lines.append('- 图上看出的生产特征：' + fmt(extra))
        if vt.get('suggested', {}).get('style'):
            conf = vt.get('style_confidence')
            pct = ('把握 %.0f%%' % (conf * 100)) if isinstance(conf, float) else '把握未知'
            if isinstance(conf, float) and conf < 0.6:
                # 实测风格维度摇摆（同一张图会给出不同答案），把握低就别硬猜
                lines.append('- 风格**看不准**（%s，候选值：%s）。不要替用户拍板，'
                             '直接把候选摆出来让他选；你倾向的那个可以放在最后说"我看着像X"。'
                             % (pct, '、'.join((self.data.get('vocab') or {}).get('style', []))))
            else:
                lines.append('- 风格是我看图**猜**的：%s（%s）。必须在回复里说明这是猜的、'
                             '并请用户确认或改正，不要当成既定事实。'
                             % (vt['suggested']['style'], pct))
        if vt.get('rejected'):
            lines.append('- 这些维度没读出受控词表内的值，不要采信：' + fmt(vt['rejected']))
        lines.append('- 场景（用在什么品类上）**图上看不出来**，必须问用户。')
        lines.append('- 回复开头先用一句话说清你看到了什么，再进入正题。')
        return '\n'.join(lines)

    # ---------- 主入口 ----------
    def chat(self, agent, message, session_id='default', brief=None, image=None,
             image_mime='image/png', selected=None):
        agent = agent if agent in self.cfg.settings['agents'] else 'designer'
        sess = self.sessions.get(session_id)
        # 钉住"现在是谁在说话"。下面技能里读写的 sess.last_image / last_photo /
        # last_gen，以及回给模型的对话历史，都只认这一个角色 —— 三个机器人共用
        # 一个 session_id，但谁也不许看见别人那条线（跨机器人串味是这个会话最
        # 出名的 bug：织印师做完四方连续，花型师开口就"在现有花型上改配色"）。
        sess.act_as(agent)

        # 谁能用识图结果改需求卡 / 谁能把需求卡带进对话：
        # 得是带 update_brief 的角色（花型师/路由器）。
        # 织印师没有这个技能 —— 它要处理的是实拍照片，不是"找花型"。
        allow_brief = self._has(agent, 'update_brief')
        if brief and allow_brief:
            sess.brief.update({k: v for k, v in brief.items() if v})
            self.sessions.append(sess, {"type": "brief", "brief": dict(sess.brief)})

        # 「我的花型清单」：前端每次对话都把当前选中整份同步过来（覆盖式）。
        # 导出清单要按这份数据走，所以**先记下来再谈别的** ——
        # 用户这一句就是「帮我导出」，同一轮里就得能查到刚选的那几款。
        if selected is not None:
            ids = sess.set_selected(selected)
            self.sessions.append(sess, {"type": "selected", "selected": ids})

        used, picks, reply, mode, note = [], None, None, 'rule', None
        pick_items = None    # 检索命中项的完整资料（库里的真图要靠它带 image 地址回去）
        vision_note = None
        out_tags = None      # 只在这一轮返回识图标签，下一轮不再重复
        out_images = None    # 本轮产出的图片（生图/改图）
        out_pending = None   # 待用户确认的动作（扣费闸门）

        # ---- 识图：确定性前置步骤，不交给模型自己"看" ----
        if image:
            try:
                vt = self.vision.analyze(image, image_mime)
                # 「不是面料花型」闸门只给找花型的角色用。
                # 织印师收的正是随手拍的裙子/布料照片，那本来就不像平铺花型稿，
                # 拿这个闸门去卡他，等于把描稿仿稿的输入全拦死。
                if allow_brief and vt.get('ok') is False and vt.get('not_pattern'):
                    vision_note = ('这张图看起来不是面料花型（%s），所以我没有给它打任何标签。'
                                   '请换一张花型/面料图，或者直接用文字描述你要的风格、元素、配色、场景。'
                                   % (vt.get('reason') or '画面内容与花型无关'))
                else:
                    sess.image_tags = vt      # 只给本轮 _system 用，回合结束就清掉
                    out_tags = vt
                    self.sessions.append(sess, {"type": "image", "image_tags": vt})
                    if allow_brief:
                        # 稳定维度直接写卡；风格只当建议，交给模型去跟用户确认
                        for k, v in (vt.get('tags') or {}).items():
                            if k in ('element', 'color'):
                                sess.brief[k] = v
                        self.sessions.append(sess, {"type": "brief",
                                                    "brief": dict(sess.brief)})
            except VisionError as e:
                print('[vision] 识图失败，按纯文字处理：%s' % e)
                vision_note = '这张图我没读明白，先按你打的字来——'
            except Exception as e:
                print('[vision] 识图异常：%s: %s' % (type(e).__name__, e))
                vision_note = '这张图我没读明白，先按你打的字来——'
            finally:
                # 不管是干净花型稿还是随手拍的照片，都要留一份底图。
                # 描稿仿稿（trace_pattern）就靠它当输入，被闸门拦下也必须存，
                # 否则用户传了照片却被告知"没有可以描稿的照片"。
                self._remember_image(sess, image, image_mime, 'upload')

        # 只上传图片、没打字时，给模型一句可回应的话
        if not (message or '').strip() and image:
            message = ('我上传了一张照片，帮我把上面的图案描下来做成花型。'
                       if agent == 'engineer' else
                       '我上传了一张花型图，帮我看看这是什么花型、能不能照着找。')

        # 记下用户这一轮的原话：技能里要靠它判断"用户是要出图，还是只是在找"。
        # 必须在调模型之前写 —— generate_pattern 的 need_brief 闸门就在这一轮里读它。
        sess.last_user_text = (message or '').strip()

        # 知识库：命中就"照资料答"，没命中就"不许编"。空库/该角色没开 → None，
        # 表示这一轮不谈资料，机器人该怎么聊怎么聊。
        kb_res = self._kb_lookup(agent, message, sess) if (message or '').strip() else None

        # ---- 导出清单：确定性前置步骤，不交给模型 ----
        # 用户说「导出/整理清单」，系统就按**会话里记着的已选中花型**把 xlsx 做出来。
        # 先跑一遍、再把结果注入系统提示：模型看见"表格已经生成好、N 款分别叫什么"，
        # 就只会照实报结果 —— 不会反问项目名，更不会说"下载记录查不到、转人工"。
        export = None
        sess.export_hint = None
        if agent == 'assistant' and (message or '').strip() \
                and _EXPORT_RE.search(message):
            try:
                res = self.registry.run('export_list', {}, self._ctx(sess))
                if isinstance(res, dict) and res.get('ok'):
                    export = res
                    sess.export_hint = res
            except Exception as e:                     # noqa: BLE001
                print('[export] 清单生成失败（不影响对话）：%s: %s'
                      % (type(e).__name__, e))

        if self.llm.enabled:
            try:
                reply, used, picks, imgs, pick_items = self._chat_llm(
                    agent, sess, message, kb_res)
                mode = 'llm'
                out_images = imgs
                out_pending = self._pending_of(sess, agent)
            except LLMError as e:
                # 技术细节只留在服务端日志里，别甩给用户看（用户明确要求过：
                # 不要出现「模型调用失败 / max_tokens / 切换到本地规则模式」这类话）。
                print('[llm] 调用失败，转规则兜底：%s' % e)
                note = '刚才我这边绕得有点久、没接上，先按常规给你捋一版：'
            except Exception as e:
                print('[runtime] 异常：%s: %s' % (type(e).__name__, e))
                note = '刚才我这边卡了一下，先按常规给你捋一版：'

        # 空串也算没答上来（模型可能只吐思考过程），同样交给规则引擎兜底
        if not reply and self.cfg.settings['fallback'].get('enabled', True):
            text, picks = rules.reply(agent, message, sess,
                                      self.data.get('patterns', []), self.imagegen)
            reply = text
            mode = 'rule'
            # 规则引擎不懂知识库，但"资料里没有"这件事它也得如实说 ——
            # 否则用户问了价格，降级时却被规则引擎随口答了一个。
            if kb_res is not None and not kb_res.get('hit'):
                reply += '\n另外你说的这个，我这边资料里没有，我确认一下再答复你。'
            # 兜底到规则引擎之后，再补一道"先做后说"：用户这一轮**明说了要出图**，
            # 模型却没能把方案挂出来 —— 那就由服务端自己把方案做出来挂上，
            # 别让用户白等一场、还被回一句"去花型库里挑"（截图里就是这么出的）。
            # 需求卡一个维度都没有时不硬凑：那种情况先问清再画才对。
            if agent == 'designer' and not out_pending and rules.wants_generate(message):
                out_pending = self._propose_generate(sess, agent)
        elif reply is None:
            reply = '服务暂时无法回复，请检查配置。'
            mode = 'error'

        for n in (vision_note, note):
            if n:
                reply = n + '\n' + reply

        turn = sess.add_turn(agent, message, reply, used)
        sess.image_tags = None            # 识图结论只用一次，避免后面每轮都复述
        sess.export_hint = None           # 清单结果同理：当轮说完就清
        self.sessions.append(sess, {"type": "turn", "turn": turn,
                                    "rounds": sess.rounds})
        return {
            "ok": True,
            "agent": agent,
            "reply": reply,
            # 织印师/客服不回传需求卡：前端拿到就会 merge 进去，
            # 等于把花型师的需求卡又塞回会话里
            "brief": dict(sess.brief) if allow_brief else {},
            "picks": picks or [],
            "picks_items": pick_items or [],
            "skills": used,
            "mode": mode,
            # 知识库这一轮有没有资料。后台「检索测试」和排查"机器人为什么这么说"都靠它
            "kb": ({"hit": kb_res.get('hit'), "reason": kb_res.get('reason'),
                    "qa": len(kb_res.get('qa') or []),
                    "chunks": len(kb_res.get('chunks') or []),
                    "best": kb_res.get('best')} if kb_res else None),
            "image_tags": out_tags,
            "images": out_images,
            "pending": out_pending,
            # 这一轮如果用户要的是「导出花型清单」，表格已经做好了 ——
            # export 给前端列清单（哪几款），downloads 给前端挂真实下载按钮
            "export": export,
            "downloads": (export or {}).get('downloads') or [],
        }

    # ---------- 底图记忆 ----------
    def pick_library(self, session_id, pid, agent=None):
        """把库里的一张图选为「改图底图」。

        客户在对话里挑中一款 → 说「换个配色」→ 改图链路要能拿它当底图，
        所以这里把库图读出来塞进会话，跟用户自己上传的效果完全一样。

        底图按角色分槽：**哪个机器人页面上点的，就设给它自己**。
        这样织印师也能「从库里选一张」当底图来做四方连续 / 分层，
        而这份选择不会被花型师看见（跨角色传图必须是一次用户动作）。
        """
        sess = self.sessions.get(session_id)
        agent = self._infer_agent(sess, agent)
        sess.act_as(agent)
        if self.library is None:
            return {"ok": False, "error": "这个服务没启用花型库"}
        item = self.library.get(pid)
        if not item:
            return {"ok": False, "error": "库里没这张图：%s" % pid}
        src = self.library.path_of(item)
        if not src or not os.path.exists(src):
            return {"ok": False, "error": "这张图的原图不见了，可能被移动或删除"}
        try:
            with open(src, 'rb') as f:
                blob = f.read()
        except Exception as e:
            return {"ok": False, "error": "读图失败：%s" % e}
        self._remember_image(sess, base64.b64encode(blob).decode(),
                             sniff(blob)[1], 'library')
        self.library.touch_use(pid)
        return {"ok": True, "id": pid, "name": item.get('name'),
                "url": item.get('full'), "origin": "library", "agent": agent,
                "image": {"name": item.get('name'), "url": item.get('full'),
                          "mime": sniff(blob)[1]},
                "note": "已把「%s」设为底图" % (item.get('name') or pid)}

    def export_list(self, session_id, ids=None):
        """把用户选中的花型导成一张表格，返回下载地址。

        界面上的「导出花型清单」直接走这里 —— 确定性、不经过模型。
        跟客服对话里那条路的产物**完全一样**：同一个技能、同一份数据源
        （会话里记着的"用户已选中花型"）。区别只是这次没有模型参与，
        用户拿到的就是一份文件 + 一个下载地址，中间没人有机会反问他。
        """
        sess = self.sessions.get(session_id)
        if ids is not None:
            got = sess.set_selected(ids)
            self.sessions.append(sess, {"type": "selected", "selected": got})
        res = self.registry.run('export_list', {}, self._ctx(sess))
        return res if isinstance(res, dict) else {"ok": False, "error": "技能返回异常"}

    def hand_off(self, session_id, url, to_agent='engineer'):
        """把一张**已经产出**的图显式交给另一个机器人当底图。

        为什么需要这一步：会话记忆按角色隔离之后，织印师不会再自动"看见"
        花型师刚生成的那张图（那正是用户要求杜绝的跨机器人记忆）。
        但"花型师出一张 → 织印师做四方连续"是要能干的事，所以给它留一条
        **明路**：用户点一下「交给织印师」，这张图才落进织印师那一槽。

        跟自动串味的区别在于：这一步是用户按下的，来源和目标都写在脸上。
        """
        sess = self.sessions.get(session_id)
        to_agent = to_agent if to_agent in self.cfg.settings['agents'] else 'engineer'
        src = None
        if self.sessions.resolve:
            try:
                src = self.sessions.resolve(url)
            except Exception as e:
                print('[handoff] 按地址找图失败 %s：%s' % (url, e))
        if not src or not os.path.exists(src):
            return {"ok": False, "error": "这张图在本地找不到了，请重新生成或重新上传一张。"}
        try:
            with open(src, 'rb') as f:
                blob = f.read()
        except Exception as e:
            return {"ok": False, "error": "读图失败：%s" % e}
        sess.act_as(to_agent)
        self._remember_image(sess, base64.b64encode(blob).decode(),
                             sniff(blob)[1], 'handoff')
        name = (self.personas.get(to_agent) or {}).get('name') or to_agent
        return {"ok": True, "to": to_agent, "agent": to_agent, "name": name,
                "note": "已把这张图交给%s当底图" % name}


    def _remember_image(self, sess, b64, mime, origin, prompt=None):
        """把当前这张图记为「改图底图」。图片本体落盘，jsonl 里只存路径。"""
        try:
            saved = self.imagegen.save([base64.b64decode(b64)],
                                       'upload' if origin == 'upload'
                                       else ('lib' if origin == 'library' else 'gen'))
            f = saved['files'][0]
        except Exception as e:
            f = {"path": None, "url": None}
            print('[runtime] 底图落盘失败：%s' % e)
        sess.last_image = {"b64": b64, "mime": mime or 'image/png', "url": f.get('url'),
                           "origin": origin, "path": f.get('path'), "prompt": prompt}
        self._persist_image(sess)
        # 用户上传的照片单独留一份：描稿仿稿要以它为输入，
        # 不能被后面生成的图挤掉（否则用户先上传照片、再生成一版，就没法描稿了）。
        # 也只记给当前这个角色 —— 别人上传的照片不该变成"我这边有张照片可以描"。
        if origin == 'upload':
            sess.last_photo = {"b64": b64, "mime": mime or 'image/png',
                               "url": f.get('url'), "path": f.get('path')}
            self.sessions.append(sess, {"type": "last_photo",
                                        "agent": sess.current_agent(),
                                        "last_photo": {
                                            k: v for k, v in sess.last_photo.items()
                                            if k != 'b64'}})

    def _persist_image(self, sess):
        if not sess.last_image:
            return
        rec = {k: v for k, v in sess.last_image.items() if k != 'b64'}
        # 落盘时必须带上归属角色：重启后按它把底图放回各自的槽里。
        self.sessions.append(sess, {"type": "last_image",
                                    "agent": sess.current_agent(),
                                    "last_image": rec})

    # ---------- 出图产出入库 ----------
    def _skill_ingests(self, name):
        """这个技能出的图要不要自动进花型库？看它 skill.json 里的 `ingest`。

        查不到技能（比如被删了）时**默认入库** —— 跟 Skill 的默认值保持一致，
        宁可按老行为走，也不要因为查不到就把用户的图默默丢掉。
        """
        sk = self.registry.get(name) if self.registry else None
        return bool(getattr(sk, 'ingest', True))

    def _auto_ingest(self, images, source, gate=False):
        """把刚出的图收进花型库，后台跑（AI 打标签一张要几秒，不能拖慢出图返回）。

        为什么自动收：用户要的就是「打好的图片直接存进花型库」。
        出图这条路上的图最干净——过了扣费闸门、是用户确认要的，
        下次客户提一样的要求就能直接捞出来，不用重新生成。
        失败只进服务端日志：库没收进去不该影响用户拿到图。

        gate=True（描稿/四方连续的成品）：走 core/atag.py 的入库闸门 ——
        先过自动打标签算法，不是花型/引擎没就绪/没跑通都不入库，
            和后台导入同一条规矩（"入库的图都要过算法"）。
        gate=False（AI 生成/改图）：老路，云端 tagger 直接建档。
        """
        lib = self.library
        if lib is None or not getattr(lib, 'enabled', False) \
                or not getattr(lib, 'auto_ingest', False):
            return
        paths = [f.get('path') for f in (images or []) if f.get('path')]
        if not paths:
            return

        def job():
            eng = self.atag
            ready = None
            if gate:
                # 闸门没装好/没就绪：这一批就不入库。用户那边图已经拿到了，
                # 受影响的只是"库里少一份"—— 按规矩，没打过标不许存。
                if eng is None or not eng.available:
                    print('[library] 打标引擎没装好，产物暂不入库：%s'
                          % (eng.missing_note if eng else '没建引擎'))
                    return
                pr = eng.probe(fresh=True)
                ready = bool(pr.get('ready'))
                if not ready:
                    print('[library] 打标引擎没就绪，产物暂不入库：%s'
                          % (pr.get('note') or '引擎没起'))
                    return
            for p in paths:
                if not os.path.exists(p):
                    continue
                try:
                    if gate:
                        r = atag_gate.ingest_file(eng, lib, p, source=source,
                                                  ready=ready)
                    else:
                        # check_pattern=False：这张图是 AI 按花型提示词出的，
                        # 不该再被「这是不是花型图」卡一道（失败品会被标成待复核，仍然留档）
                        r = lib.ingest_file(p, source=source, check_pattern=False,
                                            extra={"origin_file": os.path.basename(p)})
                    if r.get('dup'):
                        print('[library] 出图与库里已有的一致，跳过：%s' % r.get('id'))
                    elif r.get('ok'):
                        print('[library] 出图已入库 %s（%s）'
                              % (r['id'], (r.get('item') or {}).get('name')))
                    else:
                        print('[library] 出图入库失败：%s' % r.get('error'))
                except Exception as e:
                    print('[library] 出图入库异常：%s: %s' % (type(e).__name__, e))

        threading.Thread(target=job, daemon=True).start()

    # ---------- 待确认方案：按角色隔离 ----------
    def _pending_of(self, sess, agent):
        """取某个角色自己的待确认方案。

        三个机器人共用同一个会话，方案必须各归各的 —— 否则织印师留的描稿方案
        会被花型师的「改配色」请求消费掉（真出过这个 bug）。
        """
        return (getattr(sess, 'pending_actions', None) or {}).get(agent)

    def _propose_generate(self, sess, agent):
        """明确要出图、但模型这条路没走通时，服务端自己把生成方案挂上。

        跟导出清单是同一个套路（"先做后说"）：动作在服务端确定性地做完，用户
        拿到一张**真能点的确认卡片**，而不是一句"你去花型库里挑挑"。这套兜底是
        为那次投诉加的 —— 用户说「给我生成一张国风的…」，模型调用失败降级到规则
        引擎，规则引擎只会检索，于是回了他一段"从库里筛到 6 款"，用户看到的是
        「我要生成，它却让我去库里选」。

        需求卡一个维度都没有时**不硬凑**：那种情况该先把需求问清楚，不是先画。
        """
        if not [v for v in (sess.brief or {}).values() if v]:
            return None
        try:
            res = self.registry.run('generate_pattern', {'confirmed': False},
                                    self._ctx(sess))
        except Exception as e:                     # noqa: BLE001
            print('[gen] 兜底提案失败（不影响对话）：%s: %s' % (type(e).__name__, e))
            return None
        if not (isinstance(res, dict) and res.get('need_confirm')):
            return None
        pending = {"action": 'generate_pattern',
                   "args": {'confirmed': False},
                   "summary": res.get('summary'),
                   "cost": res.get('cost'),
                   "changes": res.get('changes'),
                   "prompt": res.get('prompt')}
        for k in PENDING_EXTRA:
            if res.get(k) is not None:
                pending[k] = res[k]
        self._set_pending(sess, agent, pending)
        return pending

    def _set_pending(self, sess, agent, pending):
        """写入 / 清空某个角色的待确认方案（pending=None 表示清空）。"""
        if not isinstance(getattr(sess, 'pending_actions', None), dict):
            sess.pending_actions = {}
        if pending:
            old = sess.pending_actions.get(agent)
            # pid = 这张卡片的编号。同一个角色、同一个动作重跑提案（用户在卡片上
            # 切张数 / 切还原度 / 换路线）时**沿用原来的 pid**，因为它还是同一张卡，
            # 只是参数变了 —— 否则用户自己按几下按钮，转化率就被刷穿了。
            if old and old.get('action') == pending.get('action') and old.get('pid'):
                pending.setdefault('pid', old['pid'])
            if not pending.get('pid'):
                pending['pid'] = os.urandom(4).hex()
            pending['agent'] = agent
            sess.pending_actions[agent] = pending
            if not old or old.get('pid') != pending['pid']:
                self._tel('proposal', agent=agent, sid=sess.id,
                          skill=pending.get('action'), cost=pending.get('cost'),
                          n=pending.get('count'), pid=pending['pid'])
        else:
            sess.pending_actions.pop(agent, None)
        self.sessions.append(sess, {"type": "pending", "agent": agent,
                                    "pending": sess.pending_actions.get(agent)})

    # ---------- 执行：唯一会真出图、真花点的入口 ----------
    def _exec(self, sess, agent, name, call_args, pid='', phase='exec'):
        """跑一次出图类技能，顺手记一条埋点（成败 / 耗时 / 花了多少点 / 几张）。

        埋点在这里而不是在技能里：一来技能只管干活、不该操心统计，
        二来「从挂卡片到真执行隔了多久、中间用户在卡片上改了几次参数」
        这类信息只有运行时才知道。
        """
        t0 = time.time()
        try:
            res = self.registry.run(name, call_args, self._ctx(sess))
        except Exception as e:                       # noqa: BLE001
            self._tel('gen', agent=agent, sid=sess.id, skill=name, ok=0,
                      reason=type(e).__name__, phase=phase,
                      ms=int((time.time() - t0) * 1000), pid=pid or None)
            raise
        ms = int((time.time() - t0) * 1000)
        good, reason = gen_outcome(res)
        n = len(res.get('images') or []) if isinstance(res, dict) else 0
        if isinstance(res, dict) and res.get('downloads'):
            n = max(n, len(res['downloads']))
        self._tel('gen', agent=agent, sid=sess.id, skill=name,
                  ok=1 if good else 0, reason=reason or None, ms=ms,
                  points=(res.get('cost') if isinstance(res, dict) else None),
                  n=n or None, pid=pid or None, phase=phase,
                  mock=1 if (isinstance(res, dict) and res.get('mock')) else None,
                  model=((res.get('model') or None) if isinstance(res, dict) else None))
        return res


    def _infer_agent(self, sess, agent):
        """调用方没带 agent 时该落到哪个角色上。

        「不带角色」和「带错角色」不是一回事，所以不能一律按花型师处理：
        会话里只有一份待确认方案时，那一份就是唯一可能的答案，用它不会串台；
        有两份及以上才说明真有歧义，这时退回默认的花型师（跟旧行为一致）。

        之所以要兜这一层：脚本 / 老客户端 / 手工 curl 都不带 agent，
        描稿方案挂在织印师名下，一律算成花型师就永远返回「没有待确认的操作」。
        """
        if agent in self.cfg.settings['agents']:
            return agent
        pend = getattr(sess, 'pending_actions', None) or {}
        live = [k for k, v in pend.items() if v]
        if len(live) == 1:
            return live[0]
        return 'designer'

    # ---------- 确认闸门：真正执行的地方 ----------
    def run_action(self, session_id, action=None, args=None, agent=None):
        """用户在界面上点了确认 / 点了某个编辑按钮 → 这里确定性执行。

        模型永远走不到这里：对话链路里 confirm 技能的 confirmed 被强制为 false。

        **显式动作优先**：界面上的「改配色 / 换元素」是用户当下点的按钮，
        不能被"别的页面还留着一份待确认方案"顶掉 —— 之前就是这里把花型师的
        「改风格」执行成了织印师留在会话里的描稿。
        """
        sess = self.sessions.get(session_id)
        agent = self._infer_agent(sess, agent)
        # 绑定角色：这次执行读写的底图 / 记忆 / 重放记录，都只认这一个角色。
        # 「重新生成」尤其依赖它 —— last_gen 是按角色存的，绑错了会拿别人的记录重放。
        sess.act_as(agent)
        if action == 'cancel':
            self._set_pending(sess, agent, None)
            return {"ok": True, "cancelled": True}

        args = dict(args or {})

        # 「重新生成」= 把上一次成功跑过的动作原样再跑一遍。
        # 参数用调用方（前端）带回来的那份，服务端 session 里也留了一份兜底：
        # 出图成功后需求卡会被清空，靠现取的 brief 是拼不回原图的，
        # 所以必须靠当时存下来的完整提示词（args['prompt_full']）。
        if action == 'regenerate':
            spec = args.get('spec') or getattr(sess, 'last_gen', None)
            if not isinstance(spec, dict) or not spec.get('name'):
                return {"ok": False, "error": "没有可以重新生成的记录，请重新描述一次需求。"}
            name = spec['name']
            if name not in GEN_ACTIONS:
                return {"ok": False, "error": "这个操作不支持重新生成"}
            call_args = dict(spec.get('args') or {})
            call_args['confirmed'] = True
            # 重放不动需求卡：用户点「重新生成」只是想要另一版，
            # 这期间他可能已经写了新需求，不能再给它清一次。
            return self._settle(sess, agent, name, call_args,
                                self._exec(sess, agent, name, call_args,
                                           phase='replay'),
                                allow_reset=False)

        pending = self._pending_of(sess, agent)
        pid = ''
        if action in GEN_ACTIONS:
            # 界面直接点了「生成 / 改图 / 描稿」按钮：按用户显式指令执行。
            # 只有同角色的同一动作才拿待确认方案里的参数兜底（比如卡片上切过还原度），
            # 其余一律以本次请求为准。
            name = action
            call_args = dict(args)
            if pending and pending.get('action') == name:
                base = dict(pending.get('args') or {})
                base.update(args)
                call_args = base
                pid = pending.get('pid') or ''
        else:
            # 确认（或没带动作）：只能消费本角色自己的待确认方案
            if not pending:
                return {"ok": False, "error": "没有待确认的操作"}
            name = pending['action']
            call_args = dict(pending.get('args') or {})
            call_args.update(args)
            pid = pending.get('pid') or ''
        call_args['confirmed'] = True

        return self._settle(sess, agent, name, call_args,
                            self._exec(sess, agent, name, call_args, pid=pid))

    def _settle(self, sess, agent, name, call_args, result, allow_reset=True):
        """执行后收口：落底图、记下重放参数、清 pending、按需重置需求卡。

        allow_reset=False 用于「重新生成」：那是重放，不该碰用户手上的需求卡。
        """
        if isinstance(result, dict) and result.get('images'):
            self._persist_image(sess)
            # 记一份「怎么重放这次出图」：把当时的完整提示词一起存下来。
            spec_args = {k: v for k, v in call_args.items()
                         if k != 'confirmed' and not k.startswith('_')}
            if result.get('prompt'):
                spec_args['prompt_full'] = result['prompt']
            sess.last_gen = {"name": name, "args": spec_args}
            # 带 agent：last_gen 按角色存，重启后各归各的「重新生成」。
            self.sessions.append(sess, {"type": "last_gen",
                                        "agent": sess.current_agent(),
                                        "last_gen": sess.last_gen})
            result['gen'] = sess.last_gen      # 回给前端，重新生成时原样带回来
            # 「打好的图直接存库」：出图成功就后台入库打标签，不占用户的等待时间。
            # 但**入不入库由技能自己声明**（skill.json → ingest），别在这儿按
            # 技能名硬编码，那样新增一个"不入库"的技能就得回来改主程序。
            # 声明了要入库的，再按来源分两条路：描稿/四方连续的成品过算法闸门
            # （core/atag.py），AI 生成/改图走老路（云端 tagger）。
            if self._skill_ingests(name):
                src = INGEST_SOURCE.get(name, 'ai-gen')
                self._auto_ingest(result['images'], src,
                                  gate=src in GATED_SOURCES)
        self._set_pending(sess, agent, None)
        if not isinstance(result, dict):
            return {"ok": False, "error": "技能返回异常", "action": name}
        result['action'] = name
        # 需求卡自动重置：出图成功后清空，用户可以马上提下一个需求。
        # 只在技能明确要求时做（generate_pattern），且必须真的出图了；
        # 改图、描稿不重置，执行失败也不重置。
        reset_req = result.pop('reset_brief', False)
        if allow_reset and reset_req and result.get('ok'):
            sess.brief = {k: None for k in sess.brief}
            self.sessions.append(sess, {"type": "brief", "brief": dict(sess.brief)})
            result['brief_reset'] = True
        result['brief'] = {k: v for k, v in sess.brief.items() if v}
        return result

    def retune_pending(self, session_id, overrides=None, agent=None):
        """用户在确认卡片上改参数（比如切还原度）→ 重跑一次提案，只换卡片内容。

        跟 run_action 的关键差别：**不执行、不扣点**。用户按下确认时才真正出图。
        同样按角色取方案：改的是「当前这个机器人」那张卡片，不会动到别人的。
        """
        sess = self.sessions.get(session_id)
        agent = self._infer_agent(sess, agent)
        sess.act_as(agent)
        pending = self._pending_of(sess, agent)
        if not pending:
            return {"ok": False, "error": "没有待确认的操作"}
        name = pending['action']
        if name not in GEN_ACTIONS:
            return {"ok": False, "error": "这个操作不支持改参数"}
        call_args = dict(pending.get('args') or {})
        call_args.update(overrides or {})
        call_args['confirmed'] = False          # 提案模式：绝不执行
        # 告诉技能"这是在重跑一张已经在桌面上的卡片"，别走"生成前必须先查库"那道闸门。
        # 用户点卡片上的按钮时方案早就提过了，再拦一次按钮就会点不动。
        call_args['_retune'] = True
        result = self.registry.run(name, call_args, self._ctx(sess))
        if not (isinstance(result, dict) and result.get('need_confirm')):
            return {"ok": False, "error": "没能生成新的方案，请重试"}
        new_pending = {"action": name,
                       # `_retune` 只是这一次调用的信号，不写回卡片（免得进 jsonl 和 last_gen）
                       "args": {k: v for k, v in call_args.items()
                                if not k.startswith('_')},
                       "summary": result.get('summary'),
                       "cost": result.get('cost'),
                       "changes": result.get('changes'),
                       "prompt": result.get('prompt')}
        for k in PENDING_EXTRA:
            if result.get(k) is not None:
                new_pending[k] = result[k]
        self._set_pending(sess, agent, new_pending)
        return {"ok": True, "pending": new_pending}

    # ---------- 界面直通车：确定性提案（不经过模型） ----------
    def propose(self, session_id, action, args=None, agent=None):
        """按界面上的一次显式点击，确定性地跑一遍**提案**（不执行、不扣点）。

        跟 `run_action` 的区别：那个是"用户已确认，去执行"；这个是"用户点了
        一个直通车按钮，先把方案摆出来给他看"。跟模型链路的区别：不花 token、
        不受模型当时的状态影响 —— 界面上一个按钮点了没反应，比 AI 话说得漂亮
        重要得多，所以这里不走模型。

        仍然严格走确认闸门：`confirmed` 强制压成 False，点了卡片的确认才会真跑。
        """
        sess = self.sessions.get(session_id)
        agent = self._infer_agent(sess, agent)
        sess.act_as(agent)
        if action not in GEN_ACTIONS:
            return {"ok": False, "error": "这个操作不支持直接提方案"}
        call_args = dict(args or {})
        call_args['confirmed'] = False          # 提案模式：绝不执行
        try:
            result = self.registry.run(action, call_args, self._ctx(sess))
        except Exception as e:                   # noqa: BLE001
            return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        if not isinstance(result, dict):
            return {"ok": False, "error": "技能没返回可用的方案"}
        if result.get('error'):
            return {"ok": False, "error": result['error']}
        if not result.get('need_confirm'):
            return {"ok": False, "error": "这个操作没能给出待确认的方案"}
        pending = {"action": action,
                   "args": call_args,
                   "summary": result.get('summary'),
                   "cost": result.get('cost'),
                   "changes": result.get('changes'),
                   "prompt": result.get('prompt')}
        for k in PENDING_EXTRA:
            if result.get(k) is not None:
                pending[k] = result[k]
        self._set_pending(sess, agent, pending)
        return {"ok": True, "pending": pending}

    def cancel_run(self, session_id=None, agent=None):
        """用户点了界面上的「终止」——把正在跑的那次算法子进程真的掐掉。

        为什么要在服务端做这件事：前端 abort 只断掉它自己那条 fetch，
        后端子进程完全不知道，会一路跑到自己超时（四方连续 / 分层 PSD 都是
        15 分钟那一档）。后果有两个都很实在：
          · 用户以为"停了"，云端其实还在跑，钱照扣
          · engine._current 一直占着，界面上再来问进度，报的还是那个
            **已经被放弃**的任务 —— 看着像"终止了但还在跑"
        所以终止必须是一次真正的服务端调用，而不是纯前端动作。

        没有任何在跑的任务不算错误（用户可能连点了两下），一律回 ok。
        """
        out = {}
        for key, eng in (("psdlayer", getattr(self, 'psdlayer', None)),
                         ("seamless", getattr(self, 'seamless', None)),
                         ("patgen", getattr(self, 'patgen', None))):
            if eng is None or not hasattr(eng, 'cancel'):
                continue
            try:
                out[key] = bool(eng.cancel())
            except Exception as e:                        # noqa: BLE001
                print('[cancel_run] %s 终止失败：%s: %s' % (key, type(e).__name__, e))
                out[key] = False
        return {"ok": True, "stopped": out,
                "stopped_any": any(out.values())}

    # ---------- 前端埋点 ----------
    # 「会话结束方式」的判定口径（看板上会原样写出来，免得有人以为这是精确值）：
    #   done     我方给了完整答复之后他才走  —— 正常聊完
    #   asking   我方最后一句是在问他，他没回 —— 可能是问题问得不好
    #   pending  走的时候桌上还压着一张没点的确认卡片 —— 可能是被"要不要出图"卡住
    #   bounce   进来一句话没说就走
    # 前两种说明流程还能用，后两种就该去翻那个会话的记录了。
    END_LABEL = {'done': '聊完走的', 'asking': '在等用户回话',
                 'pending': '压着待确认卡片', 'bounce': '进来就走',
                 'unknown': '未知'}

    # 每种前端事件只收这些字段。**不整段收** —— 聊天内容已经在会话记录里了，
    # 统计用不上第二份，少存一分就少一分泄漏。
    TRACK_FIELDS = {'visit': ('ref', 'page', 'agent', 'w', 'h'),
                    'click': ('act', 'page', 'agent'),
                    'leave': ('dwell', 'page', 'agent')}

    def end_reason(self, sid):
        """这个会话是「聊完走的」还是「卡住走的」。"""
        try:
            sess = self.sessions.get(sid) if sid else None
        except Exception:                            # noqa: BLE001
            sess = None
        if sess is None:
            return 'unknown'
        if any(v for v in (getattr(sess, 'pending_actions', None) or {}).values()):
            return 'pending'
        turns = sess.all_turns()
        if not turns:
            return 'bounce'
        last = max(turns, key=lambda x: x.get('t') or 0)
        rep = (last.get('reply') or '').strip()
        if rep.endswith('？') or rep.endswith('?'):
            return 'asking'
        return 'done'

    def track(self, payload):
        """收下前端报上来的一件事。**永远回 ok**，不认识的类型也只是忽略。

        埋点是"顺路记一笔"，不是业务接口 —— 它不该有能力让页面报错，
        也不该因为前端版本不匹配就把请求打回去。
        """
        ev = str((payload or {}).get('event') or '').strip()[:24]
        allowed = self.TRACK_FIELDS.get(ev)
        if not allowed:
            return {"ok": False, "error": "不认识的事件：%s" % ev}
        sid = str(payload.get('session_id') or '')[:64]
        fields = {"sid": sid or None}
        for k in allowed:
            v = payload.get(k)
            if v is None or v == '':
                continue
            if k in ('w', 'h', 'dwell'):
                try:
                    fields[k] = max(0, int(float(v)))
                except (TypeError, ValueError):
                    continue
            elif k == 'ref':
                # 来路只留域名：完整 Referer 里可能带着搜索词和 token，
                # 统计用不上，所以写入时就砍掉（见 telemetry.ref_host）。
                h = telemetry_ref_host(v)
                if not h:
                    continue
                fields[k] = h
            else:
                fields[k] = str(v)[:80]
        if ev == 'leave':
            fields['reason'] = self.end_reason(sid)
            try:
                fields['turns'] = len(self.sessions.get(sid).all_turns()) if sid else 0
            except Exception:                        # noqa: BLE001
                pass
        self._tel(ev, **fields)
        return {"ok": True}

    # ---------- 模型链路 ----------
    def _chat_llm(self, agent, sess, message, kb_res=None):
        conf = self.cfg.agent_conf(agent)
        # 「给它看了哪些工具」和「允许它调哪些工具」必须是同一份清单。
        # 不给这一道，模型喊出一个没喂给它的名字时，下面会照名字直接
        # registry.run —— 客服页上凭空挂出一张「生成 4 张、240 点」的确认卡片
        # （埋点冒烟里就是这么冒出来一条 agent=assistant 的 proposal）。
        # 模型只看见自己的工具，正常不会这么喊；但"正常不会"不是一道防线。
        allowed = set(conf.get('skills') or [])
        tools = self.registry.schemas(conf.get('skills', []))
        messages = [{"role": "system", "content": self._system(agent, sess, kb_res)}]
        # 只喂这个角色自己的对话历史（见 Session.history 的说明）。
        messages += sess.history(agent, limit=6)
        messages.append({"role": "user", "content": message})

        used, picks, images, pick_items = [], None, None, None
        max_rounds = int(self.cfg.settings['llm'].get('max_tool_rounds', 3))

        for _ in range(max_rounds):
            res = self.llm.chat(messages, tools=tools or None)
            calls = res.get('tool_calls') or []
            if not calls:
                return (res.get('content') or '').strip(), used, picks, images, pick_items

            messages.append({"role": "assistant",
                             "content": res.get('content') or '',
                             "tool_calls": calls})
            for call in calls:
                fn = call.get('function') or {}
                name = fn.get('name')
                if name not in allowed:
                    # 静默跳过会让模型下一轮接着喊同一件工具、白烧两轮上下文，
                    # 所以回一条"没有这件工具"，让它自己换路子收口。
                    print('[llm] %s 喊了不属于它的工具：%s' % (agent, name))
                    messages.append({
                        "role": "tool", "tool_call_id": call.get('id'), "name": name or '',
                        "content": json.dumps(
                            {"error": "这个角色没有 %s 这件工具，别调用它。" % name},
                            ensure_ascii=False)})
                    continue
                try:
                    args = json.loads(fn.get('arguments') or '{}')
                except Exception:
                    args = {}
                skill = self.registry.get(name)
                # 扣费闸门：对话链路里 confirm 技能一律视为"未确认"，
                # 就算模型自己传了 confirmed=true 也强制压回 false。
                if skill is not None and skill.confirm:
                    args['confirmed'] = False
                result = self.registry.run(name, args, self._ctx(sess))
                if name == 'update_brief' and isinstance(result.get('brief'), dict):
                    sess.brief.update({k: v for k, v in result['brief'].items() if v})
                    self.sessions.append(sess, {"type": "brief", "brief": dict(sess.brief)})
                if name == 'search_patterns' and result.get('picks'):
                    picks = result['picks']
                    # 命中的完整资料一起带上：前端手里只有 id，
                    # 而库里那些真图必须拿到 image 地址才渲染得出来。
                    pick_items = result.get('items') or []
                if skill is not None and skill.confirm and isinstance(result, dict) \
                        and result.get('need_confirm'):
                    # 只落提案，不执行；等用户在界面上点确认。
                    # qc / probe / fidelity 这类提案附加信息也要带上：前端靠它渲染
                    # 体检结论和路线切换，只存 action/summary/cost 的话界面上就只剩
                    # 一句干巴巴的摘要。
                    # prompt 也带上：确认卡片不显示它，但联调/排查时要能看到
                    # 「这一版到底按什么提示词出的」，否则只能靠猜。
                    pending = {"action": name, "args": args,
                               "summary": result.get('summary'),
                               "cost": result.get('cost'),
                               "changes": result.get('changes'),
                               "prompt": result.get('prompt')}
                    for k in PENDING_EXTRA:
                        if result.get(k) is not None:
                            pending[k] = result[k]
                    self._set_pending(sess, agent, pending)
                if isinstance(result, dict) and result.get('images'):
                    images = result['images']
                    self._persist_image(sess)
                if name not in used:
                    used.append(name)
                messages.append({"role": "tool", "tool_call_id": call.get('id'),
                                 "name": name,
                                 "content": json.dumps(result, ensure_ascii=False)[:4000]})
        # 超过轮数仍没收敛，再做一次不带工具的收口
        messages.append({"role": "user", "content": "请基于上面的工具结果直接回复用户，不要再调用工具。"})
        res = self.llm.chat(messages, tools=None)
        return (res.get('content') or '').strip(), used, picks, images, pick_items
