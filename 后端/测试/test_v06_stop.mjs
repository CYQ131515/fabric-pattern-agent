/* v0.6 三项改动回归（前端 jsdom）：
   [1] 跑动中的等待气泡有「终止」按钮，点了能真断请求、撤气泡、退点
   [2] 新消息会顶掉上一轮还在跑的等待（不会堆一串「正在…」）
   [3] 一次出四张：两张一排铺开、标出第几张、文案说清张数
   跑法：NODE_PATH=<node-workspace>/node_modules node tools/test_v06_stop.mjs */
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
Y.S.page = 'designer';
Y.S.credits = 9999;
Y.AGENT_API.base = 'http://127.0.0.1:8787';
Y.AGENT_API.online = true;

/* ---- fetch 桩：可以「挂住不返回」，被 abort 时像真 fetch 一样 reject ---- */
let hold = false, signals = [], settle = null, actionResp = null, chatResp = null;
function held(signal){
  signals.push(signal);
  return new Promise(function(_, rej){
    const err = new Error('aborted'); err.name = 'AbortError';
    if (signal){
      if (signal.aborted) return rej(err);
      signal.addEventListener('abort', function(){ rej(err); });
    }
    settle = function(){ /* 永远不主动返回，等测试手动收尾 */ };
  });
}
w.fetch = function(url, opt){
  const body = JSON.parse(opt.body || '{}');
  if (url.indexOf('/api/health') >= 0)
    return Promise.resolve({ json: () => Promise.resolve(
      { ok: true, model: 'deepseek-flash', llm_enabled: true }) });
  if (hold) return held(opt.signal);
  if (url.indexOf('/api/action') >= 0)
    return Promise.resolve({ json: () => Promise.resolve(actionResp) });
  return Promise.resolve({ json: () => Promise.resolve(chatResp) });
};

function msgs(){ return Y.msgs().querySelectorAll('.msg'); }
function lastAssistant(){
  const all = Y.msgs().querySelectorAll('.msg:not(.me) .bubble');
  return all.length ? all[all.length - 1].textContent : '';
}
function stopButtons(){ return Y.msgs().querySelectorAll('.stopbtn'); }

function clickStop(){
  const b = stopButtons()[0];
  if (b) b.click();
  return !!b;
}

/* ================================================= [1] 思考中的主动终止 */
console.log('[1] 思考中（还没出产出）能主动终止');
hold = true; signals = [];
Y.S.agent = 'designer';
Y.say('电子游戏元素配淡绿，用在床品上');
await wait(); await wait();
ok(stopButtons().length === 1, '等待气泡里有「终止」按钮',
   String(stopButtons().length));
ok(Y.WAIT && Y.WAIT.cost === 0, '聊天这轮不扣点（cost=0）');
const sig = signals[signals.length - 1];
ok(!!sig, '请求确实带上了可中断的信号');
ok(sig && sig.aborted === false, '点之前请求还活着');

const before = Y.S.credits;
clickStop();
await wait(); await wait();
ok(stopButtons().length === 0, '点完之后等待气泡撤掉了');
ok(sig && sig.aborted === true, '底层请求真的被断掉了');
ok(!Y.WAIT, '状态清干净了，可以接着聊');
ok(lastAssistant().indexOf('停下了') >= 0, '给一句人话交代', lastAssistant());
ok(lastAssistant().indexOf('点数一点没动') >= 0, '并说明没扣点');
ok(Y.S.credits === before, '确实一分没扣');

console.log('[1b] 终止之后可以立刻接着发消息');
hold = false;
chatResp = { ok: true, mode: 'llm', skills: [], reply: '接着说，我听着。',
             brief: { style: '法式浪漫', element: null, color: null, scene: null },
             picks: [], images: null };
Y.say('换个方向，要法式碎花');
await wait(); await wait();
ok(lastAssistant().indexOf('接着说') >= 0, '下一轮正常跑通（没被上一次终止卡住）');
ok(stopButtons().length === 0, '跑完不残留终止按钮');

