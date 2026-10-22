#!/usr/bin/env node
// 第 3 步（审查）：整图接缝检测
// 不是只看外缘！补缝后的"断裂/错位线"会出现在图片内部（例如平移法把原图边缘移到中线），
// 所以这里扫描整幅图，找"贯穿式的直线断裂"：列向/行向梯度在一条线上持续偏高，且长跨度。
// 用法: node steps/03-verify-seam.mjs --image work/seamless.png [--json]
// 退出码: 0=通过, 2=检测到疑似接缝
import sharp from '../lib/sharp.mjs';

const args = parse(process.argv.slice(2));
const img = must(args.image, '--image');
const MAXW = Number(args.width || 1200);

const { data, info } = await sharp(img).removeAlpha().resize(MAXW).grayscale().raw().toBuffer({ resolveWithObject: true });
const W = info.width, H = info.height;
const g = (x, y) => data[y * W + x];

// 列向差分图 Gx[x][y] = |I(x,y)-I(x-1,y)|；行向 Gy[y][x]
const colMean = new Float64Array(W);
const colFrac = new Float64Array(W);   // 高梯度行占比
const rowMean = new Float64Array(H);
const rowFrac = new Float64Array(H);
const TH = 12; // 灰度差阈值（0-255）
for (let x = 1; x < W; x++) {
  let s = 0, c = 0;
  for (let y = 0; y < H; y++) { const d = Math.abs(g(x, y) - g(x - 1, y)); s += d; if (d > TH) c++; }
  colMean[x] = s / H; colFrac[x] = c / H;
}
for (let y = 1; y < H; y++) {
  let s = 0, c = 0;
  for (let x = 0; x < W; x++) { const d = Math.abs(g(x, y) - g(x, y - 1)); s += d; if (d > TH) c++; }
  rowMean[y] = s / H * 0 + s / W; rowFrac[y] = c / W;
}
const med = arr => { const a = Array.from(arr).filter(v => v > 0).sort((p, q) => p - q); return a.length ? a[a.length >> 1] : 0; };
const mad = (arr, m) => { const a = Array.from(arr).filter(v => v > 0).map(v => Math.abs(v - m)).sort((p, q) => p - q); return a.length ? a[a.length >> 1] : 0; };
const medX = med(colMean), madX = mad(colMean, medX);
const medY = med(rowMean), madY = mad(rowMean, medY);

const MIN_SPAN = 0.30;    // 断裂线至少要覆盖 30% 的跨度
const MIN_MEAN = 3.0;     // 平均梯度下限
const K = 3.0;            // 相对中位数的倍数
const lines = [];
for (let x = 2; x < W - 1; x++) {
  const z = (colMean[x] - medX) / (madX * 1.4826 + 1e-6);
  if (colMean[x] >= MIN_MEAN && colFrac[x] >= MIN_SPAN && (z >= K || colMean[x] >= medX * 2.2)) {
    lines.push({ axis: 'vertical', pos: +(x / W).toFixed(3), mean: +colMean[x].toFixed(2), frac: +colFrac[x].toFixed(2), z: +z.toFixed(1) });
  }
}
for (let y = 2; y < H - 1; y++) {
  const z = (rowMean[y] - medY) / (madY * 1.4826 + 1e-6);
  if (rowMean[y] >= MIN_MEAN && rowFrac[y] >= MIN_SPAN && (z >= K || rowMean[y] >= medY * 2.2)) {
    lines.push({ axis: 'horizontal', pos: +(y / H).toFixed(3), mean: +rowMean[y].toFixed(2), frac: +rowFrac[y].toFixed(2), z: +z.toFixed(1) });
  }
}
// 合并相邻候选（同一条断裂会有 1-3 px 宽）
lines.sort((a, b) => (a.axis === b.axis ? a.pos - b.pos : a.axis < b.axis ? -1 : 1));
const merged = [];
for (const l of lines) {
  const p = merged[merged.length - 1];
  if (p && p.axis === l.axis && Math.abs(l.pos - p.pos) < 0.01) {
    if (l.mean > p.mean) Object.assign(p, l);
  } else merged.push({ ...l });
}

// 外缘 wrap 检查（补充，不是唯一依据）
const wrapX = (() => { let s = 0; for (let y = 0; y < H; y++) s += Math.abs(g(0, y) - g(W - 1, y)); return s / H; })();
const wrapY = (() => { let s = 0; for (let x = 0; x < W; x++) s += Math.abs(g(x, 0) - g(x, H - 1)); return s / W; })();
const adjX = (() => { let s = 0, n = 0; for (let y = 0; y < H; y++) for (let x = 1; x < W; x++) { s += Math.abs(g(x, y) - g(x - 1, y)); n++; } return s / n; })();
const adjY = (() => { let s = 0, n = 0; for (let y = 1; y < H; y++) for (let x = 0; x < W; x++) { s += Math.abs(g(x, y) - g(x, y - 1)); n++; } return s / n; })();

const ok = merged.length === 0;
const result = {
  image: img, size: [W, H], ok,
  suspicious_lines: merged.slice(0, 12),
  wrap: { wrapX: +wrapX.toFixed(2), wrapY: +wrapY.toFixed(2), adjX: +adjX.toFixed(3), adjY: +adjY.toFixed(3),
          ratioX: +(wrapX / Math.max(adjX, 0.01)).toFixed(2), ratioY: +(wrapY / Math.max(adjY, 0.01)).toFixed(2) },
  rule: '检测整幅图中贯穿式直线断裂（列/行梯度持续偏高且跨度≥30%），外缘 wrap 仅作补充',
};
if (args.json) console.log(JSON.stringify(result, null, 2));
else { console.log(JSON.stringify(result)); }
process.exit(ok ? 0 : 2);

function parse(argv) { const a = {}; for (let i = 0; i < argv.length; i++) if (argv[i].startsWith('--')) { const k = argv[i].slice(2); const v = argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[++i] : true; a[k] = v; } return a; }
function must(v, n) { if (!v || v === true) { console.error(`缺少参数 ${n}`); process.exit(1); } return v; }
