#!/usr/bin/env node
// 第 1 步：AI 清洗（把服装照片/低清图 → 平整高清印花稿）
// 用法: node steps/01-clean.mjs --input in.jpg --out work/base.png [--model gpt-image-2.5-sunburst] [--size 9:16] [--prompt-file p.txt] [--note "用户额外要求"]
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import sharp from '../lib/sharp.mjs';
import { apiConfig, generateImage, download } from '../lib/env.mjs';

const HERE = process.cwd();
const ROOT = fileURLToPath(new URL('..', import.meta.url));
const args = parse(process.argv.slice(2));
const input = must(args.input, '--input');
const out = args.out || 'work/base.png';
const model = args.model || 'gpt-image-2.5-sunburst';
const size = args.size || 'auto';
fs.mkdirSync(path.dirname(out), { recursive: true });

const DEFAULT_PROMPT = `Reproduce this printed fabric EXACTLY as a clean flat textile design, like a high-resolution scan of the printed fabric.
Keep every motif identical in shape, position, size and colour: the same flowers, leaves, dots, stems and background texture, the same palette and the same painterly/graphic style.
Do NOT redesign, add, remove, resize or rearrange anything. Remove folds/wrinkles/shadow gradients/garment edges/background and any handwritten watermark; keep the printed texture. Flat 2D. Square or portrait framing.`;

// --note：在保留「忠实复刻」默认提示词的基础上追加用户额外要求（后端描稿用；
// --prompt / --prompt-file 仍然完全覆盖默认提示词，老用法不受影响）。
const note = typeof args.note === 'string' ? args.note.trim() : '';
const prompt = args['prompt-file'] ? fs.readFileSync(args['prompt-file'], 'utf8')
  : (args.prompt || (DEFAULT_PROMPT + (note ? `\n\nAdditional requirement from the user (everything not mentioned must stay faithful): ${note}` : '')));

// 参考图：统一放大到长边 1024 以内，减小上传体积
const meta = await sharp(input).metadata();
const ref = await sharp(input).resize({ width: Math.min(1024, meta.width) }).png().toBuffer();
const refUrl = `data:image/png;base64,${ref.toString('base64')}`;

const t0 = Date.now();
const { base, key } = apiConfig(ROOT);
const r = await generateImage({ base, key, model, prompt, size, imageUrls: [refUrl], label: 'clean' });
if (!r.urls.length) throw new Error('生成结果没有图片 URL');
const bytes = await download(r.urls[0], out);
const secs = ((Date.now() - t0) / 1000).toFixed(1);
const info = { step: '01-clean', model, size, input, out, bytes, note, cost_usd: r.cost, seconds: +secs };
fs.writeFileSync(out + '.meta.json', JSON.stringify(info, null, 2));
console.log(JSON.stringify(info));

function parse(argv) {
  const a = {};
  for (let i = 0; i < argv.length; i++) {
    if (argv[i].startsWith('--')) {
      const k = argv[i].slice(2);
      const v = argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[++i] : true;
      a[k] = v;
    }
  }
  return a;
}
function must(v, name) { if (!v || v === true) { console.error(`缺少参数 ${name}`); process.exit(1); } return v; }
