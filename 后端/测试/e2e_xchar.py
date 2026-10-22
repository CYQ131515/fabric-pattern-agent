#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真链路复现：织印师的描稿方案不会被花型师的请求消费掉。

必须连着本地服务跑：
    python3 server.py --port 8787 &
    python3 tools/e2e_xchar.py

除了织印师那一轮（识图 + 模型提案）之外不产生费用：
花型师那次「改图」故意不带任何改动，技能会直接拒绝执行，不会真出图。
"""
import base64
import glob
import json
import os
import sys
import time
import urllib.request

BASE = os.environ.get('AGENT_BASE', 'http://127.0.0.1:8787')
SID = os.environ.get('E2E_SID', 'e2e-xchar-%d' % int(time.time()))
HERE = os.path.dirname(os.path.abspath(__file__))

PASS, FAIL = 0, 0


def ok(cond, name, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('  OK   %s' % name)
    else:
        FAIL += 1
        print('  FAIL %s%s' % (name, (' → ' + str(extra)) if extra else ''))


def post(path, payload, timeout=180):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def find_photo():
    for pat in (os.path.join(HERE, 'samples', '*.png'),
                os.path.join(HERE, 'samples', '*.jpg')):
        got = sorted(glob.glob(pat))
        if got:
            return got[0]
    return None


def main():
    print('会话：%s' % SID)
    try:
        with urllib.request.urlopen(BASE + '/api/health', timeout=20) as f:
            health = json.loads(f.read().decode('utf-8'))
    except Exception as e:
        print('连不上服务（%s）。先在 后端/ 下跑 python3 server.py' % e)
        return 1
    if not health.get('llm_enabled'):
        # 提案是模型识意图之后才给的：没配 Key 就退到规则模式，出不了提案。
        # 跨角色隔离本身在 test_v06_xchar.mjs（离线）里覆盖得更全，所以这里跳过不算漏测。
        print('⚠ 对话模型没配 Key（llm_enabled=false），规则模式下出不了描稿提案。')
        print('  跨角色隔离的离线用例在 tools/test_v06_xchar.mjs，那边不依赖模型。')
        print('  这节跳过。')
        return 0

    photo = find_photo()
    if not photo:
        print('没有样例照片，跳过')
        return 0
    print('照片：%s\n' % os.path.basename(photo))
    b64 = base64.b64encode(open(photo, 'rb').read()).decode()
    mime = 'image/png' if photo.lower().endswith('.png') else 'image/jpeg'

    print('[1] 织印师上传照片 → 提描稿方案（真模型 + 真识图）')
    t0 = time.time()
    r = post('/api/chat', {'agent': 'engineer', 'session_id': SID,
                           'message': '把这张图片描一下',
                           'image': b64, 'image_mime': mime})
    print('       （%.1fs，技能 %s）' % (time.time() - t0, r.get('skills')))
    pend = r.get('pending') or {}
    ok(pend.get('action') == 'trace_pattern', '织印师拿到了描稿提案', pend.get('action'))
    if pend.get('action') != 'trace_pattern':
        print('\n没提案就无从验证串台，先停下')
        return 1

    print('\n[2] 切到花型师点「改风格」→ 必须走改图，不能被串成描稿')
    d = post('/api/action', {'session_id': SID, 'agent': 'designer',
                             'action': 'edit_pattern', 'args': {}})
    ok(d.get('action') == 'edit_pattern', '执行的是 edit_pattern', d.get('action'))
    ok(d.get('action') != 'trace_pattern', '没有被串成描稿')
    ok('fidelity' not in d, '响应里不会冒出描稿的 fidelity 字段', sorted(d.keys()))

    print('\n[3] 花型师不该动得了织印师的方案')
    d3 = post('/api/action', {'session_id': SID, 'agent': 'designer',
                              'action': 'set_fidelity', 'fidelity': 'vector'})
    ok(d3.get('ok') is False, '花型师改参数被拒（他名下没有方案）', d3)

    print('\n[4] 织印师的方案还在，且只有他自己能改')
    d4 = post('/api/action', {'session_id': SID, 'agent': 'engineer',
                              'action': 'set_fidelity', 'fidelity': 'vector'})
    ok(d4.get('ok') is True, '织印师改参数成功', d4.get('error'))
    np = d4.get('pending') or {}
    ok(np.get('fidelity') == 'vector', '方案里的还原度换成了 vector', np.get('fidelity'))
    ok(np.get('cost') == 60, '点数没变（改参数不扣点）', np.get('cost'))

    print('\n[5] 收尾：取消方案（不扣点）')
    d5 = post('/api/action', {'session_id': SID, 'agent': 'engineer',
                              'action': 'cancel'})
    ok(d5.get('ok') is True, '取消成功', d5)

    print('\n结果：%d 通过 / %d 失败' % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
