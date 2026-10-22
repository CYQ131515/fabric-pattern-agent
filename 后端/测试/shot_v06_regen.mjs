/* v0.6 重新生成按钮 · 真浏览器视觉验证（离线合成对话，不依赖服务）
   覆盖：生图消息下方的「重新生成」、展开改图维度后按钮仍在、织印师描稿出图同样有
   跑法：NODE_PATH=<node-workspace>/node_modules node tools/shot_v06_regen.mjs
   产物写到 OUT（默认 /tmp）：regen_1..regen_4 */
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
const genPic = b64('真图-额度恢复后-蓝白青花底色.jpg');
const tracePic = b64('真图-描稿仿稿-蓝白青花花卉-clean档.jpg');

/* ---------- [1] 花型师：出图后带「重新生成」 ---------- */
await p.evaluate(([gen]) => {
  const Y = window.__YQ;
  Y.AGENT_API.base = '';
  Y.S.page = 'designer'; Y.S.agent = 'designer';
  Y.S.chat.designer.length = 0;
  Y.S.credits = 9999;
  Y.push('designer', { me:true, text:'给我生成一张国风的淡红色玫瑰的图案，用于年轻女孩的连衣裙' });
  Y.push('designer', { text:'方案给你理好了，60 点。确认我就开画。' });
  const m = Y.push('designer', {
    text:'图出来了。还能接着改配色、疏密度、元素。',
    images:[{ name:'gen-1.jpg', url:gen, bytes:198000 }],
    genLabel:'AI 生成花型', genCost:60,
    gen:{ name:'generate_pattern', args:{ prompt_full:'…' } },
  });
  Y.genOf('designer').editMsgId = m.id; Y.S.editDim = null;
  Y.render();
}, [genPic]);
await p.waitForTimeout(300);
await (await p.$('#msgs')).screenshot({ path: OUT + '/regen_1_designer.png' });
console.log('[1] 花型师出图：按钮行里有「重新生成」');

/* ---------- [2] 展开改配色，按钮要还在 ---------- */
await p.click('[data-act="ed:dim:color"]');
await p.waitForTimeout(250);
await p.locator('#msgs .msg').last().screenshot({ path: OUT + '/regen_2_expanded.png' });
const hasInExpanded = await p.locator('[data-act="regen"]').count();
console.log('[2] 展开改图维度后「重新生成」按钮数量：', hasInExpanded);

/* ---------- [2b] 自定义配色：色板之外直接写 ---------- */
await p.click('[data-act="ed:custom"]');
await p.waitForTimeout(200);
await p.fill('.ed-input', '淡红');
await p.waitForTimeout(120);
await p.locator('#msgs .msg').last().screenshot({ path: OUT + '/regen_2b_custom_color.png' });
console.log('[2b] 自定义配色输入框（色板里没有「淡红」）');

/* ---------- [3] 织印师：描稿出图同样有 ---------- */
await p.evaluate(([tr]) => {
  const Y = window.__YQ;
  Y.S.page = 'engineer';
  Y.S.agent = 'engineer';
  Y.S.chat.engineer.length = 0;
  Y.S.editDim = null;
  Y.push('engineer', { me:true, text:'照着这张把图案描下来' });
  const m = Y.push('engineer', {
    text:'描稿完成。这是按照片提取的花型稿，还能接着改配色、疏密度；要做四方连续就交给织印师。',
    images:[{ name:'trace-1.jpg', url:tr, bytes:210000 }],
    genLabel:'描稿仿稿', genCost:60,
    gen:{ name:'trace_pattern', args:{ fidelity:'clean', prompt_full:'…' } },
  });
  Y.genOf('engineer').editMsgId = m.id;
  Y.render();
}, [tracePic]);
await p.waitForTimeout(300);
await p.locator('#msgs .msg').last().screenshot({ path: OUT + '/regen_3_engineer.png' });
console.log('[3] 织印师描稿出图：同样带「重新生成」');

/* ---------- [4] 需求卡重置后的顶栏 ---------- */
await p.evaluate(() => {
  const Y = window.__YQ;
  Y.S.page = 'designer'; Y.S.agent = 'designer';
  Y.S.brief = { style:null, element:null, color:null, scene:null };
  Y.render();
});
await p.waitForTimeout(200);
await p.screenshot({ path: OUT + '/regen_4_brief_reset.png' });
const st = await p.evaluate(() => {
  const Y = window.__YQ;
  return {
    regenText: (document.querySelector('[data-act="regen"]') || {}).textContent,
    briefSlots: Array.from(document.querySelectorAll('.briefbar .slot')).map(e => e.textContent.trim()),
    brief: Y.S.brief,
  };
});
console.log('[4] 状态：', JSON.stringify(st));
console.log('控制台错误：', errs.length ? errs : '无');

await b.close();
process.exit(errs.length ? 1 : 0);
