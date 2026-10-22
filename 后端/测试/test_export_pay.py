#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""花型清单导出 + 客服充值/趋势改造（服务端，零网络、不花钱）。

覆盖：
    [1] core/xlsx：手写 xlsx 的结构（zip 部件齐、XML 可解析）、内容往返、
        转义（中文 / 引号 / 换行 / 控制字符）、列字母进位
    [2] export_list 技能：没选过花型照实说、有选中出真文件、库里真图优先、
        编号查不到、关掉设计说明列
    [3] 会话记「用户选中」：去重保序、覆盖式、重启后能恢复
    [4] 客服对话里说「导出清单」→ 表格真的生成好了 + 回复自带下载地址
    [5] 只有客服走这条确定性导出；花型师说同一句话不该被拦下来
    [6] 客服从没选过花型时，话是"先去找设计师选款"，不是"查不到、转人工"
    [7] 充值意图 → 系统提示里带"卡片已挂出"，模型不会再讲"转人工"那套
    [8] 界面按钮那条路（runtime.export_list）跟对话那条路产物一致

为什么值得单测：用户提的两个毛病都是**行为**层面的 ——
「充值怎么又让我转人工」「导出怎么反问我项目名还说查不到」。
光把文案改了不够，得钉住"真有一份文件生成出来、真有一个下载地址回给前端"。

