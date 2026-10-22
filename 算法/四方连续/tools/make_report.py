"""把一次运行的结果打包成自包含的 HTML 报告（图片 base64 内嵌）。

用法：PYTHONPATH=. python tools/make_report.py 输入图 out目录 报告路径
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys

import numpy as np
from PIL import Image

from sifang.io_utils import load_image


def data_uri(path: str, max_w: int, quality: int = 86) -> str:
    im = Image.open(path).convert("RGB")
    if im.width > max_w:
        im = im.resize((max_w, int(im.height * max_w / im.width)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


CSS = """
:root{--bg:#f6f6f4;--card:#fff;--line:#e3e2dd;--ink:#1d1d1b;--mut:#6b6a65;
--ok:#0f6e56;--okbg:#e1f5ee;--warn:#a32d2d;--warnbg:#fcebeb;--amber:#854f0b;--amberbg:#faeeda;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.65 -apple-system,"PingFang SC","Hiragino Sans GB",sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:36px 28px 72px}
h1{font-size:22px;font-weight:600;margin:0 0 6px}
.sub{color:var(--mut);font-size:13px;margin-bottom:26px}
h2{font-size:16px;font-weight:600;margin:34px 0 12px;padding-bottom:8px;border-bottom:1px solid var(--line)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px 22px;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
.kv{font-size:13px}.kv b{font-weight:600}
.label{color:var(--mut);font-size:12px;display:block;margin-bottom:2px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-weight:500;font-size:12px}
td.num{font-variant-numeric:tabular-nums;text-align:right}
.best{background:var(--okbg);color:var(--ok);font-weight:600}
.pill{display:inline-block;padding:2px 9px;border-radius:99px;font-size:12px;font-weight:500}
.p-ok{background:var(--okbg);color:var(--ok)}.p-warn{background:var(--warnbg);color:var(--warn)}
.p-mid{background:var(--amberbg);color:var(--amber)}
figure{margin:0 0 18px}figure img{width:100%;border:1px solid var(--line);border-radius:10px;display:block;background:#fff}
figcaption{color:var(--mut);font-size:12px;margin-top:7px}
ul{margin:8px 0 0;padding-left:20px}li{margin-bottom:5px}
.note{background:var(--amberbg);border-left:3px solid #ef9f27;padding:12px 16px;border-radius:0 8px 8px 0;margin:14px 0 0;font-size:13px}
.ok{background:var(--okbg);border-left:3px solid #1d9e75;padding:12px 16px;border-radius:0 8px 8px 0;margin:14px 0 0;font-size:13px}
code{background:#f1efe8;padding:1px 5px;border-radius:4px;font-size:12px}
"""


def main(src: str, outdir: str, dest: str) -> int:
    rep = json.load(open(f"{outdir}/report.json", encoding="utf-8"))
    sr, br, fid = rep["seam_report"], rep["baseline_report"], rep["fidelity"]
    a = rep.get("analysis") or {}

    lo = load_image(src)
    w0, h0 = lo.shape[1], lo.shape[0]
    an = a.get("main_seam_problems") or []

    def uri(name, w):
        p = f"{outdir}/{name}"
        return data_uri(p, w) if os.path.exists(p) else ""

    imgs = {
        "PV": uri("preview_3x3.png", 700),
        "SEAM": uri("接缝特写.png", 1180),
        "BOTH": uri("两条路线_3x3.png", 1180),
        "CMP": uri("compare.png", 1180),
        "RAW": uri("model_output.png", 620),
    }

    rows = [
        ("纯算法 baseline", br, rep["route"] == "pure-algorithm"),
        ("本次结果（模型 + compose）" if rep["route"] == "model" else "本次结果",
         sr, rep["route"] == "model"),
    ]
    trs = ""
    for name, r, win in rows:
        cls = ' class="best"' if win else ""
        trs += (f"<tr><td{cls}>{name}</td>"
                f"<td class='num{cls}'>{r['boundary_error']:.3f}</td>"
                f"<td class='num{cls}'>{r['seam_visibility']:.3f}</td>"
                f"<td class='num{cls}'>{r['ghosting']:.3f}</td>"
                f"<td class='num{cls}'>{r['sharp_keep']:.3f}</td>"
                f"<td{cls}><span class='pill "
                f"{'p-ok' if r['grade'] == '优' else 'p-mid'}'>{r['grade']}</span></td></tr>")

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>四方连续测试报告 · 深底密集花卉</title><style>{CSS}</style></head><body>
<div class="wrap">

<h1>四方连续测试报告 · 深底密集花卉</h1>
<div class="sub">输入 <code>{os.path.basename(src)}</code> · 原图 {w0}×{h0} ·
{rep['log'][0]} · 路线 <b>{rep['route']}</b></div>

<h2>1 · 输入体检</h2>
<div class="card"><div class="grid">
<div class="kv"><span class="label">有纹理面积</span><b>{sr['covered_ratio']:.1%}</b><br>几乎没有空地</div>
<div class="kv"><span class="label">空白（干净底色）</span><b>{sr['white_ratio']:.1%}</b><br>接缝有东西可藏</div>
<div class="kv"><span class="label">图案类型（大模型判）</span><b>{a.get('pattern_type', '未解析')}</b>
<br>地比例 {a.get('density', '-')} · 适合平铺 {a.get('suitable', '-')}</div>
</div>
<p style="margin:14px 0 0;font-size:13px;color:var(--mut)">
这张图和上一张「花框花卉」正好相反：上一张 45.2% 是白底，补缝落在白底上无处可藏；
这张 <b>96.2% 有纹理、0% 白底</b>，属于补缝法最擅长的类型。</p></div>

<h2>2 · 两条路线</h2>
<div class="card"><table><thead><tr>
<th>路线</th><th class="num">边界误差</th><th class="num">接缝可见度</th>
<th class="num">重影</th><th class="num">保析度</th><th>评级</th></tr></thead>
<tbody>{trs}</tbody></table>
<p style="margin:14px 0 0;font-size:13px;color:var(--mut)">
边界误差 0 最佳。其余三项都是 <b>1.0 最佳</b>：接缝可见度 = 接缝带梯度 ÷ 画面内部；
重影 = 边缘带局部对比度 ÷ 内部；保析度 = 接缝带细节 ÷ <b>该区域修补前的样子</b>。
保析度 &lt;0.85 说明接缝带被抹糊了，&gt;1.30 说明补出了多余的细节。</p>
<div class="ok"><b>这次模型赢了，而且是真的赢在画面上。</b>
纯算法 0.727 的保析度是硬伤 —— 羽化把两侧的花半透明叠在一起，
接缝带整片糊掉（看下面的接缝特写，中间那格）。</div></div>

<h2>3 · 保真度（交付 tile 的主体有没有被改）</h2>
<div class="card"><table><thead><tr><th>口径</th><th class="num">主体平均差</th>
<th class="num">结构相关</th><th class="num">改动像素占比</th></tr></thead><tbody>
<tr><td>本次交付 tile</td><td class="num best">{fid['core_diff']:.2f}</td>
<td class="num best">{fid['correlation']:.3f}</td>
<td class="num best">{fid['changed_ratio']:.1%}</td></tr>
</tbody></table>
<p style="margin:14px 0 0;font-size:13px;color:var(--mut)">
模型自己其实把整张图重画了一遍（三个候选的带外改动分别是 51.3 / 50.8 / 27.1 灰阶、
结构相关只有 0.46~0.79），是 <code>compose</code> 把带外像素强制还原救回来的。
最终只有 1.7% 的像素被动过 —— 就是接缝带那一条。</p></div>

<h2>4 · 结果图</h2>

<figure><img src="{imgs['PV']}" alt="3×3 平铺">
<figcaption>交付结果 3×3 平铺 —— 接缝已经看不出来了</figcaption></figure>

<figure><img src="{imgs['SEAM']}" alt="接缝特写">
<figcaption>接缝十字放大。原图（左）花被硬切；纯算法（中）糊成一片、花瓣重影；
模型 + compose（右）干净，接缝处补了一朵小花并且接得上</figcaption></figure>

<figure><img src="{imgs['BOTH']}" alt="两条路线 3×3">
<figcaption>左：纯算法（竖着的糊带很明显）· 右：模型 + compose（本次结果）</figcaption></figure>

<figure><img src="{imgs['CMP']}" alt="三方对比">
<figcaption>原图直接平铺（有硬缝）/ 纯算法 / 本次结果</figcaption></figure>

<figure><img src="{imgs['RAW']}" alt="模型原始输出">
<figcaption>模型原始输出（未加 compose）—— 它把整张图重画了，这也是为什么保真必须靠架构兜</figcaption></figure>

<h2>5 · 大模型诊断</h2>
<div class="card"><ul>
{''.join(f'<li>{s}</li>' for s in an)}
</ul>
<p style="margin:12px 0 0;font-size:13px"><b>补缝策略</b> 　{a.get('repair_strategy', '-')}</p>
<p style="margin:6px 0 0;font-size:13px;color:var(--mut)">实际喂给出图模型的指令：<code>{rep['image_prompt'][:150]}…</code></p>
</div>

<h2>6 · 结论</h2>
<div class="card"><ul>
<li><b>这张图做得很干净。</b>满地密集花卉、96.2% 有纹理 —— 正是"位移 + 补缝"最擅长的类型，
3×3 平铺肉眼已看不出接缝。</li>
<li><b>模型路线这次明显赢。</b>纯算法的羽化带会横穿花丛、把接缝整片抹糊（保析度 0.727）；
模型补出来的接缝带保析度 1.020，几乎和原图一样利落。</li>
<li><b>保真守住了。</b>模型照旧把整张图重画，但 compose 把带外像素强制还原，
最终只动 1.7% 的像素，主体结构相关 0.997。</li>
</ul>
<div class="ok"><b>一句话：</b>拓扑轴判对了（满地散点、适合平铺），内容轴也过关
（有纹理占比 96.2%、无留白）—— 这张图是补缝法的正面样本，
和上一张「白底框架花」正好构成一对反例。</div></div>

</div></body></html>"""

    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    with open(dest, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"written {dest}  {os.path.getsize(dest) / 1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2], sys.argv[3]))
