/* 生成内置款花型的缩略图（PNG）。
 *
 * 为什么要这一步：内置款（后端/data/patterns.json）是**程序画的矢量**——
 * 后端手里根本没有位图，只有 motif/pal/seed 三个参数。而"花型清单"要给客户
 * 看得见图，所以这里借前端的 patternSVG()（唯一权威的画法）把每款渲染成 PNG
 * 预先落盘，运行时就只是读文件，不引任何渲染依赖。
 *
 * 什么时候要重跑：data/patterns.json 增删款、或改了前端的画法（MOTIF_FN/PALETTES）。
 *
 * 跑法：NODE_PATH=<node-workspace>/node_modules node 测试/gen_pattern_thumbs.mjs
 * 产物：后端/data/pattern-thumbs/<花型编号>.png（208px，方形）
 */
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const { chromium } = require('playwright');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const BACKEND = path.resolve(HERE, '..');
const ROOT = path.resolve(BACKEND, '..');
const HTML = process.env.PF_CLIENT_HTML
  ? path.resolve(process.env.PF_CLIENT_HTML)
  : path.join(ROOT, '前端', 'PatternFlow花型智造-交互原型v0.7.html');
const DATA = path.join(BACKEND, 'data', 'patterns.json');
const OUT = process.env.OUT || path.join(BACKEND, 'data', 'pattern-thumbs');

/* 画多大：104 的 viewBox 放大 2 倍，缩放时不会糊。真正进表格还会再压一次。 */
const PX = 208;

const doc = JSON.parse(fs.readFileSync(DATA, 'utf8'));
const items = doc.patterns || doc;
fs.mkdirSync(OUT, { recursive: true });

const b = await chromium.launch();
const p = await b.newPage({ viewport: { width: 640, height: 640 }, deviceScaleFactor: 1 });
await p.goto('file://' + HTML, { waitUntil: 'load' });
await p.waitForTimeout(400);

/* 一个固定尺寸的画布，每款画完截它的图 */
await p.evaluate((px) => {
  const d = document.createElement('div');
  d.id = 'thumbstage';
  d.setAttribute('data-thumbstage', '1');
  d.style.cssText = 'position:fixed;left:0;top:0;z-index:99999;background:#fff';
  d.innerHTML = '<style>#thumbstage .cell{width:' + px + 'px;height:' + px + 'px;overflow:hidden}' +
    '#thumbstage .cell svg{display:block;width:' + px + 'px;height:' + px + 'px}</style>' +
    '<div class="cell" id="thumbcell"></div>';
  document.body.appendChild(d);
}, PX);

let made = 0, skipped = [];
for (const it of items) {
  const okDraw = await p.evaluate((item) => {
    const cell = document.getElementById('thumbcell');
    try {
      // 走前端唯一权威的画法：有 image 会出 <img>，内置款出 SVG
      cell.innerHTML = window.__YQ.patternSVG(item, {});
      return !!cell.querySelector('svg,img');
    } catch (e) {
      cell.innerHTML = '';
      return false;
    }
  }, it);
  if (!okDraw) { skipped.push(it.id); continue; }
  await p.waitForTimeout(80);
  const el = await p.$('#thumbcell');
  const buf = await el.screenshot({ type: 'png' });
  fs.writeFileSync(path.join(OUT, it.id + '.png'), buf);
  made++;
}

await b.close();
console.log('写入目录：', OUT);
console.log('生成 ' + made + ' 张 / 共 ' + items.length + ' 款');
if (skipped.length) console.log('跳过（画不出来）：', skipped.join(', '));
process.exit(skipped.length ? 1 : 0);
