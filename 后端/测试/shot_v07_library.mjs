/* 花型库的可视验收：对着在线服务截图（图能真加载），
   并抓控制台报错 —— 库里全是真图，最容易出的是图片地址拼错/裂图。

   跑法：OUT=<dir> node tools/shot_v07_library.mjs               */
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { chromium } = require('playwright');

const OUT = process.env.OUT || '/tmp';
const BASE = process.env.BASE || 'http://127.0.0.1:8787/';

const IGNORE = /Failed to fetch|net::|ERR_|favicon|the server responded/i;
const errs = [];
const b = await chromium.launch();
const p = await b.newPage({ viewport: { width: 1180, height: 900 }, deviceScaleFactor: 2 });
p.on('pageerror', e => { if (!IGNORE.test(e.message)) errs.push('pageerror: ' + e.message); });
p.on('console', m => { if (m.type() === 'error' && !IGNORE.test(m.text())) errs.push('console: ' + m.text()); });

await p.goto(BASE, { waitUntil: 'load' });
await p.waitForTimeout(800);

/* ---------- [1] 花型库页面：真实缩略图网格 ---------- */
await p.evaluate(() => { window.__YQ.go('library'); });
await p.waitForSelector('[data-libopen]', { timeout: 15000 });
await p.waitForTimeout(2200);          // 等缩略图加载
const cards = await p.locator('[data-libopen]').count();
const total = await p.evaluate(() => window.__YQ.LIB.total);
const broken = await p.evaluate(() => {
  const imgs = Array.from(document.querySelectorAll('.lib-grid img'));
  return imgs.filter(i => !i.complete || i.naturalWidth === 0).length;
});
await p.screenshot({ path: OUT + '/lib_1_grid.png', fullPage: false });
console.log('[1] 库页面：卡片 ' + cards + ' 张 / 共 ' + total + ' 张，裂图 ' + broken);

/* ---------- [2] 筛选：点风格按钮 ---------- */
const chip = p.locator('[data-libdim="style"][data-libval="新中式国风"]').first();
if (await chip.count()){
  await chip.click();
  await p.waitForTimeout(1500);
}
const filtered = await p.evaluate(() => window.__YQ.LIB.total);
await p.screenshot({ path: OUT + '/lib_2_filter.png' });
console.log('[2] 按「新中式国风」筛 → ' + filtered + ' 张');

/* ---------- [3] 单张详情：看大图 + 改标签 ---------- */
await p.locator('[data-act="lib-clear"]').click();
await p.waitForTimeout(1200);
await p.locator('[data-libopen]').first().click();
await p.waitForTimeout(1400);
const fields = await p.locator('[data-libfld]').count();
const nameVal = await p.locator('[data-libfld="name"]').inputValue();
await p.screenshot({ path: OUT + '/lib_3_detail.png' });
console.log('[3] 详情弹层：' + fields + ' 个可改字段，名称 =「' + nameVal + '」');

/* ---------- [4] 话术：让花型师从库里捞图 ---------- */
await p.evaluate(() => { window.__YQ.closeLayer(); window.__YQ.go('designer'); });
await p.evaluate(() => {
  const Y = window.__YQ;
  Y.S.chat.designer.length = 0;
  const ids = Y.LIB.items.filter(i => i.color === '蓝白青花').slice(0, 2).map(i => i.id);
  Y.S.picks = ids.slice();
  Y.push('designer', { me: true, text: '客户要蓝白底的花卉，做丝巾用' });
  Y.push('designer', {
    text: '青花这一支库里有货，给你挑了两张真图：\n\n'
        + '- **青花浮影**（新中式国风 / 蓝白青花）——月白底水彩玫瑰，留白足，'
        + '最像丝巾该有的呼吸感，首选。\n'
        + '- **青花玫瑰园**（法式浪漫 / 蓝白青花）——蓝白玫瑰花束，散点满地感强。\n\n'
        + '这两张颜色和元素两项都扎实命中。要不要再收窄一下风格？',
  });
  ids.forEach(id => Y.push('designer', { pattern: id }));
  Y.push('designer', { card: Y.pickerCardHTML(4) });
  Y.render();
});
await p.waitForTimeout(1600);
await p.screenshot({ path: OUT + '/lib_4_chat.png', fullPage: true });
console.log('[4] 对话：库里捞到的候选 ' + (await p.evaluate(() => window.__YQ.S.picks.length)) + ' 张');

/* ---------- [5] 选择弹窗：库图直接显示出来 ---------- */
await p.evaluate(() => { window.__YQ.openPicker(); });
await p.waitForTimeout(2000);
await p.screenshot({ path: OUT + '/lib_5_picker.png' });
const pickImgs = await p.evaluate(() => document.querySelectorAll('.picker-grid img').length);
const pickSvg = await p.evaluate(() => document.querySelectorAll('.picker-grid svg').length);
console.log('[5] 选择弹窗：真图 ' + pickImgs + ' 张 / 矢量款 ' + pickSvg + ' 张');

console.log('控制台错误：' + (errs.length ? errs.join(' | ') : '0'));
await b.close();
const okAll = cards > 0 && broken === 0 && fields >= 9 && pickImgs > 0 && !errs.length;
process.exit(okAll ? 0 : 1);
