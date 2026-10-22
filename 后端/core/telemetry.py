"""埋点事件：把「发生过什么」追加式落盘，给后台的数据看板统计用。

**为什么不塞进会话 jsonl。** 那边的记录是*会话上下文*（重启要能恢复、要能被
技能读到），埋点是*运营数据*（谁在用、失败在哪、卡在哪一步）。两者寿命、读法、
用途都不一样 —— 混在一起，会话恢复逻辑得被一堆统计字段拖着，而且统计口径一改
老记录就全废。

**为什么不引数据库。** 整个项目是零第三方依赖。这个量级（一个原型一天几十到
几百条）用「一天一个 jsonl、只追加」足够，坏了还能直接用文本工具看。

**写入一律 best-effort。** 埋点失败绝不能让用户拿不到图，所以 record() 吞掉
所有异常，只往服务端日志留一行。

事件（k）与关键字段 —— 六个都对应后台「还缺什么数据」里列的那六件事：

    visit     来路、屏幕尺寸                      ref / w / h / page
    click     点了哪个按钮                        act / page / agent
    leave     什么时候走的、算不算「聊完」          dwell / reason
    gen       一次出图：成没成、多久、几点、几张    ok / reason / ms / points / n / pid
    proposal  挂出一张待确认卡片（报价）            skill / cost / n / pid
    kb        一次知识库检索命中没命中              hit / best / qa / chunks / q

`pid` 是提案编号：同一张卡片上切张数/还原度会重跑提案，pid 不变，
所以「转化率」不会被用户自己的按钮刷低。
"""
import json
import os
import threading
import time

DAY = 86400


class Telemetry:
    """一天的追加写入口。线程安全（C 端是多线程 HTTP 服务）。"""

    def __init__(self, directory, enabled=True):
        self.dir = directory or ''
        self.enabled = bool(directory) and bool(enabled)
        self._lock = threading.Lock()
        self._ready = False

    @classmethod
    def from_config(cls, cfg):
        """按配置找目录。环境变量优先，方便测试与临时排查：

            PATTERNFLOW_TELEMETRY_DIR=/tmp/x   换个地方落盘
            PATTERNFLOW_TELEMETRY=0            整个关掉
        """
        d = (os.environ.get('PATTERNFLOW_TELEMETRY_DIR') or '').strip()
        if not d:
            try:
                d = cfg.path('telemetry')
            except Exception:                        # noqa: BLE001
                d = ''
        off = (os.environ.get('PATTERNFLOW_TELEMETRY') or '').strip().lower()
        return cls(d, enabled=off not in ('0', 'off', 'false', 'no'))

    def _ensure(self):
        # 「建过」不缓存成永久事实：目录被运维脚本/清理工具删掉之后，
        # 只认 _ready 会让后面每一次写入都静默失败（record 吞异常），
        # 看板上从此永远是空的、而且一声不吭 —— 排查起来极其费劲。
        # 每次多一个 stat，换来的是「删了会自己长回来」。
        if self._ready and os.path.isdir(self.dir):
            return
        os.makedirs(self.dir, exist_ok=True)
        self._ready = True

    def path_of(self, ts=None):
        return os.path.join(self.dir, time.strftime(
            '%Y-%m-%d', time.localtime(ts if ts else time.time())) + '.jsonl')

    def record(self, kind, **fields):
        """记一件事。返回 True/False，**永远不抛异常**。"""
        if not self.enabled or not kind:
            return False
        rec = {"k": str(kind)[:24], "t": round(time.time(), 3)}
        for k, v in fields.items():
            if v is None or v == '':
                continue
            rec[k] = v
        line = json.dumps(rec, ensure_ascii=False)
        try:
            with self._lock:                 # 进程内串行；跨进程靠 O_APPEND
                self._ensure()
                with open(self.path_of(rec['t']), 'a', encoding='utf-8') as f:
                    f.write(line + '\n')
            return True
        except Exception as e:               # noqa: BLE001
            print('[telemetry] 写入失败：%s' % e)
            return False


def load(directory, days=14, now=None):
    """读近 N 天的事件（含今天），按时间升序。

    只按「天文件」挑，不做二次时间过滤：跨零点时文件归属的那一天就是它本身，
    再按 t 卡一遍反而会把边界上的事件漏掉。坏行跳过 —— 半个写坏的 json
    不该让整个看板打不开。
    """
    if not directory or not os.path.isdir(directory):
        return []
    now = now if now is not None else time.time()
    try:
        days = max(1, min(400, int(days)))
    except (TypeError, ValueError):
        days = 14
    out = []
    for i in range(days - 1, -1, -1):
        p = os.path.join(directory, time.strftime(
            '%Y-%m-%d', time.localtime(now - i * DAY)) + '.jsonl')
        if not os.path.exists(p):
            continue
        try:
            with open(p, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:            # noqa: BLE001
                        continue
                    if isinstance(rec, dict) and rec.get('k'):
                        out.append(rec)
        except OSError:
            continue
    out.sort(key=lambda r: r.get('t') or 0)
    return out


def by_kind(events):
    """按事件类型分桶，省得每个统计各扫一遍全表。"""
    out = {}
    for e in events:
        out.setdefault(e.get('k'), []).append(e)
    return out


def ref_host(raw):
    """把来路（Referer）压成主机名，**只留域名，去掉路径和参数**。

    为什么必须砍：Referer 是上一页的完整地址，带着搜索词、token、订单号这类东西。
    做访问来源统计只需要域名，多存的每一个字都是白捡的风险 ——
    所以这条在**写入时**就砍掉，磁盘上不留完整 URL。
    """
    s = (raw or '').strip()
    if not s:
        return ''
    try:
        from urllib.parse import urlsplit
        host = (urlsplit(s if '//' in s else '//' + s).netloc or '').lower()
    except Exception:                                # noqa: BLE001
        return ''
    if host.startswith('www.'):
        host = host[4:]
    # 只认长得像主机的（带点、localhost、或 [IPv6]）。前面那步对不认识的字符串
    # 会原样吐回来 —— 那就等于把一段不明来源的文本摆到看板上，不如丢掉。
    bare = host.split(':')[0]
    if '.' not in bare and bare != 'localhost' and not bare.startswith('['):
        return ''
    # 端口只在非默认时才留（本机调试时能分清 8787/8790）
    return host[:80]