跑法：python3 测试/test_export_pay.py
"""
import io
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.dirname(HERE)
sys.path.insert(0, AGENT)

# ---- 隔离：出图产物 / 图库 / 埋点全指去临时目录 ----
OUTDIR = tempfile.mkdtemp(prefix='pf-test-eximg-')
os.environ['PATTERNFLOW_IMAGE_PROVIDER'] = 'mock'
os.environ['PATTERNFLOW_IMAGE_OUTDIR'] = OUTDIR
os.environ['PATTERNFLOW_LIBRARY_DIR'] = tempfile.mkdtemp(prefix='pf-test-exlib-')
# 埋点也隔离：本文件建真 App、跑真链路（test_telemetry 的元测试会扫这一条）
os.environ['PATTERNFLOW_TELEMETRY_DIR'] = tempfile.mkdtemp(prefix='pf-test-extel-')

import core.xlsx as xlsx                                       # noqa: E402
from core.app import App                                       # noqa: E402
from core.session import Session, SessionStore                 # noqa: E402
from core.skills import SkillRegistry                          # noqa: E402

PASS, FAIL = 0, 0


def ok(cond, name, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('  OK   %s' % name)
    else:
        FAIL += 1
        print('  FAIL %s%s' % (name, (' → ' + str(extra)) if extra else ''))


def ok_eq(got, want, name):
    ok(got == want, name, '得到 %r，期望 %r' % (got, want))


def sheet_texts(blob):
    """把 sheet1.xml 的单元格值抠出来（内联字符串或数字），按行返回。

    数字单元格是 <v>、文本是 <t>，两个都要认 —— 只抓 <t> 的话
    "序号"列会整列消失，后面按下标取列的断言就全错位了。
    """
    z = zipfile.ZipFile(io.BytesIO(blob))
    xml = z.read('xl/worksheets/sheet1.xml').decode('utf-8')
    out = []
    # re.S：单元格文本里可能有换行（设计说明是整句话），不然行会漏
    for row in re.findall(r'<row[^>]*>(.*?)</row>', xml, re.S):
        cells = []
        for c in re.findall(r'<c\b[^>]*>(.*?)</c>', row, re.S):
            m = re.search(r'<t[^>]*>(.*?)</t>', c, re.S)
            if m is None:
                m = re.search(r'<v>(.*?)</v>', c, re.S)
            cells.append(m.group(1) if m else '')
        out.append(cells)
    return out


def _synthetic_png(w, h, rgb):
    """现场造一张纯色 PNG（零依赖，只为验证 xlsx 把图片字节原样搬进去）。"""
    import struct
    import zlib
    raw = b''.join(b'\x00' + bytes(rgb) * w for _ in range(h))

    def chunk(tag, body):
        c = tag + body
        return struct.pack('>I', len(body)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)

    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw))
            + chunk(b'IEND', b''))


def media_info(blob):
    """一份 xlsx 里带了几张图、分别叫什么 → (张数, [部件名])。"""
    z = zipfile.ZipFile(io.BytesIO(blob))
    names = [n for n in z.namelist() if n.startswith('xl/media/')]
    return len(names), sorted(names)


def draw_ext(blob):
    """从 drawing1.xml 里抠出每张图的**显示尺寸**（像素），按顺序返回。

    注意别拿 media 里那张图的原生尺寸当显示尺寸 —— 一个是图片本身的像素，
    一个是它在表里占多大，压过图的这两者本来就不一样。这里要验的是后者。
    """
    z = zipfile.ZipFile(io.BytesIO(blob))
    xml = z.read('xl/drawings/drawing1.xml').decode('utf-8')
    return [(int(cx) // 9525, int(cy) // 9525)
            for cx, cy in re.findall(r'<xdr:ext cx="(\d+)" cy="(\d+)"/>', xml)]


class FakeImageGen:
    """只要一个 outdir，技能就是往里写文件。"""

    def __init__(self, outdir):
        self.outdir = outdir


class FakeLibrary:
    def __init__(self, items):
        self._items = items

    def get(self, pid):
        for it in self._items:
            if it.get('id') == pid:
                return it
        return None


PATTERNS = [
    {'id': 'PF-OWN-2026-000128', 'name': '山茶小径', 'style': '法式浪漫',
     'element': '花卉', 'color': '莫兰迪灰调', 'scene': '女装连衣裙',
     'desc': '中小尺度山茶散点', 'sell': ['留白比例高', '灰调低饱和'],
     'fabric': '雪纺、醋酸'},
    {'id': 'PF-OWN-2026-000131', 'name': '青花缠枝', 'style': '新中式国风',
     'element': '叶片藤蔓', 'color': '蓝白青花', 'scene': '丝巾',
     'desc': '缠枝线条', 'sell': ['双主色'], 'fabric': '真丝'},
]


def main():
    tmp = tempfile.mkdtemp(prefix='pf_export_test_')
    try:
        run_all(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print('\n%s' % ('—' * 58))
    print('通过 %d · 失败 %d' % (PASS, FAIL))
    return 1 if FAIL else 0


def run_all(tmp):
    # ---------- [1] xlsx 写入器 ----------
    print('\n[1] core/xlsx：结构、内容往返、转义、列字母')
    blob = xlsx.build([['编号', '名称', '备注'],
                       ['PF-1', '山茶小径', '带 "引号" 和\n换行'],
                       [2, '数字列', '']],
                      sheet_name='花型清单', widths=[10, 20, 30])
    z = zipfile.ZipFile(io.BytesIO(blob))
    names = z.namelist()
    for part in ('[Content_Types].xml', '_rels/.rels', 'xl/workbook.xml',
                 'xl/_rels/workbook.xml.rels', 'xl/styles.xml',
                 'xl/worksheets/sheet1.xml'):
        ok(part in names, 'xlsx 里有 %s' % part)
    ok(z.testzip() is None, 'zip 完整（每个部件都能解出来）')
    import xml.dom.minidom as MD
    bad = []
    for part in names:
        try:
            MD.parseString(z.read(part))
        except Exception as e:                                 # noqa: BLE001
            bad.append('%s: %s' % (part, e))
    ok(not bad, '每个 XML 部件都能解析', bad)

    wb = z.read('xl/workbook.xml').decode('utf-8')
    ok('花型清单' in wb, '工作表名写进工作簿')
    rows = sheet_texts(blob)
    ok_eq(len(rows), 3, '三行都在')
    ok_eq(rows[0], ['编号', '名称', '备注'], '表头读回来一模一样')
    ok_eq(rows[1][0], 'PF-1', '字符串单元格往返正确')
    ok(rows[1][2].find('"引号"') >= 0, '引号原样保留（XML 转义没吃掉内容）', rows[1][2])
    ok(rows[1][2].find('换行') >= 0, '换行原样保留', rows[1][2])
    ok(rows[2][0] == '2', '数字列也读得出来')
    # XML 1.0 不允许的控制字符：混进去 Excel 会报"文件已损坏"，必须被清掉
    dirty = xlsx.build([['A'], ['x\x00y\x07z']])
    ok('x\x00' not in zipfile.ZipFile(io.BytesIO(dirty)).read(
        'xl/worksheets/sheet1.xml').decode('utf-8'),
       '控制字符被清掉（否则 Excel 打不开）')
    ok_eq(xlsx.col_letter(0), 'A', 'col_letter(0) = A')
    ok_eq(xlsx.col_letter(25), 'Z', 'col_letter(25) = Z')
    ok_eq(xlsx.col_letter(26), 'AA', 'col_letter(26) = AA')

    # ---------- [1b] xlsx 内嵌图片 ----------
    print('\n[1b] core/xlsx：内嵌图片的部件与锚点')
    png = _synthetic_png(6, 4, (200, 90, 90))
    none_img = xlsx.build([['A'], ['x']])
    zn = zipfile.ZipFile(io.BytesIO(none_img))
    ok(not [n for n in zn.namelist() if 'media' in n or 'drawing' in n],
       '不传图片时一个图片部件都不多写（老用法零影响）')
    ok('r:embed' not in zn.read('xl/worksheets/sheet1.xml').decode('utf-8'),
       '不传图片时 worksheet 里没有 drawing 引用')

    blob_i = xlsx.build([['图', '名称'], ['', '山茶小径'], ['', '青花缠枝']],
                        sheet_name='花型清单', widths=[20, 16],
                        images=[{'data': png, 'ext': 'png', 'row': 1, 'col': 0, 'w': 96, 'h': 64},
                                {'data': png, 'ext': 'png', 'row': 2, 'col': 0, 'w': 96, 'h': 64}])
    zi = zipfile.ZipFile(io.BytesIO(blob_i))
    names_i = zi.namelist()
    for part in ('xl/media/image1.png', 'xl/media/image2.png',
                 'xl/drawings/drawing1.xml',
                 'xl/drawings/_rels/drawing1.xml.rels',
                 'xl/worksheets/_rels/sheet1.xml.rels'):
        ok(part in names_i, '有 %s' % part)
    ok(zi.read('xl/media/image1.png') == png, '图片本体写进去的是原字节')
    ct = zi.read('[Content_Types].xml').decode('utf-8')
    ok('Extension="png"' in ct and 'image/png' in ct, 'Content_Types 声明了 png')
    ok('drawing+xml' in ct, 'Content_Types 声明了 drawing 部件（少这条 Excel 判损坏）')
    sh = zi.read('xl/worksheets/sheet1.xml').decode('utf-8')
    ok('<drawing r:id="rId1"/>' in sh, 'worksheet 里挂上了 drawing 引用')
    dw = zi.read('xl/drawings/drawing1.xml').decode('utf-8')
    ok(dw.count('<xdr:oneCellAnchor>') == 2, '两张图两个锚点', dw.count('<xdr:oneCellAnchor>'))
    ok('<xdr:col>0</xdr:col>' in dw and '<xdr:row>1</xdr:row>' in dw,
       '第一张钉在第 1 行第 0 列（表头下面的「图」列）')
    ok('cx="914400" cy="609600"' in dw,
       '显示尺寸按 1px=9525EMU 换算（96×64 → 914400×609600）')
    ok('r:embed="rId1"' in dw and 'r:embed="rId2"' in dw, '两张图各自引用各自的 media')
    rels = zi.read('xl/drawings/_rels/drawing1.xml.rels').decode('utf-8')
    ok(rels.count('../media/image') == 2, 'drawing → media 两条关系都在')
    ok('../drawings/drawing1.xml' in zi.read(
        'xl/worksheets/_rels/sheet1.xml.rels').decode('utf-8'),
       'sheet → drawing 的关系指对了')
    # 行高：图比字高，不撑高会压到下一行
    ok('customHeight="1"' in sh and 'ht="54' in sh,
       '带图的行被撑高了（64px → 54pt）', re.findall(r'<row[^>]*>', sh))
    ok_eq(len(sheet_texts(blob_i)), 3, '图列不吃掉数据行（还是 3 行）')
    ok_eq(sheet_texts(blob_i)[0][0], '图', '图列有自己的表头')
    import xml.dom.minidom as MD
    bad_i = []
    for part in names_i:
        if not part.endswith(('.xml', '.rels')):          # media 是二进制，别当 XML 解析
            continue
        try:
            MD.parseString(zi.read(part))
        except Exception as e:                             # noqa: BLE001
            bad_i.append('%s: %s' % (part, e))
    ok(not bad_i, '带图后每个 XML 部件仍能解析', bad_i)

    # JPEG 走 Default Extension=jpeg（sips 压出来的就是 jpg）
    blob_j = xlsx.build([['图'], ['']], images=[
        {'data': b'\xff\xd8\xff\xe0jpeg-bytes', 'ext': 'jpeg', 'row': 1, 'col': 0, 'w': 10, 'h': 10}])
    zj = zipfile.ZipFile(io.BytesIO(blob_j))
    ok('xl/media/image1.jpg' in zj.namelist(), 'jpeg 归一成 .jpg 落盘')
    ok('Extension="jpg"' in zj.read('[Content_Types].xml').decode('utf-8'),
       'Content_Types 只声明 jpg 一种（不多写没用的 jpeg）')

    # ---------- [2] export_list 技能 ----------
    print('\n[2] export_list 技能：真生成一份表格')
    reg = SkillRegistry(os.path.join(AGENT, 'skills'))
    ok('export_list' in reg.skills, 'skills/ 里挂上了 export_list')

    ctx_empty = {'patterns': PATTERNS, 'library': None,
                 'imagegen': FakeImageGen(OUTDIR), 'session': Session('t2a')}
    r0 = reg.run('export_list', {}, ctx_empty)
    ok_eq(r0.get('ok'), True, '一款没选中不算报错（是实话，不是异常）')
    ok_eq(r0.get('count'), 0, '数量 0')
    ok_eq(r0.get('downloads'), [], '没有文件可下载')
    ok('花型选择' in (r0.get('note') or ''), '话指到"先去花型选择里选款"', r0.get('note'))

    sess = Session('t2b')
    sess.set_selected(['PF-OWN-2026-000128', 'PF-OWN-2026-000131'])
    ctx = {'patterns': PATTERNS, 'library': None,
           'imagegen': FakeImageGen(OUTDIR), 'session': sess}
    r1 = reg.run('export_list', {}, ctx)
    ok_eq(r1.get('ok'), True, '有选中就出表')
    ok_eq(r1.get('count'), 2, '两款都进了表')
    dl = (r1.get('downloads') or [{}])[0]
    ok(dl.get('url', '').startswith('/api/file/'), '回的是可直接下载的地址', dl)
    ok(dl.get('name', '').endswith('.xlsx'), '扩展名是 .xlsx', dl.get('name'))
    path = os.path.join(OUTDIR, dl.get('name') or '')
    ok(os.path.exists(path), '文件真的落盘了')
    ok_eq(dl.get('bytes'), os.path.getsize(path), '回报的字节数 = 文件实际大小')
    with open(path, 'rb') as f:
        body = f.read()
    rows = sheet_texts(body)
    flat = ' '.join(' '.join(r) for r in rows)
    ok('山茶小径' in flat and '青花缠枝' in flat, '两款名字都写进表了')
    ok('法式浪漫' in flat and '蓝白青花' in flat, '四维标签写进表了')
    ok('雪纺、醋酸' in flat, '适用面料写进表了')

    # ---- 图片：内置款走预渲染缩略图 ----
    print('\n[2b] 导出带图：内置款走预渲染缩略图，压缩后进表')
    ok_eq(r1.get('images'), 2, '两款各配了一张图')
    ok('缩略图' in (r1.get('note') or ''), '回话里说明附了缩略图', r1.get('note'))
    ok(all(not k.startswith('_') for k in r1['items'][0]),
       '内部字段（图片在本机的路径）不外传给前端', sorted(r1['items'][0]))
    ok_eq(media_info(body)[0], 2, '文件里真有两张图')
    ok_eq(len(draw_ext(body)), 2, '两个锚点')
    exts = draw_ext(body)
    ok(all(w <= 128 and h <= 128 for w, h in exts), '图都被压到长边 128 以内', exts)
    ok(all(w >= 40 and h >= 40 for w, h in exts), '也不至于小到看不清', exts)
    ok_eq(sheet_texts(body)[0][0], '图', '第一列是「图」（跟前端预览一致）')
    ok_eq(sheet_texts(body)[0][1], '序号', '「序号」跟在图后面')

    # ---- 没有图的款：那一格留空，表照样交付 ----
    NOIMG = [{'id': 'PF-OWN-2099-999999', 'name': '没有图的款'}]
    r_ni = reg.run('export_list', {'ids': ['PF-OWN-2099-999999']},
                   {'patterns': NOIMG, 'library': None,
                    'imagegen': FakeImageGen(OUTDIR), 'session': Session('t2ni')})
    ok_eq(r_ni.get('ok'), True, '没图不算失败，表照样出')
    ok_eq(r_ni.get('images'), 0, '一张图都没配上')
    ok_eq(media_info(open(os.path.join(OUTDIR, r_ni['downloads'][0]['name']),
                          'rb').read())[0], 0, '文件里确实没有图片部件')
    ok('没有图片' in (r_ni.get('note') or ''), '照实说哪几款没图', r_ni.get('note'))

    # ---- 库里的款：用库自己的缩略图（真图优先） ----
    src = os.path.join(tmp, 'libsrc.png')
    with open(src, 'wb') as f:
        f.write(_synthetic_png(300, 200, (30, 60, 120)))

    class FakeLibraryWithImg:
        def __init__(self, items, img):
            self._items = items
            self._img = img

        def get(self, pid):
            for it in self._items:
                if it.get('id') == pid:
                    return it
            return None

        def path_of(self, item, thumb=False):
            return self._img

    libimg = FakeLibraryWithImg(
        [{'id': 'PF-OWN-2026-000131', 'name': '青花缠枝（库图）', 'file': 'x.png'}], src)
    r_li = reg.run('export_list', {'ids': ['PF-OWN-2026-000131']},
                   {'patterns': PATTERNS, 'library': libimg,
                    'imagegen': FakeImageGen(OUTDIR), 'session': Session('t2li')})
    blob_li = open(os.path.join(OUTDIR, r_li['downloads'][0]['name']), 'rb').read()
    ok_eq(r_li.get('images'), 1, '库图那条路也配上了图')
    ok_eq(media_info(blob_li)[0], 1, '库里那张图进了文件')
    li_ext = draw_ext(blob_li)[0]
    ok(li_ext[0] == 128 and li_ext[1] in (85, 86),
       '300×200 被等比压成 128 宽（高度按比例）', li_ext)

    # 库里真图优先于内置款
    lib = FakeLibrary([{'id': 'PF-OWN-2026-000128', 'name': '山茶小径（库里）',
                        'style': '法式浪漫', 'element': '花卉',
                        'color': '莫兰迪灰调', 'scene': '女装连衣裙',
                        'desc': '库里那份说明', 'fabric': '雪纺'}])
    ctx2 = {'patterns': PATTERNS, 'library': lib,
            'imagegen': FakeImageGen(OUTDIR), 'session': Session('t2c')}
    r2 = reg.run('export_list', {'ids': ['PF-OWN-2026-000128']}, ctx2)
    with open(os.path.join(OUTDIR, r2['downloads'][0]['name']), 'rb') as f:
        flat2 = ' '.join(' '.join(r) for r in sheet_texts(f.read()))
    ok('山茶小径（库里）' in flat2, '同编号时用库里那份（真图优先）', flat2[:80])

    # 编号查不到 + 关掉设计说明列
    r3 = reg.run('export_list', {'ids': ['PF-OWN-2026-000128', 'PF-NOPE'],
                                 'include_desc': False}, ctx2)
    ok_eq(r3.get('count'), 1, '查不到的号不进表')
    ok_eq(r3.get('missing'), ['PF-NOPE'], '查不到的号单独回，便于排查')
    with open(os.path.join(OUTDIR, r3['downloads'][0]['name']), 'rb') as f:
        rws = sheet_texts(f.read())
    # 列下标从表头现找，别写死数字 —— 加一列（比如「图」）就会整体错位
    c_desc = rws[0].index('设计说明')
    ok_eq(rws[1][c_desc], '', '关掉设计说明列后该列是空的')
    ok(rws[1][rws[0].index('适用面料')] != '', '没关的列照旧有内容（关列只关那一列）')

    r4 = reg.run('export_list', {'ids': ['PF-NOPE']}, ctx2)
    ok_eq(r4.get('count'), 0, '全查不到时也不炸')
    ok_eq(r4.get('downloads'), [], '全查不到就没有文件')
    ok('找不到' in (r4.get('note') or ''), '照实说找不到', r4.get('note'))

    # ---------- [3] 会话记「用户选中」 ----------
    print('\n[3] 会话记「用户选中」：去重保序 + 覆盖式 + 重启恢复')
    s = Session('t3')
    ok_eq(s.set_selected(['a', 'b', 'a', '', None, 'c']), ['a', 'b', 'c'],
          '去重、去空、保序')
    s.set_selected(['b'])
    ok_eq(s.selected, ['b'], '是覆盖不是追加（用户在界面上删掉一款要跟着少一款）')
    store = SessionStore(os.path.join(tmp, 'sessions'))
    ss = store.get('t3b')
    ss.set_selected(['PF-1', 'PF-2'])
    store.append(ss, {"type": "selected", "selected": ss.selected})
    store._mem.pop('t3b')
    ok_eq(store.get('t3b').selected, ['PF-1', 'PF-2'], '重启后选中清单还在')

    # ---------- [4]-[7] 对话链路 ----------
    print('\n[4] 客服说「导出花型清单」→ 表格生成好 + 回复带下载地址')
    app = App(who='client', with_runtime=True)
    rt = app.runtime
    stub = StubLLM('已经按你选中的 2 款整理好了，点下面的按钮下载。')
    rt.llm = stub

    out = rt.chat('assistant', '帮我导出花型清单', session_id='t4',
                  selected=['PF-OWN-2026-000128', 'PF-OWN-2026-000131'])
    ok(out.get('export'), '回复里带了 export 结果')
    ok_eq(out['export'].get('count'), 2, '按选中的两款整理')
    ok_eq(len(out.get('downloads') or []), 1, '回复里带了下载地址（前端挂真按钮）')
    ok('表格已经生成好了' in stub.last_system,
       '系统提示里明确告诉模型"表已经生成好了"（它才不会反问）', None)
    ok('不要' in stub.last_system and '转人工' in stub.last_system,
       '提示里点名不许说转人工')

    print('\n[5] 只有客服走这条确定性导出')
    out5 = rt.chat('designer', '帮我导出花型清单', session_id='t4', selected=[])
    ok(out5.get('export') is None, '花型师说同一句话不该被这条链路接管')
    out6 = rt.chat('engineer', '整理成表格', session_id='t4', selected=[])
    ok(out6.get('export') is None, '织印师同理')

    print('\n[6] 客服从没选过花型时的说法')
    out7 = rt.chat('assistant', '帮我把花型整理成表格', session_id='t6',
                   selected=[])
    ok(out7.get('export'), '照样回一份 export（空结果也是结果）')
    ok_eq(out7['export'].get('count'), 0, '数量 0')
    ok_eq(out7.get('downloads'), [], '没有文件')
    note = out7['export'].get('note') or ''
    ok('先' in note and '选' in note, '话是"先去选款"，不是"查不到"', note)
    ok('台账' not in note and '转人工' not in note, '不提台账/转人工', note)

    print('\n[7] 充值意图 → 系统提示里带上"卡片已挂出"')
    rt.chat('assistant', '怎么充值和计费？', session_id='t7', selected=[])
    ok('这一轮用户要充值' in stub.last_system, '充值块进了系统提示')
    ok('微信' in stub.last_system and '支付宝' in stub.last_system,
       '点名了两种支付方式', None)
    ok('不要' in stub.last_system and '转人工' in stub.last_system,
       '明确禁止"转人工"那套老话术')
    rt.chat('assistant', '有没有蓝白青花的花型', session_id='t7', selected=[])
    ok('这一轮用户要充值' not in stub.last_system,
       '跟充值无关的话不该注入充值块（别把提示词塞满）')

    # ---------- [8] 界面按钮那条路 ----------
    print('\n[8] 界面「导出花型清单」按钮 → 同一条产物')
    r8 = rt.export_list('t8', ['PF-OWN-2026-000128'])
    ok_eq(r8.get('ok'), True, '按钮那条路也能出表')
    ok_eq(r8.get('count'), 1, '按传进来的编号导出')
    ok((r8.get('downloads') or [{}])[0].get('url', '').startswith('/api/file/'),
       '同样给的是可下载地址')
    # 真 App：内置款 + 真出图目录，图片要真的进文件
    r8b = open(os.path.join(OUTDIR, r8['downloads'][0]['name']), 'rb').read()
    ok_eq(media_info(r8b)[0], 1, '真链路那份表里也有图（内置款缩略图）')
    ok(draw_ext(r8b)[0][0] <= 128, '真链路的小图同样被压过', draw_ext(r8b))
    # 不带 ids 时用会话里记着的那份
    r9 = rt.export_list('t8')
    ok_eq(r9.get('count'), 1, '不带编号就用会话里记着的选中清单')


class StubLLM:
    """只回一句话，顺手把系统提示抄下来（断言用）。"""

    enabled = True

    def __init__(self, reply):
        self.reply = reply
        self.last_system = ''

    def chat(self, messages, tools=None):
        self.last_system = messages[0].get('content') or ''
        return {'content': self.reply, 'tool_calls': []}


if __name__ == '__main__':
    sys.exit(main())
