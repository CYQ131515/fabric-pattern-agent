"""人设加载：personas/*.md，支持最简 frontmatter。

    ---
    name: 花型设计师
    description: 需求澄清 · 检索 · 推荐
    ---

    正文即系统提示词。
"""
import os
import re


def load_one(path):
    with open(path, encoding='utf-8') as f:
        raw = f.read()
    meta, body = {}, raw
    m = re.match(r'^---\s*\n(.*?)\n---\s*\n', raw, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ':' in line:
                k, v = line.split(':', 1)
                meta[k.strip()] = v.strip()
        body = raw[m.end():]
    return {
        "key": os.path.splitext(os.path.basename(path))[0],
        "name": meta.get('name') or os.path.splitext(os.path.basename(path))[0],
        "description": meta.get('description', ''),
        "body": body.strip(),
        "path": path,
    }


def load_all(directory):
    out = {}
    if not os.path.isdir(directory):
        return out
    for fn in sorted(os.listdir(directory)):
        if fn.endswith('.md'):
            p = load_one(os.path.join(directory, fn))
            out[p['key']] = p
    return out
