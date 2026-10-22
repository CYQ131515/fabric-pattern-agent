#!/usr/bin/env python3
"""管理后台在线 e2e：打真实服务，验证 B 端那套接口真的能改配置、改知识库。
跑法：python3 tools/e2e_admin.py   （先确保 python3 server.py 在跑）

和离线单测的分工：单测管 kb 的切片/检索逻辑，这里管「接口 → 落盘 → 生效」这条链。

注意：这个脚本**会真的写** config/styles.json 与知识库，
所以每条写入都要能还原 —— 跑完不该在库里留垃圾，也不该把人设改坏。
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

# 后台是独立进程（admin_server.py，默认 8788），跟 C 端 8787 不是一个地址
BASE = os.environ.get('PF_BASE', 'http://127.0.0.1:8788')
CLIENT = os.environ.get('PF_CLIENT', 'http://127.0.0.1:8787')
PW = os.environ.get('PF_ADMIN_PW', '')
PASS = FAIL = 0
MARK = 'E2E临时风格标记，跑完会还原'
# 每次跑用独立的会话：固定 session_id 会让历史越堆越长，
# 问到第七遍同样的问题时模型会开始"不耐烦"（真发生过：它回"这已经是第七遍了"），
# 于是本来就该过的断言开始随机挂。
SID = 'e2e-admin-%d' % int(time.time())
TOKEN = ''


def ok(cond, label, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('  ✓ ' + label)
    else:
        FAIL += 1
        print('  ✗ ' + label + ('  → %s' % (extra,) if extra else ''))


def _open(req, timeout=30):
    """发请求并把响应解成 dict。

    4xx/5xx **不抛异常**：后台的失败响应本身就是约定好的 JSON
    （401 未登录、口令不对…），一样要解出来看。否则调用方只能靠 try 包住，
    反而看不出服务端到底说了什么。
    """
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8') or '{}'
        try:
            obj = json.loads(raw)
        except Exception:
            obj = {"ok": False, "error": raw[:200]}
        if isinstance(obj, dict):
            obj['_status'] = e.code
        return obj


def http_code(url, method='GET'):
    """只要状态码，不要体。用来验「这个端口上根本没有这个接口」。"""
    try:
        req = urllib.request.Request(url, data=b'{}' if method == 'POST' else None,
                                     headers={'Content-Type': 'application/json'},
                                     method=method)
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return -1


def raw_status(url, method='GET'):
    """不跟重定向，返回 (状态码, Location, 响应体)。

    C 端 /admin 是指向后台的 302，跟过去就变成后台的 401 了，
    那样看不出「它是个跳转」——这个断言要的就是跳转本身。
    """
    import http.client
    from urllib.parse import urlsplit
    u = urlsplit(url)
    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=10)
    try:
        conn.request(method, u.path or '/')
        r = conn.getresponse()
        body = r.read()
        return r.status, (r.getheader('Location') or ''), body
    except Exception:
        return -1, '', b''
    finally:
        conn.close()


def get(path, timeout=20, token=None, base=None):
    h = {}
    t = token if token is not None else TOKEN
    if t:
        h['X-Admin-Token'] = t
    return _open(urllib.request.Request((base or BASE) + path, headers=h), timeout)


def post(path, body, token=None, base=None):
    h = {'Content-Type': 'application/json'}
    t = token if token is not None else TOKEN
    if t:
        h['X-Admin-Token'] = t
    req = urllib.request.Request((base or BASE) + path,
                                 data=json.dumps(body).encode('utf-8'), headers=h)
    return _open(req, 30)


def login():
    """后台现在要口令。没设口令就先不设（保持原样），设了就必须登录才继续。"""
    global TOKEN
    st = get('/api/admin/session', token='')
    ok(st.get('ok') is True, '/api/admin/session 不用登录也能问')
    ok('has_password' in st, '会话接口说了有没有设口令', st)
    if not st.get('has_password'):
        print('  （后台还没设口令，跳过登录环节）')
        return True
    if not PW:
        print('  后台设了口令，请用 PF_ADMIN_PW=xxx 传进来再跑')
        return False
    r = post('/api/admin/login', {'password': PW}, token='')
    TOKEN = r.get('token') or ''
    ok(bool(TOKEN), '口令对了拿到登录令牌', r)
    return bool(TOKEN)


def main():
    print('后台：%s   （C 端：%s）' % (BASE, CLIENT))
    try:
        health = get('/api/health')
    except Exception as e:
        print('连不上后台服务（%s）。先在 后台/ 下跑 python3 admin_server.py' % e)
        return 1
    ok(health.get('ok') is True, '后台服务在线')
    ok(health.get('who') == 'admin', '连的确实是后台进程（who=admin）', health.get('who'))
    ok('kb' in health, 'health 里带了知识库统计', sorted(health.keys()))

    print('\n[0] 端口隔离 + 登录')
    if not login():
        return finish()
    code = http_code(CLIENT + '/api/admin/state')
    ok(code == 404, 'C 端没有后台接口（%s）' % code, code)
    code2 = http_code(CLIENT + '/api/chat', 'POST')
    ok(code2 != 404, 'C 端才有对话接口（%s）' % code2, code2)
    code3 = http_code(BASE + '/api/chat', 'POST')
    ok(code3 in (401, 404), '后台不处理对话（%s）' % code3, code3)
    code4 = http_code(BASE + '/api/admin/state')
    ok(code4 == 401, '不带令牌读后台数据会被挡（%s）' % code4, code4)
    code5 = http_code(BASE + '/api/admin/kb/save', 'POST')
    ok(code5 == 401, '不带令牌写后台数据也会被挡（%s）' % code5, code5)
    r = post('/api/admin/login', {'password': '肯定不对的口令'}, token='')
    ok(r.get('ok') is False, '错口令进不去', r)

    print('\n[1] 后台概览')
    st = get('/api/admin/state')
    ok(st.get('ok') is True, '/api/admin/state 正常')
    ok(st['model']['api_key'] == '' or st['model']['api_key'].startswith('••'),
       '密钥不回显真值（要么空、要么只剩掩码）', st['model']['api_key'])
    ok('sk-' not in st['model']['api_key'], '明文 sk- 前缀没出现在响应里')
    ok('count' in st['library'], '带上了图库统计', st['library'])
    ok('docs' in st['kb'] and 'qa' in st['kb'], '带上了知识库统计', st['kb'])
    ok(len(st['presets']) == 4, '四个风格档位都在', len(st['presets']))
    ok(all('label' in p and 'key' in p for p in st['presets']), '档位带 key 和中文名')
    ok('designer' in st['styles'], '每个机器人都有风格')

    print('\n[2] 后台页面能打开（后台根路径就是后台页面）')
    with urllib.request.urlopen(BASE + '/', timeout=20) as r:
        page = r.read().decode('utf-8')
    ok(page.lstrip().startswith('<!DOCTYPE html>'), '后台根路径返回的是 HTML')
    ok('PatternFlow 管理后台' in page, '是后台页面，不是用户端原型')
    ok('花型智造' not in page.split('<style>')[0], '和用户端原型是两份独立文件')
    ok('authmask' in page, '页面里带着登录门')
    # C 端不该再服务后台页面：它只把人跳到后台那个端口
    code, loc, _ = raw_status(CLIENT + '/admin')
    ok(code == 302 and '8788' in loc,
       'C 端的 /admin 是跳到后台，不是后台页面本体', '%s %s' % (code, loc))

    print('\n[3] 配置保存：留空密钥不许把 Key 清掉')
    key_before = st['model']['api_key']
    r = post('/api/admin/config', {'llm': {'base_url': st['model']['base_url'],
                                           'model': st['model']['model']},
                                   'api_key': '', 'image_api_key': ''})
    ok(r.get('ok') is True, '保存成功', r)
    ok(r.get('changed_keys') == [], '留空 → changed_keys 为空（没动密钥）', r.get('changed_keys'))
    st2 = get('/api/admin/state')
    ok(st2['model']['api_key'] == key_before, '密钥还是原来那个，没被清空',
       st2['model']['api_key'])
    ok(st2['model']['llm_enabled'] == st['model']['llm_enabled'], '服务没掉回规则模式')

    print('\n[4] 人设与风格：能写能还原')
    pl = get('/api/admin/persona')
    ok(pl.get('ok') is True, '读到人设清单')
    ok(len(pl['items']) >= 3, '三个机器人的人设都在', len(pl['items']))
    cur = [x for x in pl['items'] if x['agent'] == 'assistant'][0]
    old_style = cur['style']['body']
    old_body = cur['body']
    post('/api/admin/persona/save',
         {'agent': 'assistant', 'body': old_body,
          'style': {'preset': cur['style']['preset'], 'body': MARK}})
    pl2 = get('/api/admin/persona')
    cur2 = [x for x in pl2['items'] if x['agent'] == 'assistant'][0]
    ok(cur2['style']['body'] == MARK, '风格写进去了并立刻可读', cur2['style']['body'][:20])
    ok(cur2['body'] == old_body, '人设全文没被顺带改坏')
    # 还原
    post('/api/admin/persona/save',
         {'agent': 'assistant', 'body': old_body,
          'style': {'preset': cur['style']['preset'], 'body': old_style}})
    cur3 = [x for x in get('/api/admin/persona')['items'] if x['agent'] == 'assistant'][0]
    ok(cur3['style']['body'] == old_style, '已还原成原来的风格')

    print('\n[5] 知识库：加文档 / 检索 / 删')
    # 先记下 C 端此刻的状态，待会用来验「后台加完 C 端能自己跟上、且没被带丢密钥」
    ch = get('/api/health', base=CLIENT)
    qa_before = int(((ch.get('kb') or {}).get('qa')) or 0)
    c1_llm = ch.get('llm_enabled')
    doc = ('E2E测试面料说明\n\n'
           '本测试面料克重 999 g/m2，门幅 111cm。\n\n'
           '交期\n测试交期 3 个工作日。\n\n'
           '价格\n测试价 1 元每米。')
    r = post('/api/admin/kb/save', {'op': 'add_doc', 'text': doc,
                                    'title': 'E2E测试资料', 'keywords': '测试价,多少钱'})
    ok(r.get('ok') is True, '文档入库', r)
    did = r.get('id')
    # 短文档（不到 320 字）本来就会被并成一片，这是对的：整篇塞得进上下文就没必要拆
    ok(r.get('chunks', 0) >= 1, '切出了片段', r.get('chunks'))
    dup = post('/api/admin/kb/save', {'op': 'add_doc', 'text': doc, 'title': '再来一次'})
    ok(dup.get('dup') is True, '同内容不重复入库', dup)

    hit = post('/api/admin/kb/save', {'op': 'lookup', 'q': '测试价', 'agent': 'assistant'})
    ok(hit.get('hit') is True, '能检索到刚加的资料', hit.get('reason'))
    ok('唯一可用的资料' in hit.get('prompt', ''), '返回了将注入的提示词（命中版）')
    miss = post('/api/admin/kb/save', {'op': 'lookup', 'q': '你们董事长属什么生肖',
                                       'agent': 'assistant'})
    ok(miss.get('hit') is False, '无关问题判定没命中', miss.get('reason'))
    ok('绝对不许' in miss.get('prompt', ''), '返回了将注入的提示词（不许编版）')

    qa = post('/api/admin/kb/save', {'op': 'add_qa', 'q': 'E2E测试问答？',
                                     'a': 'E2E标准答案：一律 42。', 'tags': ['测试']})
    ok(qa.get('ok') is True, '问答入库', qa)
    qid = qa.get('id')
    ok(post('/api/admin/kb/save', {'op': 'lookup', 'q': 'E2E测试问答',
                                   'agent': 'assistant'}).get('qa'), '问答能被检索到')

    print('\n[5b] 两个进程的同步：后台加的东西，C 端要自己跟上')
    c2 = get('/api/health', base=CLIENT)
    n2 = int((c2.get('kb') or {}).get('qa') or 0)
    ok(n2 > qa_before, 'C 端问答数 %d → %d（后台加完，C 端下一请求就跟上了）'
       % (qa_before, n2), c2.get('kb'))
    ok(c2.get('llm_enabled') == c1_llm, '同步重载没把密钥带丢')

    print('\n[6] 严格模式真的管住了机器人')
    if not health.get('llm_enabled'):
        print('  · 没配对话 Key，这一节跳过（知识库注入靠模型才看得出效果）')
        skipped = True
    else:
        skipped = False
    chat = chat2 = None
    if not skipped:
        # 聊天在 C 端进程上，不在这个后台进程上
        chat = post('/api/chat', {'agent': 'assistant', 'message': 'E2E测试问答？',
                                  'session_id': SID}, base=CLIENT)
    if not skipped:
        ok(chat.get('ok') is True, '对话成功')
        ok(chat.get('kb') and chat['kb']['hit'] is True, '这一轮命中了知识库', chat.get('kb'))
        ok('42' in (chat.get('reply') or ''), '机器人照资料答出了 42（不是自己编的）',
           (chat.get('reply') or '')[:80])
        chat2 = post('/api/chat', {'agent': 'assistant',
                                   'message': '你们公司董事长的生肖是什么',
                                   'session_id': SID}, base=CLIENT)
        ok(chat2.get('kb') and chat2['kb']['hit'] is False, '无关问题判定没资料', chat2.get('kb'))
        rep = chat2.get('reply') or ''
        ok(('没有' in rep or '不清楚' in rep or '确认' in rep),
           '机器人直说没有，没编一个生肖', rep[:80])
        ok(not any(x in rep for x in ('属龙', '属马', '属鼠', '一般来说')), '确实没编')

    print('\n[7] 花型师不吃知识库（默认只有客服）')
    chat3 = post('/api/chat', {'agent': 'designer', 'message': 'E2E测试问答？',
                               'session_id': SID}, base=CLIENT)
    ok(chat3.get('kb') is None,
       '花型师这一轮不注入知识库（默认只有客服吃知识库）', chat3.get('kb'))

    print('\n[8] 扫目录：只探测不入库')
    probe = post('/api/admin/library/scan', {'path': 'generated', 'probe': True, 'limit': 5})
    ok(probe.get('ok') is True, '探测成功', probe)
    ok(probe.get('probe') is True and probe.get('count', 0) >= 1, '扫到图片', probe.get('count'))
    ok(isinstance(probe.get('sample'), list), '返回了文件名样例')
    bad = post('/api/admin/library/scan', {'path': '/System', 'probe': True})
    ok(bad.get('ok') is False, '系统目录拒绝扫（防止手滑把 /System 吞进来）', bad)

    print('\n[9] 会话列表')
    ss = get('/api/admin/sessions')
    ok(ss.get('ok') is True, '/api/admin/sessions 正常')
    ok(ss.get('total', 0) >= 1, '至少有一个会话（刚才聊出来的）', ss.get('total'))
    ok(SID in [x['id'] for x in ss['items']] or ss['total'] > len(ss['items']),
       '刚才那个会话记下来了')

    print('\n[10] 清理：把测试资料删干净')
    ok(post('/api/admin/kb/save', {'op': 'del_doc', 'id': did}).get('ok') is True, '删文档')
    ok(post('/api/admin/kb/save', {'op': 'del_qa', 'id': qid}).get('ok') is True, '删问答')
    kb = get('/api/admin/kb')
    ok(not [d for d in kb['docs'] if d['id'] == did], '库里确实没了')
    ok(not [q for q in kb['qa'] if q['id'] == qid], '问答确实没了')

    return finish()


def finish():
    print('\n结果：%d 通过 / %d 失败' % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
