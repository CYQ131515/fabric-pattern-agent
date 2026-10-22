#!/usr/bin/env python3
"""从交互原型里抽出花型库、趋势库与四维词表，生成 data/*.json。

用法：
    python3 tools/extract_from_prototype.py

原型是唯一演示数据源，改动原型后重跑一次即可同步。
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HTML = os.path.join(ROOT, '..', '前端', '历史版本', 'PatternFlow花型智造-交互原型v0.3.html')
OUT = os.path.join(ROOT, 'data')


def read_html():
    if not os.path.exists(HTML):
        sys.exit('找不到原型文件：%s' % os.path.abspath(HTML))
    with open(HTML, encoding='utf-8') as f:
        return f.read()


def grab_array(src, name):
    """按中括号配对取出 `var name = [ ... ]` 的正文。"""
    m = re.search(r'var\s+%s\s*=\s*\[' % re.escape(name), src)
    if not m:
        raise ValueError('原型里找不到数组：%s' % name)
    start = m.end() - 1
    depth = 0
    for j in range(start, len(src)):
        c = src[j]
        if c == '[':
            depth += 1
        elif c == ']':
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise ValueError('未闭合的数组：%s' % name)


def js_array_to_json(text):
    """把 JS 对象数组字面量转成可解析的 JSON（原型里只有单引号字符串与数字）。"""
    text = text.replace("'", '"')
    text = re.sub(r'([{,]\s*)([A-Za-z_]\w*)\s*:', r'\1"\2":', text)
    text = re.sub(r',(\s*[}\]])', r'\1', text)
    return json.loads(text)


def main():
    src = read_html()
    os.makedirs(OUT, exist_ok=True)

    patterns = js_array_to_json(grab_array(src, 'PATTERNS'))
    trends = js_array_to_json(grab_array(src, 'TRENDS'))

    vocab = {}
    for key, name in (('style', 'STYLE_OPTS'), ('element', 'ELEM_OPTS'),
                      ('color', 'COLOR_OPTS'), ('scene', 'SCENE_OPTS')):
        vocab[key] = js_array_to_json(grab_array(src, name))

    for name, payload in (('patterns', patterns), ('trends', trends), ('vocab', vocab)):
        path = os.path.join(OUT, name + '.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print('写入 %s（%d 条）' % (path, len(payload)))

    ids = [p['id'] for p in patterns]
    assert len(set(ids)) == len(ids), '花型编号有重复'
    print('校验通过：%d 款花型，编号唯一' % len(ids))


if __name__ == '__main__':
    main()
