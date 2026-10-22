#!/usr/bin/env node
// 第 6 步（区域修复）：框选一块区域，只对它做 AI inpaint，再羽化贴回。
// 特点：区域被放大后再交给模型（等效提升修复分辨率），其它区域像素完全不动 → wrap/布局不受影响。
// 用法:
//   自动修一条缝线: node steps/06-repair-region.mjs --image tile.png --out fixed.png \
//        --line v:512 --pad 90 --mask-width 26 --scale 4 --feather 18 [--model gpt-image-2.5-sunburst]
//   任意框选: node steps/06-repair-region.mjs --image tile.png --out fixed.png \
//        --box 400,400,220,220 [--mask-box 20,20,180,180] --scale 4 --feather 16
import fs from 'node:fs';
import sharp from '../lib/sharp.mjs';
import { apiConfig, generateImage, download } from '../lib/env.mjs';
import { fileURLToPath } from 'node:url';
const ROOT = fileURLToPath(new URL('..', import.meta.url));
const args = parse(process.argv.slice(2));
const imgPath = must(args.image, '--image'), outPath = must(args.out, '--out');
const MODEL = args.model || 'gpt-image-2.5-sunburst';
const SCALE = Number(args.scale || 4);
const FEATHER = Number(args.feather || 18);
const PAD = Number(args.pad || 90);
const MASKW = Number(args['mask-width'] || 26);

const meta = await sharp(imgPath).metadata();
const W = meta.width, H = meta.height;
const raw = await sharp(imgPath).removeAlpha().raw().toBuffer({ resolveWithObject: true });

let box, maskBox; // 输出图坐标
if (args.box) {
  const [x, y, w, h] = String(args.box).split(',').map(Number); box = { x, y, w, h };
  if (args['mask-box']) { const [mx, my, mw, mh] = String(args['mask-box']).split(',').map(Number); maskBox = { x: mx, y: my, w: mw, h: mh }; }
  else maskBox = { x: Math.round(w * 0.15), y: Math.round(h * 0.15), w: Math.round(w * 0.7), h: Math.round(h * 0.7) };
} else {
  const [axis, pos] = String(args.line || 'v:512').split(':');
  if (axis === 'v') box = { x: Math.max(0, Number(pos) - PAD), y: 0, w: 2 * PAD, h: H };
  else box = { x: 0, y: Math.max(0, Number(pos) - PAD), w: W, h: 2 * PAD };
  if (axis === 'v') maskBox = { x: PAD - MASKW / 2, y: 0, w: MASKW, h: box.h };
  else maskBox = { x: 0, y: PAD - MASKW / 2, w: box.w, h: MASKW };
}
async function repairBox(box, maskBox) {
  const crop = await sharp(imgPath).extract({ left: box.x, top: box.y, width: box.w, height: box.h }).png().toBuffer();
  const cropMeta = await sharp(crop).metadata();
  let upW = Math.min(1536, Math.max(256, Math.round(cropMeta.width * SCALE)));
  let upH = Math.round(upW * cropMeta.height / cropMeta.width);
  if (upH > 2048) { upH = 2048; upW = Math.round(upH * cropMeta.width / cropMeta.height); }
  const up = await sharp(crop).resize(upW, upH, { fit: 'fill' }).png().toBuffer();
  const mask = Buffer.alloc(upW * upH * 4);
  const sx = upW / box.w, sy = upH / box.h;
  const mLeft = maskBox.x * sx, mTop = maskBox.y * sy, mW = maskBox.w * sx, mH = maskBox.h * sy;
  for (let y = 0; y < upH; y++) for (let x = 0; x < upW; x++) {
    const i = (y * upW + x) * 4;
    mask[i] = mask[i + 1] = mask[i + 2] = 255;
    const inside = x >= mLeft && x < mLeft + mW && y >= mTop && y < mTop + mH;
    mask[i + 3] = inside ? 0 : 255;
  }
  const maskPng = await sharp(mask, { raw: { width: upW, height: upH, channels: 4 } }).png().toBuffer();
  return { crop, cropMeta, up, maskPng, upW, upH };
}

