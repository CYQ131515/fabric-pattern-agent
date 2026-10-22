/**
 * 原型 ↔ 本地 Agent 服务 联调断言（jsdom）。
 *
 * 前置：先启动服务（本地规则模式即可）
 *   python3 server.py --port 8787
 *
 * 运行：
 *   NODE_PATH=<node workspace>/node_modules node tools/test_prototype_link.mjs
 */
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

/* ESM 不吃 NODE_PATH，用 CJS require 解析，方便指向托管 node_modules */
const require = createRequire(import.meta.url);
const { JSDOM } = require('jsdom');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const HTML = process.env.PF_CLIENT_HTML
  ? path.resolve(process.env.PF_CLIENT_HTML)
  : path.join(HERE, '..', '..', '前端', '历史版本', 'PatternFlow花型智造-交互原型v0.6.html');
const BASE = process.env.AGENT_BASE || 'http://127.0.0.1:8787';

let pass = 0, fail = 0;
const ok = (cond, name, extra = '') => {
  if (cond) { pass++; console.log('  ✓ ' + name); }
  else { fail++; console.log('  ✗ ' + name + (extra ? ' → ' + extra : '')); }
};
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
/* 轮询等待条件成立。真实大模型响应慢且不稳定，测试里所有「等结果」都应该用它。 */
const waitFor = async (fn, ms = 20000) => {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) {
    let hit = false;
    try { hit = !!fn(); } catch (_) { hit = false; }
    if (hit) return true;
    await wait(300);
  }
  return false;
};

const dom = new JSDOM(fs.readFileSync(HTML, 'utf8'), {
  runScripts: 'dangerously',
  url: BASE + '/',
  pretendToBeVisual: true,
  beforeParse(window) {
    window.fetch = (...args) => fetch(...args);
  },
});
const w = dom.window;
const errs = [];
w.addEventListener('error', (e) => errs.push(String(e.message || e)));

await wait(400);
const Y = w.__YQ;
Y.AGENT_API.base = BASE;
Y.AGENT_API.enabled = true;

console.log('\n[1] 连接自检');
await Y.pingAgent();
await wait(300);
ok(Y.AGENT_API.online === true, '原型能连上本地 Agent 服务', JSON.stringify(Y.AGENT_API));

console.log('\n[2] 设计师对话：需求卡回写 + 候选卡片');
Y.setAgent('designer');
Y.say('想找法式的小碎花，做春夏连衣裙');
/* 服务在线时走真实大模型，一轮对话通常要 10~20s（离线规则则是毫秒级），
   固定 sleep 会误判，这里改成轮询等需求卡落定。 */
await waitFor(() => /法式浪漫/.test(w.document.getElementById('body').textContent), 45000);
const body = w.document.getElementById('body').textContent;
ok(/法式浪漫/.test(body), '需求卡已写入「法式浪漫」');
ok(body.includes('连衣裙'), '需求卡含场景信息');
const briefTxt = w.document.querySelector('.briefbar') ? w.document.querySelector('.briefbar').textContent : '';
ok(briefTxt.includes('法式浪漫'), '顶部需求卡条同步显示', briefTxt.slice(0, 80));
if (Y.S.picks && Y.S.picks.length) {
  ok(w.document.querySelector('[data-act="open-picker"]') !== null,
    '服务返回候选后渲染出「打开花型选择」卡片', String(Y.S.picks.length));
  w.document.querySelector('[data-act="open-picker"]').click();
  await wait(200);
  ok(w.document.querySelectorAll('[data-pick]').length === Y.S.picks.length,
    '选择弹窗用的是服务返回的候选', String(w.document.querySelectorAll('[data-pick]').length));
  w.closeLayer();
}

console.log('\n[3] 服务不可达时自动回落离线规则');
Y.AGENT_API.base = 'http://127.0.0.1:9';
Y.say('来点国风的蓝白配色，做丝巾');
await wait(1500);
const body2 = w.document.getElementById('body').textContent;
ok(body2.includes('青花缠枝') || body2.includes('配色') || body2.includes('元素'),
  '断网时仍能给出离线回复');

console.log('\n[4] 无 JS 运行时错误');
ok(errs.length === 0, 'window error 为空', errs.join(' | '));

console.log('\n通过 %d 项，失败 %d 项', pass, fail);
dom.window.close();
process.exit(fail ? 1 : 0);
