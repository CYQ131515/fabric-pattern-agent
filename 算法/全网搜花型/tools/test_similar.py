#!/usr/bin/env python3
"""相似去重的测试（**不联网、不读真实产物**）。

盯的是四件事：

    · **同一张图的变体**（换尺寸）必须并成一张，且留质量最高的那张
    · **同风格但不同设计**绝不能被并（这是最容易做错的一侧 —— 阈值松
      一点，100 张国风水彩会掉到 40 张）
    · **同素材包**（URL 目录段相同）能在相似度低一些时也合并，
      但一深一浅两张要分开
    · 和**库里已有的**像 → 剔新图，已有的一张不动（refill 补图靠这个）

测试图全是 PIL 现画的色块图：不依赖 out/ 里的真实素材（那批会被
重新检索覆盖），也不受 JPEG 压缩差异影响 —— 同图缩放后的签名应该是
0.999 量级，用真实素材做断言只会把"素材变了"误报成"代码坏了"。
"""
from __future__ import annotations

import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw                                          # noqa: E402
from wsearch import similar                                                # noqa: E402

ok_n = 0
bad_n = 0


def ok(cond, msg, extra=""):
    global ok_n, bad_n
    if cond:
        ok_n += 1
        print("  \u2713 %s" % msg)
    else:
        bad_n += 1
        print("  \u2717 %s %s" % (msg, extra))


def pattern(path, seed=0, n=8, cell=50, colors=None):
    """画一张 n×n 色块图（每张都不一样，但同一 seed + colors 完全一致）。"""
    r = random.Random(seed)
    colors = colors or [(r.randint(20, 235), r.randint(20, 235), r.randint(20, 235))
                        for _ in range(n)]
    w = cell * n
    im = Image.new("RGB", (w, w), "white")
    px = im.load()
    for y in range(w):
        for x in range(w):
            px[x, y] = colors[((x // cell) * 3 + (y // cell) * 5) % len(colors)]
    im.save(path)
    return path


def rec(path, quality=0.5, url="", page="", w=0):
    return {"path": path, "w": w or 400, "h": w or 400,
            "tile": {"quality": quality},
            "source": {"url": url, "page": page, "site": "t.example"}}


def main():
    tmp = tempfile.mkdtemp(prefix="pf_sim_")
    a = pattern(os.path.join(tmp, "a.jpg"), 1)
    b = pattern(os.path.join(tmp, "b.jpg"), 2)

    print("\n[1] 同图变体：换尺寸要并成一张")
    small = os.path.join(tmp, "a_small.jpg")
    Image.open(a).resize((260, 260)).save(small)
    c = similar._dot(similar.sig(a)[0], similar.sig(small)[0])
    ok(c >= 0.995, "同图缩到 260px 的签名余弦 %.4f ≥ 0.995" % c)
    kept, drop = similar.dedup([rec(a, 0.50), rec(small, 0.72)])
    ok(len(kept) == 1 and len(drop) == 1, "两张并成一张（留 %d、剔 %d）"
       % (len(kept), len(drop)))
    ok(kept and kept[0]["path"] == small, "留下的是**平铺质量更高**的那张（0.72）")
    ok(drop and drop[0].get("near_of"), "剔掉的那张记了 near_of（和谁像）")
    ok(drop and "只留一张" in (drop[0].get("screen") or {}).get("why", ""),
       "why 写成了给人看的一句话（进 rejected.json 用）")

    print("\n[2] 不同设计：相似度只有 0.8 量级，绝不能并")
    d = similar._dot(similar.sig(a)[0], similar.sig(b)[0])
    ok(d < 0.95, "两张不同图案的余弦只有 %.4f" % d)
    kept, drop = similar.dedup([rec(a), rec(b)])
    ok(len(kept) == 2 and not drop, "两张都留下")

    print("\n[3] 同色系不同图案（最容易误伤的那类）")
    same_hue = pattern(os.path.join(tmp, "c.jpg"), 1,
                       colors=[(200, 90, 120), (240, 200, 210), (120, 160, 90),
                               (250, 250, 250)] * 2)
    e = similar._dot(similar.sig(a)[0], similar.sig(same_hue)[0])
    kept, drop = similar.dedup([rec(a), rec(same_hue)])
    ok(e < 0.95 and len(kept) == 2, "同色系不同图案（%.4f）不并" % e)

    print("\n[4] 同站 / 同素材包：**不能**因为来源相同就合并")
    # 这条是踩出来的坑：给"同来源包"开个 0.90/0.97 的后门，结果同一
    # 素材站里"红樱桃小花 / 粉牡丹大花"（0.984）、"两种粉牡丹"（0.993）
    # 都被并掉了 —— 配色构图接近但设计不同。现在只认 0.995。
    a_url = "https://a.example/2019/05/16/EPS-190516_33/x1_small.jpg"
    b_url = "https://b.example/2019/05/16/EPS-190516_33/x2_small.jpg"
    same_pack = [rec(a, url=a_url), rec(b, url=b_url)]
    kept, drop = similar.dedup(same_pack)
    ok(len(kept) == 2, "同一个素材包里的两张**不同设计**（0.82）不合并")
    # 同图案压一条白水印带 → 0.99 量级，落在"像但不是同图"的那档
    wm = os.path.join(tmp, "a_wm.jpg")
    im = Image.open(a).copy()
    w, h = im.size
    ImageDraw.Draw(im).rectangle([0, int(h * 0.45), w, int(h * 0.55)],
                                 fill=(255, 255, 255))
    im.save(wm)
    cw = similar._dot(similar.sig(a)[0], similar.sig(wm)[0])
    ok(0.97 <= cw < 0.995, "带水印的同图余弦 %.4f 落在中间档" % cw)
    kept, drop = similar.dedup([rec(a, url=a_url), rec(wm, url=b_url)])
    ok(len(kept) == 2, "中间档（%.4f）+ 同素材包 → 仍然**不**合并（宁可漏，不误杀）" % cw)

    print("\n[6] 和库里已有的像：剔新图，已有的一张不动")
    kept, drop = similar.dedup([rec(a, 0.4)], anchors=[rec(small, 0.6)])
    ok(not kept and len(drop) == 1, "新图和锚像 → 新图被剔")
    ok(drop and drop[0].get("near_of"), "剔的理由指到库里那张")

    print("\n[7] 开关与边界")
    kept, drop = similar.dedup([rec(a), rec(small)], near=0)
    ok(len(kept) == 2 and not drop, "--sim 0 = 关掉这层（不是全删）")
    kept, drop = similar.dedup([])
    ok(kept == [] and drop == [], "空列表不炸")
    missing = os.path.join(tmp, "nope.jpg")
    kept, drop = similar.dedup([rec(missing), rec(a)])
    ok(len(kept) == 2, "读不出来的图当「和谁都不像」，不拖累整批")
    kept, drop = similar.dedup([rec(a), rec(small), rec(b)])
    ok(len(kept) + len(drop) == 3, "数量守恒：留下的 + 剔掉的 = 进来的")

    print("\n[8] 大一点的批：三张同图变体 + 两张不同的")
    trio = []
    for i, side in enumerate((400, 300, 220)):
        p = os.path.join(tmp, "t%d.jpg" % i)
        Image.open(a).resize((side, side)).save(p)
        trio.append(rec(p, 0.3 + i * 0.1))
    kept, drop = similar.dedup(trio + [rec(b), rec(same_hue)])
    ok(len(kept) == 3 and len(drop) == 2, "5 张 → 留 3 张（同图三张并一张）")

    print("\n%d 项通过，%d 项失败" % (ok_n, bad_n))
    return 0 if bad_n == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
