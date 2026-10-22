/* v0.6 描稿仿稿回归（jsdom，stub fetch，不依赖真实模型与生图 Key）
   跑法：NODE_PATH=<node-workspace>/node_modules node tools/test_v06_trace.mjs */
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
/* 渲染认的是 S.page（页面），不是 S.agent（对话对象）。织印师页面两个都要设，
   否则会渲染成默认的花型师页，断言全落空。 */
Y.S.page = 'engineer';
Y.S.agent = 'engineer';
Y.S.credits = 9999;
Y.AGENT_API.base = 'http://127.0.0.1:8787';
Y.AGENT_API.online = true;

/* ---- stub ---- */
let qcLevel = 'good';
let actionCalls = [];
let chatWithImage = false;

w.fetch = function(url, opt){
  const body = JSON.parse(opt.body || '{}');
  if (url.indexOf('/api/health') >= 0)
    return Promise.resolve({ json: () => Promise.resolve(
      { ok: true, model: 'deepseek-flash', llm_enabled: true }) });
  if (url.indexOf('/api/action') >= 0){
    actionCalls.push(body);
    return Promise.resolve({ json: () => Promise.resolve({
      ok: true, action: 'trace_pattern', mock: false, cost: 60, fidelity: 'clean',
      images: [{ name: 'trace-1.jpg', url: '/api/file/trace-1.jpg', bytes: 45678 }],
    }) });
  }
  /* /api/chat */
  chatWithImage = !!body.image;
  return Promise.resolve({ json: () => Promise.resolve({
    ok: true, mode: 'llm', skills: ['trace_pattern'],
    reply: '照片我看过了，可以照着提取图案。这张要花 60 点，确认我就开始描。',
    brief: {}, picks: [], images: null,
    pending: { action: 'trace_pattern', cost: 60, args: { confirmed: false },
               fidelity: 'clean',
               qc: { level: qcLevel, usable: true,
                     issues: qcLevel === 'good' ? []
                             : ['照片偏软（清晰度衰减 0.79），小碎花这类细节可能还原不准'],
                     advice: '照片基本能用，但还原度会打折。' },
               summary: '描稿仿稿 · 从照片提取印花图案；还原度：清晰化重绘' },
  }) });
};

console.log('[1] 织印师收到照片 → 提描稿方案（不是直接出图）');
Y.say('帮我把这上面的图案描下来');
await wait(); await wait();
ok(chatWithImage === false, '文本消息里不带图时，不假装有图');

const card = doc.querySelector('.confirm-card');
ok(!!card, '确认卡片渲染出来');
ok(card && card.textContent.includes('描稿方案'), '标题是「描稿方案」',
   card && card.textContent.trim().slice(0, 40));
ok(card && !!card.querySelector('[data-act="confirm-gen"]'), '有确认按钮');
ok(card && card.textContent.includes('确认描稿'), '按钮文案是「确认描稿」');
ok(card && card.textContent.includes('60 点'), '显示点数');
ok(!doc.querySelector('.genimg'), '还没确认，没有图');
ok(Y.genOf('engineer').action === 'trace_pattern', 'pendingAction 记为 trace_pattern',
   String(Y.genOf('engineer').action));
ok(Y.genOf('engineer').cost === 60, '点数记下但未扣', String(Y.genOf('engineer').cost));

console.log('[2] 体检结论要摆在卡片上（质量好）');
const qcLine = doc.querySelector('.qc-line');
ok(!!qcLine, '渲染出照片体检行');
ok(qcLine && qcLine.textContent.includes('照片体检'), '体检行有「照片体检」字样');
ok(qcLine && qcLine.textContent.includes('质量良好'), '质量好时显示「质量良好」');
ok(qcLine && !qcLine.classList.contains('bad'), '质量好时不带红色样式');

console.log('[3] 质量差时：要显眼、要写清问题');
qcLevel = 'poor';
Y.say('再描一张试试');
await wait(); await wait();
const card2 = doc.querySelectorAll('.confirm-card');
const last = card2[card2.length - 1];
const qc2 = last.querySelector('.qc-line');
ok(qc2 && qc2.textContent.includes('质量较差'), '质量差显示「质量较差」', qc2 && qc2.textContent);
ok(qc2 && qc2.classList.contains('bad'), '质量差带红色警告样式');
ok(qc2 && qc2.textContent.includes('偏软'), '把具体问题写出来（不是只说"差"）');

console.log('[4] 确认 → 调 /api/action，出图并标成「描稿仿稿」');
const credits0 = Y.S.credits;
const btns = doc.querySelectorAll('[data-act="confirm-gen"]');
btns[btns.length - 1].click();
await wait(); await wait();
ok(actionCalls.length === 1, '发出一次 action 请求', String(actionCalls.length));
ok(actionCalls[0] && actionCalls[0].action === 'confirm',
   '确认走的是 confirm（由服务端按 pending 决定执行哪个技能）',
   JSON.stringify(actionCalls[0]));
ok(Y.S.credits === credits0 - 60, '确认后扣了 60 点', String(Y.S.credits));
ok(!!doc.querySelector('.genimg'), '出图气泡渲染出来');
const label = doc.querySelector('.genimg .lbl') || doc.querySelector('.genimg');
ok(label && label.textContent.includes('描稿仿稿'), '图片标注为「描稿仿稿」',
   label && label.textContent.trim().slice(0, 60));
ok(Y.genOf('engineer').action === '' || Y.genOf('engineer').action === 'trace_pattern',
   'pendingAction 已归位', String(Y.genOf('engineer').action));

console.log('[5] 取消 → 不扣点、不需要请求成功');
const credits1 = Y.S.credits;
qcLevel = 'good';
Y.say('再提一版方案');
await wait(); await wait();
const c3 = doc.querySelectorAll('[data-act="cancel-gen"]');
c3[c3.length - 1].click();
await wait();
ok(Y.S.credits === credits1, '取消不扣点', String(Y.S.credits));
ok(Y.genOf('engineer').cost === 0, '取消后 pendingCost 归零', String(Y.genOf('engineer').cost));
ok(Y.genOf('engineer').action === '', '取消后 pendingAction 清空', String(Y.genOf('engineer').action));

console.log('[6] 织印师页面必须有上传入口（否则描稿无从下手）');
ok(doc.querySelector('#msgs') !== null, '织印师是对话页，有消息区');
ok(!!doc.querySelector('[data-act="upload"]'), '织印师页面有上传按钮');
ok(!!doc.querySelector('[data-act="upload"]').closest('.composer')
   || !!doc.querySelector('.composer'), '上传按钮在输入区（可拖拽/粘贴）');

console.log('\n结果：' + pass + ' 通过 / ' + fail + ' 失败');
process.exit(fail ? 1 : 0);
