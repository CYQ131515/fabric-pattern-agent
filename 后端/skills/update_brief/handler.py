"""需求卡写入：词表是「推荐档位」，不是「唯一合法值」。

原先这里是硬闸门——不在词表里的值一律退回，让模型改用「最接近的合法档」。
后果是用户说「淡红」，系统回一句「色板里没有淡红这一档，我选了最接近的粉紫梦幻」，
用户当场觉得自由度太低（原话）。

现在的规则：
    - 词表里的值照收。标准档在检索、统计、下游技能之间是对齐的；
    - 词表外的值**也收**，只记一条 off_vocab 让模型知道「这个词不在常用档位里，
      但已经按用户说的采用，会原样写进生图提示词」。
    换句话说：闸门从「拦」变成「提示」，最终以用户说的为准。

词表仍然返回给模型（vocab 字段），用户没明确指定时它可以优先挑标准档。
"""
DIMS = ('style', 'element', 'color', 'scene')


def run(args, ctx):
    vocab = ctx.get('vocab') or {}
    brief = dict(ctx.get('brief') or {})
    accepted, off_vocab = {}, []

    for dim in DIMS:
        val = args.get(dim)
        if not val:
            continue
        val = str(val).strip()
        if not val:
            continue
        allowed = vocab.get(dim) or []
        accepted[dim] = val
        if allowed and val not in allowed:
            near = [v for v in allowed if v[:2] in val or val[:2] in v]
            off_vocab.append({
                "dim": dim,
                "value": val,
                "near": near,
                "note": ("「%s」不在常用档位里，但已按用户说的采用，会原样写进生图提示词，"
                         "不要替他改成别的说法。" % val)
                        + (("如果用户没特别坚持，也可以建议靠近标准档：" + '、'.join(near))
                           if near else ""),
            })

    for dim in (args.get('clear') or []):
        if dim in DIMS:
            brief[dim] = None

    brief.update(accepted)
    return {
        "brief": {k: v for k, v in brief.items() if v},
        "accepted": accepted,
        "off_vocab": off_vocab,
        "vocab": {k: (vocab.get(k) or []) for k in DIMS},
        "filled": [k for k in DIMS if brief.get(k)],
        "missing": [k for k in DIMS if not brief.get(k)],
    }
