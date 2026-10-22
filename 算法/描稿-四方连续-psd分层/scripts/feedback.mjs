#!/usr/bin/env node
// 人工/用户反馈录入：accept（通过）或 reject（打回）
// 用法:
//   node scripts/feedback.mjs --list
//   node scripts/feedback.mjs --delivery <delivery_id|latest> --verdict reject \
//        --reason "..." --tags band-boundary,lineart [--screenshot 图.png] [--case case-xxx]
// reject 会同时创建 knowledge/cases/ 案例（case.json + notes.md，截图缩略图）
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const ROOT = path.dirname(fileURLToPath(import.meta.url)) + '/..';
const FB = path.join(ROOT, 'feedback');
const KB = path.join(ROOT, 'knowledge');
fs.mkdirSync(FB, { recursive: true });
const args = parse(process.argv.slice(2));
const delivPath = path.join(FB, 'deliveries.jsonl');
const recPath = path.join(FB, 'records.jsonl');

function readJsonl(p) {
  if (!fs.existsSync(p)) return [];
  return fs.readFileSync(p, 'utf8').split('\n').filter(Boolean).map(l => { try { return JSON.parse(l); } catch { return null; } }).filter(Boolean);
}
function appendJsonl(p, obj) { fs.appendFileSync(p, JSON.stringify(obj) + '\n', 'utf8'); }

if (args.list) {
  const ds = readJsonl(delivPath);
  const rs = readJsonl(recPath);
  const judged = new Set(rs.map(r => r.delivery_id));
  console.table(ds.map(d => ({ id: d.delivery_id, time: d.ts, path: d.chosen_path, file: (d.outputs?.[0]?.file || '').split('/').pop(), status: judged.has(d.delivery_id) ? 'judged' : 'PENDING' })));
  process.exit(0);
}
if (!args.delivery || !args.verdict) { console.error('用法: --delivery <id|latest> --verdict accept|reject [--reason ...] [--tags a,b] [--screenshot 图]'); process.exit(1); }
const ds = readJsonl(delivPath);
let d = null;
if (args.delivery === 'latest') d = ds[ds.length - 1];
else d = ds.find(x => x.delivery_id === args.delivery) || null;
if (!d) { console.error('找不到交付记录:', args.delivery); process.exit(1); }
const verdict = String(args.verdict);
if (!['accept', 'reject'].includes(verdict)) { console.error('verdict 只能是 accept / reject'); process.exit(1); }
const tags = args.tags ? String(args.tags).split(',').map(s => s.trim()).filter(Boolean) : [];
const rec = {
  id: `fb-${Date.now()}`, delivery_id: d.delivery_id, ts: new Date().toISOString(),
  verdict, reason: args.reason || '', tags, screenshot: args.screenshot ? path.resolve(args.screenshot) : null,
  features: d.features, chosen_path: d.chosen_path, strategy: d.strategy,
  outputs: (d.outputs || []).map(o => o.file),
};
appendJsonl(recPath, rec);
console.log(JSON.stringify({ recorded: rec.id, delivery: d.delivery_id, verdict, tags }));

// reject → 自动建案例 + 进回归集
if (verdict === 'reject') {
  const date = new Date().toISOString().slice(0, 10).replace(/-/g, '');
  const casesDir = path.join(KB, 'cases');
  fs.mkdirSync(casesDir, { recursive: true });
  const existing = fs.readdirSync(casesDir).filter(x => x.startsWith(`case-${date}-`));
  const id = `case-${date}-${String(existing.length + 1).padStart(2, '0')}`;
  const dir = path.join(casesDir, id);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, 'case.json'), JSON.stringify({
    id, date: new Date().toISOString().slice(0, 10), title: `[用户打回] ${args.reason || (d.chosen_path + ' 输出')}`,
    source: 'user-feedback', status: 'open', symptom: args.reason || '',
    root_cause: '', fix: '', regression: '', tags,
    delivery_id: d.delivery_id, chosen_path: d.chosen_path, features: d.features,
    artifacts: [], file: (d.outputs?.[0]?.file) || '',
  }, null, 2), 'utf8');
  fs.writeFileSync(path.join(dir, 'notes.md'), `# ${args.reason || '用户打回'}

- 日期：${new Date().toISOString().slice(0, 10)}
- 来源：user-feedback ｜ 状态：open
- 交付：${d.delivery_id}（路径 ${d.chosen_path}）
- 特征：${JSON.stringify(d.features)}

## 现象（用户反馈）
${args.reason || '(未填写)'}

## 根因
(待定位)

## 修复 / 待办
(待修复)

## 标签
${tags.join(', ')}
`, 'utf8');
  console.log(JSON.stringify({ case: id, dir }));
}
function parse(argv) { const a = {}; for (let i = 0; i < argv.length; i++) if (argv[i].startsWith('--')) { const k = argv[i].slice(2); const v = argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[++i] : true; a[k] = v; } return a; }
