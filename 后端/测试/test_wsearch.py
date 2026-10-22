#!/usr/bin/env python3
"""全网搜花型（算法/全网搜花型）—— 接进全量回归的薄壳。

跑法：python3 后端/测试/test_wsearch.py

为什么不把断言抄一份到这里
    真正的断言在 算法/全网搜花型/tools/ 下的三个文件里：
        test_tile.py       平铺判定：42 条（**合成已知答案**，不联网）
        test_pipeline.py   整条产线：134 条（桩掉网络，不联网、不需要 key）
        test_screen.py     语义甄别与补齐：126 条（视觉端点也是桩的）
        test_pick.py       路线 2（不凑数）：25 条
        test_similar.py    相似去重（同图变体只留一张）：18 条（PIL 现画图）
    它们要算法自己的 .venv（有 Pillow）。抄一份到这儿等于养两份会漂移的
    断言 —— 项目里已经吃过一次亏（生成动作词表曾经两处各一份）。所以这里
    **只做三件事**：用对的解释器跑那五个文件、把结果并进本测试的计数、
    再加上一条这个位置才该管的断言 —— **别污染真花型库**。

不需要联网，不需要任何 API key，**也不需要本地视觉模型在跑**。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
ROOT = os.path.dirname(BACKEND)
ALGO = os.path.join(ROOT, '算法', '全网搜花型')
TOOLS = os.path.join(ALGO, 'tools')
REAL_LIB = os.path.join(ROOT, '数据', 'library')

# 算法自己的 venv（有 Pillow）；没有就退回当前解释器并如实说明
PY = os.path.join(ALGO, '.venv', 'bin', 'python3')
if not os.path.exists(PY):
    PY = sys.executable

PASS = FAIL = 0


def _snapshot(path):
    """目录快照：文件名 + 大小 + mtime。用来证明"一个字都没写进去"。"""
    got = {}
    if not os.path.isdir(path):
        return got
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            p = os.path.join(dirpath, f)
            try:
                st = os.stat(p)
                got[os.path.relpath(p, path)] = (st.st_size, int(st.st_mtime))
            except OSError:
                pass
    return got


def run_script(name):
    """跑算法里的一个测试文件，把它的通过/失败数并进来。"""
    global PASS, FAIL
    path = os.path.join(TOOLS, name)
    if not os.path.exists(path):
        print('  \u2717 %s 不存在（%s）' % (name, path))
        FAIL += 1
        return
    try:
        p = subprocess.run([PY, path], cwd=ALGO, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=600)
    except subprocess.TimeoutExpired:
        print('  \u2717 %s 跑超时（超过 10 分钟）' % name)
        FAIL += 1
        return
    out = (p.stdout or b'').decode('utf-8', 'replace')
    # 算法侧自己会打"结果：N 通过 / M 失败"，按**退出码**判成败（不猜关键词）
    tail = [ln for ln in out.strip().splitlines() if '通过' in ln]
    summary = tail[-1].strip() if tail else out.strip().splitlines()[-1:] or ['']
    summary = summary if isinstance(summary, str) else summary[0]
    counts = _counts(out)
    if counts:
        PASS += counts[0]
        FAIL += counts[1]
    if p.returncode == 0:
        print('  \u2713 %s :: %s' % (name, summary))
    else:
        print('  \u2717 %s (rc=%d) :: %s' % (name, p.returncode, summary))
        for ln in out.strip().splitlines():
            if '\u2717' in ln:
                print('      %s' % ln.strip())
        FAIL += 1


def _counts(out):
    for ln in reversed(out.strip().splitlines()):
        if '结果：' in ln and '通过' in ln and '失败' in ln:
            try:
                a = int(ln.split('：')[1].split('通过')[0].strip())
                b = int(ln.split('通过')[1].split('/')[1].split('失败')[0].strip())
                return a, b
            except Exception:                                # noqa: BLE001
                return None
    return None


def case_import():
    """算法包要能被导进来，而且**不能**在导入时就连网或者要 key。

    这条必须在**算法自己的解释器**里跑（有 Pillow 的那个）—— 后端解释器是
    刻意零依赖的，它 import 不了 wsearch.tile，那是设计如此，不是缺陷。

    断言里的"一个 key 都不配时可用的是谁"：**百度图片 + 360 图片 + 花瓣 +
    堆糖**（免 key 的公开接口，开箱即用；必应免 key 但默认不跑，所以它不在
    这个集合里）。要 key 的那几家必须仍然是空的 —— 不能因为加了免 key 来源，
    就把"没配 key 也装作能用"混进来。

    Patternbank 特殊：**它在不在可用列表里，取决于这台机器装没装 Scrapling**
    （可选依赖）。所以这里不能写死集合，要钉的是这条对应关系 ——
    `('patternbank' in av) == scrape.have()`。
    """
    global PASS, FAIL
    code = (
        "import os,sys\n"
        "for k in [k for k in os.environ if k.startswith('WSEARCH_')]:\n"
        "    os.environ.pop(k, None)\n"
        "import wsearch\n"
        "from wsearch import (providers, tile, analyze, fetch, rank, screen,\n"
        "                     store, service)\n"
        "av = providers.available()\n"
        "assert {'baidu', 'so360', 'huaban', 'duitang'} <= set(av), av\n"
        "assert 'bing' not in av, av\n"      # 免 key，但限流时会回不相关的图 → 默认不跑
        "assert 'bing' in providers.on_demand()\n"
        "keyed = [n for n in av if not providers.REGISTRY[n].get('keyless')]\n"
        "assert not keyed, keyed\n"
        # Patternbank 是可选依赖（Scrapling）：装了才在列表里，没装就不在
        "from wsearch import scrape\n"
        "assert ('patternbank' in av) == scrape.have(), (av, scrape.have())\n"
        "assert providers.key_of('baidu') == '' and providers.enabled('baidu')\n"
        # 甄别模块的三条底线：能导入、解析容错、探不到模型时**不抛异常**
        "assert screen.DEFAULT_BASE.startswith('http://127.0.0.1')\n"
        "v = screen.parse('[{\"i\":1,\"pattern\":true,\"match\":true,\"why\":\"x\"}]', 1)\n"
        "assert v[0]['match'] is True, v\n"
        "assert not screen.accepts({'screen': {'pattern': False, 'match': True}})\n"
        "assert screen.accepts({'screen': None})\n"
        "os.environ['WSEARCH_VLM_BASE'] = 'http://127.0.0.1:9/v1'\n"   # 9 号端口没人听
        "assert screen.VlmClient(timeout=2).available() is False\n"
        "print('IMPORT_OK')\n"
    )
    try:
        p = subprocess.run([PY, '-c', code], cwd=ALGO, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=120)
    except subprocess.TimeoutExpired:
        FAIL += 1
        print('  \u2717 算法包导入超时（导入时不该做重活）')
        return
    out = (p.stdout or b'').decode('utf-8', 'replace')
    if p.returncode == 0 and 'IMPORT_OK' in out:
        PASS += 3
        print('  \u2713 算法包能导入，导入时不连网、不要求任何 key')
        print('  \u2713 没配 key 时只有免 key 的百度/360 可用（要 key 的不假装能用）')
        print('  \u2713 甄别模块：解析容错、拒绝语义正确、探不到模型时不炸')
    else:
        FAIL += 1
        print('  \u2717 算法包导入检查失败（rc=%s）' % p.returncode)
        for ln in out.strip().splitlines()[-6:]:
            print('      %s' % ln)


def case_no_pollution(before):
    """全流程不许往真花型库写一个字（这是硬要求）。"""
    global PASS, FAIL
    after = _snapshot(REAL_LIB)
    if before == after:
        PASS += 1
        print('  \u2713 真花型库文件数/大小/mtime 一个字没变')
    else:
        FAIL += 1
        add = set(after) - set(before)
        chg = {k for k in set(after) & set(before) if after[k] != before[k]}
        print('  \u2717 真花型库被动过了！新增 %d 个、改动 %d 个' % (len(add), len(chg)))


def main():
    print('全网搜花型 · 回归（解释器：%s）' % PY)
    before = _snapshot(REAL_LIB)
    case_import()
    run_script('test_tile.py')
    run_script('test_screen.py')
    run_script('test_pipeline.py')
    run_script('test_pick.py')          # 路线 2（不凑数）
    run_script('test_similar.py')       # 相似去重
    case_no_pollution(before)
    print('\n结果：%d 通过 / %d 失败' % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
