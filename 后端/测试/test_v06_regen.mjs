/* v0.6 四项细化回归：
   [1] 需求卡只走花型师那条链路（织印师发消息不带 brief、返回的 brief 也不 merge）
   [2] 设计师生图成功后自动重置需求卡
   [3] 出图消息下有「重新生成」按钮
   [4] 重新生成走 regenerate、按原参数重放、失败退点
   跑法：NODE_PATH=<node-workspace>/node_modules node tools/test_v06_regen.mjs */
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

let chatResp = null, confirmResp = null, regenResp = null;
let chatCalls = [], actionCalls = [];

w.fetch = function(url, opt){
  const body = JSON.parse(opt.body || '{}');
  if (url.indexOf('/api/health') >= 0)
    return Promise.resolve({ json: () => Promise.resolve(
      { ok: true, model: 'deepseek-flash', llm_enabled: true }) });
  if (url.indexOf('/api/action') >= 0){
    actionCalls.push(body);
    if (body.action === 'regenerate')
      return Promise.resolve({ json: () => Promise.resolve(regenResp) });
    return Promise.resolve({ json: () => Promise.resolve(confirmResp) });
  }
  chatCalls.push(body);
  return Promise.resolve({ json: () => Promise.resolve(chatResp) });
};

function tracePending(reply){
  return {
    ok: true, mode: 'llm', skills: ['trace_pattern'],
    reply: reply || '照片我看过了，能描。这次出 1 张。',
    brief: {}, picks: [], images: null,
    pending: { action: 'trace_pattern', cost: 60, args: { confirmed: false },
               fidelity: 'clean',
               qc: { level: 'good', usable: true, issues: [], advice: '' },
               summary: '描稿仿稿 · 从照片提取印花图案' },
  };
}
function genOk(extra){
  return Object.assign({
    ok: true, action: 'trace_pattern', cost: 60, fidelity: 'clean',
    images: [{ name: 'trace-1.jpg', url: '/api/file/trace-1.jpg', bytes: 45678 }],
    gen: { name: 'trace_pattern', args: { fidelity: 'clean', prompt_full: '把这张照片…' } },
    brief: {},
  }, extra || {});
}

/* ===== [1] 需求卡不跨角色 ===== */
console.log('[1] 需求卡只属于花型师这条链路');
Y.S.brief = { style: '法式浪漫', element: '花卉', color: '粉紫梦幻', scene: null };
chatCalls = [];
chatResp = tracePending();
Y.S.agent = 'engineer';
Y.say('照着这张描一下');
await wait(); await wait();
const engCall = chatCalls[chatCalls.length - 1];
ok(engCall && engCall.brief === undefined,
   '织印师发消息不带需求卡', JSON.stringify(engCall && engCall.brief));
ok(Y.S.brief.color === '粉紫梦幻', '本地需求卡没被这次发送动过');

console.log('[2] 织印师返回的 brief 不许 merge 回本地');
chatResp = Object.assign(tracePending('好的。'), { brief: { color: '蓝白青花' } });
Y.say('再描一张');
await wait(); await wait();
ok(Y.S.brief.color === '粉紫梦幻',
   '织印师回的 brief 被忽略（设计师那边是设计师）', Y.S.brief.color);

console.log('[3] 花型师照旧带需求卡往返');
Y.S.agent = 'designer';
Y.S.page = 'designer';
chatResp = { ok: true, mode: 'llm', skills: [], reply: '收到，我看看。',
             brief: { style: '法式浪漫', element: '花卉', color: '淡红', scene: null },
             picks: [], images: null };
Y.say('要淡红的玫瑰');
await wait(); await wait();
const desCall = chatCalls[chatCalls.length - 1];
ok(desCall && desCall.brief && desCall.brief.color === '粉紫梦幻',
   '花型师发消息带需求卡', JSON.stringify(desCall && desCall.brief));
ok(Y.S.brief.color === '淡红', '花型师返回的 brief 正常 merge（淡红这一档收下了）',
   Y.S.brief.color);

