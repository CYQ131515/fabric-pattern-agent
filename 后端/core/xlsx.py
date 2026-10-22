"""最小 XLSX 写入器 —— 只用标准库，不引第三方依赖。

为什么要手写：项目有条硬规矩，**后端零第三方依赖**（openpyxl 这类不能进），
但"花型清单"这种交付物必须是 Excel 能直接打开、能打印、能当打样单附页的表格。
所以这里按 OOXML 最小集自己拼一个 xlsx。

xlsx 本质就是一个 zip，里面是几个固定名字的 XML 部件：

    [Content_Types].xml          声明各部件类型
    _rels/.rels                  包级关系（指向 xl/workbook.xml）
    xl/workbook.xml              工作簿（有哪些 sheet）
    xl/_rels/workbook.xml.rels   工作簿级关系（sheet、样式）
    xl/styles.xml                样式表（这里只用到"普通"和"表头加粗"两种）
    xl/worksheets/sheet1.xml     数据

要插图片时再多四个部件（Excel 少一个就判"文件已损坏"，所以必须齐）：

    xl/media/image1.png              图片本体
    xl/drawings/drawing1.xml         图放哪、多大（oneCellAnchor + 固定 ext）
    xl/drawings/_rels/drawing1.xml.rels    drawing → media 的关系
    xl/worksheets/_rels/sheet1.xml.rels    sheet → drawing 的关系
    另外 [Content_Types].xml 要补 png/jpeg 的 Default 和 drawing 的 Override，
    sheet1.xml 里要有一个 <drawing r:id="..."/> 把 drawing 挂上去。

字符串一律走 **inlineStr**（内联字符串），不搞 sharedStrings ——
少一个部件、少一处索引对不上的机会，代价是文件略大，清单这种体量无所谓。

单元格取值规则：int/float 写成数字（Excel 里能求和、能排序），其余一律当字符串。
"""

import re
import zipfile
from xml.sax.saxutils import escape

# XML 1.0 不允许的控制字符（Excel 遇上会直接报"文件已损坏"）。
_BAD = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')

# 1 像素 = 9525 EMU（English Metric Unit）。OOXML 里图片尺寸只认 EMU。
_EMU = 9525
# 图片在单元格内的内边距（EMU）≈ 2px，别贴着格线
_PAD = 19050


def col_letter(idx):
    """0 → A，25 → Z，26 → AA。"""
    s = ''
    idx += 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


def _text(v):
    """清洗成能安全塞进 XML 的文本。"""
    s = '' if v is None else str(v)
    return escape(_BAD.sub('', s))


def _cell(ref, v):
    if isinstance(v, bool) or v is None:
        return '<c r="%s" t="inlineStr"><is><t>%s</t></is></c>' % (ref, _text(
            '是' if v is True else ('' if v is None else v)))
    if isinstance(v, (int, float)):
        return '<c r="%s"><v>%s</v></c>' % (ref, v)
    s = '' if v is None else str(v)
    # 前后有空格时保留（Excel 默认会吃掉）
    keep = ' xml:space="preserve"' if s != s.strip() else ''
    return '<c r="%s" t="inlineStr"><is><t%s>%s</t></is></c>' % (ref, keep, _text(s))


