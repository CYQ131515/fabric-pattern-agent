/* v0.6 对话细节 · 真浏览器视觉验证（不依赖服务，直接开原型文件）
   覆盖：气泡下方的缩放提示、精简后的回复观感、卡片上的还原度按钮与切换
   跑法：NODE_PATH=<node-workspace>/node_modules node tools/shot_v06_chat.mjs
   产物写到 OUT（默认 /tmp）：chat_1..chat_4 */
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
const PIC = path.join(ROOT, '前端', '历史版本', '验图');
const OUT = process.env.OUT || '/tmp';

const IGNORE = /\/api\/|Failed to fetch|net::|ERR_|favicon|load/i;
const errs = [];
const b = await chromium.launch();
const p = await b.newPage({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 2 });
p.on('pageerror', e => { if (!IGNORE.test(e.message)) errs.push('pageerror: ' + e.message); });
p.on('console', m => { if (m.type() === 'error' && !IGNORE.test(m.text())) errs.push('console: ' + m.text()); });

await p.goto('file://' + HTML, { waitUntil: 'load' });
await p.waitForTimeout(600);

const b64 = f => 'data:image/jpeg;base64,' + fs.readFileSync(path.join(PIC, f)).toString('base64');
const photo = b64('真图-额度恢复后-蓝白青花底色.jpg');

await p.evaluate(([ph]) => {
  const Y = window.__YQ;
  Y.AGENT_API.base = '';
  Y.S.page = 'engineer'; Y.S.agent = 'engineer';
  Y.S.chat.engineer.length = 0;
  Y.S.credits = 9999;
  /* 1) 上传了一张超过 2048 的照片 → 气泡下方要有小字说明 */
  Y.push('engineer', { me:true, text:'帮我把这上面的图案描下来做成花型',
    image:ph, imgName:'随手拍的裙子.jpg',
    imgNote:'原图 4032×3024，已按最长边等比缩放到 2048×1536px' });
  /* 2) 精简后的回复：体检 + 张数 + 产出，不再解释三档还原度 */
  const m = Y.push('engineer', { text:
    '照片我看过了，清晰度和曝光都挺好，能直接描，不用重拍。\n\n' +
    '这次出 1 张，60 点，失败自动退。拿到手是一张平面花型设计稿，' +
    '不含四方连续——那个是下一步的事。' });
  m.pendingCard = Y.confirmCardHTML({
    action:'trace_pattern', cost:60, fidelity:'clean',
    qc:{ level:'good', usable:true, issues:[] },
    summary:'描稿仿稿 · 从照片提取印花图案；产出平面花型设计稿',
  });
  Y.render();
}, [photo]);
await p.waitForTimeout(300);

const msgs = await p.$('#msgs');
await msgs.screenshot({ path: OUT + '/chat_1_reply_and_card.png' });
console.log('[1] 回复 + 确认卡片（含还原度按钮）');

/* 3) 点「简化描摹」→ 卡片原地换成该档并高亮 */
await p.evaluate(() => {
  const Y = window.__YQ;
  /* 离线也要能演示：直接本地换卡片，模拟服务端重提方案后的结果 */
  const card = Y.confirmCardHTML({
    action:'trace_pattern', cost:60, fidelity:'vector',
    qc:{ level:'good', usable:true, issues:[] },
    summary:'描稿仿稿 · 从照片提取印花图案；产出平面花型设计稿',
  });
  const list = Y.S.chat.engineer;
  for (let i = list.length - 1; i >= 0; i--){ if (list[i].pendingCard){ list[i].pendingCard = card; break; } }
  Y.render();
});
await p.waitForTimeout(250);
await (await p.$('#msgs')).screenshot({ path: OUT + '/chat_2_fidelity_vector.png' });

const st = await p.evaluate(() => {
  const on = document.querySelector('.fid-row .fid.on');
  return {
    onFid: on ? on.dataset.fid : null,
    fids: Array.from(document.querySelectorAll('.fid-row .fid')).map(b => b.textContent),
    hint: (document.querySelector('.fid-row .fd') || {}).textContent,
    note: (document.querySelector('.imgnote') || {}).textContent,
  };
});
console.log('[2] 切档后状态：', JSON.stringify(st, null, 0));
console.log('控制台错误：', errs.length ? errs : '无');

await b.close();
process.exit(errs.length ? 1 : 0);