/* ===== [2] 出图后自动重置需求卡 ===== */
console.log('[4] 设计师生图成功后需求卡自动重置');
Y.S.page = 'designer';
Y.S.agent = 'designer';
Y.S.brief = { style: '新中式国风', element: '花卉', color: '淡红', scene: '女装连衣裙' };
Y.S.credits = 9999;
chatResp = {
  ok: true, mode: 'llm', skills: ['generate_pattern'], reply: '方案给你理好了。',
  brief: {}, picks: [], images: null,
  pending: { action: 'generate_pattern', cost: 60, args: { confirmed: false },
             summary: '按需求卡生成一块面料花型' },
};
Y.say('生成一块');
await wait(); await wait();
let confirmBtns = doc.querySelectorAll('[data-act="confirm-gen"]');
ok(confirmBtns.length >= 1, '有确认按钮');
confirmResp = {
  ok: true, action: 'generate_pattern', cost: 60,
  images: [{ name: 'gen-1.jpg', url: '/api/file/gen-1.jpg', bytes: 400000 }],
  gen: { name: 'generate_pattern',
         args: { prompt_full: '面料印花花型设计，新中式国风，花卉，淡红，女装连衣裙。…' } },
  brief: {}, brief_reset: true,
};
const creditsBefore = Y.S.credits;
confirmBtns[confirmBtns.length - 1].click();
await wait(); await wait();
ok(!!doc.querySelector('.genimg'), '出图了');
ok(Y.S.brief.style === null && Y.S.brief.color === null && Y.S.brief.scene === null,
   '需求卡四维被清空（图已落地，不再粘着旧标签）', JSON.stringify(Y.S.brief));
ok(Y.S.credits === creditsBefore - 60, '该扣的点数照扣',
   String(creditsBefore) + ' → ' + String(Y.S.credits));

/* ===== [3] 重新生成按钮 ===== */
console.log('[5] 出图消息下有「重新生成」按钮');
const genMsgs = doc.querySelectorAll('#msgs .msg');
let lastGenMsg = null;
genMsgs.forEach(function(m){ if (m.querySelector('.genimg')) lastGenMsg = m; });
ok(!!lastGenMsg, '找到出图那条消息');
const regenBtn = lastGenMsg && lastGenMsg.querySelector('[data-act="regen"]');
ok(!!regenBtn, '图下面挂着「重新生成」按钮');
ok(regenBtn && regenBtn.textContent.trim() === '重新生成',
   '按钮文案就叫重新生成', regenBtn && regenBtn.textContent.trim());
ok(regenBtn && regenBtn.classList.contains('opt'), '和「改图」按钮同一套样式，排在一排里');

console.log('[6] 描稿出图也有同一个按钮');
Y.S.agent = 'engineer';
Y.S.page = 'engineer';
chatResp = tracePending();
Y.say('描一张');
await wait(); await wait();
confirmResp = genOk();
confirmBtns = doc.querySelectorAll('[data-act="confirm-gen"]');
confirmBtns[confirmBtns.length - 1].click();
await wait(); await wait();
let traceMsg = null;
doc.querySelectorAll('#msgs .msg').forEach(function(m){ if (m.querySelector('.genimg')) traceMsg = m; });
ok(!!(traceMsg && traceMsg.querySelector('[data-act="regen"]')),
   '描稿仿稿的出图同样带重新生成');

/* ===== [4] 重新生成的行为 ===== */
console.log('[7] 点重新生成 → 走 regenerate，按原参数重放');
let before = Y.S.credits;
actionCalls = [];
regenResp = genOk();
traceMsg.querySelector('[data-act="regen"]').click();
await wait(); await wait();
const regenCall = actionCalls[actionCalls.length - 1];
ok(regenCall && regenCall.action === 'regenerate', '动作是 regenerate', JSON.stringify(regenCall));
ok(regenCall && regenCall.args && regenCall.args.spec
   && regenCall.args.spec.name === 'trace_pattern',
   '带回上一次的技能名，服务端照着重放',
   JSON.stringify(regenCall && regenCall.args));
ok(regenCall && regenCall.args.spec.args
   && String(regenCall.args.spec.args.prompt_full).length > 0,
   '带回完整提示词（需求卡已清空，靠它才拼得回同一张图）');