def _sheet_xml(rows, widths=None, with_drawing=False, row_heights=None):
    cols = ''
    if widths:
        cols = '<cols>' + ''.join(
            '<col min="%d" max="%d" width="%s" customWidth="1"/>' % (i + 1, i + 1, w)
            for i, w in enumerate(widths) if w) + '</cols>'
    body = []
    for r, row in enumerate(rows, start=1):
        cells = ''.join(_cell(col_letter(c) + str(r), v) for c, v in enumerate(row))
        # 表头行（第 1 行）用样式 1 = 加粗
        if r == 1:
            cells = cells.replace('<c ', '<c s="1" ')
        ht = row_heights.get(r) if row_heights else None
        if ht:
            # 行里塞了图就得把行撑高，否则图会压到下一行上（Excel 不会自动撑）
            body.append('<row r="%d" ht="%s" customHeight="1">%s</row>' % (r, ht, cells))
        else:
            body.append('<row r="%d">%s</row>' % (r, cells))
    # <drawing> 在 CT_Worksheet 里排在 sheetData 之后；引了它 Excel 才会画图。
    drawing = '<drawing r:id="rId1"/>' if with_drawing else ''
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
            ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheetViews><sheetView workbookViewId="0">'
            '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
            '</sheetView></sheetViews>'
            '<sheetFormatPr defaultRowHeight="15"/>'
            + cols +
            '<sheetData>' + ''.join(body) + '</sheetData>' + drawing + '</worksheet>')


# ---------- 图片 ----------
def _media_ext(ext):
    """图片扩展名归一：只认 png / jpg（jpeg 一律写成 jpg，少一个 Default 声明）。"""
    e = (ext or 'png').lower().lstrip('.')
    return 'jpg' if e in ('jpg', 'jpeg') else 'png'


def _anchor(n, rid, col, row, w, h):
    """把一张图钉在 (col,row) 这个单元格左上角，尺寸固定 —— 用 oneCellAnchor。

    不用 twoCellAnchor：那种把图的尺寸绑在单元格宽高上，
    列宽一改图就跟着变形，而在 Excel 里列宽是会被用户随手拖的。
    """
    cx, cy = int(w * _EMU), int(h * _EMU)
    return (
        '<xdr:oneCellAnchor>'
        '<xdr:from><xdr:col>%d</xdr:col><xdr:colOff>%d</xdr:colOff>'
        '<xdr:row>%d</xdr:row><xdr:rowOff>%d</xdr:rowOff></xdr:from>'
        '<xdr:ext cx="%d" cy="%d"/>'
        '<xdr:pic>'
        '<xdr:nvPicPr>'
        '<xdr:cNvPr id="%d" name="图片 %d"/>'
        '<xdr:cNvPicPr><a:picLocks noChangeAspect="1"/></xdr:cNvPicPr>'
        '</xdr:nvPicPr>'
        '<xdr:blipFill><a:blip r:embed="%s"/><a:stretch><a:fillRect/></a:stretch></xdr:blipFill>'
        '<xdr:spPr>'
        '<a:xfrm><a:off x="0" y="0"/><a:ext cx="%d" cy="%d"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        '</xdr:spPr>'
        '</xdr:pic><xdr:clientData/>'
        '</xdr:oneCellAnchor>'
        % (col, _PAD, row, _PAD, cx, cy, n, n, rid, cx, cy)
    )


def _drawing_xml(images):
    body = ''.join(
        _anchor(i + 1, 'rId%d' % (i + 1), im['col'], im['row'], im['w'], im['h'])
        for i, im in enumerate(images))
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<xdr:wsDr'
            ' xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"'
            ' xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
            ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            + body + '</xdr:wsDr>')


def _drawing_rels(images):
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + ''.join(
                '<Relationship Id="rId%d"'
                ' Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"'
                ' Target="../media/image%d.%s"/>' % (i + 1, i + 1, _media_ext(im.get('ext')))
                for i, im in enumerate(images))
            + '</Relationships>')

_SHEET_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
               '<Relationship Id="rId1"'
               ' Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing"'
               ' Target="../drawings/drawing1.xml"/>'
               '</Relationships>')


_IMG_TYPES = {'png': 'image/png', 'jpg': 'image/jpeg'}


def _types(exts=()):
    """声明包内各部件的类型。用到的图片扩展名才声明，不多写没用的 Default。"""
    media = ''.join(
        '<Default Extension="%s" ContentType="%s"/>' % (e, _IMG_TYPES[e])
        for e in sorted(set(exts)) if e in _IMG_TYPES)
    drawing = ('<Override PartName="/xl/drawings/drawing1.xml"'
               ' ContentType="application/vnd.openxmlformats-officedocument.drawing+xml"/>'
               if exts else '')
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            + media +
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            + drawing +
            '</Types>')

