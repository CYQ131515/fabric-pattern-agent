#!/usr/bin/env python3
"""PSD 分层：网络出口必须**强制直连**（不走代理）。

跑法：python3 测试/test_psd_net.py

为什么要有这条测试
    用户报过：PSD 分层一跑就网络错误，关掉 VPN/代理才好。根因是
    `urllib` 会自动读 HTTP_PROXY / HTTPS_PROXY（以及 macOS 系统设置），
    而 RunningHub 和视觉网关走这台机器上的本地代理一律失败 ——
    报出来的样子还特别像"key 不对"，能白查半天。

    所以出口策略是**无条件直连**，两条线同时保障：
        · 算法侧 rhpsd/net.py —— ProxyHandler({}) 关掉一切代理
        · 后端 core/psdlayer.py —— 起子进程时摘掉代理环境变量
    这个用例把两条线都钉住，防止哪天被顺手改回裸 urlopen。

    另外跑一次**端到端对照实验**：植入死代理后，算法子进程仍要能连上
    RunningHub（对照组：老写法必须失败 —— 否则说明这个测试没测到东西）。
"""

import os
import sys

# 2026-10-20：描稿/四方连续/分层 PSD 已换成新算法（算法/描稿-四方连续-psd分层），
# 这条用例针对的是旧引擎（RunningHub SAM3.1、纯 Python 子进程、代理摘除、
# 逐层进度与逐层超时），这些内部实现已不存在。保留原文作历史参考；
# 需要对照旧行为时设置 PATTERNFLOW_LEGACY_TESTS=1 再跑。
if os.environ.get('PATTERNFLOW_LEGACY_TESTS') != '1':
    print('（旧引擎回归已归档，跳过。设置 PATTERNFLOW_LEGACY_TESTS=1 可强制运行）')
    sys.exit(0)

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
ROOT = os.path.dirname(BACKEND)
ALGO = os.path.join(ROOT, '算法', 'psd分层')

sys.path.insert(0, BACKEND)

from core.psdlayer import PROXY_ENV_KEYS, PsdLayerEngine   # noqa: E402

PASS = FAIL = 0
PROXY_KEYS = list(PROXY_ENV_KEYS)
DEAD = 'http://127.0.0.1:1/'          # 必然连不通：1 号端口不会有人监听


