"""知识库：让机器人「只按资料答」。

和检索的两个不同点，写在最前面免得后面改串：
1. **切片不是给人看的，是给模型看的**。一片太长会占满上下文，太短会丢上下文。
   默认 320 字一片、重叠 60 字 —— 重叠是为了让跨片的句子至少有一片是完整的。
2. **命中与否是个硬判定，不是排序**。页面检索可以「沾边也带回来」，
   知识库不行：没过阈值就是没资料，运行时要据此下「不许编」的指令。
   所以 `lookup()` 一定回 `hit:bool`，别让调用方自己看分数猜。

两种内容：
- 文档（doc）：上传 txt/md 或粘贴 → 自动切片
- 问答对（qa）：一问一答，优先级高于文档片段，命中后**原样照答**

检索：中文没有空格，用**字符 bigram** 重合度打分（零依赖且对中文够用）。
"""
import hashlib
import json
import os
import re
import threading
import time

from . import sync
# 知识库**不**上跨进程文件锁，是有意的：只有后台进程写它（C 端只读），
# 不存在两个写方互相覆盖的问题；落盘又是 os.replace 原子写，
# 读的一方要么拿到旧版要么拿到新版，不会读到半截。
# 图库不一样 —— C 端出图自动入库、后台扫目录入库，两边都在写，所以那边必须加锁。

# 切片
CHUNK_SIZE = 320
CHUNK_OVERLAP = 60
MIN_CHUNK = 24          # 短于这个的碎片直接丢（多半是空行/标题）

# 命中阈值：bigram 重合度的下限。低于它就是「资料里没有」
# 定 0.18 是偏松的：宁可多给一点让它照着答，也不要动不动就说不知道。
# 但也不能太低，否则随便问一句都能捞到一段不相干的。
SCORE_MIN = 0.18
QA_SCORE_MIN = 0.24     # 问答对要求更严：它命中就是"原样照答"，错了很扎眼

TOP_K = 4               # 最多注入几片
MAX_CHARS = 2400        # 注入正文总字数上限，超了从尾巴砍


def _now():
    return time.strftime('%Y-%m-%d %H:%M:%S')


def _bigrams(text):
    """字符 bigram 集合。中文按字切，英文/数字按词切，标点忽略。

    纯按字切会把 "abc" 切成 "ab"/"bc"，和中文混在一起权重会很怪，
    所以先把连续的英文数字串当成一个整体。
    """
    toks = re.findall(r'[a-zA-Z0-9]+|[\u4e00-\u9fff]', (text or '').lower())
    out = set()
    for t in toks:
        if len(t) == 1:
            out.add(t)
        else:
            for i in range(len(t) - 1):
                out.add(t[i:i + 2])
    # 单字也补进去：短的问句（"运费？"）才能匹配上
    for t in toks:
        if len(t) > 1:
            out.add(t[0])
    return out


def _score(query_bg, text_bg):
    """重合度：交集 / 问句长度。除以问句长度，长文档才不会因为字多而占便宜。"""
    if not query_bg or not text_bg:
        return 0.0
    inter = len(query_bg & text_bg)
    if not inter:
        return 0.0
    return inter / float(len(query_bg))


