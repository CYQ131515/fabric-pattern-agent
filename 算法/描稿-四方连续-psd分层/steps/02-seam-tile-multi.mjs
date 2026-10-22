#!/usr/bin/env node
// 第 2 步（方案 D）：多样本重生成 + wrap 硬筛
// 生成 N 个候选 tile，逐个计算平铺接缝指标（wrap/相邻像素），只接受真正可平铺的样本。
// 用法: node steps/02-seam-tile-multi.mjs --base work/base.png --out work/seamless.png \
//         [--model gpt-image-2.5-sunburst] [--samples 4] [--max-ratio 1.3]
// 退出码: 0=找到合格样本；2=无合格样本（out 保存最佳样本，meta 标记 ok=false）
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import sharp from '../lib/sharp.mjs';
import { apiConfig, generateImage, download } from '../lib/env.mjs';

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), '..');
const args = parse(process.argv.slice(2));
const basePath = must(args.base, '--base');
const out = args.out || 'work/seamless.png';
const model = args.model || 'gpt-image-2.5-sunburst';
const size = args.size || '1:1';
const N = Math.max(1, Number(args.samples || 4));
const MAX_RATIO = Number(args['max-ratio'] || 1.3);
fs.mkdirSync(path.dirname(out), { recursive: true });

const PROMPT = `A seamless repeating textile pattern tile, flat 2D surface design, exactly tileable: the left edge continues perfectly into the right edge and the top into the bottom, no visible seam lines anywhere.
Keep the same visual language as the reference: same motif vocabulary (the same kinds of drawn objects/shapes), same two-tone/limited palette, same line quality (hand-drawn outlines / grain / brush texture) and the same background treatment (flat colour or soft gradient).
Square tile, centred composition, no borders, no text, no watermark.`;
const images = [];
{
  const meta = await sharp(basePath).metadata();
  const buf = await sharp(basePath).resize({ width: Math.min(1254, meta.width) }).png().toBuffer();
  images.push(`data:image/png;base64,${buf.toString('base64')}`);
}
function wrapMetrics(p) {
  return sharp(p).removeAlpha().raw().toBuffer({ resolveWithObject: true }).then(({ data, info }) => {
    const W = info.width, H = info.height, ch = info.channels;
    const g = (x, y) => { const i = (y * W + x) * ch; return 0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2]; };
    let wx = 0, ax = 0, wy = 0, ay = 0;
    for (let y = 0; y < H; y++) wx += Math.abs(g(0, y) - g(W - 1, y));
    for (let y = 0; y < H; y++) for (let x = 1; x < W; x++) ax += Math.abs(g(x, y) - g(x - 1, y));
    for (let x = 0; x < W; x++) wy += Math.abs(g(x, 0) - g(x, H - 1));
    for (let y = 1; y < H; y++) for (let x = 0; x < W; x++) ay += Math.abs(g(x, y) - g(x, y - 1));
    const wX = wx / H, wY = wy / W, aX = ax / (W * H), aY = ay / (W * H);
    return { wrapX: +wX.toFixed(2), wrapY: +wY.toFixed(2), ratioX: +(wX / (aX + 1e-6)).toFixed(2), ratioY: +(wY / (aY + 1e-6)).toFixed(2), size: [W, H] };
  });
}

const t0 = Date.now();
const { base, key } = apiConfig(ROOT);
const samples = [];
let best = null;
for (let i = 0; i < N; i++) {
  const tmp = out + `.sample${i}.png`;
  try {
    const r = await generateImage({ base, key, model, prompt: PROMPT, size, imageUrls: images, label: `tile-multi#${i}` });
    if (!r.urls.length) throw new Error('no url');
    await download(r.urls[0], tmp);
    const m = await wrapMetrics(tmp);
    const score = Math.max(m.ratioX, m.ratioY);
    const ok = m.ratioX <= MAX_RATIO && m.ratioY <= MAX_RATIO;
    samples.push({ i, ...m, ok, score, cost_usd: r.cost });
    console.log(JSON.stringify({ sample: i, ...m, ok }));
    if (!best || score < best.score) { best = { i, tmp, ...m, score, ok }; }
    if (ok) break;
  } catch (e) { samples.push({ i, error: e.message }); }
}
if (!best) { console.error('全部样本生成失败'); process.exit(2); }
fs.copyFileSync(best.tmp, out);
for (const s of samples) fs.rmSync(out + `.sample${s.i}.png`, { force: true });
const infoOut = {
  step: '02-seam-tile-multi', model, size, samples_requested: N, samples_used: samples.length,
  ok: best.ok, best: { sample: best.i, ...best, tmp: undefined }, samples,
  out, cost_usd: +samples.reduce((s, x) => s + (x.cost_usd || 0), 0).toFixed(5),
  seconds: +((Date.now() - t0) / 1000).toFixed(1),
};
fs.writeFileSync(out + '.meta.json', JSON.stringify(infoOut, null, 2));
console.log(JSON.stringify({ step: '02-seam-tile-multi', ok: infoOut.ok, best: infoOut.best, cost_usd: infoOut.cost_usd }));
process.exit(best.ok ? 0 : 2);

function parse(argv) { const a = {}; for (let i = 0; i < argv.length; i++) if (argv[i].startsWith('--')) { const k = argv[i].slice(2); const v = argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[++i] : true; a[k] = v; } return a; }
function must(v, n) { if (!v || v === true) { console.error(`缺少参数 ${n}`); process.exit(1); } return v; }
