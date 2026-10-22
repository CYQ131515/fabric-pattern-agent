#!/usr/bin/env python3
"""拆分回归：C 端和后台拆成两个进程之后，那些「分开」的约定有没有守住。

**不需要服务在跑** —— 直接调模块、构造假请求对象，零网络。
真链路（端口是不是真隔离、登录是不是真拦得住、后台改完 C 端是不是真跟上）
在 tools/e2e_admin.py，那个要两个进程都起着。

这里盯的是四件事：
  A. 路由隔离：C 端不许再长出后台接口，后台不许长出对话接口
  B. 跨进程同步：变更号别自己触发自己重载，图库的「读→改→写」别互相覆盖
  C. 后台口令：不存明文、错口令进不去、令牌会过期、退出即失效
  D. HTTP 公共件：跨端口不算同源（CSRF 那一刀）

跑法：python3 tools/test_split.py
"""
import inspect
import json
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
sys.path.insert(0, BACKEND)
# 后台服务在隔壁「后台/」文件夹里（后端和后台拆成了两个目录），也要能 import 到
sys.path.insert(0, os.path.join(os.path.dirname(BACKEND), '后台'))

from core import auth, sync                                        # noqa: E402
from core.httpsrv import BaseHandler, cookie_of, query             # noqa: E402
from core.kb import KnowledgeBase                                  # noqa: E402
from core.library import PatternLibrary                            # noqa: E402

PASS = FAIL = 0


