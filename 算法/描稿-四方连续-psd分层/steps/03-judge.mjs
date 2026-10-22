#!/usr/bin/env node
// 第 3 步：判断该图适合"语义分层"还是"色层分色"
// 最便宜可靠组合：gemini-2.5-flash-lite @512px + 特征入 prompt + 2 次自洽 + 特征规则兜底
// 成本约 $0.0004 / 3 张图（实测），约 17s
// 用法: node steps/03-judge.mjs img1.png [img2.png ...]
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import sharp from '../lib/sharp.mjs';
import { apiConfig, apiJson } from '../lib/env.mjs';

const ROOT = fileURLToPath(new URL('..', import.meta.url));

const MODEL = process.env.JUDGE_MODEL || 'gemini-2.5-flash-lite';
const PRICE_IN = 0.10 / 1e6, PRICE_OUT = 0.40 / 1e6; // Gemini 2.5 Flash-Lite 公开牌价

export async function features(path) {
  const { data, info } = await sharp(path).removeAlpha().resize(512).raw().toBuffer({ resolveWithObject: true });
  const W = info.width, H = info.height, N = W * H, ch = info.channels;
  let sat = 0, hx = 0, hy = 0, edges = 0, low = 0;
  const g = (x, y) => { const i = (y * W + x) * ch; return 0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2]; };
  for (let i = 0; i < N; i++) {
    const r = data[i * ch] / 255, gg = data[i * ch + 1] / 255, b = data[i * ch + 2] / 255;
    const mx = Math.max(r, gg, b), mn = Math.min(r, gg, b), d = mx - mn;
    sat += mx ? d / mx : 0;
    let h = 0;
    if (d > 1e-6) { if (mx === r) h = ((gg - b) / d) % 6; else if (mx === gg) h = (b - r) / d + 2; else h = (r - gg) / d + 4; h *= 60; if (h < 0) h += 360; }
    hx += Math.cos(h * Math.PI / 180); hy += Math.sin(h * Math.PI / 180);
  }
  for (let y = 1; y < H - 1; y++) for (let x = 1; x < W - 1; x++) {
    const c = g(x, y), l = Math.abs(2 * c - g(x - 1, y) - g(x + 1, y)) + Math.abs(2 * c - g(x, y - 1) - g(x, y + 1));
    edges += l; if (l < 2) low++;
  }
  return { satMean: +(sat / N).toFixed(3), hueConc: +(Math.sqrt(hx * hx + hy * hy) / N).toFixed(3), edgeMean: +(edges / N).toFixed(2), lowGradFrac: +(low / N).toFixed(3) };
}

function buildPrompt(feats) {
  const list = feats.map((f, i) => `图${i + 1} ${JSON.stringify(f)}`).join('\n');
  return `你是纺织印花描稿/分色专家。判断每张图更适合：
A=semantic（语义分层：按对象分组，如底色/叶子/蓝花/橙花/茎线/黑墨/驼色花瓣/底纹）
B=color（色层分色：按颜色聚类 8-12 层，保留渐变与纹理）
判据：颜色少且同色系、渐变/晕染强、语义边界模糊→B；多花种、色相差异大、轮廓清晰、需按对象控墨→A；水彩/低饱和晕染→B；荧光/高饱和撞色→A；单色渐变/矢量平涂→B；图文/点阵/丝印强对比→A。
量化特征（satMean 平均饱和度, hueConc 色相集中度, edgeMean 边缘强度, lowGradFrac 平坦区占比）：
${list}
只输出 JSON（不要解释）：{"patterns":[{"id":1,"recommended":"A"|"B","confidence":0.0},...]}`;
}

async function callVlm(feats, imgs, { base, key }) {
  const content = [{ type: 'text', text: buildPrompt(feats) }];
  for (const p of imgs) content.push({ type: 'image_url', image_url: { url: `data:image/png;base64,${fs.readFileSync(p).toString('base64')}` } });
  const { json: body } = { json: { model: MODEL, messages: [{ role: 'user', content }], temperature: 0, max_tokens: 300, stream: false } };
  const res = await fetch(`${base}/chat/completions`, { method: 'POST', headers: { Authorization: `Bearer ${key}`, 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const txt = await res.text();
  let out = '', usage = null;
  if (txt.trim().startsWith('data:')) {
    for (const line of txt.split('\n')) {
      const s = line.trim(); if (!s.startsWith('data:')) continue;
      const j = s.slice(5).trim(); if (j === '[DONE]') continue;
      try { const o = JSON.parse(j); out += o.choices?.[0]?.delta?.content || ''; if (o.usage) usage = o.usage; } catch {}
    }
  } else {
    try { const o = JSON.parse(txt); out = o.choices?.[0]?.message?.content || txt; usage = o.usage; } catch { out = txt; }
  }
  const m = out.match(/\{[\s\S]*\}/); const parsed = m ? JSON.parse(m[0]) : null;
  const map = { A: 'semantic', B: 'color' };
  const rec = parsed?.patterns?.map(p => ({ id: p.id, method: map[String(p.recommended).toUpperCase()] || null, confidence: p.confidence ?? null }));
  return { rec, usage, cost: usage ? usage.prompt_tokens * PRICE_IN + usage.completion_tokens * PRICE_OUT : 0 };
}

function heuristic(f) {
  // 高饱和 + 多色相 + 清晰边缘 → 语义；否则色层
  return (f.satMean > 0.25 && f.lowGradFrac < 0.20 && f.hueConc < 0.60) ? 'semantic' : 'color';
}

export async function judge(imgs, cwd = ROOT) {
  const t0 = Date.now();
  const { base, key } = apiConfig(cwd);
  const feats = [];
  for (const p of imgs) feats.push(await features(p));
  const runs = [];
  for (let i = 0; i < 2; i++) runs.push(await callVlm(feats, imgs, { base, key }));
  const valid = runs.filter(r => r.rec && r.rec.length === imgs.length);
  let picks, source;
  if (valid.length >= 2 && JSON.stringify(valid[0].rec.map(r => r.method)) === JSON.stringify(valid[1].rec.map(r => r.method))) {
    picks = valid[0].rec; source = 'vlm-2x-agree';
  } else {
    runs.push(await callVlm(feats, imgs, { base, key }));
    const v2 = runs.filter(r => r.rec && r.rec.length === imgs.length);
    if (v2.length >= 2) {
      const keys = v2.map(r => JSON.stringify(r.rec.map(x => x.method)));
      const counts = {}; keys.forEach(k => counts[k] = (counts[k] || 0) + 1);
      const best = Object.entries(counts).sort((a, b) => b[1] - a[1])[0];
      picks = v2[keys.indexOf(best[0])].rec; source = 'vlm-majority';
    } else {
      picks = feats.map((f, i) => ({ id: i + 1, method: heuristic(f), confidence: null }));
      source = 'heuristic-fallback';
    }
  }
  const totalCost = runs.reduce((s, r) => s + r.cost, 0);
  return {
    model: MODEL,
    method_source: source,
    patterns: picks.map((p, i) => ({ id: p.id ?? i + 1, image: imgs[i], recommended: p.method || heuristic(feats[i]), confidence: p.confidence, features: feats[i] })),
    est_cost_usd: +totalCost.toFixed(6),
    calls: runs.length,
    seconds: +((Date.now() - t0) / 1000).toFixed(1),
  };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const imgs = process.argv.slice(2);
  if (!imgs.length) { console.error('用法: node steps/03-judge.mjs <img1> [img2 ...]'); process.exit(1); }
  const r = await judge(imgs);
  console.log(JSON.stringify(r, null, 2));
}
