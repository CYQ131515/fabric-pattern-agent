#!/usr/bin/env node
// 把产出归档到项目 deliverables/（用户要求：所有产出必须存在算法目录）
// 用法: node scripts/save-deliverable.mjs <文件...>
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const DELIV = path.join(ROOT, 'deliverables');
const dirs = { psd: 'psd', seamless: 'seamless', preview: 'previews', input: 'inputs' };
function bucket(p) {
  const e = path.extname(p).toLowerCase();
  if (e === '.psd') return dirs.psd;
  if (/-seamless\.png$/i.test(p)) return dirs.seamless;
  return dirs.preview;
}
const files = process.argv.slice(2);
if (!files.length) { console.error('用法: node scripts/save-deliverable.mjs <文件...>'); process.exit(1); }
const saved = [];
for (const f of files) {
  if (!fs.existsSync(f)) { console.error(`跳过（不存在）: ${f}`); continue; }
  const b = bucket(f);
  const dstDir = path.join(DELIV, b);
  fs.mkdirSync(dstDir, { recursive: true });
  const dst = path.join(dstDir, path.basename(f));
  fs.copyFileSync(f, dst);
  saved.push(path.relative(ROOT, dst));
}
console.log(JSON.stringify({ saved, deliverables: path.relative(ROOT, DELIV) }, null, 2));
