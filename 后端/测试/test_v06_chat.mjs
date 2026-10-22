/* v0.6 对话细节回归：输入框发送后清空 / 上传图 2048 限制与提示 / 还原度按钮
   跑法：NODE_PATH=<node-workspace>/node_modules node tools/test_v06_chat.mjs */
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
const wait = () => new Promise(r => setTimeout(r, 60));

const Y = w.__YQ;
Y.S.page = 'engineer';
Y.S.agent = 'engineer';
Y.S.credits = 9999;
Y.AGENT_API.base = 'http://127.0.0.1:8787';
Y.AGENT_API.online = true;

/* ---- stub：FileReader / Image / canvas —— jsdom 三者都没有实现 ---- */
w.FileReader = function(){
  const self = this;
  self.readAsDataURL = function(){
    setTimeout(function(){ self.result = 'data:image/jpeg;base64,SRC'; if (self.onload) self.onload(); }, 0);
  };
};
function fakeImage(w0, h0){
  return function(){
    const o = { width:0, height:0, onload:null, onerror:null };
    Object.defineProperty(o, 'src', {
      set(){ setTimeout(function(){ o.width = w0; o.height = h0; if (o.onload) o.onload(); }, 0); },
    });
    return o;
  };
}
const origCreate = doc.createElement.bind(doc);
doc.createElement = function(tag){
  if (String(tag).toLowerCase() === 'canvas'){
    return {
      width:0, height:0,
      getContext: () => ({ fillStyle:'', fillRect(){}, drawImage(){} }),
      toDataURL(){ return 'data:image/jpeg;base64,SCALED' + this.width + 'x' + this.height; },
    };
  }
  return origCreate(tag);
};

let pendingResp = null, actionCalls = [], chatCalls = [];
w.fetch = function(url, opt){
  const body = JSON.parse(opt.body || '{}');
  if (url.indexOf('/api/health') >= 0)
    return Promise.resolve({ json: () => Promise.resolve(
      { ok: true, model: 'deepseek-flash', llm_enabled: true }) });
  if (url.indexOf('/api/action') >= 0){
    actionCalls.push(body);
    if (body.action === 'set_fidelity'){
      return Promise.resolve({ json: () => Promise.resolve({
        ok: true,
        pending: { action: 'trace_pattern', cost: 60, fidelity: body.fidelity,
                   qc: { level: 'good', usable: true, issues: [], advice: '' },
                   summary: '描稿仿稿 · 从照片提取印花图案' },
      }) });
    }
    return Promise.resolve({ json: () => Promise.resolve({
      ok: true, action: 'trace_pattern', cost: 60, fidelity: 'clean',
      images: [{ name: 'trace-1.jpg', url: '/api/file/trace-1.jpg', bytes: 45678 }],
    }) });
  }
  chatCalls.push(body);
  return Promise.resolve({ json: () => Promise.resolve(pendingResp) });
};

function tracePending(fidelity, reply){
  return {
    ok: true, mode: 'llm', skills: ['trace_pattern'],
    reply: reply || '照片我看过了，能描。这次出 1 张，产出一张平面花型设计稿。',
    brief: {}, picks: [], images: null,
    pending: { action: 'trace_pattern', cost: 60, args: { confirmed: false },
               fidelity: fidelity || 'clean',
               qc: { level: 'good', usable: true, issues: [], advice: '' },
               summary: '描稿仿稿 · 从照片提取印花图案；产出平面花型设计稿' },
  };
}

/* ================= [1] 发送后输入框要清空 ================= */
console.log('[1] 发送后输入框自动清空');
pendingResp = tracePending('clean');
const ta = doc.getElementById('input');
ok(!!ta, '输入框在');
ta.value = '帮我把这上面的图案描下来';
doc.querySelector('[data-act="send"]').click();
await wait(); await wait();
const ta2 = doc.getElementById('input');
ok(!!ta2, '重渲染后输入框还在');
ok(ta2 && ta2.value === '', '发送后输入框已清空', JSON.stringify(ta2 && ta2.value));
ok(chatCalls.length === 1 && chatCalls[0].message === '帮我把这上面的图案描下来',
   '发出去的是刚才输入的内容', JSON.stringify(chatCalls[0] && chatCalls[0].message));
const meMsg = doc.querySelectorAll('#msgs .msg.me');
ok(meMsg.length === 1 && meMsg[0].textContent.includes('描下来'), '输入内容进了用户气泡');

console.log('[1b] 打字中途重渲染不丢字（不能为清空把保留逻辑整个删掉）');
const ta3 = doc.getElementById('input');
ta3.value = '写到一半';
Y.render();
const ta4 = doc.getElementById('input');
ok(ta4 && ta4.value === '写到一半', '未发送时 render 仍保留草稿', JSON.stringify(ta4 && ta4.value));
ta4.value = '';

/* ================= [2] 上传图长边限制 2048 ================= */
console.log('[2] 上传图超过 2048 才等比缩放');
ok(Y.UP_MAX_PX === 2048, '长边上限是 2048', String(Y.UP_MAX_PX));

w.Image = fakeImage(4000, 3000);
let got = null;
Y.compressImage({ name: '随手拍.jpg', size: 4 * 1024 * 1024 },
  (err, info) => { got = info; });
await wait();
ok(got && got.scaled === true, '4000×3000 被判定为需要缩放');
ok(got && got.w === 2048 && got.h === 1536, '按最长边等比缩放到 2048×1536',
   got && (got.w + '×' + got.h));
