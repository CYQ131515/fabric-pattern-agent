/* v0.6 图案放大查看回归（jsdom，纯前端，不依赖服务）
   覆盖：上传图 / AI 生图 / 花型卡 / 任务中心 / 交付页 / 选择弹窗角标，
   以及缩放、平移、平铺、Esc/遮罩/按钮三种关闭路径。
   跑法：NODE_PATH=<node-workspace>/node_modules node tools/test_v06_zoom.mjs */
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const { JSDOM } = require('jsdom');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const HTML = process.env.PF_CLIENT_HTML
  ? path.resolve(process.env.PF_CLIENT_HTML)
  : path.resolve(HERE, '..', '..', '前端', '历史版本', 'PatternFlow花型智造-交互原型v0.6.html');
let pass = 0, fail = 0;
function ok(cond, label, extra){
  if (cond){ pass++; console.log('  ✓ ' + label); }
  else { fail++; console.log('  ✗ ' + label + (extra ? '  → ' + extra : '')); }
}

const dom = new JSDOM(fs.readFileSync(HTML, 'utf8'), {
  runScripts: 'dangerously', pretendToBeVisual: true, url: 'http://127.0.0.1:8787/',
});
const w = dom.window, doc = w.document;
const Y = w.__YQ;

Y.S.page = 'designer'; Y.S.agent = 'designer'; Y.S.credits = 9999;
Y.AGENT_API.base = 'http://127.0.0.1:8787';

const zl = () => doc.getElementById('zoom');
const PAPER = 'data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAA==';
const PID = Y.PATTERNS[0].id;

/* jsdom 没有 WheelEvent 的构造细节，用自定义 deltaY 更稳 */
function wheel(el, dy){
  const e = new w.Event('wheel', { bubbles:true, cancelable:true });
  Object.defineProperty(e, 'deltaY', { value: dy });
  el.dispatchEvent(e);
}
function mouse(type, x, y, target){
  const e = new w.MouseEvent(type, { bubbles:true, cancelable:true, clientX:x, clientY:y });
  (target || doc).dispatchEvent(e);
}

console.log('[1] 初始状态：不占位、不挡页面');
ok(!!zl(), '#zoom 容器存在');
ok(zl().hidden === true, '默认隐藏');
ok(zl().innerHTML === '', '默认是空的');
ok(Y.ZOOM.open === false, 'ZOOM.open 为 false');

console.log('[2] 点用户上传的图 → 打开放大');
Y.S.chat.designer.length = 0;
Y.push('designer', { me:true, text:'', image:PAPER, imgName:'随手拍的裙子.jpg' });
Y.render();
const mi = doc.querySelector('.msg-img.zoomable');
ok(!!mi, '上传图带 zoomable 类（可点）');
ok(mi && mi.dataset.zoom === 'img', '标记为位图');
const hint = mi && mi.querySelector('.zoom-hint');
ok(!!hint, '悬停有放大角标');
/* 图标用内联 SVG 而不是「⤢」这类字符：字符在缺字体的系统上会变豆腐块 */
ok(!!hint && !!hint.querySelector('svg'), '角标是内联 SVG 图标（不吃字体）');
mi.click();
ok(Y.ZOOM.open === true, '点击后打开放大层');
ok(zl().hidden === false, '放大层显示出来');
const imgIn = zl().querySelector('.zoom-paper img');
ok(!!imgIn, '放大层里渲染出图片');
ok(imgIn && imgIn.getAttribute('src') === PAPER,
   'src 直接复用 DOM 那颗图（不存第二份 base64）');
ok(zl().textContent.includes('随手拍的裙子.jpg'), '标题带图名',
   zl().textContent.trim().slice(0, 40));
ok(!zl().querySelector('[data-act="zoom-tile"]'), '位图不给平铺按钮（平铺是花型的事）');

console.log('[3] 缩放 / 适应窗口');
const s0 = Y.ZOOM.s;
zl().querySelector('[data-act="zoom-in"]').click();
ok(Y.ZOOM.s > s0, '放大按钮生效', String(Y.ZOOM.s));
ok(zl().textContent.includes(Math.round(Y.ZOOM.s * 100) + '%'), '百分比同步显示');
zl().querySelector('[data-act="zoom-out"]').click();
zl().querySelector('[data-act="zoom-out"]').click();
ok(Y.ZOOM.s < 1, '缩小按钮生效', String(Y.ZOOM.s));
zl().querySelector('[data-act="zoom-fit"]').click();
ok(Y.ZOOM.s === 1, '适应窗口回到 100%', String(Y.ZOOM.s));
ok(Y.ZOOM.x === 0 && Y.ZOOM.y === 0, '适应窗口同时把平移复位');

