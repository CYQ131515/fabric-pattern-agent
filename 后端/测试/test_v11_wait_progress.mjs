/* v0.11 前端回归：织印师「看图定方案」这段别让用户干等。
   跑法：NODE_PATH=<node-workspace>/node_modules node 后端/测试/test_v11_wait_progress.mjs

   用户报的现象（截图）：
       ⟳ 正在看图、定分层方案…  已等 1:11   [终止]
   一分多钟一个字的进展都没有。查下来前端有两处配合不上：
     · proposeForEngineer 没传 psd:true → 秒表压根不轮询 /api/psd_progress，
       旁边那行小字永远是空的；
     · 「终止」只 abort 了自己的 fetch，服务端算法子进程还在跑云端任务，
       用户以为停了、钱还在扣。
   这个用例把两处都钉住：
     [1] 提案阶段秒表要写出"正在做什么"（不是只有秒表在走）
     [2] 分层跑动中照旧显示"第 N/M 层"（别为了提案把原来那行弄丢）
     [3] 等久了要如实交代（提案阶段的"最长两分钟"）
     [4] 提案请求带上 psd:true，才会去轮询进度
     [5] 点「终止」要通知服务端 cancel_run（fire-and-forget，不带 abort 信号）
*/
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const { JSDOM } = require('jsdom');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const HTML = process.env.PF_CLIENT_HTML
  ? path.resolve(process.env.PF_CLIENT_HTML)
  : path.resolve(HERE, '..', '..', '前端', 'PatternFlow花型智造-交互原型v0.7.html');
let pass = 0, fail = 0;
function ok(cond, label, extra){
  if (cond){ pass++; console.log('  ✓ ' + label); }
  else { fail++; console.log('  ✗ ' + label + (extra !== undefined ? '  → ' + extra : '')); }
}

const dom = new JSDOM(fs.readFileSync(HTML, 'utf8'), {
  runScripts: 'dangerously', pretendToBeVisual: true, url: 'http://127.0.0.1:8787/',
});
const w = dom.window, doc = w.document;
const wait = (ms) => new Promise(r => setTimeout(r, ms || 40));
const Y = w.__YQ;
Y.AGENT_API.base = 'http://127.0.0.1:8787';
Y.AGENT_API.online = true;
Y.S.credits = 9999;
Y.S.agent = 'engineer';
Y.S.page = 'engineer';

/* 记下每一次请求（含它带的 abort 信号），用例据此判断前端发了什么 */
let calls = [];
let psdProgress = { ok: true, running: true, cmd: 'plan',
                    label: '正在请视觉模型看这张图、定分层', elapsed: 7 };
/* propose 挂住不返回，模拟"还在看图" */
let holdPropose = true;
w.fetch = function(url, opt){
  const body = opt && opt.body ? JSON.parse(opt.body) : null;
  calls.push({ url, body, signal: opt && opt.signal });
  const json = (o) => Promise.resolve({ json: () => Promise.resolve(o) });
  if (url.indexOf('/api/health') >= 0) return json({ ok: true, llm_enabled: true });
  if (url.indexOf('/api/psd_progress') >= 0) return json(psdProgress);
  if (url.indexOf('/api/action') >= 0 && body && body.action === 'propose'){
    if (!holdPropose) return json({ ok: true, pending: {
      action: 'layer_psd', cost: 45, count: 1, summary: '分层 PSD · 3 层（主花、叶片、花苞）' } });
    return new Promise(function(_, rej){
      const sig = opt && opt.signal;
      if (!sig) return;                              // 没有信号就永远挂着
      const err = new Error('aborted'); err.name = 'AbortError';
      sig.addEventListener('abort', function(){ rej(err); });
    });
  }
  if (url.indexOf('/api/action') >= 0) return json({ ok: true, stopped_any: true });
  return json({ ok: false, error: 'unexpected ' + url });
};
const actCalls = (name) => calls.filter(c => c.body && c.body.action === name);
const lastAct = (name) => { const a = actCalls(name); return a.length ? a[a.length - 1] : null; };

function runMetaTexts(){
  return Array.prototype.map.call(
    Y.msgs().querySelectorAll('.runmeta'), e => e.textContent);
}

