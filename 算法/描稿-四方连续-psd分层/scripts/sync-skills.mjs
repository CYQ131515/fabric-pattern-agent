#!/usr/bin/env node
// skill 双向同步：项目 skills/  <->  用户级 ~/.agents/skills/
// 用法:
//   node scripts/sync-skills.mjs --pull     # 用户级 -> 项目（默认）
//   node scripts/sync-skills.mjs --push     # 项目 -> 用户级（改完项目副本后推送生效）
//   node scripts/sync-skills.mjs --diff     # 只比较差异，不改
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath } from 'node:url';
const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const PROJ = path.join(ROOT, 'skills');
const USER = path.join(os.homedir(), '.agents', 'skills');
const NAMES = ['fabric-seamless-psd', 'seamless-tile-repair', 'print-psd-layering', 'raster-vector-posterization'];
const args = process.argv.slice(2);
const mode = args.includes('--push') ? 'push' : args.includes('--diff') ? 'diff' : 'pull';

function walk(dir, base = dir) {
  const out = [];
  if (!fs.existsSync(dir)) return out;
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) out.push(...walk(p, base));
    else out.push(path.relative(base, p));
  }
  return out.sort();
}
function cmp(a, b) {
  const fa = walk(a), fb = walk(b);
  const set = new Set([...fa, ...fb]);
  const diffs = [];
  for (const f of set) {
    const pa = path.join(a, f), pb = path.join(b, f);
    if (!fs.existsSync(pa)) { diffs.push({ file: f, state: 'missing-in-A' }); continue; }
    if (!fs.existsSync(pb)) { diffs.push({ file: f, state: 'missing-in-B' }); continue; }
    if (fs.readFileSync(pa).equals(fs.readFileSync(pb))) continue;
    diffs.push({ file: f, state: 'different' });
  }
  return diffs;
}

const report = { mode, project: PROJ, user: USER, skills: [] };
for (const n of NAMES) {
  const src = mode === 'push' ? path.join(PROJ, n) : path.join(USER, n);
  const dst = mode === 'push' ? path.join(USER, n) : path.join(PROJ, n);
  if (mode === 'diff') {
    report.skills.push({ name: n, diffs: cmp(path.join(PROJ, n), path.join(USER, n)) });
    continue;
  }
  if (!fs.existsSync(src)) { report.skills.push({ name: n, error: '源目录不存在', src }); continue; }
  fs.mkdirSync(dst, { recursive: true });
  fs.cpSync(src, dst, { recursive: true });
  report.skills.push({ name: n, copied: walk(src).length, from: src, to: dst });
}
console.log(JSON.stringify(report, null, 2));
