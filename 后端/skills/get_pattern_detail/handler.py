"""花型详情：给模型提供可引用的事实，避免它编造参数。"""


def run(args, ctx):
    patterns = ctx.get('patterns') or []
    pid = (args.get('pattern_id') or '').strip()
    name = (args.get('name') or '').strip()

    hit = None
    for p in patterns:
        if pid and p['id'].upper() == pid.upper():
            hit = p
            break
        if name and name in p['name']:
            hit = p
            break
    if not hit:
        return {"error": "没找到这款花型，请确认编号或名称。",
                "available": [p['name'] for p in patterns[:12]]}

    rights = ('授权素材，需按授权范围使用' if '-LIC-' in hit['id']
              else '客户私有，不对外流通' if '-CUS-' in hit['id']
              else '自有原创 / AI 生成，可商用')
    return {
        "id": hit['id'], "name": hit['name'], "version": hit.get('v'),
        "style": hit.get('style'), "element": hit.get('element'),
        "color": hit.get('color'), "scene": hit.get('scene'),
        "l2": hit.get('l2'), "desc": hit.get('desc'),
        "sell": hit.get('sell'), "fabric": hit.get('fabric'),
        "seam": hit.get('seam'), "quality": hit.get('quality'), "heat": hit.get('heat'),
        "rights": rights,
    }
