/* v0.6 生图 / 改图回归（jsdom，stub fetch，不依赖真实模型与生图 Key）
   跑法：NODE_PATH=<node-workspace>/node_modules node tools/test_v06_gen.mjs */
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
Y.S.agent = 'designer';
Y.S.credits = 9999;
Y.AGENT_API.base = 'http://127.0.0.1:8787';
Y.AGENT_API.online = true;

// ---- stub：chat 给一份待确认方案；action 按 actionType 决定成败 ----
let sent = [];
let actionMode = 'ok';            // ok | error
let wantMock = true;
function img(name){ return { name, url: '/api/file/' + name, bytes: 1234 }; }

w.fetch = function(url, opt){
  const body = JSON.parse(opt.body || '{}');
  if (url.indexOf('/api/health') >= 0)
    return Promise.resolve({ json: () => Promise.resolve(
      { ok: true, model: 'deepseek-flash', llm_enabled: true, image: { enabled: true } }) });
  if (url.indexOf('/api/action') >= 0){
    sent.push({ url, body });
    if (actionMode === 'error')
      return Promise.resolve({ json: () => Promise.resolve(
        { ok: false, error: '未配置生图 Key' }) });
    return Promise.resolve({ json: () => Promise.resolve({
      ok: true, action: 'generate_pattern', mock: wantMock, cost: 60,
      changes: body.args && Object.keys(body.args).length ? body.args : null,
      images: [img(body.action === 'edit_pattern' ? 'edit-1.png' : 'gen-1.png')],
    }) });
  }
  // /api/chat：返回一份待确认方案（模拟模型识别到生图意图）
  return Promise.resolve({ json: () => Promise.resolve({
    ok: true, mode: 'llm', skills: ['generate_pattern'],
    reply: '按需求卡出一块无缝四方连续的花型，60 点，确认我就开工。',
    brief: { style: '法式浪漫', element: '花卉', color: '蓝白青花', scene: '女装连衣裙' },
    picks: [], images: null,
    pending: { action: 'generate_pattern',
               summary: '按需求卡（法式浪漫、花卉、蓝白青花、女装连衣裙）生成一块面料花型',
               cost: 60, args: { confirmed: false } },
  }) });
};

console.log('[1] 模型提方案 → 渲染待确认卡片（不是直接出图）');
Y.say('帮我生成一块法式小碎花');
await wait(); await wait();
const card = doc.querySelector('.confirm-card');
ok(!!card, '确认卡片渲染出来');
ok(card && card.textContent.includes('生成方案'), '标题是「生成方案」');
ok(card && card.textContent.includes('60 点'), '显示点数', card && card.textContent.trim().slice(0, 40));
ok(card && !!card.querySelector('[data-act="confirm-gen"]'), '有「确认生成」按钮');
ok(card && !!card.querySelector('[data-act="cancel-gen"]'), '有「先不要」按钮');
ok(!doc.querySelector('.genimg'), '还没点确认，**没有**图');
ok(Y.genOf('designer').cost === 60, '点数记在本角色的待确认状态里，还没扣',
   String(Y.genOf('designer').cost));

console.log('[2] 取消：不扣点、不请求生图');
const credits0 = Y.S.credits;
doc.querySelector('[data-act="cancel-gen"]').click();
await wait();
ok(!doc.querySelector('.confirm-card'), '确认卡片消失');
ok(Y.S.credits === credits0, '点数没动', String(Y.S.credits));
ok(sent.length === 1 && sent[0].body.action === 'cancel', '通知服务端取消',
   JSON.stringify(sent.map(s => s.body.action)));

console.log('[3] 重新提方案并确认 → 真出图');
Y.say('帮我生成一块法式小碎花');
await wait(); await wait();
const credits1 = Y.S.credits;
doc.querySelector('[data-act="confirm-gen"]').click();
await wait(); await wait();
const act = sent.filter(s => s.url.indexOf('/api/action') >= 0).pop();
ok(act && act.body.action === 'confirm', '确认走 /api/action · confirm',
   act && JSON.stringify(act.body));
