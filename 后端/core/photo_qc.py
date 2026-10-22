"""照片体检 —— 描稿仿稿的前置质量闸门。

为什么需要：
    描稿仿稿的输入是用户随手拍的照片（裙子、布料、样衣），质量参差不齐：
    糊了、太暗、过曝、图案只占一小角、甚至是张截图。这些图丢给生图模型，
    模型不会拒绝，只会闷头脑补出一张看起来还行、但跟原图案毫无关系的图——
    用户拿到才发现不对，钱已经扣了。
    所以出图前先量一下这张照片到底能不能用，不能就直说，别浪费用户点数。

怎么量（都是零依赖的像素统计）：
    先把图做「块均值」降到统一网格，再在网格上量。这一步不能省——
    否则 2048 图量的是隔 8 像素的差、640 图量的是隔 3 像素的差，
    数值根本不可比，同一张好图换个分辨率就会得出相反结论。

    模糊度 blur_ratio —— **判定糊不糊主要看这个，不看绝对梯度。**
                         绝对梯度同时受「图案复杂度」和「模糊程度」两个因素影响：
                         一张清晰但图案简单（大色块）的照片，梯度天然就低，
                         用绝对阈值判会误伤。所以改成量「相对量」——
                         在网格上再模糊一次，看梯度衰减多少：
                         清晰图有大量高频可被破坏，衰减明显；已经糊掉的图
                         没有高频可破坏，再模糊几乎不变。这个比值与图案复杂度无关。
    清晰度 sharpness  —— 网格上的亮度梯度均值。只作为展示与"几乎无结构"的兜底判断。
    亮度   brightness  —— 亮度均值。只用来抓"太暗"。
    对比度 contrast    —— 亮度标准差。低则灰蒙蒙一片，图案和底色分不开。
    高光溢出 clip      —— 接近纯白的像素占比。**只作为展示信息，不参与判定。**
                         原因：白底印花稿天然就有 75% 左右是纯白，真过曝的照片
                         也是一片纯白，两者在像素统计上无法区分（都表现为
                         "255 处堆一个峰、峰内零梯度"）。既然分辨不了，
                         就不要拿它拦人，否则所有白底稿都会被误判成过曝。
    分辨率 resolution  —— 短边像素。太小说明图案区域本身就没多少信息。

阈值怎么来的：
    用清晰的生成花型图与人工模糊/压暗/提亮/压对比/缩小的同图对照标定，
    详见 tools/photo_qc_probe.py 的 --calibrate。
    注意：这是拿花型稿标定的。等有真实用户照片样本后应重新校准一遍。
"""
import os
import tempfile

from core.imgio import as_png, cleanup, decode_png

# 统一网格的目标边长（短边）。所有图都降到这个尺度再量指标，保证可比。
GRID_SIDE = 320

# 判定阈值。标定值（模糊两遍后的梯度衰减，详见 tools/photo_qc_probe.py --calibrate）：
#     清晰细花型 0.333 / 大色块清晰图 0.547 / 轻度模糊 0.441 /
#     重度模糊 0.551 / 大色块+模糊 0.790
#
# 已知局限（重要，别试图用调阈值"修"）：这张表里「大色块清晰图 0.547」和
# 「重度模糊 0.551」是重合的。这不是阈值没调好，而是两种图在像素统计上确实等价——
# 图案本身就简单的图，和糊掉的图，高频含量一样低。要区分二者得知道"原本应该有多少
# 细节"，单张图无法得知。所以这里选择**偏向不误伤**：
# 宁肯放过一张糊图（用户看到结果不满意），也不要把清晰照片说成糊的
# （那会让用户怀疑工具本身）。漏判由确认卡片上的体检数据兜底，让用户自己判断。
BLUR_POOR = 0.90        # 两遍模糊后梯度衰减不到 10% → 几乎没高频了
BLUR_FAIR = 0.72        # 衰减不到 28% → 偏软，细节可能不够
DARK_POOR = 55.0        # 均值亮度低于此值 → 太暗
DARK_FAIR = 82.0
CONTRAST_POOR = 12.0    # 亮度标准差低于此值 → 灰平，图案与底色难分
CONTRAST_FAIR = 20.0
MIN_SIDE_POOR = 320     # 短边小于此值 → 图案区域信息太少
MIN_SIDE_FAIR = 640

