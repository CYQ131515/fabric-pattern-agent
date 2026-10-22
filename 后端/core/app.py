"""两个进程共用的应用装配。

C 端（server.py）和后台（admin_server.py）要的是同一批东西：配置、人设、
技能、图库、知识库。差别只在要不要建"能聊天"的运行时——后台不聊天，
建 LLM / 识图 / 生图客户端纯属浪费（生图密钥没配时还会打一堆告警）。

所以装配逻辑放这里，两边各取所需：`App(who='admin', with_runtime=False)`。
"""
import json
import os
import sys

from . import sync
from .config import Config
from .imagegen import ImageGen
from .patgen import PatternEngine
from .tracealgo import TraceEngine
from .kb import KnowledgeBase
from .library import PatternLibrary, DIMS as LIB_DIMS
from .netenv import sanitize_proxies
from .persona import load_all as load_personas
from .session import SessionStore
from .skills import SkillRegistry

# 进程一起来就先体检一次环境：本地代理要是没在跑，出网请求会全灭
# （对话退回离线规则、生图报错），而且症状看起来跟代理毫无关系。
# 放在模块级是为了在**任何**用法下都生效，包括后台进程和脱离 server.py 跑的脚本。
sanitize_proxies()


MAX_IMAGE_BYTES = 20 * 1024 * 1024      # base64 字符串长度上限（前端已把长边压到 2048 以内）


def take_image(payload):
    """从请求里取图片，返回 (base64, mime)。支持 data: 前缀和裸 base64。"""
    raw = payload.get('image')
    if not raw or not isinstance(raw, str):
        return None, 'image/png'
    raw = raw.strip()
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError('图片太大（%d 字节），请在前端先把长边缩到 2048px 以内' % len(raw))
    mime = payload.get('image_mime') or 'image/png'
    if raw.startswith('data:'):
        head, _, body = raw.partition(',')
        if ';' in head:
            mime = head[5:].split(';')[0] or mime
        return body, mime
    return raw, mime


def facets(lib):
    """库里**实际有**的标签值 + 每档数量，前端筛选按钮直接照着渲染。

    不读词表是因为：词表是"推荐的档位"，库里可能一个都没有；
    照词表渲染会摆出一排点不动的死按钮。
    """
    out = {}
    for dim in (LIB_DIMS + ('composition', 'density', 'scale')):
        cnt = {}
        for p in lib.items:
            v = p.get(dim)
            if v:
                cnt[v] = cnt.get(v, 0) + 1
        out[dim] = sorted(cnt.items(), key=lambda x: (-x[1], x[0]))
    src = {}
    for p in lib.items:
        s = p.get('source') or 'import'
        src[s] = src.get(s, 0) + 1
    out['source'] = sorted(src.items(), key=lambda x: -x[1])
    return out


