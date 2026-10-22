/* v0.6 跨角色串台回归（前端）。
   三个机器人共用一个会话，但「待确认方案」和「可改的那张图」必须各归各的。
   出过的 bug：织印师提了描稿方案没确认，用户切到花型师点「改风格」，
   结果服务端用织印师留在会话里的方案执行了描稿，花型师页上出来一张描稿图。

   跑法：NODE_PATH=<node-workspace>/node_modules node tools/test_v06_xchar.mjs */
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

Y.AGENT_API.base = 'http://127.0.0.1:8787';
Y.AGENT_API.online = true;
Y.S.credits = 9999;

let pendingResp = null, actionCalls = [], chatCalls = [];
w.fetch = function(url, opt){
  const body = JSON.parse(opt.body || '{}');
  if (url.indexOf('/api/health') >= 0)
    return Promise.resolve({ json: () => Promise.resolve({ ok: true, llm_enabled: true }) });
  if (url.indexOf('/api/action') >= 0){
    actionCalls.push(body);
    return Promise.resolve({ json: () => Promise.resolve({
      ok: true, action: body.action || 'edit_pattern', cost: 60,
      images: [{ name: 'x.jpg', url: '/api/file/x.jpg', mime: 'image/jpeg', bytes: 1000 }],
    }) });
  }
  chatCalls.push(body);
  return Promise.resolve({ json: () => Promise.resolve(pendingResp) });
};

function tracePending(){
  return {
    ok: true, mode: 'llm', skills: ['trace_pattern'],
    reply: '照片我看过了，能描。这次出 1 张。', brief: {}, picks: [], images: null,
    pending: { action: 'trace_pattern', cost: 60, args: { confirmed: false },
               fidelity: 'clean',
               qc: { level: 'good', usable: true, issues: [], advice: '' },
               summary: '描稿仿稿 · 从照片提取印花图案' },
  };
}
function genPending(){
  return {
    ok: true, mode: 'llm', skills: ['generate_pattern'],
    reply: '方案给你理好了，4 张一共 240 点。', brief: {}, picks: [], images: null,
    pending: { action: 'generate_pattern', cost: 240, args: { confirmed: false },
               count: 4, summary: '按需求卡生成一块面料花型（4 张）' },
  };
}
function imgs(url, mime){
  return [{ name: url.split('/').pop(), url: url, mime: mime || 'image/jpeg', bytes: 1000 }];
}

/* ============ [1] 待确认状态按角色存 ============ */
console.log('[1] 待确认状态按角色存：织印师的方案不会跑到花型师页');
Y.S.page = 'engineer'; Y.S.agent = 'engineer';
Y.S.chat.engineer.length = 0;
pendingResp = tracePending();
Y.say('帮我把这上面的图案描下来');
await wait(); await wait();

ok(Y.genOf('engineer').action === 'trace_pattern', '织印师的状态记为 trace_pattern',
   Y.genOf('engineer').action);
ok(Y.genOf('engineer').cost === 60, '点数记在织印师名下',
   String(Y.genOf('engineer').cost));
ok(Y.genOf('designer').action === '' && Y.genOf('designer').cost === 0,
   '花型师那份是独立的、没被写脏', JSON.stringify(Y.genOf('designer')));
ok(!!doc.querySelector('.confirm-card'), '织印师页上确认卡片渲染出来了');

/* ============ [2] 切角色不继承 ============ */
console.log('[2] 切到花型师后，不继承织印师的待确认方案');
Y.S.page = 'designer'; Y.S.agent = 'designer';
Y.render();
ok(Y.genOf().action === '', '当前角色拿到的是自己的（空）', Y.genOf().action);
ok(Y.genOf().cost === 0, '点数不串', String(Y.genOf().cost));
ok(Y.genOf('engineer').action === 'trace_pattern', '织印师那份还在（没被顺走）');
ok(!doc.querySelector('.confirm-card'), '花型师页上没有织印师的确认卡片');

/* ============ [3] 请求要带上是哪个角色点的 ============ */
console.log('[3] 界面请求带上「哪个机器人页面上点的」');
actionCalls.length = 0;
Y.apiAction({ action: 'confirm' }, function(){});
await wait();
ok(actionCalls[0] && actionCalls[0].agent === 'designer', '花型师的确认带 designer',
   JSON.stringify(actionCalls[0]));