async function main(){
  /* ================================================[1] 提案阶段说什么 */
  console.log('[1] 提案阶段（看图定层）秒表旁边要写出"在做什么"');
  Y.proposeForEngineer('layer_psd', '正在看图、定分层方案');
  await wait(60);
  ok(Y.WAIT && Y.WAIT.m && Y.WAIT.m.psd === true,
     '提案请求带上了 psd 标记（秒表才会去轮询进度）', Y.WAIT && Y.WAIT.m && Y.WAIT.m.psd);
  ok(actCalls('propose').length === 1, '提案请求发出去了');
  ok(actCalls('propose')[0].body.skill === 'layer_psd',
     '提的就是分层 PSD（不是别的技能）', actCalls('propose')[0].body.skill);

  Y.PSD_PROG = psdProgress;
  Y.paintRunMeta();
  let texts = runMetaTexts();
  ok(texts.length === 1, '等待气泡里有那行小字', String(texts.length));
  ok(texts[0].indexOf('正在请视觉模型看这张图、定分层') >= 0,
     '写出的是服务端报的"在做什么"，不是一句空话', texts[0]);
  ok(texts[0].indexOf('已等') >= 0, '秒表照旧在（用户要知道等了多久）', texts[0]);
  /* 静态那句「正在看图、定分层方案…」必须让位 —— 两句并排是一个意思说两遍 */
  const labNodes = Y.msgs().querySelectorAll('.waitlabel');
  ok(labNodes.length === 1, '静态那句还在 DOM 里（只是被收起来）', String(labNodes.length));
  ok(labNodes[0] && labNodes[0].style.display === 'none',
     '有实时说法时，静态那句收起来（不重复说两遍）',
     labNodes[0] && labNodes[0].style.display);
  ok(Y.liveLabel() === '正在请视觉模型看这张图、定分层',
     'liveLabel() 报得出服务端那句话');

  console.log('[1b] 进度还没回来之前不许编，也不许空着');
  Y.PSD_PROG = null;
  Y.paintRunMeta();
  texts = runMetaTexts();
  ok(texts[0].indexOf('已等') >= 0 && texts[0].indexOf('层') < 0,
     '没进度时就只报秒表，不瞎编"第几层"', texts[0]);
  ok(labNodes[0] && labNodes[0].style.display !== 'none',
     '没有实时说法时，静态那句照旧显示（气泡不会空着）',
     labNodes[0] && labNodes[0].style.display);
  ok(Y.liveLabel() === '', '没进度时 liveLabel() 是空的');

  /* ================================================[2] 分层跑动照旧 */
  console.log('[2] 真分层跑动时照旧显示"第 N/M 层"（别为了提案把老那行弄丢）');
  Y.PSD_PROG = { ok: true, running: true, cmd: 'run', label: '正在分层',
                 phase: 'layer', index: 2, total: 3 };
  Y.paintRunMeta();
  texts = runMetaTexts();
  ok(texts[0].indexOf('第 2/3 层') >= 0, '显示正在跑第几层', texts[0]);
  ok(labNodes[0] && labNodes[0].style.display !== 'none',
     '真分层时静态那句照旧显示（"第几层"和"正在看图"说的不是一回事，不冲突）',
     labNodes[0] && labNodes[0].style.display);

  /* ================================================[3] 等久了如实说 */
  console.log('[3] 提案等过一分钟要如实交代（别让人以为还能等很久）');
  Y.PSD_PROG = psdProgress;                        // 提案：没有 total
  Y.RUN_SEC = 70;
  texts = [Y.runMetaText()];
  ok(texts[0].indexOf('最长两分钟') >= 0,
     '提案超过一分钟就说明这一步的上限', texts[0]);
  Y.RUN_SEC = 20;
  ok(Y.runMetaText().indexOf('最长两分钟') < 0,
     '刚等 20 秒不用吓唬用户', Y.runMetaText());
  Y.RUN_SEC = 310;
  ok(Y.runMetaText().indexOf('15 分钟') >= 0,
     '拖到 5 分钟以上给出分层那套上限说明', Y.runMetaText());
  Y.RUN_SEC = 0;

  /* ================================================[5] 终止要通知服务端 */
  console.log('[5] 点「终止」要通知服务端把算法子进程一起掐掉');
  const before = actCalls('cancel_run').length;
  const stopBtn = Y.msgs().querySelectorAll('.stopbtn')[0];
  ok(!!stopBtn, '等待气泡里有「终止」按钮');
  stopBtn.click();
  await wait(60);
  ok(actCalls('cancel_run').length === before + 1,
     '确实发了一条 cancel_run 给服务端',
     JSON.stringify(actCalls('cancel_run').map(c => c.body)));
  const cc = lastAct('cancel_run');
  ok(cc && cc.body.agent === 'engineer', '带上了当前角色（只停这一槽）', cc && cc.body.agent);
  ok(cc && cc.signal === undefined,
     '这条请求不带 abort 信号 —— 它必须真的发出去，不能跟着一起被断掉');
  ok(Y.msgs().querySelectorAll('.stopbtn').length === 0, '等待气泡撤掉了');
  ok(!Y.WAIT, '状态清干净了，可以接着聊');
  const bubbles = Y.msgs().querySelectorAll('.msg:not(.me) .bubble');
  const lastTxt = bubbles.length ? bubbles[bubbles.length - 1].textContent : '';
  ok(lastTxt.indexOf('停下了') >= 0, '给一句人话交代', lastTxt);

  /* ================================================[4] 不联网也不能炸 */
  console.log('[4] 没有网络时通知终止不该报错（吞掉就行）');
  const savedBase = Y.AGENT_API.base;
  Y.AGENT_API.base = '';
  let threw = false;
  try { Y.notifyStop(); } catch (e) { threw = true; }
  ok(!threw, '离线时 notifyStop() 直接返回，不抛异常');
  Y.AGENT_API.base = savedBase;

  Y.endRun();
  console.log('\n结果：' + pass + ' 通过 / ' + fail + ' 失败');
  process.exit(fail ? 1 : 0);
}
main();