_ROOT_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
              '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
              '</Relationships>')

_WB_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            '</Relationships>')

# 两种样式：0 = 普通，1 = 表头（加粗）
_STYLES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
           # 颜色用 rgb 直写，不用 theme="1" —— 引主题色就得再带一份
           # xl/theme/theme1.xml，少一个部件 Excel 会当成损坏文件
           '<fonts count="2">'
           '<font><sz val="11"/><color rgb="FF000000"/><name val="Calibri"/></font>'
           '<font><b/><sz val="11"/><color rgb="FF000000"/><name val="Calibri"/></font>'
           '</fonts>'
           '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
           '<borders count="1"><border/></borders>'
           '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
           '<cellXfs count="2">'
           '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
           '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
           '</cellXfs>'
           # 默认单元格样式（各家的解析器都拿它当"整表默认"，
           # 少了它 openpyxl 会警告、个别老版 Excel 也会把首行当没格式）
           '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
           '</styleSheet>')


def _workbook_xml(sheet_name):
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
            ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="%s" sheetId="1" r:id="rId1"/></sheets>'
            '</workbook>' % _text(sheet_name or 'Sheet1'))


def build(rows, sheet_name='Sheet1', widths=None, images=None, row_heights=None):
    """把二维表编成 xlsx 字节流。rows 是 list[list]，第一行当表头。

    images：可选的图片列表，每项一个 dict：
        {'data': b'...',            # 图片字节
         'ext': 'png' | 'jpg',      # 扩展名（jpeg 会自动归一成 jpg）
         'row': 0, 'col': 0,        # 0 基的单元格坐标（图片钉在这个格左上角）
         'w': 128, 'h': 128}        # 显示尺寸（像素）
    row_heights：可选的 {1 基行号: 磅高}，盖过自动算出来的行高。
    """
    import io
    images = list(images or [])
    exts = [_media_ext(im.get('ext')) for im in images]

    # 行高：图比字高得多，不撑高就会压到下一行上。取这一行里最高的那张图，
    # 再按 1px = 0.75pt 换算加一点余量。调用方给了 row_heights 就听调用方的。
    if row_heights is None and images:
        row_heights = {}
        for im in images:
            r = int(im['row']) + 1                       # 转成 1 基
            pt = round(float(im.get('h') or 0) * 0.75) + 6
            row_heights[r] = max(row_heights.get(r, 0), pt)

    buf = io.BytesIO()
    # ZIP 里的顺序不影响解析，但 [Content_Types].xml 放最前更规矩
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', _types(exts))
        z.writestr('_rels/.rels', _ROOT_RELS)
        z.writestr('xl/workbook.xml', _workbook_xml(sheet_name))
        z.writestr('xl/_rels/workbook.xml.rels', _WB_RELS)
        z.writestr('xl/styles.xml', _STYLES)
        z.writestr('xl/worksheets/sheet1.xml',
                   _sheet_xml(rows, widths, with_drawing=bool(images),
                              row_heights=row_heights))
        if images:
            for i, im in enumerate(images):
                z.writestr('xl/media/image%d.%s' % (i + 1, _media_ext(im.get('ext'))),
                           im['data'])
            z.writestr('xl/drawings/drawing1.xml', _drawing_xml(images))
            z.writestr('xl/drawings/_rels/drawing1.xml.rels', _drawing_rels(images))
            z.writestr('xl/worksheets/_rels/sheet1.xml.rels', _SHEET_RELS)
    return buf.getvalue()


def write(path, rows, sheet_name='Sheet1', widths=None, images=None, row_heights=None):
    """写到文件，返回路径。"""
    with open(path, 'wb') as f:
        f.write(build(rows, sheet_name=sheet_name, widths=widths,
                      images=images, row_heights=row_heights))
    return path