Y.S.page = 'engineer'; Y.S.agent = 'engineer';
Y.apiAction({ action: 'confirm' }, function(){});
await wait();
ok(actionCalls[1] && actionCalls[1].agent === 'engineer', '织印师的确认带 engineer',
   JSON.stringify(actionCalls[1]));

Y.S.page = 'designer'; Y.S.agent = 'designer';
Y.apiAction({ action: 'edit_pattern', args: { style: '波西米亚' } }, function(){});
await wait();
ok(actionCalls[2] && actionCalls[2].agent === 'designer'
   && actionCalls[2].action === 'edit_pattern',
   '「改风格」发的是 edit_pattern 且带 designer', JSON.stringify(actionCalls[2]));

/* ============ [4] 是不是描稿，看服务端执行的 action ============ */
console.log('[4] 是不是描稿，看服务端实际执行的 action，不看有没有 fidelity 字段');
Y.S.chat.designer.length = 0;
Y.showGenResult('designer', null,
  { ok: true, cost: 60, model: 'm', action: 'edit_pattern', fidelity: 'strict',
    images: imgs('/api/file/e.jpg') },
  true, '风格改好了。');
await wait();
const box1 = Y.msgs();
ok(box1.textContent.indexOf('描稿仿稿') < 0,
   'action=edit_pattern 时，就算带着 fidelity 也不标成「描稿仿稿」',
   box1.textContent.trim().slice(0, 90));
ok(box1.textContent.indexOf('按照片提取') < 0, '文案也不是描稿那一套');
ok(box1.textContent.indexOf('改好了') >= 0, '保留改图自己的收尾话术');

console.log('[4b] 真描稿仍然标成描稿');
Y.S.page = 'engineer'; Y.S.agent = 'engineer';
Y.S.chat.engineer.length = 0;
Y.showGenResult('engineer', null,
  { ok: true, cost: 60, model: 'm', action: 'trace_pattern',
    images: imgs('/api/file/t.jpg') },
  true, '描稿完成。');
await wait();
ok(Y.msgs().textContent.indexOf('描稿仿稿') >= 0, 'action=trace_pattern → 标「描稿仿稿」');

/* ============ [5] 「可改的那张图」也按角色分 ============ */
console.log('[5] 「可改的那张图」各归各的');
Y.S.page = 'designer'; Y.S.agent = 'designer';
Y.S.chat.designer.length = 0;
Y.showGenResult('designer', null,
  { ok: true, cost: 240, model: 'm', action: 'generate_pattern',
    images: imgs('/api/file/g.jpg') },
  true, '图出来了。');
await wait();
const dEdit = Y.genOf('designer').editMsgId;
ok(!!dEdit, '花型师记下了自己那张可改的图');
ok(!!doc.querySelector('[data-act="ed:dim:color"]'), '花型师页上挂着改图按钮');

Y.S.page = 'engineer'; Y.S.agent = 'engineer';
Y.S.chat.engineer.length = 0;
Y.showGenResult('engineer', null,
  { ok: true, cost: 60, model: 'm', action: 'trace_pattern',
    images: imgs('/api/file/t2.jpg') },
  true, '描稿完成。');
await wait();
ok(Y.genOf('designer').editMsgId === dEdit,
   '织印师出图没有抢走花型师的改图入口',
   Y.genOf('designer').editMsgId + ' vs ' + dEdit);

/* ============ [6] 两个角色的方案并存 ============ */
console.log('[6] 两个角色各自留着方案，互不覆盖');
Y.S.page = 'designer'; Y.S.agent = 'designer';
Y.S.chat.designer.length = 0;
pendingResp = genPending();
Y.say('帮我生成一块法式小碎花');
await wait(); await wait();
ok(Y.genOf('designer').action === 'generate_pattern', '花型师记下生成方案');
ok(Y.genOf('engineer').cost === 60 || Y.genOf('engineer').action === '',
   '织印师那份不受影响', JSON.stringify(Y.genOf('engineer')));

console.log('\n结果：' + pass + ' 通过 / ' + fail + ' 失败');
process.exit(fail ? 1 : 0);
