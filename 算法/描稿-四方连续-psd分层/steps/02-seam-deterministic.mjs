#!/usr/bin/env node
// 第 2 步（方案 B）：确定性补缝（零 API 成本，适合繁复纹理，如人字纹/点阵/霓虹）
// 原理：平移到中线 → 逐列/逐行自适应平移补十字带 → Laplacian 多频段融合
// 注意：平滑渐变/大面积平涂图会出现矩形色块，这类图请改用 02-seam-ai.mjs
// 用法: node steps/02-seam-deterministic.mjs --base work/base.png --out work/seamless.png
import fs from 'node:fs';
import path from 'node:path';
import sharp from '../lib/sharp.mjs';

const args = parse(process.argv.slice(2));
const basePath = must(args.base, '--base');
const out = args.out || 'work/seamless.png';
fs.mkdirSync(path.dirname(out), { recursive: true });

const B = 190, T = 26, MARGIN = 20, FE = 6;
const { data, info } = await sharp(basePath).removeAlpha().raw().toBuffer({ resolveWithObject: true });
const W = info.width, H = info.height, ch = info.channels;
const px = (buf, x, y, c) => buf[(((y % H) + H) % H * W + ((x % W) + W) % W) * ch + c];
const set = (buf, x, y, c, v) => { buf[(((y % H) + H) % H * W + ((x % W) + W) % W) * ch + c] = v; };

// 1) 平移到中线
const O = Buffer.alloc(data.length);
for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
  const sx = (x + (W >> 1)) % W, sy = (y + (H >> 1)) % H;
  for (let c = 0; c < ch; c++) O[(y * W + x) * ch + c] = data[(sy * W + sx) * ch + c];
}
const cy = H >> 1, h0 = cy - (B >> 1), h1 = h0 + B;
const cx = W >> 1, v0 = cx - (B >> 1), v1 = v0 + B;
const smooth = (arr, r) => { const o = new Int32Array(arr.length), t = []; for (let i = 0; i < arr.length; i++) { t.length = 0; for (let d = -r; d <= r; d++) t.push(arr[(i + d + arr.length) % arr.length]); t.sort((a, b) => a - b); o[i] = t[t.length >> 1]; } return o; };

// 2) 水平缝：逐列最优 dy
const dyA = new Int32Array(W);
for (let x = 0; x < W; x++) {
  let bd = 0, be = Infinity;
  for (let dy = 0; dy < H; dy++) {
    if (dy > h0 - T - B && dy < h1 + T) continue;
    let e = 0;
    for (let t = 0; t < T; t++) for (let c = 0; c < ch; c++) { let d = px(O, x, h0 - 1 - t, c) - px(O, x, h0 - 1 - t + dy, c); e += d * d; d = px(O, x, h1 + t, c) - px(O, x, h1 + t + dy, c); e += d * d; }
    if (e < be) { be = e; bd = dy; }
  }
  dyA[x] = bd;
}
const dyS = smooth(dyA, 9);
for (let x = MARGIN; x < W - MARGIN; x++) for (let y = h0 - FE; y < h1 + FE; y++) for (let c = 0; c < ch; c++) set(O, x, y, c, px(O, x, y + dyS[x], c));

// 3) 垂直缝：逐行最优 dx
const dxA = new Int32Array(H);
for (let y = 0; y < H; y++) {
  let bd = 0, be = Infinity;
  for (let dx = 0; dx < W; dx++) {
    if (dx > v0 - T - B && dx < v1 + T) continue;
    let e = 0;
    for (let t = 0; t < T; t++) for (let c = 0; c < ch; c++) { let d = px(O, v0 - 1 - t, y, c) - px(O, v0 - 1 - t + dx, y, c); e += d * d; d = px(O, v1 + t, y, c) - px(O, v1 + t + dx, y, c); e += d * d; }
    if (e < be) { be = e; bd = dx; }
  }
  dxA[y] = bd;
}
const dxS = smooth(dxA, 9);
for (let y = MARGIN; y < H - MARGIN; y++) for (let x = v0 - FE; x < v1 + FE; x++) for (let c = 0; c < ch; c++) set(O, x, y, c, px(O, x + dxS[y], y, c));

// 4) 边界窄羽化
for (const [axis, lo, hi] of [['x', h0, h1], ['y', v0, v1]]) {
  for (let f = 1; f <= FE; f++) {
    const a = f / (FE + 1);
    if (axis === 'x') for (let x = 0; x < W; x++) for (let c = 0; c < ch; c++) {
      set(O, x, lo - 1 - f, c, Math.round(px(O, x, lo - 1 - f, c) * (1 - a) + px(O, x, lo, c) * a));
      set(O, x, hi + f, c, Math.round(px(O, x, hi + f, c) * (1 - a) + px(O, x, hi - 1, c) * a));
    }
    else for (let y = 0; y < H; y++) for (let c = 0; c < ch; c++) {
      set(O, lo - 1 - f, y, c, Math.round(px(O, lo - 1 - f, y, c) * (1 - a) + px(O, lo, y, c) * a));
      set(O, hi + f, y, c, Math.round(px(O, hi + f, y, c) * (1 - a) + px(O, hi - 1, y, c) * a));
    }
  }
}

