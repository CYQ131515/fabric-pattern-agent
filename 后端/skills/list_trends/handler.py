"""趋势方向：与需求卡已填维度做一致性打分，冲突越少越靠前。"""


def run(args, ctx):
    trends = ctx.get('trends') or []
    brief = ctx.get('brief') or {}
    try:
        limit = int(args.get('limit') or 3)
    except (TypeError, ValueError):
        limit = 3

    scored = []
    for t in trends:
        tags = t.get('tags') or {}
        hit = sum(1 for k, v in brief.items() if v and tags.get(k) == v)
        conflict = sum(1 for k, v in brief.items() if v and tags.get(k) and tags.get(k) != v)
        scored.append((hit - conflict, hit, t))

    scored.sort(key=lambda x: (-x[0], -x[1]))
    pool = [x for x in scored if x[0] >= 0][:limit] or scored[:limit]
    return {
        "count": len(pool),
        "items": [{
            "id": t['id'], "title": t['title'], "window": t.get('window'),
            "why": t.get('why'), "tags": t.get('tags'),
        } for _, _, t in pool],
        "note": "趋势内容需标注来源与日期；原型数据仅供演示。",
    }