/* ================================================= [2] 新消息顶掉上一轮 */
console.log('[2] 上一轮还在跑时又发一条，旧的等待让位');
hold = true; signals = [];
Y.say('先想个慢的');
await wait();
ok(stopButtons().length === 1, '先有一条等待');
Y.say('算了，我说这个');
await wait(); await wait();
ok(stopButtons().length === 1, '界面上始终只有一条等待', String(stopButtons().length));
ok(signals.length >= 2 && signals[0].aborted === true,
   '旧那次的请求被断掉（不会两个请求同时挂着）');
ok(lastAssistant().indexOf('停下了') < 0, '顶掉旧等待时不必啰嗦地报「停下了」');
hold = false;
clickStop();
await wait();

/* ================================================= [3] 出图这轮终止要退点 */
console.log('[3] 出图跑到一半喊停 → 退点');
hold = true; signals = [];
Y.genOf(Y.S.agent).cost = 240;
Y.genOf(Y.S.agent).action = 'generate_pattern';
const credits0 = Y.S.credits;
Y.runGen();
await wait(); await wait();
ok(Y.S.credits === credits0 - 240, '确认生成时先按 4 张扣了 240 点',
   String(credits0 - Y.S.credits));
ok(stopButtons().length === 1, '出图过程中同样有「终止」');
clickStop();
await wait(); await wait();
ok(Y.S.credits === credits0, '终止后点数退回', String(Y.S.credits));
ok(lastAssistant().indexOf('退回你账户') >= 0, '明说点数已退', lastAssistant());
ok(Y.genOf(Y.S.agent).cost === 240, '注意：终止的是「等」，不是把方案本身取消掉');

/* ================================================= [4] 一次四张怎么摆 */
console.log('[4] 一次四张：铺开 + 标注第几张 + 文案说清张数');
Y.S.agent = 'designer';
Y.S.page = 'designer';
Y.genOf('designer').cost = 0;
Y.S.editsel = null;
const imgs = [1, 2, 3, 4].map(function(i){
  return { name: 'gen-' + i + '.jpg', url: '/api/file/gen-' + i + '.jpg',
           mime: 'image/jpeg', bytes: 500000 + i };
});
Y.showGenResult('designer', null,
  { ok: true, images: imgs, cost: 240, model: 'doubao-seedream',
    gen: { name: 'generate_pattern', args: { n: 4 } }, brief: {} },
  true, '图出来了。');
await wait();
const grid = Y.msgs().querySelectorAll('.genimgs.grid');
ok(grid.length === 1, '多张时用网格铺开（不是竖着一长条）');
ok(grid.length && grid[0].querySelectorAll('.genimg').length === 4,
   '四张都在', grid.length ? String(grid[0].querySelectorAll('.genimg').length) : '0');
const caps = Array.prototype.map.call(
  Y.msgs().querySelectorAll('.genimg .cap'), function(e){ return e.textContent; });
ok(caps.length === 4, '四张各有标注', String(caps.length));
ok(caps.join('|').indexOf('第 1 张') >= 0 && caps.join('|').indexOf('第 4 张') >= 0,
   '标注写清是第几张（用户说「第二张」时对得上）', caps.join(' / '));
ok(lastAssistant().indexOf('一次出了 4 张') >= 0, '文案说清这次出了几张',
   lastAssistant());

console.log('[5] 单张时保持原样（不硬套网格）');
Y.showGenResult('designer', null,
  { ok: true, images: [imgs[0]], cost: 60, gen: null, brief: {} }, true, '图出来了。');
await wait();
ok(Y.msgs().querySelectorAll('.genimgs.grid').length === 1,
   '单张不进网格（网格还是只有上一条那一个）');

console.log('\n结果：' + pass + ' 通过 / ' + fail + ' 失败');
process.exit(fail ? 1 : 0);