# 硬拦线：到这个程度已经不是"还原度打折"，而是"给模型也是瞎猜"。
# 卡在这里的图直接不给确认，别让用户白花点数。
HARD_BLUR = 0.97        # 再模糊都不变 = 高频彻底没了（一片糊或一片纯色）
HARD_SHARP = 1.2        # 绝对梯度极低，且衰减也高 = 完全没有结构
HARD_CONTRAST = 6.0     # 图案和底色完全分不开
HARD_SIDE = 160         # 比缩略图还小

LEVEL_ORDER = {'good': 0, 'fair': 1, 'poor': 2}


def luma_grid(w, h, ch, px, side=GRID_SIDE):
    """块均值降采样 → (nw, nh, [亮度...])。

    用切片求和走 C 层，别逐像素 Python 循环：2048² 逐点算要好几秒。
    """
    step = max(1, min(w, h) // side)
    nw, nh = max(1, w // step), max(1, h // step)
    n = step * step
    grid = [0.0] * (nw * nh)
    stride = w * ch
    for by in range(nh):
        acc = [0.0] * (nw * 3)
        for dy in range(step):
            base = (by * step + dy) * stride
            row = px[base:base + stride]
            for bx in range(nw):
                seg = row[bx * step * ch:(bx + 1) * step * ch]
                if ch >= 3:
                    acc[bx * 3] += sum(seg[0::ch])
                    acc[bx * 3 + 1] += sum(seg[1::ch])
                    acc[bx * 3 + 2] += sum(seg[2::ch])
                else:
                    s = sum(seg)
                    acc[bx * 3] += s
                    acc[bx * 3 + 1] += s
                    acc[bx * 3 + 2] += s
        o = by * nw
        for bx in range(nw):
            r, g, b = acc[bx * 3], acc[bx * 3 + 1], acc[bx * 3 + 2]
            grid[o + bx] = (0.299 * r + 0.587 * g + 0.114 * b) / n
    return nw, nh, grid


def _grid_grad(grid, nw, nh):
    """网格梯度的均方根（4 邻域）。

    这里必须用均方，不能用平均绝对值。原因：
    平均绝对值对「稀疏的阶跃边缘」是模糊不变量——一条硬边模糊后，
    梯度总值没变，只是从 1 个点摊到 3 个点上，再除以网格总数，
    结果几乎一样。于是清晰的大色块图案（边缘少而硬）算出来衰减接近 1.0，
    会被误判成"糊了"。均方对峰值更敏感：摊开之后单点幅值下降，
    均方随之下降，衰减才有意义。
    """
    ss = 0.0
    cnt = 0
    for y in range(nh):
        row = y * nw
        for x in range(nw):
            v = grid[row + x]
            if x + 1 < nw:
                d = v - grid[row + x + 1]
                ss += d * d
                cnt += 1
            if y + 1 < nh:
                d = v - grid[row + nw + x]
                ss += d * d
                cnt += 1
    return ((ss / cnt) ** 0.5) if cnt else 0.0


def _blur_grid(grid, nw, nh):
    """网格上做一次 3×3 均值模糊（边界夹取）。

    用分离式（先横向再纵向）而不是直接取 9 邻域：
    320² 网格下后者要多跑近一倍的 Python 循环。
    """
    tmp = [0.0] * (nw * nh)
    for y in range(nh):
        row = y * nw
        for x in range(nw):
            a = grid[row + (x - 1 if x > 0 else 0)]
            b = grid[row + x]
            c = grid[row + (x + 1 if x + 1 < nw else nw - 1)]
            tmp[row + x] = (a + b + c) / 3.0
    out = [0.0] * (nw * nh)
    for y in range(nh):
        up = (y - 1 if y > 0 else 0) * nw
        mid = y * nw
        dn = (y + 1 if y + 1 < nh else nh - 1) * nw
        for x in range(nw):
            out[mid + x] = (tmp[up + x] + tmp[mid + x] + tmp[dn + x]) / 3.0
    return out


def measure(w, h, ch, px):
    """对像素做统计。返回原始指标，不做判定，方便标定阈值。"""
    nw, nh, grid = luma_grid(w, h, ch, px)
    n = nw * nh
    if not n:
        raise ValueError('图片没有可用像素')

    total = sum(grid)
    mean = total / n
    var = max(0.0, sum(v * v for v in grid) / n - mean * mean)

    # 模糊两遍（等效 5×5）再看衰减：只模糊一遍的话，清晰图与重度模糊图
    # 的衰减挤在 0.45~0.85 这一段，阈值没法取。模糊两遍能把差距拉开。
    g0 = _grid_grad(grid, nw, nh)
    blurred = _blur_grid(_blur_grid(grid, nw, nh), nw, nh)
    g1 = _grid_grad(blurred, nw, nh)
    ratio = (g1 / g0) if g0 > 0.05 else 1.0

    clipped = sum(1 for v in grid if v >= 250.0)

    return {
        'w': w, 'h': h,
        'sharpness': round(g0, 2),
        'blur_ratio': round(ratio, 3),
        'brightness': round(mean, 1),
        'contrast': round(var ** 0.5, 1),
        'clip': round(clipped / n, 3),
        'grid': '%dx%d' % (nw, nh),
    }


def judge(m):
    """把指标翻成人话：能不能用、哪里不行、该怎么改。"""
    issues = []
    level = 'good'

    def bump(lv):
        nonlocal level
        if LEVEL_ORDER[lv] > LEVEL_ORDER[level]:
            level = lv

    short_side = min(m['w'], m['h'])

    if m['blur_ratio'] >= BLUR_POOR:
        issues.append('照片糊了（清晰度衰减 %.2f），花纹的边缘和细节已经丢失'
                      % m['blur_ratio'])
        bump('poor')
    elif m['blur_ratio'] >= BLUR_FAIR:
        issues.append('照片偏软（清晰度衰减 %.2f），小碎花这类细节可能还原不准'
                      % m['blur_ratio'])
        bump('fair')

    if m['brightness'] < DARK_POOR:
        issues.append('照片太暗（亮度 %.0f），暗部细节已经压没了' % m['brightness'])
        bump('poor')
    elif m['brightness'] < DARK_FAIR:
        issues.append('照片偏暗（亮度 %.0f），花纹颜色会比实物深' % m['brightness'])
        bump('fair')

    # 过曝不参与判定：白底稿与过曝照片在统计上无法区分，见模块 docstring。

    if m['contrast'] < CONTRAST_POOR:
        issues.append('画面对比度低（%.1f），图案和底色贴在一起分不开' % m['contrast'])
        bump('poor')
    elif m['contrast'] < CONTRAST_FAIR:
        issues.append('画面对比度偏低（%.1f），浅色细节可能还原不准' % m['contrast'])
        bump('fair')

    if short_side < MIN_SIDE_POOR:
        issues.append('图太小（短边 %d 像素），花纹本身就没剩多少信息' % short_side)
        bump('poor')
    elif short_side < MIN_SIDE_FAIR:
        issues.append('分辨率一般（短边 %d 像素），建议用原图再试' % short_side)
        bump('fair')

    if level == 'good':
        advice = '照片质量可以，直接描稿就行。'
    elif level == 'fair':
        advice = '照片基本能用，但还原度会打折。条件允许的话，换个光线均匀、离近一点的角度重拍，效果更准。'
    else:
        advice = '这张照片不太适合直接描稿。建议重新拍：光线均匀（别背光）、正对图案、拍清楚、让图案占满画面；或者直接文字描述你要的风格，让花型师重新设计一款。'

    # usable 不是"质量好"，而是"还能不能描"。质量差但结构还在的图，交给用户自己决定；
    # 已经糊成一片 / 一片纯色的，描了也是编，直接拦。
    hopeless = (m['blur_ratio'] >= HARD_BLUR or m['contrast'] < HARD_CONTRAST
                or short_side < HARD_SIDE)

    return {
        'level': level,
        'usable': not hopeless,
        'issues': issues,
        'advice': advice,
    }


def qc_from_path(path):
    """体检一张图（PNG/JPEG 都行）。解码失败会抛 ValueError。"""
    real, tmp = as_png(path)
    try:
        w, h, ch, px = decode_png(real)
        res = measure(w, h, ch, px)
    finally:
        cleanup(tmp)
    res.update(judge(res))
    return res


def qc_from_bytes(blob, suffix='.jpg'):
    """体检内存里的图（上传的图是 base64，解出来是 bytes）。"""
    fd, path = tempfile.mkstemp(prefix='qc_', suffix=suffix)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(blob)
        return qc_from_path(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