console.log('[4] 滚轮缩放 / 拖动平移');
const before = Y.ZOOM.s;
wheel(zl().querySelector('#zstage'), -100);
ok(Y.ZOOM.s > before, '滚轮向上放大', String(Y.ZOOM.s));
wheel(zl().querySelector('#zstage'), 100);
ok(Y.ZOOM.s <= before, '滚轮向下缩小', String(Y.ZOOM.s));

Y.zoomFit();
const stage = zl().querySelector('#zstage');
mouse('mousedown', 100, 100, stage);
ok(!!Y.ZOOM.drag, '按下开始拖动');
mouse('mousemove', 160, 130, stage);
ok(Y.ZOOM.x === 60 && Y.ZOOM.y === 30, '拖动位移作用到平移量',
   Y.ZOOM.x + ',' + Y.ZOOM.y);
mouse('mouseup', 160, 130, doc);
ok(Y.ZOOM.drag === null, '松手结束拖动');

console.log('[5] 花型：矢量放大 + 平铺预览');
Y.closeZoom();
Y.S.chat.designer.length = 0;
Y.push('designer', { pattern:PID });
Y.render();
const pic = doc.querySelector('.pmsg .pic.zoomable');
ok(!!pic, '花型卡的图案可点击放大');
pic.click();
ok(Y.ZOOM.open === true, '点击打开');
ok(!!zl().querySelector('.zoom-paper svg'), '渲染的是矢量花型（放大不失真）');
ok(zl().textContent.includes(Y.PAT[PID].name), '标题是花型名',
   zl().textContent.trim().slice(0, 40));
ok(zl().textContent.includes('PF-'), '副标题带花型编号');

const tb = zl().querySelector('[data-act="zoom-tile"]');
ok(!!tb, '花型提供「平铺预览」');
tb.click();
ok(Y.ZOOM.tiled === true, '切到平铺态');
const paper = zl().querySelector('.zoom-paper.tiled');
ok(!!paper, '纸面切到 tiled 模式');
ok(!!paper && !paper.querySelector('svg'), '平铺态不再放单张 svg（改用背景平铺）');
ok(zl().textContent.includes('平铺'), '标题标出平铺');

const tileUrl = Y.zoomTileURL(Y.PAT[PID]);
ok(tileUrl.indexOf('data:image/svg+xml') === 0, '平铺背景是 SVG data URL（矢量，放大不糊）');
/* 少了 xmlns，内联时看不出问题，但当成独立图片源浏览器直接不渲染 → 平铺一片空白 */
const tileSvg = decodeURIComponent(tileUrl);
ok(tileSvg.indexOf('xmlns="http://www.w3.org/2000/svg"') > 0,
   'data URL 里带 xmlns（否则平铺会是空白）');
/* 光影叠加层每个花回重来一遍，平铺后成网格状，会被误当成接缝 */
ok(tileSvg.indexOf('linearGradient') < 0, '平铺版本去掉光影叠加层（免得看成接缝）');
/* 平铺源是一张约 12KB 的 SVG data URL。**不能**直接读 paper.style.backgroundImage：
   dev 版 jsdom（30.1.2）的 cssstyle 对超过约 1000 字符的属性值会整条丢弃
   （实测 998 字符保留、999 就丢），而浏览器没有这个限制、真机上是正常的。
   所以这里只验「要挂上去的那个源」合不合规；挂载动作本身由下面 backgroundSize
   那两条兜住（它跟 backgroundImage 是同一次赋值写进去的）。
   真机效果交给 Playwright 截图脚本看，不靠 jsdom 判。 */
ok((Y.ZOOM.bgUrl || '').indexOf('data:image/svg+xml') === 0,
   '要挂上去的平铺背景源是 SVG data URL', (Y.ZOOM.bgUrl || '').slice(0, 36));
const bs0 = paper.style.backgroundSize || '';
ok(/^\(?\d+px \d+px\)?$/.test(bs0) || /\d+px \d+px/.test(bs0),
   '平铺单元是正方形尺寸', bs0);
zl().querySelector('[data-act="zoom-in"]').click();
ok((zl().querySelector('.zoom-paper.tiled').style.backgroundSize || '') !== bs0,
   '放大时平铺单元同步变大');
zl().querySelector('[data-act="zoom-tile"]').click();
ok(Y.ZOOM.tiled === false, '再点一次切回单张');
ok(!!zl().querySelector('.zoom-paper svg'), '切回后又渲染单张花型');
ok(!!zl().querySelector('.zoom-paper svg linearGradient'),
   '单张版本保留光影（面料有立体感）');

