/* v0.7 C 端回归：花型库**页面**已下线，但库里的真图照样能用。
   跑法：NODE_PATH=<node-workspace>/node_modules node tools/test_v07_client.mjs

   这一版要盯的是「拆掉一个页面之后别把能力一起拆没了」：
     - 左栏不再有花型库入口，硬跳过去也不能白屏（落到任务中心）
     - 用户看到的话术里不再出现「花型库」这个内部词
     - 但对话检索出来的库图**必须还能显示**（patternSVG 的 <img> 分支）
     - 选到一张库图要能直接「拿这张改图」—— 这个能力跟着候选卡片走，不跟着库页面走
     - 位图在放大层走 img 通道，且不提供平铺预览
     - 「交给织印师」必须把图真送过去，再由织印师摆出「四方连续 / 分层 PSD」两个选项
     - 库图缺的字段（版本/热度/接缝评分/色板）不许渲染成 undefined 或 NaN
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

/* ---------- 假数据：一个内置矢量款 + 两张库里的真图 ---------- */
const VEC = { id: 'PF-OWN-2026-0001', name: '内置青花', style: '新中式国风', element: '花卉',
              color: '蓝白青花', scene: '丝巾', pal: 'morandi', motif: 'floral', seed: 7 };
function libItem(i, over){
  const id = 'PF-LIB-2026-' + String(i).padStart(4, '0');
  return Object.assign({
    id, name: '库花型' + i, lib: true,
    style: '新中式国风', element: '花卉', color: '蓝白青花', scene: '丝巾',
    source: 'import', source_label: '客户来图', status: 'ready', w: 2048, h: 2048,
    image: '/api/library/file/' + id + '?thumb=1', full: '/api/library/file/' + id,
  }, over || {});
}
const L1 = libItem(1), L2 = libItem(2, { name: '蓝白小簇' });

let calls = [];
/* 用例可以往这里塞 /api/action 的返回体（key = action 名） */
let actionHooks = {};
/* /api/psd_progress 回什么由用例决定（分层跑动中的进度） */
let psdProgress = null;
/* /api/chat 回什么由用例决定（默认是检索候选那条老路） */
let chatResp = { ok: true, agent: 'designer', reply: '给你挑了几张', picks: [], mode: 'rule' };
w.fetch = function(url, opt){
  const body = opt && opt.body ? JSON.parse(opt.body) : null;
  calls.push({ url, body });
  const json = (o) => Promise.resolve({ json: () => Promise.resolve(o) });
  if (url.indexOf('/api/health') >= 0) return json({ ok: true, llm_enabled: true });
  if (url.indexOf('/api/psd_progress') >= 0)
    return json(psdProgress || { ok: true, running: false });
  if (url.indexOf('/api/action') >= 0){
    /* 用例自己摆的返回体优先（交接 / 直通车提案用）。
       给函数就直接调 —— 用例可以用它造一个"永不回来"的慢响应。 */
    if (actionHooks[body.action])
      return typeof actionHooks[body.action] === 'function'
        ? actionHooks[body.action]() : json(actionHooks[body.action]);
    /* 切张数：服务端只重跑提案、不执行不扣点，报价跟着张数走 */
    if (body.action === 'set_count')
      return json({ ok: true, pending: {
        action: 'generate_pattern', cost: 60 * Number(body.n || 1), count: Number(body.n || 1),
        args: { confirmed: false },
        summary: '按需求卡生成 ' + (body.n || 1) + ' 张不一样的面料花型' } });
    return json({ ok: true, id: body.id, name: '库花型1', action: 'pick_library' });
  }
  if (url.indexOf('/api/chat') >= 0) return json(chatResp);
  return json({ ok: false, error: 'unexpected ' + url });
};
const lastCall = (frag) => {
  for (let i = calls.length - 1; i >= 0; i--) if (calls[i].url.indexOf(frag) >= 0) return calls[i];
  return null;
};

