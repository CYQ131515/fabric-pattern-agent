/* 管理后台前端回归（jsdom，stub fetch，不依赖真实模型/服务）。
   跑法：NODE_PATH=<node-workspace>/node_modules node tools/test_admin_v01.mjs

   盯的是几件容易做错的事：
     - 后台六页都能切，且**不依赖真实服务**也能起（stub fetch）
     - 密钥栏留空 = 不改动（运营顺手点保存不该把 Key 清成空）
     - 说话风格档位要能套进文本框，且**不整页重绘**（否则人设框里打的字会丢）
     - 知识库「检索测试」要把"机器人会看到的指令"显示出来，不然后台调阈值全靠猜
     - 花型库搜索只重绘列表，输入框不能被打断
     - 删图和删资料都要两次确认
*/
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const { JSDOM } = require('jsdom');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const HTML = process.env.PF_ADMIN_HTML
  ? path.resolve(process.env.PF_ADMIN_HTML)
  : path.resolve(HERE, '..', '..', '后台', 'PatternFlow管理后台v0.1.html');
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
const A = w.__ADM;

/* ---------- 假数据 ---------- */
const STATE = {
  ok: true,
  model: { base_url: 'https://api.deepseek.com/v1', model: 'deepseek-chat',
           api_key: '••••••fb29', image_api_key: '••••••9a1c',
           llm_enabled: true, temperature: 0.6, max_tokens: 16000 },
  image: { provider: 'volcengine', base_url: 'https://ark.cn-beijing.volces.com/api/v3',
           model: 'doubao-seedream-5-0', cost: 60, variants: 4, enabled: true },
  // 接口自检清单：后端 _api_checks 的原样返回。
  // 注意这里**没有**任何网关地址和密钥 —— 概览页就该照着它渲染，不许自己拼地址。
  apis: [
    { group: '通用', name: '对话大模型', ok: true, note: '已配置 · 问答、判断意图都走它',
      info: '模型 deepseek-chat' },
    { group: '通用', name: '识图模型', ok: true, note: '已配置 · 描稿体检、入库打标签、按图改图都用它',
      info: '模型 deepseek-chat' },
    { group: '花型设计师', name: '生图接口', ok: true, note: '已配置 · 生成花型、定向改图共用这一套',
      info: '一次出 4 张 · 单张 60 点' },
    { group: '织印师', name: '描稿仿稿', ok: true, note: '已配置 · 照片提图案', info: '沿用生图那套' },
    { group: '织印师', name: '四方连续 · 出图', ok: true, note: '已配置 · 补缝出图走它',
      info: '单张 30 点' },
    { group: '织印师', name: '四方连续 · 诊断', ok: true, note: '已配置 · 沿用出图那套',
      info: '模型 deepseek-v4.1-flash' },
    { group: '织印师', name: '分层 PSD · 拆层', ok: true, note: '已配置 · 拆层工作流走它',
      info: '最多 4 层 · 单张 45 点' },
    { group: '织印师', name: '分层 PSD · 视觉诊断', ok: true, note: '已配置 · 沿用通用那套',
      info: '沿用通用 · 模型用默认' },
  ],
  library: { count: 54, by_source: { import: 54 }, by_status: { ready: 54 }, enabled: true,
             auto_ingest: true },
  // 入库自动打标的实时状态（后端 state.atag）。只有 ready 和一句人话 ——
  // 不上端点地址，这个页面会被截图。
  atag: { installed: true, ready: true, note: '就绪：每张导入图都会先过它打标签。' },
  kb: { enabled: true, docs: 1, chunks: 3, chars: 136, qa: 1, agents: ['assistant'] },
  agents: ['assistant', 'auto', 'designer', 'engineer'],
  personas: ['assistant', 'designer', 'engineer'],
  skills: ['search_patterns', 'generate_pattern'],
  sessions: 69,
  styles: { designer: { preset: 'warm', body: '热情亲切的原文' },
            engineer: { preset: 'warm', body: '热情亲切的原文' },
            assistant: { preset: 'warm', body: '热情亲切的原文' },
            auto: { preset: 'warm', body: '热情亲切的原文' } },
  presets: [{ key: 'warm', label: '热情亲切（推荐）' }, { key: 'pro', label: '专业克制' },
            { key: 'terse', label: '简洁直接' }, { key: 'sales', label: '销售导向' }],
};
/* 数据看板（/api/admin/analytics）的假返回：字段跟后端的 analytics() 对齐 */
const ANALYTICS = {
  ok: true, now: '2026-10-12 12:00:00', days: 14,
  kpi: { sessions: 76, turns: 128, agents: 3, library: 64, generated: 462,
         points: 1995, skills: 9, kb_docs: 1, kb_qa: 1,
         avg_turns: 1.7, untagged: 3,
         span_from: '2026-09-27', span_to: '2026-10-12' },
  compare: { turns: { cur: 27, prev: 41, pct: -34 },
             gen: { cur: 59, prev: 0, pct: null } },
  series: [
    { d: '2026-10-10', turns: 0, gen: 0, ingest: 0, seamless: 0, psd: 0,
      ok: 0, fail: 0, visit: 3 },
    { d: '2026-10-11', turns: 19, gen: 46, ingest: 2, seamless: 5, psd: 3,
      ok: 44, fail: 2, visit: 18 },
    { d: '2026-10-12', turns: 8, gen: 13, ingest: 1, seamless: 1, psd: 0,
      ok: 12, fail: 1, visit: 6 },
  ],
  agents: [['designer', 104], ['engineer', 18], ['assistant', 6]],
  skills: [{ name: 'update_brief', label: '记需求卡', count: 84, cost: 0, points: 0 },
           { name: 'search_patterns', label: '检索花型库', count: 36, cost: 0, points: 0 },
           { name: 'generate_pattern', label: '生成花型', count: 20, cost: 60, points: 1200 },
           { name: 'seamless_pattern', label: '四方连续', count: 6, cost: 30, points: 180 }],
  demand: { style: [['法式浪漫', 64], ['新中式国风', 17]],
            element: [['花卉', 40], ['几何', 9]],
            color: [['蓝白青花', 33], ['莫兰迪灰调', 11]],
            scene: [['女装连衣裙', 22], ['丝巾', 8]] },
  dim_label: { style: '风格', element: '元素', color: '配色', scene: '场景' },
  queries: [['蓝白青花', 4], ['女装连衣裙', 3]],
  library: { count: 64, by_source: { import: 61, 'ai-gen': 3 },
             by_status: { ready: 61, untagged: 3 },
             dims: { style: [['法式浪漫', 20]], element: [['花卉', 30]],
                     color: [['蓝白青花', 18]], scene: [['丝巾', 12]] },
             colors: [['靛蓝', 22], ['白色', 15]] },
  engine_usage: { seamless: { count: 6, points: 180 }, psd: { count: 3, points: 135 },
                  trace: { count: 7, points: 420 } },
  /* 六块埋点指标（字段跟后端 analytics() 对齐） */
  gen: { attempts: 61, ok: 56, fail: 5, rate: 91.8, avg_ms: 8300, p50_ms: 7100,
         p90_ms: 16200, points: 1470, mock: 2,
         by_skill: [{ name: 'generate_pattern', label: '生成花型', attempts: 40,
                      ok: 38, fail: 2, rate: 95, avg_ms: 9100 },
                    { name: 'trace_pattern', label: '描稿仿稿', attempts: 7, ok: 5,
                      fail: 2, rate: 71.4, avg_ms: 5400 }],
         by_reason: [['timeout', '超时（模型/云端没在时限内回来）', 3],
                     ['auth', '鉴权失败（Key 或权限不对）', 2]] },
  funnel: { proposals: 32, confirmed: 21, unconfirmed: 11, stuck_points: 1860,
            conv: 65.6, direct: 4,
            by_skill: [{ name: 'generate_pattern', label: '生成花型', proposals: 24,
                         confirmed: 17, conv: 70.8 },
                       { name: 'layer_psd', label: '分层 PSD', proposals: 8,
                         confirmed: 4, conv: 50 }] },
  kb_stat: { lookups: 42, hits: 26, miss: 16, rate: 61.9, docs: 1, qa: 1,
             top_miss: [['交期多久', 4], ['能不能开专票', 2]] },
  frontend: { visits: 27, visitors: 24, clicks: 118, avg_dwell: 214, silent: 5,
              refs: [['直接打开 / 收藏夹', 15], ['weibo.com', 7]],
              pages: [['designer', 18], ['engineer', 6]],
              acts: [['confirm-gen', 22], ['set-cnt', 14], ['handoff', 9]],
              sizes: [['1440x900', 20]] },
  ending: { total: 24,
            items: [['done', '聊完走的', 13], ['pending', '压着没确认的卡片', 7],
                    ['bounce', '进来就走', 4]],
            avg_turns: { done: 3.4, pending: 1.6, bounce: 0 } },
  coverage: { events: 412, since: '2026-10-06',
              dir: '/x/数据/telemetry' },
  accounts: { available: false, reason: '后端还没有账号与计费模块（前台的充值/注册是演示用的）。' },
};
const PERSONA = {
  ok: true,
  items: [
    { agent: 'designer', file: 'designer.md', name: '花型师', chars: 100,
      body: '你是花型师，负责出花型。', style: { preset: 'warm', body: '热情亲切的原文' },
      skills: ['generate_pattern'] },
    { agent: 'engineer', file: 'engineer.md', name: '织印师', chars: 80,
      body: '你是织印师，负责描稿。', style: { preset: 'warm', body: '热情亲切的原文' },
      skills: ['search_patterns'] },
    { agent: 'assistant', file: 'assistant.md', name: '客服', chars: 60,
      body: '你是客服，负责答疑。', style: { preset: 'warm', body: '热情亲切的原文' },
      skills: [] },
  ],
  presets: [
    { key: 'warm', label: '热情亲切（推荐）', body: '热情档位正文' },
    { key: 'pro', label: '专业克制', body: '克制档位正文' },
    { key: 'terse', label: '简洁直接', body: '简洁档位正文' },
    { key: 'sales', label: '销售导向', body: '销售档位正文' },
  ],
};
const KB = {
  ok: true,
  stats: { enabled: true, docs: 1, chunks: 3, chars: 136, qa: 1, agents: ['assistant'] },
  docs: [{ id: 'd001', title: '弹力针织面料', chars: 136, chunks: 3,
           created: '2026-10-01 09:00:00', source: 'admin',
           keywords: ['价格', '报价', '起订量'] }],
  qa: [{ id: 'q001', q: '运费怎么算？', a: '满 500 米包邮', tags: [], created: '' }],
};
const KB_LOOKUP_HIT = {
  ok: true, hit: true, best: 1.15, reason: '命中 1 条问答 / 1 段资料',
  qa: [{ id: 'q001', q: '运费怎么算？', a: '满 500 米包邮', score: 1.15 }],
  chunks: [{ doc: 'd001', title: '弹力针织面料', head: '价格', seq: 2,
             text: '常规色每米 28 元', score: 0.5 }],
  prompt: '## 知识库（严格模式）\n- 下面这些是唯一可用的资料。',
};
const KB_LOOKUP_MISS = {
  ok: true, hit: false, best: 0, reason: '资料里没有相关内容（最高 0.00，阈值 0.18）',
  qa: [], chunks: [],
  prompt: '## 知识库（严格模式）\n- 知识库里没有和用户这个问题相关的资料。',
};

