// LLM Planner：读经验库（lessons/rules/learned）+ 图像特征 + 可用方法，决定候选顺序/参数，或拒单。
// 设计原则：
//  - planner 只做"选择"，不发明未实现的方法；
//  - 失败/拒单也要给出理由，写入 knowledge/plans.jsonl 供后续学习；
//  - LLM 失败时调用方回退到 rules 策略（strategy.mjs）。
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), '..');
const KB = path.join(ROOT, 'knowledge');

export const METHODS = [
  { id: 'fast', desc: '跳过 AI 清洗，直接对原图做确定性补缝（最便宜，纹理密集图可试；渐变图会出矩形色块 L02）' },
  { id: 'det', desc: 'AI 清洗 + 确定性补缝（quilt+multiband；wrap 好但可能留细缝线）' },
  { id: 'ai', desc: 'AI 清洗 + AI 缝带修复（band 120-160；内部自然但带边界可能切断 L05/L12，且必须过 wrap 指标）' },
  { id: 'regenerate', desc: 'AI 清洗 + 原生重生成（同风格重排；模型不保证可平铺，wrap 常超 1.3）' },
  { id: 'tile-multi', desc: 'AI 清洗 + 多样本重生成 + wrap 硬筛（只接受 wrap<=1.3，成本 N×$0.016，可能全被拒）' },
];

function readText(p) { try { return fs.readFileSync(p, 'utf8'); } catch { return ''; } }
export function loadKnowledge() {
  let rules = {}, learned = {};
  try { rules = JSON.parse(readText(path.join(KB, 'rules.json'))); } catch {}
  try { learned = JSON.parse(readText(path.join(KB, 'learned.json'))); } catch {}
  const lessons = readText(path.join(KB, 'lessons.md'))
    .split('\n').filter(l => /^## L\d+/.test(l)).map(l => l.replace(/^## /, '')).slice(-14);
  return { rules, learned, lessons };
}

export function clusterKey(f) {
  const bin = (x, edges) => { const i = edges.findIndex(e => x < e); return i < 0 ? edges.length : i; };
  return `lg${bin(f?.lowGradFrac ?? 0.5, [0.25, 0.5, 0.75])}-hc${bin(f?.hueConc ?? 0.5, [0.5, 0.85])}-ed${bin(f?.edgeMean ?? 20, [15, 40])}`;
}

function buildPrompt({ feats, cluster, learnedCluster, lessons, rulesText }) {
  const paths = learnedCluster?.paths || {};
  const norm = (p) => String(p).startsWith('B-clean+ai') ? 'ai' : String(p).startsWith('C-clean') ? 'regenerate'
    : String(p).includes('deterministic') || String(p).startsWith('B-clean+det') ? 'det' : String(p).startsWith('A-fast') ? 'fast' : p;
  const failed = [...new Set(Object.entries(paths).filter(([, v]) => (v.score ?? 0) <= 0.4).map(([k]) => norm(k)))];
  const ok = [...new Set(Object.entries(paths).filter(([, v]) => (v.score ?? 0) > 0.4).map(([k]) => norm(k)))];
  const tried = new Set([...failed, ...ok]);
  const untested = METHODS.map(m => m.id).filter(id => !tried.has(id));
  const tags = {};
  for (const v of Object.values(paths)) for (const [t, n] of Object.entries(v.tags || {})) tags[t] = (tags[t] || 0) + n;
  const tagText = Object.entries(tags).sort((a, b) => b[1] - a[1]).slice(0, 8).map(([t, n]) => `${t}(${n})`).join(' ') || '无';
  return `你是面料描稿流水线的调度员（planner）。根据图像特征与历史经验，从可用方法中选出候选执行顺序，或直接拒单（abort）。
可用方法（只能选这些 id）：${METHODS.map(m => `${m.id}: ${m.desc}`).join('\n')}
硬性约束：任何输出必须通过整图审查：watch_lines 无贯穿直线 + wrap ratio<=1.2；不通过则不能交付。
图像特征：${JSON.stringify(feats)}
该图所属特征簇：${cluster}
该簇历史反馈统计（可能为空）：${JSON.stringify(learnedCluster || {})}
本簇已失败路径：${failed.join(',') || '无'}；本簇成功路径：${ok.join(',') || '无'}；未试过的路径：${untested.join(',') || '无'}；高频问题标签：${tagText}
历史教训（节选）：${lessons.join(' | ')}
先验规则：${rulesText}
决策要求：
1) 有历史成功路径 → 优先它；有失败路径 → 降权；
2) 该簇所有已试路径都失败过 → 优先未试过的方法；
3) **如果存在未试过的路径，order 必须以它们开头**；已失败路径排在后面作为兜底；
   若高频问题标签包含 band-boundary / rect-overlay / review-gap 等结构性问题，且未试过路径数量有限，
   请评估是否 abort（说明缺什么能力），不要为了试而试；
4) 若判断没有任何方法可能通过（例如已知该簇受 L12 全高缝带约束且可用方法都不适用），输出 abort=true 并说明缺什么能力；
4) 给出 order（方法 id 数组）、params（可选：band/feather/samples）、rationale（一句话中文）。
只输出 JSON：{"order":["ai"],"params":{"band":140,"feather":20},"abort":false,"rationale":"..."}`;
}

async function chat({ base, key, model, prompt, timeoutMs = 45000 }) {
  const ctrl = AbortSignal.timeout(timeoutMs);
  const res = await fetch(`${base}/chat/completions`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${key}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ model, messages: [{ role: 'user', content: prompt }], temperature: 0, max_tokens: 400, stream: false }),
    signal: ctrl,
  });
  const text = await res.text();
  let content = '';
  if (text.trim().startsWith('data:')) {
    for (const line of text.split('\n')) {
      const s = line.trim(); if (!s.startsWith('data:')) continue;
      const j = s.slice(5).trim(); if (j === '[DONE]') continue;
      try { const o = JSON.parse(j); content += o.choices?.[0]?.delta?.content || ''; } catch {}
    }
  } else {
    try { const o = JSON.parse(text); content = o.choices?.[0]?.message?.content || text; } catch { content = text; }
  }
  return content;
}

