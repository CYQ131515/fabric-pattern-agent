#!/usr/bin/env python3
"""自动打标签 · 命令行（给人用的）。

    python run.py probe                     看看环境齐不齐
    python run.py recommend                 该选哪个模型（含体积/速度对照）
    python run.py tag ./inputs              给一个文件夹里所有图打标
    python run.py tag a.jpg b.png           给指定几张图打标
    python run.py one a.jpg                 只打一张，结果直接打屏
    python run.py summary                   看已经打好的统计

第一次用建议顺序：probe → recommend → 按提示拉模型 → tag

打标结果默认存在 ./out，结构：
    out/images/   原图副本
    out/tags/     一张图一条 JSON
    out/index.json  汇总索引
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from atag import __version__                                   # noqa: E402
from atag.batch import DEFAULT_MAX_SIDE, Progress, run as run_batch, tag_one  # noqa: E402
from atag.client import Client, VisionError, DEFAULT_MODEL # noqa: E402
from atag.prompt import PROMPT_VERSION, build_prompt           # noqa: E402
from atag.store import Store                                   # noqa: E402
from atag.taxonomy import DIMS, DIM_CN, describe, load_vocab   # noqa: E402

# 模型推荐表。这份表是调研结论的直接落地：**先看体积，再看够不够用**。
# 关键依据是视觉类基准上 4B 和 9B 差距很小（MMMU 77.6 vs 78.4），
# 所以对"打标签"这个任务，4B 是性价比拐点，9B 主要赢在复杂推理。
# 量化档位不是随便选的：社区实测同一家族里，4B 的最佳点是 Q5，
# 9B 的最佳点是 Q4 —— 小模型用 Q4 掉得比大模型明显多。
RECOMMEND = [
    ('Qwen3.5-4B-MLX-4bit',   '3.0 GB', '**推荐**', '日常主力。四维细粒度够用，'
                                                 'M4 Pro 上一张几秒，Apache 2.0 可商用'),
    ('Qwen3.5-2B-MLX-4bit',   '1.6 GB', '省资源',   '快近一倍，细粒度标签会掉一档，'
                                                 '适合先把流程跑通'),
    ('Qwen3.5-0.8B-MLX-4bit', '0.6 GB', '极限小',   '只够判断"是不是花型"和粗分类，'
                                                 '四维标签别指望它'),
    ('Qwen3.5-9B-MLX-4bit',   '约 5 GB', '更高精度', '复杂画面更稳，但视觉任务上提升有限，'
                                                 '速度约慢一半'),
]

NOT_RECOMMENDED = [
    ('qwen3-vl:8b', '上一代（Qwen3-VL）。同尺寸不如 Qwen3.5，没必要'),
    ('fashion-clip', '只能做固定词表的匹配，中文弱，也说不出词表外的标签；'
                     '当批量预筛可以，当主力不行'),
]


def _say(*a):
    sys.stderr.write(' '.join(str(x) for x in a) + '\n')


def _p(outdir):
    return os.path.abspath(outdir)


def _client(a):
    return Client(base_url=a.base_url, model=a.model, api_key=a.api_key,
                  timeout=a.timeout, max_tokens=a.max_tokens)


def _vocab(a):
    return load_vocab(a.vocab)


# ------------------------------------------------------------------ probe
def cmd_probe(a):
    c = _client(a)
    vocab = _vocab(a)
    print('自动打标签 %s' % __version__)
    print('  词表      %s' % describe(vocab))
    print('  提示词版本 %s' % PROMPT_VERSION)
    print('  模型端点   %s' % c.base_url)
    print('  模型       %s' % c.model)
    try:
        models = c.list_models()
    except VisionError as e:
        print('\n环境还差一步：%s' % e)
        print('\n怎么办：起一个本地推理服务。本项目自带了一个，最省事：')
        print('    双击  本地模型/安装.command        # 装引擎（第一次几分钟）')
        print('    双击  本地模型/启动本地模型.command  # 下模型（约 3 GB）并起服务')
        print('装好之后这个窗口别关，再跑一次 python run.py probe')
        print('\n如果你已经装了 Ollama，也可以直接用：')
        print('    ollama serve && ollama pull qwen3.5:4b')
        print('    python run.py probe --base-url http://127.0.0.1:11434/v1')
        return 1
    print('  服务上共有  %d 个模型' % len(models))
    if c.has_model():
        print('\n就绪，可以直接跑了：python run.py tag ./inputs')
        return 0
    print('\n服务通了，但里面没有 %s。' % c.model)
    near = [m for m in models if 'qwen' in m.lower() or 'vl' in m.lower()][:8]
    if near:
        print('现有的视觉模型大概是：%s' % '、'.join(near))
        print('也可以直接用它：python run.py tag ./inputs --model %s' % near[0])
    print('自带引擎默认带的模型：%s（见 本地模型/）' % DEFAULT_MODEL)
    return 1


# -------------------------------------------------------------- recommend
def cmd_recommend(a):
    print('该选哪个模型')
    print()
    print('结论：如果只挑一个 —— **Qwen3.5-4B**。MLX 4bit 量化后 3.0 GB，')
    print('      四维细粒度够用，本机 M4 Pro 上一张图几秒，Apache 2.0 可商用。')
    print()
    print('为什么不是别的：')
    print('  · 4B 和 9B 在**视觉**基准上几乎持平（MMMU 77.6 vs 78.4，')
    print('    OCRBench 85.0 vs 89.2）。9B 那点提升主要在数学/代码这类推理上，')
    print('    而打标签不吃这个 —— 所以没必要为了它多花一倍内存和等待时间。')
    print('  · 再往下到 2B / 0.8B，会开始丢细节：一条细枝叶、一个小波点看没了，')
    print('    标签就从"叶片藤蔓"掉到"抽象笔触"，配色也会整片跑偏。')
    print('  · 往上到 27B 这类，本机 24 GB 内存跑得动，但一张图要等十几秒，')
    print('    批量打标的时间成本比那点精度更贵。')
    print()
    print('候选对照（体积是量化后的下载大小，不是运行内存）：')
    print('  %-24s %-9s %-9s %s' % ('模型', '体积', '定位', '说明'))
    for name, size, tag, why in RECOMMEND:
        print('  %-24s %-9s %-9s %s' % (name, size, tag, why))
    print()
    print('上面的名字是 HuggingFace 上的 MLX 版（自带引擎用的就是这套）。')
    print('如果你走 Ollama，对应的名字是 qwen3.5:4b / :2b / :0.8b / :9b，')
    print('体积会大一些（Ollama 默认 GGUF Q4_K_M）。')
    print()
    print('不建议当主力的：')
    for name, why in NOT_RECOMMENDED:
        print('  %-24s %s' % (name, why))
    print()
    print('量化档位别乱选（这条是给 GGUF / Ollama 那套的）：同一家族里，')
    print('4B 的最佳点是 Q5，9B 的最佳点是 Q4 —— 小模型用 Q4 掉得比大模型明显')
    print('多，跟"越压越小越好"的直觉相反。MLX 这套直接用 4bit 版本就行。')
    print()
    print('本机适配（Apple M4 Pro / 24 GB）：')
    print('  · 4B 随便跑，还能同时开 3 路并发')
    print('  · 9B 也能跑，但建议并发降到 1~2')
    print('  · MLX 走统一内存，4B 常驻大约占 3~4 GB，不用担心挤爆')
    print('  · 磁盘要留出模型体积，打标结果本身不占地方（一条记录 1 KB 左右）')
    return 0


# -------------------------------------------------------------------- tag
def cmd_tag(a):
    store = Store(_p(a.outdir))
    c = _client(a)
    vocab = _vocab(a)

    try:
        if not c.available:
            _say('连不上 %s。先跑 python run.py probe 看看差什么。' % c.base_url)
            return 1
    except VisionError as e:
        _say(str(e))
        _say('先跑 python run.py probe 看看差什么。')
        return 1

    if not c.has_model():
        _say('提示：服务里没有 %s，试试 python run.py probe 看现有模型。'
             % c.model)

    _say('模型 %s   并发 %d   最长边 %d' % (c.model, a.workers, a.max_side))
    _say('结果写到 %s' % store.root)
    _say('')

    prog = Progress('', total=0)
    last = [0]

    def _on(rec, n):
        # 每张打一行，看得见进度。用 . - ? x 表示四种结果
        mark = {'ok': '.', 'not_pattern': '-', 'empty': '?', 'failed': 'x'} \
            .get(rec.get('status'), '?')
        if rec.get('status') == 'ok':
            tags = []
            for d in DIMS:
                v = rec.get(d) or []
                if v:
                    tags.append('%s:%s' % (DIM_CN[d], '/'.join(v)))
            _say('  %s %-28s %s' % (mark, os.path.basename(rec['source']),
                                    '  '.join(tags)))
        elif rec.get('status') == 'not_pattern':
            _say('  %s %-28s 不是花型图' % (mark, os.path.basename(rec['source'])))
        elif rec.get('status') == 'empty':
            _say('  %s %-28s 是花型但四维都没给出标签' % (
                mark, os.path.basename(rec['source'])))
        else:
            _say('  %s %-28s %s' % (mark, os.path.basename(rec['source']),
                                    rec.get('error') or '失败'))
        last[0] = n

    try:
        s = run_batch(a.paths, store, c, vocab,
                      max_side=a.max_side, workers=a.workers, force=a.force,
                      progress=prog, on_done=_on, limit=a.limit)
    except FileNotFoundError as e:
        _say(str(e))
        return 1

    _say('')
    dup = s.get('duplicate') or 0
    print('扫到 %d 张，跳过已打标 %d 张%s，用时 %.1f 秒' % (
        s['scanned'], s['skipped'],
        '，另有 %d 张内容和别的重复（只留一份）' % dup if dup else '',
        s['elapsed']))
    print('  打上标 %d   不是花型 %d   四维全空 %d   失败 %d' % (
        s['ok'], s['not_pattern'], s['empty'], s['failed']))
    print('都存到 %s 了（图片在 images/，标签在 tags/，索引是 index.json）'
          % store.root)
    if s['failed']:
        print('失败的可以先看看 python run.py summary 里的记录，'
              '重跑一遍会只处理没成功的那些。')
    return 0


# -------------------------------------------------------------------- one
def cmd_one(a):
    if not os.path.isfile(a.path):
        _say('找不到这个文件：%s' % a.path)
        return 1
    c = _client(a)
    vocab = _vocab(a)
    store = Store(_p(a.outdir))
    try:
        rec = tag_one(c, build_prompt(vocab, a.note or ''), a.path, vocab,
                      a.max_side)
    except VisionError as e:
        _say(str(e))
        return 1

    if rec.get('status') == 'failed':
        _say('这张没打上：%s' % rec.get('error'))
        return 1

    saved = store.save(rec, a.path)
    print('文件 %s  (%dx%d)' % (os.path.basename(a.path),
                              rec.get('w') or 0, rec.get('h') or 0))
    if saved['status'] == 'not_pattern':
        print('判断：不是花型图，没打标签。%s' % (saved.get('reason') or ''))
        return 0
    for d in DIMS:
        vals = saved.get(d) or []
        free = (saved.get('free') or {}).get(d) or []
        line = '、'.join(vals) if vals else '（没给）'
        if free:
            line += '   [词表外] ' + '、'.join(free)
        print('%-5s %s' % (DIM_CN[d], line))
    for k, label in (('colors', '主要用色'), ('keywords', '检索词'),
                     ('desc', '描述'), ('reason', '判断依据')):
        v = saved.get(k)
        if v:
            print('%-5s %s' % (label, '、'.join(v) if isinstance(v, list) else v))
    if saved.get('confidence') is not None:
        print('%-5s %s' % ('把握', saved['confidence']))
    if saved.get('truncated'):
        print('注意：模型输出被截断了，上面可能缺字段，建议重跑一次。')
    if saved.get('suspect'):
        print('注意：输出里出现了像指令的措辞 —— 可能图上有文字被模型当成了命令，'
              '这条建议人工看一眼。')
    print('已存到 %s' % store.root)
    return 0


# ---------------------------------------------------------------- summary
def cmd_summary(a):
    store = Store(_p(a.outdir))
    items = store.all()
    s = store.summary()
    if not items:
        print('这个目录里还没有打标结果：%s' % store.root)
        return 0
    print('共 %d 张：打上标 %d，不是花型 %d，四维全空 %d，失败 %d'
          % (s['total'], s['ok'], s['not_pattern'], s['empty'], s['failed']))
    print()
    for d in DIMS:
        c = {}
        for r in items:
            for v in r.get(d) or []:
                c[v] = c.get(v, 0) + 1
        top = sorted(c.items(), key=lambda kv: -kv[1])[:12]
        print('%s  (%d 种)' % (DIM_CN[d], len(c)))
        for v, n in top:
            bar = '#' * min(40, int(round(n * 40.0 / max(1, top[0][1]))))
            print('   %-14s %-40s %d' % (v, bar, n))
        print()
    free = []
    for r in items:
        for d in DIMS:
            for v in (r.get('free') or {}).get(d) or []:
                free.append((DIM_CN[d], v, r.get('id')))
    if free:
        print('词表外的说法（共 %d 个，这些是扩词表的原料）：' % len(free))
        for dim_cn, w, ident in free[:30]:
            print('   %-5s %-12s %s' % (dim_cn, w, ident))
        print()
    suspicious = [r for r in items if r.get('suspect')]
    if suspicious:
        print('有 %d 张的输出里出现了像指令的措辞，建议人工看一眼：' % len(suspicious))
        for r in suspicious[:10]:
            print('   %s' % r.get('id'))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        prog='run.py', description='花型图自动打标签（四维：风格/元素/配色/适用场景）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='第一次用：python run.py probe  →  python run.py recommend')
    p.add_argument('--version', action='version', version='atag ' + __version__)
    sub = p.add_subparsers(dest='cmd', required=True)

    def common(sp, with_out=False):
        sp.add_argument('--model', default=None,
                        help='模型名（默认 qwen3.5:4b）')
        sp.add_argument('--base-url', default=None,
                        help='视觉模型端点（默认用自带引擎 11435；'
                             'Ollama 用 http://127.0.0.1:11434/v1）')
        sp.add_argument('--api-key', default=None, help='云端端点才需要')
        sp.add_argument('--timeout', type=float, default=None)
        sp.add_argument('--max-tokens', type=int, default=None)
        sp.add_argument('--vocab', default=None,
                        help='用主程序 data/vocab.json 的词表')
        sp.add_argument('--max-side', type=int, default=DEFAULT_MAX_SIDE,
                        help='传给模型的图最长边（默认 %d）' % DEFAULT_MAX_SIDE)
        if with_out:
            sp.add_argument('--outdir', '-o', default='out',
                            help='结果存哪（默认 ./out）')

    q = sub.add_parser('probe', help='检查环境')
    common(q)
    q.set_defaults(func=cmd_probe)

    rc = sub.add_parser('recommend', help='该选哪个模型')
    rc.set_defaults(func=cmd_recommend)

    t = sub.add_parser('tag', help='批量打标（文件夹会递归收图）')
    common(t, with_out=True)
    t.add_argument('paths', nargs='+', help='文件夹或图片文件')
    t.add_argument('--workers', type=int, default=3, help='并发数（默认 3）')
    t.add_argument('--force', action='store_true', help='已打过的也重打')
    t.add_argument('--limit', type=int, default=0, help='只跑前 N 张')
    t.set_defaults(func=cmd_tag)

    o = sub.add_parser('one', help='只打一张，结果直接打屏')
    common(o, with_out=True)
    o.add_argument('path', help='图片文件')
    o.add_argument('--note', default='', help='追加给模型的话，比如"这批是家纺图"')
    o.set_defaults(func=cmd_one)

    sm = sub.add_parser('summary', help='看已打好的统计')
    common(sm, with_out=True)
    sm.set_defaults(func=cmd_summary)

    a = p.parse_args(argv)
    try:
        return a.func(a)
    except KeyboardInterrupt:
        _say('\n停下了。已经打完的都在库里，重跑会接着来。')
        return 130


if __name__ == '__main__':
    sys.exit(main())
