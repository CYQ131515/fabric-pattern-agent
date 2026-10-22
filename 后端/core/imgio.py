"""最小图片读取 —— 零第三方依赖，供花型算法（接缝检测、照片体检）共用。

为什么自己写：
    生图接口返回的是 JPEG，而纯标准库读不了 JPEG。macOS 自带 `sips` 能转，
    所以策略是「JPEG 先转 PNG，再用自带的 PNG 解码器读」，全程不装任何包。

限制：只支持 8bit、RGB/RGBA、非隔行的 PNG —— 主流生图输出都在这个范围内。
"""
import os
import shutil
import struct
import subprocess
import tempfile
import zlib


def decode_png(path):
    """返回 (width, height, channels, bytearray 像素数据)。"""
    with open(path, 'rb') as f:
        data = f.read()
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('不是 PNG 文件：%s' % path)
    pos, idat, meta = 8, bytearray(), None
    while pos < len(data):
        length = struct.unpack('>I', data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if tag == b'IHDR':
            w, h, depth, ctype, comp, filt, inter = struct.unpack('>IIBBBBB', body)
            if depth != 8 or inter != 0 or ctype not in (2, 6):
                raise ValueError('暂不支持的 PNG 格式：depth=%s type=%s 隔行=%s'
                                 % (depth, ctype, inter))
            meta = (w, h, 3 if ctype == 2 else 4)
        elif tag == b'IDAT':
            idat += body
        elif tag == b'IEND':
            break
        pos += 12 + length
    if not meta:
        raise ValueError('PNG 缺少 IHDR：%s' % path)
    w, h, ch = meta
    raw = zlib.decompress(bytes(idat))
    stride = w * ch
    out = bytearray(h * stride)
    prev = bytearray(stride)
    p = 0
    for y in range(h):
        ft = raw[p]; p += 1
        line = bytearray(raw[p:p + stride]); p += stride
        if ft == 1:
            for i in range(ch, stride):
                line[i] = (line[i] + line[i - ch]) & 0xFF
        elif ft == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ft == 3:
            for i in range(stride):
                a = line[i - ch] if i >= ch else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ft == 4:
            for i in range(stride):
                a = line[i - ch] if i >= ch else 0
                b = prev[i]
                c = prev[i - ch] if i >= ch else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        elif ft != 0:
            raise ValueError('未知 filter 类型 %s（第 %d 行）' % (ft, y))
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return w, h, ch, out


def as_png(path):
    """非 PNG 就先用 sips 转成 PNG 再解码。返回 (真实路径, 临时目录)。"""
    with open(path, 'rb') as f:
        head = f.read(12)
    if head[:8] == b'\x89PNG\r\n\x1a\n':
        return path, None
    if not shutil.which('sips'):
        raise ValueError('只支持 PNG，或需要 macOS 的 sips 来转换：%s' % path)
    tmp = tempfile.mkdtemp(prefix='imgio_')
    out = os.path.join(tmp, 'x.png')
    r = subprocess.run(['sips', '-s', 'format', 'png', path, '--out', out],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out):
        shutil.rmtree(tmp, ignore_errors=True)
        raise ValueError('sips 转 PNG 失败：%s' % (r.stderr or '').strip()[:200])
    return out, tmp


def cleanup(tmp):
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)


def read_pixels(path, max_side=1024):
    """一步到位：读任意常见格式 → (w, h, ch, pixels)。

    max_side 只用于提示调用方图片是否过大，不做缩放（缩放要重采样，成本高）。
    """
    real, tmp = as_png(path)
    try:
        return decode_png(real)
    finally:
        cleanup(tmp)
