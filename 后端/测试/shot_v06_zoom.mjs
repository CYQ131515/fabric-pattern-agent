/* v0.6 图案放大查看 · 真浏览器视觉验证（不依赖服务，直接开原型文件）
   跑法：NODE_PATH=<node-workspace>/node_modules node tools/shot_v06_zoom.mjs
   产物写到 OUT（默认 /tmp）：zoom_1..zoom_6 */
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

/* 连不上本地服务是预期的（这里只验前端视觉），别把它算成错误 */
const IGNORE = /\/api\/|Failed to fetch|net::|ERR_|favicon|load/i;
const errs = [];
const b = await chromium.launch();
const p = await b.newPage({ viewport: { width: 1440, height: 960 }, deviceScaleFactor: 2 });
p.on('pageerror', e => { if (!IGNORE.test(e.message)) errs.push('pageerror: ' + e.message); });
p.on('console', m => { if (m.type() === 'error' && !IGNORE.test(m.text())) errs.push('console: ' + m.text()); });

await p.goto('file://' + HTML, { waitUntil: 'load' });
await p.waitForTimeout(600);

const b64 = f => 'data:image/jpeg;base64,' +
  fs.readFileSync(path.join(PIC, f)).toString('base64');
const photo = b64('真图-额度恢复后-蓝白青花底色.jpg');      // 当「用户上传的照片」
const made = b64('真图-描稿仿稿-蓝白青花花卉-clean档.jpg'); // 当「AI 出的花型稿」

await p.evaluate(([ph, gn]) => {
  const Y = window.__YQ;
  Y.AGENT_API.base = '';
  Y.S.page = 'engineer'; Y.S.agent = 'engineer';
  Y.S.chat.engineer.length = 0;
  Y.push('engineer', { me:true, text:'这条裙子上的图案能帮我描下来吗',
    image:ph, imgName:'随手拍的裙子.jpg' });
  Y.push('engineer', { text:'描稿完成。这是按照片提取的花型稿，还能接着改配色、疏密度。',
    images:[{ name:'trace-1.jpg', url:gn, bytes:652666, mime:'image/jpeg' }],
    model:'doubao-seedream-5-0', genLabel:'描稿仿稿' });
  Y.render();
}, [photo, made]);
await p.waitForTimeout(500);

console.log('[1] 悬停出角标');
await p.hover('.genimg');
await p.waitForTimeout(450);
await p.locator('#msgs').screenshot({ path: OUT + '/zoom_1_hover_hint.png' });

console.log('[2] AI 生图 → 放大层');
await p.click('.genimg');
await p.waitForTimeout(500);
await p.screenshot({ path: OUT + '/zoom_2_lightbox_img.png' });

console.log('[3] 放大看细节');
for (let i = 0; i < 4; i++){ await p.click('[data-act="zoom-in"]'); await p.waitForTimeout(110); }
await p.screenshot({ path: OUT + '/zoom_3_zoomed_in.png' });
await p.click('[data-act="zoom-fit"]');

console.log('[4] 用户上传的图');
await p.click('[data-act="zoom-close"]');
await p.waitForTimeout(250);
await p.click('.msg-img');
await p.waitForTimeout(500);
await p.screenshot({ path: OUT + '/zoom_4_lightbox_photo.png' });
await p.click('[data-act="zoom-close"]');
await p.waitForTimeout(250);

console.log('[5] 花型图案 + 平铺预览');
await p.evaluate(() => {
  const Y = window.__YQ;
  Y.push('engineer', { pattern: Y.PATTERNS[1].id });
  Y.render();
});
await p.waitForTimeout(350);
await p.click('.pmsg .pic');
await p.waitForTimeout(500);
await p.screenshot({ path: OUT + '/zoom_5_lightbox_pattern.png' });
await p.click('[data-act="zoom-tile"]');
await p.waitForTimeout(700);
await p.screenshot({ path: OUT + '/zoom_6_tile_preview.png' });

const st = await p.evaluate(() => {
  const Z = window.__YQ.ZOOM, z = document.getElementById('zoom');
  const paper = document.getElementById('zpaper');
  return { open:Z.open, tiled:Z.tiled, scale:Z.s, hidden:z.hidden,
           paperBox: paper ? [Math.round(paper.getBoundingClientRect().width),
                              Math.round(paper.getBoundingClientRect().height)] : null,
           bgSize: paper ? paper.style.backgroundSize : '' };
});
console.log('放大层状态：', JSON.stringify(st));

console.log('[6] 花型选择弹窗里的角标（右上角已被勾选标记占用）');
await p.click('[data-act="zoom-close"]');
await p.waitForTimeout(250);
await p.evaluate(() => {
  const Y = window.__YQ;
  Y.S.picks = Y.PATTERNS.slice(0, 4).map(x => x.id);
  Y.openPicker();
});
await p.waitForTimeout(450);
await p.locator('.modal').screenshot({ path: OUT + '/zoom_7_picker_badge.png' });

console.log('控制台错误：', errs.length ? errs : '无');
await b.close();
process.exit(errs.length ? 1 : 0);
