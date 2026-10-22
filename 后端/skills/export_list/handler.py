"""花型清单导出：把「用户选中的花型」整理成一张真表格（xlsx），**带小图**。

**它不在任何角色的技能清单里。** 对话链路不给模型这件工具 ——
模型看不见就不会自己编参数、也不用想法子把"一个文件"塞回回复里。
由 runtime 在识别到「导出花型清单」意图时**确定性执行**，
结果直接挂在回复下方当下载按钮（见 core/runtime.py 的 _export_intent）。

数据来源分两步：
    1. 用户选中的是哪些款 —— 会话里的 `sess.selected`（前端每次对话都会
       把"我的花型清单"同步过来，见 core/runtime.py chat 的 selected 参数）
    2. 每款长什么样 —— 先查本地花型库（真图，带四维标签和设计说明），
       查不到再查内置款（data/patterns.json，带设计说明和卖点）

图片从哪来（见 _image_for）：
    · 库里的款  → 用库自己的缩略图（thumb/，本来就只有 480px），没有再退回原图
    · 内置款    → 用预渲染的 `data/pattern-thumbs/<编号>.png`
      （内置款是程序画的矢量，后端手里没有位图；那份缩略图由
       测试/gen_pattern_thumbs.mjs 借前端 patternSVG() 画出来落盘，见那个脚本）
    · 都没有    → 这一格留空，**不报错也不编一张图**

进表前一律用 sips 压成小图（长边 128px、JPEG 质量 68）：客户要的是看得清的缩略图，
不是每张几百 KB 的原图 —— 二十款的原图能把附件撑到十几 MB，邮件直接发不出去。

表格用 core/xlsx.py 手写的 xlsx 生成（后端零第三方依赖，见那个模块的说明）。
"""

import os
import shutil
import tempfile
import time

# 表头固定成客户/打样单能直接用的字段；顺序就是导出的列序。
# 「图」放第一列：这是一份给人看的清单，先看到花型再看到字。
COLS = ('图', '序号', '花型编号', '花型名称', '风格', '元素', '配色', '场景',
        '设计说明', '卖点', '适用面料')
WIDTHS = (20, 6, 22, 16, 12, 12, 14, 14, 52, 46, 22)

# 小图规格：长边 128px 印在表格里够看清花型结构，二十张也就一百多 KB。
THUMB_BOX = 128
THUMB_QUALITY = 68


def _clean(v):
    return ('' if v is None else str(v)).strip()


def _backend_dir():
    """后端/ 目录 —— 从本文件往上走三层（skills/export_list/handler.py）。"""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _builtin_thumb(pid):
    p = os.path.join(_backend_dir(), 'data', 'pattern-thumbs', pid + '.png')
    return p if os.path.exists(p) else None


def _from_library(ctx, pid):
    """先从本地花型库找真图。库里没有再看内置款。"""
    lib = ctx.get('library')
    if lib is None:
        return None
    try:
        it = lib.get(pid)
    except Exception:                                  # noqa: BLE001
        return None
    if not it:
        return None
    img = None
    try:
        # 缩略图优先：列表页要的就是小的，没必要搬 480px 的原图出来压
        img = lib.path_of(it, True)
    except Exception:                                  # noqa: BLE001
        img = None
    return {
        'id': it.get('id') or pid,
        'name': it.get('name') or pid,
        'style': it.get('style'), 'element': it.get('element'),
        'color': it.get('color'), 'scene': it.get('scene'),
        'desc': it.get('desc'), 'sell': [], 'fabric': it.get('fabric'),
        '_img': img,
    }


def _from_builtin(ctx, pid):
    for p in (ctx.get('patterns') or []):
        if str(p.get('id')) == pid:
            return {
                'id': p.get('id') or pid,
                'name': p.get('name') or pid,
                'style': p.get('style'), 'element': p.get('element'),
                'color': p.get('color'), 'scene': p.get('scene'),
                'desc': p.get('desc'), 'sell': p.get('sell') or [],
                'fabric': p.get('fabric'),
                '_img': _builtin_thumb(pid),
            }
    return None


def _image_for(item):
    """这款的图在哪。库里那张不顶用就退回内置款的预渲染图，都没有就是 None。"""
    p = item.get('_img')
    if p and os.path.exists(p):
        return p
    return _builtin_thumb(_clean(item.get('id')))


def collect(ctx, ids):
    """按编号取资料。返回 (找到的, 没找到的编号)。"""
    got, missing = [], []
    for pid in ids:
        pid = _clean(pid)
        if not pid or any(g['id'] == pid for g in got):
            continue
        item = _from_library(ctx, pid) or _from_builtin(ctx, pid)
        if item:
            got.append(item)
        else:
            missing.append(pid)
    return got, missing


def _public(it):
    """给前端的字段。`_` 开头的都是内部用的（比如图片在本机的路径），不外传。"""
    return dict((k, v) for k, v in it.items() if not k.startswith('_'))