// 5) Laplacian 多频段融合（压低缝带低频色差）
const A = Buffer.from(O); // 已补缝图（细节来源）
const Bimg = Buffer.alloc(data.length); // 纯平移图（低频/边界参考）
for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) { const sx = (x + (W >> 1)) % W, sy = (y + (H >> 1)) % H; for (let c = 0; c < ch; c++) Bimg[(y * W + x) * ch + c] = data[(sy * W + sx) * ch + c]; }
const M = new Float32Array(W * H), FE2 = 40;
const sm = t => (t <= 0 ? 0 : t >= 1 ? 1 : t * t * (3 - 2 * t));
for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) { const dH = Math.min(y - h0, h1 - 1 - y), dV = Math.min(x - v0, v1 - 1 - x); M[y * W + x] = Math.max(sm((dH + FE2) / FE2), sm((dV + FE2) / FE2)); }
const blur = async (buf, sigma, channels = ch) => (await sharp(buf, { raw: { width: W, height: H, channels } }).blur(sigma).raw().toBuffer({ resolveWithObject: true })).data;
const blurM = async s => (await sharp(Buffer.from(Float32Array.from(M).map(v => Math.round(v * 255))), { raw: { width: W, height: H, channels: 1 } }).blur(s).raw().toBuffer({ resolveWithObject: true })).data;
const sigmas = [1.5, 6, 24];
const Al = [], Bl = [], Ml = [];
for (const s of sigmas) { Al.push(await blur(A, s)); Bl.push(await blur(Bimg, s)); Ml.push(await blurM(s)); }
const Ab = await blur(A, 72), Bb = await blur(Bimg, 72), Mb = await blurM(72);
const res = Buffer.alloc(A.length);
for (let i = 0; i < W * H; i++) for (let c = 0; c < ch; c++) {
  const k = i * ch + c;
  const d0 = A[k] - Al[0][k], d1 = Al[0][k] - Al[1][k], d2 = Al[1][k] - Al[2][k], baseA = Al[2][k];
  const e0 = Bimg[k] - Bl[0][k], e1 = Bl[0][k] - Bl[1][k], e2 = Bl[1][k] - Bl[2][k], baseB = Bl[2][k];
  const m0 = Ml[0][i] / 255, m1 = Ml[1][i] / 255, m2 = Ml[2][i] / 255, mB = Mb[i] / 255;
  res[k] = Math.max(0, Math.min(255, Math.round((d0 * (1 - m0) + e0 * m0) + (d1 * (1 - m1) + e1 * m1) + (d2 * (1 - m2) + e2 * m2) + (baseA * (1 - mB) + baseB * mB))));
}
await sharp(res, { raw: { width: W, height: H, channels: ch } }).png().toFile(out);

// 指标
const g = (buf, x, y) => { const i = (((y % H) + H) % H * W + ((x % W) + W) % W) * ch; return 0.299 * res[i] + 0.587 * res[i + 1] + 0.114 * res[i + 2]; };
let wx = 0, ax = 0, n2 = 0, wy = 0, ay = 0;
for (let y = 0; y < H; y++) wx += Math.abs(g(res, 0, y) - g(res, W - 1, y));
for (let y = 0; y < H; y++) for (let x = 1; x < W; x++) { ax += Math.abs(g(res, x, y) - g(res, x - 1, y)); n2++; }
for (let x = 0; x < W; x++) wy += Math.abs(g(res, x, 0) - g(res, x, H - 1));
for (let y = 1; y < H; y++) for (let x = 0; x < W; x++) ay += Math.abs(g(res, x, y) - g(res, x, y - 1));
const infoOut = { step: '02-seam-deterministic', base: basePath, out, size: [W, H], wrapXvsAdj: +((wx / H) / (ax / n2)).toFixed(2), wrapYvsAdj: +((wy / W) / (ay / (W * (H - 1)))).toFixed(2), cost_usd: 0 };
fs.writeFileSync(out + '.meta.json', JSON.stringify(infoOut, null, 2));
console.log(JSON.stringify(infoOut));

function parse(argv) { const a = {}; for (let i = 0; i < argv.length; i++) if (argv[i].startsWith('--')) { const k = argv[i].slice(2); const v = argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[++i] : true; a[k] = v; } return a; }
function must(v, n) { if (!v || v === true) { console.error(`缺少参数 ${n}`); process.exit(1); } return v; }
