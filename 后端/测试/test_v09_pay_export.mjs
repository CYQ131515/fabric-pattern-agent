/* v0.9 回归：客服页的充值扫码卡片 + 花型清单导出 + 不再挂花型风格快捷条
   跑法：NODE_PATH=<node-workspace>/node_modules node 测试/test_v09_pay_export.mjs

   这一版对着用户的两条反馈写：
     1. 「充值怎么又让我转人工？直接给二维码、能选微信/支付宝」
     2. 「导出怎么反问我项目名，还说查不到下载记录？我要一份能真下载的表」
   —— 所以断言的重点是"卡片真在他眼前""文件真能下"，不是文案对不对仗。 */
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
  else { fail++; console.log('  ✗ ' + label + (extra ? '  → ' + extra : '')); }
}

const dom = new JSDOM(fs.readFileSync(HTML, 'utf8'), {
  runScripts: 'dangerously', pretendToBeVisual: true, url: 'http://127.0.0.1:8787/',
});
const w = dom.window, doc = w.document;
const wait = () => new Promise(r => setTimeout(r, 70));
const Y = w.__YQ;

Y.AGENT_API.base = 'http://127.0.0.1:8787';
Y.AGENT_API.online = true;
Y.AGENT_API.enabled = true;
Y.S.credits = 500;

/* 一个小工具：让下一轮 /api/chat 回指定的东西 */
let chatBody = null, chatReply = null;
w.fetch = function(url, opt){
  const body = JSON.parse((opt && opt.body) || '{}');
  if (String(url).indexOf('/api/chat') >= 0){
    chatBody = body;
    return Promise.resolve({ json: () => Promise.resolve(chatReply) });
  }
  return Promise.resolve({ json: () => Promise.resolve({ ok: true }) });
};

function botMsg(){
  const msgs = doc.querySelectorAll('#msgs .msg:not(.me)');
  return msgs[msgs.length - 1];
}
async function ask(text){
  Y.say(text);
  await wait(); await wait();
  return botMsg();
}

/* =====================================================================
   [1] 客服回复下方：要充值卡片，不要花型风格快捷条
   ===================================================================== */
console.log('\n[1] 客服对话：充值给卡片，不再挂风格/趋势快捷条');
Y.go('assistant');
chatReply = { ok: true, mode: 'llm',
  reply: '扫下面的码就能充，微信和支付宝都可以，充多少自己选，付完点数立刻到账。',
  skills: [] };
let bot = await ask('怎么充值和计费？');

ok(!!bot.querySelector('.paycard'), '回复下方挂出了充值卡片');
ok(!bot.querySelector('.opt-row .opt'), '客服回复下方没有花型风格快捷条',
   bot.querySelector('.opt-row') ? bot.querySelector('.opt-row').textContent : '—');
ok(bot.textContent.indexOf('看流行趋势推荐') < 0, '客服页不出现「看流行趋势推荐」');
ok(!bot.querySelector('button.trend'), '没有趋势按钮');

/* =====================================================================
   [2] 充值卡片：二维码 + 微信/支付宝可选 + 档位 + 模拟支付
   ===================================================================== */
console.log('\n[2] 充值卡片：二维码 + 支付方式可选');
const card = bot.querySelector('.paycard');
const tabs = Array.from(card.querySelectorAll('.paym .pm'));
ok(tabs.length === 2, '支付方式两个（微信/支付宝）', tabs.length);
ok(tabs.map(t => t.textContent.trim()).join('|') === '微信支付|支付宝',
   '文案是微信支付 / 支付宝', tabs.map(t => t.textContent.trim()).join('|'));
ok(!!card.querySelector('svg.qr'), '卡片里有二维码图形');
ok(card.querySelectorAll('.pack').length === 3, '三个金额档位可选',
   card.querySelectorAll('.pack').length);
ok(card.textContent.indexOf('订单号') >= 0, '带订单号（对账用）');
ok(!!card.querySelector('[data-act="pay-ok-chat"]'), '有「模拟支付成功」按钮');
ok(tabs[0].getAttribute('aria-pressed') === 'true', '默认选中微信支付');

const qr0 = card.querySelector('svg.qr').innerHTML;
tabs[1].click();
await wait();
const card2 = botMsg().querySelector('.paycard');
const tabs2 = Array.from(card2.querySelectorAll('.paym .pm'));
ok(tabs2[1].getAttribute('aria-pressed') === 'true', '点支付宝后它变成选中态');
ok(tabs2[0].getAttribute('aria-pressed') === 'false', '微信不再是选中态');
ok(card2.querySelector('svg.qr').innerHTML !== qr0,
   '换支付方式后二维码跟着换（挂同一个码会让人以为选错了）');