console.log('[6] 平铺入口（接缝预览）进来就是平铺态');
Y.closeZoom();
Y.push('designer', {
  card: '<div class="seamwrap zoomable" data-zoom="tile" data-pid="' + PID + '">' +
        Y.patternSVG(Y.PAT[PID]) + '</div>' });
Y.render();
const sw = doc.querySelector('.seamwrap.zoomable');
ok(!!sw, '平铺预览块可点击放大');
sw.click();
ok(Y.ZOOM.open === true && Y.ZOOM.tiled === true, '从平铺入口进来自动是平铺态');

console.log('[7] 关闭的三条路径：按钮 / Esc / 点空白');
zl().querySelector('[data-act="zoom-close"]').click();
ok(Y.ZOOM.open === false, '点 ✕ 关闭');
ok(zl().hidden === true && zl().innerHTML === '', '关闭后清空且隐藏，不留残影');

Y.openZoom({ kind:'pat', pid:PID });
doc.dispatchEvent(new w.KeyboardEvent('keydown', { key:'Escape', bubbles:true }));
ok(Y.ZOOM.open === false, 'Esc 关闭');

Y.openZoom({ kind:'pat', pid:PID });
zl().click();
ok(Y.ZOOM.open === false, '点空白遮罩关闭');

console.log('[8] 选择弹窗里的角标：点了只放大，不误选');
Y.S.picks = Y.PATTERNS.slice(0, 3).map(p => p.id);
Y.openPicker();
const badge = doc.querySelector('.psel .zoom-badge');
ok(!!badge, '多选卡片上有放大角标');
ok(!!badge && !!badge.querySelector('svg'), '角标图标同样是内联 SVG');
ok(Y.PICKER.sel.length === 0, '打开弹窗时还未选中');
badge.click();
ok(Y.ZOOM.open === true, '点角标打开放大层');
ok(Y.PICKER.sel.length === 0, '没有把这张花型误选上（角标优先于选择）');
Y.closeZoom();
Y.closeLayer();

console.log('[9] 覆盖检查：任务中心 / 交付页的图案都能点开');
Y.S.tasks = [{ tid:'TJ-1', type:'repeat', pid:PID, status:'done', progress:100, step:3 }];
Y.S.confirmed = [{ pid:PID, at:'2026-09-28 10:00' }];
Y.S.assets = [{ pid:PID, name:'x.svg', fmt:'四方连续 600dpi', at:'2026-09-28',
                pts:30, tid:'TJ-1', type:'repeat' }];
Y.go('tasks');
ok(!!doc.querySelector('#body .card [data-zoom="pat"]'), '任务卡的缩略图可放大');
ok(!!doc.querySelector('#body .mini-row .thumb.zoomable'), '已确认花型的缩略图可放大');
ok(!!doc.querySelector('#body .fthumb.zoomable'), '文件历史的缩略图可放大');
/* 注：任务卡的「接缝检测预览」只在 status=done 渲染，而 viewTasks 目前没把 done 任务列出来
   （原型既有疏漏，另记），所以这里改用注入的方式验证，见 [6]。 */
const one = doc.querySelector('#body [data-zoom="pat"]');
if (one) one.click();
ok(Y.ZOOM.open === true, '任务中心缩略图点开成功');
Y.closeZoom();
Y.go('assets');
ok(doc.querySelectorAll('#body [data-zoom]').length >= 1, '交付下载页的图案也能点开');
Y.closeZoom();
Y.go('designer');

console.log('[10] AI 生成的花型图也能放大');
Y.S.chat.designer.length = 0;
Y.push('designer', { text:'图出来了。',
  images:[{ name:'trace-1.jpg', url:'/api/file/trace-1.jpg', bytes:456789, mime:'image/jpeg' }],
  model:'doubao-seedream-5-0', genLabel:'描稿仿稿' });
Y.render();
const gi = doc.querySelector('.genimg.zoomable');
ok(!!gi, 'AI 生图带 zoomable 类');
gi.click();
const gImg = zl().querySelector('.zoom-paper img');
ok(!!gImg, '放大层里渲染出生图');
ok(gImg && gImg.getAttribute('src').indexOf('/api/file/trace-1.jpg') >= 0,
   'src 走了服务的文件地址', gImg && gImg.getAttribute('src'));
ok(zl().textContent.includes('描稿仿稿'), '标题是「描稿仿稿」');
ok(zl().textContent.includes('456') || zl().textContent.includes('KB'),
   '副标题带上文件信息', zl().querySelector('.zt-sub') && zl().querySelector('.zt-sub').textContent);
Y.closeZoom();

console.log('\n结果：' + pass + ' 通过 / ' + fail + ' 失败');
process.exit(fail ? 1 : 0);
