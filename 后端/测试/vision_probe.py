#!/usr/bin/env python3
"""识图能力探针：拿一张花型图问模型，看它能不能读出六维标签。

这是后续做 analyze_pattern_image 技能前的验证工具，不是正式链路。
用法：
    python3 tools/vision_probe.py 图片路径
    python3 tools/vision_probe.py --make-sample     # 先造一张测试用小碎花图

注意：deepseek-flash 是推理模型，思考 token 从 max_tokens 里扣，
      预算不够会只吐 reasoning_content、正文为空。
"""
import argparse
import base64
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FIELDS = {
    'style': '法式浪漫/新中式国风/复古波普/现代极简/童趣插画/波西米亚/Y2K未来/田园草木',
    'element': '花卉/叶片藤蔓/几何/动物纹/条纹格纹/波点/抽象笔触/海洋贝壳',
    'color': '莫兰迪灰调/马卡龙甜彩/黑白灰/高饱和撞色/大地色系/蓝白青花/粉紫梦幻/暖橙焦糖',
    'composition': '散点/满地/定位/条纹/边花',
    'density': '疏/中/密',
    'scale': '小碎花/中花/大花',
}

PROMPT = '这是一块面料花型图。只输出一行 JSON，不要解释，字段：' + \
         '、'.join('%s（限：%s）' % (k, v) for k, v in FIELDS.items()) + '、reason（一句话判断依据）。'


def make_sample(path):
    """纯标准库造一张测试用小碎花 PNG（奶油白底 + 蓝花散点 + 零星叶片）。"""
    import random
    import struct
    import zlib
    W = H = 384
    BG, BLUE, GREEN = (245, 239, 226), (58, 92, 148), (90, 120, 96)
    px = [[BG] * W for _ in range(H)]

    def disc(cx, cy, r, c):
        for y in range(max(0, cy - r), min(H, cy + r + 1)):
            for x in range(max(0, cx - r), min(W, cx + r + 1)):
                if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                    px[y][x] = c

    rnd = random.Random(7)
    for i in range(26):
        cx, cy = rnd.randint(24, W - 24), rnd.randint(24, H - 24)
        disc(cx, cy, 4, BLUE)
        for dx, dy in ((9, 0), (-9, 0), (0, 9), (0, -9)):
            disc(cx + dx, cy + dy, 6, BLUE)
        if i % 3 == 0:
            disc(cx + 13, cy + 11, 4, GREEN)

    raw = bytearray()
    for row in px:
        raw.append(0)
        for p in row:
            raw += bytes(p)

    def chunk(tag, data):
        return (struct.pack('>I', len(data)) + tag + data
                + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff))

    png = (b'\x89PNG\r\n\x1a\n'
           + chunk(b'IHDR', struct.pack('>IIBBBBB', W, H, 8, 2, 0, 0, 0))
           + chunk(b'IDAT', zlib.compress(bytes(raw), 9))
           + chunk(b'IEND', b''))
    with open(path, 'wb') as f:
        f.write(png)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('image', nargs='?', help='图片路径')
    ap.add_argument('--make-sample', action='store_true', help='先造一张测试图')
    ap.add_argument('--max-tokens', type=int, default=4000)
    args = ap.parse_args()

    img = args.image
    if args.make_sample or not img:
        img = make_sample(img or 'sample_pattern.png')
        print('已生成测试图：%s' % img)

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core.config import Config
    cfg = Config()
    if not cfg.llm_enabled:
        print('没配 api_key，跑不了。')
        return 1

    mime = mimetypes.guess_type(img)[0] or 'image/png'
    with open(img, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode()

    body = {
        'model': cfg.model,
        'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': PROMPT},
            {'type': 'image_url', 'image_url': {'url': 'data:%s;base64,%s' % (mime, b64)}}]}],
        'max_tokens': args.max_tokens,
    }
    if cfg.settings['llm'].get('effort'):
        body['effort'] = cfg.settings['llm']['effort']

    req = urllib.request.Request(
        cfg.base_url + '/chat/completions',
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + cfg.api_key},
        method='POST')
    try:
        with urllib.request.urlopen(req, timeout=cfg.settings['llm'].get('timeout', 60)) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print('HTTP %s：%s' % (e.code, e.read().decode()[:400]))
        return 1

    ch = data['choices'][0]
    usage = data.get('usage', {})
    print('finish_reason :', ch.get('finish_reason'))
    print('思考/输出 token:', usage.get('completion_tokens_details', {}).get('reasoning_tokens'),
          '/', usage.get('completion_tokens'))
    print('识图结果     :', (ch['message'].get('content') or '(空)').strip())
    return 0


if __name__ == '__main__':
    sys.exit(main())
