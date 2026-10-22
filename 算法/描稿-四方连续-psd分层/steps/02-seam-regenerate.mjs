#!/usr/bin/env node
// 第 2 步（方案 C，兜底）：原生 seamless 重生成
// 适用：原图是无周期的一次性构图（没有可复制的"另一半"），前两种补缝都会留痕时。
// 代价：花型按同风格重新排布，不是逐朵复刻；换来数学上真正的无缝。
// 用法: node steps/02-seam-regenerate.mjs --base work/base.png --out work/seamless.png \
//         [--model gpt-image-2.5-ext] [--size 1:1] [--reference 1|0]
import fs from 'node:fs';
import path from 'node:path';
import sharp from '../lib/sharp.mjs';
import { apiConfig, generateImage, download } from '../lib/env.mjs';

const args = parse(process.argv.slice(2));
const base = must(args.base, '--base');
const out = args.out || 'work/seamless.png';
const model = args.model || 'gpt-image-2.5-ext';
const size = args.size || '1:1';
const useRef = args.reference !== '0';
fs.mkdirSync(path.dirname(out), { recursive: true });

const PROMPT = `A seamless repeating textile pattern tile, flat 2D surface design, exactly tileable: the left edge continues perfectly into the right edge and the top into the bottom, no visible seam lines anywhere.
Keep the same visual language as the reference: same motif vocabulary (the same kinds of drawn objects/shapes), same two-tone/limited palette, same line quality (hand-drawn outlines / grain / brush texture) and the same background treatment (flat colour or soft gradient).
Square tile, centred composition, no borders, no text, no watermark.`;

const images = [];
if (useRef) {
  const meta = await sharp(base).metadata();
  const buf = await sharp(base).resize({ width: Math.min(1254, meta.width) }).png().toBuffer();
  images.push(`data:image/png;base64,${buf.toString('base64')}`);
}

const t0 = Date.now();
const { base: apiBase, key } = apiConfig(process.cwd());
const r = await generateImage({ base: apiBase, key, model, prompt: PROMPT, size, imageUrls: images, label: 'seam-regenerate' });
if (!r.urls.length) throw new Error('生成结果没有图片 URL');
const bytes = await download(r.urls[0], out);

// 无缝指标
const { data, info } = await sharp(out).removeAlpha().raw().toBuffer({ resolveWithObject: true });
const W = info.width, H = info.height, ch = info.channels;
const g = (x, y) => { const i = (y * W + x) * ch; return 0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2]; };
let wx = 0, ax = 0, wy = 0, ay = 0;
for (let y = 0; y < H; y++) wx += Math.abs(g(0, y) - g(W - 1, y));
for (let y = 0; y < H; y++) for (let x = 1; x < W; x++) ax += Math.abs(g(x, y) - g(x - 1, y));
for (let x = 0; x < W; x++) wy += Math.abs(g(x, 0) - g(x, H - 1));
for (let y = 1; y < H; y++) for (let x = 0; x < W; x++) ay += Math.abs(g(x, y) - g(x, y - 1));
const infoOut = {
  step: '02-seam-regenerate', model, size, reference: useRef, base, out, bytes,
  size_px: [W, H],
  wrapXvsAdj: +((wx / H) / (ax / (W * H))).toFixed(2),
  wrapYvsAdj: +((wy / W) / (ay / (W * H))).toFixed(2),
  cost_usd: r.cost, seconds: +((Date.now() - t0) / 1000).toFixed(1),
};
fs.writeFileSync(out + '.meta.json', JSON.stringify(infoOut, null, 2));
console.log(JSON.stringify(infoOut));

function parse(argv) { const a = {}; for (let i = 0; i < argv.length; i++) if (argv[i].startsWith('--')) { const k = argv[i].slice(2); const v = argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[++i] : true; a[k] = v; } return a; }
function must(v, n) { if (!v || v === true) { console.error(`缺少参数 ${n}`); process.exit(1); } return v; }
