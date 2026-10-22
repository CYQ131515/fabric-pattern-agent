#!/usr/bin/env node
// 第 2 步（方案 A，推荐用于渐变/扁平色块/单色图）：AI 补缝
// 原理：把图平移到中线 → 十字带作为 mask → 让 2.5-ext 只重画缝带 → 输出真正无缝且不镜像
// 用法: node steps/02-seam-ai.mjs --base work/base.png --out work/seamless.png [--model gpt-image-2.5-sunburst] [--size 9:16] [--band 140] [--feather 20]
// 参数经验：band 120–160 效果最好；>200 时模型容易把邻近纹理整块复制进缝带（出现“贴图块/硬边”）
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import sharp from '../lib/sharp.mjs';
import { apiConfig, generateImage, download } from '../lib/env.mjs';

const HERE = process.cwd();
const ROOT = fileURLToPath(new URL('..', import.meta.url));
const args = parse(process.argv.slice(2));
const basePath = must(args.base, '--base');
const out = args.out || 'work/seamless.png';
const model = args.model || 'gpt-image-2.5-sunburst';
const size = args.size || 'auto';
const B = Number(args.band || 140);
const FEATHER = Number(args.feather || 20);  // 边界羽化宽度
const AUTO_SHIFT = args['auto-shift'] ? true : false;  // 默认关闭：切到的内容固定在原图边缘区，偏移只会移动接缝位置
fs.mkdirSync(path.dirname(out), { recursive: true });

// ---- 自动偏移：让"原图四条边附近的墨量"最少（缝带会落在原图边缘区）----
let useBase = basePath;
if (AUTO_SHIFT) {
  const m0 = await sharp(basePath).metadata();
  const w0 = m0.width, h0 = m0.height;
  const raw0 = (await sharp(basePath).removeAlpha().raw().toBuffer({ resolveWithObject: true })).data;
  const g0 = new Float32Array(w0 * h0);
  for (let i = 0, p = 0; i < w0 * h0; i++, p += 3) g0[i] = 0.299 * raw0[p] + 0.587 * raw0[p + 1] + 0.114 * raw0[p + 2];
  const blur0 = (await sharp(Buffer.from(g0.map(v => Math.max(0, Math.min(255, Math.round(v))))), { raw: { width: w0, height: h0, channels: 1 } }).blur(12).raw().toBuffer({ resolveWithObject: true })).data;
  const ink = new Uint8Array(w0 * h0);
  for (let i = 0; i < w0 * h0; i++) ink[i] = (Math.abs(g0[i] - blur0[i]) > 12 || g0[i] > 205) ? 1 : 0;
  const edgeW = Math.round(B / 2 + FEATHER + 12);
  // 评分：原图四条边附近（宽度 edgeW）内的墨量；分两阶段搜索（粗→细）
  function scoreShift(dx, dy) {
    let s = 0, n = 0;
    const xs = [];
    for (let x = 0; x < edgeW; x += 2) xs.push(x);
    for (let x = w0 - edgeW; x < w0; x += 2) xs.push(x);
    for (const x of xs) for (let y = 0; y < h0; y += 3) {
      const sx = (x + dx) % w0, sy = (y + dy) % h0;
      s += ink[sy * w0 + sx]; n++;
    }
    for (let y = 0; y < edgeW; y += 2) for (let x = edgeW; x < w0 - edgeW; x += 3) {
      const sx = (x + dx) % w0, sy = (y + dy) % h0;
      s += ink[sy * w0 + sx]; n++;
    }
    for (let y = h0 - edgeW; y < h0; y += 2) for (let x = edgeW; x < w0 - edgeW; x += 3) {
      const sx = (x + dx) % w0, sy = (y + dy) % h0;
      s += ink[sy * w0 + sx]; n++;
    }
    return n ? s / n : 0;
  }
  let best = { dx: 0, dy: 0, score: Infinity };
  const coarse = 32;
  for (let dy = 0; dy < h0; dy += coarse) for (let dx = 0; dx < w0; dx += coarse) {
    const sc = scoreShift(dx, dy);
    if (sc < best.score) best = { dx, dy, score: sc };
  }
  const fine = 8;
  const b0 = { ...best };
  for (let dy = b0.dy - coarse; dy <= b0.dy + coarse; dy += fine) for (let dx = b0.dx - coarse; dx <= b0.dx + coarse; dx += fine) {
    const ddx = ((dx % w0) + w0) % w0, ddy = ((dy % h0) + h0) % h0;
    const sc = scoreShift(ddx, ddy);
    if (sc < best.score) best = { dx: ddx, dy: ddy, score: sc };
  }
  // 循环平移 base
  const shifted = Buffer.alloc(raw0.length);
  for (let y = 0; y < h0; y++) for (let x = 0; x < w0; x++) {
    const sx = (x + best.dx) % w0, sy = (y + best.dy) % h0;
    const dp = (y * w0 + x) * 3, sp = (sy * w0 + sx) * 3;
    shifted[dp] = raw0[sp]; shifted[dp + 1] = raw0[sp + 1]; shifted[dp + 2] = raw0[sp + 2];
  }
  useBase = basePath.replace(/\.png$/, `-shift${best.dx}_${best.dy}.png`);
  await sharp(shifted, { raw: { width: w0, height: h0, channels: 3 } }).png().toFile(useBase);
  console.log(JSON.stringify({ step: '02-seam-ai:auto-shift', dx: best.dx, dy: best.dy, score: +best.score.toFixed(4), edgeW }));
}

