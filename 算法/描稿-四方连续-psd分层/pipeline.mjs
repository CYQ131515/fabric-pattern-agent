#!/usr/bin/env node
// 一键流程（带接缝审查 + 失败回退）：
//   候选A 快速路径：跳过 AI 清洗，确定性补缝 → 整图审查
//        ├─ 通过 → 出色层 PSD
//        └─ 不通过 → 删除候选A产物 → 候选B
//   候选B：AI 清洗 + AI 补缝 → 整图审查
//        ├─ 通过 → 出色层 PSD
//        └─ 不通过 → 删除候选B产物 → 候选C
//   候选C：AI 清洗 + 原生 seamless 重生成 → 整图审查（稀疏线稿/无周期图推荐直接 --seam regenerate）
//        └─ 通过 → 出色层 PSD；仍不通过则中止（不产出文件）
// 用法：node pipeline.mjs --input 样品.jpg --outdir work --height 9382
// 参数：--seam auto|fast|ai|deterministic  --k 8  --group-name Colors
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
const HERE = path.dirname(fileURLToPath(import.meta.url));
const stepPath = (f) => path.join(HERE, 'steps', f);
import sharp from './lib/sharp.mjs';
import { planAttempts } from './lib/strategy.mjs';
import { planRun } from './lib/planner.mjs';

const args = parse(process.argv.slice(2));
const input = args.input;
if (!input || !fs.existsSync(input)) { console.error('缺少 --input <图片>'); process.exit(1); }
const outdir = args.outdir || 'work';
const height = Number(args.height || 9382);
const seamMode = args.seam || 'auto';       // auto=先试快速路径；fast=只快速路径；ai=直接 AI 补缝
const k = String(args.k || 8);
const groupName = args['group-name'] || 'Colors';
const recordCases = args['no-record'] ? false : true;
const usePlanner = args['no-planning'] ? false : true;
const KB = path.join(HERE, 'knowledge');
const DELIV = path.join(HERE, 'deliverables');
const modelClean = args['model-clean'] || 'gpt-image-2.5-sunburst';
const modelSeam = args['model-seam'] || 'gpt-image-2.5-ext';
const size = args.size || 'auto';
fs.mkdirSync(outdir, { recursive: true });

const report = { input, outdir, height, seam_mode: seamMode, started: new Date().toISOString(), steps: [], candidates: [] };
const t = () => Date.now();
async function run({ name, fn }) {
  const t0 = t(); const r = await fn();
  const step = { name, seconds: +((t() - t0) / 1000).toFixed(1), ...r };
  report.steps.push(step); console.log(JSON.stringify(step)); return step;
}
const UV_ARGS = ['run', '--quiet', '--with', 'psd-tools==1.17.4', '--with', 'pillow', '--with', 'numpy', 'python'];
function hasCmd(c) { try { execFileSync('which', [c], { stdio: 'ignore' }); return true; } catch { return false; } }
function runPy(script, argv, extra = []) {
  const useUv = hasCmd('uv');
  const cmd = useUv ? 'uv' : 'python3';
  const all = useUv ? [...UV_ARGS, script, ...argv] : [script, ...argv];
  const r = spawnSync(cmd, all, { encoding: 'utf8' });
  return { status: r.status, stdout: r.stdout || '', stderr: r.stderr || '' };
}
function runNode(stepFile, argv) {
  execFileSync(process.execPath, [stepPath(stepFile), ...argv], { encoding: 'utf8', cwd: HERE });
}
function nodeMeta(p) { return JSON.parse(fs.readFileSync(p + '.meta.json', 'utf8')); }
function rmFiles(...files) { for (const f of files) { try { fs.rmSync(f, { force: true }); } catch {} } }

