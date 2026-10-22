"""收尾核验：产物是否自洽、判词是否齐全、有没有残留自相矛盾。只读。

用法：
    python3 tools/verify.py [out目录]

之前这是个临时脚本，每次交付前现拼一遍，判「自证其罪」时直接用 screen.CRAFT_BAD
词表 —— 那张表是给扎染写的（反对「平涂」），套到卡通需求上把「平涂圆润」这种
合格判词全报成残留。现在统一走 screen.reconcile()，由它按 craft 家族决定哪些词
算反例。**任何绕过 reconcile 自己扫词表的核验都会复现这个误报。**
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from wsearch import screen  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "out")
if not os.path.isabs(OUT):
    OUT = os.path.join(HERE, OUT)

d = json.load(open(os.path.join(OUT, "index.json"), encoding="utf-8"))
its = d["items"]
need = d.get("need") or {}
craft = need.get("craft") or ""
el = screen._element(need)

ok = True


def chk(cond, msg, extra=""):
    global ok
    ok = ok and bool(cond)
    print(("  ✓ " if cond else "  ✗ ") + msg + (("   " + str(extra)) if not cond else ""))


chk(d["count"] == len(its), "count 与 items 一致（%d / %d）" % (d["count"], len(its)))
chk(len({i["id"] for i in its}) == len(its), "id 不重复")
chk(all(os.path.exists(os.path.join(OUT, i["file"])) for i in its), "原图全在")
chk(all(os.path.exists(os.path.join(OUT, i["thumb"])) for i in its), "缩略图全在")
chk(all((i.get("screen") or {}).get("why") for i in its), "每条都有判词")
chk(all((i.get("screen") or {}).get("match") for i in its), "留下来的全是 match=true")

# 走 reconcile：它会按 craft 挑反例词表，顺便把 _single / _topic 一起查了
bad = []
for i in its:
    v = i.get("screen") or {}
    if screen.passes(v) and not screen.passes(screen.reconcile(v, craft, el)):
        bad.append((i["id"], (screen.reconcile(v, craft, el).get("why") or "")[:30]))
chk(not bad, "没有「判词自证其罪」的残留", bad[:4])
chk(bool(craft), "索引里记着工艺判据（%s）" % craft)

qs = sorted(i.get("tile_quality") or 0 for i in its)
if qs:
    print("\n  张数 %d｜平铺质量 最低 %.3f / 中位 %.3f / 平均 %.3f"
          % (len(its), qs[0], qs[len(qs) // 2], sum(qs) / len(qs)))
print("  工艺判据：%s｜题材：%s" % (craft or "（无）", el or "（无）"))
print("  来源分布：%s" % json.dumps(
    {k: sum(1 for i in its if i.get("src_site") == k)
     for k in {i.get("src_site") for i in its}}, ensure_ascii=False))
rj = os.path.join(OUT, "rejected.json")
if os.path.exists(rj):
    r = json.load(open(rj, encoding="utf-8"))
    thumbs = sum(1 for x in r["items"] if x.get("thumb") and os.path.exists(os.path.join(OUT, x["thumb"])))
    print("  留档 %d 条，缩略图在场的 %d 条" % (r.get("count", len(r["items"])), thumbs))
print("\n%s" % ("全部通过" if ok else "有项目没通过，见上面的 ✗"))
sys.exit(0 if ok else 1)
