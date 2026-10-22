#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实出图冒烟测试：不启服务，直接打生图接口。

用法：
    python3 tools/image_probe.py                 # 用内置示例需求出一张图
    python3 tools/image_probe.py --edit          # 出图后再改一次（验证编辑链路）
    python3 tools/image_probe.py --seam          # 出图后顺带跑四方连续检测
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.dirname(HERE)
sys.path.insert(0, AGENT)

from core.config import Config            # noqa: E402
from core.imagegen import ImageGen, build_prompt, build_edit_prompt  # noqa: E402

# 示例需求卡：法式浪漫 / 花卉 / 蓝白青花 / 女装连衣裙
BRIEF = {
    'style': '法式浪漫',
    'element': '花卉',
    'color': '蓝白青花',
    'scene': '女装连衣裙',
    'composition': '散点排列',
    'density': '中等密度',
    'scale': '中等花型',
}


def seam(path):
    """跑四方连续检测。注意：这是织印师阶段的验收项，生图阶段的图不达标是正常现象。"""
    import subprocess
    r = subprocess.run([sys.executable, os.path.join(HERE, 'seam_check.py'), path],
                       capture_output=True, text=True)
    print((r.stdout or '').strip() or (r.stderr or '').strip())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--edit', action='store_true', help='出图后再改一版')
    ap.add_argument('--seam', action='store_true', help='出图后跑四方连续检测')
    ap.add_argument('--style', default=BRIEF['style'])
    ap.add_argument('--element', default=BRIEF['element'])
    ap.add_argument('--color', default=BRIEF['color'])
    ap.add_argument('--scene', default=BRIEF['scene'])
    ap.add_argument('--from', dest='src', default=None,
                    help='复用已有图片做改图底图，跳过首次出图（省额度）')
    ap.add_argument('--n', type=int, default=1, help='一次出几张（验证组图/逐张补齐）')
    args = ap.parse_args()

    brief = dict(BRIEF)
    brief.update({'style': args.style, 'element': args.element,
                  'color': args.color, 'scene': args.scene})

    cfg = Config()
    gen = ImageGen(cfg)
    prompt = build_prompt(brief)
    print('[配置] provider=%s model=%s size=%s' % (gen.provider, gen.model, gen.size))
    print('[配置] key=%s...%s' % (gen.api_key[:6], gen.api_key[-4:]) if gen.api_key else '[配置] 无 key')
    print('[提示词] %s' % prompt)
    print('-' * 60)

    if args.src:
        # 复用已有图当底图，只走改图链路（省一次出图额度）
        import mimetypes
        item = {'path': os.path.abspath(args.src),
                'mime': mimetypes.guess_type(args.src)[0] or 'image/jpeg'}
        print('[复用底图] %s' % item['path'])
        if args.seam:
            seam(item['path'])
    else:
        t0 = time.time()
        try:
            res = gen.generate(prompt, n=args.n, tag='probe')
        except Exception as e:
            print('[失败] %s: %s' % (type(e).__name__, e))
            return 1
        dt = time.time() - t0
        item = res['files'][0]
        print('[出图成功] 实际模型=%s  张数=%d  耗时=%.1fs'
              % (res.get('model'), res.get('count', 1), dt))
        print('[文件] %s' % item.get('path'))
        print('[格式] %s  %.1f KB' % (item.get('mime'), item.get('bytes', 0) / 1024.0))
        if len(res['files']) > 1:
            for f in res['files']:
                print('[第 %d 张] %s  %.1f KB' % (
                    res['files'].index(f) + 1, f.get('path'), f.get('bytes', 0) / 1024.0))
        if args.seam and os.path.exists(item.get('path', '')):
            print('-' * 60)
            seam(item['path'])

    if not args.edit:
        return 0

    print('=' * 60)
    print('[改图] 只改配色 → 粉紫柔雾，其余保持原样')
    ep = build_edit_prompt(prompt, {'color': '粉紫柔雾'}, brief)
    print('[改图提示词] %s' % ep)
    t1 = time.time()
    try:
        with open(item['path'], 'rb') as f:
            import base64
            b64 = base64.b64encode(f.read()).decode('ascii')
        res2 = gen.generate(ep, image=b64, image_mime=item.get('mime') or 'image/png', tag='probe-edit')
    except Exception as e:
        print('[改图失败] %s: %s' % (type(e).__name__, e))
        return 1
    it2 = res2['files'][0]
    print('[改图成功] 耗时=%.1fs' % (time.time() - t1))
    print('[文件] %s  %s  %.1f KB' % (
        it2.get('path'), it2.get('mime'), it2.get('bytes', 0) / 1024.0))
    if args.seam and os.path.exists(it2.get('path', '')):
        seam(it2['path'])
    return 0


if __name__ == '__main__':
    sys.exit(main())
