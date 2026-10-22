#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨角色串台回归（服务端，不连网、不花钱）。

三个机器人共用同一个会话。出过的 bug：
织印师提了「描稿方案」还没确认，用户切到花型师点「改风格」，
服务端却把花型师的请求执行成了织印师的描稿 —— 因为 run_action 只要发现
会话里存在待确认方案，就无条件拿它的 action 覆盖用户当下的请求；
而那份方案又是整个会话共享的，不分角色。

这里盯的就是这条边界：
    [1] 待确认方案按角色分开存，互不覆盖
    [2] 花型师点「改图」就执行改图，不会被织印师的描稿顶掉
    [3] 「确认」只消费本角色自己的方案，动不到别人
    [4] 切回原角色点确认，才真正执行它自己的方案
    [5] 改参数（切还原度）只动本角色，且绝不执行、不扣点
    [6] 「重新生成」不碰别的角色的方案，也不碰需求卡
    [7] 待确认方案带角色落盘，重启读回不串台
    [8] 调用方没带 agent 时（脚本 / 老客户端）怎么落到对的角色上

跑法：python3 tools/test_v06_xchar.py
生图走 mock，LLM 被替换成"未配置"，全程零网络。
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.dirname(HERE)
sys.path.insert(0, AGENT)

os.environ['PATTERNFLOW_IMAGE_PROVIDER'] = 'mock'
# 出图产物挪去临时目录，别堆进项目的 generated/
os.environ['PATTERNFLOW_IMAGE_OUTDIR'] = tempfile.mkdtemp(prefix='pf-test-img-')
# 图库目录也挪走：出图会自动入库，别把测试产物混进真图库
os.environ['PATTERNFLOW_LIBRARY_DIR'] = tempfile.mkdtemp(prefix='pf-test-lib-')
# 埋点也挪走：跑一次回归会记下几十条出图/提案，混进真看板就成了假数据
os.environ['PATTERNFLOW_TELEMETRY_DIR'] = tempfile.mkdtemp(prefix='pf-test-tel-')

from core.config import Config                        # noqa: E402
from core.persona import load_all as load_personas    # noqa: E402
from core.skills import SkillRegistry                 # noqa: E402
from core.session import SessionStore                 # noqa: E402
from core.runtime import AgentRuntime                 # noqa: E402

PASS, FAIL = 0, 0