def _row(i, it, include_desc, include_sell):
    sell = it.get('sell') or []
    return [
        '',                                # 图列：图片是画在格子上层的，单元格本身留空
        i,
        _clean(it.get('id')),
        _clean(it.get('name')),
        _clean(it.get('style')),
        _clean(it.get('element')),
        _clean(it.get('color')),
        _clean(it.get('scene')),
        _clean(it.get('desc')) if include_desc else '',
        '；'.join(_clean(s) for s in sell if s) if include_sell else '',
        _clean(it.get('fabric')),
    ]


def _unique_path(path):
    """绝不覆盖已有文件（同一秒里连点两次导出会撞名）。"""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    for n in range(2, 100):
        q = '%s-%d%s' % (stem, n, ext)
        if not os.path.exists(q):
            return q
    return path


def _shrink(src):
    """把一张图压成小图，返回 (bytes, 'png'|'jpg', w, h)。做不出来返回 None。

    复用花型库那套 make_thumb（macOS 自带 sips）。它按源图有没有透明通道决定
    出 png 还是 jpg，所以输出路径**故意不带扩展名** —— 先用魔法字节认格式，
    再告诉 xlsx 写什么扩展名。带个骗人的 .jpg 后缀、里面其实是 PNG，
    Excel 那边就是内容与声明对不上。
    """
    try:
        from core.library import image_size, make_thumb
    except Exception:                                  # noqa: BLE001
        return None
    tmp = tempfile.mkdtemp(prefix='pf_listimg_')
    try:
        dst = os.path.join(tmp, 'thumb')
        if not make_thumb(src, dst, THUMB_BOX, THUMB_QUALITY):
            return None
        if not os.path.exists(dst):
            return None
        with open(dst, 'rb') as f:
            data = f.read()
        if not data:
            return None
        if data[:8] == b'\x89PNG\r\n\x1a\n':
            ext = 'png'
        elif data[:3] == b'\xff\xd8\xff':
            ext = 'jpg'
        else:
            return None                                # 认不出的格式就别往表里塞
        w, h = image_size(dst)
        if w <= 0 or h <= 0:
            w = h = THUMB_BOX
        return data, ext, w, h
    except Exception:                                  # noqa: BLE001
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _images_for(items):
    """给每款配一张小图。返回 (images, 有几款没配到图)。

    图片钉在「图」列（第 0 列）；第 0 行是表头，所以第 i 款的数据在第 i+1 行。
    """
    images, noimg = [], 0
    for i, it in enumerate(items):
        src = _image_for(it)
        got = _shrink(src) if src else None
        if not got:
            noimg += 1
            continue
        data, ext, w, h = got
        images.append({'data': data, 'ext': ext, 'row': i + 1, 'col': 0, 'w': w, 'h': h})
    return images, noimg


def run(args, ctx):
    args = args or {}
    ids = args.get('ids')
    sess = ctx.get('session')
    if not ids:
        ids = list(getattr(sess, 'selected', None) or [])

    include_desc = args.get('include_desc', True)
    include_sell = args.get('include_sell', True)

    if not ids:
        # 「还没选过花型」不是错误，是实话：给一张空表不如直接说清楚
        return {
            'ok': True, 'count': 0, 'items': [], 'downloads': [],
            'empty': True,
            'note': '你还没在花型选择里确认过花型，所以这边没有清单可以整理。'
                    '先去花型设计师那边选几款，我马上给你出一张表。',
        }

    items, missing = collect(ctx, ids)
    if not items:
        return {
            'ok': True, 'count': 0, 'items': [], 'downloads': [],
            'empty': True, 'missing': missing,
            'note': '你之前选的那几款（%s）在库里已经找不到了，'
                    '重新挑几款我再给你出表。' % '、'.join(missing[:5]),
        }

    rows = [list(COLS)]
    for i, it in enumerate(items, start=1):
        rows.append(_row(i, it, include_desc, include_sell))

    gen = ctx.get('imagegen')
    if gen is None or not getattr(gen, 'outdir', None):
        return {'ok': False, 'error': '这个服务没启用出图目录，表格文件存不下来'}

    try:
        import core.xlsx as xlsx
    except Exception as e:                             # noqa: BLE001
        return {'ok': False, 'error': '表格生成器没装上：%s' % e}

    images, noimg = _images_for(items)

    try:
        name = 'pattern-list-%s.xlsx' % time.strftime('%Y%m%d-%H%M%S')
        path = _unique_path(os.path.join(gen.outdir, name))
        xlsx.write(path, rows, sheet_name='花型清单', widths=list(WIDTHS),
                   images=images)
        size = os.path.getsize(path)
    except Exception as e:                             # noqa: BLE001
        return {'ok': False, 'error': '表格没生成出来（%s: %s）'
                                      % (type(e).__name__, e)}

    fname = os.path.basename(path)
    note = '已按你选中的 %d 款生成清单表格。' % len(items)
    if images:
        note += '每款都附了缩略图。'
    if noimg:
        # 有图没配上是实话，得说；但别把整件事说成失败 —— 表是好的
        note += '其中 %d 款这边没有图片，那几格的图留空了。' % noimg
    return {
        'ok': True,
        'count': len(items),
        'items': [_public(it) for it in items],
        'missing': missing,
        'images': len(images),
        'downloads': [{'name': fname, 'url': '/api/file/' + fname,
                       'bytes': size, 'label': '花型清单.xlsx'}],
        'note': note,
    }
