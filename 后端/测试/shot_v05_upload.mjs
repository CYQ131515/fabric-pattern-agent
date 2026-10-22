/* v0.5 上传识图：真实浏览器 + 真实模型跑完整链路并截图
   跑法：BASE=http://127.0.0.1:8802 IMG=图片路径 node tools/shot_v05_upload.mjs */
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright');

const BASE = process.env.BASE || 'http://127.0.0.1:8802';
const IMG = process.env.IMG || 'tools/samples/小碎花测试图.png';

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 960 }, deviceScaleFactor: 2 });
const errs = [];
page.on('pageerror', e => errs.push(String(e)));
page.on('console', m => { if (m.type() === 'error') errs.push('console: ' + m.text()); });

await page.goto(BASE, { waitUntil: 'networkidle' });
await page.waitForTimeout(1000);
await page.evaluate(() => window.__YQ.setAgent('designer'));

const settle = async (label) => {
  await page.waitForFunction(() => !document.querySelector('#msgs .typing'), { timeout: 180000 })
    .catch(() => console.log('  ! 等待超时：' + label));
  await page.waitForTimeout(500);
};
const brief = () => page.locator('.briefbar').innerText().catch(() => '(无需求卡)');
const lastBot = () => page.locator('#msgs .msg:not(.me)').last();

console.log('— 上传 —');
const chooser = page.waitForEvent('filechooser');
await page.click('[data-act="upload"]');
(await chooser).setFiles(IMG);
await page.waitForSelector('.pending', { timeout: 10000 });
console.log('待发区：', (await page.locator('.pending .meta').innerText()).replace(/\n/g, ' | '));
await page.locator('.chat').screenshot({ path: '/tmp/v05_1_pending.png' });

console.log('— 发送并识图 —');
const t0 = Date.now();
await page.click('[data-act="send"]');
await settle('识图');
console.log('耗时 %ss', ((Date.now() - t0) / 1000).toFixed(1));
console.log('需求卡：', (await brief()).replace(/\n/g, ' | '));
console.log('识图标签：', (await page.locator('#msgs .tagrow').last().innerText().catch(() => '(无)')).replace(/\n/g, ' '));
console.log('回复：', JSON.stringify((await lastBot().innerText()).slice(0, 260)));
await page.locator('#msgs').screenshot({ path: '/tmp/v05_2_vision.png' });

console.log('— 点选项按钮补齐风格 —');
const opt0 = page.locator('#msgs .msg:not(.me)').last().locator('.opt').first();
const optTxt = await opt0.innerText().catch(() => '');
if (optTxt){
  console.log('点击：', JSON.stringify(optTxt));
  await opt0.click();
  await settle('补风格');
  console.log('需求卡：', (await brief()).replace(/\n/g, ' | '));
  console.log('回复：', JSON.stringify((await lastBot().innerText()).slice(0, 260)));
}

console.log('— 再点一次补齐场景 —');
const opt2 = page.locator('#msgs .msg:not(.me)').last().locator('.opt').first();
const opt2Txt = await opt2.innerText().catch(() => '');
if (opt2Txt && !/打开花型选择/.test(opt2Txt)){
  console.log('点击：', JSON.stringify(opt2Txt));
  await opt2.click();
  await settle('补场景');
  console.log('需求卡：', (await brief()).replace(/\n/g, ' | '));
  const tail = (await lastBot().innerText()).slice(0, 300);
  console.log('回复：', JSON.stringify(tail));
  const hasPicker = await page.locator('[data-act="open-picker"]').count();
  console.log('是否出了候选入口：', hasPicker > 0);
}

await page.screenshot({ path: '/tmp/v05_3_full.png' });
console.log('页面错误：', errs.length ? errs : '无');
await browser.close();