async function main(){
  console.log('[1] C 端不再有花型库入口');
  await wait(60);
  Y.render();
  const nav = doc.getElementById('nav').textContent;
  ok(nav.indexOf('花型库') < 0, '左栏导航里没有「花型库」', nav.slice(0, 80));
  ok(nav.indexOf('任务中心') >= 0 && nav.indexOf('交付与下载') >= 0, '其余入口都还在');
  const navKeys = [];
  Y.NAV.forEach(g => (g.items || []).forEach(it => navKeys.push(it.key)));
  ok(navKeys.indexOf('library') < 0, 'NAV 数据里也没有 library', JSON.stringify(navKeys));

  console.log('\n[2] 硬跳到已下线的页面不能白屏');
  Y.go('library');
  await wait(40);
  ok(Y.S.page !== 'library', 'S.page 不再是 library', Y.S.page);
  ok(Y.S.page === 'tasks', '落回任务中心', Y.S.page);
  ok((doc.getElementById('body') || {}).textContent !== '', '页面有内容，不是空白');

  console.log('\n[3] 用户看到的话术里不出现内部仓库名');
  const html = fs.readFileSync(HTML, 'utf8');
  const visible = html.split('<script>')[0];      // 只看 HTML 与 CSS 部分
  ok(visible.indexOf('花型库') < 0, '静态部分没有「花型库」字样');
  Y.showGenResult('designer', null, { ok: true, images: [{ n: 'a.png' }, { n: 'b.png' }] }, true);
  await wait(40);
  const tb = (doc.getElementById('toastbox') || {}).textContent || '';
  ok(tb.indexOf('花型库') < 0, '出图后的提示不提「花型库」', tb);
  ok(tb.indexOf('留好') >= 0 || tb.indexOf('帮你') >= 0, '提示说的是用户听得懂的话', tb);

  console.log('\n[4] 库里的真图在对话里照样能显示');
  Y.registerLibItems([L1, L2]);
  const svg1 = Y.patternSVG(Y.PAT[L1.id]);
  ok(svg1.indexOf('<img') === 0, '库图渲染成 <img>，不是程序画的 SVG', svg1.slice(0, 40));
  ok(svg1.indexOf('/api/library/file/' + L1.id) >= 0, 'img 用的是库图地址');
  ok(svg1.indexOf('http://127.0.0.1:8787') >= 0, '地址补成了绝对地址（file:// 打开也能加载）');
  const svg2 = Y.patternSVG(Y.PATTERNS[0]);
  ok(svg2.indexOf('<svg') >= 0, '内置矢量款还是走 SVG');

  console.log('\n[5] 选到一张库图可以直接「拿这张改图」');
  Y.S.picks = [L1.id, L2.id, Y.PATTERNS[0].id];
  Y.openPicker();
  await wait(40);
  const useBadges = doc.querySelectorAll('[data-pick-use]');
  ok(useBadges.length === 2, '两张库图各有一个「改」角标，矢量款没有', useBadges.length);
  const ids = [...useBadges].map(b => b.getAttribute('data-pick-use')).sort();
  ok(ids.join(',') === [L1.id, L2.id].sort().join(','), '角标挂的正是那两张库图', ids.join(','));
  calls = [];
  doc.querySelector('[data-pick-use="' + L1.id + '"]')
     .dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await wait(60);
  const ac = lastCall('/api/action');
  ok(!!ac, '点击发出了请求');
  ok(ac && ac.body.action === 'pick_library', '请求的是 pick_library', ac && ac.body.action);
  ok(ac && ac.body.id === L1.id, '带上了这张图的 id');
  ok(ac && ac.body.agent, '带上了角色（服务端靠它隔离待确认方案）', ac && ac.body.agent);
  await wait(40);
  ok(Y.S.page === 'designer', '设成底图后回到花型师页面', Y.S.page);
  ok(doc.getElementById('layer').innerHTML === '', '弹层已关闭');
  const ta = doc.getElementById('input');
  ok(ta && ta.value.indexOf('配色换成') >= 0, '输入框预填了一句引导语', ta && ta.value);

  console.log('\n[6] 放大层：位图走 img 通道，平铺预览也给');
  Y.openZoom({ kind: 'pat', pid: L1.id });
  await wait(40);
  const zl = doc.getElementById('zoom').innerHTML;
  ok(zl.indexOf('<img') >= 0, '放大层里库图是 <img>');
  ok(zl.indexOf('/api/library/file/' + L1.id + '"') >= 0 ||
     zl.indexOf('/api/library/file/' + L1.id + "'") >= 0 ||
     zl.indexOf(L1.full) >= 0, '放大用的是原图 full 地址');
  /* 位图也要能给平铺预览：四方连续的交付就是一张单元图，铺开看接缝最直接。 */
  ok(!!doc.querySelector('[data-act="zoom-tile"]'), '位图也给「平铺预览」按钮');
  ok(!!Y.ZOOM.bgUrl, '位图有平铺源（用的是它自己）', Y.ZOOM.bgUrl);
  doc.querySelector('[data-act="zoom-tile"]')
     .dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await wait(40);
  ok(Y.ZOOM.tiled === true, '点一下切成平铺态');
  ok((doc.getElementById('zoom').innerHTML || '').indexOf('tiled') >= 0, '纸面真的铺开了');
  /* 位图必须有下载出口：四方连续这类产物按用户要求**不进花型库**，
     没有这个入口用户就只看得见、拿不走。 */
  ok(!!doc.querySelector('[data-act="zoom-dl"]'), '放大层给了「下载」按钮');
  const dlGot = [];
  const origAnchorClick = w.HTMLAnchorElement.prototype.click;
  w.HTMLAnchorElement.prototype.click = function(){
    dlGot.push({ href: this.getAttribute('href'), dl: this.download });
  };
  doc.querySelector('[data-act="zoom-dl"]')
     .dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await wait(40);
  w.HTMLAnchorElement.prototype.click = origAnchorClick;
  ok(dlGot.length === 1, '点一下只触发一次下载', JSON.stringify(dlGot));
  /* registerLibItems 是合并进 PAT（不再就地改传进来的对象），所以按 PAT 里那份比 */
  ok(dlGot[0] && dlGot[0].href === Y.PAT[L1.id].full, '下载地址就是这张图的原图地址',
     dlGot[0] && dlGot[0].href);
  ok(dlGot[0] && dlGot[0].dl === L1.id, '文件名取库里的编号（下来能认出是哪张）', dlGot[0] && dlGot[0].dl);
  Y.closeZoom();

  console.log('\n[7] 对话主流程没被拆坏');
  Y.go('designer');
  await wait(40);
  ok(doc.getElementById('input') !== null, '花型师页面有输入框');
  Y.S.brief = { style: '新中式国风', element: '花卉', color: '蓝白青花', scene: '丝巾' };
  const res = Y.filtered();
  ok(Array.isArray(res) && res.length >= 0, '本地检索还能跑', res && res.length);
  ok(typeof Y.confirmCardHTML === 'function', '确认卡片还在');

  console.log('\n[8] 张数做成卡片上的按钮（用户说一张，就必须只出一张）');
  ok(Array.isArray(Y.CNT_OPTS) && Y.CNT_OPTS.join(',') === '1,2,4',
     '可选张数是 1/2/4', String(Y.CNT_OPTS));
  const gcard = Y.confirmCardHTML({ action: 'generate_pattern', cost: 240, count: 4,
    summary: '按需求卡（童趣插画、几何）生成 4 张不一样的面料花型' });
  ok(gcard.indexOf('cnt-row') >= 0, '生成方案的卡片上有张数按钮行');
  ok(gcard.indexOf('240 点') >= 0, '报价按总价说（4 张 = 240 点）');
  ok(Y.confirmCardHTML({ action: 'edit_pattern', cost: 60, summary: 'x' }).indexOf('cnt-row') < 0,
     '改图卡片不显示张数（改的就是手上这一张）');
  const tcardHtml = Y.confirmCardHTML({ action: 'trace_pattern', cost: 60, fidelity: 'clean',
                         summary: 'x' });
  ok(tcardHtml.indexOf('cnt-row') < 0, '描稿卡片不显示张数');
  ok(tcardHtml.indexOf('fid-row') < 0, '描稿卡片不再摆还原度三档（自动档）');
  /* 后台把默认张数配成别的值时也要能显示出来，别把当前值漏掉 */
  ok(Y.cntRowHTML({ count: 3, cost: 180 }).indexOf('data-n="3"') >= 0,
     '当前张数不在预设档位里时，也把它显示出来');

  console.log('\n[9] 点一张 → 只重提方案，不扣点、不出图');
  chatResp = { ok: true, agent: 'designer', mode: 'llm', skills: ['generate_pattern'],
    reply: '先看库里有没有对得上的——没对上的话，我给你出 4 张（一共 240 点）。',
    brief: { style: '新中式国风', element: '花卉', color: '蓝白青花', scene: '丝巾' },
    picks: [], images: null,
    pending: { action: 'generate_pattern', cost: 240, count: 4, args: { confirmed: false },
               summary: '按需求卡生成 4 张不一样的面料花型' } };
  Y.go('designer');
  await wait(40);
  Y.say('我想要新中式的花卉丝巾');
  await wait(80); await wait(80);
  const gc = doc.querySelectorAll('.confirm-card');
  const lastCard = gc[gc.length - 1];
  ok(!!lastCard && !!lastCard.querySelector('.cnt-row'), '卡片上真的渲染出张数按钮行');
  const cntBtns = lastCard.querySelectorAll('.cnt-row .cnt');
  ok(cntBtns.length === 3, '渲染出 3 个张数按钮', String(cntBtns.length));
  const cntOn = lastCard.querySelector('.cnt-row .cnt.on');
  ok(cntOn && cntOn.getAttribute('data-n') === '4', '默认高亮在 4 张',
     cntOn && cntOn.getAttribute('data-n'));

  const credits0 = Y.S.credits;
  const cards0 = doc.querySelectorAll('.confirm-card').length;
  const imgs0 = doc.querySelectorAll('.genimg').length;
  calls = [];
  lastCard.querySelector('.cnt-row .cnt[data-n="1"]')
          .dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await wait(100);
  const sc = lastCall('/api/action');
  ok(sc && sc.body.action === 'set_count', '点击走 set_count（不是 confirm）',
     sc && JSON.stringify(sc.body));
  ok(sc && Number(sc.body.n) === 1, '带上目标张数 n=1', sc && sc.body.n);
  ok(sc && sc.body.agent === 'designer', '带上了角色（服务端靠它找对那张卡片）',
     sc && sc.body.agent);
  ok(Y.S.credits === credits0, '切张数不扣点', String(Y.S.credits));
  ok(doc.querySelectorAll('.genimg').length === imgs0, '切张数不出图',
     String(doc.querySelectorAll('.genimg').length));
  const gc2 = doc.querySelectorAll('.confirm-card');
  ok(gc2.length === cards0, '是原地换卡片，不是又冒出一张', String(gc2.length));
  const lastCard2 = gc2[gc2.length - 1];
  const cntOn2 = lastCard2.querySelector('.cnt-row .cnt.on');
  ok(cntOn2 && cntOn2.getAttribute('data-n') === '1', '高亮切到 1 张',
     cntOn2 && cntOn2.getAttribute('data-n'));
  ok(lastCard2.textContent.indexOf('60 点') >= 0, '报价跟着张数走（1 张 = 60 点）',
     lastCard2.textContent.replace(/\s+/g, ' ').slice(0, 90));

  console.log('\n[10] 四方连续的卡片与下载出口');
  const frag = (html) => { const d = doc.createElement('div'); d.innerHTML = html; return d; };
  const seProbe = { summary: '可以做：图案内容够，接缝能对齐。',
                    route_hint: 'model', warnings: [] };
  const seCard = Y.confirmCardHTML({ action: 'seamless_pattern', cost: 30, count: 1,
    engine: 'auto', probe: seProbe, summary: '可以做：图案内容够，接缝能对齐。' });
  ok(seCard.indexOf('cnt-row') < 0,
     '四方连续卡片不显示张数（一张确定性产物，不是出几张挑一张）');
  ok(seCard.indexOf('四方连续方案') >= 0 && seCard.indexOf('开始做连续') >= 0,
     '卡片走「四方连续方案 / 开始做连续」这套说法');
  ok(seCard.indexOf('图片判断') >= 0, '把图片判断结论摆出来（只一句可行性）');
  ok(seCard.indexOf('图案特征') < 0 && seCard.indexOf('sat=') < 0,
     '卡片不摆特征数字（用户不需要看这些）');
  /* 2026-10-20 起：四方连续改自动选路线，卡片上不再摆「纯算法 / 模型补缝」按钮。
     路线由服务端按输入体检决定，结果里 route / fallback 会如实回。
     这里钉住「不再出现路线切换控件」，防止旧 UI 被顺手加回来。 */
  ok(frag(seCard).querySelectorAll('[data-act="set-eng"]').length === 0,
     '卡片上不再摆路线切换按钮（自动选路线）');
  ok(seCard.indexOf('路线') < 0,
     '卡片文案里没有「选路线」这类控件说明');

  /* 织印师那边真的挂出这张卡片，并且点路线只重提方案（不执行、不扣点） */
  chatResp = { ok: true, agent: 'engineer', mode: 'llm', skills: ['seamless_pattern'],
    reply: '这张我看过了：满地密排的大花，画面上几乎没有留白。',
    pending: { action: 'seamless_pattern', cost: 30, count: 1, engine: 'model',
               args: { confirmed: false, engine: 'auto' }, probe: seProbe,
               summary: '四方连续 · 508×500 · 满地（有纹理 96%）' } };
  Y.go('engineer');
  await wait(40);
  Y.say('把这张花型做成四方连续');
  await wait(80); await wait(80);
  const seCards = doc.querySelectorAll('.confirm-card');
  const seLast = seCards[seCards.length - 1];
  ok(!!seLast && seLast.textContent.indexOf('四方连续方案') >= 0,
     '织印师那边挂出了四方连续卡片',
     seLast && seLast.textContent.replace(/\s+/g, ' ').slice(0, 60));
  /* 卡片上没有路线切换控件了，执行入口只剩「开始做连续（30 点）」——
     扣费闸门仍在这里：用户点确认才执行。 */
  const seGo = seLast.querySelector('[data-act="confirm-gen"]');
  ok(!!seGo && seGo.textContent.indexOf('开始做连续') >= 0,
     '四方连续卡片保留「开始做连续」确认入口', seGo && seGo.textContent);
  ok(seLast.querySelectorAll('[data-act="set-eng"]').length === 0,
     '卡片上没有路线切换按钮（自动选路线）');

  /* 产物不进库 + 能下载：这两件事是一对 —— 没有库，就必须有下载 */
  ok(typeof Y.zoomDownload === 'function' && typeof Y.zoomFilename === 'function',
     '页面导出了下载能力');
  ok(Y.zoomFilename('data:image/jpeg;base64,AAAA', '花型原图') === '花型原图.jpg',
     '上传图（data URL）也能取到 .jpg 名字');
  ok(Y.zoomFilename('/api/file/20261010-194044-seamless-tile-2110.png', 'x')
     === '20261010-194044-seamless-tile-2110.png',
     '四方连续产物取后台落盘名（带时间戳，天然不重名）');
  Y.openZoom({ kind: 'img', cap: '四方连续单元',
               src: '/api/file/20261010-194044-seamless-tile-2110.png' });
  await wait(40);
  ok(!!doc.querySelector('[data-act="zoom-dl"]'), '四方连续产物点开大图后有下载按钮');
  const seDl = [];
  w.HTMLAnchorElement.prototype.click = function(){
    seDl.push({ href: this.getAttribute('href'), dl: this.download });
  };
  doc.querySelector('[data-act="zoom-dl"]')
     .dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await wait(40);
  w.HTMLAnchorElement.prototype.click = origAnchorClick;
  ok(seDl.length === 1 && !!seDl[0].dl.match(/seamless-tile/),
     '点下载拿到的就是那张四方连续单元图', JSON.stringify(seDl));
  Y.closeZoom();

  console.log('\n[10b] 描稿结果卡：不给「接着改」，直接两个下一步');
  /* 用户要求：描稿出来之后不要提示改配色 / 改疏密度，
     直接摆「四方连续 / 生成 PSD」两个按钮 —— 底图已经在织印师这一槽里，
     按钮走提案直通车（不绕模型，仍要先确认才扣点）。 */
  Y.S.page = 'engineer'; Y.S.agent = 'engineer';
  Y.S.chat.engineer.length = 0;
  Y.showGenResult('engineer', null,
    { ok: true, cost: 60, model: 'm', action: 'trace_pattern',
      images: [{ name: 'trace-x.jpg', url: '/api/file/trace-x.jpg',
                 bytes: 1024, mime: 'image/jpeg' }] },
    true, '描稿完成。');
  await wait(60);
  ok(!!doc.querySelector('[data-act="eng-seamless"]'), '有「生成四方连续」按钮');
  ok(!!doc.querySelector('[data-act="eng-psd"]'), '有「生成 PSD」按钮');
  ok(doc.querySelector('[data-act="eng-psd"]').textContent.indexOf('直接生成 PSD') >= 0,
     'PSD 选项写明「直接生成」（不先做连续）');
  ok(!doc.querySelector('[data-act="ed:dim:color"]'), '不再有「改配色」等接着改按钮');
  const traceMsgs = doc.querySelector('#msgs');
  ok(traceMsgs.textContent.indexOf('接着改') < 0, '文案不再提示「接着改」');
  ok(traceMsgs.textContent.indexOf('生成分层 PSD') >= 0,
     '文字里直接给出下一步（四方连续 / 分层 PSD）');

  console.log('\n[10c] 生成/改图产物：默认单张打开，平铺预览按钮按原样保留');
  /* 原项目里平铺预览的渲染本来就是"位图铺自己"，效果用户认可 —— 不要动。
     这里只钉住入口：生成图点开先单张，点「平铺预览」才铺开；改图同样。 */
  Y.S.page = 'designer'; Y.S.agent = 'designer';
  Y.S.chat.designer.length = 0;
  Y.showGenResult('designer', null,
    { ok: true, cost: 240, action: 'generate_pattern', provider: 'pattern-studio',
      seamless: true,
      images: [{ name: 'g.png', url: '/api/file/g.png', bytes: 1024, mime: 'image/png' }] },
    true, '图出来了。');
  await wait(60);
  const gimg = doc.querySelector('.genimg');
  ok(!!gimg, '生成图在会话里');
  gimg.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await wait(60);
  ok(Y.ZOOM.tiled === false, '点开默认单张（保持原项目手感）');
  const ztile = doc.querySelector('[data-act="zoom-tile"]');
  ok(!!ztile && ztile.textContent.indexOf('平铺') >= 0,
     '放大层提供「平铺预览」按钮', ztile && ztile.textContent);
  ok((Y.ZOOM.bgUrl || '').indexOf('g.png') >= 0, '平铺源就是这张图本身', Y.ZOOM.bgUrl);
  ztile.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await wait(40);
  ok(Y.ZOOM.tiled === true, '点按钮才铺开（渲染沿用原实现）');
  Y.closeZoom();

  /* 改图产物：同样单张打开，平铺按钮照样可用 */
  Y.S.chat.designer.length = 0;
  Y.showGenResult('designer', null,
    { ok: true, action: 'edit_pattern', changes: { color: '蓝白青花' },
      images: [{ name: 'e.png', url: '/api/file/e.png', bytes: 1, mime: 'image/png' }] },
    true, '改好了。');
  await wait(60);
  doc.querySelector('.genimg').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await wait(60);
  ok(Y.ZOOM.tiled === false, '改图产物也是单张打开');
  Y.closeZoom();

  console.log('\n[11] 后端回传的「内置款」不许把本地画图案的参数抹掉');
  /* 服务端检索是「内置款 + 库里的真图」合并返回的，而内置款只带 id/名字/四维标签，
     没有 motif / pal / seed —— 早先这里是直接 PAT[id] = it 覆盖，弹窗渲染缩略图时
     MOTIF_FN[undefined] 抛异常，整个「打开花型选择」就再也点不开了。
     真踩过：后端一上线内置款就跟着回传，花型库必崩。 */
  const BUILTIN = Y.PATTERNS[0];
  const stripped = { id: BUILTIN.id, name: BUILTIN.name, lib: false, hit: 3, match: 88,
                     style: BUILTIN.style, element: BUILTIN.element,
                     color: BUILTIN.color, scene: BUILTIN.scene,
                     desc: BUILTIN.desc, fabric: BUILTIN.fabric };
  Y.registerLibItems([stripped, L1]);
  ok(Y.PAT[BUILTIN.id].motif === BUILTIN.motif,
     '内置款还是刚才那个画法（motif 没被抹掉）', Y.PAT[BUILTIN.id].motif);
  ok(Y.PAT[BUILTIN.id].seed === BUILTIN.seed, '随机种子也留着（图案不会突然变一张）');
  ok(Y.PAT[BUILTIN.id].hit === 3, '服务端给的匹配度照样写进去（合并，不是丢掉）');
  ok(Y.PAT[L1.id].image.indexOf('/api/library/file/') >= 0, '库图的地址照旧补成绝对地址');

  let threw = null;
  try { Y.patternSVG(Y.PAT[BUILTIN.id]); } catch (e) { threw = e.message; }
  ok(threw === null, '单张渲染不再抛异常', threw);

  /* 真刀真枪：把这种混合候选塞进去开弹窗 */
  Y.S.picks = [L1.id, BUILTIN.id, L2.id];
  let openErr = null;
  try { Y.openPicker(); } catch (e) { openErr = e.message; }
  await wait(40);
  ok(openErr === null, '「打开花型选择」能打开了', openErr);
  ok(doc.querySelectorAll('.psel').length === 3, '三款都渲染出来了',
     doc.querySelectorAll('.psel').length);
  ok((doc.getElementById('layer').innerHTML || '').indexOf('<img') >= 0, '库图走 <img>');
  ok((doc.getElementById('layer').innerHTML || '').indexOf('<svg') >= 0,
     '内置款走 SVG（就是刚被抹掉 motif 的那一款）');
  Y.closeLayer();

  console.log('\n[12] 四方连续的卡片上有「生成 PSD 文件」');
  Y.go('engineer');
  await wait(40);
  const SEIMG = '/api/file/20261010-194044-seamless-tile-2110.png';
  Y.showGenResult('engineer', null, { ok: true, action: 'seamless_pattern', cost: 30,
    images: [{ url: SEIMG, mime: 'image/png' }],
    gen: { name: 'seamless_pattern', args: {} } }, true);
  await wait(40);
  const bar = doc.querySelector('.edit-bar');
  ok(!!bar && bar.innerHTML.indexOf('data-act="to-psd"') >= 0,
     '四方连续卡片上有「生成 PSD 文件」按钮', bar && bar.innerHTML.slice(0, 120));
  ok(!!doc.querySelector('[data-act="regen"]'), '「重新生成」还在（没被顶掉）');
  ok(bar.innerHTML.indexOf('生成 PSD 文件') >= 0, '按钮文案是人话');
  /* 做完四方连续交付的是单元图 —— 文案要主动提示还能接着做 PSD（不是自动做）。 */
  ok(doc.querySelector('#msgs').textContent.indexOf('要分层的话') >= 0,
     '四方连续交付文案里提示可以继续做 PSD');
  /* 四方连续的成品现在也留档（2026-10-12 起入库过算法闸门）——
     可以顺口说"已经帮你留好了"，但依旧不提「花型库」这个内部名。 */
  const seToast = (doc.getElementById('toastbox') || {}).textContent || '';
  ok(seToast.indexOf('花型库') < 0, '四方连续的提示也不提「花型库」', seToast);
  ok(seToast.indexOf('留好') >= 0, '四方连续的成品也提示"已经帮你留好了"', seToast);

  calls = [];
  const creditsBefore = Y.S.credits;
  actionHooks = {
    hand_off: { ok: true, to: 'engineer' },
    propose: { ok: true, pending: { action: 'layer_psd', cost: 45, count: 1,
      args: { confirmed: false },
      summary: '分层 PSD · 3 层' } },
  };
  const psdBtn = doc.querySelector('[data-act="to-psd"]');
  ok(psdBtn.getAttribute('data-url') === SEIMG, '按钮带的是这张产物图的地址',
     psdBtn.getAttribute('data-url'));
  psdBtn.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await wait(80);
  const ho = lastCall('/api/action');
  ok(lastCall('/api/action') !== null, '点下去有请求');
  const acts = calls.filter(c => c.url.indexOf('/api/action') >= 0).map(c => c.body);
  const hand = acts.filter(b => b.action === 'hand_off')[0];
  const prop = acts.filter(b => b.action === 'propose')[0];
  ok(!!hand, '第一步是交接（跨机器人传图只能显式交）');
  ok(hand && hand.url === SEIMG && hand.to === 'engineer', '交的是这张图，交给织印师', hand && hand.url);
  ok(!!prop, '第二步才提分层方案');
  ok(prop && prop.skill === 'layer_psd', '提的是 layer_psd', prop && prop.skill);
  ok(prop && prop.agent === 'engineer', '以织印师的身份提（方案要落进他那一槽）',
     prop && prop.agent);
  ok(!!acts.find(b => b.action === 'hand_off') && acts.indexOf(hand) < acts.indexOf(prop),
     '顺序是先交接后提案');

  /* 取最后一张卡片：织印师这条线上前面还挂着四方连续的方案卡 */
  const cc = [...doc.querySelectorAll('.confirm-card')].pop();
  ok(!!cc, '弹出了确认卡片（服务端只提案，执行要点确认）');
  ok(cc && cc.textContent.indexOf('分层方案') >= 0, '卡片标题是「分层方案」',
     cc && cc.textContent.slice(0, 60));
  ok(cc && cc.textContent.indexOf('确认分层') >= 0, '按钮是「确认分层」');
  ok(cc && cc.textContent.indexOf('45 点') >= 0, '报价 45 点摆出来了');
  ok(cc && cc.textContent.indexOf('3 层') >= 0, '只说分几层');
  ok(cc && /Color\d/.test(cc.textContent) === false &&
     cc.textContent.indexOf('橙粉色大花') < 0, '不列具体图层名');
  ok(!doc.querySelector('.cnt-row'), '分层不显示「出几张」（它是确定性的一张）');
  ok(Y.S.credits === creditsBefore, '提案阶段一分钱没扣', Y.S.credits);

  console.log('\n[13] 分层跑动中：秒表 + 「第几层」，别让用户分不清"在跑"和"死了"');
  /* [12] 走完之后，织印师那一槽的待确认方案就是 layer_psd（psdFromImage 设好的） */
  const g13 = Y.genOf('engineer');
  ok(g13.action === 'layer_psd', '确认执行的是分层（不是别的出图动作）', g13.action);

  psdProgress = { ok: true, running: true, phase: 'layer', index: 2, total: 3,
                  name: '蓝色小花', elapsed: 42.5 };
  /* 确认这条请求故意"不回来"，好让跑动中的气泡留在页面上被检查
     （真跑起来要几分钟；这里只要那段时间的界面状态） */
  actionHooks = Object.assign({}, actionHooks, {
    confirm: function(){ return new Promise(function(){}); },
  });
  /* 点了确认就"在跑"了 —— 这时气泡里必须已经挂上秒表 */
  const cc13 = [...doc.querySelectorAll('.confirm-card')].pop();
  const goBtn = cc13 && cc13.querySelector('[data-act="confirm-gen"]');
  ok(!!goBtn, '分层方案卡片上有「确认分层」按钮');
  goBtn.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await wait(40);

  const typing = doc.querySelector('.msg .typing');
  ok(!!typing, '点了确认进入了"跑动中"气泡');
  const metaNode = doc.querySelector('.runmeta');
  ok(!!metaNode, '气泡里挂了秒表（不用等整页刷新就能看见在动）');
  ok(!!metaNode && metaNode.textContent.indexOf('已等 0:00') >= 0,
     '秒表从 0 开始，不是空白', metaNode && metaNode.textContent);

  /* 服务端的层数进度回来之后，小字要变成「第 N/M 层 · 已等 X:XX」 */
  await Y.pollPsdProgress();
  await wait(20);
  ok(Y.PSD_PROG && Y.PSD_PROG.index === 2, '进度从服务端读到了"正在跑第 2 层"',
     JSON.stringify(Y.PSD_PROG));
  const meta2 = doc.querySelector('.runmeta').textContent;
  ok(meta2.indexOf('第 2/3 层') >= 0, '小字里带上了层数进度', meta2);
  ok(meta2.indexOf('已等') >= 0, '也带上了已经等了多久', meta2);

  /* 拖久了要改口径：正常一两分钟，超过 5 分钟就是云端在排队，得如实说 */
  Y.RUN_SEC = 312; Y.paintRunMeta();
  const meta3 = doc.querySelector('.runmeta').textContent;
  ok(meta3.indexOf('5:12') >= 0, '秒表按分:秒显示', meta3);
  ok(meta3.indexOf('排队') >= 0, '拖过 5 分钟改口径说"云端在排队"', meta3);
  ok(meta3.indexOf('15 分钟') >= 0, '并且把"超过 15 分钟会自动停并退点"交代清楚', meta3);

  ok(Y.fmtDur(9) === '0:09' && Y.fmtDur(125) === '2:05', '秒表格式补零正确',
     Y.fmtDur(9) + ' / ' + Y.fmtDur(125));

  /* 跑完（成功或失败）必须收掉秒表，别在历史消息里留个一直涨的计时器。
     真实链路里 endRun() 后面紧跟 render()（showGenResult 收尾），这里照做。 */
  Y.endRun();
  Y.render();
  await wait(20);
  ok(Y.RUN_SEC === 0, '收尾时秒表停掉并归零', Y.RUN_SEC);
  ok(!doc.querySelector('.runmeta'), '跑动气泡撤掉，历史里不留计时器');

  console.log('\n[14] 「交给织印师」= 把这张图送过去，再由织印师摆出两个选项');
  /* 老毛病：点「交给织印师」只切页面、丢一句"你说一句要做什么"，或者在本页
     摆一张任务卡。图其实没交过去（会话记忆按角色隔离），织印师那边什么也做不了。
     现在必须是：图先落进织印师那一槽 → 人到织印师页面 → 两个选项摆出来。 */
  Y.setAgent('designer');
  Y.registerLibItems([L1]);
  Y.push('designer', { pattern: L1.id, seq: 1, total: 1 });
  Y.render();
  await wait(40);
  const hb = doc.querySelector('[data-act="handoff"][data-pid="' + L1.id + '"]');
  ok(!!hb, '库图卡片上的「交给织印师」按钮在', !!hb);
  calls = [];
  const c14 = Y.S.credits;
  actionHooks = {
    /* 库图不按地址找：服务端直接读库（前端拿到的 /api/library/file/… 是下载地址，
       不是磁盘上的生成物，按地址找必然找不到） */
    pick_library: { ok: true, id: L1.id, name: L1.name, url: L1.full,
                    agent: 'engineer', origin: 'library' },
    propose: { ok: true, pending: { action: 'seamless_pattern', cost: 30, count: 1,
      engine: 'model', probe: { summary: '图幅 2048×2048 · 地比例 38%，留白够补缝' },
      args: { confirmed: false },
      summary: '四方连续 · 图幅 2048×2048，地比例 38%' } },
  };
  hb.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await wait(140);
  ok(Y.S.page === 'engineer', '人已经被带到织印师页面', Y.S.page);
  const acts14 = calls.filter(c => c.url.indexOf('/api/action') >= 0).map(c => c.body);
  const pick = acts14.filter(b => b.action === 'pick_library')[0];
  ok(!!pick, '第一步是把这张库图送进织印师那一槽');
  ok(pick && pick.id === L1.id && pick.agent === 'engineer',
     '库图走读库这条路，且落给织印师', pick && (pick.id + ' / ' + pick.agent));
  ok(!acts14.filter(b => b.action === 'hand_off').length,
     '库图不再按地址找一遍（按地址一定找不到）');
  const ask = doc.querySelector('.ask-card');
  ok(!!ask, '织印师这边弹出了「要做成哪一种」的卡片');
  ok(!!doc.querySelector('.ask-card .ask-thumb img'), '卡片上带着刚交过来的那张图');
  const askActs = ask ? [...ask.querySelectorAll('[data-act]')].map(b => b.dataset.act) : [];
  ok(askActs.indexOf('eng-seamless') >= 0 && askActs.indexOf('eng-psd') >= 0,
     '两个选项都在：生成四方连续 / 生成 PSD 文件', JSON.stringify(askActs));
  ok(!!ask && ask.textContent.indexOf('生成四方连续') >= 0 &&
     ask.textContent.indexOf('生成 PSD 文件') >= 0, '按钮文案是人话',
     ask && ask.textContent.slice(0, 50));
  ok(Y.S.credits === c14, '到这一步一分钱没扣', Y.S.credits);

  console.log('\n[15] 点了哪个选项，才提哪个方案（都走直通车，不扣点）');
  calls = [];
  doc.querySelector('.ask-card [data-act="eng-seamless"]')
    .dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await wait(140);
  const prop15 = calls.filter(c => c.url.indexOf('/api/action') >= 0)
    .map(c => c.body).filter(b => b.action === 'propose')[0];
  ok(!!prop15, '点「生成四方连续」发出的是提案');
  ok(prop15 && prop15.skill === 'seamless_pattern', '提的是 seamless_pattern', prop15 && prop15.skill);
  ok(prop15 && prop15.agent === 'engineer', '以织印师的身份提（方案落进他那一槽）', prop15 && prop15.agent);
  const cc15 = [...doc.querySelectorAll('.confirm-card')].pop();
  ok(!!cc15, '弹出了四方连续的确认卡片（执行还是要用户点确认）');
  ok(!!cc15 && cc15.textContent.indexOf('四方连续方案') >= 0, '卡片标题是「四方连续方案」',
     cc15 && cc15.textContent.slice(0, 50));
  ok(!!cc15 && cc15.textContent.indexOf('30 点') >= 0, '报价 30 点摆出来了');
  ok(!!cc15 && cc15.textContent.indexOf('开始做连续') >= 0, '按钮是「开始做连续」');
  ok(Y.S.credits === c14, '提案阶段一分钱没扣', Y.S.credits);

  /* 另一条路也要通：同一张卡上点「生成 PSD 文件」→ 提案换成 layer_psd */
  calls = [];
  actionHooks = Object.assign({}, actionHooks, {
    propose: { ok: true, pending: { action: 'layer_psd', cost: 45, count: 1,
      args: { confirmed: false }, summary: '分层 PSD · 3 层（粉橘大花、蓝色小花、黄色小穗）' } },
  });
  doc.querySelector('.ask-card [data-act="eng-psd"]')
    .dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await wait(140);
  const prop16 = calls.filter(c => c.url.indexOf('/api/action') >= 0)
    .map(c => c.body).filter(b => b.action === 'propose')[0];
  ok(prop16 && prop16.skill === 'layer_psd', '点「生成 PSD 文件」提的是 layer_psd', prop16 && prop16.skill);
  const cc16 = [...doc.querySelectorAll('.confirm-card')].pop();
  ok(!!cc16 && cc16.textContent.indexOf('分层方案') >= 0 && cc16.textContent.indexOf('45 点') >= 0,
     '换成了分层方案的卡片', cc16 && cc16.textContent.slice(0, 40));
  ok(Y.S.credits === c14, '两条路都不在提案阶段扣点', Y.S.credits);

  console.log('\n[16] 库图没有的字段不许显示成 undefined');
  /* 服务端库里的真图只有「图 + 四维标签 + 说明」：没有版本号、热度、质量、
     接缝评分、色板。以前详情页会甩出「PF-LIB-… undefined · 接缝评分 undefined」，
     而且 PALETTES[p.pal] 为空时 pal.slice 直接抛异常 —— 详情页整个打不开。 */
  let drawerErr = null;
  try { Y.openDrawer(L1.id); } catch (e) { drawerErr = e.message; }
  await wait(30);
  ok(drawerErr === null, '库图的「查看详情」能打开（不再因为没色板就抛异常）', drawerErr);
  const drawer = (doc.getElementById('layer').innerHTML || '');
  ok(drawer.indexOf('undefined') < 0, '详情页里没有 undefined', drawer.indexOf('undefined'));
  ok(drawer.indexOf('NaN') < 0, '也没有 NaN（热度缺失不该算成 NaN 匹配度）');
  ok(drawer.indexOf('接缝评分') < 0, '没有接缝评分就不摆这一行');
  ok(drawer.indexOf(L1.name) >= 0, '该有的还是有的：花型名在', L1.name);
  Y.closeLayer();

  /* 原型那张任务卡（本地款还在用）也不许拼出 undefined */
  let cardErr = null;
  try { Y.offerTask('repeat', L1.id); } catch (e) { cardErr = e.message; }
  await wait(30);
  ok(cardErr === null, '任务卡渲染不抛异常', cardErr);
  const lastMsg = (doc.getElementById('msgs').textContent || '');
  ok(lastMsg.indexOf('undefined') < 0, '任务卡里也没有 undefined',
     lastMsg.slice(-80));

  console.log('\n[17] 埋点：来路 / 点击 / 离开，且不影响任何行为');
  /* 后台看板那六块里有两块（前端埋点、会话结束方式）只能由页面报上来。
     要点：① 连上后端才报「来访」（打不开的页面不算访问）
           ② 一次访问只报一次
           ③ 点按钮记下是哪个动作
           ④ 离开时带总停留时长
           ⑤ **不带聊天内容** —— 统计用不上，少传一句就少一分泄漏 */
  const tracks = () => calls.filter(c => c.url.indexOf('/api/track') >= 0).map(c => c.body);

  calls = [];
  Y.trackVisit();
  await wait(30);
  let tv = tracks();
  ok(tv.length === 1 && tv[0].event === 'visit', '连上后端后报一次「来访」', JSON.stringify(tv));
  ok(tv[0] && typeof tv[0].dwell === 'number', '带上停留秒数');
  ok(tv[0] && tv[0].ref !== undefined && tv[0].w > 0, '带来路与屏幕尺寸', JSON.stringify(tv[0]));
  ok(tv[0] && tv[0].session_id === Y.SESSION_ID, '带上会话号');
  ok(!tv[0] || tv[0].message === undefined, '不带聊天内容（就不整段上传）');

  calls = [];
  Y.trackVisit();
  await wait(20);
  ok(tracks().length === 0, '同一次访问只报一次，不重复上报');

  Y.S.pending = null;                       /* 空输入：点发送不会真发消息 */
  calls = [];
  doc.querySelector('[data-act="send"]').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await wait(30);
  let tc = tracks();
  ok(tc.length === 1 && tc[0].event === 'click', '点按钮记一笔', JSON.stringify(tc));
  ok(tc[0] && tc[0].act === 'send', '记下点的是哪个动作：' + (tc[0] && tc[0].act));

  calls = [];
  w.dispatchEvent(new w.Event('pagehide'));
  await wait(30);
  let tl = tracks();
  ok(tl.length === 1 && tl[0].event === 'leave', '离开页面报一笔', JSON.stringify(tl));
  ok(tl[0] && typeof tl[0].dwell === 'number', '离开时带总停留时长');

  console.log('\n结果：' + pass + ' 通过 / ' + fail + ' 失败');
  process.exit(fail ? 1 : 0);
}

main().catch(e => { console.error(e); process.exit(1); });
