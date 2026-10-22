/* v0.6 生图链路真机验证：真浏览器 + 真对话模型 + mock 生图（无 Key 也能跑通链路）
   跑法：BASE=http://127.0.0.1:8813 OUT=/tmp node tools/shot_v06_gen.mjs */
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright');

const BASE = process.env.BASE || 'http://127.0.0.1:8813';
const OUT = process.env.OUT || '/tmp';
const errs = [];

const b = await chromium.launch();
const p = await b.newPage({ viewport: { width: 1440, height: 960 }, deviceScaleFactor: 2 });
p.on('pageerror', e => errs.push('pageerror: ' + e.message));
p.on('console', m => { if (m.type() === 'error') errs.push('console: ' + m.text()); });
p.on('requestfailed', r => errs.push('reqfail: ' + r.url() + ' ' + (r.failure() || {}).errorText));

await p.goto(BASE + '/', { waitUntil: 'networkidle' });
await p.waitForTimeout(1200);
const st = await p.locator('.hintline').first().innerText();
console.log('连接状态：', st.replace(/\n/g, ' | '));

// 一句话把四维说全，避免模型先追问配色
await p.fill('#input', '帮我生成一块面料花型：法式浪漫风格，花卉元素，蓝白青花配色，用在女装连衣裙上');
await p.click('[data-act="send"]');

console.log('等模型提方案…');
await p.waitForSelector('.confirm-card', { timeout: 120000 });
const cardTxt = (await p.locator('.confirm-card').innerText()).replace(/\n+/g, ' / ');
console.log('确认卡片：', cardTxt);
await p.locator('#msgs .msg').last().screenshot({ path: OUT + '/v06_1_confirm.png' });

console.log('点确认生成…');
await p.click('[data-act="confirm-gen"]');
// 成功出图 / 失败给提示，两条路都要能走完（真接口没开通时走后者）
const got = await Promise.race([
  p.waitForSelector('.genimg', { timeout: 150000 }).then(() => 'image'),
  p.waitForFunction(() => {
    const t = document.querySelector('#msgs') || {};
    return (t.textContent || '').includes('生图没跑通');
  }, { timeout: 150000 }).then(() => 'error'),
]).catch(() => 'timeout');
await p.waitForTimeout(900);
await p.locator('#msgs .msg').last().screenshot({ path: OUT + '/v06_2_generated.png' });

if (got === 'error' || got === 'timeout') {
  const txt = (await p.locator('#msgs .msg').last().innerText()).replace(/\n+/g, ' / ');
  console.log('未出图（%s），界面提示：%s', got, txt.slice(0, 400));
  console.log('控制台错误：', errs.length ? errs : '无');
  await b.close();
  process.exit(errs.length ? 1 : 0);
}
const imgSrc = await p.locator('.genimg img').first().getAttribute('src');
const naturalW = await p.locator('.genimg img').first().evaluate(e => e.naturalWidth);
console.log('生成图 src：', imgSrc, '| 实际加载宽度：', naturalW);

console.log('改配色 → 大地色系…');
await p.click('[data-act="ed:dim:color"]');
await p.waitForTimeout(300);
await p.locator('#msgs .msg').last().screenshot({ path: OUT + '/v06_3_editmenu.png' });
await p.click('[data-act="ed:do:color:大地色系"]');
await p.waitForTimeout(1500);
await p.waitForSelector('.edit-bar', { timeout: 120000 });
await p.waitForTimeout(800);
const count = await p.locator('.genimg').count();
console.log('当前对话里的生成图数量：', count);
await p.locator('#msgs').screenshot({ path: OUT + '/v06_4_edited.png' });

const credits = await p.evaluate(() => window.__YQ.S.credits);
console.log('剩余点数：', credits);
console.log('控制台错误：', errs.length ? errs : '无');
await b.close();
process.exit(errs.length ? 1 : 0);