class KnowledgeBase:
    def __init__(self, config=None, root=None):
        self.cfg = config
        conf = ((config or {}).settings if hasattr(config, 'settings') else {}) or {}
        self.conf = conf.get('kb') or {}
        self.enabled = bool(self.conf.get('enabled', True))
        # 哪些角色吃知识库。默认是"只有客服"，花型师出图为主，塞一堆资料反而压住构图指令
        self.agents = self.conf.get('agents') or ['assistant']
        self.top_k = int(self.conf.get('top_k') or TOP_K)
        self.score_min = float(self.conf.get('score_min') or SCORE_MIN)
        self.root = root or os.path.join(
            getattr(config, 'ROOT', os.getcwd()) if config else os.getcwd(), 'kb')
        self.dir_docs = os.path.join(self.root, 'docs')
        self.path_index = os.path.join(self.root, 'index.json')
        self._lock = threading.RLock()
        os.makedirs(self.dir_docs, exist_ok=True)
        self.docs = {}      # id -> {id,title,chars,chunks,created,source}
        self.qa = []        # [{id,q,a,tags,created}]
        self._bg = {}       # 缓存 bigram：key -> set
        self._load()

    # ---------- 持久化 ----------
    def _load(self):
        idx = {}
        if os.path.exists(self.path_index):
            try:
                with open(self.path_index, encoding='utf-8') as f:
                    idx = json.load(f) or {}
            except Exception as e:
                print('[kb] index.json 读不了（%s），按空库启动' % e)
        self.docs = {}
        for meta in (idx.get('docs') or []):
            p = os.path.join(self.dir_docs, meta['id'] + '.json')
            if not os.path.exists(p):
                continue
            try:
                with open(p, encoding='utf-8') as f:
                    d = json.load(f)
            except Exception:
                continue
            d['_path'] = p
            self.docs[d['id']] = d
        self.qa = [q for q in (idx.get('qa') or []) if q.get('q') and q.get('a')]
        self._bg = {}

    def _write_index(self):
        idx = {
            "docs": [{"id": d['id'], "title": d.get('title'), "chars": d.get('chars', 0),
                      "chunks": len(d.get('chunks') or []), "created": d.get('created'),
                      "source": d.get('source')}
                     for d in self.docs.values()],
            "qa": [{"id": q['id'], "q": q['q'], "a": q['a'], "tags": q.get('tags') or [],
                    "created": q.get('created')} for q in self.qa],
            "updated": _now(),
        }
        tmp = self.path_index + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(idx, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path_index)
        # 广播：C 端进程内存里也有一份知识库，不通知它就永远用旧的
        sync.rev_bump('kb')

    def _write_doc(self, d):
        p = os.path.join(self.dir_docs, d['id'] + '.json')
        tmp = p + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, p)

    # ---------- 切片 ----------
    @staticmethod
    def split(text, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
        """按标点优先、长度兜底切。返回 [{seq, head, text}]。

        **小节标题要跟着片段走**：文档里「价格」这种标题常常自己占一行，
        正文里反而不出现这个词。标题若被当成短碎片并进上一段，
        用户问「价格」就永远捞不到那段正文（实测就踩了这个）。
        所以单独占行、短、不以句号结尾的，一律当标题记下来，
        贴在它后面每个片段上 —— 参与检索，但不算进正文字数。
        """
        text = (text or '').replace('\r\n', '\n').strip()
        if not text:
            return []
        head_re = re.compile(r'^#{1,6}\s*')
        segs = []          # [(head, body)]
        cur_head = ''
        for para in re.split(r'\n+', text):
            para = para.strip()
            if not para:
                continue
            line = head_re.sub('', para).strip()
            is_head = (len(line) <= 30 and not re.search(r'[。！？；，、,]$', line)
                       and not re.search(r'[0-9]{2,}', line))
            if is_head:
                cur_head = line
                continue
            if len(para) <= size:
                segs.append((cur_head, para))
                continue
            buf = ''
            for sent in re.split(r'(?<=[。！？；!?;])', para):
                if not sent:
                    continue
                if len(buf) + len(sent) > size and buf:
                    segs.append((cur_head, buf))
                    # 重叠：把上一片尾巴带过来，跨片句子才不会两边都残缺
                    buf = (buf[-overlap:] if overlap else '') + sent
                else:
                    buf += sent
            if buf:
                segs.append((cur_head, buf))
        # 整篇只有标题（一行短文档）→ 别让库里空着
        if not segs and cur_head:
            segs = [('', cur_head)]
        # 太短的碎片并进上一片，别单独占一格（列点、残句）
        merged = []
        for head, body in segs:
            body = body.strip()
            if not body:
                continue
            if merged and len(body) < MIN_CHUNK:
                merged[-1] = (merged[-1][0], merged[-1][1] + body)
            else:
                merged.append((head, body))
        return [{"seq": i, "head": h, "text": b} for i, (h, b) in enumerate(merged)]

    # ---------- 文档 ----------
    @staticmethod
    def parse_keywords(raw):
        """关键词/别名：逗号、空格、顿号都认。给运营一个能补召回的口子。"""
        if not raw:
            return []
        if isinstance(raw, (list, tuple)):
            items = [str(x).strip() for x in raw]
        else:
            items = re.split(r'[,，、\s;；]+', str(raw))
        out = []
        for x in items:
            if x and x not in out:
                out.append(x)
        return out[:12]

    def add_doc(self, text, title=None, source=None, keywords=None):
        with self._lock:
            text = (text or '').strip()
            if not text:
                return {"ok": False, "error": "内容为空"}
            md5 = hashlib.md5(text.encode('utf-8')).hexdigest()[:12]
            for d in self.docs.values():
                # 同内容不重复入库：用户手抖连点两次会存两份
                if d.get('md5') == md5:
                    return {"ok": True, "dup": True, "id": d['id'],
                            "note": "这份资料已经在库里了（内容完全相同）"}
            chunks = self.split(text)
            if not chunks:
                return {"ok": False, "error": "切不出有效片段"}
            did = 'd' + md5
            while did in self.docs:
                did += 'x'
            title = (title or '').strip() or self._guess_title(text)
            self.docs[did] = {
                "id": did, "title": title, "md5": md5, "chars": len(text),
                "chunks": chunks, "created": _now(), "source": source or 'upload',
                "keywords": self.parse_keywords(keywords),
            }
            self._write_doc(self.docs[did])
            self._write_index()
            self._bg.pop(('d', did), None)
            return {"ok": True, "id": did, "title": title, "chunks": len(chunks),
                    "chars": len(text)}

    @staticmethod
    def _guess_title(text):
        for line in text.split('\n'):
            line = line.strip().lstrip('#').strip()
            if line:
                return line[:40]
        return '未命名资料'

    def remove_doc(self, did):
        with self._lock:
            d = self.docs.pop(did, None)
            if not d:
                return {"ok": False, "error": "没有这份资料"}
            p = os.path.join(self.dir_docs, did + '.json')
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
            self._write_index()
            return {"ok": True, "id": did}

    # ---------- 问答对 ----------
    def add_qa(self, q, a, tags=None):
        with self._lock:
            q = (q or '').strip()
            a = (a or '').strip()
            if not q or not a:
                return {"ok": False, "error": "问题和答案都要填"}
            for it in self.qa:
                if it['q'] == q:
                    it['a'] = a
                    it['tags'] = tags or it.get('tags') or []
                    it['created'] = _now()
                    self._write_index()
                    return {"ok": True, "id": it['id'], "updated": True,
                            "note": "这个问题已经有了，答案已更新"}
            qid = 'q' + hashlib.md5(q.encode('utf-8')).hexdigest()[:10]
            while any(x['id'] == qid for x in self.qa):
                qid += 'x'
            self.qa.append({"id": qid, "q": q, "a": a, "tags": tags or [],
                            "created": _now()})
            self._write_index()
            return {"ok": True, "id": qid, "q": q}

    def update_qa(self, qid, fields):
        with self._lock:
            for it in self.qa:
                if it['id'] == qid:
                    for k in ('q', 'a', 'tags'):
                        if k in (fields or {}):
                            it[k] = fields[k]
                    self._write_index()
                    return {"ok": True, "item": it}
            return {"ok": False, "error": "没有这条问答"}

    def remove_qa(self, qid):
        with self._lock:
            n = len(self.qa)
            self.qa = [x for x in self.qa if x['id'] != qid]
            if len(self.qa) == n:
                return {"ok": False, "error": "没有这条问答"}
            self._write_index()
            return {"ok": True, "id": qid}

    # ---------- 检索 ----------
    def _bg_of(self, key, text):
        got = self._bg.get(key)
        if got is None:
            got = _bigrams(text)
            self._bg[key] = got
        return got

    def lookup(self, query, agent=None, top_k=None):
        """返回 {hit, qa[], chunks[], best, reason}。

        `hit` 是硬判定 —— 运行时据此下「照资料答」还是「不许编」。
        """
        out = {"ok": True, "hit": False, "qa": [], "chunks": [], "best": 0.0,
               "reason": "", "strict": True}
        if agent is not None and self.agents and agent not in self.agents:
            out['reason'] = '当前角色未启用知识库'
            out['strict'] = False
            return out
        q = (query or '').strip()
        if not q:
            out['reason'] = '空问题'
            return out
        if not self.docs and not self.qa:
            out['reason'] = '知识库是空的'
            return out
        qb = _bigrams(q)
        if not qb:
            out['reason'] = '问题里没有可匹配的内容'
            return out
        k = top_k or self.top_k

        # 1) 问答对优先：命中就是"原样照答"，所以阈值更严
        scored = []
        for it in self.qa:
            s = _score(qb, self._bg_of(('q', it['id']), it['q']))
            for t in (it.get('tags') or []):
                if t and t in q:
                    s += 0.15
            if s >= max(QA_SCORE_MIN, self.score_min):
                scored.append((s, it))
        scored.sort(key=lambda x: -x[0])
        out['qa'] = [{"id": it['id'], "q": it['q'], "a": it['a'], "score": round(s, 3)}
                     for s, it in scored[:2]]

        # 2) 文档片段
        # 文档级提权：关键词/别名命中就把整篇片段往上抬。
        # 「多少钱一米」和正文里的「价格」一个字都不重合，纯字面检索捞不到，
        # 运营在后台挂个别名是唯一能补的口子 —— 所以这个提权必须存在。
        bonus = {}
        for d in self.docs.values():
            b = 0.0
            for kw in (d.get('keywords') or []):
                if kw and (kw in q or _score(qb, _bigrams(kw)) > 0.5):
                    b = max(b, 0.30)
            t = (d.get('title') or '').strip()
            if t and (t in q or (len(t) >= 2 and q in t)):
                b = max(b, 0.30)
            bonus[d['id']] = b
        cs = []
        for d in self.docs.values():
            for c in (d.get('chunks') or []):
                # 标题和正文一起参与匹配，光靠正文会漏掉「价格」这类只出现在标题上的词
                hay = ((c.get('head') or '') + c['text'])
                s = _score(qb, self._bg_of(('c', d['id'], c['seq']), hay))
                s += bonus.get(d['id'], 0.0)
                if s >= self.score_min:
                    cs.append((s, d, c))
        cs.sort(key=lambda x: -x[0])
        for s, d, c in cs[:k]:
            out['chunks'].append({"doc": d['id'], "title": d.get('title'),
                                  "head": c.get('head'), "seq": c['seq'],
                                  "text": c['text'], "score": round(s, 3)})
        out['best'] = round(max([x['score'] for x in out['qa']] +
                                [x['score'] for x in out['chunks']] or [0.0]), 3)
        out['hit'] = bool(out['qa'] or out['chunks'])
        out['reason'] = ('命中 %d 条问答 / %d 段资料' % (len(out['qa']),
                                                    len(out['chunks']))
                         if out['hit'] else '资料里没有相关内容（最高 %.2f，阈值 %.2f）'
                         % (out['best'], self.score_min))
        return out

    def render_for_prompt(self, res, max_chars=MAX_CHARS):
        """把命中结果渲染成系统提示里的一块。没命中就返回"不许编"的指令块。

        **这里返回的东西会直接决定模型的回答边界**，措辞要写死，不能让模型自己判断。
        """
        if not res or not res.get('hit'):
            return ('\n## 知识库（严格模式）\n'
                    '- 知识库里**没有**和用户这个问题相关的资料。\n'
                    '- **绝对不许**用你自己的知识去回答这个问题，也不许编造面料参数、'
                    '工艺、价格、交期、起订量这类具体信息。\n'
                    '- 直接说你这边没有这份资料，说清楚你会去确认，'
                    '并主动问他还想了解什么别的（别的可以照常答）。\n'
                    '- 不要用「一般来说」「通常情况下」这类含糊话术蒙过去 —— 那是编。')
        lines = ['\n## 知识库（严格模式）']
        lines.append('- 下面这些是**唯一可用的资料**。回答必须完全基于它们，'
                     '不许补充资料外的信息，不许换算、不许推导、不许举例。')
        lines.append('- 资料里没有的点，直接说没有。')
        lines.append('- 可以口语化组织语言，但**事实和数字一个字都不能改**。')
        if res.get('qa'):
            lines.append('\n### 标准问答（优先级最高，命中就照这个答）')
            for it in res['qa']:
                lines.append('问：%s\n答：%s' % (it['q'], it['a']))
        if res.get('chunks'):
            lines.append('\n### 相关资料片段')
            used = 0
            for c in res['chunks']:
                if used + len(c['text']) > max_chars:
                    break
                used += len(c['text'])
                tag = c.get('head') or c.get('title') or c.get('doc')
                lines.append('[%s·第%d段] %s' % (tag, c.get('seq', 0) + 1, c['text']))
        return '\n'.join(lines)

    # ---------- 概览 ----------
    def stats(self):
        n_chunk = sum(len(d.get('chunks') or []) for d in self.docs.values())
        n_char = sum(d.get('chars', 0) for d in self.docs.values())
        return {"enabled": self.enabled, "docs": len(self.docs),
                "chunks": n_chunk, "chars": n_char, "qa": len(self.qa),
                "agents": list(self.agents), "root": self.root}

    def list_docs(self):
        return [{"id": d['id'], "title": d.get('title'), "chars": d.get('chars', 0),
                 "chunks": len(d.get('chunks') or []), "created": d.get('created'),
                 "source": d.get('source'), "keywords": d.get('keywords') or []}
                for d in self.docs.values()]

    def list_qa(self):
        return [dict(x) for x in self.qa]

    def doc_text(self, did):
        d = self.docs.get(did)
        if not d:
            return None
        return '\n'.join(c['text'] for c in (d.get('chunks') or []))
