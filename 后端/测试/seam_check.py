"""四方连续「无缝度」检测 —— 织印师机器人的验收工具。

定位（重要）：
    这是【织印师】阶段的验收项，不是生图阶段的验收项。
    生图（Seedream 等扩散模型）做不到四方连续，提示词里写 seamless 实测无效，
    所以出图后接缝比值高是【正常现象】，不代表生图失败。
    只有经过织印师的「周期偏移 + 补缝」处理之后，才该拿这个工具来判分。

为什么要有这个工具：
    花型上印花机前必须确认平铺不露缝，这条不能靠肉眼，必须量化。
    做法是把图自己跟自己拼，看接缝处的像素跳变有多大。

怎么做：
    水平对接误差 = 平均 |最左列 - 最右列|   （四方连续里，右边缘要接左边缘）
    垂直对接误差 = 平均 |最上行 - 最下行|
    对照基线     = 图内【相邻】列/行的平均差异（如第 k 列与第 k+1 列）
                   —— 这才是"本来就该连续"的参照。
                   注意别用「最左列 vs 图中央列」当对照：中央列本来就不该跟边缘接得上，
                   那么算出来的比值会虚低（真图只有 1.12），造成假阳性。

怎么读数（看比值，不是看绝对值）：
    < 1.6  接缝基本看不出来，可直接平铺
    < 3.0  有轻微痕迹，视花型疏密可能可接受
    >= 3.0 明显断层，需重做补缝
    标定参考：正弦波连续图 ≈ 1.00，完全不连续的图 ≈ 14.3。

零第三方依赖：解码器在 core/imgio.py（8bit、RGB/RGBA、非隔行，覆盖主流生图输出）。
跑法：python3 tools/seam_check.py <图片路径> [...] [--preview=输出.png]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.imgio import as_png, cleanup, decode_png  # noqa: E402


# ---------------------------------------------------------------- 度量
def _col_mean_abs(px, ch, w, h, xa, xb):
    """整列 xa 与整列 xb 之间的平均绝对差（只比 RGB 前三通道）。"""
    total = 0
    for y in range(h):
        ia, ib = (y * w + xa) * ch, (y * w + xb) * ch
        for c in range(min(3, ch)):
            total += abs(px[ia + c] - px[ib + c])
    return total / (h * min(3, ch))


def _row_mean_abs(px, ch, w, ya, yb):
    total = 0
    for x in range(w):
        ia, ib = (ya * w + x) * ch, (yb * w + x) * ch
        for c in range(min(3, ch)):
            total += abs(px[ia + c] - px[ib + c])
    return total / (w * min(3, ch))


def _baseline(px, ch, w, h, step=4):
    """图内相邻列/行的自然跳变基线。

    这才是正确的参照系：四方连续里，最右列接最左列，本质上就是「相邻两列」。
    所以判断标准应该是——接缝跳变是不是跟图内普通的相邻跳变差不多大。
    (拿「最左列 vs 图中央列」当对照是错的：那本来就不该连续，会把所有图都判成合格。)
    """
    hb = [_col_mean_abs(px, ch, w, h, x, x + 1)
          for x in range(0, w - 1, max(1, step))]
    vb = [_row_mean_abs(px, ch, w, y, y + 1)
          for y in range(0, h - 1, max(1, step))]
    return (sum(hb) / len(hb) if hb else 0.0,
            sum(vb) / len(vb) if vb else 0.0)


def seam_score(path):
    real, tmp = as_png(path)
    try:
        w, h, ch, px = decode_png(real)
    finally:
        cleanup(tmp)
    hor = _col_mean_abs(px, ch, w, h, 0, w - 1)      # 最左列 接 最右列
    ver = _row_mean_abs(px, ch, w, 0, h - 1)         # 最上行 接 最下行
    base_h, base_v = _baseline(px, ch, w, h)
    ratio_h = hor / base_h if base_h > 1e-6 else 0.0
    ratio_v = ver / base_v if base_v > 1e-6 else 0.0
    ratio = max(ratio_h, ratio_v)
    worst = max(hor, ver)
    verdict = ('可直接平铺' if ratio < 1.6 else
               '有轻微接缝痕迹，可接受' if ratio < 3.0 else
               '明显断层，平铺会露馅 —— 生图阶段的图基本都在这个区间，'
               '属正常现象，交给织印师做周期偏移补缝即可')
    return {"w": w, "h": h, "horizontal": round(hor, 2), "vertical": round(ver, 2),
            "worst": round(worst, 2),
            "base_h": round(base_h, 2), "base_v": round(base_v, 2),
            "ratio_h": round(ratio_h, 2), "ratio_v": round(ratio_v, 2),
            "ratio": round(ratio, 2), "verdict": verdict}


def tile_preview(path, out, cell=420):
    """输出 2×2 平铺预览：把图自己拼 4 份，接缝处画红线。

    数字只能说明"断层了"，这张图能让人一眼看见断在哪。
    """
    real, tmp = as_png(path)
    try:
        w, h, ch, px = decode_png(real)
    finally:
        pass
    step = max(1, w // cell)
    cw, chh = w // step, h // step
    W, H = cw * 2, chh * 2
    def sample(x, y):
        ia = ((y % h) * w + (x % w)) * ch
        return px[ia], px[ia + 1], px[ia + 2]
    rows = bytearray()
    for Y in range(H):
        rows.append(0)
        for X in range(W):
            # 接缝标红：横竖各 2 像素，落在 2×2 的十字线上
            if abs(X - cw) < 2 or abs(Y - chh) < 2:
                rows += bytes((226, 60, 90))
            else:
                rows += bytes(sample((X % cw) * step, (Y % chh) * step))
    def chunk(tag, data):
        return (struct.pack('>I', len(data)) + tag + data
                + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff))
    png = (b'\x89PNG\r\n\x1a\n'
           + chunk(b'IHDR', struct.pack('>IIBBBBB', W, H, 8, 2, 0, 0, 0))
           + chunk(b'IDAT', zlib.compress(bytes(rows), 6))
           + chunk(b'IEND', b''))
    with open(out, 'wb') as f:
        f.write(png)
    cleanup(tmp)
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    preview = next((a.split('=', 1)[1] for a in sys.argv[1:]
                    if a.startswith('--preview=')), None)
    if not args:
        print(__doc__)
        print('用法：python3 tools/seam_check.py <图片> [...] [--preview=输出.png]')
        return 1
    for p in args:
        if not os.path.exists(p):
            print('❌ 找不到文件：%s' % p)
            continue
        try:
            r = seam_score(p)
        except Exception as e:
            print('❌ %s → %s：%s' % (os.path.basename(p), type(e).__name__, e))
            continue
        print('%s  %dx%d' % (os.path.basename(p), r['w'], r['h']))
        print('  水平接缝 %5.1f（内部相邻基线 %4.1f → 比值 %.2f）'
              % (r['horizontal'], r['base_h'], r['ratio_h']))
        print('  垂直接缝 %5.1f（内部相邻基线 %4.1f → 比值 %.2f）'
              % (r['vertical'], r['base_v'], r['ratio_v']))
        print('  判定：%s' % r['verdict'])
        if preview:
            dst = preview if len(args) == 1 else (
                os.path.splitext(preview)[0] + '_' + os.path.splitext(
                    os.path.basename(p))[0] + os.path.splitext(preview)[1])
            print('  平铺预览：%s' % tile_preview(p, dst))
    return 0


if __name__ == '__main__':
    sys.exit(main())