// 裁 patch
const crop = await sharp(imgPath).extract({ left: box.x, top: box.y, width: box.w, height: box.h }).png().toBuffer();
const cropMeta = await sharp(crop).metadata();
let upW = Math.min(2048, Math.max(cropMeta.width, cropMeta.height) * SCALE);
let upH = Math.round(upW * cropMeta.height / cropMeta.width);
if (upH > 2048) { upH = 2048; upW = Math.round(upH * cropMeta.width / cropMeta.height); }
const up = await sharp(crop).resize(upW, upH, { fit: 'fill' }).png().toBuffer();
// 掩膜（放大后坐标）
const mask = Buffer.alloc(upW * upH * 4);
const sx = upW / box.w, sy = upH / box.h;
const mLeft = maskBox.x * sx, mTop = maskBox.y * sy, mW = maskBox.w * sx, mH = maskBox.h * sy;
for (let y = 0; y < upH; y++) for (let x = 0; x < upW; x++) {
  const i = (y * upW + x) * 4;
  mask[i] = mask[i + 1] = mask[i + 2] = 255;
  const inside = x >= mLeft && x < mLeft + mW && y >= mTop && y < mTop + mH;
  mask[i + 3] = inside ? 0 : 255;
}
const maskPng = await sharp(mask, { raw: { width: upW, height: upH, channels: 4 } }).png().toBuffer();
const prompt = `This is a close-up of a watercolor floral textile painting. A thin transparent strip hides a small part of the painting. Continue the painting naturally across the strip: petals, leaves, specks, brush strokes and paper grain must flow continuously and match the surroundings; keep everything outside the strip identical; no new motifs, no colour shift, no visible seam.`;
const { base, key } = apiConfig(ROOT);
const r = await generateImage({ base, key, model: MODEL, prompt, size: 'auto', imageUrls: [`data:image/png;base64,${up.toString('base64')}`], maskUrl: `data:image/png;base64,${maskPng.toString('base64')}`, label: 'repair-region' });
if (!r.urls.length) throw new Error('no url');
const rawTmp = outPath + '.raw';
await download(r.urls[0], rawTmp);
const fixedCrop = await sharp(rawTmp).removeAlpha().resize(cropMeta.width, cropMeta.height, { fit: 'fill' }).raw().toBuffer({ resolveWithObject: true });
// 羽化贴回（只改 box 内、mask 附近）
const outRaw = Buffer.from(raw.data);
const fw = Buffer.alloc(box.w * box.h);
for (let y = 0; y < box.h; y++) for (let x = 0; x < box.w; x++) {
  const inside = x >= maskBox.x && x < maskBox.x + maskBox.w && y >= maskBox.y && y < maskBox.y + maskBox.h;
  // 到 mask 边界距离
  const d = Math.min(x - maskBox.x, maskBox.x + maskBox.w - 1 - x, y - maskBox.y, maskBox.y + maskBox.h - 1 - y);
  let a = inside ? 1 : Math.max(0, Math.min(1, 1 - (d + FEATHER) / FEATHER));
  fw[y * box.w + x] = a;
}
for (let y = 0; y < box.h; y++) for (let x = 0; x < box.w; x++) {
  const a = fw[y * box.w + x];
  if (a <= 0) continue;
  const dp = ((box.y + y) * W + (box.x + x)) * 3;
  const sp = (y * cropMeta.width + x) * 3;
  for (let c = 0; c < 3; c++) outRaw[dp + c] = Math.round(outRaw[dp + c] * (1 - a) + fixedCrop.data[sp + c] * a);
}
fs.writeFileSync(outPath, await sharp(outRaw, { raw: { width: W, height: H, channels: 3 } }).png().toBuffer());
fs.rmSync(rawTmp, { force: true });
const info = { step: '06-repair-region', image: imgPath, out: outPath, box, maskBox, upscale: [upW, upH], feather: FEATHER, cost_usd: r.cost };
fs.writeFileSync(outPath + '.meta.json', JSON.stringify(info, null, 2));
console.log(JSON.stringify(info));

function parse(argv) { const a = {}; for (let i = 0; i < argv.length; i++) if (argv[i].startsWith('--')) { const k = argv[i].slice(2); const v = argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[++i] : true; a[k] = v; } return a; }
function must(v, n) { if (!v || v === true) { console.error(`缺少参数 ${n}`); process.exit(1); } return v; }