ok(act && act.body.session_id, '带上 session_id');
const gi = doc.querySelector('.genimg');
ok(!!gi, '生成图渲染出来');
ok(gi && gi.querySelector('img').getAttribute('src') === 'http://127.0.0.1:8787/api/file/gen-1.png',
   '图片 src = 服务地址 + /api/file/ 路径', gi && gi.querySelector('img').getAttribute('src'));
ok(Y.S.credits === credits1 - 60, '确认后扣了 60 点', String(Y.S.credits));
ok(gi && gi.querySelector('.mock'), 'mock 图有「占位图」角标');
ok(doc.getElementById('msgs').textContent.includes('还没配生图 Key'), '文案说明是占位图、不是真图');

console.log('[4] 图下方有改图入口');
const bar = doc.querySelector('.edit-bar');
ok(!!bar, '编辑按钮行渲染出来');
const barBtns = bar ? Array.from(bar.querySelectorAll('.opt')).map(b => b.textContent) : [];
ok(barBtns.includes('改配色') && barBtns.includes('改元素') && barBtns.includes('改疏密度'),
   '含改配色/改元素/改疏密度', JSON.stringify(barBtns));
ok(barBtns.includes('改构图') && barBtns.includes('改尺度'), '也含改构图/改尺度');

console.log('[5] 点「改配色」→ 展开候选值');
doc.querySelector('[data-act="ed:dim:color"]').click();
await wait();
const bar2 = doc.querySelector('.edit-bar');
const vals = bar2 ? Array.from(bar2.querySelectorAll('.opt')).map(b => b.textContent) : [];
ok(Y.S.editDim === 'color', 'editDim 切到 color', String(Y.S.editDim));
ok(vals.includes('蓝白青花') && vals.includes('大地色系'), '列出配色候选值', JSON.stringify(vals));
ok(vals.includes('取消'), '有取消按钮');

console.log('[6] 选一个值 → 执行改图');
const credits2 = Y.S.credits;
doc.querySelector('[data-act="ed:do:color:蓝白青花"]').click();
await wait(); await wait();
const act2 = sent.filter(s => s.url.indexOf('/api/action') >= 0).pop();
ok(act2 && act2.body.action === 'edit_pattern', '走 edit_pattern', act2 && JSON.stringify(act2.body));
ok(act2 && act2.body.args && act2.body.args.color === '蓝白青花', 'args 里带上要改的维度',
   act2 && JSON.stringify(act2.body.args));
ok(Y.S.editDim === null, '改完收起候选值');
ok(Y.S.credits === credits2 - 60, '改图也扣了 60 点', String(Y.S.credits));
const imgs = doc.querySelectorAll('.genimg');
ok(imgs.length >= 2, '历史图 + 新图都在', String(imgs.length));
ok(doc.querySelectorAll('.edit-bar').length === 1, '编辑按钮只挂在当前这张图上',
   String(doc.querySelectorAll('.edit-bar').length));

console.log('[7] 生图失败 → 明确报错且退回点数');
actionMode = 'error';
const credits3 = Y.S.credits;
Y.editAct('do:density:密');
await wait(); await wait();
const bodyTxt = doc.getElementById('msgs').textContent;
ok(bodyTxt.includes('生图没跑通'), '给出失败提示', bodyTxt.slice(-60));
ok(bodyTxt.includes('未配置生图 Key'), '把服务端的错误原因透传出来');
ok(Y.S.credits === credits3, '失败把点数退回去了', String(Y.S.credits));

console.log('[8] 生图技能不会在对话里被模型自己确认');
ok(!doc.getElementById('msgs').textContent.includes('已经生成'), '没有「已经生成」这类谎报文案');

console.log('\n结果：' + pass + ' 通过 / ' + fail + ' 失败');
process.exit(fail ? 1 : 0);