def ok(cond, name, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('  OK   %s' % name)
    else:
        FAIL += 1
        print('  FAIL %s%s' % (name, (' → ' + str(extra)) if extra else ''))


def ok_eq(got, want, name):
    ok(got == want, name, '得到 %r，期望 %r' % (got, want))


class OffLLM:
    """把模型关掉，逼运行时走本地规则 —— 测试不连网、不花额度。"""
    enabled = False

    def chat(self, *a, **k):
        raise AssertionError('测试里不该真的调模型')


def build(tmpdir):
    cfg = Config()
    data = {}
    for k, fn in (('patterns', 'patterns.json'), ('trends', 'trends.json'),
                  ('vocab', 'vocab.json')):
        p = os.path.join(cfg.path('data'), fn)
        data[k] = (json.load(open(p, encoding='utf-8'))
                   if os.path.exists(p) else [])
    rt = AgentRuntime(cfg, load_personas(cfg.path('personas')),
                      SkillRegistry(cfg.path('skills')),
                      SessionStore(os.path.join(tmpdir, 'sessions')), data)
    rt.llm = OffLLM()
    return rt


def main():
    tmp = tempfile.TemporaryDirectory()
    rt = build(tmp.name)

    calls = []
    real_run = rt.registry.run

    def spy(name, args, ctx):
        """记下每次技能调用，并把出图类技能换成确定性结果（不真出图）。"""
        args = dict(args or {})
        calls.append({'name': name, 'args': args})
        if name in ('generate_pattern', 'edit_pattern', 'trace_pattern'):
            if not args.get('confirmed'):
                return {'need_confirm': True, 'summary': '方案', 'cost': 60,
                        'prompt': 'P', 'action': name,
                        'fidelity': args.get('fidelity') or 'clean',
                        'qc': {'level': 'good', 'usable': True, 'issues': []},
                        'count': 1}
            return {'ok': True, 'count': 1, 'cost': 60, 'prompt': 'P',
                    'provider': 'mock', 'model': 'mock',
                    'images': [{'url': '/api/file/fake.png', 'mime': 'image/png',
                                'bytes': 1000, 'name': 'fake.png'}]}
        return real_run(name, args, ctx)

    rt.registry.run = spy
    SID = 'xchar'
    sess = rt.sessions.get(SID)

    # ================================================== [1] 方案按角色分开存
    print('\n[1] 待确认方案按角色分开存，互不覆盖')
    rt._set_pending(sess, 'engineer',
                    {'action': 'trace_pattern', 'args': {'prompt': '配色改成雾霾蓝'},
                     'cost': 60, 'summary': '描稿方案'})
    rt._set_pending(sess, 'designer',
                    {'action': 'generate_pattern', 'args': {}, 'cost': 240,
                     'summary': '生成方案'})
    ok(rt._pending_of(sess, 'engineer') is not None, '织印师有自己的方案')
    ok(rt._pending_of(sess, 'designer') is not None, '花型师也有自己的方案')
    ok_eq((rt._pending_of(sess, 'engineer') or {}).get('action'), 'trace_pattern',
          '两份没有互相覆盖（织印师那份还在）')
    ok_eq((rt._pending_of(sess, 'designer') or {}).get('action'), 'generate_pattern',
          '花型师那份也没被顶掉')

    # ================================================== [2] 显式动作优先
    print('\n[2] 花型师点「改图」→ 必须执行改图，不能被织印师的描稿顶掉')
    calls.clear()
    r = rt.run_action(SID, 'edit_pattern', {'style': '波西米亚'}, 'designer')
    ok_eq(r.get('action'), 'edit_pattern', '执行的是 edit_pattern')
    ok_eq([c['name'] for c in calls], ['edit_pattern'], '只调了一次改图')
    ok_eq((calls[0]['args'] if calls else {}).get('style'), '波西米亚',
          '用户点的「波西米亚」如实传进去')
    ok('fidelity' not in (calls[0]['args'] if calls else {}),
       '没有把描稿的还原度参数捎带进来')
    ok(rt._pending_of(sess, 'engineer') is not None,
       '织印师的描稿方案**没有**被这次操作消费掉')
    ok(rt._pending_of(sess, 'designer') is None,
       '花型师自己那份被消费掉了')

    # ================================================== [3] 确认只认本角色
    print('\n[3] 花型师点「确认」不会去执行织印师的方案')
    calls.clear()
    r = rt.run_action(SID, 'confirm', {}, 'designer')
    ok(r.get('ok') is False, '花型师没有待确认方案 → 明确拒绝', r)
    ok(not calls, '没有执行任何技能', [c['name'] for c in calls])
    ok(rt._pending_of(sess, 'engineer') is not None, '织印师的方案原封不动')
    ok('待确认' in (r.get('error') or ''), '拒绝时说的是人话', r.get('error'))

    # ================================================== [4] 各自确认各自执行
    print('\n[4] 切回织印师点确认 → 这时才执行描稿')
    calls.clear()
    r = rt.run_action(SID, 'confirm', {}, 'engineer')
    ok_eq(r.get('action'), 'trace_pattern', '执行的是描稿')
    ok_eq((calls[0]['args'] if calls else {}).get('prompt'), '配色改成雾霾蓝',
          '用的是织印师那份方案里记的额外要求')
    ok((calls[0]['args'] if calls else {}).get('confirmed') is True, '带确认态')
    ok(rt._pending_of(sess, 'engineer') is None, '执行后织印师那份清掉')

    # ================================================== [5] 改参数不越界
    print('\n[5] 改额外要求只动本角色，且绝不执行、不扣点')
    rt._set_pending(sess, 'engineer',
                    {'action': 'trace_pattern', 'args': {}, 'cost': 60})
    calls.clear()
    r0 = rt.retune_pending(SID, {'prompt': '配色改成雾霾蓝'}, 'designer')
    ok(r0.get('ok') is False, '花型师没有方案 → 改参数被拒', r0)
    ok(not calls, '被拒时没有触发任何技能调用')

    r1 = rt.retune_pending(SID, {'prompt': '配色改成雾霾蓝'}, 'engineer')
    ok(r1.get('ok') is True, '织印师改额外要求成功')
    ok_eq(((rt._pending_of(sess, 'engineer') or {}).get('args') or {}).get('prompt'),
          '配色改成雾霾蓝', '方案里的额外要求换成了新值')
    ok_eq((calls[-1]['args'] if calls else {}).get('confirmed'), False,
          '改参数只是重跑提案（confirmed=False），绝不执行')
    ok_eq((rt._pending_of(sess, 'engineer') or {}).get('cost'), 60,
          '方案点数没变（没扣点）')

    # ================================================== [6] 重新生成不越界
    print('\n[6] 「重新生成」不碰别的角色的方案，也不碰需求卡')
    sess.brief = {'style': '法式浪漫', 'element': None, 'color': None, 'scene': None}
    sess.last_gen = {'name': 'generate_pattern',
                     'args': {'prompt_full': 'P', 'n': 4}}
    rt._set_pending(sess, 'engineer',
                    {'action': 'trace_pattern', 'args': {'fidelity': 'clean'},
                     'cost': 60})
    calls.clear()
    r = rt.run_action(SID, 'regenerate', {'spec': sess.last_gen}, 'designer')
    ok_eq(r.get('action'), 'generate_pattern', '重放的是生成')
    ok_eq((calls[0]['args'] if calls else {}).get('prompt_full'), 'P',
          '重放用的是当时存下的完整提示词')
    ok(rt._pending_of(sess, 'engineer') is not None,
       '织印师的方案没被重放动到')
    ok_eq(sess.brief.get('style'), '法式浪漫', '需求卡没被重放清掉')

    # ================================================== [7] 落盘 / 读回
    print('\n[7] 待确认方案带角色落盘，重启读回不串台')
    import glob
    path = os.path.join(tmp.name, 'sessions', SID + '.jsonl')
    ok(os.path.exists(path), '会话已落盘')
    recs = [json.loads(x) for x in open(path, encoding='utf-8') if x.strip()]
    pres = [x for x in recs if x.get('type') == 'pending']
    ok(pres and all('agent' in x for x in pres),
       '每条 pending 记录都带了 agent', pres[:2])
    ok(any(x.get('agent') == 'engineer' and x.get('pending') for x in pres),
       '织印师那份写进了盘')
    sess2 = SessionStore(os.path.join(tmp.name, 'sessions')).get(SID)
    ok_eq((sess2.pending_actions.get('engineer') or {}).get('action'),
          'trace_pattern', '重启读回：织印师的方案回来了')
    ok('designer' not in sess2.pending_actions,
       '读回后花型师那份是空的（之前已执行掉）')

    # ================================================== [8] 不带角色的调用
    print('\n[8] 调用方没带 agent：只有一份方案就认它，真有歧义才退回花型师')
    # 脚本 / 老客户端 / 手工 curl 都不带 agent。一律算成花型师的话，
    # 织印师的描稿方案就永远消费不掉（e2e_trace 挂在这里好一阵子）。
    rt._set_pending(sess, 'designer', None)
    rt._set_pending(sess, 'engineer', {'action': 'trace_pattern',
                                       'args': {'fidelity': 'clean'}, 'cost': 60})
    # 先认角色，再执行 —— 执行完方案就被消费掉了，那时再问是没有意义的
    ok_eq(rt._infer_agent(sess, None), 'engineer', '只有一份方案 → 认的就是它')
    ok_eq(rt._infer_agent(sess, 'nobody'), 'engineer',
          '带了个不存在的角色，跟没带是一回事')
    calls.clear()
    r = rt.run_action(SID, 'confirm', {})          # ← 不带 agent
    ok_eq(r.get('action'), 'trace_pattern', '唯一那份是描稿 → 就执行描稿')
    ok(rt._pending_of(sess, 'engineer') is None, '执行后织印师那份清掉')

    # 两份同时在 → 真有歧义，不猜，退回默认的花型师（跟旧行为一致）
    rt._set_pending(sess, 'designer', {'action': 'generate_pattern',
                                       'args': {}, 'cost': 240})
    rt._set_pending(sess, 'engineer', {'action': 'trace_pattern',
                                       'args': {}, 'cost': 60})
    ok_eq(rt._infer_agent(sess, None), 'designer', '两份都在 → 有歧义，不猜')
    calls.clear()
    r = rt.run_action(SID, 'confirm', {})
    ok_eq(r.get('action'), 'generate_pattern', '两份都在 → 按默认的花型师那份')
    ok(rt._pending_of(sess, 'engineer') is not None, '织印师那份没被误消费掉')

    print('\n结果：%d 通过 / %d 失败' % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
