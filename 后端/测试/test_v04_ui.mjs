/* v0.4 界面改动回归：品牌名 / 去掉搜索框 / 去掉「本地技能」尾巴 / 快捷选项按钮 / 轻量 Markdown 渲染
   跑法：NODE_PATH=<node-workspace>/node_modules node tools/test_v04_ui.mjs
   纯前端测试：stub 掉 fetch，不依赖真实服务与模型。 */
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const { JSDOM } = require('jsdom');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const HTML = process.env.PF_CLIENT_HTML
  ? path.resolve(process.env.PF_CLIENT_HTML)
  : path.resolve(HERE, '..', '..', '前端', '历史版本', 'PatternFlow花型智造-交互原型v0.4.html');
let pass = 0, fail = 0;
function ok(cond, label, extra){
  if (cond){ pass++; console.log('  ✓ ' + label); }
  else { fail++; console.log('  ✗ ' + label + (extra ? '  → ' + extra : '')); }
}

const src = fs.readFileSync(HTML, 'utf8');
const dom = new JSDOM(src, { runScripts: 'dangerously', pretendToBeVisual: true, url: 'http://127.0.0.1:8787/' });
const w = dom.window;
const doc = w.document;

// ---------- 1. 侧栏 ----------
console.log('[1] 侧栏：品牌名与搜索框');
const brand = doc.querySelector('.rail-head .brand .tx b');
ok(!!brand, '侧栏品牌节点存在');
ok(brand && brand.textContent.replace(/\s+/g, '') === 'CYQ·PatternFlow',
   '品牌名为 CYQ·PatternFlow', brand && JSON.stringify(brand.textContent));
const pre = doc.querySelector('.rail-head .brand-pre');
ok(pre && pre.textContent === 'CYQ·', 'CYQ· 为独立高亮片段', pre && JSON.stringify(pre.textContent));
ok(!doc.getElementById('navsearch'), '搜索输入框已删除');
ok(!doc.querySelector('.rail-search'), '搜索框容器已删除');
ok(doc.querySelectorAll('#nav .row').length >= 4, '导航条目仍正常渲染',
   String(doc.querySelectorAll('#nav .row').length));

// ---------- 2. header 里不能有搜索 ----------
ok(!doc.querySelector('input[placeholder*="搜索"]'), '页面内已无任何“搜索…”输入框');

// ---------- 3. 轻量 Markdown 渲染 ----------
console.log('[2] 回复渲染：Markdown 与「本地技能」尾巴');
ok(typeof w.mdLine === 'function' && typeof w.richText === 'function', 'mdLine / richText 已暴露');
if (typeof w.richText === 'function'){
  const h = w.richText('**首选 — 花园下午茶**（匹配 91）\n> 雪纺、人造丝\n\n要不要再放宽一档？');
  ok(h.includes('<b>首选 — 花园下午茶</b>'), '**加粗** 渲染成 <b>');
  ok(!h.includes('**'), '正文不含裸露星号');
  ok(h.includes('class="qline"'), '“> 引用”渲染成引用块');
  ok((h.match(/<p>/g) || []).length === 2, '空行切分成两段',
     String((h.match(/<p>/g) || []).length));
}

// ---------- 4. 模型回复后的快捷选项按钮 ----------
console.log('[3] 模型回复后补一排选项按钮');
// stub fetch：模拟服务返回「只缺配色」的需求卡 + 追问配色的回复
const REPLY = '配色想走哪一路？莫兰迪灰调、蓝白青花，还是奶油粉调这类？';
w.fetch = function(){
  return Promise.resolve({ json: function(){
    return Promise.resolve({
      ok: true, agent: 'designer', mode: 'llm',
      reply: REPLY,
      brief: { style: '法式浪漫', element: '花卉', color: null, scene: '女装连衣裙' },
      picks: [], skills: ['update_brief']
    });
  }});
};

const wait = () => new Promise(r => setTimeout(r, 60));
await w.say('想要法式的小碎花，做春夏连衣裙');
await wait(); await wait();

const msgs = doc.querySelectorAll('#msgs .msg');
ok(msgs.length >= 2, '对话区有用户与机器人两条消息', String(msgs.length));
const bot = msgs[msgs.length - 1];
const botText = bot.textContent;
ok(botText.includes('配色想走哪一路'), '机器人回复正文已渲染');
ok(!botText.includes('本地技能'), '机器人回复不含「本地技能」文案', botText.slice(0, 60));
ok(!botText.includes('update_brief'), '机器人回复不含技能名 update_brief');

const opts = bot.querySelectorAll('.opt-row .opt');
ok(opts.length === 6, '追问时出现 6 个快捷选项（4 候选 + 不知道 + 趋势）', String(opts.length));
const labels = Array.from(opts).map(b => b.textContent.replace(/^↗/, '').trim());
// 回复里被点名的取值要排在最前，且顺序与话术一致
ok(labels.slice(0, 3).join('|') === '莫兰迪灰调|蓝白青花|粉紫梦幻',
   '按钮优先给回复里点名过的取值（按出现顺序）', JSON.stringify(labels));
ok(labels.length === 6 && new Set(labels).size === 6, '按钮无重复');
ok(labels.includes('不知道'), '含「不知道」兜底');
ok(labels.includes('看流行趋势推荐'), '含「看流行趋势推荐」');
ok(bot.querySelectorAll('.opt-row button.trend').length === 1, '趋势按钮带 trend 样式与标记');

// ---------- 5. 点选项 = 直接把该值发出去 ----------
console.log('[4] 点选项行为');
const first = opts[0];
ok(first.getAttribute('data-say') === '莫兰迪灰调', '选项挂 data-say 可直接发送',
   first.getAttribute('data-say'));

// ---------- 6. 有候选时不叠加快捷选项 ----------
console.log('[5] 出候选时只显示候选卡，不叠加选项');
w.fetch = function(){
  return Promise.resolve({ json: function(){
    return Promise.resolve({ ok: true, mode: 'llm',
      reply: '给你挑了 3 款：\n\n**花园下午茶**（匹配 91）',
      brief: { style: '法式浪漫', element: '花卉', color: '蓝白青花', scene: '女装连衣裙' },
      picks: ['PF-OWN-2026-000184', 'PF-OWN-2026-000128', 'PF-OWN-2026-000131'],
      skills: ['update_brief', 'search_patterns'] });
  }});
};
await w.say('奶油白底，蓝白青花');
await wait(); await wait();
const last = doc.querySelectorAll('#msgs .msg');
const tail = last[last.length - 1];
ok(!tail.textContent.includes('本地技能'), '带候选的回复同样不含「本地技能」');
const picker = Array.from(doc.querySelectorAll('#msgs [data-act="open-picker"]'));
ok(picker.length === 1, '出候选时挂出「打开花型选择」入口', String(picker.length));
ok(picker.length && picker[0].textContent.includes('3 款'), '入口文案带上候选数量',
   picker.length ? picker[0].textContent : '');
ok(!tail.textContent.includes('不知道'), '最后一轮没有叠加多余的维度快捷项');

console.log('\n结果：' + pass + ' 通过 / ' + fail + ' 失败');
process.exit(fail ? 1 : 0);