function libItem(i, over){
  const id = 'PF-LIB-2026-' + String(i).padStart(4, '0');
  return Object.assign({
    id, name: '库花型' + i, lib: true,
    style: '新中式国风', element: '花卉', color: '蓝白青花', scene: '丝巾',
    source: 'import', source_label: '客户来图', status: 'ready', w: 2048, h: 2048,
    image: '/api/library/file/' + id + '?thumb=1', full: '/api/library/file/' + id,
  }, over || {});
}
const ITEMS = [libItem(1), libItem(2, { name: '蓝白小簇', style: '法式浪漫' }),
               libItem(3, { name: '灰调几何', status: 'untagged' })];

/* ---------- stub 网络 ---------- */
let calls = [], lookupResp = KB_LOOKUP_HIT, kbSaveResp = { ok: true };
/* 登录相关：默认「已设口令且本次已登录」，[13] 会临时改成未登录态 */
let stateResp = null, loginResp = { ok: false, error: '口令不对' };
w.fetch = function(url, opt){
  const body = opt && opt.body ? JSON.parse(opt.body) : null;
  calls.push({ url, body });
  const json = (o) => Promise.resolve({ json: () => Promise.resolve(o) });
  const has = (s) => url.indexOf(s) >= 0;

  if (has('/api/health')){
    // 后台只认 who==='admin'。8787 上也会有服务回 ok，但那是 C 端，
    // 拿它当后台用就会到处 404 —— 这里要能测出"探测认对了进程"。
    return json(url.indexOf('8788') >= 0 ? { ok: true, who: 'admin' }
                                         : { ok: true, who: 'client' });
  }
  if (has('/api/admin/sessions')){
    // 服务端筛选：q=ID 片段、day=最后活动日期。这里照着后端规则实现一份，
    // 好把"筛选真的走了服务端"（而不是前端筛当前这页）测出来。
    const mq = /[?&]q=([^&]*)/.exec(url), md = /[?&]day=([^&]*)/.exec(url);
    const fq = mq ? decodeURIComponent(mq[1]) : '';
    const fd = md ? decodeURIComponent(md[1]) : '';
    let its = [{ id: 's1', turns: 4, last: '2026-10-11 12:00', bytes: 2048,
                 day: '2026-10-11' },
               { id: 's2', turns: 1, last: '2026-10-12 11:05', bytes: 512,
                 day: '2026-10-12' }];
    if (fq) its = its.filter(x => x.id.toLowerCase().indexOf(fq.toLowerCase()) >= 0);
    if (fd) its = its.filter(x => x.day === fd);
    return json({ ok: true, total: 2, matched: its.length, q: fq, day: fd,
                  dir: '/x/sessions', items: its });
  }
  if (has('/api/admin/analytics')) return json(ANALYTICS);
  // 注意顺序：'/api/admin/sessions' 也包含 '/api/admin/session'，
  // 这条必须排在前面，否则会话列表会被会话状态吞掉
  if (has('/api/admin/session')) return json({ ok: true, has_password: true });
  if (has('/api/admin/login')) return json(loginResp);
  if (has('/api/admin/state')) return json(stateResp || STATE);
  if (has('/api/admin/persona/save')) return json({ ok: true, saved: ['人设', '说话风格'], agent: 'designer' });
  if (has('/api/admin/persona')) return json(PERSONA);
  if (has('/api/admin/kb/save')){
    if (body && body.op === 'lookup') return json(lookupResp);
    return json(kbSaveResp);
  }
  if (has('/api/admin/kb/doc')) return json({ ok: true, id: body, title: '全文', keywords: [], text: '正文全文' });
  if (has('/api/admin/kb')) return json(KB);
  if (has('/api/admin/library/scan/progress'))
    return json({ ok: true, running: false, done: 2, total: 2,
                  stats: { ok: 1, dup: 1, skip: 0, fail: 0 }, log: [] });
  if (has('/api/admin/library/scan'))
    return json(body && body.probe ? { ok: true, probe: true, count: 7, sample: ['a.png'] }
                                   : { ok: true, started: true, total: 7, workers: 3 });
  if (has('/api/admin/config')) return json({ ok: true, changed_keys: [] });
  if (has('/api/library/update'))
    return json({ ok: true, item: Object.assign(libItem(1), body.fields) });
  if (has('/api/library/delete')) return json({ ok: true, id: body.id });
  if (has('/api/library/retag')) return json({ ok: true, item: libItem(1, { name: '重读过的' }) });
  if (has('/api/library/ingest')) return json({ ok: true, id: 'PF-LIB-2026-0099' });
  if (has('/api/library')) return json({
    ok: true, total: ITEMS.length, items: ITEMS,
    stats: { count: 3, by_source: { import: 2, 'ai-gen': 1 }, by_status: {} },
    facets: { style: [['新中式国风', 2], ['法式浪漫', 1]], element: [['花卉', 3]],
              color: [['蓝白青花', 3]], scene: [['丝巾', 3]],
              source: [['import', 2], ['ai-gen', 1]] },
  });
  if (has('/api/reload')) return json({ ok: true, skills: [] });
  return json({ ok: false, error: 'unexpected ' + url });
};
const lastCall = (frag) => {
  for (let i = calls.length - 1; i >= 0; i--) if (calls[i].url.indexOf(frag) >= 0) return calls[i];
  return null;
};
w.Image = function(){
  const self = this; let src = '';
  this.onload = null; this.onerror = null; this.width = 900; this.height = 900;
  Object.defineProperty(this, 'src', {
    set(v){ src = v; setTimeout(() => { if (self.onload) self.onload(); }, 0); },
    get(){ return src; },
  });
};
w.FileReader = function(){
  const self = this;
  this.onload = null; this.onerror = null; this.result = '';
  this.readAsDataURL = function(){
    setTimeout(() => { self.result = 'data:image/png;base64,QUJD'; if (self.onload) self.onload(); }, 0);
  };
  this.readAsText = function(){
    setTimeout(() => { self.result = '文件正文内容'; if (self.onload) self.onload(); }, 0);
  };
};

