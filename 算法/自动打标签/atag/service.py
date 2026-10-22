#!/usr/bin/env python3
"""自动打标签 · 对外服务入口（给主程序调用，不是给人用的）。

契约（主程序只依赖这几条，改动必须向后兼容）
    命令
        python -m atag.service probe  --json-out <结果.json>
                      自检：模型端点通不通、要用的模型装没装

        python -m atag.service one    --input <图> --outdir <目录>
                                      --json-out <结果.json>
                      单张打标（用户在对话里传一张图时用）

        python -m atag.service run    --input <路径> [--input <路径> ...]
                                      --outdir <目录> --json-out <结果.json>
                      批量打标。--input 可以是文件夹（会递归收图）或单个文件

        python -m atag.service summary --outdir <目录> --json-out <结果.json>
                      读一遍已打好的结果，给统计

    通用参数
        --model / --base-url / --api-key    覆盖模型端点
        --vocab <path>      用主程序的 data/vocab.json 覆盖内置词表
        --max-side N        传给模型的图最长边（默认 1280）
        --workers N         并发数（默认 3；本地模型别开太大）
        --force             已经打过标的也重打（默认跳过）
        --limit N           只跑前 N 张（试跑用）
        --progress-out <p>  进度原子写到这里，主程序边跑边读；
                            形状 {phase, total, done, ok, failed, current, elapsed, ts}
        --redact            自检结果里隐去端点地址（页面展示场景用）

    环境（由主程序注入，不落盘、不进配置文件）
        ATAG_BASE_URL   视觉模型端点，默认 http://127.0.0.1:11435/v1（自带本地引擎）
        ATAG_MODEL      模型名，默认 mlx-community/Qwen3.5-4B-MLX-4bit
        ATAG_API_KEY    云端端点才需要；本地引擎不用
        ATAG_TIMEOUT    单张超时秒数，默认 180

    输出
        --json-out 里是一个 JSON 对象，永远有 "ok" 字段；
        失败时 {"ok": false, "error": "人话", "stage": "..."}。
        stdout 也打一份同样的 JSON，日志一律走 stderr。

    退出码
        0 = 跑完了（业务失败也算，看 JSON 的 ok）
        2 = 参数错误
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback

from . import __version__
from .batch import DEFAULT_MAX_SIDE, DEFAULT_WORKERS, Progress, run as run_batch, tag_one
from .client import Client, VisionError
from .prompt import PROMPT_VERSION, build_prompt
from .store import Store
from .taxonomy import DIMS, DIM_CN, load_vocab


def _emit(payload: dict, json_out: str | None, code: int = 0) -> int:
    """结果写进 --json-out，同时 stdout 打一份。日志走 stderr，互不干扰。"""
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if json_out:
        tmp = json_out + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(text)
            os.replace(tmp, json_out)        # 原子替换：读方看不到半截文件
        except OSError as e:
            sys.stderr.write('结果写不进 %s：%s\n' % (json_out, e))
    sys.stdout.write(text + '\n')
    sys.stdout.flush()
    return code


def _fail(json_out, error, stage, code=0):
    return _emit({'ok': False, 'error': error, 'stage': stage,
                  'trace': traceback.format_exc()[-500:]}, json_out, code)


def _vocab_of(args):
    return load_vocab(args.vocab)


def _store(args) -> Store:
    if not args.outdir:
        raise ValueError('没给 --outdir，不知道把结果存哪。')
    return Store(args.outdir)


# ------------------------------------------------------------------- probe
def cmd_probe(args) -> int:
    """自检：端点通不通、模型在不在、词表几项。

    只回"能不能用"和一句人话说明，不把整份配置倒出来 —— 这个结果经常
    被拿去展示甚至截图，端点地址和密钥不能顺手带出去（--redact 时连地址
    也隐去，只留 host）。
    """
    client = Client(base_url=args.base_url, model=args.model,
                    api_key=args.api_key, timeout=args.timeout)
    vocab = _vocab_of(args)

    out = {
        'ok': True, 'version': __version__,
        'base_url': client.base_url, 'model': client.model,
        'available': False, 'model_ready': False,
        'models': [], 'vocab': {d: len(vocab.get(d) or []) for d in DIMS},
        'prompt_version': PROMPT_VERSION,
        'note': '',
    }
    try:
        models = client.list_models()
        out['available'] = bool(models)
        out['models'] = models[:40]
        out['model_ready'] = client.has_model(client.model)
        if not out['available']:
            out['note'] = '视觉服务起来了但一个模型都没有，先拉一个。'
        elif not out['model_ready']:
            out['note'] = ('服务通了，但里面没有 %s 这个模型。'
                           '装好再来。' % client.model)
        else:
            out['note'] = '就绪：%s 可以用，四维词表共 %d 项。' % (
                client.model, sum(out['vocab'].values()))
    except VisionError as e:
        out['ok'] = False
        out['error'] = str(e)
        out['note'] = ('没法在本机跑视觉模型。先把自带的本地引擎起起来'
                       '（本地模型/启动本地模型.command），再回来试。')

    if args.redact:
        out['base_url'] = _host_only(out['base_url'])
        out['models'] = []          # 模型名也算配置信息，展示场景不给
    return _emit(out, args.json_out)


def _host_only(url):
    u = str(url or '')
    for pre in ('https://', 'http://'):
        if u.startswith(pre):
            u = u[len(pre):]
            break
    return u.split('/')[0]


# --------------------------------------------------------------------- one
def cmd_one(args) -> int:
    """单张打标。用户在对话里传一张图，走这条。"""
    if not args.input:
        return _fail(args.json_out, '没给 --input，不知道要打哪张图。', 'config')
    if not os.path.isfile(args.input):
        return _fail(args.json_out, '找不到这张图。', 'input')
    client = Client(base_url=args.base_url, model=args.model,
                    api_key=args.api_key, timeout=args.timeout,
                    max_tokens=args.max_tokens)
    vocab = _vocab_of(args)
    try:
        store = _store(args)
    except ValueError as e:
        return _fail(args.json_out, str(e), 'config')

    prompt = build_prompt(vocab, args.note or '')
    rec = tag_one(client, prompt, args.input, vocab, args.max_side)

    if rec.get('status') == 'failed' and not rec.get('md5'):
        return _fail(args.json_out, rec.get('error') or '打标失败。', 'input')

    # 明确失败（模型/解析出问题）就不写盘：单张场景用户马上会重试，
    # 存一堆失败记录只会把索引搞脏。走批量时相反 —— 失败也要留痕。
    if rec.get('status') == 'failed':
        return _emit({'ok': False, 'error': rec.get('error') or '打标失败。',
                      'stage': 'model', 'record': _brief(rec)}, args.json_out)

    saved = store.save(rec, rec['source'])
    return _emit({'ok': True, 'record': _brief(saved),
                  'tags_cn': {DIM_CN[d]: (saved.get(d) or [])
                              for d in DIMS},
                  'summary': _sentence(saved)}, args.json_out)


# --------------------------------------------------------------------- run
def cmd_run(args) -> int:
    """批量打标。--input 可以给多次，文件夹会递归收图。"""
    if not args.input:
        return _fail(args.json_out, '没给 --input，不知道要打哪些图。', 'config')
    try:
        store = _store(args)
    except ValueError as e:
        return _fail(args.json_out, str(e), 'config')

    client = Client(base_url=args.base_url, model=args.model,
                    api_key=args.api_key, timeout=args.timeout,
                    max_tokens=args.max_tokens)
    vocab = _vocab_of(args)

    # 先探一下，别让用户等半天才发现模型没装
    try:
        if not client.available:
            return _fail(args.json_out,
                         '连不上视觉模型服务（%s）。先把本地推理服务起起来。'
                         % client.base_url, 'probe')
    except VisionError as e:
        return _fail(args.json_out, str(e), 'probe')

    prog = Progress(args.progress_out, total=0)

    def _on(rec, n):
        mark = {'ok': '.', 'not_pattern': '-', 'empty': '?', 'failed': 'x'}.get(
            rec.get('status'), '?')
        sys.stderr.write(mark)
        if n % 40 == 0:
            sys.stderr.write(' %d\n' % n)
        sys.stderr.flush()

    try:
        s = run_batch(args.input, store, client, vocab,
                      max_side=args.max_side, workers=args.workers,
                      force=args.force, progress=prog,
                      on_done=_on, extra_note=args.note or '',
                      limit=args.limit)
    except FileNotFoundError as e:
        return _fail(args.json_out, str(e), 'input')
    except Exception:                                     # noqa: BLE001
        return _fail(args.json_out, '批量打标中途出错了，已经打好的都在库里。',
                     'batch')

    sys.stderr.write('\n')
    return _emit({'ok': True, 'summary': s, 'store': store.summary(),
                  'outdir': store.root}, args.json_out)


# ----------------------------------------------------------------- summary
def cmd_summary(args) -> int:
    try:
        store = _store(args)
    except ValueError as e:
        return _fail(args.json_out, str(e), 'config')
    items = store.all()
    # 四个维度各出现过的标签排行 —— 一眼看出这批图都长什么样
    facets = {}
    for d in DIMS:
        c = {}
        for r in items:
            for v in r.get(d) or []:
                c[v] = c.get(v, 0) + 1
        facets[d] = sorted(c.items(), key=lambda kv: -kv[1])[:15]
    free_hits = []
    for r in items:
        for d in DIMS:
            for v in (r.get('free') or {}).get(d) or []:
                free_hits.append({'id': r.get('id'), 'dim': d, 'word': v})
    return _emit({'ok': True, 'summary': store.summary(),
                  'facets': facets, 'facets_cn': DIM_CN,
                  'free_words': free_hits[:60],
                  'free_total': len(free_hits)}, args.json_out)


def _brief(rec):
    """给用户看的精简版记录 —— 不带 raw / usage 这些内部字段。"""
    keys = ('id', 'file', 'status', 'status_label', 'w', 'h', 'md5',
            'model', 'elapsed_ms', 'error', 'confidence', 'is_pattern',
            'truncated', 'suspect')
    out = {k: rec.get(k) for k in keys if rec.get(k) is not None}
    for d in DIMS:
        out[d] = rec.get(d) or []
    out['free'] = {d: (rec.get('free') or {}).get(d) or []
                   for d in DIMS if (rec.get('free') or {}).get(d)}
    for k in ('colors', 'keywords', 'desc', 'reason'):
        if rec.get(k):
            out[k] = rec[k]
    return out


def _sentence(rec):
    """一句话说清打了什么标。用户面话术，不用技术词。"""
    parts = []
    for d in DIMS:
        vals = rec.get(d) or []
        if vals:
            parts.append('%s：%s' % (DIM_CN[d], '、'.join(vals)))
    if not parts:
        if rec.get('status') == 'not_pattern':
            return '这张看着不像花型图，没打标签。'
        return '四个维度都没给出合适的标签。'
    return '；'.join(parts)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog='atag.service', add_help=True)
    p.add_argument('--version', action='version', version='atag ' + __version__)
    sub = p.add_subparsers(dest='cmd', required=True)

    def common(sp):
        sp.add_argument('--json-out', default='')
        sp.add_argument('--model', default=None)
        sp.add_argument('--base-url', default=None)
        sp.add_argument('--api-key', default=None)
        sp.add_argument('--timeout', type=float, default=None)
        sp.add_argument('--max-tokens', type=int, default=None)
        sp.add_argument('--vocab', default=None)
        sp.add_argument('--redact', action='store_true')

    q = sub.add_parser('probe')
    common(q)
    q.set_defaults(func=cmd_probe)

    o = sub.add_parser('one')
    common(o)
    # --outdir / --input 都不设 required：缺参数时也要给出**结构化**的
    # JSON 错误（{"ok": false, "stage": "config"}），而不是让 argparse
    # 往 stderr 打一段话就以 2 退出 —— 调用方是按 JSON 解析结果的。
    o.add_argument('--input', '-i', default='')
    o.add_argument('--outdir', default='')
    o.add_argument('--max-side', type=int, default=DEFAULT_MAX_SIDE)
    o.add_argument('--note', default='')
    o.set_defaults(func=cmd_one)

    r = sub.add_parser('run')
    common(r)
    r.add_argument('--input', '-i', action='append', default=[])
    r.add_argument('--outdir', default='')
    r.add_argument('--max-side', type=int, default=DEFAULT_MAX_SIDE)
    r.add_argument('--workers', type=int, default=DEFAULT_WORKERS)
    r.add_argument('--force', action='store_true')
    r.add_argument('--limit', type=int, default=0)
    r.add_argument('--progress-out', default='')
    r.add_argument('--note', default='')
    r.set_defaults(func=cmd_run)

    s = sub.add_parser('summary')
    common(s)
    s.add_argument('--outdir', default='')
    s.set_defaults(func=cmd_summary)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except SystemExit:
        raise
    except Exception:                                     # noqa: BLE001
        return _fail(getattr(args, 'json_out', ''),
                     '打标服务内部出错。', 'fatal')


if __name__ == '__main__':
    sys.exit(main())
