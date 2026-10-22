/* 跨角色串台修复的可视验收：织印师的方案只留在织印师页，不会出现在花型师页。
   对着在线服务截图（图能真加载）：
       OUT=<dir> node tools/shot_v06_xchar.mjs */
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { chromium } = require('playwright');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const OUT = process.env.OUT || '/tmp';
const BASE = process.env.BASE || 'http://127.0.0.1:8787/';
const GEN = process.env.GEN_IMG || '/api/file/20260928-184037-gen-4800.png';

const IGNORE = /\/api\/|Failed to fetch|net::|ERR_|favicon|load/i;
const errs = [];
const b = await chromium.launch();
const p = await b.newPage({ viewport: { width: 900, height: 820 }, deviceScaleFactor: 2 });
p.on('pageerror', e => { if (!IGNORE.test(e.message)) errs.push('pageerror: ' + e.message); });
p.on('console', m => { if (m.type() === 'error' && !IGNORE.test(m.text())) errs.push('console: ' + m.text()); });

await p.goto(BASE, { waitUntil: 'load' });
await p.waitForTimeout(900);

/* ---------- [1] 织印师页：描稿方案只挂在这里 ---------- */
await p.evaluate(() => {
  const Y = window.__YQ;
  Y.S.page = 'engineer'; Y.S.agent = 'engineer';
  Y.S.chat.engineer.length = 0;
  Y.S.credits = 1280;
  Y.push('engineer', { me:true, text:'把这张图片描一下' });
  const m = Y.push('engineer', {
    text:'这张我看下来了：满地密排的几何撞色花，大花路数。\n'
       + '这类照片能直接描，出 1 张平面花型设计稿（不含四方连续，那是下一步）。'
       + '还原度可以在卡片上切，60 点，你点确认我就开跑。',
  });
  m.pendingCard = Y.confirmCardHTML({
    action:'trace_pattern', cost:60, fidelity:'clean', count:1,
    qc:{ level:'good', usable:true, issues:[], advice:'' },
    summary:'描稿仿稿 · 从照片提取印花图案；产出平面花型设计稿',
  });
  const g = Y.genOf('engineer');
  g.action = 'trace_pattern'; g.cost = 60; g.msgId = m.id;
  Y.render();
});
await p.waitForTimeout(500);
await (await p.$('#msgs')).screenshot({ path: OUT + '/xchar_1_engineer.png' });
console.log('[1] 织印师页：描稿方案在织印师这里（含确认卡片）');

/* ---------- [2] 切到花型师页：不该看到织印师的方案 ---------- */
await p.evaluate((gen) => {
  const Y = window.__YQ;
  Y.S.page = 'designer'; Y.S.agent = 'designer';
  Y.S.chat.designer.length = 0;
  const m = Y.push('designer', {
    text:'图出来了。觉得哪张方向对，直接说编号，我接着在那张上调；也能改配色、疏密度、元素。',
    images:[{ name:'gen.jpg', url:gen, mime:'image/jpeg', bytes:420000 }],
    genLabel:'AI 生成花型', genCost:60,
    gen:{ name:'generate_pattern', args:{ prompt_full:'…' } },
  });
  Y.genOf('designer').editMsgId = m.id;
  Y.render();
}, GEN);
await p.waitForTimeout(900);
await (await p.$('#msgs')).screenshot({ path: OUT + '/xchar_2_designer.png' });

const pendingOnDesigner = await p.locator('.confirm-card').count();
const editBtns = await p.locator('[data-act="ed:dim:color"]').count();
console.log('[2] 花型师页：确认卡片数量 =', pendingOnDesigner, '（应为 0）；改图按钮 =', editBtns);
console.log('控制台错误：' + (errs.length ? errs.join(' | ') : '0'));
await b.close();
process.exit((pendingOnDesigner === 0 && editBtns > 0 && !errs.length) ? 0 : 1);
