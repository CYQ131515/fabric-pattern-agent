"""会话记忆：内存态 + 追加式 jsonl 落盘（重启不丢上下文）。

**每个机器人各记各的。** 一个会话里三个机器人共用一个 session_id，但下面这些
"记忆"都按角色分槽存放：

    turns       跟用户的对话历史
    last_image  最近一张能改的底图
    last_photo  用户上传的照片（描稿仿稿用）
    last_gen    最近一次成功出图（「重新生成」重放用）

为什么必须分开：用户先让织印师做了四方连续，再切到花型师要花型图案 ——
花型师**不该**看见织印师那条线，否则它会拿刚做好的四方连续当"现有花型"，
直接提一版改图方案（真出过这个 bug）。跨角色要传图就走**用户显式交接**
（自己上传 / 从库选一张 / 点「交给织印师」），不靠会话记忆自动串。

`pending_actions`（待确认方案）本来就是分角色的，同一套思路。

`brief`（需求卡）仍然**全会话共享**：那是"客户要什么"，只有花型师/路由器
读写，本来就在一条链路上。

实现方式：私有字典按角色存；`last_image` / `last_photo` / `last_gen` 做成
**属性视图**，读写都落在"当前角色"那一槽上。技能里那句 `sess.last_image`
不用改 —— AgentRuntime 每轮开始时用 `sess.act_as(agent)` 把当前角色钉住，
谁在说话就取谁的。
"""
import base64
import json
import os
import time

# 分槽记忆的默认归属。运行时一定会先 act_as 再读写；这个只是兜底，
# 保证脚本 / 单测里直接 new 一个 Session 也能用。
DEFAULT_AGENT = 'designer'


class Session:
    def __init__(self, sid):
        self.id = sid
        self.brief = {"style": None, "element": None, "color": None, "scene": None}
        # 最近一次识图结果。**只在这一轮用**，回合结束就清掉 ——
        # 既不跨轮，也不跨重启恢复（恢复回来会变成"上个机器人看过的那张图"）。
        self.image_tags = None
        # 这一轮「导出花型清单」的结果（清单生成在调模型之前就做完了）。
        # 同样只服务当轮：喂给系统提示让模型照实报结果，回合结束清掉。
        self.export_hint = None
        # 待用户确认的动作（生图/改图/描稿），扣费闸门。
        # **按角色分开存**：一个会话会被三个机器人共用 —— 织印师留了一份描稿方案，
        # 用户切到花型师又提一版，两份既不能互相顶掉，更不能互相执行。
        self.pending_actions = {}   # {agent: pending}
        # 用户这一轮的原话。技能里要用它判断"用户是要生成，还是只是在找"
        # （「我想要一张卡通图案」不是生成指令），所以必须在调模型之前记下来。
        self.last_user_text = ''
        # 这个回合里"需求卡没补齐、被服务端打回过几次"。连打回 2 次就放行，
        # 对应人设里的「追问最多 2 轮」—— 不设上限的话，用户回一句"嗯"就卡死在追问里。
        # 纯内存态，不落盘：它只描述"眼下这一串对话"，重启后重新数。
        self.brief_blocks = 0
        self.rounds = 0
        self.updated = time.time()
        # 用户在「花型选择」里确认过的花型编号（"我的花型清单"）。
        # **全会话共享，不按角色分槽** —— 那是"这个客户看中了哪些款"，
        # 不是某个机器人自己的记忆。客服要给他导清单表就靠这一份。
        # 前端每次对话都会同步一遍（见 runtime.chat 的 selected 参数）。
        self.selected = []
        # ---------- 按角色分槽的记忆（外面只走属性/方法，别直接动这几个字典） ----------
        self._agent = DEFAULT_AGENT     # 眼下是谁在说话
        self._turns = {}                # {agent: [turn, ...]}
        self._last_image = {}           # {agent: {b64, mime, url, origin, path, prompt}}
        self._last_photo = {}           # {agent: {b64, mime, url, path}}
        self._last_gen = {}             # {agent: {name, args}}

    # ---------- 角色绑定 ----------
    def act_as(self, agent):
        """把"现在是谁在说话"钉住。不传就只返回当前角色。

        运行时每轮开头调一次，之后技能里所有 `sess.last_image` /
        `sess.last_photo` / `sess.last_gen` 的读写都自动落到这个角色头上。
        """
        if agent:
            self._agent = agent
        return self.current_agent()

    def current_agent(self):
        return self._agent or DEFAULT_AGENT

    # ---------- 分槽记忆的属性视图 ----------
    @property
    def last_image(self):
        return self._last_image.get(self.current_agent())

    @last_image.setter
    def last_image(self, rec):
        self._last_image[self.current_agent()] = rec

    @property
    def last_photo(self):
        return self._last_photo.get(self.current_agent())

    @last_photo.setter
    def last_photo(self, rec):
        self._last_photo[self.current_agent()] = rec

    @property
    def last_gen(self):
        return self._last_gen.get(self.current_agent())

    @last_gen.setter
    def last_gen(self, rec):
        self._last_gen[self.current_agent()] = rec

    # ---------- 对话历史 ----------
    def add_turn(self, agent, user, reply, skills):
        """记一轮对话，**只进这个角色自己的流水**。返回记下的那条。"""
        t = {"t": time.time(), "agent": agent, "user": user,
             "reply": reply, "skills": skills}
        arr = self._turns.setdefault(agent or DEFAULT_AGENT, [])
        arr.append(t)
        if len(arr) > 40:
            self._turns[agent or DEFAULT_AGENT] = arr[-40:]
        return t

    def history(self, agent, limit=6):
        """**只回这个角色自己的对话。**

        跨角色拼历史会把三条线搅在一起：用户在织印师那边聊的四方连续，
        会变成花型师的"上文"，于是花型师开口就是"在现有花型上改"。
        """
        out = []
        for t in (self._turns.get(agent or DEFAULT_AGENT) or [])[-limit:]:
            out.append({"role": "user", "content": t['user']})
            out.append({"role": "assistant", "content": t['reply']})
        return out

    @property
    def turns(self):
        """当前角色的对话流水（只读视图，留着给排查和单测看）。"""
        return self._turns.get(self.current_agent()) or []

    def all_turns(self):
        """**所有角色**的对话流水（含别人的）。

        只给运营统计用（判断这个会话是聊完走的还是卡住走的）——
        正常链路一律走 history()，一次只取一个角色，别在这儿抄近路。
        """
        return [t for arr in self._turns.values() for t in arr]

    def set_selected(self, ids):
        """记下用户当前选中的花型编号。去重、保序（后选的排后面）。

        前端每次对话都会把"我的花型清单"整份同步过来，所以是**覆盖**不是追加 ——
        用户在界面上删掉一款，这边也得跟着少一款。
        """
        out = []
        for pid in (ids or []):
            pid = str(pid or '').strip()
            if pid and pid not in out:
                out.append(pid)
        self.selected = out
        return out


