/* 管理后台的可视验收：对着在线服务截图，并抓控制台报错。
   跑法：OUT=<dir> BASE=http://127.0.0.1:8788/ node tools/shot_admin.mjs

   后台页面全是表单和真图，最容易出的是：卡片被长图撑变形、弹窗底部跑出视口、
   知识库检索结果区溢出。jsdom 没有布局引擎，这些它一条都看不出来，必须真浏览器。 */
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { chromium } = require('playwright');

const OUT = process.env.OUT || '/tmp';
const BASE = process.env.BASE || 'http://127.0.0.1:8788/';
const PW = process.env.PW || 'test1234';

const IGNORE = /Failed to fetch|net::|ERR_|favicon|the server responded/i;
const errs = [];
const b = await chromium.launch();
const p = await b.newPage({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 2 });
p.on('pageerror', e => { if (!IGNORE.test(e.message)) errs.push('pageerror: ' + e.message); });
p.on('console', m => { if (m.type() === 'error' && !IGNORE.test(m.text())) errs.push('console: ' + m.text()); });

const shot = async (name) => { await p.screenshot({ path: OUT + '/' + name }); };

await p.goto(BASE, { waitUntil: 'load' });
await p.waitForTimeout(1000);

/* ---------- [0] 登录门 ---------- */
const needAuth = await p.evaluate(() => {
  const m = document.getElementById('authmask');
  return !!m && m.style.display !== 'none';
});
if (needAuth){
  await shot('admin_0_login.png');
  await p.fill('#authpw', PW);
  await p.locator('#authgo').click();
  await p.waitForTimeout(1400);
}
const authed = await p.evaluate(() => window.__ADM.AUTH.authed);
console.log('[0] 登录门：' + (needAuth ? '弹了' : '没弹（可能没设口令）') +
            '，登录后 authed=' + authed);
if (!authed) console.log('    ⚠ 没登进去，后面的截图都会是登录框 —— 用 PW=<口令> 再跑');

/* ---------- [1] 概览 ---------- */
const online = await p.evaluate(() => window.__ADM && window.__ADM.ONLINE);
await shot('admin_1_overview.png');
console.log('[1] 概览：online=' + online);

/* ---------- [2] 模型与密钥 ---------- */
await p.evaluate(() => window.__ADM.go('model'));
await p.waitForTimeout(400);
const keyType = await p.evaluate(() => {
  const el = document.getElementById('f_api_key');
  return el ? el.getAttribute('type') + '|' + el.value : 'none';
});
await shot('admin_2_model.png');
console.log('[2] 模型与密钥：密钥框 type|value = ' + keyType);

/* ---------- [3] 机器人人设：套一个档位 ---------- */
await p.evaluate(() => window.__ADM.go('persona'));
await p.waitForTimeout(500);
await p.evaluate(() => {
  document.getElementById('personabox').value = '（截图演示）我是花型师，负责出花型。';
});
await p.locator('[data-preset="pro"]').first().click();
await p.waitForTimeout(300);
const after = await p.evaluate(() => ({
  style: document.getElementById('stylebox').value.slice(0, 12),
  persona: document.getElementById('personabox').value.slice(0, 12),
}));
await shot('admin_3_persona.png');
console.log('[3] 人设：套档位后 style=' + after.style + ' / persona=' + after.persona);

/* ---------- [4] 知识库：检索测试 ---------- */
await p.evaluate(() => window.__ADM.go('kb'));
await p.waitForTimeout(600);
const hasDoc = await p.locator('[data-kbdel]').count();
await p.fill('#kbq', '起订量');
await p.locator('[data-act="kb-test"]').click();
await p.waitForTimeout(900);
const hasResult = await p.locator('#kbresult .resultbox').count();
await shot('admin_4_kb.png');
console.log('[4] 知识库：资料 ' + hasDoc + ' 篇，检索结果区 ' + hasResult);

/* ---------- [5] 花型库：真图网格 ---------- */
await p.evaluate(() => window.__ADM.go('lib'));
await p.waitForSelector('[data-libopen]', { timeout: 20000 });
await p.waitForTimeout(2200);
const cards = await p.locator('[data-libopen]').count();
const broken = await p.evaluate(() => {
  const imgs = Array.from(document.querySelectorAll('.lib-grid img'));
  return imgs.filter(i => !i.complete || i.naturalWidth === 0).length;
});
/* 卡片高度是否一致（长图撑变形是最典型的布局 bug） */
const heights = await p.evaluate(() => {
  const hs = Array.from(document.querySelectorAll('.lib-card')).map(c => Math.round(c.getBoundingClientRect().height));
  return { min: Math.min(...hs), max: Math.max(...hs), n: hs.length };
});
await shot('admin_5_lib.png');
console.log('[5] 花型库：卡片 ' + cards + ' 张，裂图 ' + broken +
            '，高度 ' + heights.min + '~' + heights.max);

/* ---------- [6] 单张详情弹层 ---------- */
await p.locator('[data-libopen]').first().click();
await p.waitForTimeout(1200);
const flds = await p.locator('[data-libfld]').count();
const overflow = await p.evaluate(() => {
  const m = document.querySelector('.modal');
  return m ? Math.round(m.getBoundingClientRect().bottom - window.innerHeight) : 0;
});
await shot('admin_6_lib_detail.png');
console.log('[6] 详情弹层：可编辑字段 ' + flds + ' 个，底部超出视口 ' + overflow + 'px');

/* ---------- [7] 关弹层 + 扫目录弹窗 ---------- */
await p.evaluate(() => window.__ADM.closeLayer());
await p.waitForTimeout(200);
await p.locator('[data-act="lib-scan"]').click();
await p.waitForTimeout(400);
await shot('admin_7_scan.png');
console.log('[7] 扫目录弹窗');

await p.evaluate(() => window.__ADM.closeLayer());

/* ---------- [8] 会话 ---------- */
await p.evaluate(() => window.__ADM.go('sessions'));
await p.waitForTimeout(700);
const rows = await p.locator('.tbl tbody tr').count();
await shot('admin_8_sessions.png');
console.log('[8] 会话：' + rows + ' 行');

console.log('\nerrors: ' + errs.length + (errs.length ? '\n' + errs.join('\n') : ''));
await b.close();
process.exit(errs.length ? 1 : 0);
