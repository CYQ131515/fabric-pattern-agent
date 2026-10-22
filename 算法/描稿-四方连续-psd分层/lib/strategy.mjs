// 经验驱动的策略派发：读 knowledge/rules.json，根据图像特征给出候选路径顺序
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const ROOT = path.dirname(fileURLToPath(import.meta.url)) + '/..';
const RULES_PATH = path.join(ROOT, 'knowledge', 'rules.json');
const LEARNED_PATH = path.join(ROOT, 'knowledge', 'learned.json');

export function loadRules() {
  try { return JSON.parse(fs.readFileSync(RULES_PATH, 'utf8')); } catch { return { strategy: [] }; }
}
export function loadLearned() {
  try { return JSON.parse(fs.readFileSync(LEARNED_PATH, 'utf8')); } catch { return null; }
}
function clusterKey(f) {
  const bin = (x, edges) => { const i = edges.findIndex(e => x < e); return i < 0 ? edges.length : i; };
  const lg = bin(f?.lowGradFrac ?? 0.5, [0.25, 0.5, 0.75]);
  const hc = bin(f?.hueConc ?? 0.5, [0.5, 0.85]);
  const ed = bin(f?.edgeMean ?? 20, [15, 40]);
  return `lg${lg}-hc${hc}-ed${ed}`;
}

// 条件求值：支持 lowGradFrac_gte / hueConc_gte / edgeMean_lt / lowGradFrac_lt / satMean_gte 等
function matchWhen(when, feats) {
  if (!when || !Object.keys(when).length) return true;
  for (const [k, v] of Object.entries(when)) {
    if (k.endsWith('_unknown')) return false;
    const m = k.match(/^(\w+?)_(gte|lte|gt|lt)$/);
    if (!m) return false;                   // 未知/语义条件（如 lineart）不匹配，避免误命中
    const [, key, op] = m;
    const x = feats?.[key];
    if (typeof x !== 'number') continue;
    if (op === 'gte' && !(x >= v)) return false;
    if (op === 'lte' && !(x <= v)) return false;
    if (op === 'gt' && !(x > v)) return false;
    if (op === 'lt' && !(x < v)) return false;
  }
  return true;
}
const PREF_TO_CANDIDATE = (p) => {
  const s = String(p);
  if (s.startsWith('seam-ai')) return 'ai';
  if (s.startsWith('seam-deterministic')) return 'det';
  if (s.startsWith('seam-regenerate')) return 'regenerate';
  if (s.startsWith('seam-tile-multi')) return 'tile-multi';
  if (s === 'fast' || s.startsWith('fast')) return 'fast';
  return null;
};

// 返回 { attempts:['fast','ai',...], matched:[ruleIds], defaulted:bool }
export function planAttempts(feats, seamMode = 'auto', flatLike = false) {
  if (seamMode && seamMode !== 'auto') {
    const forced = seamMode === 'fast' ? 'fast' : seamMode === 'ai' ? 'ai' : seamMode === 'regenerate' ? 'regenerate' : 'det';
    return { attempts: [forced], matched: [], forced: true };
  }
  const rules = loadRules().strategy || [];
  const matched = [];
  let ordered = [];
  for (const r of rules) {
    if (!matchWhen(r.when, feats)) continue;
    // 带 avoid 的场景：对应路径排到最后
    const before = ordered.length;
    for (const p of r.prefer || []) {
      const c = PREF_TO_CANDIDATE(p);
      if (c && !ordered.includes(c)) ordered.push(c);
    }
    for (const p of r.avoid || []) {
      const c = PREF_TO_CANDIDATE(p);
      if (c) ordered = ordered.filter(x => x !== c);
    }
    if (ordered.length !== before || (r.prefer || []).some(p => PREF_TO_CANDIDATE(p))) matched.push(r.id);
  }
  // 默认顺序：平坦图先试快速路径；再 AI 补缝；最后重生成
  const fallback = [...(flatLike ? ['fast'] : []), 'ai', 'regenerate'];
  for (const c of fallback) if (!ordered.includes(c)) ordered.push(c);
  // 保证三条路径都在（避免 avoid 把某类彻底删掉后无兜底）
  for (const c of ['fast', 'ai', 'regenerate', 'tile-multi']) if (!ordered.includes(c)) ordered.push(c);
  // 经验学习：若该特征簇的反馈样本足够，按历史通过率重排
  const learned = loadLearned();
  const ck = clusterKey(feats);
  const cl = learned?.clusters?.[ck];
  let learnedApplied = false;
  let prefNote = null;
  if (cl?.recommended?.length) {
    const norm = (p) => String(p).startsWith('B-clean') || String(p).includes('seam-ai') ? 'ai'
      : String(p).startsWith('C-clean') || String(p).includes('regenerate') ? 'regenerate'
      : String(p).includes('deterministic') || String(p).startsWith('B-clean+det') ? 'det'
      : String(p).startsWith('A-fast') ? 'fast' : p;
    const rec = [...new Set(cl.recommended.map(norm))].filter(p => ordered.includes(p));
    const scores = rec.map(p => cl.paths[Object.keys(cl.paths).find(k => norm(k) === p)]?.score ?? 0);
    // 该簇所有已尝试路径都失败过 → 优先尝试未试过的路径（避免重复踩坑）
    if (rec.length && scores.every(x => x <= 0.4)) {
      const known = new Set(rec);
      // 已试路径全失败：优先补一个没试过的路径（即使规则默认顺序里没有它）
      const untried = ['det', 'fast', 'regenerate', 'tile-multi', 'ai'].filter(p => !known.has(p));
      if (untried.length) {
        ordered = [...untried, ...rec, ...ordered.filter(x => !untried.includes(x) && !rec.includes(x))];
        prefNote = 'all-known-failed → try untried: ' + untried.join(',');
      } else {
        ordered = [...rec, ...ordered.filter(x => !rec.includes(x))];
        prefNote = 'known paths only';
      }
      learnedApplied = true;
    } else if (rec.length) {
      ordered = [...rec, ...ordered.filter(x => !rec.includes(x))];
      learnedApplied = true;
    }
  }
  ordered = [...new Set(ordered)];   // 去重（学习重排可能与规则顺序重叠）
  return { attempts: ordered, matched, forced: false, cluster: ck, learned: learnedApplied, learned_note: prefNote };
}