ok(got && got.origW === 4000 && got.origH === 3000, '原始尺寸被保留（提示要用）');
ok(got && String(got.dataUrl).indexOf('SCALED2048x1536') > 0, 'dataURL 是缩放后的图');

console.log('[3] 没超过 2048 且体积不大 → 一个像素都不动');
w.Image = fakeImage(1600, 1200);
got = null;
Y.compressImage({ name: '小图.jpg', size: 900 * 1024 }, (err, info) => { got = info; });
await wait();
ok(got && got.scaled === false, '1600×1200 判定为无需缩放');
ok(got && got.dataUrl === 'data:image/jpeg;base64,SRC', '原图直传，没有走 canvas',
   String(got && got.dataUrl).slice(0, 40));
ok(got && got.w === 1600 && got.h === 1200, '尺寸原样保留');

console.log('[4] 提示文案：缩放过才提示');
const note = Y.imgNoteOf({ scaled: true, origW: 4000, origH: 3000, w: 2048, h: 1536 });
ok(note.includes('4000×3000') && note.includes('2048×1536'),
   '提示写清原图与缩放后尺寸', note);
ok(Y.imgNoteOf({ scaled: false, origW: 1600, origH: 1200, w: 1600, h: 1200 }) === '',
   '没缩放就不提示');
ok(Y.pendingTip({ scaled: true, origW: 4000, origH: 3000 }).includes('2048'),
   '待发区也说明超限已缩放');
ok(Y.pendingTip({ scaled: false }).includes('已就绪'), '没缩放时待发区是常规文案');

console.log('[5] 缩放提示要出现在我的气泡下方（小字）');
pendingResp = tracePending('clean', '好的，我看过了。');
Y.S.pending = { dataUrl: 'data:image/jpeg;base64,SCALED2048x1536', name: '随手拍.jpg',
                w: 2048, h: 1536, origW: 4000, origH: 3000, scaled: true, size: 700000 };
Y.say('');
await wait(); await wait();
const meLast = doc.querySelectorAll('#msgs .msg.me');
const mine = meLast[meLast.length - 1];
const noteEl = mine.querySelector('.imgnote');
ok(!!noteEl, '气泡下方多了一行说明');
ok(noteEl && noteEl.textContent.includes('4000×3000') && noteEl.textContent.includes('2048'),
   '说明里写了原图与缩放结果', noteEl && noteEl.textContent);
ok(noteEl && !noteEl.classList.contains('cap'), '是独立小字行，不是压在图上那层标题');
ok(noteEl && !noteEl.closest('.bubble'),
   '小字在气泡外面（压在红色气泡上对比度不够，看不清）');

/* ================= [6] 还原度按钮 ================= */
console.log('[6] 还原度做成卡片上的按钮');
pendingResp = tracePending('clean');
Y.say('描这一张');
await wait(); await wait();
const cards = doc.querySelectorAll('.confirm-card');
const card = cards[cards.length - 1];
const row = card.querySelector('.fid-row');
ok(!!row, '卡片上有还原度按钮行');
const fids = card.querySelectorAll('.fid-row .fid');
ok(fids.length === 3, '三档都给了', String(fids.length));
const labels = Array.from(fids).map(b => b.textContent);
ok(labels.join('/') === '严格复刻/清晰化重绘/简化描摹', '档位名称正确', labels.join('/'));
const onBtn = card.querySelector('.fid-row .fid.on');
ok(onBtn && onBtn.dataset.fid === 'clean', '当前档（clean）高亮',
   onBtn && onBtn.dataset.fid);
ok(card.querySelector('.fid-row .fd').textContent.length > 0, '当前档配一句短说明');

console.log('[7] 点另一档 → 只重提方案，不扣点、不出图');
const credits0 = Y.S.credits;
actionCalls = [];
card.querySelector('.fid-row .fid[data-fid="vector"]').click();
await wait(); await wait();
ok(actionCalls.length === 1 && actionCalls[0].action === 'set_fidelity',
   '走的是 set_fidelity（不是 confirm）', JSON.stringify(actionCalls[0]));
ok(actionCalls[0] && actionCalls[0].fidelity === 'vector', '带上目标档位 vector');
ok(Y.S.credits === credits0, '切档不扣点', String(Y.S.credits));
ok(!doc.querySelector('.genimg'), '切档不出图');
const cardAfter = doc.querySelectorAll('.confirm-card');
const c2 = cardAfter[cardAfter.length - 1];
const onBtn2 = c2.querySelector('.fid-row .fid.on');
ok(onBtn2 && onBtn2.dataset.fid === 'vector', '卡片上的高亮跟着切到 vector',
   onBtn2 && onBtn2.dataset.fid);
ok(doc.querySelectorAll('.confirm-card').length === cardAfter.length,
   '是原地换卡片，不是又冒出一张');

console.log('[8] 切档后再确认，仍是同一个描稿动作');
const btns = doc.querySelectorAll('[data-act="confirm-gen"]');
btns[btns.length - 1].click();
await wait(); await wait();
const last = actionCalls[actionCalls.length - 1];
ok(last && last.action === 'confirm', '确认走 confirm', JSON.stringify(last));
ok(!!doc.querySelector('.genimg'), '确认后正常出图');

console.log('\n结果：' + pass + ' 通过 / ' + fail + ' 失败');
process.exit(fail ? 1 : 0);
