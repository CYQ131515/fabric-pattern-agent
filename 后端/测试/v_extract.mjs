#!/usr/bin/env node
/* 从原型 HTML 里抽出 <script> 正文，写成临时 .js 供 `node --check` 做语法检查。

用法：node tools/v_extract.mjs <原型.html> [输出.js]
默认输出 /tmp/v_<名字>.js
*/
import fs from 'node:fs';
import path from 'node:path';

const html = process.argv[2];
const out = process.argv[3] || ('/tmp/v_' + path.basename(html, '.html') + '.js');
if (!html) { console.error('用法：node tools/v_extract.mjs <原型.html>'); process.exit(1); }

const src = fs.readFileSync(html, 'utf8');
const blocks = [...src.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
if (!blocks.length) { console.error('这个 HTML 里没有 script 块'); process.exit(1); }

// 有多个 script 时全拼起来（按出现顺序），保持行号大致可对上
const js = blocks.join('\n/* ---- 下一个 script 块 ---- */\n');
fs.writeFileSync(out, js, 'utf8');
console.error('抽出 %d 个 script 块 → %s（%d 行）', blocks.length, out, js.split('\n').length);
console.log(out);