// 主入口：返回 {order, params, abort, rationale, source}
export async function planRun({ feats, apiBase, key, model = 'gemini-2.5-flash-lite', log = true }) {
  const { rules, learned, lessons } = loadKnowledge();
  const cluster = clusterKey(feats);
  const learnedCluster = learned?.clusters?.[cluster] || null;
  const rulesText = JSON.stringify(rules?.strategy || []).slice(0, 1200);
  const prompt = buildPrompt({ feats, cluster, learnedCluster, lessons, rulesText });
  let parsed = null;
  try {
    const txt = await chat({ base: apiBase, key, model, prompt });
    const m = txt.match(/\{[\s\S]*\}/);
    parsed = m ? JSON.parse(m[0]) : null;
  } catch (e) { parsed = null; }
  let result;
  if (parsed && Array.isArray(parsed.order)) {
    const valid = parsed.order.filter(id => METHODS.some(x => x.id === id));
    result = { order: valid.length ? valid : METHODS.map(m => m.id), params: parsed.params || {}, abort: !!parsed.abort, rationale: parsed.rationale || '', cluster, source: 'llm' };
  } else {
    result = { order: null, params: {}, abort: false, rationale: 'planner 不可用，回退先验规则', cluster, source: 'fallback' };
  }
  if (log) {
    try {
      fs.appendFileSync(path.join(KB, 'plans.jsonl'), JSON.stringify({ ts: new Date().toISOString(), feats, cluster, learned_cluster: learnedCluster, result }) + '\n');
    } catch {}
  }
  return result;
}

// CLI（调试用）：node lib/planner.mjs --features '{"lowGradFrac":0.07,...}'
if (process.argv[1] && process.argv[1].endsWith('planner.mjs')) {
  const args = {};
  for (let i = 2; i < process.argv.length; i++) if (process.argv[i].startsWith('--')) { const k = process.argv[i].slice(2); const v = process.argv[i + 1] && !process.argv[i + 1].startsWith('--') ? process.argv[++i] : true; args[k] = v; }
  const { apiConfig } = await import('./env.mjs');
  const { base, key } = apiConfig(ROOT);
  const feats = JSON.parse(args.features || '{"lowGradFrac":0.5,"hueConc":0.5,"satMean":0.2,"edgeMean":20}');
  const r = await planRun({ feats, apiBase: base, key });
  console.log(JSON.stringify(r, null, 2));
}