/* 选档位 + 支付成功 */
const packBtns = Array.from(card2.querySelectorAll('.pack'));
packBtns[1].click();
await wait();
ok(botMsg().querySelectorAll('.pack')[1].getAttribute('aria-pressed') === 'true',
   '点第二个档位后它被选中');
const before = Y.S.credits, ledger0 = Y.S.ledger.length;
botMsg().querySelector('[data-act="pay-ok-chat"]').click();
await wait();
ok(Y.S.credits > before, '模拟支付成功后点数到账', Y.S.credits + ' > ' + before);
ok(Y.S.ledger.length === ledger0 + 1, '流水里多了一条充值记录');
ok(Y.S.ledger[Y.S.ledger.length - 1].t.indexOf('支付宝') >= 0,
   '流水记下了用的哪种支付方式', Y.S.ledger[Y.S.ledger.length - 1].t);

/* =====================================================================
   [3] 花型师那边照旧：风格候选 + 趋势按钮还在
   ===================================================================== */
console.log('\n[3] 花型师对话不受影响：风格候选与趋势按钮照旧');
Y.go('designer');
chatReply = { ok: true, mode: 'llm',
  reply: '这个花型主要想表达什么风格？配色上想走哪一路？',
  brief: {}, skills: ['update_brief'] };
bot = await ask('帮我找个花型');
const dOpts = bot.querySelectorAll('.opt-row .opt');
ok(dOpts.length > 0, '花型师回复下方还有快捷条', dOpts.length);
ok(Array.from(dOpts).some(b => b.textContent.indexOf('看流行趋势推荐') >= 0),
   '花型师那边趋势按钮还在（那是他的工具）');
ok(!bot.querySelector('.paycard'), '花型师页不会冒出充值卡片');

/* =====================================================================
   [4] 导出花型清单：真文件 + 真下载按钮
   ===================================================================== */
console.log('\n[4] 导出花型清单：回复带真实下载地址 + 结果卡');
Y.S.confirmed = [{ pid:'PF-OWN-2026-000128', at:'10-01 09:00' },
                 { pid:'PF-OWN-2026-000131', at:'10-01 09:01' }];
Y.go('assistant');
chatReply = { ok: true, mode: 'llm',
  reply: '已经按你选中的 2 款整理好了，点下面的按钮下载。',
  skills: [],
  export: { ok:true, count:2, note:'已按你选中的 2 款生成清单表格。',
            items:[{ id:'PF-OWN-2026-000128', name:'山茶小径', style:'法式浪漫',
                     element:'花卉', color:'莫兰迪灰调', scene:'女装连衣裙' },
                   { id:'PF-OWN-2026-000131', name:'青花缠枝', style:'新中式国风',
                     element:'叶片藤蔓', color:'蓝白青花', scene:'丝巾' }] },
  downloads: [{ name:'pattern-list-20261014-120000.xlsx',
                url:'/api/file/pattern-list-20261014-120000.xlsx',
                bytes: 4096, label:'花型清单.xlsx' }] };
bot = await ask('帮我导出花型清单');

ok(chatBody && chatBody.selected
   && chatBody.selected.join(',') === 'PF-OWN-2026-000128,PF-OWN-2026-000131',
   '发请求时把"我的花型清单"同步给了服务端（服务端才查得到）',
   chatBody && JSON.stringify(chatBody.selected));
const dl = bot.querySelector('.dlchip');
ok(!!dl, '回复下方有下载按钮');
ok(dl && dl.getAttribute('href').indexOf('/api/file/pattern-list-') >= 0,
   '下载按钮指到真实文件地址', dl && dl.getAttribute('href'));
ok(dl && dl.getAttribute('download') === '花型清单.xlsx',
   '下载文件名是人话「花型清单.xlsx」', dl && dl.getAttribute('download'));
ok(dl && dl.textContent.indexOf('4 KB') >= 0, '按钮上标了文件大小', dl && dl.textContent);
const exCard = bot.querySelector('.confirm-card');
ok(!!exCard, '有清单结果卡');
ok(exCard.textContent.indexOf('已按你选中的 2 款整理好') >= 0, '卡上说清整理了几款');
ok(exCard.querySelectorAll('.exrow').length === 2, '结果卡把两款都列出来了',
   exCard.querySelectorAll('.exrow').length);
ok(exCard.textContent.indexOf('山茶小径') >= 0 && exCard.textContent.indexOf('青花缠枝') >= 0,
   '列的是具体哪几款，不是一句空话');