ok(Y.S.credits === before - 60, '重新生成同样扣点', String(before) + ' → ' + String(Y.S.credits));

console.log('[8] 服务端没记录 → 退点 + 说人话，不静默');
before = Y.S.credits;
regenResp = { ok: false, error: '没有可以重新生成的记录，请重新描述一次需求。' };
const genMsgNow = (function(){
  let m = null;
  doc.querySelectorAll('#msgs .msg').forEach(function(x){
    if (x.querySelector('[data-act="regen"]')) m = x;
  });
  return m;
})();
genMsgNow.querySelector('[data-act="regen"]').click();
await wait(); await wait();
ok(Y.S.credits === before, '失败了把点数退回去', String(before) + ' → ' + String(Y.S.credits));
const texts = Array.from(doc.querySelectorAll('#msgs .msg')).map(x => x.textContent).join('\n');
ok(texts.includes('重新生成') && texts.includes('没有可以重新生成的记录'),
   '把失败原因讲清楚');

console.log('[9] 展开改图维度选项后，重新生成按钮仍在（不然只有收起态点得到）');
/* render() 会重建 DOM，展开后必须重新查节点，不能拿旧引用断言 */
function liveGenMsg(){
  let m = null;
  doc.querySelectorAll('#msgs .msg').forEach(function(x){
    if (x.querySelector('.genimg')) m = x;
  });
  return m;
}
liveGenMsg().querySelector('[data-act="ed:dim:color"]').click();
await wait();
const live = liveGenMsg();
ok(!!live.querySelector('[data-act="regen"]'), '展开态下按钮还在');
const vals = live.querySelectorAll('[data-act^="ed:do:"]');
ok(vals.length > 0, '维度选项正常展开', String(vals.length));
ok(Y.S.editDim === 'color', '展开的是配色这一维', String(Y.S.editDim));

console.log('[10] 配色自由度：色板之外的颜色可以直接写');
ok(!!live.querySelector('[data-act="ed:custom"]'),
   '配色行里有「自定义…」（色板只给常用档，淡红就不在里面）');
live.querySelector('[data-act="ed:custom"]').click();
await wait();
const inp = doc.querySelector('.ed-input');
ok(!!inp, '点开后行里变成输入框');
ok(!doc.querySelector('.edit-bar .ed-input[disabled]'), '输入框可用');

console.log('[11] 空值不许提交（别把空字符串当成一个需求）');
actionCalls = [];
doc.querySelector('[data-act="ed:apply"]').click();
await wait();
ok(actionCalls.length === 0, '没写内容时什么都没发出去', JSON.stringify(actionCalls));
ok(!!doc.querySelector('.ed-input'), '输入框还在，等用户补上');

console.log('[12] 写「淡红」→ 走 edit_pattern，原样带着淡红');
confirmResp = genOk({ changes: { color: '淡红' } });
actionCalls = [];
doc.querySelector('.ed-input').value = '淡红';
doc.querySelector('[data-act="ed:apply"]').click();
await wait(); await wait();
const editCall = actionCalls[actionCalls.length - 1];
ok(editCall && editCall.action === 'edit_pattern', '改配色走 edit_pattern',
   JSON.stringify(editCall));
ok(editCall && editCall.args && editCall.args.color === '淡红',
   '自由配色原样传给服务端，没有被换成「最接近的档」',
   JSON.stringify(editCall && editCall.args));
const allText = Array.from(doc.querySelectorAll('#msgs .msg.me')).map(x => x.textContent).join('|');
ok(allText.includes('把配色改成淡红'), '用户气泡如实记下改了什么');

console.log('[13] 自定义入口不会把「重新生成」挤掉（取消后回到按钮态）');
liveGenMsg().querySelector('[data-act="ed:dim:density"]').click();
await wait();
ok(!!liveGenMsg().querySelector('[data-act="regen"]'), '换一维展开后重新生成仍在');
ok(!!liveGenMsg().querySelector('[data-act="ed:custom"]'), '别的维度也能自定义');

console.log('\n结果：' + pass + ' 通过 / ' + fail + ' 失败');
process.exit(fail ? 1 : 0);