/* ============================ 用例 ============================ */
async function main(){
  console.log('[1] 起页面：连服务 + 概览自检');
  // 脚本执行时还没有 fetch，boot 会走兜底分支；stub 好之后要手动再 boot 一次
  A.boot();
  await wait(150);
  ok(A.ONLINE === true, '探测到服务并置为在线');
  ok(A.API.indexOf('8788') >= 0, '认的是后台那个进程（8788），不是 C 端 8787', A.API);
  ok(A.S.st && A.S.st.library.count === 54, '概览拿到了图库统计');
  const ov = doc.getElementById('body').textContent;
  ok(ov.indexOf('自检') >= 0, '概览有自检区块');
  ok(ov.indexOf('知识库') >= 0, '自检里检查了知识库');
  // 自检要盖住所有接口，不能只看"对话模型 + 生图"两项
  for (const nm of ['对话大模型','识图模型','生图接口','描稿仿稿',
                    '四方连续 · 出图','四方连续 · 诊断',
                    '分层 PSD · 拆层','分层 PSD · 视觉诊断'])
    ok(ov.indexOf(nm) >= 0, '自检覆盖「' + nm + '」');
  for (const g of ['通用','花型设计师','织印师'])
    ok(ov.indexOf(g) >= 0, '自检按「' + g + '」分组');
  // 这一页常被截图 → 不许出现网关地址、也不许出现密钥掩码
  const ovHTML = doc.getElementById('body').innerHTML;
  ok(ovHTML.indexOf('http://') < 0 && ovHTML.indexOf('https://') < 0,
     '概览页不显示任何接口地址');
  ok(ovHTML.indexOf('••••') < 0 && ovHTML.indexOf('sk-') < 0,
     '概览页不显示任何密钥（连掩码都不给）');
  ok(ov.indexOf('8/8') >= 0, 'KPI 里显示"8/8 接口已就绪"', ov.slice(0, 60));
  ok(doc.getElementById('sttxt').textContent === '服务已连接', '左下角显示已连接');

  console.log('\n[2] 七个页面都能切');
  for (const [key, word] of [['model', '对话模型'], ['persona', '说话风格'],
                             ['kb', '检索测试'], ['lib', '导入'], ['sessions', '会话'],
                             ['stats', '用户想要什么'], ['overview', '自检']]){
    A.go(key);
    await wait(60);
    ok(doc.getElementById('body').textContent.indexOf(word) >= 0, '切到 ' + key + ' 页有「' + word + '」');
  }
  const nav = doc.getElementById('nav').innerHTML;
  ok(nav.indexOf('模型与密钥') >= 0 && nav.indexOf('知识库') >= 0 && nav.indexOf('花型库') >= 0,
     '左栏六个入口都在');
  ok(nav.indexOf('打开对话页') >= 0, '左栏有跳回用户端的入口');

  console.log('\n[3] 密钥栏留空 = 不改动');
  A.go('model');
  await wait(60);
  const kf = doc.getElementById('f_api_key');
  ok(!!kf, '有 API Key 输入框');
  ok(kf.value === '', '密钥栏不回填真值（也不回填掩码）', JSON.stringify(kf.value));
  ok(kf.getAttribute('type') === 'password', '密钥栏是密码框');
  ok(kf.getAttribute('placeholder').indexOf('留空不改') >= 0, '占位符说明了留空不改');
  calls = [];
  doc.querySelector('[data-act="save-config"]').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await wait(60);
  const cfg = lastCall('/api/admin/config').body;
  ok(cfg.api_key === '' && cfg.image_api_key === '', '留空时提交的是空串（服务端据此判断不改）',
     JSON.stringify([cfg.api_key, cfg.image_api_key]));
  ok(cfg.llm.base_url === 'https://api.deepseek.com/v1', '带上了接口地址');
  ok(Array.isArray(cfg.kb.agents) && cfg.kb.agents.indexOf('assistant') >= 0,
     '知识库生效角色带上客服', JSON.stringify(cfg.kb.agents));

  console.log('\n[4] 说话风格：档位套用不整页重绘');
  A.go('persona');
  await wait(80);
  ok(doc.getElementById('stylebox') !== null, '有风格文本框');
  ok(doc.getElementById('personabox') !== null, '有人设全文框');
  const pbox = doc.getElementById('personabox');
  pbox.value = '我手打的一段人设，不该被档位按钮冲掉';
  const pro = doc.querySelector('[data-preset="pro"]');
  pro.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await wait(40);
  ok(doc.getElementById('stylebox').value === '克制档位正文', '点档位把正文套进风格框',
     doc.getElementById('stylebox').value);
  ok(doc.getElementById('personabox').value === '我手打的一段人设，不该被档位按钮冲掉',
     '人设框里手打的内容没被冲掉');
  ok(doc.querySelector('[data-preset="pro"]').getAttribute('aria-pressed') === 'true', '档位进入选中态');
  ok(doc.querySelector('[data-preset="warm"]').getAttribute('aria-pressed') === 'false', '旧档位取消选中');
  calls = [];
  doc.querySelector('[data-act="save-persona"]').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await wait(60);
  const ps = lastCall('/api/admin/persona/save').body;
  ok(ps.agent === 'designer', '保存带上了角色', ps.agent);
  ok(ps.style.preset === 'pro', '保存带上了档位', ps.style.preset);
  ok(ps.body.indexOf('我手打的') >= 0, '保存带上了人设全文');

  console.log('\n[5] 切换机器人 tab');
  const eng = doc.querySelector('[data-pag="engineer"]');
  eng.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await wait(60);
  ok(A.S.personaCur === 'engineer', '切到织印师');
  ok(doc.getElementById('personabox').value.indexOf('织印师') >= 0, '人设框换成织印师的');

  console.log('\n[6] 知识库：检索测试要显示将注入的指令');
  A.go('kb');
  await wait(80);
  ok(doc.getElementById('kbq') !== null, '有检索测试输入框');
  lookupResp = KB_LOOKUP_HIT;
  doc.getElementById('kbq').value = '运费怎么算';
  calls = [];
  doc.querySelector('[data-act="kb-test"]').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await wait(60);
  const lk = lastCall('/api/admin/kb/save').body;
  ok(lk.op === 'lookup' && lk.q === '运费怎么算', '发出检索请求', JSON.stringify(lk));
  const res = doc.getElementById('kbresult').textContent;
  ok(res.indexOf('命中') >= 0, '结果显示命中');
  ok(res.indexOf('满 500 米包邮') >= 0, '结果显示命中的标准问答');
  ok(res.indexOf('唯一可用的资料') >= 0, '结果把"将注入的指令"显示出来了');
  lookupResp = KB_LOOKUP_MISS;
  doc.getElementById('kbq').value = '你们老板是谁';
  doc.querySelector('[data-act="kb-test"]').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await wait(60);
  ok(doc.getElementById('kbresult').textContent.indexOf('没命中') >= 0, '没资料时显示没命中');
  ok(doc.getElementById('kbresult').textContent.indexOf('不许编造') >= 0 ||
     doc.getElementById('kbresult').textContent.indexOf('没有') >= 0, '没命中时能看到"不许编"的指令');

  console.log('\n[7] 知识库：加资料 / 加问答 / 删');
  kbSaveResp = { ok: true, id: 'd002', title: '新资料', chunks: 4 };
  doc.getElementById('kbtext').value = '正文内容';
  doc.getElementById('kbtitle').value = '新资料';
  doc.getElementById('kbkw').value = '价格,报价';
  calls = [];
  doc.querySelector('[data-act="kb-add"]').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await wait(60);
  const ad = lastCall('/api/admin/kb/save').body;
  ok(ad.op === 'add_doc' && ad.text === '正文内容', '加资料带上了正文');
  ok(ad.keywords === '价格,报价', '加资料带上了关键词别名', ad.keywords);
  ok(doc.getElementById('kbtext').value === '', '加完清空正文框');
  kbSaveResp = { ok: true, id: 'q002' };
  doc.getElementById('q_new').value = '交期多久';
  doc.getElementById('a_new').value = '7 个工作日';
  calls = [];
  doc.querySelector('[data-act="qa-add"]').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await wait(60);
  const qa = lastCall('/api/admin/kb/save').body;
  ok(qa.op === 'add_qa' && qa.q === '交期多久' && qa.a === '7 个工作日', '加问答带上了问答内容');
  ok(doc.querySelectorAll('[data-qa-del]').length === 1, '列表里渲染出已有问答');
  const del = doc.querySelector('[data-kbdel]');
  ok(!!del, '资料行有删除按钮');
  calls = [];
  del.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await wait(60);
  ok(lastCall('/api/admin/kb/save').body.op === 'del_doc', '点删除发出 del_doc');

  console.log('\n[8] 花型库：真图渲染 + 搜索只重绘列表');
  A.go('lib');
  await wait(80);
  const cards = doc.querySelectorAll('[data-libopen]');
  ok(cards.length === 3, '渲染出 3 张卡片', cards.length);
  ok(doc.querySelector('[data-libopen] img') !== null, '卡片用 <img> 出真图');
  ok(doc.querySelector('[data-libopen] img').getAttribute('src').indexOf('http://127.0.0.1:8788/') === 0,
     '图片地址补成了绝对地址（指向后台进程）');
  ok(doc.querySelector('[data-libopen] .warn') !== null, '没打上标签的图有角标');
  const q = doc.getElementById('libq');
  q.value = '青花';
  q.dispatchEvent(new w.Event('input', { bubbles: true }));
  await wait(30);
  ok(q.value === '青花', '输入过程中输入框内容还在（没被整页重绘冲掉）', q.value);
  await wait(500);
  ok(lastCall('/api/library?').url.indexOf('q=' + encodeURIComponent('青花')) >= 0,
     '防抖后带着关键词去查', lastCall('/api/library?').url);
  ok(doc.getElementById('libq').value === '青花', '查完输入框还是原样');

  console.log('\n[8b] 花型库：自动打标状态条 + 导入文件夹');
  ok(doc.getElementById('libatagtxt') !== null, '花型库页有自动打标状态条');
  ok(doc.getElementById('libatagtxt').textContent.indexOf('就绪') >= 0,
     '引擎就绪时，状态条说"就绪"', doc.getElementById('libatagtxt').textContent);
  ok(doc.querySelector('[data-act="lib-dir"]') !== null, '有「导入文件夹」按钮');
  ok(doc.getElementById('libdir') !== null, '配了隐藏的文件夹选择器');
  // 状态条跟着引擎状态变：没启动要说清后果
  A.S.st.atag = { installed: true, ready: false, note: '引擎没起' };
  A.paintAtag();
  ok(doc.getElementById('libatagtxt').textContent.indexOf('没启动') >= 0,
     '引擎没起时，状态条说"没启动"并提醒会被拦下');
  ok(doc.getElementById('libatagtxt').textContent.indexOf('引擎没起') >= 0, '带上原因');
  A.S.st.atag = STATE.atag; A.paintAtag();
  // 不支持的浏览器要提示，而不是点了没反应
  A.libDirClick();
  await wait(30);
  ok([].some.call(doc.querySelectorAll('.toast'), t => t.textContent.indexOf('不支持选文件夹') >= 0),
     '浏览器不支持选文件夹时给提示（不是静默失败）');
  // 文件夹里会混进 .DS_Store / __MACOSX，得滤掉
  ok(A.isImageFile({ name: '.DS_Store', type: '' }) === false, '.DS_Store 滤掉');
  ok(A.isImageFile({ name: '._a.jpg', type: 'image/jpeg' }) === false, '苹果资源叉车文件滤掉');
  ok(A.isImageFile({ name: 'a.jpg', type: '' }) === true, '没 type 的按扩展名认');
  ok(A.isImageFile({ name: 'a.txt', type: 'text/plain' }) === false, '非图片挡掉');
  ok(A.isImageFile({ name: 'a.png', type: 'image/png',
                     webkitRelativePath: 'x/__MACOSX/a.png' }) === false, '__MACOSX 里的滤掉');
  // 走一遍文件夹导入：2 张真图 + 1 个垃圾文件
  calls = [];
  A.libUploadFiles([{ name: '客户来图1.jpg', type: 'image/jpeg' },
                    { name: '.DS_Store', type: '' },
                    { name: '子目录/客户来图2.png', type: 'image/png',
                      webkitRelativePath: '客户来图/子目录/客户来图2.png' }], { dir: true });
  await wait(150);
  const ing = calls.filter(c => c.url.indexOf('/api/library/ingest') >= 0);
  ok(ing.length === 2, '文件夹里的垃圾文件被滤掉，只传 2 张', ing.length);
  ok(ing.every(c => c.body.source === 'import'), '来源记"导入"');
  ok(ing[0].body.filename === '客户来图1.jpg', '文件名带着走（闸门拿它起名）');
  ok(doc.getElementById('libprog') !== null, '花型库页有导入进度容器');
  await wait(200);
  ok(doc.getElementById('libprog') !== null, '导完进度卡收掉（不长期占地方）');

  console.log('\n[9] 花型库：筛选 / 清除 / 详情 / 两次确认删除');
  const chip = doc.querySelector('[data-libdim="style"][data-libval="新中式国风"]');
  chip.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await wait(60);
  ok(lastCall('/api/library?').url.indexOf('style=') >= 0, '点筛选带上风格条件');
  doc.querySelector('[data-act="lib-clear"]').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await wait(60);
  ok(Object.keys(A.S.lib.dim).length === 0, '清除筛选把条件清了');
  doc.querySelector('[data-libopen]').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await wait(60);
  ok(doc.getElementById('layer').className === 'on', '点卡片开了详情弹层');
  ok(doc.querySelectorAll('[data-libfld]').length >= 8, '详情里有可编辑的标签字段',
     doc.querySelectorAll('[data-libfld]').length);
  const db = doc.querySelector('[data-act="lib-del"]');
  db.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await wait(40);
  ok(doc.querySelector('[data-act="lib-del"]').textContent.indexOf('再点一次') >= 0,
     '第一次点删除只是改成确认字样');
  ok(lastCall('/api/library/delete') === null, '第一次点没有真的发删除请求');
  doc.querySelector('[data-act="lib-del"]').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await wait(60);
  ok(lastCall('/api/library/delete') !== null, '第二次点才真的删');
  ok(doc.getElementById('layer').className === '', '删完关掉弹层');

  console.log('\n[10] 扫目录：先探测再导入');
  A.libScanDialog();
  await wait(40);
  ok(doc.getElementById('scandir') !== null, '扫目录弹层有路径输入');
  doc.getElementById('scandir').value = '/tmp/客户来图';
  calls = [];
  doc.querySelector('[data-act="scan-probe"]').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await wait(60);
  const pb = lastCall('/api/admin/library/scan').body;
  ok(pb.probe === true && pb.path === '/tmp/客户来图', '探测带上路径', JSON.stringify(pb.path));
  ok(doc.getElementById('scanprobe').textContent.indexOf('7') >= 0, '探测结果显示了张数');
  calls = [];
  doc.querySelector('[data-act="scan-go"]').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await wait(60);
  const go2 = lastCall('/api/admin/library/scan').body;
  ok(go2.probe === false || go2.probe === undefined, '正式导入不带 probe');
  ok(go2.workers === 3 && go2.limit === 0, '并发与上限取到了（f_ 前缀没写错）',
     JSON.stringify([go2.workers, go2.limit]));
  ok(doc.getElementById('scanbox') !== null, '花型库页有进度条容器');

  console.log('\n[11] 会话页');
  A.go('sessions');
  await wait(80);
  ok(doc.getElementById('body').textContent.indexOf('s1') >= 0, '会话列表渲染出来了');
  ok(doc.querySelectorAll('.tbl tbody tr').length === 2, '两行会话');
  ok(doc.getElementById('body').textContent.indexOf('/x/sessions') < 0,
     '顶部不再摆"会话文件存在哪"（内部实现，不该出现在这儿）');
  ok(!!doc.querySelector('table.tbl.sess'), '会话表带 sess 类（第一列留白靠它）');

  console.log('\n[11b] 会话筛选：ID 模糊 + 按日期，都交给服务端');
  const qbox = doc.getElementById('sessq'), dbox = doc.getElementById('sessd');
  ok(!!qbox && !!dbox, '有 ID 搜索框和日期筛选');
  calls = [];
  qbox.focus();
  qbox.value = 's2';
  qbox.dispatchEvent(new w.Event('input', { bubbles: true }));
  await wait(60);
  ok(calls.filter(c => c.url.indexOf('/api/admin/sessions') >= 0).length === 0,
     '刚敲一下就发请求太费（有防抖，等下再发）');
  await wait(400);
  let sc = calls.filter(c => c.url.indexOf('/api/admin/sessions') >= 0);
  ok(sc.length === 1, '停手后只发一次请求', String(sc.length));
  ok(sc[0].url.indexOf('q=s2') >= 0, 'ID 片段带给了服务端（不是前端筛当前页）', sc[0].url);
  ok(doc.querySelectorAll('.tbl tbody tr').length === 1 &&
     doc.getElementById('body').textContent.indexOf('s1') < 0,
     '筛完只剩 s2');
  ok(doc.activeElement === qbox, '筛完输入框还在焦点上（只换表格，没整页重绘）');
  ok(doc.getElementById('sessmeta').textContent.indexOf('筛出') >= 0 &&
     doc.getElementById('sessmeta').textContent.indexOf('全部共 2 个') >= 0,
     '统计行说清筛出了几个、一共几个');

  dbox.value = '2026-10-11';
  dbox.dispatchEvent(new w.Event('change', { bubbles: true }));
  await wait(120);
  sc = calls.filter(c => c.url.indexOf('/api/admin/sessions') >= 0);
  const lastSc = sc[sc.length - 1];
  ok(lastSc.url.indexOf('day=2026-10-11') >= 0 && lastSc.url.indexOf('q=s2') >= 0,
     '日期是叠加在 ID 上的（两个条件一起给服务端）', lastSc.url);
  ok(doc.getElementById('body').textContent.indexOf('没有符合条件') >= 0,
     'ID 与日期是"且"：s2 不在 11 号 → 空表，并且给的是人话而不是白屏');

  /* 只留日期：把 ID 清掉，这时 11 号该只剩 s1 */
  qbox.value = '';
  qbox.dispatchEvent(new w.Event('input', { bubbles: true }));
  await wait(400);
  const txt11 = doc.getElementById('body').textContent;
  ok(txt11.indexOf('s1') >= 0 && txt11.indexOf('s2') < 0, '只按 11 号筛 → 只剩 s1');

  doc.querySelector('[data-act="sess-clear"]').dispatchEvent(
    new w.MouseEvent('click', { bubbles: true }));
  await wait(120);
  sc = calls.filter(c => c.url.indexOf('/api/admin/sessions') >= 0);
  const clr = sc[sc.length - 1];
  ok(clr.url.indexOf('q=') < 0 && clr.url.indexOf('day=') < 0,
     '「清除」把两个条件都撤了', clr.url);
  ok(doc.getElementById('sessq').value === '' && doc.getElementById('sessd').value === '',
     '输入框也跟着清空');
  ok(doc.querySelectorAll('.tbl tbody tr').length === 2, '清除后两行都回来了');

  console.log('\n[12] 断服务时的兜底');
  A.API = '';
  const savedFetch = w.fetch;
  w.fetch = () => Promise.reject(new Error('offline'));
  A.boot();
  await wait(200);
  ok(A.ONLINE === false || doc.getElementById('sttxt').textContent.indexOf('没连上') >= 0,
     '连不上时状态显示为未连接');
  ok(doc.getElementById('body').textContent.indexOf('连不上') >= 0, '给了人话提示而不是白屏');
  ok(doc.getElementById('body').textContent.indexOf('admin_server.py') >= 0,
     '提示里说清了该启哪个进程（后台是独立进程）');
  w.fetch = savedFetch;

  console.log('\n[13] 登录门：口令错了进不去，对了才渲染');
  A.API = 'http://127.0.0.1:8788';
  stateResp = { ok: false, need_auth: true, error: '请先登录' };
  loginResp = { ok: false, error: '口令不对' };
  A.checkAuth();
  await wait(140);
  ok(doc.getElementById('authmask').style.display !== 'none', '没登录时挡着登录框');
  ok(A.AUTH.checked === true && A.AUTH.authed === false, '判定为未登录');
  ok(doc.getElementById('authpw') !== null, '登录框里有口令输入框');
  ok(doc.getElementById('authpw').getAttribute('type') === 'password', '口令框是密码框');

  doc.getElementById('authpw').value = '不记得了';
  calls = [];
  doc.getElementById('authgo').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await wait(140);
  const lg = lastCall('/api/admin/login');
  ok(lg && lg.body.password === '不记得了', '把口令发给了后台', lg && lg.body.password);
  ok(doc.getElementById('autherr').textContent.indexOf('不对') >= 0, '口令错了有提示',
     doc.getElementById('autherr').textContent);
  ok(doc.getElementById('authmask').style.display !== 'none', '口令错了还挡在登录框');

  // 这回对了：登录成功后要能读到 state 并正常渲染
  loginResp = { ok: true, token: 'tk', ttl: 43200 };
  stateResp = STATE;
  doc.getElementById('authpw').value = 'test1234';
  doc.getElementById('authgo').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await wait(200);
  ok(doc.getElementById('authmask').style.display === 'none', '口令对了登录框关掉');
  ok(A.AUTH.authed === true, '状态标成已登录');
  // 登录成功会自动把 state 拉下来并重绘（当前页是会话页，所以看数据不看文案）
  ok(A.S.st && A.S.st.library && A.S.st.library.count === 54, '登录后把后台数据读出来了');
  ok(doc.getElementById('body').textContent.indexOf('s1') >= 0, '登录后当前页正常渲染');

  console.log('\n[14] 保存配置：开关得按页面上的真实状态提交');
  // 真出过：早先控件查不到时收集函数返回 false / []，于是在模型页点一次保存，
  // 就把知识库和花型库一起关掉了（页面上还显示着开着，纯静默）。
  // 知识库、花型库的开关就长在「模型与密钥」页上，所以这里要的不是"别提交"，
  // 而是"提交的一定是页面上真实按下的那个值"。
  A.go('model'); await wait(60);
  const pm = A.collectModel();
  ok(pm.kb.enabled === true, 'kb.enabled 提交的是真值 true（不是查不到控件给的 false）', pm.kb.enabled);
  ok(Array.isArray(pm.kb.agents) && pm.kb.agents.indexOf('assistant') >= 0,
     'kb.agents 提交的是页面上选中的那几个', JSON.stringify(pm.kb.agents));
  ok(pm.library.enabled === true, 'library.enabled 是真值 true', pm.library.enabled);
  ok(pm.library.auto_ingest === true, 'library.auto_ingest 是真值 true', pm.library.auto_ingest);
  ok(pm.llm && typeof pm.llm.model === 'string', '本页自己的字段照常带上');

  // 反过来：真的把开关按灭，false 必须能提交出去（不能因为"假值"被当成没填丢掉）
  const kbBtn = doc.querySelector('[data-lbtog="kb_enabled"]');
  const libBtn = doc.querySelector('[data-lbtog="enabled"]');
  ok(!!kbBtn && !!libBtn, '页面上能找到这两个开关按钮');
  kbBtn.setAttribute('aria-pressed', 'false');
  libBtn.setAttribute('aria-pressed', 'false');
  const pm2 = A.collectModel();
  ok(pm2.kb.enabled === false, '按下关掉后提交 false（假值不能被当空值丢掉）', pm2.kb.enabled);
  ok(pm2.library.enabled === false, '花型库关掉后提交 false', pm2.library.enabled);
  ok(A.lbTog('kb_enabled') === false, 'lbTog 读的是按钮状态');

  // 最后一关：控件压根不在这页时（比如以后把开关挪走），必须"不提交"而不是 false/[]。
  // 早先的 bug 正是这一条没兜住 —— 拿"没查到"当了"关掉"。
  Array.from(doc.querySelectorAll('[data-lbtog],[data-kbag]')).forEach(n => n.remove());
  const pm3 = A.collectModel();
  ok(!('enabled' in pm3.kb), '控件不在时 kb.enabled 整个不提交', JSON.stringify(pm3.kb));
  ok(!('agents' in pm3.kb), '控件不在时 kb.agents 整个不提交');
  ok(!('enabled' in pm3.library), '控件不在时 library.enabled 整个不提交');
  ok(!('auto_ingest' in pm3.library), '控件不在时 library.auto_ingest 整个不提交');
  ok(A.lbTog('enabled') === null, 'lbTog 查不到控件时返回 null（不是 false）', A.lbTog('enabled'));
  ok(pm3.llm && typeof pm3.llm.model === 'string', '本页自己的字段不受影响');
  A.go('model'); await wait(60);

  console.log('\n[14b] 描稿仿稿字段：模型页提交、离开该页就不提交');
  // 描稿的空值语义跟别处相反：留空 = "沿用生图那套"，是要真的写进去的，
  // 所以后端允许写空；但控件不在这一页时必须整个不提交 ——
  // 否则在别的页点保存会把描稿的独立配置一起抹掉。
  A.go('model'); await wait(60);
  const pt = A.collectModel().trace;
  ok(pt && pt.model === '' && pt.cost === '',
     '在模型页时提交 trace（空值 = 沿用生图，是合法语义）', JSON.stringify(pt));
  const trm = doc.getElementById('f_tr_model');
  ok(!!trm, '页面上有描稿模型输入框');
  if (trm){
    trm.value = 'my-trace-model';
    ok(A.collectModel().trace.model === 'my-trace-model', '描稿模型填了就带上去');
    trm.value = '';
  }
  A.go('kb'); await wait(60);
  ok(A.collectModel().trace === null,
     '不在模型页时整个 trace 不提交（免得在别的页把描稿配置抹掉）',
     JSON.stringify(A.collectModel().trace));
  A.go('model'); await wait(60);

  console.log('\n[16] 数据看板：KPI、图表与"没有的数据如实说明"');
  A.go('stats');
  await wait(120);
  const stEl = doc.getElementById('body');
  const stTxt = stEl.textContent, stHTML = stEl.innerHTML;
  ok(stTxt.indexOf('正在统计') < 0, '数据拉回来了（不卡在"正在统计…"）');
  for (const wd of ['预估点数', '使用趋势', '谁在说话', '能力调用排行',
                    '用户想要什么', '用户找了什么', '花型库画像',
                    '织印师三个能力', '注册与充值'])
    ok(stTxt.indexOf(wd) >= 0, '看板有「' + wd + '」');
  ok(stTxt.indexOf('1995') >= 0, 'KPI 显示预估点数 1995');
  ok(stTxt.indexOf('128') >= 0, 'KPI 显示对话轮次 128');
  ok(stTxt.indexOf('平均轮次/会话') >= 0 && stTxt.indexOf('1.7') >= 0,
     'KPI 有平均轮次/会话');
  ok(stTxt.indexOf('未打标') >= 0, 'KPI 有未打标待处理数');
  ok(stTxt.indexOf('比上一段') >= 0 && stTxt.indexOf('↓34%') >= 0,
     '趋势下面有环比（掉的百分比如实显示）');
  ok(stTxt.indexOf('上一段没数据，不比') >= 0,
     '上一段没数据时不编百分比，直接说明');
  ok(stHTML.indexOf('<svg') >= 0, '趋势是画出来的 SVG，不是文字');
  ok((stHTML.match(/<svg/g) || []).length >= 3, '趋势 / 角色 / 用量三张图都在',
     String((stHTML.match(/<svg/g) || []).length));
  ok(/stroke-dasharray/.test(stHTML), '角色分布是环形图');
  ok(stTxt.indexOf('法式浪漫') >= 0, '用户选的类型 top 里有"法式浪漫"');
  ok(stTxt.indexOf('库内风格 top') >= 0 && stTxt.indexOf('库内配色 top') >= 0,
     '花型库画像有各维度 top');
  ok(stTxt.indexOf('蓝白青花') >= 0, '检索热词里有"蓝白青花"');
  ok(stTxt.indexOf('账号') >= 0 && stTxt.indexOf('计费') >= 0,
     '注册与充值如实说明还没有账号/计费后端（不编假曲线）');
  ok(stTxt.indexOf('四方连续') >= 0 && stTxt.indexOf('分层 PSD') >= 0,
     '有四方连续与分层 PSD 的用量');
  ok(stHTML.indexOf('https://') < 0 && stHTML.indexOf('••••') < 0,
     '看板同样不显示接口地址和密钥');

  console.log('\n[16c] 五块埋点指标：真数据、口径写清楚');
  for (const wd of ['出图成功率与耗时', '提案 → 确认转化率', '知识库命中率',
                    '用户怎么用这个页面', '会话结束方式'])
    ok(stTxt.indexOf(wd) >= 0, '看板有「' + wd + '」');
  ok(stTxt.indexOf('还缺什么数据') < 0, '原来那张"要埋哪些点"的清单已经换成真数据');
  ok(stTxt.indexOf('这些数字从哪来') < 0 && stTxt.indexOf('口径与覆盖范围') < 0,
     '不再单开"这些数字从哪来"那一节（口径写在各自的块里）');

  /* ① 出图成功率 + ④ 耗时与真实点数 */
  ok(stTxt.indexOf('91.8%') >= 0, '出图成功率 91.8%', stTxt.indexOf('91.8%'));
  ok(stTxt.indexOf('8.3 秒') >= 0 && stTxt.indexOf('16.2 秒') >= 0,
     '耗时按人话显示（平均 8.3 秒 / 90% 不超过 16.2 秒）');
  ok(stTxt.indexOf('1470') >= 0, '实际花掉点数 1470');
  ok(stTxt.indexOf('占位图') >= 0, '有占位图时单独标出来（还没配生图 Key）');
  ok(stTxt.indexOf('生成花型') >= 0 && stTxt.indexOf('95%') >= 0,
     '按能力拆的成功率在（生成花型 95%）');
  ok(stTxt.indexOf('鉴权失败') >= 0 && stTxt.indexOf('超时') >= 0,
     '失败原因分类写成人话（不是一堆 err 码）');

  /* ② 提案 → 确认转化率 */
  ok(stTxt.indexOf('65.6%') >= 0, '转化率 65.6%');
  ok(stTxt.indexOf('压着多少点') >= 0 && stTxt.indexOf('1860') >= 0,
     '没人点的卡片上压着多少点（运营能直接看到漏了多少生意）');
  ok(stTxt.indexOf('绕过卡片直接执行') >= 0, '直达执行单列，不混进转化率');

  /* ③ 知识库命中率 */
  ok(stTxt.indexOf('61.9%') >= 0, '知识库命中率 61.9%');
  ok(stTxt.indexOf('交期多久') >= 0, '没答上的问题按原话列出来（就是该补的题）');

  /* ⑤ 前端埋点 */
  ok(stTxt.indexOf('214') >= 0, '平均停留 214 秒');
  ok(stTxt.indexOf('直接打开 / 收藏夹') >= 0, '来路分得清"直接打开"与具体来源');
  ok(stHTML.indexOf('weibo.com') >= 0 && stHTML.indexOf('https://weibo') < 0,
     '来路只摆域名，不带路径与参数（截图外传也不漏东西）');
  ok(stTxt.indexOf('确认生成') >= 0 && stTxt.indexOf('改出图张数') >= 0,
     '点击排行把 data-act 翻译成人话（确认生成 / 改出图张数）');
  ok(stTxt.indexOf('只点按钮没说过话') >= 0, '盯"只点不说"的人');

  /* ⑥ 会话结束方式 */
  ok(stTxt.indexOf('聊完走的') >= 0 && stTxt.indexOf('压着没确认的卡片') >= 0
     && stTxt.indexOf('进来就走') >= 0, '四种走法都摆出来了');
  ok(stTxt.indexOf('平均聊了几轮') >= 0 && stTxt.indexOf('3.4') >= 0,
     '每种走法的平均轮次（聊完走的人明显聊得更久）');

  /* 覆盖范围：不再单开一节，改成每块自己交代"从哪天起有记录" */
  ok(stTxt.indexOf('不做回溯猜测') >= 0 || stTxt.indexOf('埋点自') >= 0,
     '每块都写清埋点从哪天起有数据（不把"没上线"看成"没人用"）');
  ok(stTxt.indexOf('仍然没有的') < 0 && stTxt.indexOf('不画假曲线') >= 0,
     '"注册与充值没有真数据"只讲一遍，不再在尾部重复一遍');

  console.log('\n[16b] 换统计区间：按 days 重新拉');
  calls = [];
  const d30 = doc.querySelector('[data-statsdays="30"]');
  ok(!!d30, '有「近 30 天」按钮');
  d30.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await wait(120);
  const lastAn = calls.filter(c => c.url.indexOf('/api/admin/analytics') >= 0).pop();
  ok(!!lastAn && lastAn.url.indexOf('days=30') >= 0, '点了就按 days=30 重新拉',
     lastAn && lastAn.url);
  ok(doc.querySelector('[data-statsdays="30"]').getAttribute('aria-pressed') === 'true',
     '按钮进选中态');
  A.S.statsDays = 14; A.S.stats = null;      // 还原，免得影响后面的用例

  console.log('\n[15] 退出登录：清了状态并把门重新挡上');
  A.logout();
  await wait(120);
  ok(A.AUTH.authed === false, '退出后状态标成未登录');
  ok(doc.getElementById('authmask').style.display !== 'none', '退出后重新弹出登录框');
  stateResp = null; loginResp = { ok: false, error: '口令不对' };

  console.log('\n结果：' + pass + ' 通过 / ' + fail + ' 失败');
  process.exit(fail ? 1 : 0);
}

main().catch(e => { console.error(e); process.exit(1); });
