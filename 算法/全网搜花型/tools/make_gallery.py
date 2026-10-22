#!/usr/bin/env python3
"""把产物目录画成一个单文件画廊页，点开就能逐张看 + 看机器判词。

为什么要有这个：用户验收的时候不会去翻 `orig/` 那一百个文件，
他要的是一个能滚动的页面。做成单文件（缩略图 base64 内嵌）是因为
它要能直接丢进任何浏览器 / 预览面板，不依赖本地服务、不依赖相对路径。

    ./.venv/bin/python tools/make_gallery.py                 # 默认画 out/
    ./.venv/bin/python tools/make_gallery.py --outdir out --side 360

产出 `out/_gallery.html`（下划线开头 = 辅助件，不是算法产物）。
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image                                                  # noqa: E402

HTML = """<!doctype html><html lang="zh"><meta charset="utf-8">
<title>%(title)s</title>
<style>
body{margin:0;padding:22px 26px;font:14px/1.6 -apple-system,"PingFang SC",sans-serif;
     background:#faf8f4;color:#2a2620}
h1{font-size:19px;margin:0 0 6px}
.meta{color:#7a7268;font-size:12.5px;margin-bottom:16px}
.meta b{color:#AE8836}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(%(cell)dpx,1fr));gap:14px}
.c{margin:0;background:#fff;border:1px solid #e8e2d8;border-radius:10px;overflow:hidden}
.c img{display:block;width:100%%;aspect-ratio:1;object-fit:cover;background:#f2efe9}
figcaption{padding:7px 9px 9px;font-size:11.5px;line-height:1.45;color:#6b6459}
figcaption b{color:#AE8836}
figcaption code{color:#b0a89c;font-size:10.5px}
.q{display:inline-block;margin-left:4px;padding:0 4px;border-radius:4px;
   background:#f3eee4;color:#8a7c5f}
figcaption em{display:block;font-style:normal;color:#9a928a;margin-top:2px}
</style>
<h1>%(title)s</h1>
<div class="meta">需求：<b>%(need)s</b>　·　共 <b>%(n)d</b> 张%(craft)s
　·　%(q)s</div>
<div class="grid">%(cards)s</div>
</html>"""


def _b64(path, side, quality=62):
    with Image.open(path) as im:
        im.load()
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            flat = Image.new("RGB", im.size, (255, 255, 255))
            flat.paste(im, mask=im.split()[-1])
            im = flat
        else:
            im = im.convert("RGB")
        w, h = im.size
        k = min(1.0, side / float(max(w, h)))
        if k < 1:
            im = im.resize((max(1, int(w * k)), max(1, int(h * k))), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def build(outdir, side=320, title="全网搜花型"):
    idx = json.load(open(os.path.join(outdir, "index.json"), encoding="utf-8"))
    items = idx.get("items") or []
    meta = idx.get("need") or {}
    # 老产物可能只记了 meta 里的 need 字符串，两种都认
    need = meta.get("need") or meta.get("queries") or ""
    if isinstance(need, (list, tuple)):
        need = need[0] if need else ""
    craft = ("　·　判据按「%s」这种做法" % meta["craft"]) if meta.get("craft") else ""

    cards = []
    for n, it in enumerate(items, 1):
        rel = it.get("thumb") or it.get("file") or ""
        src = os.path.join(outdir, rel)
        if not rel or not os.path.exists(src):
            continue                      # 文件被删了就别放空壳
        sc = it.get("screen") or {}
        why = (sc.get("why") or "").replace("<", "&lt;")
        cards.append(
            '<figure class="c"><img src="data:image/jpeg;base64,%s" alt="">'
            '<figcaption><b>%02d</b> <code>%s</code>'
            '<span class="q">%s</span><em>%s</em></figcaption></figure>'
            % (_b64(src, side), n, str(it.get("id", "")).rsplit("-", 1)[-1],
               ("平铺 未检测" if it.get("tile_quality") is None
                else "平铺 %s" % it.get("tile_quality")), why))

    qs = [it.get("tile_quality") for it in items
          if it.get("tile_quality") is not None]
    qtxt = ("平均平铺质量 %.3f" % (sum(qs) / len(qs))) if qs else "平铺检测：未检测"
    html = HTML % {
        "title": title, "need": need, "n": len(cards), "craft": craft,
        "q": qtxt, "cell": int(side * 0.78),
        "cards": "".join(cards)}
    path = os.path.join(outdir, "_gallery.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path, len(cards)


def main():
    p = argparse.ArgumentParser(description="把产物目录画成单文件画廊页")
    # 产物统一在 `产物/` 下（见 run.py 的 OUT_ROOT）。不写就画默认那轮。
    p.add_argument("--outdir", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "产物", "out"))
    p.add_argument("--side", type=int, default=320, help="缩略图长边（默认 320）")
    p.add_argument("--title", default="全网搜花型 · 甄别之后")
    a = p.parse_args()
    if not os.path.exists(os.path.join(a.outdir, "index.json")):
        print("这个目录里没有 index.json，先跑 search。")
        return 1
    path, n = build(a.outdir, a.side, a.title)
    print("画廊页：%s（%d 张，%.1f MB）"
          % (path, n, os.path.getsize(path) / 1048576.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
