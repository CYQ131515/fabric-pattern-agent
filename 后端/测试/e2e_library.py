#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真链路：图片入库 → AI 打标签 → 按客户要求检索出真图 → 拿库图去改图。

必须连着本地服务跑（后台和 C 端是两个进程，两个都要起）：
    python3 server.py --port 8787 &
    python3 admin_server.py --port 8788 &
    python3 tools/e2e_library.py

用真实模型打标签（一次约 5 秒），不产生生图费用：
「拿去改图」只验到"底图已换成库图"，不真的出图。

B/C 分开之后，图的**写**操作（入库 / 改标签 / 重打标签）只在后台 8788，
C 端 8787 只剩读（检索、取图）。所以这个脚本要打两个地址。
"""
import base64
import json
import os
import sys
import time
import urllib.parse
import urllib.request

BASE = os.environ.get('AGENT_BASE', 'http://127.0.0.1:8787')          # C 端：读
ADMIN = os.environ.get('AGENT_ADMIN', 'http://127.0.0.1:8788')        # 后台：写
ADMIN_PW = os.environ.get('PF_ADMIN_PW', '')                          # 后台口令
SID = os.environ.get('E2E_SID', 'e2e-lib-%d' % int(time.time()))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

PASS, FAIL = 0, 0


def ok(cond, name, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('  OK   %s' % name)
    else:
        FAIL += 1
        print('  FAIL %s%s' % (name, (' → ' + str(extra)) if extra else ''))


def skip(name):
    """环境不具备（比如没配模型 Key）→ 跳过，不计失败。

    要跟「真的坏了」分清楚，否则跑一次全是红的，看不出到底哪出了事。
    """
    print('  ·    %s（环境不具备，跳过）' % name)


TOKEN = ''


def _raw(base, path, payload=None, timeout=240, method=None, token=''):
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    h = {'Content-Type': 'application/json'}
    if token:
        h['X-Admin-Token'] = token
    r = urllib.request.Request(base + path, data=data, headers=h,
                               method=method or ('POST' if data else 'GET'))
    with urllib.request.urlopen(r, timeout=timeout) as f:
        return json.loads(f.read().decode('utf-8'))


def req(path, payload=None, timeout=240, method=None):
    """C 端（8787）：检索、取图、对话。"""
    return _raw(BASE, path, payload, timeout, method)


def admin_req(path, payload=None, timeout=240, method=None):
    """后台（8788）：入库、改标签、重打标签 —— 要带登录令牌。"""
    return _raw(ADMIN, path, payload, timeout, method, TOKEN)


def admin_login():
    """后台设了口令就登录拿令牌；没设就直接过（首次使用的机器）。"""
    global TOKEN
    try:
        st = _raw(ADMIN, '/api/admin/session')
    except Exception as e:
        print('后台没起（%s）—— 图库写操作验不了' % e)
        return False
    if not st.get('has_password'):
        return True
    pw = ADMIN_PW
    if not pw:
        print('后台设了口令，用 PF_ADMIN_PW 环境变量传进来才能验写操作')
        return False
    r = _raw(ADMIN, '/api/admin/login', {'password': pw})
    TOKEN = r.get('token') or ''
    return bool(TOKEN)


def make_pattern():
    """造一张"蓝白花卉散点"的测试图，让 AI 有话可说。"""
    from vision_probe import make_sample             # noqa
    p = os.path.join(HERE, 'samples', '_e2e_lib.png')
    os.makedirs(os.path.dirname(p), exist_ok=True)
    make_sample(p)
    with open(p, 'rb') as f:
        return p, base64.b64encode(f.read()).decode()


def main():
    print('会话：%s' % SID)
    print('C 端：%s   后台：%s\n' % (BASE, ADMIN))
    if not admin_login():
        return finish()

    health = req('/api/health')
    LLM = bool(health.get('llm_enabled'))
    if not LLM:
        print('⚠ 对话模型没配 Key（llm_enabled=false）：机器人退到规则模式，')
        print('  下面依赖模型的两节会跳过 —— 那是环境问题，不是代码坏了。\n')

    print('[1] 单张上传入库（真 AI 打标签，走后台）')
    path, b64 = make_pattern()
    t0 = time.time()
    r = admin_req('/api/library/ingest',
                  {'image': b64, 'image_mime': 'image/png',
                   'filename': os.path.basename(path), 'source': 'upload'})
    dt = time.time() - t0
    if r.get('dup'):
        # 判重是按**内容**（md5）走的，不是文件名 —— 所以直接拿服务端回的那条记录继续验。
        # 测试图是同一张，第二次跑必然走到这里。
        item = r.get('item') or {}
        print('  （这张测试图之前已入库，用库里那条继续验）')
        print('    ⚠ 因此下面那些标签断言看的是**上次**打好的结果，')
        print('      不是这一轮真跑的 AI 打标签。要验真打标签，看 [6] 或在库里换张新图。')
        ok(bool(item), '重复入库被判重，没产生第二份')
        ok(item.get('md5') == __import__('hashlib').md5(base64.b64decode(b64)).hexdigest(),
           '判重依据是内容指纹，不是文件名')
    else:
        item = r.get('item') or {}
    ok(bool(item), '入库返回了记录', r)
    if not item:
        return finish()
    print('     %s / %s  %.1fs' % (item.get('name'), item.get('id'), dt))
    print('     标签：%s / %s / %s / %s'
          % (item.get('style'), item.get('element'), item.get('color'), item.get('scene')))
    print('     描述：%s' % item.get('desc'))
    print('     关键词：%s · 主色：%s'
          % ('、'.join(item.get('keywords') or []), '、'.join(item.get('colors') or [])))
    ok(item.get('status') == 'ready', '状态是"已入库"')
    ok(bool(item.get('name')) and not str(item['name']).startswith('未命名'),
       'AI 给起了名字')
    ok(bool(item.get('style') or item.get('element')), '至少读出标签')
    ok(bool(item.get('keywords')), '给了检索关键词')
    ok(bool(item.get('desc')), '写了一句话描述')
    ok(item.get('element') in ('花卉', '叶片藤蔓', '几何', '波点'), '元素归到了词表内',
       item.get('element'))

    pid = item['id']

    print('\n[2] 单张取图：缩略图 / 原图都能出')
    for thumb in (True, False):
        url = '%s/api/library/file/%s%s' % (BASE, pid, '?thumb=1' if thumb else '')
        with urllib.request.urlopen(url, timeout=30) as f:
            blob = f.read()
        ok(len(blob) > 500 and f.headers.get('Content-Type', '').startswith('image/'),
           '%s 返回真图片（%d 字节，%s）'
           % ('缩略图' if thumb else '原图', len(blob),
              f.headers.get('Content-Type')))

    print('\n[3] 让花型师按客户要求检索——要能出库里的真图')
    if not LLM:
        skip('没配对话模型 Key，机器人走规则模式、选不出检索条件')
    else:
        d = req('/api/chat', {'agent': 'designer', 'session_id': SID,
                              'message': '客户要蓝白底的花卉，做丝巾，库里有对得上的吗'})
        ok(d.get('ok'), '这一轮答上来了')
        items = d.get('picks_items') or []
        libs = [x for x in items if x.get('lib')]
        print('     命中 %d 条，其中库里的真图 %d 条' % (len(items), len(libs)))
        for x in items[:5]:
            print('       - %s %s %s' % (x.get('id'), x.get('name'),
                                         ('真图 ' + str(x.get('image'))) if x.get('lib') else '内置款'))
        ok(bool(items), '给了候选')
        if libs:
            ok(all(str(x.get('image') or '').startswith('/api/library/') for x in libs),
               '库里的条目都带图片地址')
            ok('search_patterns' in (d.get('skills') or []), '真的调了检索技能')
        else:
            ok(False, '这一轮没命中库里的图（库里应该有刚入库的那张）')

    print('\n[4] 库图当底图：拿着它去改')
    r2 = req('/api/action', {'agent': 'designer', 'session_id': SID,
                             'action': 'pick_library', 'id': pid})
    ok(r2.get('ok'), '设底图成功', r2)
    ok(r2.get('origin') == 'library', '底图来源标成 library')
    ok(bool(r2.get('name')), '回传了花型名字')
    # 再设一次 → 热度 +1，检索排序会用到
    req('/api/action', {'agent': 'designer', 'session_id': SID,
                        'action': 'pick_library', 'id': pid})
    one = req('/api/library?q=' + urllib.parse.quote(item['name']))
    got = (one.get('items') or [{}])[0]
    ok(got.get('id') == pid, '按名字能搜回这张图')
    ok(int(got.get('in_use') or 0) >= 2, '选用次数记下来了（%s）' % got.get('in_use'))

    print('\n[5] 改标签：人工把风格改掉，检索立刻跟着变')
    new_style = '现代极简' if item.get('style') != '现代极简' else '童趣插画'
    u = admin_req('/api/library/update',
                  {'id': pid, 'fields': {'style': new_style,
                                         'keywords': ['e2e改过', '对花']}})
    ok(u.get('ok'), '保存成功')
    ok(u['item']['style'] == new_style, '风格已更新')
    ok(u['item']['tag_mode'] == 'manual', '标记成人工改过')
    lst = req('/api/library?style=' + urllib.parse.quote(new_style))
    ok(any(x['id'] == pid for x in (lst.get('items') or [])),
       '严格筛选按新风格能筛到它')
    old = req('/api/library?style=' + urllib.parse.quote(item.get('style') or ''))
    ok(not any(x['id'] == pid for x in (old.get('items') or [])),
       '旧风格里已经搜不到（真的换了）')

    print('\n[6] 重打标签：AI 再读一遍')
    if not LLM:
        skip('没配对话模型 Key，打标签这条路走不通')
    else:
        rt = admin_req('/api/library/retag', {'id': pid}, timeout=300)
        ok(rt.get('ok'), '重打成功', rt.get('error'))
        if rt.get('ok'):
            print('     重读后：%s / %s / %s'
                  % (rt['item'].get('style'), rt['item'].get('element'), rt['item'].get('color')))
            ok(rt['item'].get('status') == 'ready', '状态就绪')
            ok(rt['item'].get('tag_mode') == 'ai', '标记回 AI 打的')

    print('\n[7] 库的统计与筛选面')
    hp = req('/api/health')
    lib_stat = hp.get('library') or {}
    ok(int(lib_stat.get('count') or 0) > 0, '健康检查报出了库内张数：%s' % lib_stat.get('count'))
    lst = req('/api/library?limit=5')
    ok(lst.get('ok') and lst.get('stats', {}).get('count') is not None, '列表接口带统计')
    ok(bool(lst.get('facets', {}).get('style')), '筛选面有风格档位（前端照着渲染按钮）')
    ok(len(lst.get('items') or []) <= 5, 'limit 生效')

    return finish()


def finish():
    print('\n%s' % ('—' * 58))
    print('通过 %d · 失败 %d' % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