class App:
    """一份配置 + 一批数据。reload() 会整体换一遍。"""

    def __init__(self, who='client', with_runtime=True):
        sync.set_who(who)
        self.who = who
        self.with_runtime = with_runtime
        self.cfg = Config()
        self.watch = sync.Watcher(who)
        self.reload()

    # ---------- 装配 ----------
    def _load_data(self):
        data = {}
        for key, fn in (('patterns', 'patterns.json'), ('trends', 'trends.json'),
                        ('vocab', 'vocab.json')):
            p = os.path.join(self.cfg.path('data'), fn)
            if os.path.exists(p):
                with open(p, encoding='utf-8') as f:
                    data[key] = json.load(f)
            else:
                print('[data] 缺少 %s，相关技能会退化' % p)
                data[key] = []
        return data

    def _resolve_image(self, url):
        """前端图片地址 → 本地路径：/api/file/<名字> → 出图目录里的同名文件。

        会话恢复时用（早期记录只存了 url 没存 path）。
        找不到就返回 None —— 让调用方按"这张图真没了"处理，别抛异常。
        """
        try:
            name = os.path.basename((url or '').split('?')[0].split('#')[0])
            root = getattr(getattr(self, 'imagegen', None), 'outdir', None)
            if not name or not root:
                return None
            p = os.path.join(root, name)
            return p if os.path.exists(p) else None
        except Exception as e:
            print('[app] 按地址找图失败 %s：%s' % (url, e))
            return None

    def reload(self):
        self.personas = load_personas(self.cfg.path('personas'))
        self.registry = SkillRegistry(self.cfg.path('skills'))
        # resolve 让会话恢复时能按 url 找回图（老记录只有 url 没有 path）。
        # 这里传方法引用即可，真正调用发生在 imagegen 建好之后。
        self.sessions = SessionStore(self.cfg.path('sessions'),
                                     resolve=self._resolve_image)
        self.data = self._load_data()
        self.library = PatternLibrary(self.cfg, vocab=self.data.get('vocab'))
        self.kb = KnowledgeBase(self.cfg, root=self.cfg.path('kb'))
        self.imagegen = ImageGen(self.cfg)
        # 花型生成：走新算法 pattern_studio（九宫格元素 + 自动排版 + 无缝校验），
        # 见 core/patgen.py。老 imagegen 只保留给「定向改图」和落盘。
        self.patgen = PatternEngine(self.cfg)
        # 描稿仿稿：走新算法 01-clean（AI 清洗保花型），见 core/tracealgo.py
        self.tracegen = TraceEngine(self.cfg)
        self.runtime = None
        if self.with_runtime:
            from .runtime import AgentRuntime
            self.runtime = AgentRuntime(self.cfg, self.personas, self.registry,
                                        self.sessions, self.data,
                                        library=self.library, kb=self.kb)
        self.watch.sync()
        print('[reload:%s] 人设 %d 个，技能 %d 个，花型 %d 款，图库 %d 张，知识库 %d 篇/%d 问答'
              % (self.who, len(self.personas), len(self.registry.skills),
                 len(self.data.get('patterns', [])), len(self.library.items),
                 len(self.kb.docs), len(self.kb.qa)))
        return self

    def chat(self, payload):
        """C 端唯一的主接口。后台进程没有 runtime，调这个会明确报错而不是 500。"""
        if not self.runtime:
            return {"ok": False, "error": "这个进程不处理对话（后台进程没有运行时）"}
        agent = payload.get('agent') or 'designer'
        message = (payload.get('message') or '').strip()
        sid = payload.get('session_id') or 'default'
        brief = payload.get('brief') or {}
        image, mime = take_image(payload)
        if not message and not image:
            return {"ok": False, "error": "message 不能为空"}
        # 「我的花型清单」：前端每次都把当前选中整份同步过来，服务端照单全收 ——
        # 客服要按这份数据导出清单，不能靠它自己去猜用户选过哪些。
        # 传 None（字段缺失）表示"这次不同步"，区别于"同步成空"。
        return self.runtime.chat(agent, message, sid, brief, image, mime,
                                 selected=payload.get('selected'))

    def maybe_reload(self):
        """每个请求进来先调一次：后台改了东西，这边要跟上。

        `changed()` 只在**别人** bump 过时返回 True —— 自己刚写的数据
        本来就在内存里，再重载一遍是白费。
        """
        try:
            if self.watch.changed():
                self.reload()
                return True
        except Exception as e:
            print('[reload] 自动重载失败：%s: %s' % (type(e).__name__, e))
        return False

    # ---------- 自检 ----------
    def health(self):
        info = self.cfg.describe()
        info.update({"ok": True, "who": self.who,
                     "personas": sorted(self.personas.keys()),
                     "skills": [s['name'] for s in self.registry.describe()],
                     "patterns": len(self.data.get('patterns', [])),
                     "library": self.library.stats(),
                     "kb": self.kb.stats(),
                     "image": self.imagegen.describe(),
                     "patgen": self.patgen.describe(),
                     "trace": self.tracegen.describe()})
        if self.runtime:
            info["vision_enabled"] = self.runtime.vision.enabled
            # 四方连续引擎是起子进程跑的，装没装好跟服务本身无关，
            # 所以单独报出来 —— 排查"为什么这个功能用不了"第一步就看它
            info["seamless"] = self.runtime.seamless.describe()
            info["psdlayer"] = self.runtime.psdlayer.describe()
        return info
