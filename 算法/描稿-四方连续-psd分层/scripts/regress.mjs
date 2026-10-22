#!/usr/bin/env node
// 回归验证：跑 knowledge/regression.json 里的全部图，检查审查器结论是否符合预期。
// 用法: node scripts/regress.mjs
// 退出码: 0=全部通过；1=有不符合（红）
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
const ROOT = path.dirname(fileURLToPath(import.meta.url)) + '/..';
const KB = path.join(ROOT, 'knowledge');
const reg = JSON.parse(fs.readFileSync(path.join(KB, 'regression.json'), 'utf8'));
const py = path.join(ROOT, 'steps', '03-verify-seam.py');
const hasUv = (() => { try { return spawnSync('which', ['uv']).status === 0; } catch { return false; } })();
function review(img) {
  const args = hasUv
    ? ['run', '--quiet', '--with', 'pillow', '--with', 'numpy', 'python', py, '--image', img]
    : ['python3', py, '--image', img];
  const r = spawnSync(hasUv ? 'uv' : 'python3', args, { encoding: 'utf8' });
  try { return JSON.parse(r.stdout.trim().split('\n').pop()); } catch { return { ok: false, parse_error: true, stderr: r.stderr }; }
}
let bad = 0;
const rows = [];
for (const c of reg.cases) {
  const img = path.resolve(KB, c.image);
  if (!fs.existsSync(img)) { rows.push({ ...c, actual: 'MISSING', pass: false }); bad++; continue; }
  const res = review(img);
  let actual = res.ok ? 'pass' : 'fail';
  if (actual === 'pass' && c.bands) {
    const pyB = path.join(ROOT, 'steps', '03b-verify-repair.py');
    const rb = spawnSync('uv', ['run', '--quiet', '--with', 'pillow', '--with', 'numpy', 'python', pyB, '--image', img, '--bands', c.bands], { encoding: 'utf8' });
    actual = rb.status === 0 ? 'pass' : 'fail';
  }
  const pass = actual === c.expect;
  if (!pass) bad++;
  rows.push({ id: c.id, case: c.case, image: path.basename(img), expect: c.expect, actual, pass, lines: (res.suspicious_lines || []).slice(0, 2) });
}
console.log(JSON.stringify({ summary: { total: rows.length, failed: bad }, rows }, null, 2));
// 回归红时把摘要写回 knowledge/regression-result.json，便于 agent 感知
fs.writeFileSync(path.join(KB, 'regression-result.json'), JSON.stringify({ ran_at: new Date().toISOString(), failed: bad, rows }, null, 2));
process.exit(bad === 0 ? 0 : 1);
