#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""照片体检模块的标定与试跑。

用法：
    python3 tools/photo_qc_probe.py <图片> ...        # 体检指定图片
    python3 tools/photo_qc_probe.py --calibrate <图>  # 拿清晰图造各种劣化变体，校准阈值
"""
import os
import struct
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.dirname(HERE)
sys.path.insert(0, AGENT)

from core import photo_qc              # noqa: E402
from core.imgio import as_png, cleanup, decode_png  # noqa: E402


# ---------------------------------------------------------------- PNG 写出
def encode_png(w, h, ch, px):
    """把像素写成 PNG（RGB，filter 0）。只为造测试样本，不做优化。"""
    raw = bytearray()
    stride = w * ch
    for y in range(h):
        raw.append(0)
        raw += px[y * stride:(y + 1) * stride]
    def chunk(tag, body):
        return (struct.pack('>I', len(body)) + tag + body
                + struct.pack('>I', zlib.crc32(tag + body) & 0xFFFFFFFF))
    ctype = 2 if ch == 3 else 6
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, ctype, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(bytes(raw), 6))
            + chunk(b'IEND', b''))


# ---------------------------------------------------------------- 劣化模拟
def blur(w, h, ch, px, rounds=2):
    """3×3 均值模糊：模拟手抖 / 对焦不准。"""
    cur = bytearray(px)
    stride = w * ch
    for _ in range(rounds):
        nxt = bytearray(cur)
        for y in range(1, h - 1):
            for x in range(1, w - 1):
                base = (y * w + x) * ch
                for c in range(ch):
                    s = (cur[base - stride - ch + c] + cur[base - stride + c]
                         + cur[base - stride + ch + c] + cur[base - ch + c]
                         + cur[base + c] + cur[base + ch + c]
                         + cur[base + stride - ch + c] + cur[base + stride + c]
                         + cur[base + stride + ch + c])
                    nxt[base + c] = s // 9
        cur = nxt
    return cur


def scale_light(w, h, ch, px, k):
    """整体乘系数：k<1 压暗，k>1 提亮（截断到 255）。"""
    out = bytearray(len(px))
    for i in range(len(px)):
        v = int(px[i] * k)
        out[i] = 255 if v > 255 else (0 if v < 0 else v)
    return out


def flatten_contrast(w, h, ch, px, k=0.22):
    """对比度压平：所有值往中灰 128 靠。模拟灰蒙蒙的阴天照。"""
    out = bytearray(len(px))
    for i in range(len(px)):
        v = int(128 + (px[i] - 128) * k)
        out[i] = 255 if v > 255 else (0 if v < 0 else v)
    return out


def downscale(w, h, ch, px, target=200):
    """最近邻缩小：模拟图案本身就没拍够像素。"""
    step = max(1, min(w, h) // target)
    nw, nh = max(1, w // step), max(1, h // step)
    out = bytearray(nw * nh * ch)
    for y in range(nh):
        for x in range(nw):
            si = ((y * step) * w + x * step) * ch
            di = (y * nw + x) * ch
            out[di:di + ch] = px[si:si + ch]
    return nw, nh, out


def box_downscale(w, h, ch, px, target=640):
    """均值缩小。标定用：纯 Python 的 3×3 模糊在 2048² 上要跑上百秒，
    先降到 640 再劣化，量级才可接受。用均值而非最近邻，避免凭空抬高清晰度。"""
    step = max(1, min(w, h) // target)
    nw, nh = max(1, w // step), max(1, h // step)
    out = bytearray(nw * nh * ch)
    n = step * step
    for y in range(nh):
        for x in range(nw):
            acc = [0] * ch
            for dy in range(step):
                base = ((y * step + dy) * w + x * step) * ch
                for dx in range(step):
                    for c in range(ch):
                        acc[c] += px[base + dx * ch + c]
            di = (y * nw + x) * ch
            for c in range(ch):
                out[di + c] = acc[c] // n
    return nw, nh, out


def show(label, m):
    print('  %-12s 衰减 %-6.3f 梯度 %-6.2f 亮度 %-6.1f 对比 %-6.1f 纯白 %-5.1f%% 短边 %-5d [%s] → %s'
          % (label, m['blur_ratio'], m['sharpness'], m['brightness'], m['contrast'],
             m['clip'] * 100, min(m['w'], m['h']), m['grid'], m['level']))
    if m.get('issues'):
        print('               └ %s' % '；'.join(m['issues']))


def make_blocks(w=900, h=900):
    """造一张「清晰但图案简单」的图：白底 + 稀疏大圆点。

    专门用来验证新指标不会误伤：这种图细节少、绝对梯度天然低，
    用旧的绝对阈值会被判成"糊"，但人眼一看就知道是清晰的。
    """
    px = bytearray(w * h * 3)
    for y in range(h):
        for x in range(w):
            i = (y * w + x) * 3
            d = (((x % 120) - 60) ** 2 + ((y % 120) - 60) ** 2) ** 0.5
            if d < 26:
                px[i], px[i + 1], px[i + 2] = 40, 70, 150
            elif d < 34:
                px[i], px[i + 1], px[i + 2] = 120, 150, 210
            else:
                px[i] = px[i + 1] = px[i + 2] = 252
    return w, h, 3, px


def calibrate(path):
    real, tmp = as_png(path)
    try:
        w, h, ch, px = decode_png(real)
    finally:
        cleanup(tmp)
    if ch == 4:
        rgb = bytearray(w * h * 3)
        for i in range(w * h):
            rgb[i * 3:i * 3 + 3] = px[i * 4:i * 4 + 3]
        ch, px = 3, rgb

    # 先量未降采样的原图：用来验证「指标与分辨率无关」这条设计前提。
    # 原图与 640 降采样图的数值应该基本一致，否则阈值就不可信。
    m0 = photo_qc.measure(w, h, ch, px); m0.update(photo_qc.judge(m0))
    print('原图 %dx%d' % (w, h))
    show('原图直测', m0)

    w, h, px = box_downscale(w, h, ch, px, target=640)
    m = photo_qc.measure(w, h, ch, px); m.update(photo_qc.judge(m))
    show('基准(清晰)', m)
    print('  —— 下面在同一基准上做各种劣化 ——')

    b = blur(w, h, ch, px, rounds=1)
    m = photo_qc.measure(w, h, ch, b); m.update(photo_qc.judge(m))
    show('轻度模糊', m)

    b = blur(w, h, ch, px, rounds=3)
    m = photo_qc.measure(w, h, ch, b); m.update(photo_qc.judge(m))
    show('重度模糊', m)

    d = scale_light(w, h, ch, px, 0.42)
    m = photo_qc.measure(w, h, ch, d); m.update(photo_qc.judge(m))
    show('压暗', m)

    l = scale_light(w, h, ch, px, 1.7)
    m = photo_qc.measure(w, h, ch, l); m.update(photo_qc.judge(m))
    show('过曝', m)

    f = flatten_contrast(w, h, ch, px)
    m = photo_qc.measure(w, h, ch, f); m.update(photo_qc.judge(m))
    show('灰平低对比', m)

    nw, nh, s = downscale(w, h, ch, px, target=180)
    m = photo_qc.measure(nw, nh, ch, s); m.update(photo_qc.judge(m))
    show('缩到%dpx' % nw, m)

    print('\n  对照：清晰但图案简单的图（绝对梯度低，不该被判糊）')
    bw, bh, bch, bpx = make_blocks()
    m = photo_qc.measure(bw, bh, bch, bpx); m.update(photo_qc.judge(m))
    show('大色块清晰图', m)
    b2 = blur(bw, bh, bch, bpx, rounds=3)
    m = photo_qc.measure(bw, bh, bch, b2); m.update(photo_qc.judge(m))
    show('大色块+模糊', m)

    print('\n  期望：原图 / 大色块清晰图 good（大色块不能被误判成糊，这是关键回归）；'
          '压暗、大色块+模糊 fair；灰平、缩小 poor。')
    print('  注意：重度模糊(0.551)与"大色块清晰"(0.547)数值重合，判不出来，'
          '这是已知取舍——阈值偏向不误伤好图，理由见 core/photo_qc.py。')


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    if args[0] == '--calibrate':
        for p in args[1:]:
            print('=== %s' % os.path.basename(p))
            calibrate(p)
        return 0
    for p in args:
        if not os.path.exists(p):
            print('跳过（不存在）：%s' % p)
            continue
        try:
            m = photo_qc.qc_from_path(p)
        except Exception as e:
            print('%s  体检失败：%s' % (os.path.basename(p), e))
            continue
        print('%s  %dx%d' % (os.path.basename(p), m['w'], m['h']))
        show('指标', m)
        print('    结论：%s' % m['advice'])
    return 0


if __name__ == '__main__':
    sys.exit(main())
