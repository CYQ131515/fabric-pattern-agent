import fs from 'node:fs';
import sharp from '../lib/sharp.mjs';
import { apiConfig, generateImage, download } from '../lib/env.mjs';
import { fileURLToPath } from 'node:url';
const ROOT = fileURLToPath(new URL('..', import.meta.url));
const [,, basePath, outPath, bandArg='36', offArg='', modelArg='gpt-image-2.5-sunburst'] = process.argv;
const BW = Number(bandArg);

// 2x2 画布
const meta = await sharp(basePath).metadata();
const W = meta.width, H = meta.height;
const { data, info } = await sharp(basePath).removeAlpha().raw().toBuffer({ resolveWithObject: true });
const CW = W * 2, CH = H * 2;
const canvas = Buffer.alloc(CW * CH * 3);
for (let y = 0; y < CH; y++) for (let x = 0; x < CW; x++) {
  const sx = x % W, sy = y % H;
  const dp = (y * CW + x) * 3, sp = (sy * W + sx) * 3;
  canvas[dp] = data[sp]; canvas[dp + 1] = data[sp + 1]; canvas[dp + 2] = data[sp + 2];
}
const png = await sharp(canvas, { raw: { width: CW, height: CH, channels: 3 } }).png().toBuffer();
// 掩膜：x=W 竖线、y=H 横线（width 2*BW）
const mask = Buffer.alloc(CW * CH * 4);
for (let y = 0; y < CH; y++) for (let x = 0; x < CW; x++) {
  const i = (y * CW + x) * 4;
  mask[i] = mask[i + 1] = mask[i + 2] = 255;
  const onV = Math.abs(x - W) <= BW, onH = Math.abs(y - H) <= BW;
  mask[i + 3] = (onV || onH) ? 0 : 255;
}
const maskPng = await sharp(mask, { raw: { width: CW, height: CH, channels: 4 } }).png().toBuffer();
const prompt = `This is a 2x2 tiled watercolor floral textile. Two thin transparent strips (one vertical at the middle, one horizontal at the middle) hide content that must be reconstructed. Continue the painting naturally across the strips: flowers, leaves, specks, brush strokes and paper grain must flow continuously; do not change anything outside the strips; no new motifs, no colour shift.`;
const { base, key } = apiConfig(ROOT);
const r = await generateImage({ base, key, model: modelArg, prompt, size: 'auto', imageUrls: [`data:image/png;base64,${png.toString('base64')}`], maskUrl: `data:image/png;base64,${maskPng.toString('base64')}`, label: 'wrap-inpaint' });
if (!r.urls.length) throw new Error('no url');
const rawTmp = outPath + '.raw';
await download(r.urls[0], rawTmp);
// 裁中心 tile
const gen = await sharp(rawTmp).removeAlpha().resize(CW, CH, { fit: 'fill' }).raw().toBuffer({ resolveWithObject: true });
// 蒙版外强制还原：只采用模型在缝带内的结果，保证 wrap/其它内容不被破坏
const restored = Buffer.from(canvas);
const mm = await sharp(mask, { raw: { width: CW, height: CH, channels: 4 } }).raw().toBuffer({ resolveWithObject: true });
for (let i = 0; i < CW * CH; i++) {
  if (mm.data[i * 4 + 3] !== 0) continue;
  for (let c = 0; c < 3; c++) restored[i * 3 + c] = gen.data[i * 3 + c];
}
const out = await sharp(restored, { raw: { width: CW, height: CH, channels: 3 } }).png().toBuffer();
const [ox, oy] = offArg ? offArg.split(',').map(Number) : [W / 2, H / 2];
const tile = await sharp(out).extract({ left: ox, top: oy, width: W, height: H }).png().toBuffer();
fs.writeFileSync(outPath, tile);
const { data: d2 } = await sharp(tile).removeAlpha().raw().toBuffer({ resolveWithObject: true });
const g = (x, y) => { const i = (y * W + x) * 3; return 0.299 * d2[i] + 0.587 * d2[i + 1] + 0.114 * d2[i + 2]; };
let wx = 0, wy = 0, ax = 0, ay = 0;
for (let y = 0; y < H; y++) wx += Math.abs(g(0, y) - g(W - 1, y));
for (let x = 0; x < W; x++) wy += Math.abs(g(x, 0) - g(x, H - 1));
for (let y = 0; y < H; y++) for (let x = 1; x < W; x++) ax += Math.abs(g(x, y) - g(x - 1, y));
for (let y = 1; y < H; y++) for (let x = 0; x < W; x++) ay += Math.abs(g(x, y) - g(x, y - 1));
wx /= H; wy /= W; ax /= (W * H); ay /= (W * H);
fs.rmSync(rawTmp, { force: true });
const metaOut = { step: 'wrap-inpaint', model: modelArg, base: basePath, out: outPath, size: [W, H], strip: BW,
  wrapX: +wx.toFixed(2), wrapY: +wy.toFixed(2), ratioX: +(wx / ax).toFixed(2), ratioY: +(wy / ay).toFixed(2),
  ok: wx <= Math.max(6, 1.3 * ax) && wy <= Math.max(6, 1.3 * ay), cost_usd: r.cost };
fs.writeFileSync(outPath + '.meta.json', JSON.stringify(metaOut, null, 2));
console.log(JSON.stringify(metaOut));
