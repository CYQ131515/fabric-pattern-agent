#!/usr/bin/env node
// 从反馈数据学习：统计「特征簇 × 派发路径」的通过/打回，输出 knowledge/learned.json 与学习报告
// 用法: node scripts/learn.mjs [--min-samples 2]
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const ROOT = path.dirname(fileURLToPath(import.meta.url)) + '/..';
const FB = path.join(ROOT, 'feedback');
const KB = path.join(ROOT, 'knowledge');
const args = parse(process.argv.slice(2));
const MIN_N = Number(args['min-samples'] || 2);
const readJsonl = (p) => fs.existsSync(p) ? fs.readFileSync(p, 'utf8').split('\n').filter(Boolean).map(l => { try { return JSON.parse(l); } catch { return null; } }).filter(Boolean) : [];
const deliveries = readJsonl(path.join(FB, 'deliveries.jsonl'));
const records = readJsonl(path.join(FB, 'records.jsonl'));

// 自动打回的候选也是负样本（knowledge/cases/auto-*）
const casesDir = path.join(KB, 'cases');
const autoCases = fs.existsSync(casesDir)
  ? fs.readdirSync(casesDir).filter(d => d.startsWith('auto-')).map(d => {
      try { return JSON.parse(fs.readFileSync(path.join(casesDir, d, 'case.json'), 'utf8')); } catch { return null; }
    }).filter(Boolean)
  : [];
for (const c of autoCases) {
  const name = String(c.title || '') + ' ' + String((c.tags || []).join(' '));
  const path0 = /fast/i.test(name) ? 'A-fast' : /regenerate/i.test(name) ? 'C-clean+regenerate' : /ai/i.test(name) ? 'B-clean+ai' : 'unknown';
  records.push({ id: c.id, delivery_id: c.id, verdict: 'reject', reason: (c.symptom || '') + ' [auto]', tags: ['auto-rejected', ...(c.tags || [])], features: c.features, chosen_path: path0 });
}
const byId = new Map(deliveries.map(d => [d.delivery_id, d]));

// 特征分箱：把连续特征离散成"图类型簇"
function clusterKey(f) {
  if (!f) return 'unknown';
  const bin = (x, edges) => { const i = edges.findIndex(e => x < e); return i < 0 ? edges.length : i; };
  const lg = bin(f.lowGradFrac ?? 0.5, [0.25, 0.5, 0.75]);      // 0=纹理多 1=中 2=平 3=极平
  const hc = bin(f.hueConc ?? 0.5, [0.5, 0.85]);                 // 0=多色相 1=中 2=单色相
  const ed = bin(f.edgeMean ?? 20, [15, 40]);                    // 0=线少 1=中 2=线多
  return `lg${lg}-hc${hc}-ed${ed}`;
}

const stats = {}; // {cluster: {path: {n, ok, fail, tags:[]}}}
for (const r of records) {
  const d = byId.get(r.delivery_id);
  const f = r.features || d?.features;
  const ck = clusterKey(f);
  const p = r.chosen_path || d?.chosen_path || 'unknown';
  stats[ck] ??= {};
  stats[ck][p] ??= { n: 0, ok: 0, fail: 0, tags: {} };
  stats[ck][p].n++;
  if (r.verdict === 'accept') stats[ck][p].ok++; else stats[ck][p].fail++;
  for (const t of r.tags || []) stats[ck][p].tags[t] = (stats[ck][p].tags[t] || 0) + 1;
}

// Laplace 平滑得分：success rate，样本少时向 0.5 shrink
const out = { updated_at: new Date().toISOString(), min_samples: MIN_N, clusters: {} };
for (const [ck, paths] of Object.entries(stats)) {
  const o = { paths: {}, total: Object.values(paths).reduce((s, x) => s + x.n, 0) };
  for (const [p, s] of Object.entries(paths)) {
    const score = (s.ok + 1) / (s.n + 2);
    o.paths[p] = { n: s.n, ok: s.ok, fail: s.fail, score: +score.toFixed(3), weighted: +(score * Math.min(s.n, 5)).toFixed(3), tags: s.tags };
  }
  // 排序后的推荐顺序（样本≥MIN_N 才生效；否则保持默认）
  o.recommended = o.total >= MIN_N
    ? Object.entries(o.paths).sort((a, b) => b[1].score - a[1].score).map(([p]) => p)
    : null;
  out.clusters[ck] = o;
}

fs.writeFileSync(path.join(KB, 'learned.json'), JSON.stringify(out, null, 2), 'utf8');

// 人可读报告
let md = `# 学习报告（自动生成）

生成时间：${out.updated_at}
最小样本数：${MIN_N}

| 特征簇 | 路径 | 样本 | 通过 | 打回 | 得分 | 高频问题标签 |
| --- | --- | --- | --- | --- | --- | --- |
`;
for (const [ck, o] of Object.entries(out.clusters)) {
  for (const [p, s] of Object.entries(o.paths)) {
    const topTags = Object.entries(s.tags).sort((a, b) => b[1] - a[1]).slice(0, 3).map(([t, n]) => `${t}(${n})`).join(' ');
    md += `| ${ck} | ${p} | ${s.n} | ${s.ok} | ${s.fail} | ${s.score} | ${topTags || '-'} |\n`;
  }
}
md += `
## 推荐派发（样本≥${MIN_N} 的簇）

${Object.entries(out.clusters).filter(([, o]) => o.recommended).map(([ck, o]) => `- ${ck}: ${o.recommended.join(' → ')}`).join('\n') || '- （样本不足，暂无）'}
`;
fs.writeFileSync(path.join(KB, 'LEARNING-REPORT.md'), md, 'utf8');
console.log(JSON.stringify({ clusters: Object.keys(out.clusters), learned_at: out.updated_at, report: 'knowledge/LEARNING-REPORT.md' }, null, 2));
function parse(argv) { const a = {}; for (let i = 0; i < argv.length; i++) if (argv[i].startsWith('--')) { const k = argv[i].slice(2); const v = argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[++i] : true; a[k] = v; } return a; }