// 候选被打回：入库（缩略图+审查JSON+元数据），再删除全尺寸产物
async function recordRejected(c) {
  if (!recordCases) return;
  const date = new Date().toISOString().slice(0, 10).replace(/-/g, '');
  const casesDir = path.join(KB, 'cases');
  fs.mkdirSync(casesDir, { recursive: true });
  const existing = fs.readdirSync(casesDir).filter(d => d.startsWith(`auto-${date}-`));
  const id = `auto-${date}-${String(existing.length + 1).padStart(2, '0')}-${c.name}`;
  const dir = path.join(casesDir, id);
  fs.mkdirSync(dir, { recursive: true });
  try {
    if (c.flat && fs.existsSync(c.flat)) await sharp(c.flat).resize({ width: 700 }).png().toFile(path.join(dir, 'rejected-preview.png'));
    if (c.base && fs.existsSync(c.base)) await sharp(c.base).resize({ width: 500 }).png().toFile(path.join(dir, 'base-preview.png'));
    fs.writeFileSync(path.join(dir, 'case.json'), JSON.stringify({
      id, date: new Date().toISOString().slice(0, 10), title: `[自动打回] ${c.name} 未通过接缝审查`,
      source: 'auto-rejected', status: 'open',
      symptom: JSON.stringify(c.review?.detail?.suspicious_lines || []),
      root_cause: '', fix: '', regression: '', artifacts: ['rejected-preview.png', 'base-preview.png'],
      tags: ['auto-rejected', c.name], features: feats,
    }, null, 2));
    fs.writeFileSync(path.join(dir, 'review.json'), JSON.stringify(c.review?.detail || {}, null, 2));
    console.log(JSON.stringify({ step: 'record-case', id, dir }));
  } catch (e) { console.warn('[record-case] 失败:', e.message); }
}

// 接缝审查开关：--no-review / NO_REVIEW=1 时跳过 03/03b（用户要求时用）
const noReview = !!(args['no-review'] || args.review === 'off' || process.env.NO_REVIEW === '1');

// 修复带连续性检查（亮度/颗粒与周围是否一致）
function reviewRepairBand(img, bands) {
  if (noReview) return { ok: true, skipped: true, detail: { reason: '接缝审查已关闭（按用户要求）' } };
  const r = runPy(stepPath('03b-verify-repair.py'), ['--image', img, '--bands', bands]);
  let parsed = null;
  try { parsed = JSON.parse(r.stdout.trim().split('\n').pop()); } catch {}
  return { ok: r.status === 0 && parsed?.ok === true, detail: parsed, exit: r.status };
}

// 整图接缝审查（不只看外缘）
function reviewSeam(img) {
  if (noReview) return { ok: true, skipped: true, detail: { reason: '接缝审查已关闭（按用户要求）' } };
  const r = runPy(stepPath('03-verify-seam.py'), ['--image', img]);
  let parsed = null;
  try { parsed = JSON.parse(r.stdout.trim().split('\n').pop()); } catch {}
  return { ok: r.status === 0 && parsed?.ok === true, detail: parsed, exit: r.status };
}

async function features(p) {
  const { data, info } = await sharp(p).removeAlpha().resize(512).raw().toBuffer({ resolveWithObject: true });
  const W = info.width, H = info.height, N = W * H, ch = info.channels;
  let sat = 0, hx = 0, hy = 0, edges = 0, low = 0;
  const g = (x, y) => { const i = (y * W + x) * ch; return 0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2]; };
  for (let i = 0; i < N; i++) {
    const r = data[i * ch] / 255, gg = data[i * ch + 1] / 255, b = data[i * ch + 2] / 255;
    const mx = Math.max(r, gg, b), mn = Math.min(r, gg, b), d = mx - mn;
    sat += mx ? d / mx : 0;
    let h = 0;
    if (d > 1e-6) { if (mx === r) h = ((gg - b) / d) % 6; else if (mx === gg) h = (b - r) / d + 2; else h = (r - gg) / d + 4; h *= 60; if (h < 0) h += 360; }
    hx += Math.cos(h * Math.PI / 180); hy += Math.sin(h * Math.PI / 180);
  }
  for (let y = 1; y < H - 1; y++) for (let x = 1; x < W - 1; x++) {
    const c = g(x, y), l = Math.abs(2 * c - g(x - 1, y) - g(x + 1, y)) + Math.abs(2 * c - g(x, y - 1) - g(x, y + 1));
    edges += l; if (l < 2) low++;
  }
  return { satMean: +(sat / N).toFixed(3), hueConc: +(Math.sqrt(hx * hx + hy * hy) / N).toFixed(3), edgeMean: +(edges / N).toFixed(2), lowGradFrac: +(low / N).toFixed(3) };
}

const feats = await features(input);
const flatLike = feats.lowGradFrac >= 0.35;   // 大面积平坦/纯色：可尝试跳过 AI 清洗