const { data, info } = await sharp(useBase).removeAlpha().raw().toBuffer({ resolveWithObject: true });
const W = info.width, H = info.height, ch = info.channels;

// 1) 平移到中线：外边缘变连续，原图边缘差移到中心十字
const off = Buffer.alloc(data.length);
for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
  const sx = (x + (W >> 1)) % W, sy = (y + (H >> 1)) % H;
  for (let c = 0; c < ch; c++) off[(y * W + x) * ch + c] = data[(sy * W + sx) * ch + c];
}
const offPng = await sharp(off, { raw: { width: W, height: H, channels: ch } }).png().toBuffer();

// 2) mask：透明（可重绘）=十字带，其余保留
const mask = Buffer.alloc(W * H * 4);
for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
  const i = (y * W + x) * 4;
  mask[i] = mask[i + 1] = mask[i + 2] = 255;
  mask[i + 3] = (Math.abs(x - W / 2) <= B / 2 || Math.abs(y - H / 2) <= B / 2) ? 0 : 255;
}
const maskPng = await sharp(mask, { raw: { width: W, height: H, channels: 4 } }).png().toBuffer();

const prompt = `This seamless textile tile was shifted by half, so the seam cross is in the centre. Blend the centre cross so the flowers, leaves, dots, stems and the background texture continue naturally across it (keep gradients and grain). Everything outside the cross must stay identical. No mirror, no new motifs, no colour change. Output must tile seamlessly.`;

const t0 = Date.now();
const { base, key } = apiConfig(ROOT);
const r = await generateImage({
  base, key, model, prompt, size,
  imageUrls: [`data:image/png;base64,${offPng.toString('base64')}`],
  maskUrl: `data:image/png;base64,${maskPng.toString('base64')}`,
  label: 'seam-ai',
});
if (!r.urls.length) throw new Error('补缝结果没有图片 URL');
const rawPath = out + '.ai-raw.png';
const bytes = await download(r.urls[0], rawPath);

// ---- 后处理：低频校正 + 羽化融合（消除缝带边界的亮度台阶/硬直线）----
// 思路：out = base + m*(AI - base + (blur(base)-blur(AI)))
//   m=1（带内）→ 保留 AI 高频细节，同时低频亮度对齐 base；m=0（带外）→ 原图；
//   过渡区羽化，避免硬 mask 边界。
const baseMeta = await sharp(useBase).metadata();
const BW = baseMeta.width, BH = baseMeta.height;
const baseRaw = off;  // bug 修复：合成基准必须是「平移后的图」，否则外缘不连续、中间内容错位
const aiRaw = (await sharp(rawPath).removeAlpha().resize(BW, BH, { fit: 'fill' }).raw().toBuffer({ resolveWithObject: true })).data;
const blurBase = (await sharp(baseRaw, { raw: { width: BW, height: BH, channels: 3 } }).blur(15).raw().toBuffer({ resolveWithObject: true })).data;
const blurAI = (await sharp(aiRaw, { raw: { width: BW, height: BH, channels: 3 } }).blur(15).raw().toBuffer({ resolveWithObject: true })).data;
const sm = t => (t <= 0 ? 0 : t >= 1 ? 1 : t * t * (3 - 2 * t));
const res = Buffer.alloc(baseRaw.length);
for (let y = 0; y < BH; y++) {
  for (let x = 0; x < BW; x++) {
    // 到十字带内核的距离（带内 m=1，向外羽化到 0）
    const dH = Math.min(y - (BH / 2 - B / 2), (BH / 2 + B / 2) - 1 - y);
    const dV = Math.min(x - (BW / 2 - B / 2), (BW / 2 + B / 2) - 1 - x);
    const m = Math.max(sm((dH + FEATHER) / FEATHER), sm((dV + FEATHER) / FEATHER));
    const i = (y * BW + x) * 3;
    for (let c = 0; c < 3; c++) {
      const corr = blurBase[i + c] - blurAI[i + c];
      const v = baseRaw[i + c] + m * (aiRaw[i + c] - baseRaw[i + c] + corr);
      res[i + c] = Math.max(0, Math.min(255, Math.round(v)));
    }
  }
}
await sharp(res, { raw: { width: BW, height: BH, channels: 3 } }).png().toFile(out);
const secs = ((Date.now() - t0) / 1000).toFixed(1);
const infoOut = { step: '02-seam-ai', model, size, band: B, feather: FEATHER, base: basePath, work_base: useBase, out, ai_raw: rawPath, bytes, cost_usd: r.cost, seconds: +secs };
fs.writeFileSync(out + '.meta.json', JSON.stringify(infoOut, null, 2));
console.log(JSON.stringify(infoOut));

function parse(argv) { const a = {}; for (let i = 0; i < argv.length; i++) if (argv[i].startsWith('--')) { const k = argv[i].slice(2); const v = argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[++i] : true; a[k] = v; } return a; }
function must(v, n) { if (!v || v === true) { console.error(`缺少参数 ${n}`); process.exit(1); } return v; }
