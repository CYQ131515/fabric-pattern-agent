import fs from 'node:fs';
import sharp from '../lib/sharp.mjs';
import { apiConfig, generateImage, download } from '../lib/env.mjs';
import { fileURLToPath } from 'node:url';
const ROOT = fileURLToPath(new URL('..', import.meta.url));
const args = parse(process.argv.slice(2));
const input = must(args.input), outPath = must(args.out);
const mode = args.mode || 'tile';           // tile | canvas2x2
const BAND = Number(args.band || 30);
const MODEL = args.model || 'gpt-image-2.5-sunburst';
const RESTORE = args.restore === '1';
const EXPLICIT = args.lines || null;

const src = await sharp(input).removeAlpha().raw().toBuffer({ resolveWithObject: true });
const W0 = src.info.width, H0 = src.info.height;
let CW, CH, canvas;
if (mode === 'canvas2x2') {
  CW = W0 * 2; CH = H0 * 2;
  canvas = Buffer.alloc(CW * CH * 3);
  for (let y = 0; y < CH; y++) for (let x = 0; x < CW; x++) {
    const sx = x % W0, sy = y % H0;
    const dp = (y * CW + x) * 3, sp = (sy * W0 + sx) * 3;
    canvas[dp] = src.data[sp]; canvas[dp + 1] = src.data[sp + 1]; canvas[dp + 2] = src.data[sp + 2];
  }
} else { CW = W0; CH = H0; canvas = src.data; }

// 检测直线位置（跨线梯度离群）
const g = new Float32Array(CW * CH);
for (let i = 0, p = 0; i < CW * CH; i++, p += 3) g[i] = 0.299 * canvas[p] + 0.587 * canvas[p + 1] + 0.114 * canvas[p + 2];
function detect(axis) {
  const n = axis === 'v' ? CW : CH, m = axis === 'v' ? CH : CW;
  const means = new Float64Array(n), fracs = new Float64Array(n);
  for (let i = 1; i < n; i++) {
    let s = 0, c = 0;
    for (let j = 0; j < m; j++) {
      const a = axis === 'v' ? g[j * CW + i] : g[i * CW + j];
      const b = axis === 'v' ? g[j * CW + i - 1] : g[(i - 1) * CW + j];
      const d = Math.abs(a - b); s += d; if (d > 10) c++;
    }
    means[i] = s / m; fracs[i] = c / m;
  }
  const vals = Array.from(means).filter(v => v > 0).sort((a, b) => a - b);
  const med = vals[Math.floor(vals.length / 2)] || 1;
  const lines = [];
  for (let i = Math.floor(n * 0.06); i < n - Math.floor(n * 0.06); i++) {
    if (means[i] > Math.max(6, med * 2.0) && fracs[i] > 0.15) lines.push(i);
  }
  // 合并相邻
  const merged = [];
  for (const i of lines) { if (merged.length && i - merged[merged.length - 1] <= 3) merged[merged.length - 1] = i; else merged.push(i); }
  return merged;
}
let vLines = detect('v'), hLines = detect('h');
if (EXPLICIT) { const parts = EXPLICIT.split(';'); vLines = (parts.find(x=>x.startsWith('v:'))||'v:').slice(2).split(',').filter(Boolean).map(Number); hLines = (parts.find(x=>x.startsWith('h:'))||'h:').slice(2).split(',').filter(Boolean).map(Number); }
console.log(JSON.stringify({ mode, size: [CW, CH], vLines, hLines }));