// ---------- 候选执行器 ----------
async function candidateFast() {
  const name = 'A-fast';
  const base = path.join(outdir, '01-base-fast.png');
  const flat = path.join(outdir, '02-seamless-fast.png');
  const t0 = t();
  const meta = await sharp(input).metadata();
  await sharp(input).resize({ width: Math.min(1024, meta.width) }).png().toFile(base);
  runNode('02-seam-deterministic.mjs', ['--base', base, '--out', flat]);
  const seam = nodeMeta(flat);
  const rev = reviewSeam(flat);
  const seconds = +((t() - t0) / 1000).toFixed(1);
  if (!rev.ok) { rmFiles(base, base + '.meta.json', flat, flat + '.meta.json'); }
  return { name, ok: rev.ok, base, flat, seam, review: rev, seconds, cost_usd: 0 };
}
async function candidateCleanSeam(kind, P = {}) {
  const name = kind === 'ai' ? 'B-clean+ai' : 'B-clean+det';
  const base = path.join(outdir, '01-base.png');
  const flat = path.join(outdir, '02-seamless.png');
  const t0 = t();
  const c = await run({ name: '01-clean', fn: async () => {
    runNode('01-clean.mjs', ['--input', input, '--out', base, '--model', modelClean, '--size', size]);
    return nodeMeta(base);
  }});
  const cost = c.cost_usd || 0;
  if (kind === 'ai') {
    await run({ name: '02-seam-ai', fn: async () => {
      runNode('02-seam-ai.mjs', ['--base', base, '--out', flat, '--model', modelSeam, '--size', size, ...(P.band ? ['--band', String(P.band)] : []), ...(P.feather ? ['--feather', String(P.feather)] : [])]);
      // 修补带后处理（L39）：wrap 强制还原 + AI 贡献收敛 + 带均值匹配。
      // 密集大花/高对比图不加这步，03/03b 基本必挂（2026-10-20 蓝罂粟实测）。
      if (fs.existsSync(stepPath('09-seam-postfix.py'))) {
        const B0 = Number(P.band || 140), F0 = Number(P.feather || 20);
        const pf = runPy(stepPath('09-seam-postfix.py'),
          ['--base', base, '--ai', flat, '--out', flat, '--band', String(B0 + 2 * F0 + 2)]);
        if (pf.status !== 0) throw new Error('修补带后处理失败: ' + (pf.stderr || '').slice(0, 200));
        console.log(JSON.stringify({ step: '09-seam-postfix', ok: true }));
      }
      return nodeMeta(flat);
    }});
  } else {
    await run({ name: '02-seam-deterministic', fn: async () => {
      runNode('02-seam-deterministic.mjs', ['--base', base, '--out', flat]);
      return nodeMeta(flat);
    }});
  }
  const seam = nodeMeta(flat);
  let rev = reviewSeam(flat);
  if (rev.ok && kind === 'ai') {
    const m = await sharp(flat).metadata();
    const B = Number(P.band || 140), F = Number(P.feather || 20);
    const bands = `v:${Math.round(m.width / 2)},${B + 2 * F};h:${Math.round(m.height / 2)},${B + 2 * F}`;
    const rb = reviewRepairBand(flat, bands);
    if (!rb.ok) { rev = { ok: false, detail: { reason: 'repair-band-visible', repair: rb.detail, seam: rev.detail } }; }
  }
  const seconds = +((t() - t0) / 1000).toFixed(1);
  const totalCost = cost + (seam.cost_usd || 0);
  if (!rev.ok) { rmFiles(base, base + '.meta.json', flat, flat + '.meta.json'); }
  return { name, ok: rev.ok, base, flat, seam, review: rev, seconds, cost_usd: +totalCost.toFixed(5) };
}
async function candidateTileMulti(P = {}) {
  const name = 'D-clean+tile-multi';
  const base = path.join(outdir, '01-base.png');
  const flat = path.join(outdir, '02-seamless.png');
  const t0 = t();
  const c = nodeMetaExists(base) ? { cost_usd: 0 } : await run({ name: '01-clean', fn: async () => {
    runNode('01-clean.mjs', ['--input', input, '--out', base, '--model', modelClean, '--size', size]);
    return nodeMeta(base);
  }});
  let seam = null;
  try {
    await run({ name: '02-seam-tile-multi', fn: async () => {
      runNode('02-seam-tile-multi.mjs', ['--base', base, '--out', flat, '--model', modelSeam === 'gpt-image-2.5-ext' ? 'gpt-image-2.5-sunburst' : modelSeam, '--samples', String(P.samples || args['multi-samples'] || 4)]);
      return nodeMeta(flat);
    }});
    seam = nodeMeta(flat);
  } catch (e) { seam = { ok: false, error: String(e.message).slice(0, 200) }; }
  const rev = seam?.ok ? reviewSeam(flat) : { ok: false, detail: { error: seam?.error || 'no tileable sample' } };
  const seconds = +((t() - t0) / 1000).toFixed(1);
  if (!rev.ok) { rmFiles(base, base + '.meta.json', flat, flat + '.meta.json'); }
  return { name, ok: rev.ok, base, flat, seam, review: rev, seconds, cost_usd: +(c.cost_usd || 0) + (seam?.cost_usd || 0) };
}
async function candidateRegenerate() {
  const name = 'C-clean+regenerate';
  const base = path.join(outdir, '01-base.png');
  const flat = path.join(outdir, '02-seamless.png');
  const t0 = t();
  const c = nodeMetaExists(base) ? { cost_usd: 0 } : await run({ name: '01-clean', fn: async () => {
    runNode('01-clean.mjs', ['--input', input, '--out', base, '--model', modelClean, '--size', size]);
    return nodeMeta(base);
  }});
  await run({ name: '02-seam-regenerate', fn: async () => {
    runNode('02-seam-regenerate.mjs', ['--base', base, '--out', flat, '--model', modelSeam, '--size', '1:1']);
    return nodeMeta(flat);
  }});
  const seam = nodeMeta(flat);
  const rev = reviewSeam(flat);
  const seconds = +((t() - t0) / 1000).toFixed(1);
  if (!rev.ok) { rmFiles(base, base + '.meta.json', flat, flat + '.meta.json'); }
  return { name, ok: rev.ok, base, flat, seam, review: rev, seconds, cost_usd: +(c.cost_usd || 0) + (seam.cost_usd || 0) };
}
function nodeMetaExists(p) { return fs.existsSync(p + '.meta.json'); }

