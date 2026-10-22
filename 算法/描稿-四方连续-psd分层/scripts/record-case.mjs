#!/usr/bin/env node
// 记录一个经验案例（用户指出的问题 / 回归问题 / 手动补录）
// 用法:
//   node scripts/record-case.mjs --title "现象一句话" --source user-reported --status open \
//     --symptom "..." --root-cause "..." --fix "..." [--regression "..."] \
//     [--input 图.png] [--rejected 图.png] [--tags band-boundary,lineart]
// 会自动：建 knowledge/cases/case-<日期>-<序号>/、写 case.json 与 notes.md、更新 INDEX.md
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const ROOT = path.dirname(fileURLToPath(import.meta.url)) + '/..';
const KB = path.join(ROOT, 'knowledge');
const args = parse(process.argv.slice(2));
if (!args.title) { console.error('缺少 --title'); process.exit(1); }
const date = new Date().toISOString().slice(0, 10);
const casesDir = path.join(KB, 'cases');
fs.mkdirSync(casesDir, { recursive: true });
const existing = fs.readdirSync(casesDir).filter(d => d.startsWith(`case-${date.replace(/-/g, '')}-`));
const id = `case-${date.replace(/-/g, '')}-${String(existing.length + 1).padStart(2, '0')}`;
const dir = path.join(casesDir, id);
fs.mkdirSync(dir, { recursive: true });
const artifacts = [];
function copyThumb(src, name) {
  if (!src || !fs.existsSync(src)) return;
  const out = path.join(dir, name);
  const sharp = requireSharp();
  sharp(src).resize({ width: 700 }).png().toFile(out).then(() => artifacts.push(path.basename(out)));
}
function requireSharp() {
  const { createRequire } = require('node:module');
  const req = createRequire(import.meta.url);
  for (const spec of [process.env.SHARP_PATH, 'sharp']) { if (!spec) continue; try { return req(spec); } catch {} }
  throw new Error('未找到 sharp');
}
const meta = {
  id, date,
  title: args.title,
  source: args.source || 'user-reported',
  status: args.status || 'open',
  symptom: args.symptom || '',
  root_cause: args['root-cause'] || '',
  fix: args.fix || '',
  regression: args.regression || '',
  artifacts: [],
  tags: args.tags ? String(args.tags).split(',').map(s => s.trim()) : [],
};
fs.writeFileSync(path.join(dir, 'case.json'), JSON.stringify(meta, null, 2), 'utf8');
fs.writeFileSync(path.join(dir, 'notes.md'), `# ${meta.title}

- 日期：${date}
- 来源：${meta.source} ｜ 状态：${meta.status}

## 现象
${meta.symptom || '(待补)'}

## 根因
${meta.root_cause || '(待定位)'}

## 修复 / 待办
${meta.fix || '(待补)'}

## 回归验证
${meta.regression || '(待补)'}

## 标签
${meta.tags.join(', ') || '-'}
`, 'utf8');
// 缩略图（≤700px，避免占用空间；失败产物全尺寸在录入后应删除）
const jobs = [];
if (args.input) jobs.push(copyThumb(args.input, 'input.png'));
if (args.rejected) jobs.push(copyThumb(args.rejected, 'rejected.png'));
Promise.all(jobs).then(() => {
  meta.artifacts = artifacts;
  fs.writeFileSync(path.join(dir, 'case.json'), JSON.stringify(meta, null, 2), 'utf8');
  // 更新 INDEX.md
  const idx = path.join(KB, 'INDEX.md');
  let s = fs.existsSync(idx) ? fs.readFileSync(idx, 'utf8') : '# 案例索引\n\n| ID | 日期 | 状态 | 标题 |\n| --- | --- | --- | --- |\n';
  s = s.trimEnd() + `\n| ${id} | ${date} | ${meta.status} | ${meta.title} |\n`;
  fs.writeFileSync(idx, s, 'utf8');
  console.log(JSON.stringify({ id, dir, artifacts: meta.artifacts }));
}).catch(e => { console.error('记录失败（案例目录已创建）:', e.message); process.exit(1); });

function parse(argv) { const a = {}; for (let i = 0; i < argv.length; i++) if (argv[i].startsWith('--')) { const k = argv[i].slice(2); const v = argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[++i] : true; a[k] = v; } return a; }
