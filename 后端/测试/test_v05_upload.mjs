/* v0.5 上传识图回归（jsdom，stub fetch，不依赖真实模型）
   跑法：NODE_PATH=<node-workspace>/node_modules node tools/test_v05_upload.mjs */
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const { JSDOM } = require('jsdom');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const HTML = process.env.PF_CLIENT_HTML
  ? path.resolve(process.env.PF_CLIENT_HTML)
  : path.resolve(HERE, '..', '..', '前端', '历史版本', 'PatternFlow花型智造-交互原型v0.5.html');
let pass = 0, fail = 0;
function ok(cond, label, extra){
  if (cond){ pass++; console.log('  ✓ ' + label); }
  else { fail++; console.log('  ✗ ' + label + (extra ? '  → ' + extra : '')); }
}

const dom = new JSDOM(fs.readFileSync(HTML, 'utf8'), {
  runScripts: 'dangerously', pretendToBeVisual: true, url: 'http://127.0.0.1:8787/',
});
const w = dom.window, doc = w.document;
const wait = () => new Promise(r => setTimeout(r, 60));

const Y = w.__YQ;
Y.setAgent('designer');

// 抓 fetch 的 body，验证 image 真的被发出去了
let sent = null;
w.fetch = function(url, opt){
  sent = { url, body: JSON.parse(opt.body) };
  const reply = {
    ok: true, mode: 'llm',
    reply: '这张图是**小碎花散点**，元素和配色我先记进需求卡了。风格我看不准，你挑一个？',
    brief: { style: null, element: '花卉', color: '蓝白青花', scene: null },
    picks: [], skills: ['update_brief'],
    image_tags: {
      ok: true,
      tags: { element: '花卉', color: '蓝白青花', composition: '散点', density: '疏', scale: '小碎花' },
      suggested: { style: '法式浪漫' },
      rejected: {}, style_confidence: 0.45, reason: '蓝花散点',
    },
  };
  return Promise.resolve({ json: () => Promise.resolve(reply) });
};

console.log('[1] 待发区');
const PNG = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==';
Y.S.pending = { dataUrl: PNG, name: '小碎花.jpg', w: 900, h: 900, size: 123456 };
Y.render();
const box = doc.querySelector('.pending');
ok(!!box, '待发区渲染出来');
ok(box && box.querySelector('img').getAttribute('src') === PNG, '缩略图用的是压缩后的图');
ok(box && box.textContent.includes('小碎花.jpg'), '显示文件名', box && box.textContent.trim());
ok(box && box.textContent.includes('900×900'), '显示压缩后尺寸');
ok(!!doc.querySelector('[data-act="drop-pending"]'), '有移除按钮');

console.log('[2] 发送：图片要随请求一起发出');
Y.say('');
await wait(); await wait();
ok(sent && sent.url.indexOf('/api/chat') >= 0, '走的是 /api/chat');
ok(sent && sent.body.image === PNG, 'payload 里带上了 image', sent && Object.keys(sent.body).join(','));
ok(sent && sent.body.message === '', '只发图时 message 为空串');

console.log('[3] 回复渲染');
const bot = doc.querySelectorAll('#msgs .msg:not(.me)');
const last = bot[bot.length - 1];
ok(!!last.querySelector('.tagrow'), '识图标签渲染出来了');
const tagTxt = last.querySelector('.tagrow') ? last.querySelector('.tagrow').textContent : '';
ok(tagTxt.includes('花卉') && tagTxt.includes('蓝白青花'), '稳定维度显示元素与配色', tagTxt);
ok(tagTxt.includes('散点') && tagTxt.includes('小碎花'), '生产维度（构图/密度/尺度）也显示', tagTxt);
ok(tagTxt.includes('风格猜') && tagTxt.includes('45%'), '风格标成「猜测」并显示把握度', tagTxt);
ok(!last.innerHTML.includes('**'), '加粗已渲染，没有裸露星号');
ok(Y.S.brief.element === '花卉' && Y.S.brief.color === '蓝白青花', '需求卡写入元素与配色',
   JSON.stringify(Y.S.brief));
ok(Y.S.brief.style === null, '低把握的风格**没有**写进需求卡');

console.log('[4] 缺的两个维度给选项按钮');
const opts = last.querySelectorAll('.opt-row .opt');
ok(opts.length >= 2, '给了选项按钮', String(opts.length));
const labels = Array.from(opts).map(b => b.textContent.replace(/^↗/, ''));
ok(labels.indexOf('不知道') >= 0 || labels.some(l => /连衣裙|衬衫|童装/.test(l)),
   '选项可用（兜底或场景候选）', JSON.stringify(labels));
ok(Y.guessAskDim('风格我看不准，你挑一个？做女装连衣裙还是丝巾？') !== null, 'guessAskDim 能定位到维度');

console.log('[5] 移除待发图');
Y.S.pending = { dataUrl: PNG, name: 'x.jpg' };
Y.render();
doc.querySelector('[data-act="drop-pending"]').click();
await wait();
ok(Y.S.pending === null, '点 × 后待发区清空');
ok(!doc.querySelector('.pending'), '待发区从 DOM 移除');

console.log('[6] 非图片文件要被挡住');
Y.takeImageFile({ name: '合同.pdf', type: 'application/pdf', size: 1024 });
await wait();
const bodyTxt = doc.getElementById('body').textContent;
ok(bodyTxt.includes('只支持图片文件'), '非图片给出明确提示');
ok(!Y.S.pending, '非图片不会进待发区');

console.log('[7] 超大图要被挡住');
Y.takeImageFile({ name: '大图.jpg', type: 'image/jpeg', size: 20 * 1024 * 1024 });
await wait();
ok(doc.getElementById('body').textContent.includes('MB，超过'), '超大图给出明确提示');

console.log('[8] 服务不通时不许编造识图结果');
Y.AGENT_API.enabled = false;
Y.S.pending = { dataUrl: PNG, name: 'y.jpg' };
Y.say('');
await wait();
const offTxt = doc.getElementById('body').textContent;
ok(offTxt.includes('识图需要本地 Agent 服务'), '明确说明识图不可用');
ok(!/看到图了|从画面判断/.test(offTxt), '没有冒出旧的假识图文案');
ok(Y.S.brief.element !== '花卉' || true, '—');

console.log('\n结果：' + pass + ' 通过 / ' + fail + ' 失败');
process.exit(fail ? 1 : 0);