def ok(cond, label, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('  ✓ ' + label)
    else:
        FAIL += 1
        print('  ✗ ' + label + ('  → %s' % (extra,) if extra else ''))


class StubCfg:
    """只提供 psdlayer 需要的三样，免得依赖真实密钥。"""
    settings = {}
    secrets = {'runninghub_api_key': 'stub-key'}
    api_key = 'stub-vlm-key'


def engine():
    return PsdLayerEngine(StubCfg())


def case_env():
    """子进程环境里不能有代理变量，该带的要带。"""
    print('\n[1] 子进程环境：摘代理、留必需项')
    e = engine()
    env = e._subprocess_env()
    left = {k: env[k] for k in PROXY_KEYS if env.get(k)}
    ok(not left, '六个代理变量（含小写）全部摘掉', left)
    ok(env.get('RUNNINGHUB_API_KEY') == 'stub-key', 'RUNNINGHUB_API_KEY 照常注入')
    ok(env.get('PYTHONIOENCODING') == 'utf-8', 'PYTHONIOENCODING=utf-8')
    ok(e.root in (env.get('PYTHONPATH') or ''), 'PYTHONPATH 含算法目录',
       env.get('PYTHONPATH'))

    # 只改副本：本进程的环境不能被牵连（主程序别的网络行为要照旧）
    before = {k: os.environ.get(k) for k in PROXY_KEYS}
    e._subprocess_env()
    after = {k: os.environ.get(k) for k in PROXY_KEYS}
    ok(before == after, '不修改本进程环境（只摘副本）', '%s → %s' % (before, after))

    # 父进程环境里有代理时，也必须摘干净
    saved = {k: os.environ.get(k) for k in PROXY_KEYS}
    try:
        for k in PROXY_KEYS:
            os.environ[k] = DEAD
        env2 = engine()._subprocess_env()
        left2 = {k: env2[k] for k in PROXY_KEYS if env2.get(k)}
        ok(not left2, '父进程带着代理时，子进程环境仍然干净', left2)
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def case_algo_source():
    """算法侧不能再出现裸 urlopen（唯一的出口是 net.urlopen）。

    用 AST 判定，不做字符串匹配：这几个文件开头的文档字符串里就写着
    "别改回裸 urllib.request.urlopen"，按键搜会把说明文字当成违规
    （第一版就是这么误报的）。AST 只看真实调用，天然绕开注释与字符串。
    """
    print('\n[2] 算法侧源码：不许有裸 urlopen')
    import ast
    import io

    def dotted(node):
        """把属性访问还原成点号形式：urllib.request / net / ''。"""
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            base = dotted(node.value)
            return (base + '.' + node.attr) if base else node.attr
        return ''

    hits = []
    for name in ('client.py', 'analyze.py', 'pipeline.py', 'service.py',
                 'fill.py', 'psd.py'):
        p = os.path.join(ALGO, 'rhpsd', name)
        if not os.path.exists(p):
            continue
        tree = ast.parse(io.open(p, encoding='utf-8').read(), filename=name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr == 'urlopen':
                # net.urlopen 是**允许的**（本算法唯一的出口）——
                # 注意它的 attr 也叫 urlopen，只按属性名判定会误伤
                if dotted(f.value) == 'net':
                    continue
                hits.append('%s:%d  %s.urlopen' % (name, node.lineno,
                                                   dotted(f.value) or '?'))
            elif isinstance(f, ast.Name) and f.id == 'urlopen':
                hits.append('%s:%d  裸 urlopen（应改成 net.urlopen）'
                            % (name, node.lineno))
    ok(not hits, 'rhpsd/*.py 里没有裸 urlopen 调用', hits)
    ok(os.path.exists(os.path.join(ALGO, 'rhpsd', 'net.py')),
       'net.py 存在（唯一出口）')


def case_algo_behavior():
    """真起一次算法子进程：跑它自己的出口自检。"""
    print('\n[3] 算法子进程：出口不带代理 + 真能连上 RunningHub')
    e = engine()
    if not e.installed:
        print('  （算法环境没装好，跳过这一节：%s）' % (e.python or '无 python'))
        return
    probe = (
        'import json, os\n'
        'from rhpsd import net\n'
        'd = net.describe()\n'
        'ok, detail = net._probe("https://www.runninghub.cn", 20)\n'
        'print(json.dumps({"has_proxy": d["outlet_has_proxy_handler"],\n'
        '                  "env_proxies": d["env_proxies"],\n'
        '                  "probe_ok": ok, "detail": detail}))\n')

    def run(label, extra_env=None):
        env = e._subprocess_env()
        env.update(extra_env or {})
        p = subprocess.run([e.python, '-c', probe], cwd=ALGO, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out = (p.stdout or b'').decode('utf-8', 'ignore').strip()
        if not out:
            return None, (p.stderr or b'').decode('utf-8', 'ignore')[-200:]
        return json.loads(out), ''

    saved = {k: os.environ.get(k) for k in PROXY_KEYS}
    try:
        r, err = run('正常')
        if r is None:
            ok(False, '子进程能跑起来', err)
            return
        ok(r['has_proxy'] is False, '出口未挂代理处理器', r['has_proxy'])
        ok(not r['env_proxies'], '子进程里没有代理变量', r['env_proxies'])
        ok(r['probe_ok'] is True, '能连上 RunningHub（正常环境）', r['detail'])

        # 植入死代理后重跑一遍
        for k in PROXY_KEYS:
            os.environ[k] = DEAD
        r2, err2 = run('死代理')
        if r2 is None:
            ok(False, '死代理下子进程能跑起来', err2)
            return
        ok(r2['probe_ok'] is True,
           '能连上 RunningHub（父进程带死代理时）', r2['detail'])

        # 对照：老写法必须失败，否则这条测试等于没测
        old = subprocess.run(
            [e.python, '-c',
             'import urllib.request\n'
             'try:\n'
             '    r = urllib.request.urlopen("https://www.runninghub.cn", timeout=15)\n'
             '    print("OLD_OK")\n'
             'except Exception as exc:\n'
             '    print("OLD_FAIL", type(exc).__name__)\n'],
            cwd=ALGO, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        o = (old.stdout or b'').decode().strip()
        ok(o.startswith('OLD_FAIL'),
           '对照：老写法在死代理下确实失败（说明测试有效）', o)
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def case_describe():
    """体检里要能一眼看到"机器上开着代理"这件事。"""
    print('\n[4] 体检字段')
    d = engine().describe()
    ok(d.get('direct_net') is True, 'describe().direct_net = True')
    ok(isinstance(d.get('proxy_env_seen'), list),
       'describe().proxy_env_seen 是列表（列父进程捡到的代理变量）',
       d.get('proxy_env_seen'))
    ok(bool(json.dumps(d, ensure_ascii=False)), 'describe() 可 JSON 序列化')


def main():
    print('PSD 分层网络出口用例（强制直连，不走代理）')
    case_env()
    case_algo_source()
    case_algo_behavior()
    case_describe()
    print('\n结果：%d 通过 / %d 失败' % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
