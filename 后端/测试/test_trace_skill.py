#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""描稿仿稿（trace_pattern）技能回归。

直接调 handler，不启服务、不花钱：生图走 mock provider，只验证
「照片来源 → 体检 → 闸门 → 提示词 → 落盘」这条逻辑链。

跑法：python3 tools/test_trace_skill.py
"""
import base64
import os
import struct
import sys
import tempfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.dirname(HERE)
sys.path.insert(0, AGENT)

os.environ['PATTERNFLOW_IMAGE_PROVIDER'] = 'mock'
# 出图产物挪去临时目录，别堆进项目的 generated/
os.environ['PATTERNFLOW_IMAGE_OUTDIR'] = tempfile.mkdtemp(prefix='pf-test-img-')
# 图库目录也挪走：出图会自动入库，别把测试产物混进真图库
os.environ['PATTERNFLOW_LIBRARY_DIR'] = tempfile.mkdtemp(prefix='pf-test-lib-')

from core.config import Config            # noqa: E402
from core.imagegen import ImageGen, build_trace_prompt  # noqa: E402
from core.session import Session          # noqa: E402
from core import photo_qc                 # noqa: E402
from core.imgio import as_png, cleanup, decode_png  # noqa: E402

sys.path.insert(0, os.path.join(AGENT, 'skills', 'trace_pattern'))
import handler as trace_handler           # noqa: E402

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


# ---------------------------------------------------------------- 造测试图
def png(w, h, ch, px):
    raw = bytearray()
    stride = w * ch
    for y in range(h):
        raw.append(0)
        raw += px[y * stride:(y + 1) * stride]
    def chunk(tag, body):
        return (struct.pack('>I', len(body)) + tag + body
                + struct.pack('>I', zlib.crc32(tag + body) & 0xFFFFFFFF))
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2 if ch == 3 else 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(bytes(raw), 6))
            + chunk(b'IEND', b''))


def make_pattern_photo(w=900, h=900):
    """造一张"像照片"的图：白底 + 蓝花点，有足够梯度。"""
    px = bytearray(w * h * 3)
    for y in range(h):
        for x in range(w):
            i = (y * w + x) * 3
            cx, cy = x % 120, y % 120
            d = ((cx - 60) ** 2 + (cy - 60) ** 2) ** 0.5
            if d < 26:
                px[i], px[i + 1], px[i + 2] = 40, 70, 150
            elif d < 34:
                px[i], px[i + 1], px[i + 2] = 120, 150, 210
            else:
                px[i] = px[i + 1] = px[i + 2] = 252
    return png(w, h, 3, px)


def make_flat_image(w=600, h=600):
    """造一张"废图"：接近纯色，没有可提取的结构。"""
    px = bytearray(bytes([128, 128, 128]) * (w * h))
    return png(w, h, 3, px)


def b64(blob):
    return base64.b64encode(blob).decode()


def make_ctx(sess):
    cfg = Config()
    return {'imagegen': ImageGen(cfg), 'session': sess, 'brief': {}}


def main():
    print('\n[1] 没有照片时应当拒绝，并指路')
    sess = Session('t-trace-1')
    r = trace_handler.run({'confirmed': False}, make_ctx(sess))
    ok('error' in r, '无照片 → 返回 error', r)
    ok('上传' in (r.get('error') or ''), '错误信息告诉用户要上传照片', r.get('error'))
    ok('need_confirm' not in r, '无照片时不产生确认卡片')

    print('\n[2] 正常照片 → 只提案，不执行（扣费闸门）')
    sess = Session('t-trace-2')
    sess.last_photo = {'b64': b64(make_pattern_photo()), 'mime': 'image/png'}
    r = trace_handler.run({'confirmed': False}, make_ctx(sess))
    ok(r.get('need_confirm') is True, '返回 need_confirm', r)
    ok_eq(r.get('action'), 'trace', 'action 标记为 trace')
    ok_eq(r.get('cost'), 60, '点数 60')
    ok('ok' not in r or not r.get('ok'), '提案阶段没有真正出图')
    ok(sess.last_image is None, '提案阶段没有写 last_image（未出图）')
    ok('qc' in r, '携带照片体检结果')
    qc_pub = r.get('qc') or {}
    ok('clip' not in qc_pub and 'blur_ratio' not in qc_pub and 'sharpness' not in qc_pub,
       '原始指标不外泄（模型拿到数字会自己编解读）', sorted(qc_pub.keys()))
    ok('level' in qc_pub and 'issues' in qc_pub, '体检里带结论和问题清单')
    print('       体检：%s' % qc_pub.get('level'))
    print('       摘要：%s' % r.get('summary'))
    ok('fidelity' not in r, '提案里不再有还原度字段（2026-10-20 去掉三档）', sorted(r.keys()))
    ok_eq(r.get('count'), 1, '提案里带张数（默认 1 张）')
    ok('清晰化重绘' not in (r.get('summary') or ''),
       '摘要不再写档位名（还原度改由界面按钮承担）', r.get('summary'))
    hint = r.get('hint') or ''
    ok('三句' in hint, 'hint 要求只回三句（回答要精简）', hint[:70])
    ok('还原度' in hint and '不要' in hint,
       'hint 明确：不要提还原度/风格档位')
    ok('确认清单' in hint, 'hint 禁止列确认清单')

    print('\n[3] 废图 → 直接拦住，不给确认')
    sess = Session('t-trace-3')
    sess.last_photo = {'b64': b64(make_flat_image()), 'mime': 'image/png'}
    r = trace_handler.run({'confirmed': False}, make_ctx(sess))
    ok('error' in r, '纯色图 → 返回 error', r)
    ok('need_confirm' not in r, '纯色图不产生确认卡片')
    ok('重拍' in (r.get('error') or '') or '文字描述' in (r.get('error') or ''),
       '错误信息给了出路（重拍或改用文字描述）')

    print('\n[4] 用户确认后真正执行（mock 生图）')
    sess = Session('t-trace-4')
    sess.last_photo = {'b64': b64(make_pattern_photo()), 'mime': 'image/png'}
    r = trace_handler.run({'confirmed': True}, make_ctx(sess))
    ok(r.get('ok') is True, '确认后执行成功', r)
    ok(bool(r.get('images')), '返回了图片')
    ok(sess.last_image is not None, '执行后写了 last_image（可继续改图）')
    ok('四方连续' in (r.get('note') or ''), '完成提示里点明不含四方连续（边界不越界）')
    ok('平面' in r.get('note', '') or '照片' in r.get('note', ''),
       'note 说明产物是什么')

    print('\n[5] 还原度档位与提示词')
    p_clean = build_trace_prompt({}, 'clean')
    p_strict = build_trace_prompt({}, 'strict')
    ok('清晰化重绘' in p_clean, 'clean 档写入提示词')
    ok('严格复刻' in p_strict, 'strict 档写入提示词')
    ok(p_clean != p_strict, '两档提示词不同')
    ok('褶皱' in p_clean and '透视' in p_clean,
       '提示词明确要求丢弃拍摄条件（褶皱/透视）')
    ok('无缝' not in p_clean and '四方连续' not in p_clean,
       '描稿提示词不掺入四方连续（那是下一步的事）')
    p_brief = build_trace_prompt({'color': '粉紫柔雾'}, 'clean')
    ok('配色粉紫柔雾' in p_brief, 'brief 作为额外要求拼进提示词')
    ok('图案仍以照片为准' in p_brief, '明确图案以照片为准，brief 只覆盖指定项')

    print('\n[6] 非法还原度回落默认值')
    p_bad = build_trace_prompt({}, 'nonsense')
    ok('清晰化重绘' in p_bad, '未知档位回落到 clean')

    print('\n[7] 只有生成图、没有照片时不给描')
    sess = Session('t-trace-7')
    sess.last_image = {'b64': b64(make_pattern_photo()), 'mime': 'image/png',
                       'origin': 'generated'}
    r = trace_handler.run({'confirmed': False}, make_ctx(sess))
    ok('error' in r, '生成图不能当照片描（避免自己描自己）', r)

    print('\n[8] 体检指标本身：清晰图应当过关')
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, 'a.png')
        with open(p, 'wb') as f:
            f.write(make_pattern_photo())
        qc = photo_qc.qc_from_path(p)
    ok(qc['level'] == 'good', '清晰图判为 good', qc)
    ok(qc['usable'] is True, '清晰图可用')

    print('\n[9] 描稿用自己的出图客户端（后台可单独配），没有时退回共用的')
    sess = Session('t-trace-9')
    sess.last_photo = {'b64': b64(make_pattern_photo()), 'mime': 'image/png'}
    cfg = Config()
    own = ImageGen(cfg, section='trace')
    own.cost = 7                       # 冒充"后台给描稿单独配了点数和模型"
    ctx = {'tracegen': own, 'imagegen': ImageGen(cfg),
           'session': sess, 'brief': {}}
    r = trace_handler.run({'confirmed': False}, ctx)
    ok(r.get('cost') == 7, '有独立描稿客户端时用它报价（不是生图那个）', r.get('cost'))
    r2 = trace_handler.run({'confirmed': False}, make_ctx(sess))
    ok(r2.get('cost') == 60, '没有独立实例时退回共用生图客户端', r2.get('cost'))
    ok(r2.get('need_confirm') is True, '退回后仍正常出提案')

    print('\n结果：%d 通过 / %d 失败' % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
