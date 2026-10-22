/* 用真实浏览器截图核对 v0.4 改动（侧栏品牌/无搜索框、回复带选项按钮、无「本地技能」）
   跑法：BASE=http://127.0.0.1:8795 OUT=/tmp node tools/shot_v04.mjs */
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright');

const BASE = process.env.BASE || 'http://127.0.0.1:8795';
const OUT = process.env.OUT || '/tmp';

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 });
const errs = [];
page.on('pageerror', e => errs.push(String(e)));
page.on('console', m => { if (m.type() === 'error') errs.push('console: ' + m.text()); });

await page.goto(BASE, { waitUntil: 'networkidle' });
await page.waitForTimeout(1200);

// 1) 侧栏
await page.locator('.rail').screenshot({ path: `${OUT}/v04_1_rail.png` });
const brand = (await page.locator('.rail-head .brand .tx b').textContent()).replace(/\s+/g, '');
const hasSearch = await page.locator('#navsearch').count();
console.log('侧栏品牌 =', brand, '| 搜索框数量 =', hasSearch);

// 2) 模型追问 → 选项按钮
const send = async (t) => {
  await page.fill('#input', t);
  await page.press('#input', 'Enter');
  await page.waitForSelector('#msgs .typing', { state: 'detached', timeout: 90000 }).catch(() => {});
  await page.waitForTimeout(600);
};

await send('想要法式的小碎花，做春夏连衣裙');
await page.locator('#msgs').screenshot({ path: `${OUT}/v04_2_ask.png` });
const botMsg = page.locator('#msgs .msg:not(.me)').last();
console.log('追问回复 =', JSON.stringify((await botMsg.innerText()).slice(0, 120)));
console.log('选项按钮 =', JSON.stringify(await botMsg.locator('.opt').allInnerTexts()));

// 3) 补齐配色 → 出候选
await send('奶油白底，蓝白青花的感觉吧');
await page.locator('#msgs').screenshot({ path: `${OUT}/v04_3_result.png` });
const lastBot = page.locator('#msgs .msg:not(.me)').last();
console.log('候选回复片段 =', JSON.stringify((await lastBot.innerText()).slice(0, 200)));
console.log('含"本地技能" =', (await page.locator('#msgs').innerText()).includes('本地技能'));
console.log('含裸露星号 =', (await page.locator('#msgs').innerText()).includes('**'));

// 4) 整页
await page.screenshot({ path: `${OUT}/v04_4_full.png`, fullPage: false });
console.log('页面错误 =', errs.length ? errs : '无');
await browser.close();