// ---------- 选择路径：planner（LLM，读经验库）优先，失败回退先验规则 ----------
let plannerResult = null;
if (usePlanner && seamMode === 'auto') {
  try {
    const { apiConfig } = await import('./lib/env.mjs');
    const { base: apiBase, key } = apiConfig(path.join(HERE, '.'));
    plannerResult = await planRun({ feats, apiBase, key });
    console.log(JSON.stringify({ step: 'planner', ...plannerResult }));
  } catch (e) { console.warn('[planner] 失败，回退 rules:', e.message); }
}
if (plannerResult?.abort) {
  report.finished = new Date().toISOString();
  report.planner = plannerResult;
  report.error = `planner 拒单：${plannerResult.rationale || '现有方法均不适用'}`;
  fs.writeFileSync(path.join(outdir, 'run-report.json'), JSON.stringify(report, null, 2));
  console.error(report.error);
  process.exit(4);
}
// dry-run：只做特征+planner+策略，不花钱
if (args['dry-run']) {
  const sp = planAttempts(feats, seamMode, flatLike);
  report.planner = plannerResult; report.strategy = sp;
  report.features = feats; report.dry_run = true;
  report.attempts = plannerResult?.order?.length ? plannerResult.order : sp.attempts;
  fs.writeFileSync(path.join(outdir, 'run-report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ dry_run: true, cluster: sp.cluster, feats, planner: plannerResult, attempts: report.attempts }, null, 2));
  process.exit(0);
}
const strategyPlan = planAttempts(feats, seamMode, flatLike);
const attempts = plannerResult?.order?.length ? plannerResult.order.filter(x => ['fast','det','ai','regenerate','tile-multi'].includes(x)) : strategyPlan.attempts;
const planParams = plannerResult?.params || {};
report.strategy = strategyPlan;
report.planner = plannerResult;
report.attempts = attempts;
let chosen = null;
for (const kind of attempts) {
  let c;
  if (kind === 'fast') c = await candidateFast();
  else if (kind === 'ai') c = await candidateCleanSeam('ai', planParams);
  else if (kind === 'det') c = await candidateCleanSeam('det', planParams);
  else if (kind === 'tile-multi') c = await candidateTileMulti(planParams);
  else c = await candidateRegenerate();
  report.candidates.push({ name: c.name, ok: c.ok, review: c.review, seam: c.seam, seconds: c.seconds, cost_usd: c.cost_usd });
  if (c.ok) { chosen = c; break; }
  await recordRejected(c);
  console.warn(`[审查] ${c.name} 未通过，案例已入库，产物已删除，尝试下一条路径`);
}
if (!chosen) {
  report.finished = new Date().toISOString();
  report.error = '三条路径均未通过接缝审查，未生成 PSD';
  fs.writeFileSync(path.join(outdir, 'run-report.json'), JSON.stringify(report, null, 2));
  console.error(report.error);
  process.exit(3);
}

// ---------- 出色层 PSD（每层保留原始像素；背景根层 + Colors 分组）----------
const tag = path.basename(input).replace(/\.[^.]+$/, '');
fs.mkdirSync(path.join(DELIV, 'psd'), { recursive: true });
fs.mkdirSync(path.join(DELIV, 'seamless'), { recursive: true });
const psdOut = path.join(outdir, `${tag}-colorlayers-${height}.psd`);
const psdDeliver = path.join(DELIV, 'psd', `${tag}-colorlayers-${height}.psd`);
const seamlessDeliver = path.join(DELIV, 'seamless', `${tag}-seamless.png`);
const layerStep = await run({ name: '04-layers-color', fn: async () => {
  const r = runPy(stepPath('04-layers-color.py'), [chosen.flat, psdOut, '--height', String(height), '--k', k, '--group-name', groupName]);
  if (r.status !== 0) throw new Error(`色层 PSD 生成失败: ${r.stderr.slice(0, 400)}`);
  return JSON.parse(r.stdout.trim().split('\n').pop());
}});
report.chosen_path = chosen.name;
report.finished = new Date().toISOString();
report.total_seconds = +(report.steps.reduce((s, x) => s + x.seconds, 0)).toFixed(1);
report.total_cost_usd = +(report.steps.reduce((s, x) => s + (x.cost_usd || 0), 0)).toFixed(5);
report.features = feats;
// 交付物归档：PSD + 无缝图复制到项目 deliverables/（用户要求：产出必须存算法目录）
try { fs.copyFileSync(psdOut, psdDeliver); } catch (e) { console.error('归档 PSD 失败:', e.message); }
try { fs.copyFileSync(chosen.flat, seamlessDeliver); } catch (e) { console.error('归档无缝图失败:', e.message); }
report.outputs = [
  { file: psdOut, layers: layerStep.layers.length, size: layerStep.size, structure: 'Background(root) + Colors(group)' },
  { file: psdDeliver, note: '归档副本（deliverables/psd）' },
  { file: seamlessDeliver, note: '无缝图（deliverables/seamless）' }
];
fs.writeFileSync(path.join(outdir, 'run-report.json'), JSON.stringify(report, null, 2));
// 写交付记录（待人工/用户评判），反馈闭环的输入
try {
  const fbDir = path.join(HERE, 'feedback');
  fs.mkdirSync(fbDir, { recursive: true });
  const deliveryId = `d-${Date.now()}`;
  const delivery = {
    delivery_id: deliveryId, ts: new Date().toISOString(),
    input, features: feats, strategy: plan, chosen_path: chosen.name,
    review: { ok: chosen.review?.ok, reviewed: !chosen.review?.skipped, reason: chosen.review?.detail?.reason || '', lines: chosen.review?.detail?.suspicious_lines || [] },
    candidates: report.candidates.map(c => ({ name: c.name, ok: c.ok })),
    outputs: report.outputs,
  };
  fs.appendFileSync(path.join(fbDir, 'deliveries.jsonl'), JSON.stringify(delivery) + '\n', 'utf8');
  report.delivery_id = deliveryId;
  console.log(JSON.stringify({ step: 'feedback:delivery', delivery_id: deliveryId, hint: 'node scripts/feedback.mjs --delivery latest --verdict accept|reject --reason ...' }));
} catch (e) { console.warn('[feedback] 记录失败:', e.message); }

console.log('=== 完成 ===');
console.log(JSON.stringify({ chosen: chosen.name, review: chosen.review.ok, reviewed: !chosen.review?.skipped, total_seconds: report.total_seconds, total_cost_usd: report.total_cost_usd, output: psdOut }, null, 2));

function parse(argv) { const a = {}; for (let i = 0; i < argv.length; i++) if (argv[i].startsWith('--')) { const k = argv[i].slice(2); const v = argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[++i] : true; a[k] = v; } return a; }
