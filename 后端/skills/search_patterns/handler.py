"""花型库检索：内置款 + 本地真图库，标签命中优先，其次匹配度与热度。

两个来源合并检索：
    patterns —— 内置演示款，图案由前端用 SVG 程序画出来，没有实体图片；
    library  —— 本地真图库，AI 打过标签的图片文件，有实体图。

**真图优先**：客户说"要蓝白青花的花卉连衣裙料"，能直接给他看图片才有意义，
所以同分时库里的真图排在程序画的款前面。返回里 `lib:true` 的条目带
`image` / `full` 两个地址，前端照着显示图片就行。

这是「本地算法」挂载点——换向量检索时只改这个文件和 library.search()，
模型侧和前端都不用动。
"""
import time

DIMS = ('style', 'element', 'color', 'scene')


def _score(p, brief):
    known = [k for k in DIMS if brief.get(k)]
    if not known:
        return min(96, int(62 + p.get('heat', 0) * 0.2)), 0
    hit = sum(1 for k in known if p.get(k) == brief.get(k))
    return max(46, min(99, int(52 + hit / len(known) * 42))), hit


def _svg_item(p, match, hit):
    return {
        "id": p['id'],
        "name": p['name'],
        "style": p.get('style'),
        "element": p.get('element'),
        "color": p.get('color'),
        "scene": p.get('scene'),
        "match": match,
        "hit": hit,
        "seam": p.get('seam'),
        "quality": p.get('quality'),
        "desc": p.get('desc'),
        "fabric": p.get('fabric'),
        "lib": False,
    }


def run(args, ctx):
    patterns = ctx.get('patterns') or []
    lib = ctx.get('library')
    brief = dict(ctx.get('brief') or {})
    for k in DIMS:
        if args.get(k):
            brief[k] = args[k]
    query = (args.get('query') or '').strip()
    try:
        limit = int(args.get('limit') or 6)
    except (TypeError, ValueError):
        limit = 6

    rows = []
    for p in patterns:
        score, hit = _score(p, brief)
        if query and (query in p.get('name', '') or query in p.get('desc', '')
                      or query in ' '.join(p.get('l2', []))):
            score += 8
        rows.append({"hit": hit, "match": score, "lib": 0, "item": _svg_item(p, score, hit)})

    lib_total, lib_note = 0, None
    if lib is not None and getattr(lib, 'enabled', True):
        try:
            # 库里多要几条：真图同分的多（一次生成的几个变体标签几乎一样），
            # 只要 limit 条的话合并后容易被挤没。
            res = lib.search(brief=brief, q=query, limit=max(limit, 12))
            lib_total = res.get('total', 0)
            for it in res.get('items') or []:
                rows.append({"hit": it.get('hit', 0), "match": it.get('match', 0),
                             "lib": 1, "item": it})
        except Exception as e:
            lib_note = '%s: %s' % (type(e).__name__, e)
            print('[search_patterns] 图库检索失败，只返回内置款：%s' % e)

    # 命中维度数 → 匹配度 → 真图优先（同分时客户能直接看图）
    rows.sort(key=lambda x: (-x['hit'], -x['match'], -x['lib']))
    items = [r['item'] for r in rows[:limit]]

    # 「库里有对得上的吗」必须由本地算法定，不能交给模型自己感觉。
    # 模型看到一堆 60~80 分的近似款，很容易当成命中就开始报候选。
    known = [k for k in DIMS if brief.get(k)]
    best_hit = max([i.get('hit', 0) for i in items] + [0])
    # 用户只给了一个维度 → 命中 1 个就算对上；给了多个 → 至少命中 2 个才算，
    # 只中 1 个维度的那是"沾边"，不是"有货"。
    need = 1 if len(known) <= 1 else 2
    matched = bool(known) and best_hit >= need

    # 记一笔「这次按什么条件查的、有没有对上」。
    # 干什么用：`generate_pattern` 靠它判断"生成之前到底查过库没有" ——
    # 「花型库没有匹配的才要生成」这条不能指望模型自觉（实测它经常直接开提案）。
    sess = ctx.get('session')
    if sess is not None:
        sess.last_search = {
            "brief": {k: v for k, v in brief.items() if v},
            "matched": matched,
            "best_hit": best_hit,
            "at": time.time(),
        }

    lib_count = sum(1 for i in items if i.get('lib'))
    note = ("hit 为命中的维度数；match 为匹配度（含热度加成）。"
            "matched=false 表示库里没有真正对得上的款（最多只是沾边），"
            "这种情况不要硬推近似款当答案，应引导用户改用 AI 生成。")
    if lib_count:
        note += ("其中 %d 条来自花型库的**真图**（lib=true，带 image 地址），"
                 "回复里要让用户能直接看到这几张。" % lib_count)
    if lib_total:
        note += "本地图库共 %d 张在库。" % lib_total
    if lib_note:
        note += "（图库这次没检索成功：%s）" % lib_note

    return {
        "brief": {k: v for k, v in brief.items() if v},
        "count": len(items),
        "items": items,
        "picks": [i['id'] for i in items],
        "matched": matched,
        "best_hit": best_hit,
        "known_dims": len(known),
        "need_hit": need,
        "lib_count": lib_count,
        "lib_total": lib_total,
        "note": note,
    }
