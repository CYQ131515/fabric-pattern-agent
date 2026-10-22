"""图片准备：读图 → 纠方向 → 缩到模型能看清又不太慢的尺寸 → base64。

两个尺寸上的取舍，都是试出来的：

**为什么要缩**     库里原图 2048×2048、PNG 能有 3MB，base64 之后 4MB。
                   传给模型不但慢，很多服务还有请求体上限。
                   VLM 的视觉编码器本来也会把图切成固定大小的 patch，
                   给 2048 和给 1280 进去，它"看到"的细节差别很小。

**为什么不能缩太小** 花型识别恰恰吃细节：一条枝叶、一个波点、一根细条纹
                   缩没了，标签就从"叶片藤蔓"变成"抽象笔触"。
                   1280 是试下来"细节还在、传输不肉"的位置。
                   要跟主程序的出图尺寸对齐时，用 --max-side 调。

另外：**PNG 优先，太大才转 JPEG**。花型图对压缩伪影敏感，伪影会让模型
把平整色块看成肌理、把边缘看成笔触。所以只有在 PNG 大到离谱时才降级，
而且质量给到 90。
"""
import base64
import hashlib
import io
import os

from PIL import Image, ImageOps

# 给模型的默认最长边
DEFAULT_MAX_SIDE = 1280

# PNG 超过这个大小（字节）就转 JPEG。本地传大图其实无所谓，
# 但云端端点会明显变慢，统一一个上限省心。
PNG_KEEP_LIMIT = 2_500_000
JPEG_QUALITY = 90

# 防解压炸弹。主程序上传口限制长边 2048，这里给到 8000 的余量。
Image.MAX_IMAGE_PIXELS = 8000 * 8000

# Pillow 认得的格式 → mime。认不出的按 PNG 处理。
MIME = {
    'PNG': 'image/png',
    'JPEG': 'image/jpeg',
    'WEBP': 'image/webp',
    'BMP': 'image/bmp',
    'GIF': 'image/gif',
    'TIFF': 'image/tiff',
}


class ImageError(Exception):
    pass


def md5_file(path, chunk=1 << 20):
    """按**内容**算指纹。判重、断点续跑都靠它 —— 不靠文件名，
    因为客户重命名的图太常见了。"""
    h = hashlib.md5()
    with open(path, 'rb') as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def md5_bytes(blob):
    return hashlib.md5(blob).hexdigest()


def open_image(path):
    """打开并纠正方向。手机翻拍的图带 EXIF 旋转，不纠的话模型看到的是躺着的。

    RGBA/P 模式统一压到白底 RGB —— 花型图常有透明底，直接转 RGB 会让
    透明区变黑，模型就会把"黑底"当成配色的一部分。
    """
    try:
        im = Image.open(path)
        im.load()
    except Exception as e:                                # noqa: BLE001
        raise ImageError('这张图读不出来（%s）。' % type(e).__name__)
    try:
        im = ImageOps.exif_transpose(im)
    except Exception:                                     # noqa: BLE001
        pass
    if im.mode in ('RGBA', 'LA', 'P'):
        im = im.convert('RGBA')
        bg = Image.new('RGB', im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        im = bg
    elif im.mode != 'RGB':
        im = im.convert('RGB')
    return im


def fit(im, max_side=DEFAULT_MAX_SIDE):
    """等比缩到最长边不超过 max_side。小图不放大 —— 放大只会插值出假细节。"""
    w, h = im.size
    m = max(w, h)
    if m <= max_side:
        return im, False
    scale = max_side / float(m)
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    return im.resize((nw, nh), Image.LANCZOS), True


def encode(im, prefer_png=True):
    """编码成 (base64 字符串, mime)。"""
    buf = io.BytesIO()
    if prefer_png:
        im.save(buf, format='PNG', optimize=True)
        if buf.tell() <= PNG_KEEP_LIMIT:
            return base64.b64encode(buf.getvalue()).decode('ascii'), 'image/png'
        # 太大了，降级 JPEG
        buf = io.BytesIO()
    im.save(buf, format='JPEG', quality=JPEG_QUALITY, optimize=True)
    return base64.b64encode(buf.getvalue()).decode('ascii'), 'image/jpeg'


def load_for_model(path, max_side=DEFAULT_MAX_SIDE):
    """一步到位：读 → 纠向 → 缩 → 编码。返回 (b64, mime)。"""
    if not os.path.isfile(path):
        raise ImageError('找不到文件：%s' % path)
    im = open_image(path)
    im, _ = fit(im, max_side)
    return encode(im)


def probe(path):
    """读一张图的元信息。探测/建档用，不做缩放。"""
    im = Image.open(path)
    try:
        fmt = im.format or 'PNG'
        w, h = im.size
    finally:
        im.close()
    return {'w': int(w), 'h': int(h), 'format': fmt,
            'mime': MIME.get(fmt, 'image/png'),
            'bytes': os.path.getsize(path)}


def sniff(blob):
    """从字节头判断格式，给没有扩展名/扩展名不对的图兜底。"""
    if not blob:
        return 'image/png'
    if blob[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png'
    if blob[:3] == b'\xff\xd8\xff':
        return 'image/jpeg'
    if blob[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif'
    if blob[:4] == b'RIFF' and blob[8:12] == b'WEBP':
        return 'image/webp'
    if blob[:2] in (b'BM',):
        return 'image/bmp'
    return 'image/png'


# 收图时按扩展名筛。用扩展名而不是"试着打开"，因为批量场景下
# 目录里混着 .txt/.psd 的概率很高，逐个开图太慢。
IMAGE_EXT = ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif', '.tif', '.tiff')


def is_image(path):
    return os.path.splitext(path)[1].lower() in IMAGE_EXT