const mask = Buffer.alloc(CW * CH * 4);
for (let y = 0; y < CH; y++) for (let x = 0; x < CW; x++) {
  const i = (y * CW + x) * 4;
  mask[i] = mask[i + 1] = mask[i + 2] = 255;
  const E = 10; // 不碰外缘，保证 wrap 不被破坏
  const tv = vLines.some(v => Math.abs(x - v) <= BAND / 2) && y >= E && y < CH - E;
  const th = hLines.some(h => Math.abs(y - h) <= BAND / 2) && x >= E && x < CW - E;
  mask[i + 3] = (tv || th) ? 0 : 255;
}
const png = await sharp(canvas, { raw: { width: CW, height: CH, channels: 3 } }).png().toBuffer();
const maskPng = await sharp(mask, { raw: { width: CW, height: CH, channels: 4 } }).png().toBuffer();
const prompt = `This is a watercolor floral textile tile. Thin transparent strips hide parts of the painting that must be reconstructed. Continue the painting naturally across every strip: flowers, leaves, specks, brush strokes and paper texture must flow continuously; keep everything outside the strips identical; no new motifs, no colour shift.`;
const { base, key } = apiConfig(ROOT);
const r = await generateImage({ base, key, model: MODEL, prompt, size: 'auto', imageUrls: [`data:image/png;base64,${png.toString('base64')}`], maskUrl: `data:image/png;base64,${maskPng.toString('base64')}`, label: 'inpaint-lines' });
if (!r.urls.length) throw new Error('no url');
const rawTmp = outPath + '.raw';
await download(r.urls[0], rawTmp);
const resized = await sharp(rawTmp).removeAlpha().resize(CW, CH, { fit: 'fill' }).png().toBuffer();
let tile = resized;
if (mode === 'canvas2x2') tile = await sharp(resized).extract({ left: W0 / 2, top: H0 / 2, width: W0, height: H0 }).png().toBuffer();
let finalTile = tile;
if (RESTORE) {
  // 只把蒙版内区域采用模型结果，其余强制还原（wrap/其它内容不可能被破坏）
  const orig = await sharp(input).removeAlpha().resize(W0, H0, { fit: 'fill' }).raw().toBuffer({ resolveWithObject: true });
  const gen = await sharp(tile).removeAlpha().raw().toBuffer({ resolveWithObject: true });
  const buf = Buffer.from(orig.data);
  const m = await sharp(mask, { raw: { width: CW, height: CH, channels: 4 } }).resize(W0, H0, { fit: 'fill' }).raw().toBuffer({ resolveWithObject: true });
  const FE = 2;
  for (let y = 0; y < H0; y++) for (let x = 0; x < W0; x++) {
    const ai = (y * W0 + x) * 4; const inside = m.data[ai + 3] === 0;
    if (!inside) continue;
    const p3 = (y * W0 + x) * 3;
    for (let c = 0; c < 3; c++) buf[p3 + c] = gen.data[p3 + c];
  }
  finalTile = await sharp(buf, { raw: { width: W0, height: H0, channels: 3 } }).png().toBuffer();
}
fs.writeFileSync(outPath, finalTile);
const t = await sharp(tile).removeAlpha().raw().toBuffer({ resolveWithObject: true });
const W = t.info.width, H = t.info.height, d2 = t.data;
const gg = (x, y) => { const i = (y * W + x) * 3; return 0.299 * d2[i] + 0.587 * d2[i + 1] + 0.114 * d2[i + 2]; };
let wx = 0, wy = 0, ax = 0, ay = 0;
for (let y = 0; y < H; y++) wx += Math.abs(gg(0, y) - gg(W - 1, y));
for (let x = 0; x < W; x++) wy += Math.abs(gg(x, 0) - gg(x, H - 1));
for (let y = 0; y < H; y++) for (let x = 1; x < W; x++) ax += Math.abs(gg(x, y) - gg(x - 1, y));
for (let y = 1; y < H; y++) for (let x = 0; x < W; x++) ay += Math.abs(gg(x, y) - gg(x, y - 1));
wx /= H; wy /= W; ax /= W * H; ay /= W * H;
fs.rmSync(rawTmp, { force: true });
const meta = { step: 'inpaint-lines', mode, input, out: outPath, size: [W, H], band: BAND,
  vLines: vLines.length, hLines: hLines.length,
  wrapX: +wx.toFixed(2), wrapY: +wy.toFixed(2), ratioX: +(wx / ax).toFixed(2), ratioY: +(wy / ay).toFixed(2),
  ok: wx <= Math.max(6, 1.3 * ax) && wy <= Math.max(6, 1.3 * ay), cost_usd: r.cost };
fs.writeFileSync(outPath + '.meta.json', JSON.stringify(meta, null, 2));
console.log(JSON.stringify(meta));

function parse(argv) { const a = {}; for (let i = 0; i < argv.length; i++) if (argv[i].startsWith('--')) { const k = argv[i].slice(2); const v = argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[++i] : true; a[k] = v; } return a; }
function must(v, n) { if (!v || v === true) { console.error(`缺少参数 ${n}`); process.exit(1); } return v; }
