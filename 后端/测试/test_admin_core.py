#!/usr/bin/env python3
"""后台写入逻辑的离线单测：**密钥不能被静默清空**。
跑法：python3 tools/test_admin_core.py

这条是血泪教训：有一次 e2e 调 /api/admin/config 时传了空密钥，
服务端把真 Key 写成了空串 —— 服务当场降级到规则模式，还查不出原因。
所以「空值 = 不改动」这件事要有两层保障，而且两层都要有测试：

    第一层 admin.save_config：空串/占位符一律跳过，不写进 payload
    第二层 config.save_secrets：新值为空而旧值非空时保留旧值（除非 force）

没有第二层，第一层哪天被改坏就又出事。
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import core.config as CC                      # noqa: E402
from core import admin as adminapi            # noqa: E402

PASS = FAIL = 0


def ok(cond, label, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('  ✓ ' + label)
    else:
        FAIL += 1
        print('  ✗ ' + label + ('  → %s' % (extra,) if extra else ''))


def sandbox():
    """造一个临时项目根，把 config.ROOT 指过去，测完还原。"""
    tmp = tempfile.mkdtemp(prefix='pf-admin-test-')
    os.makedirs(os.path.join(tmp, 'config'), exist_ok=True)
    return tmp


def read_secrets(root):
    with open(os.path.join(root, 'config', 'secrets.json'), encoding='utf-8') as f:
        return json.load(f)


def write_secrets(root, data):
    with open(os.path.join(root, 'config', 'secrets.json'), 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


class FakeApp:
    """够 save_config 用的最小 App：有 cfg、有 reload。"""
    def __init__(self, cfg):
        self.cfg = cfg
        self.reloaded = 0
    def reload(self):
        self.reloaded += 1
        return self


def main():
    old_root = CC.ROOT
    tmp = sandbox()
    try:
        CC.ROOT = tmp
        write_secrets(tmp, {'api_key': 'sk-REALKEY123', 'image_api_key': 'ark-REALKEY456'})

        print('[1] 第二层兜底：save_secrets 不许把非空写成空')
        cfg = CC.Config()
        ok(cfg.api_key == 'sk-REALKEY123', '读到了真密钥')
        cfg.save_secrets({'api_key': '', 'image_api_key': ''})
        got = read_secrets(tmp)
        ok(got['api_key'] == 'sk-REALKEY123', '空值被忽略，原密钥还在', got['api_key'])
        ok(got['image_api_key'] == 'ark-REALKEY456', '生图密钥也还在')
        cfg.save_secrets({'api_key': 'sk-NEWKEY999'})
        got = read_secrets(tmp)
        ok(got['api_key'] == 'sk-NEWKEY999', '真传了新值时正常写入', got['api_key'])
        ok(got['image_api_key'] == 'ark-REALKEY456', '没传的那个保持原样')
        cfg.save_secrets({'api_key': ''}, force=True)
        got = read_secrets(tmp)
        ok(got['api_key'] == '', '显式 force=True 才能清空', got['api_key'])

        print('\n[2] 第一层：admin.save_config 传空串 = 不改')
        write_secrets(tmp, {'api_key': 'sk-REALKEY123', 'image_api_key': 'ark-REALKEY456'})
        cfg2 = CC.Config()
        app = FakeApp(cfg2)
        r = adminapi.save_config(app, {'llm': {'model': 'deepseek-chat'},
                                       'api_key': '', 'image_api_key': ''})
        ok(r.get('ok') is True, '保存成功', r)
        ok(r.get('changed_keys') == [], 'changed_keys 为空', r.get('changed_keys'))
        got = read_secrets(tmp)
        ok(got['api_key'] == 'sk-REALKEY123', '密钥没被清掉', got['api_key'])
        ok(app.reloaded == 1, '保存后触发了重载（否则页面上改了实际没生效）')

        print('\n[3] 占位符也当作没改动')
        write_secrets(tmp, {'api_key': 'sk-REALKEY123', 'image_api_key': 'ark-REALKEY456'})
        cfg3 = CC.Config()
        app3 = FakeApp(cfg3)
        r = adminapi.save_config(app3, {'api_key': adminapi.KEEP,
                                        'image_api_key': adminapi.MASK + 'abcd'})
        ok(r.get('changed_keys') == [], 'KEEP 与掩码都不算改动', r.get('changed_keys'))
        ok(read_secrets(tmp)['api_key'] == 'sk-REALKEY123', '原密钥还在')

        print('\n[4] 真要改的时候能改')
        cfg4 = CC.Config()
        app4 = FakeApp(cfg4)
        r = adminapi.save_config(app4, {'api_key': 'sk-CHANGED-777'})
        ok('api_key' in r.get('changed_keys', []), '新值被记进 changed_keys', r.get('changed_keys'))
        ok(read_secrets(tmp)['api_key'] == 'sk-CHANGED-777', '新密钥写进去了')

        print('\n[5] 知识库角色白名单：不存在的角色不会被写进去')
        cfg5 = CC.Config()
        app5 = FakeApp(cfg5)
        adminapi.save_config(app5, {'kb': {'agents': ['assistant', '不存在的角色']}})
        ok(cfg5.settings['kb']['agents'] == ['assistant'],
           '非法角色被过滤掉', cfg5.settings['kb']['agents'])

        print('\n[6] 密钥脱敏：回给前端的不能是真值')
        ok(adminapi.mask_key('sk-abcdefgh1234') == '••••••1234',
           '只留最后 4 位', adminapi.mask_key('sk-abcdefgh1234'))
        ok('abcdefgh' not in adminapi.mask_key('sk-abcdefgh1234'), '中段不回显')
        ok(adminapi.mask_key('') == '', '空密钥不强凑掩码')
        ok('短' not in adminapi.mask_key('短'), '极短值也不露原文', adminapi.mask_key('短'))

        print('\n[7] 在模型页点保存，不能把知识库 / 花型库顺手关掉')
        # 真出过：知识库和花型库的开关不在「模型与密钥」页上，
        # 前端在那一页保存时收集不到控件，于是当成 false/[] 一起提交，
        # 运营只是改个模型名，知识库就被静默关掉了（机器人不再按资料回答）。
        cfg8 = CC.Config()
        app8 = FakeApp(cfg8)
        kb_before = dict(cfg8.settings['kb'])
        lib_before = dict(cfg8.settings['library'])

        adminapi.save_config(app8, {'llm': {'model': 'deepseek-chat'},
                                    'kb': {'score_min': '0.18'}})
        ok(cfg8.settings['kb']['enabled'] == kb_before['enabled'],
           'payload 没提 enabled，就不动它', cfg8.settings['kb']['enabled'])
        ok(cfg8.settings['kb']['agents'] == kb_before['agents'],
           '没提 agents 也不动', cfg8.settings['kb']['agents'])

        adminapi.save_config(app8, {'kb': {'enabled': None, 'agents': None}})
        ok(cfg8.settings['kb']['enabled'] == kb_before['enabled'], 'null 不当成 false')
        ok(cfg8.settings['kb']['agents'] == kb_before['agents'], 'null 不当成空数组')

        r = adminapi.save_config(app8, {'kb': {'agents': []}})
        ok(cfg8.settings['kb']['agents'] == kb_before['agents'],
           '真给空数组也不覆盖（否则页面永远回显不出角色，修不回来）',
           cfg8.settings['kb']['agents'])
        ok(bool(r.get('notes')), '并且明确告诉运营这一项没保存', r.get('notes'))

        adminapi.save_config(app8, {'library': {'enabled': None, 'auto_ingest': None}})
        ok(cfg8.settings['library']['auto_ingest'] == lib_before['auto_ingest'],
           '花型库的自动入库也不会被 null 关掉')

        adminapi.save_config(app8, {'kb': {'enabled': False}, 'library': {'enabled': False}})
        ok(cfg8.settings['kb']['enabled'] is False, '明确传 false 才真的关掉知识库')
        ok(cfg8.settings['library']['enabled'] is False, '花型库同理')

        print('\n[8] 扫目录：系统目录一律拒绝')
        for bad in ('/System', '/usr', '/', '/etc'):
            path, err = adminapi._safe_dir(bad)
            ok(path is None, '拒绝扫 %s' % bad, err)
        path, err = adminapi._safe_dir(tmp)
        ok(path == os.path.abspath(tmp), '普通目录放行', err)
        path, err = adminapi._safe_dir('/不存在的目录')
        ok(path is None, '不存在的目录给出明确错误', err)

        print('\n[9] 按机器人分组后新增的字段：能改、留空不改、超范围夹紧')
        # 「模型与密钥」页改成按角色分组（通用 / 花型设计师 / 织印师）之后，
        # 后台能改的东西多了一批：PSD 的 RunningHub 与视觉 Key、点数与层数、
        # 生图张数、识图模型。它们必须跟老字段守同一套规矩 ——
        # 否则运营在页面上改不动，或者更糟：顺手一保存把 Key 清掉。
        write_secrets(tmp, {'api_key': 'sk-REALKEY123',
                            'runninghub_api_key': 'rh-OLD999'})
        cfg9 = CC.Config()
        app9 = FakeApp(cfg9)
        adminapi.save_config(app9, {
            'runninghub_api_key': '', 'psdlayer_vlm_key': '',
            'psdlayer': {'cost': '', 'max_layers': '', 'vlm_base': '', 'vlm_model': ''},
            'image': {'variants': ''},
            'vision': {'model': ''}})
        ok(read_secrets(tmp)['runninghub_api_key'] == 'rh-OLD999',
           '留空不会把 RunningHub 那把 Key 清掉',
           read_secrets(tmp).get('runninghub_api_key'))
        ok(not cfg9.settings['psdlayer'].get('vlm_base'),
           '留空的视觉网关不写进配置（= 沿用通用那套）',
           cfg9.settings['psdlayer'].get('vlm_base'))
        ok(cfg9.settings.get('vision', {}).get('model') == '',
           '识图模型留空是合法值（跟对话模型同一个），允许写空')

        adminapi.save_config(app9, {
            'runninghub_api_key': 'rh-NEW111', 'psdlayer_vlm_key': 'sk-VLM222',
            'psdlayer': {'cost': '50', 'max_layers': '99',
                         'vlm_base': 'https://vlm.example/v1', 'vlm_model': 'vlm-1'},
            'image': {'variants': '99'},
            'vision': {'model': 'qwen-vl'}})
        got = read_secrets(tmp)
        ok(got['runninghub_api_key'] == 'rh-NEW111', 'RunningHub Key 改得动')
        ok(got['psdlayer_vlm_key'] == 'sk-VLM222', '视觉诊断 Key 改得动')
        psd = cfg9.settings['psdlayer']
        ok(psd.get('cost') == 50, 'PSD 单张点数改得动', psd.get('cost'))
        ok(psd.get('max_layers') == 8, '层数填 99 会被夹到 8（多层 = 多花钱多等待）',
           psd.get('max_layers'))
        ok(psd.get('vlm_base') == 'https://vlm.example/v1', '视觉网关改得动')
        ok(cfg9.settings['image'].get('variants') == 8,
           '出图张数填 99 会被夹到 8', cfg9.settings['image'].get('variants'))
        ok(cfg9.settings['vision'].get('model') == 'qwen-vl', '识图模型改得动')

        adminapi.save_config(app9, {'clear_keys': ['psdlayer_vlm_key']})
        ok(read_secrets(tmp)['psdlayer_vlm_key'] == '', '显式 clear_keys 才清空')
        ok(read_secrets(tmp)['runninghub_api_key'] == 'rh-NEW111',
           '清一个不影响另一个')

        print('\n[10] 描稿仿稿：默认沿用生图，也能单独配（且退得回共用）')
        # 描稿仿稿从"完全复用生图"改成"可单独配、也可沿用生图"。
        # 这里的坑跟别处**相反**：别的字段是"留空 = 不改"，
        # 描稿是"留空 = 继承生图"，空值必须真的写进去 ——
        # 否则运营填过一次模型，就再也退不回"跟花型设计师共用"的状态。
        from core.imagegen import ImageGen
        write_secrets(tmp, {'api_key': 'sk-REALKEY123',
                            'image_api_key': 'ark-IMGKEY',
                            'trace_api_key': ''})
        cfgA = CC.Config()
        appA = FakeApp(cfgA)
        base = ImageGen(cfgA)
        t0 = ImageGen(cfgA, section='trace')
        ok(t0.model == base.model and t0.base_url == base.base_url,
           '默认：描稿模型/网关完全沿用生图', '%s / %s' % (t0.model, t0.base_url))
        ok(t0.api_key == base.api_key, '默认：描稿沿用生图那把 Key')

        adminapi.save_config(appA, {'trace': {'provider': 'openai',
                                              'base_url': 'https://trace.example/v1',
                                              'model': 'trace-model-x',
                                              'cost': '35'}})
        t1 = ImageGen(CC.Config(), section='trace')
        ok(t1.provider == 'openai' and t1.model == 'trace-model-x',
           '单独填了就覆盖（服务商/模型）', '%s / %s' % (t1.provider, t1.model))
        ok(t1.base_url == 'https://trace.example/v1', '描稿可以单独走另一家网关', t1.base_url)
        ok(t1.cost == 35, '描稿点数可以单独定', t1.cost)

        # 只改一项时，其余项仍继承生图 —— 不能把生图那套一起抹了
        adminapi.save_config(appA, {'trace': {'provider': '', 'base_url': '',
                                              'model': 'trace-only-model',
                                              'cost': ''}})
        cfgC = CC.Config()
        t2, imgC = ImageGen(cfgC, section='trace'), ImageGen(cfgC)
        ok(t2.model == 'trace-only-model', '只填模型时模型生效')
        ok(t2.base_url == imgC.base_url and t2.provider == imgC.provider,
           '没填的网关/服务商回落生图', '%s / %s' % (t2.provider, t2.base_url))
        ok(t2.cost == imgC.cost, '点数留空 = 继承生图点数', t2.cost)

        # 退得回共用：全部留空 = 完全继承生图
        adminapi.save_config(appA, {'trace': {'provider': '', 'base_url': '',
                                              'model': '', 'cost': ''}})
        cfgD = CC.Config()
        t3, imgD = ImageGen(cfgD, section='trace'), ImageGen(cfgD)
        ok(t3.model == imgD.model and t3.base_url == imgD.base_url
           and t3.cost == imgD.cost,
           '全部留空 = 完全退回沿用生图', '%s / %s / %s' % (t3.model, t3.base_url, t3.cost))

        # 描稿 Key：可单独配、留空不改、可显式清空（清空后回落生图）
        adminapi.save_config(appA, {'trace_api_key': 'sk-TRACE999'})
        ok(read_secrets(tmp)['trace_api_key'] == 'sk-TRACE999', '描稿 Key 能单独配')
        adminapi.save_config(appA, {'trace_api_key': ''})
        ok(read_secrets(tmp)['trace_api_key'] == 'sk-TRACE999',
           '描稿 Key 留空不改（跟其它 Key 同一套规矩）')
        adminapi.save_config(appA, {'clear_keys': ['trace_api_key']})
        ok(read_secrets(tmp)['trace_api_key'] == '', '显式 clear_keys 才清空描稿 Key')
        cfgE = CC.Config()
        ok(ImageGen(cfgE, section='trace').api_key == ImageGen(cfgE).api_key,
           '描稿 Key 清空后又回落到生图 Key')

        print('\n[11] 客服助理的技能清单：接上真检索，不给专业活，也不管趋势')
        # 2026-10-13：用户发现客服只有 get_pattern_detail（客户得先报编号），
        # 描述需求它只能干说"转给设计师"，于是配上 search_patterns。
        # 2026-10-14：用户要求客服对话里**不要趋势和花型风格** —— 趋势是花型设计师
        # 挑风格时的工具，摆在客服这边只会让客户以为"客服也能给我定方向"，
        # 所以 list_trends 从客服摘掉了。出图/描稿/连续/PSD 本来就不给。
        cfg11 = CC.Config()
        sk = cfg11.agent_conf('assistant').get('skills') or []
        ok('search_patterns' in sk, '默认配置里客服能查花型库', sk)
        ok('get_pattern_detail' in sk, '详情查询还在', sk)
        ok('list_trends' not in sk, '客服不再管趋势（要方向找花型设计师）', sk)
        ok(not ({'generate_pattern', 'edit_pattern', 'trace_pattern',
                 'seamless_pattern', 'layer_psd'} & set(sk)),
           '客服不许出图/跑算法（专业活转交，别在客服页报点数）', sk)
        ok('assistant' in (cfg11.settings['kb'].get('agents') or []),
           '知识库仍只开给客服（问答 + 检索组合拳不断线）',
           cfg11.settings['kb'].get('agents'))
        from core.skills import SkillRegistry                      # noqa: E402
        reg = SkillRegistry(os.path.join(os.path.dirname(HERE), 'skills'))
        ok(all(n in reg.skills for n in sk),
           '配的技能名都真实存在（拼错一个线上就静默少个能力）',
           [n for n in sk if n not in reg.skills])
        # export_list 是**服务端自己调**的：模型看不见才不会自己编参数。
        # 一旦它出现在任何角色的清单里，就说明有人把"确定性导出"改成了"让模型发挥"。
        ok('export_list' in reg.skills, 'export_list 技能挂上了（服务端导出用）')
        for who in ('assistant', 'designer', 'engineer', 'auto'):
            s = cfg11.agent_conf(who).get('skills') or []
            ok('export_list' not in s, '%s 的技能清单里没有 export_list（服务端调）' % who, s)
        # 默认配置改了，线上 settings.json 也得同步 —— 改一处忘一处是老毛病
        live = json.load(open(os.path.join(os.path.dirname(HERE), 'config',
                                           'settings.json'), encoding='utf-8'))
        live_sk = ((live.get('agents') or {}).get('assistant') or {}).get('skills') or []
        ok(live_sk == sk, '线上 settings.json 跟默认配置一致（防漂移）', live_sk)
    finally:
        CC.ROOT = old_root
        shutil.rmtree(tmp, ignore_errors=True)

    print('\n结果：%d 通过 / %d 失败' % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
