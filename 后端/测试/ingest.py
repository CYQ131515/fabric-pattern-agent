#!/usr/bin/env python3
"""批量入库：把一个目录（或几个文件）的图扫进本地花型库，AI 自动打标签。

用法：
    python3 tools/ingest.py generated/                    # 整个目录
    python3 tools/ingest.py 客户来图/ --limit 20          # 只导前 20 张
    python3 tools/ingest.py 图1.jpg 图2.png               # 指定几个文件
    python3 tools/ingest.py generated/ --dry-run          # 只打标签看结果，不入库
    python3 tools/ingest.py generated/ --workers 5        # 并发 5 路打标签
    python3 tools/ingest.py 客户来图/ --any-image         # 不校验"是不是花型图"
    python3 tools/ingest.py 客户来图/ --source import --move   # 导完删源文件

为什么并发：打一次标签要 2~5 秒，串行导 200 张要十几分钟。
并发 3~5 路基本是"边聊边导"的体验，再高意义不大（模型端也会限流）。
"""
import argparse
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import Config            # noqa: E402
from core.library import PatternLibrary   # noqa: E402

EXTS = ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff')

OK = '\033[32m✓\033[0m'
DUP = '\033[36m↺\033[0m'
SKIP = '\033[33m·\033[0m'
BAD = '\033[31m✗\033[0m'


def scan(paths, limit=None, grep=None):
    out = []
    for p in paths:
        if os.path.isfile(p):
            out.append(p)
            continue
        for root, dirs, files in os.walk(p):
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for f in sorted(files):
                if f.lower().endswith(EXTS) and not f.startswith('.'):
                    full = os.path.join(root, f)
                    # 库自己的目录不要重复吞进去
                    if os.sep + 'library' + os.sep in full + os.sep:
                        continue
                    if grep and grep not in f:
                        continue
                    out.append(full)
    out.sort()
    return out[:limit] if limit else out


def short(s, n=26):
    s = str(s or '')
    return s if len(s) <= n else s[:n - 1] + '…'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('paths', nargs='+', help='目录或图片文件')
    ap.add_argument('--limit', type=int, default=0, help='最多处理多少张')
    ap.add_argument('--workers', type=int, default=3, help='并发打标签路数（默认 3）')
    ap.add_argument('--dry-run', action='store_true', help='只打标签不入库')
    ap.add_argument('--no-tag', action='store_true', help='不打标签直接入库（之后可重打）')
    ap.add_argument('--any-image', action='store_true',
                    help='不校验"是不是花型图"，什么都收')
    ap.add_argument('--source', default='import', help='来源标记（默认 import）')
    ap.add_argument('--grep', default=None, help='文件名包含这个才处理')
    ap.add_argument('--move', action='store_true', help='入库成功后删除源文件')
    ap.add_argument('--force', action='store_true', help='忽略重复检测，硬导')
    args = ap.parse_args()

    cfg = Config()
    lib = PatternLibrary(cfg)
    files = scan(args.paths, args.limit or None, args.grep)
    if not files:
        print('没扫到图片。支持的后缀：%s' % '、'.join(EXTS))
        return 1

    print('花型库：%s' % lib.root)
    print('扫描到 %d 张图%s，并发 %d 路%s\n'
          % (len(files), '（已截断）' if args.limit and len(files) >= args.limit else '',
             max(1, args.workers), ' [预演，不入库]' if args.dry_run else ''))

    stats = {"ok": 0, "dup": 0, "skip": 0, "not_pattern": 0, "fail": 0}
    lock = threading.Lock()
    t0 = time.time()
    done = [0]
    problems = []

    def work(path):
        t = time.time()
        try:
            if args.dry_run:
                with open(path, 'rb') as f:
                    blob = f.read()
                from core.imagegen import sniff
                res = lib.tagger.tag(blob, sniff(blob)[1])
                if res.get('ok'):
                    r = {"ok": True, "dry": True, "tag": res}
                elif res.get('not_pattern'):
                    r = {"ok": False, "not_pattern": True, "reason": res.get('reason')}
                else:
                    r = {"ok": False, "error": "没读出标签"}
            else:
                r = lib.ingest_file(path, source=args.source, tag=not args.no_tag,
                                    check_pattern=not args.any_image, force=args.force,
                                    move=args.move)
        except Exception as e:
            r = {"ok": False, "error": '%s: %s' % (type(e).__name__, e)}
        r['_path'] = path
        r['_secs'] = time.time() - t

        with lock:
            done[0] += 1
            i = done[0]
            name = ''
            if r.get('dup'):
                stats['dup'] += 1
                tag = '库里已有'
                mark = DUP
            elif r.get('not_pattern'):
                stats['not_pattern'] += 1
                tag = '不是花型图'
                mark = SKIP
                problems.append((path, r.get('reason') or r.get('error')))
            elif not r.get('ok'):
                stats['fail'] += 1
                tag = r.get('error') or '失败'
                mark = BAD
                problems.append((path, r.get('error')))
            else:
                stats['ok'] += 1
                mark = OK
                if r.get('dry'):
                    t2 = r.get('tag') or {}
                    tag = '%s / %s / %s' % (t2.get('style') or '?',
                                            t2.get('element') or '?',
                                            t2.get('color') or '?')
                    name = t2.get('name') or ''
                else:
                    it = r.get('item') or {}
                    tag = '%s / %s / %s' % (it.get('style') or '?', it.get('element') or '?',
                                            it.get('color') or '?')
                    name = it.get('name') or ''
            print('[%3d/%3d] %s %-14s %-34s %s  %.1fs'
                  % (i, len(files), mark, short(name, 14), short(tag, 34),
                     '' if r.get('ok') or r.get('dup') or r.get('not_pattern')
                     else short(r.get('error'), 20), r['_secs']))
        return r

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        list(ex.map(work, files))

    dt = time.time() - t0
    print('\n%s' % ('—' * 64))
    print('入库 %d · 重复 %d · 非花型 %d · 失败 %d · 用时 %.0fs（%.1fs/张）'
          % (stats['ok'], stats['dup'], stats['not_pattern'], stats['fail'],
             dt, dt / max(1, len(files))))
    if problems:
        print('\n没进库的：')
        for p, why in problems[:15]:
            print('  - %s  →  %s' % (os.path.basename(p), short(why, 60)))
        if len(problems) > 15:
            print('  …还有 %d 个' % (len(problems) - 15))
    if not args.dry_run and stats['ok']:
        s = lib.stats()
        print('\n库内共 %d 张。启动服务后打开花型库页面就能看到。' % s['count'])
    return 0


if __name__ == '__main__':
    sys.exit(main())