class SessionStore:
    def __init__(self, directory, resolve=None):
        self.dir = directory
        os.makedirs(directory, exist_ok=True)
        self._mem = {}
        # 前端拿到的图片地址（/api/file/<名字>）→ 本地路径。
        # 恢复底图时用：老记录里可能只有 url 没有 path。
        self.resolve = resolve

    def _locate(self, rec):
        """找出这张图在磁盘上的位置：先看 path，再拿 url 反查。

        为什么要有第二步：底图记录曾经只写 url 不写 path，
        一重启就"图还在、会话记不起来"，改图直接说没有底图（真出过）。
        """
        p = rec.get('path')
        if p and os.path.exists(p):
            return p
        url = rec.get('url') or ''
        if url and self.resolve:
            try:
                q = self.resolve(url)
                if q and os.path.exists(q):
                    return q
            except Exception as e:
                print('[session] 按地址找图失败 %s：%s' % (url, e))
        return None

    def get(self, sid):
        sid = (sid or 'default').strip() or 'default'
        if sid not in self._mem:
            s = Session(sid)
            path = os.path.join(self.dir, sid + '.jsonl')
            if os.path.exists(path):
                try:
                    with open(path, encoding='utf-8') as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            rec = json.loads(line)
                            self._restore(s, rec)
                except Exception as e:
                    print('[session] 读取失败 %s：%s' % (sid, e))
            self._mem[sid] = s
        return self._mem[sid]

    def _restore(self, s, rec):
        kind = rec.get('type')
        if kind == 'brief':
            s.brief.update(rec.get('brief') or {})
        elif kind == 'turn':
            turn = rec.get('turn') or {}
            who = turn.get('agent') or DEFAULT_AGENT
            s._turns.setdefault(who, []).append(turn)
            s.rounds = rec.get('rounds', s.rounds)
        elif kind == 'selected':
            # "我的花型清单"：**不分角色**，全会话一份（见 Session.selected 的说明）
            s.selected = [str(x) for x in (rec.get('selected') or []) if x]
        elif kind == 'pending':
            # 只有带 agent 的记录才恢复；老格式（无 agent）是待确认方案还没
            # 按角色隔离时的产物，恢复回来只会再串一次台。
            who = rec.get('agent')
            if who:
                if rec.get('pending'):
                    s.pending_actions[who] = rec['pending']
                else:
                    s.pending_actions.pop(who, None)
        elif kind in ('last_image', 'last_photo', 'last_gen'):
            # 同理：这几样也都按角色存了，**无 agent 的老记录一律丢弃**。
            # 分不清是谁的那份，恢复回来就是给下一个人当"上文"用（正是要杜绝的）。
            who = rec.get('agent')
            if not who:
                return
            if kind == 'last_gen':
                s._last_gen[who] = rec.get('last_gen')
                return
            blob = rec.get(kind) or {}
            p = self._locate(blob)
            if not p:
                return
            try:
                with open(p, 'rb') as fh:
                    blob['b64'] = base64.b64encode(fh.read()).decode()
            except Exception as e:
                print('[session] 图体读回失败 %s：%s' % (p, e))
                return
            blob['path'] = p
            (s._last_image if kind == 'last_image' else s._last_photo)[who] = blob
        # 'image'（识图结论）刻意不恢复：它只服务当轮，落盘只是为了留痕。

    def append(self, session, rec):
        try:
            with open(os.path.join(self.dir, session.id + '.jsonl'), 'a', encoding='utf-8') as f:
                f.write(json.dumps(rec, ensure_ascii=False) + '\n')
        except Exception as e:
            print('[session] 写入失败：%s' % e)
