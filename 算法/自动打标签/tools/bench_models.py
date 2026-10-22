#!/usr/bin/env python3
"""同批图跑多个模型，对比谁更合适。

用途：模型推荐是调研结论，但**你机器上的实际表现**才作数。
拿十几张你自己的图跑一遍，比看任何评测表都直接。

    python tools/bench_models.py ./inputs --models qwen3.5:2b qwen3.5:4b qwen3.5:9b

它会打印：
    · 每个模型的平均耗时、四维填充率、平均标签数、失败数
    · 两个模型在同一张图上给出的标签**重合程度**（看小模型丢了多少）

注意：会真的跑模型，比较慢。建议先 --limit 8 跑个小样本。
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atag.batch import DEFAULT_MAX_SIDE, collect, tag_one            # noqa: E402
from atag.client import Client, VisionError                          # noqa: E402
from atag.image import ImageError                                     # noqa: E402
from atag.prompt import build_prompt                                  # noqa: E402
from atag.taxonomy import DIMS, DIM_CN, load_vocab                    # noqa: E402


def run_one_model(name, files, vocab, base_url, api_key, max_side, timeout):
    c = Client(base_url=base_url, model=name, api_key=api_key,
               timeout=timeout)
    prompt = build_prompt(vocab)
    rows, ms, fails = {}, [], 0
    for f in files:
        t0 = time.time()
        rec = tag_one(c, prompt, f, vocab, max_side)
        dt = time.time() - t0
        if rec.get('status') == 'failed':
            fails += 1
            continue
        ms.append(rec.get('elapsed_ms') or int(dt * 1000))
        rows[os.path.basename(f)] = rec
    avg = sum(ms) / len(ms) if ms else 0
    return rows, avg, fails


def fill_rate(rows):
    """四维填充率：每张图平均有几维给出了标签（0~4）。"""
    if not rows:
        return 0.0
    tot = 0
    for r in rows.values():
        tot += sum(1 for d in DIMS if r.get(d))
    return tot / float(len(rows))


def label_count(rows):
    if not rows:
        return 0.0
    tot = 0
    for r in rows.values():
        tot += sum(len(r.get(d) or []) for d in DIMS)
    return tot / float(len(rows))


def jaccard(a, b, dim):
    """两个模型在同一张图、同一维上的标签重合度。"""
    sa = set(a.get(dim) or [])
    sb = set(b.get(dim) or [])
    if not sa and not sb:
        return None            # 两边都空，算"都没说"，不参与平均
    return len(sa & sb) / float(len(sa | sb))


def main(argv=None):
    p = argparse.ArgumentParser(prog='bench_models')
    p.add_argument('paths', nargs='+', help='图文件夹或图片')
    p.add_argument('--models', '-m', nargs='+', required=True,
                   help='要比的模型名，比如 qwen3.5:2b qwen3.5:4b qwen3.5:9b')
    p.add_argument('--base-url', default=None)
    p.add_argument('--api-key', default=None)
    p.add_argument('--vocab', default=None)
    p.add_argument('--max-side', type=int, default=DEFAULT_MAX_SIDE)
    p.add_argument('--timeout', type=float, default=300)
    p.add_argument('--limit', type=int, default=8, help='每种模型跑几张（默认 8）')
    a = p.parse_args(argv)

    vocab = load_vocab(a.vocab)
    files = collect(a.paths, limit=a.limit)
    if not files:
        print('这些路径里没找到图片。')
        return 1
    print('样本 %d 张，跑 %d 个模型\n' % (len(files), len(a.models)))

    out = {}
    for name in a.models:
        c = Client(base_url=a.base_url, model=name, api_key=a.api_key)
        try:
            if not c.has_model():
                print('%-18s 服务里没有这个模型，跳过' % name)
                continue
        except VisionError as e:
            print('连不上服务：%s' % e)
            return 1
        print('%s 跑起来了…' % name)
        rows, avg, fails = run_one_model(name, files, vocab, a.base_url,
                                        a.api_key, a.max_side, a.timeout)
        out[name] = rows
        print('  %-18s 平均 %5.0f ms   四维填充 %.2f/4   平均标签 %.2f 条   '
              '失败 %d' % (name, avg, fill_rate(rows), label_count(rows), fails))

    names = list(out.keys())
    if len(names) >= 2:
        print('\n标签重合度（同一张图、同一维度，两个模型给出的标签有多一致）')
        print('  1.00 = 完全一样，0 = 一个都没重合')
        print('  %-18s %s' % ('', '  '.join('%-14s' % n for n in names[1:])))
        for i, n1 in enumerate(names):
            cells = []
            for n2 in names:
                if n2 == n1 or names.index(n2) < i:
                    cells.append('%-14s' % '-')
                    continue
                shared = [f for f in out[n1] if f in out[n2]]
                vals = []
                for d in DIMS:
                    v = [jaccard(out[n1][f], out[n2][f], d) for f in shared]
                    v = [x for x in v if x is not None]
                    if v:
                        vals.append(sum(v) / len(v))
                cells.append('%-14s' % ('%.2f' % (sum(vals) / len(vals))
                                        if vals else 'n/a'))
            print('  %-18s %s' % (n1, '  '.join(cells)))

        print('\n分维度看（数字越高说明两个模型越一致）')
        print('  %-10s %s' % ('维度', '  '.join('%-14s' % n for n in names)))
        for d in DIMS:
            cells = []
            for n in names:
                tot, cnt = 0.0, 0
                for f in out[names[0]]:
                    if f not in out[n]:
                        continue
                    v = jaccard(out[names[0]][f], out[n][f], d)
                    if v is not None:
                        tot += v
                        cnt += 1
                cells.append('%-14s' % ('%.2f' % (tot / cnt) if cnt else 'n/a'))
            print('  %-10s %s' % (DIM_CN[d], '  '.join(cells)))

    print('\n一点读表建议：')
    print('  · 先看四维填充率。明显低于别的模型，说明它想不出标签、开始空着 ——')
    print('    这正是小模型最先垮掉的地方。')
    print('  · 再看和最大模型的重合度。有 0.7 以上，日常用就够；')
    print('    掉到 0.5 以下，说明它给的标签已经和你预期不太一样了。')
    print('  · 最后才看耗时。快 3 倍但标签差一档，不值。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