ok(exCard.textContent.indexOf('查不到') < 0 && exCard.textContent.indexOf('转人工') < 0,
   '不说"查不到/转人工"');
ok(!!exCard.querySelector('[data-act="goto-assets"]'), '给了去「交付与下载」的入口');

/* =====================================================================
   [5] 没选过花型时：照实说先去选款
   ===================================================================== */
console.log('\n[5] 没选过花型：照实说"先去选款"，不编也不推给人工');
Y.S.confirmed = [];
chatReply = { ok: true, mode: 'llm',
  reply: '你还没在花型选择里确认过花型，先去挑几款，我马上给你出一张表。',
  skills: [],
  export: { ok:true, count:0, items:[], downloads:[],
            note:'你还没在花型选择里确认过花型，所以这边没有清单可以整理。' },
  downloads: [] };
bot = await ask('帮我导出花型清单');
ok(!bot.querySelector('.dlchip'), '没有文件就别摆下载按钮（摆个假的更糟）');
const ex2 = bot.querySelector('.confirm-card');
ok(ex2 && ex2.textContent.indexOf('还没在花型选择里确认过花型') >= 0,
   '照实说还没选过', ex2 && ex2.textContent.slice(0, 40));
ok(!!(ex2 && ex2.querySelector('[data-act="goto-designer"]')),
   '给一条明路：去找花型设计师选款');

/* =====================================================================
   [6] 「交付与下载」页的导出面板：预览用的是真实选中款
   ===================================================================== */
console.log('\n[6] 导出面板的预览按"已确认的花型"走');
Y.S.confirmed = [{ pid:'PF-OWN-2026-000128', at:'10-01 09:00' }];
Y.exportBook();
const modal = doc.querySelector('.modal');
ok(!!modal, '面板打开了');
const body = modal.textContent;
ok(body.indexOf('山茶小径') >= 0, '预览里是用户确认过的那款');
ok(body.indexOf('已确认的 1 款') >= 0 || body.indexOf('1 款') >= 0,
   '说清是按已确认的几款导出', body.slice(0, 60));
ok(body.indexOf('包含缩略图') < 0, '不再有"缩略图"开关（表格里放不了图，别骗人）');
ok(body.indexOf('趋势') < 0, '面板里也不提趋势了');
Y.closeLayer();

Y.S.confirmed = [];
Y.exportBook();
const modal2 = doc.querySelector('.modal');
ok(modal2 && modal2.textContent.indexOf('还是空的') >= 0,
   '一款没选时面板直说清单是空的，而不是摆 6 张不相干的图',
   modal2 && modal2.textContent.slice(0, 50));
Y.closeLayer();

/* =====================================================================
   [7] 离线兜底：前端也能出真文件（CSV）
   ===================================================================== */
console.log('\n[7] 离线兜底：没有服务也能真下载');
Y.S.confirmed = [{ pid:'PF-OWN-2026-000128', at:'10-01 09:00' }];
let blobUrl = null, clicks = 0;
const origCreateURL = w.URL.createObjectURL;
w.URL.createObjectURL = function(b){ blobUrl = b; return 'blob:fake-csv'; };
const origClick = w.HTMLAnchorElement.prototype.click;
w.HTMLAnchorElement.prototype.click = function(){ clicks++; };
Y.localExport();
ok(!!blobUrl && blobUrl.size > 0, '生成了 CSV 文件对象', blobUrl && blobUrl.size);
ok(clicks > 0, '触发了真实下载');
let csvText = '';
try { csvText = await blobUrl.text(); } catch (e) { csvText = ''; }
ok(csvText.indexOf('山茶小径') >= 0, 'CSV 里就是那款花型', csvText.slice(0, 70));
ok(csvText.indexOf('花型编号') >= 0, 'CSV 有表头');
/* 注意：Blob.text() 按规范会把开头的 BOM 吃掉，所以不能拿读回的字符串验 BOM，
   得直接看字节 —— 头三个字节是 EF BB BF 才算真带了。 */
let head = [];
try { head = Array.from(new Uint8Array(await blobUrl.arrayBuffer()).slice(0, 3)); } catch (e) { head = []; }
ok(head[0] === 0xEF && head[1] === 0xBB && head[2] === 0xBF, 'CSV 带 BOM（Excel 打开不乱码）',
   head.map(function(b){ return b.toString(16); }).join(' '));
w.URL.createObjectURL = origCreateURL;
w.HTMLAnchorElement.prototype.click = origClick;

console.log('\n%s', '—'.repeat(58));
console.log(`通过 ${pass} · 失败 ${fail}`);
process.exit(fail ? 1 : 0);
