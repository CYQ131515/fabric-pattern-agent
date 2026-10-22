"""跨进程同步：文件锁 + 变更通知。

拆成两个进程（C 端 8787 / 后台 8788）之后，两边会同时读写同一批文件：
后台扫目录往图库里塞图，C 端出图也在往同一个 index.json 里塞。
单进程时代靠 threading.Lock 就够了，现在那把锁**只在同一进程内有效**，
两边各写各的会互相覆盖——所以这里补两件东西：

1. `guard(name)`：跨进程排他锁（fcntl.flock），包住「读索引 → 改 → 写回」；
2. `rev`：变更计数器。谁改了磁盘上的配置/知识库/图库就 bump 一下，
   另一个进程下次处理请求时发现号变了就重新加载。

不用「后台主动通知 C 端」那套，是因为 C 端可能没起、也可能晚起。
用文件当中间人，谁在什么时候起来都能对齐。
"""
import json
import os
import threading
import time

try:
    import fcntl
except ImportError:                       # Windows 兜底：退化成仅进程内锁
    fcntl = None

from .config import ROOT

LOCK_DIR = os.path.join(ROOT, 'config', '.locks')
REV_PATH = os.path.join(ROOT, 'config', '.rev')

_TL = {}                                  # name -> threading.RLock（进程内先挡一层）
_TL_GUARD = threading.Lock()
_FDS = {}                                 # name -> 已打开的锁文件 fd（进程内复用）
_FDS_GUARD = threading.Lock()


def _thread_lock(name):
    with _TL_GUARD:
        lk = _TL.get(name)
        if lk is None:
            lk = threading.RLock()
            _TL[name] = lk
        return lk


def _fd(name):
    """每个进程、每个名字只开一次锁文件。重复开会让 flock 在进程内互相打架。"""
    with _FDS_GUARD:
        fd = _FDS.get(name)
        if fd is not None:
            return fd
        os.makedirs(LOCK_DIR, exist_ok=True)
        p = os.path.join(LOCK_DIR, name + '.lock')
        fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o600)
        _FDS[name] = fd
        return fd


class _Guard:
    def __init__(self, name):
        self.name = name
        self.tl = _thread_lock(name)

    def __enter__(self):
        self.tl.acquire()
        if fcntl is not None:
            try:
                fcntl.flock(_fd(self.name), fcntl.LOCK_EX)
            except OSError:
                pass                     # 锁不住也不能崩，最多是并发写偶尔丢一次
        return self

    def __exit__(self, *exc):
        if fcntl is not None:
            try:
                fcntl.flock(_fd(self.name), fcntl.LOCK_UN)
            except OSError:
                pass
        self.tl.release()
        return False


def guard(name):
    """跨进程排他锁。用法：`with sync.guard('library'): ...`

    注意 flock 是**劝告锁**：两个进程都得自觉走这个函数才有效。
    """
    return _Guard(name)


# ---------------- 变更通知 ----------------
WHO = {'name': ''}


def set_who(name):
    """本进程叫什么（'client' / 'admin'）。自己 bump 的号不会触发自己重载。"""
    WHO['name'] = name


def who():
    return WHO['name']


def rev_read():
    """返回 {"n": int, "by": str, "t": float}。文件不存在当 0 处理。"""
    try:
        with open(REV_PATH, encoding='utf-8') as f:
            d = json.load(f)
        if isinstance(d, dict) and 'n' in d:
            return {"n": int(d['n']), "by": d.get('by') or '', "t": d.get('t') or 0}
    except Exception:
        pass
    return {"n": 0, "by": '', "t": 0}


def rev_bump(by=None):
    """有人改了磁盘上的东西 → 号 +1。返回新的号。

    by 不传就用本进程名（set_who 设过的话），这样自己改的不会触发自己重载。
    """
    with guard('rev'):
        cur = rev_read()
        nxt = {"n": cur['n'] + 1, "by": by or who(), "t": time.time()}
        tmp = REV_PATH + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(nxt, f)
            os.replace(tmp, REV_PATH)
        except OSError as e:
            print('[sync] 写变更标记失败：%s' % e)
            return cur['n']
        return nxt['n']


class Watcher:
    """一个进程持有一个：比对号变了没有，变了就让调用方重新加载。

    `bump()` 之后自己这边不会触发重载（自己刚写的数据本来就在内存里）。
    """

    def __init__(self, me=''):
        self.me = me or who()
        self.n = rev_read()['n']

    def bump(self):
        self.n = rev_bump(self.me)
        return self.n

    def changed(self):
        """别人改了磁盘上的东西吗？是就返回 True（调用方应重新加载）。

        自己改的只把号对齐、不重载——刚写进去的数据本来就在内存里，
        再重载一遍纯属浪费，还可能在别的线程正写入时把对象换掉。
        """
        try:
            cur = rev_read()
        except Exception:
            return False
        if cur['n'] == self.n:
            return False
        if cur['by'] and cur['by'] == self.me:
            self.n = cur['n']
            return False
        return True

    def sync(self):
        """把已知号对齐到磁盘上的当前号。重载之后调一次。"""
        self.n = rev_read()['n']
        return self.n
