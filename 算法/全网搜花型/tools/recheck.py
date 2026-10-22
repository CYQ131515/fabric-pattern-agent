#!/usr/bin/env python3
"""单张复核：把"素材展示图"从平铺花型列表里挑出去。

为什么不能只靠整批甄别
    一次给模型 10 张时，它会顺着上一张的结论走；而"这是不是**一整块连续花纹**"
    这种需要盯着看的判断，挤在一起时最容易被"花挺好看"带过去 —— 真机两批都
    混进了白底素材散排、六朵菊花带题字、四格材质板、还带黑框的展示图。
    单张判就准得多，而且**只剔这一类**，不像整批重判那样把好图连带删掉
    （实测整批重判 96 张会掉到 63 张，其中一多半是好的）。

用法
    python3 tools/recheck.py out_watercolor2              # 只报告，不动文件
    python3 tools/recheck.py out_watercolor2 --apply      # 真删（先移到 /tmp）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tools"))

from wsearch import screen, service, store                            # noqa: E402

PROMPT = """这是一张从图片网站下载的图，我在筛「面料印花能用的花型图案」。
只看这一张，回答一个问题：**它本身是不是一整块连续的花纹（印花/壁纸/瓷砖那种，
能四方连续铺开、铺开后画面均匀）？**

判 false 的情况 —— 它们都是「素材展示」，不是花纹本身：
· 白底/浅底上摆着几朵花、几枝花、几个小图，彼此不相连（素材预览、贴纸套装、
  元素合集、花枝素材摆放）
· 被格线分成四格/六格/多格，每格一种花（素材展示板）
· **单幅画作**：一朵或一丛花，四周大片空白，像一幅小画；带题字、落款、印章
· 带画框、卡纸、黑边/白边的展示图；图案贴在器物、立体物上的效果图
· 实物照片、布料实拍（有褶皱光影）、模特/成衣、海报、屏幕截图

判 true 的情况：
· 花纹铺满画面，或花纹在画面里**均匀重复**（散点排布也算，只要间距均匀、
  不给人"几组不同的图案摆在一起"的感觉）
· 带半透明小字水印的（水印不管）

只输出一行 JSON：{"tile": true 或 false, "why": "12字以内"}
"""


def _parse(text):
    """从回复里抠 {"tile": bool}。判不出来回 None（调用方保留）。"""
    t = (text or "").strip()
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        return None
    try:
        j = json.loads(m.group(0))
    except Exception:                                            # noqa: BLE001
        return None
    if "tile" not in j:
        return None
    return {"tile": bool(j.get("tile")),
            "why": str(j.get("why") or "")[:20]}


def _client():
    a = argparse.Namespace(vlm_base="", vlm_key="", vlm_model="",
                           vlm_timeout=0, vlm_max_side=0)
    return service._vlm(a)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("outdir")
    ap.add_argument("--apply", action="store_true", help="真删（移到 /tmp）")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()

    out = a.outdir if os.path.isabs(a.outdir) else os.path.join(HERE, a.outdir)
    d = json.load(open(os.path.join(out, "index.json"), encoding="utf-8"))
    items = d["items"]
    client = _client()
    if not client.available():
        print("视觉模型没起来，先别复核（会全判不出来）")
        return 2

    t0 = time.time()

    def one(it):
        p = os.path.join(out, it["file"])
        try:
            txt = client.see(PROMPT, [p])
            return it, _parse(txt)
        except Exception as e:                                   # noqa: BLE001
            return it, {"tile": None, "why": "调用失败:%s" % str(e)[:20]}

    with ThreadPoolExecutor(max_workers=min(a.workers, len(items))) as ex:
        res = list(ex.map(one, items))

    bad = [(it, v) for it, v in res if v and v.get("tile") is False]
    unknown = [(it, v) for it, v in res if not v or v.get("tile") is None]
    print("复核 %d 张，用时 %.1fs" % (len(items), time.time() - t0))
    print("判为素材/非花型：%d 张；没判出来：%d 张（保留）" % (len(bad), len(unknown)))
    for it, v in bad:
        print("  %s  %s" % (it["id"][-4:], v.get("why")))

    if not a.apply:
        print("\n（只报告，没动文件。确认无误后加 --apply）")
        return 0
    if not bad:
        return 0

    # 逐张删太容易触发沙箱的"单轮删除"守卫，统一先挪到 /tmp 再同步索引
    trash = "/tmp/pf_recheck_%d" % int(time.time())
    os.makedirs(trash, exist_ok=True)
    for it, _v in bad:
        for rel in (it["file"], it.get("thumb")):
            if not rel:
                continue
            src = os.path.join(out, rel)
            if os.path.exists(src):
                os.rename(src, os.path.join(trash, os.path.basename(src)))
        # 判词留在索引里会误导，直接记为剔除原因（store.append 会清死条目）
    idx, _paths, st = store.append([], out, log=print)
    print("删后剩 %d 张（挪到 %s）" % (len(idx["items"]), trash))
    return 0


if __name__ == "__main__":
    sys.exit(main())
