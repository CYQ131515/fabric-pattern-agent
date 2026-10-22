/* v0.6 主动终止 + 一次四张 · 真浏览器视觉验证（不依赖服务）
   跑法：NODE_PATH=<node-workspace>/node_modules node tools/shot_v06_stop.mjs
   产物写到 OUT（默认 /tmp）：stop_1_thinking / stop_2_four */
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const { chromium } = require('playwright');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..', '..');
const HTML = process.env.PF_CLIENT_HTML
  ? path.resolve(process.env.PF_CLIENT_HTML)
  : path.join(ROOT, '前端', '历史版本', 'PatternFlow花型智造-交互原型v0.6.html');
const OUT = process.env.OUT || '/tmp';
const BASE = process.env.AGENT_BASE || 'http://127.0.0.1:8787';
const GENDIR = path.join(HERE, '..', 'generated');

/* 用刚出的真图当展品；服务已经在跑就直接从服务上取，这样图能真的显示出来 */
const real = fs.existsSync(GENDIR)
  ? fs.readdirSync(GENDIR).filter(f => /\.(jpg|png|webp)$/i.test(f))
      .map(f => ({ f, t: fs.statSync(path.join(GENDIR, f)).mtimeMs }))
      .sort((a, b) => b.t - a.t).slice(0, 4).map(x => x.f)
  : [];
const ONLINE = real.length >= 4;

const IGNORE = /\/api\/|Failed to fetch|net::|ERR_|favicon|load/i;
const errs = [];
const b = await chromium.launch();
const p = await b.newPage({ viewport: { width: 900, height: 820 }, deviceScaleFactor: 2 });
p.on('pageerror', e => { if (!IGNORE.test(e.message)) errs.push('pageerror: ' + e.message); });
p.on('console', m => { if (m.type() === 'error' && !IGNORE.test(m.text())) errs.push('console: ' + m.text()); });

await p.goto(ONLINE ? BASE + '/' : 'file://' + HTML, { waitUntil: 'load' });
await p.waitForTimeout(800);
if (ONLINE) console.log('    从服务取真图：' + real.join(', '));

/* 1) 思考中：等待气泡上挂着「终止」 */
await p.evaluate(() => {
  const Y = window.__YQ;
  Y.S.page = 'designer'; Y.S.agent = 'designer';
  Y.S.chat.designer.length = 0;
  Y.S.credits = 1280;
  Y.push('designer', { me:true, text:'电子游戏元素配淡绿，用在床品上' });
  /* 模拟「请求已经发出去、还在等」的状态：beginRun 本身不发请求，
     真发请求的是 apiChat，所以这里不会真打服务 */
  Y.beginRun('designer', '', 0);
});
await p.waitForTimeout(250);
const box = await p.$('.stopbtn');
console.log('[1] 等待气泡里的终止按钮：', box ? '在' : '不在');
console.log('    WAIT =', await p.evaluate(() => JSON.stringify(
  window.__YQ.WAIT ? { cost: window.__YQ.WAIT.cost } : null)));
await (await p.$('#msgs')).screenshot({ path: OUT + '/stop_1_thinking.png' });

/* 2) 点终止 → 等待撤掉、给出人话交代 */
await p.click('.stopbtn');
await p.waitForTimeout(250);
const after = await p.evaluate(() => ({
  stopLeft: document.querySelectorAll('.stopbtn').length,
  last: (document.querySelector('#msgs .msg:not(.me):last-child .bubble') || {}).textContent,
  credits: window.__YQ.S.credits,
}));
console.log('[2] 终止后：按钮剩 ' + after.stopLeft + ' 个；点数 ' + after.credits);
console.log('    交代：' + (after.last || '').trim());
await (await p.$('#msgs')).screenshot({ path: OUT + '/stop_2_aborted.png' });

/* 3) 一次四张的排布 */
await p.evaluate((names) => {
  const Y = window.__YQ;
  Y.S.chat.designer.length = 0;
  const imgs = (names.length ? names : ['a.jpg', 'b.jpg', 'c.jpg', 'd.jpg']).map((n, i) => ({
    name: n, url: '/api/file/' + n, mime: 'image/jpeg', bytes: 420000 + i * 97000,
  }));
  Y.showGenResult('designer', null, {
    ok: true, images: imgs, cost: 240, model: 'doubao-seedream-5-0',
    gen: { name: 'generate_pattern', args: { n: 4 } }, brief: {},
  }, true, '图出来了。');
}, real);
await p.waitForTimeout(1200);
const st = await p.evaluate(() => ({
  grid: document.querySelectorAll('.genimgs.grid').length,
  n: document.querySelectorAll('.genimgs.grid .genimg').length,
  caps: Array.from(document.querySelectorAll('.genimg .cap')).map(e => e.textContent),
  text: (document.querySelector('#msgs .msg:not(.me):last-child .bubble') || {}).textContent.slice(0, 40),
}));
console.log('[3] 四张排布：grid=' + st.grid + ' 张数=' + st.n);
console.log('    标注：' + st.caps.join(' / '));
console.log('    文案：' + (st.text || '').trim());
await (await p.$('#msgs')).screenshot({ path: OUT + '/stop_3_four.png' });

console.log('控制台错误：', errs.length ? errs : '无');
await b.close();
process.exit(errs.length ? 1 : 0);