def ok(cond, label, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('  ✓ ' + label)
    else:
        FAIL += 1
        print('  ✗ ' + label + ('  → %s' % (extra,) if extra != '' else ''))


# 全部写到临时目录，不碰真实的 config/secrets.json —— 上次拿真实配置当靶子的教训
TMP = tempfile.mkdtemp(prefix='pf_split_')
os.makedirs(os.path.join(TMP, 'config'), exist_ok=True)
sync.LOCK_DIR = os.path.join(TMP, 'locks')
sync.REV_PATH = os.path.join(TMP, '.rev')
auth.ROOT = TMP
# 注意：跑完不清理这个临时目录。系统会清 /tmp，而在这里 rmtree
# 会触发沙箱的批量删除拦截，把整个测试进程打断在用例外之前。


# ============================ A. 路由隔离 ============================
def test_isolation():
    print('\n[A] 路由隔离：两个进程各管各的')
    import server
    import admin_server

    src_c = inspect.getsource(server.Handler)
    src_a = inspect.getsource(admin_server.Handler)

    ok('/api/admin/' not in src_c, 'C 端处理器里没有后台接口')
    ok('/api/chat' not in src_a, '后台处理器里没有对话接口')
    ok('/api/action' not in src_a, '后台处理器里没有「确认执行」接口')
    ok('core.admin' not in inspect.getsource(server), 'C 端不引后台模块')
    ok('core.auth' not in inspect.getsource(server), 'C 端不引口令模块')

    ok(server.APP.who == 'client', 'C 端进程身份是 client', server.APP.who)
    ok(admin_server.APP.who == 'admin', '后台进程身份是 admin', admin_server.APP.who)
    ok(server.APP.runtime is not None, 'C 端带 runtime（要能聊天）')
    ok(admin_server.APP.runtime is None, '后台不带 runtime（不聊天，省一份内存）')

    # 后台的白名单：这几个接口不登录就能问，其余一律要令牌
    open_api = set(admin_server.OPEN_API)
    ok('/api/admin/state' not in open_api, '概览接口不在白名单里（要登录）')
    ok('/api/admin/kb/save' not in open_api, '知识库写接口不在白名单里')
    ok('/api/admin/session' in open_api, '登录状态查询在白名单里（前端一进来就得问）')
    ok('/api/admin/login' in open_api, '登录接口在白名单里')


# ============================ B. 跨进程同步 ============================
def test_sync():
    print('\n[B] 跨进程同步：变更号 + 图库读改写')
    n0 = sync.rev_read()['n']
    n1 = sync.rev_bump('admin')
    ok(n1 == n0 + 1, 'bump 让变更号 +1', '%s → %s' % (n0, n1))
    ok(sync.rev_read()['by'] == 'admin', '记下了是谁改的', sync.rev_read()['by'])

    w = sync.Watcher('client')
    ok(w.changed() is False, '没人改的时候不认为变了')
    time.sleep(0.01)
    sync.rev_bump('admin')
    ok(w.changed() is True, '别人改了 → 认为自己该重载')
    w.sync()
    ok(w.changed() is False, '对齐之后不再反复重载')

    w2 = sync.Watcher('admin')
    w2.bump()
    ok(w2.changed() is False, '自己改的不触发自己重载（刚写的数据本来就在内存里）')
    ok(w2.n == sync.rev_read()['n'], '自己 bump 之后号是对齐的')

    with sync.guard('library'):
        ok(True, '跨进程锁能正常进出')
    # 锁要能重入：入库流程里嵌套调 mutate 不会自己把自己锁死
    with sync.guard('library'):
        with sync.guard('library'):
            ok(True, '同一进程内重复加锁不会死锁')

    # 图库：两个实例 = 两个进程各自的内存副本。
    # 关键在 mutate() 里那次 load() —— 不加就会「后写的把先写的整份盖掉」。
    root = os.path.join(TMP, 'lib')
    a = PatternLibrary(root=root)
    b = PatternLibrary(root=root)
    with a.mutate():
        a.items.append({'id': 'PF-LIB-2026-0001', 'name': 'A 加的'})
    with b.mutate():
        b.items.append({'id': 'PF-LIB-2026-0002', 'name': 'B 加的'})
    c = PatternLibrary(root=root)
    ids = sorted(x['id'] for x in c.items)
    ok(len(c.items) == 2, '两边各写一条，磁盘上是两条（没互相覆盖）', ids)
    ok(ids == ['PF-LIB-2026-0001', 'PF-LIB-2026-0002'], '两条都在', ids)

    # 知识库：只有一个写方，但落盘得是原子的（读的一方不能读到半截）
    kb = KnowledgeBase(root=os.path.join(TMP, 'kb'))
    ids2 = []
    guard = threading.Lock()

    def add(i):
        r = kb.add_doc('第 %d 篇\n\n这是第 %d 篇的正文内容，用来测并发。' % (i, i),
                       title='T%d' % i)
        with guard:
            ids2.append(r.get('id'))

    ts = [threading.Thread(target=add, args=(i,)) for i in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    ok(len(ids2) == 6 and len(set(ids2)) == 6, '并发加 6 篇不重号', ids2)
    ok(len(kb.docs) == 6, '六篇都在库里', len(kb.docs))


# ============================ C. 后台口令 ============================
def test_auth():
    print('\n[C] 后台口令：不存明文、错口令进不去、令牌会失效')
    ok(auth.has_password() is False, '一开始没有口令')
    r = auth.login('随便什么')
    ok(r.get('ok') is False and r.get('need_set') is True,
       '还没设口令时，登录会要求先设一个', r)

    ok(auth.set_password('abc').get('ok') is False, '口令太短被拒')
    ok(auth.set_password('test1234').get('ok') is True, '口令设上了')
    ok(auth.has_password() is True, 'has_password 变 True')

    with open(os.path.join(TMP, 'config', 'secrets.json'), encoding='utf-8') as f:
        raw = f.read()
    ok('test1234' not in raw, '口令不存明文（文件里搜不到）')
    ok(':' in (json.loads(raw).get('admin_password') or ''), '存的是 salt:hash')

    ok(auth.verify('test1234') is True, '对的口令校验通过')
    ok(auth.verify('nope') is False, '错的口令校验不通过')
    ok(auth.hash_password('同样的口令') != auth.hash_password('同样的口令'),
       '同一个口令两次哈希不一样（加了随机 salt）')

    ok(auth.login('nope').get('ok') is False, '错口令登录失败')
    r = auth.login('test1234')
    tok = r.get('token')
    ok(bool(tok), '对的口令拿到令牌')
    ok(auth.valid(tok) is True, '令牌有效')
    ok(auth.valid('我自己编一个') is False, '乱编的令牌无效')
    auth.logout(tok)
    ok(auth.valid(tok) is False, '退出登录后令牌立刻失效')

    tok2 = auth.login('test1234')['token']
    auth._TOKENS[tok2] = time.time() - 1
    ok(auth.valid(tok2) is False, '过期令牌失效（过期的不留着）')
    ok(tok2 not in auth._TOKENS, '过期的令牌顺手被清掉了')

    ok(auth.change_password('错的旧口令', 'newpass123').get('ok') is False,
       '旧口令不对不给改')
    ok(auth.change_password('test1234', 'newpass123').get('ok') is True,
       '旧口令对了能改')
    ok(auth.verify('newpass123') is True, '新口令生效')
    ok(auth.verify('test1234') is False, '旧口令作废')
    ok(auth.session()['has_password'] is True, 'session 接口如实报已设口令')


# ============================ D. HTTP 公共件 ============================
class _Fake(BaseHandler):
    """只为了测 same_origin：把 headers 塞进去就行，不走真实 socket。"""

    def __init__(self, headers):
        self.headers = headers


def test_httpsrv():
    print('\n[D] HTTP 公共件：跨端口不算同源')
    q = query('/api/library?style=%E8%8A%B1%E5%8D%89&limit=20')
    ok(q.get('style') == '花卉', 'query 能解出中文', q.get('style'))
    ok(q.get('limit') == '20', 'query 能解出普通参数')
    ok(query('/api/health') == {}, '没有 query 时给空 dict')

    ck = cookie_of({'Cookie': 'pf_admin=abc123; other=1'})
    ok(ck.get('pf_admin') == 'abc123', 'cookie 能解出来')
    ok(cookie_of({}) == {}, '没有 cookie 时给空 dict')

    ok(_Fake({'Host': '127.0.0.1:8788',
              'Origin': 'http://127.0.0.1:8788'}).same_origin() is True,
       '同源写操作放行')
    ok(_Fake({'Host': '127.0.0.1:8788',
              'Origin': 'http://127.0.0.1:8787'}).same_origin() is False,
       'C 端页面来打后台（跨端口）→ 拒绝')
    ok(_Fake({'Host': '127.0.0.1:8788',
              'Origin': 'http://evil.example.com'}).same_origin() is False,
       '外站来源 → 拒绝')
    ok(_Fake({'Host': '127.0.0.1:8788',
              'Referer': 'http://127.0.0.1:8788/'}).same_origin() is True,
       '只带 Referer 但同源 → 放行')
    ok(_Fake({'Host': '127.0.0.1:8788'}).same_origin() is True,
       '两个头都没有（curl/脚本直连）→ 放行，否则自己都没法调')


def main():
    print('临时目录：%s' % TMP)
    test_isolation()
    test_sync()
    test_auth()
    test_httpsrv()
    print('\n结果：%d 通过 / %d 失败' % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
