#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""描稿仿稿端到端联调：真服务 + 真模型 + 真生图。

前置：先把服务跑起来（python3 server.py）。
用法：
    python3 tools/e2e_trace.py                  # 用默认底图
    python3 tools/e2e_trace.py <照片路径>
    python3 tools/e2e_trace.py <照片> --no-gen  # 只走到提案，不出图（省钱）

流程：上传照片给织印师 → 看是否识别出描稿意图 → 确认 → 出图。
"""
import base64
import json
import mimetypes
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.dirname(HERE)
sys.path.insert(0, AGENT)
BASE = os.environ.get('AGENT_BASE', 'http://127.0.0.1:8787')
# 出图目录跟着配置走 —— 重构后它不在「后端/」下了，写死相对路径会指空
from core.config import Config as _Config                            # noqa: E402
DEFAULT_IMG = os.path.normpath(os.path.join(
    AGENT, _Config().settings['image'].get('outdir') or 'generated',
    '20260928-122211-probe-380.jpg'))


def post(path, payload, timeout=300):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    no_gen = '--no-gen' in sys.argv
    img = args[0] if args else DEFAULT_IMG
    if not os.path.exists(img):
        print('找不到图片：%s' % img)
        return 1

    mime = mimetypes.guess_type(img)[0] or 'image/jpeg'
    with open(img, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode()
    print('底图：%s（%s，%.0f KB）' % (os.path.basename(img), mime, os.path.getsize(img) / 1024))
    sid = 'e2e-trace-%d' % int(time.time())

    print('\n[1] 以织印师身份上传照片，说「把图案描下来」')
    t0 = time.time()
    r = post('/api/chat', {
        'agent': 'engineer', 'session_id': sid, 'brief': {},
        'message': '帮我把这上面的图案描下来做成花型', 'image': b64, 'image_mime': mime})
    print('  耗时 %.1fs' % (time.time() - t0))
    print('  回复：%s' % (r.get('reply') or '').replace('\n', ' ')[:300])
    print('  调用技能：%s' % r.get('skills'))
    if r.get('image_tags'):
        t = (r['image_tags'].get('tags') or {})
        print('  识图标签：%s' % json.dumps(t, ensure_ascii=False))
    pend = r.get('pending')
    print('  待确认动作：%s' % (json.dumps(pend, ensure_ascii=False) if pend else '（无）'))

    if not pend:
        print('\n结论：模型没有提出描稿方案。检查 engineer 的 skills 是否含 trace_pattern，'
              '或这句意图是否被识别。')
        return 1
    if pend.get('action') != 'trace_pattern':
        print('\n结论：识别成了别的动作（%s），不是描稿。' % pend.get('action'))
        return 1

    qc = pend.get('qc') or {}
    print('  照片体检：level=%s usable=%s' % (qc.get('level'), qc.get('usable')))
    for i in (qc.get('issues') or []):
        print('    - %s' % i)

    fid = None
    for a in sys.argv[1:]:
        if a.startswith('--fid='):
            fid = a.split('=', 1)[1].strip()
    if fid:
        print('\n[1b] 在卡片上把还原度切到 %s（只重提方案，不扣点、不出图）' % fid)
        # 跟前端一致：带上 agent。三个机器人共用一个会话，
        # 服务端只认「这个机器人自己那份」待确认方案。
        sw = post('/api/action', {'session_id': sid, 'agent': 'engineer',
                                  'action': 'set_fidelity', 'fidelity': fid})
        if not sw.get('ok'):
            print('  切换失败：%s' % sw.get('error'))
            return 1
        np = sw.get('pending') or {}
        print('  新卡片：fidelity=%s' % np.get('fidelity'))
        print('  摘要：%s' % np.get('summary'))
        if np.get('fidelity') != fid:
            print('\n结论：切换没生效。')
            return 1
        print('  体检结论保留：%s（切档不该把体检弄丢）'
              % (np.get('qc') or {}).get('level'))

    if no_gen:
        print('\n（--no-gen：到此为止，不出图不扣点）')
        return 0

    print('\n[2] 用户点确认 → 服务端确定性执行')
    t1 = time.time()
    res = post('/api/action', {'session_id': sid, 'agent': 'engineer',
                               'action': 'confirm', 'args': {}})
    print('  耗时 %.1fs' % (time.time() - t1))
    if not res.get('ok'):
        print('  失败：%s' % res.get('error'))
        return 1
    for f in (res.get('images') or []):
        print('  出图：%s  %s  %.0f KB' % (f.get('name'), f.get('mime'),
                                          (f.get('bytes') or 0) / 1024))
    print('  模型：%s  还原度：%s  点数：%s'
          % (res.get('model'), res.get('fidelity'), res.get('cost')))
    print('  提示词：%s' % (res.get('prompt') or '')[:220])
    print('\n结论：描稿仿稿链路跑通。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
