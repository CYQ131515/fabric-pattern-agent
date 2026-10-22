import fs from 'node:fs';
import path from 'node:path';

export function loadEnv(dir = process.cwd()) {
  const env = { ...process.env };
  for (const name of ['.env', '.apimart.env']) {
    const p = path.join(dir, name);
    if (fs.existsSync(p)) {
      for (const line of fs.readFileSync(p, 'utf8').split('\n')) {
        const s = line.trim();
        if (!s || s.startsWith('#')) continue;
        const i = s.indexOf('=');
        // 进程环境优先：后端（PatternFlow）启动算法时会把后台配置的 key 注入
        // 环境变量，这里不能再被 .env 盖掉；单独跑算法时没有这些变量，照旧读 .env。
        if (i > 0 && env[s.slice(0, i).trim()] === undefined) {
          env[s.slice(0, i).trim()] = s.slice(i + 1).trim();
        }
      }
    }
  }
  return env;
}

export function apiConfig(dir) {
  const env = loadEnv(dir);
  if (!env.APIMART_BASE || !env.APIMART_KEY) {
    throw new Error('缺少 APIMART_BASE / APIMART_KEY：请复制 .env.example 为 .env 并填写');
  }
  return { base: env.APIMART_BASE.replace(/\/$/, ''), key: env.APIMART_KEY, env };
}

export async function apiJson(url, key, body, { method = 'POST', timeoutMs = 25000, tries = 3 } = {}) {
  let lastErr;
  for (let i = 0; i < tries; i++) {
    try {
      const res = await fetch(url, {
        method,
        headers: { Authorization: `Bearer ${key}`, 'Content-Type': 'application/json' },
        body: body ? JSON.stringify(body) : undefined,
        signal: AbortSignal.timeout(timeoutMs),
      });
      const text = await res.text();
      let json = null;
      try { json = JSON.parse(text); } catch {}
      return { status: res.status, json, text };
    } catch (e) {
      lastErr = e;
      const msg = String(e?.message || e);
      const netErr = e?.name === 'TimeoutError' || e?.name === 'AbortError'
        || /fetch failed|ECONN|ETIMEDOUT|ENOTFOUND|EAI_AGAIN|socket hang up/i.test(msg);
      // 代理/线路会偶发抖动：轮询任务时单次抖动就放弃太脆了，
      // 短暂退避后重试；3 次都失败才抛错（最坏 ~80s，不会无限拖）。
      if (i < tries - 1) {
        await new Promise(r => setTimeout(r, netErr ? 1200 : 2000));
        continue;
      }
    }
  }
  const detail = lastErr?.cause?.code || lastErr?.cause?.message || lastErr?.message || lastErr;
  throw new Error(`连不上生图网关（网络/代理不通）: ${detail}`);
}

export async function waitTask(base, key, taskId, { intervalMs = 5000, maxTries = 90, label = '' } = {}) {
  for (let i = 0; i < maxTries; i++) {
    await new Promise(r => setTimeout(r, intervalMs));
    // 轮询任务状态：网关偶尔慢，给 60s；走的是 GET，不走提交重试
    const { json } = await apiJson(`${base}/tasks/${taskId}`, key, null,
                                  { method: 'GET', timeoutMs: 60000 });
    const d = json?.data;
    if (d?.status === 'completed') return d;
    if (d?.status === 'failed') throw new Error(`${label} 任务失败: ${JSON.stringify(d).slice(0, 300)}`);
    if (i % 6 === 5) console.log(`  ${label} 等待中… ${i * intervalMs / 1000}s`);
  }
  throw new Error(`${label} 超时`);
}

export async function generateImage({ base, key, model, prompt, size, imageUrls = [], maskUrl, n = 1, label = '' }) {
  const body = { model, prompt, size, n };
  if (imageUrls.length) body.image_urls = imageUrls;
  if (maskUrl) body.mask_url = maskUrl;
  let taskId = null, lastStatus = null, lastJson = null;
  for (let attempt = 0; attempt < 6; attempt++) {
    const { status, json } = await apiJson(`${base}/images/generations`, key, body);
    lastStatus = status; lastJson = json;
    taskId = json?.data?.[0]?.task_id;
    if (taskId) break;
    // 503 model_not_found / 429 等临时错误：等待后重试
    if (status === 503 || status === 429 || status >= 500) {
      await new Promise(r => setTimeout(r, 8000 + attempt * 5000));
      continue;
    }
    break;
  }
  if (!taskId) throw new Error(`${label} 提交失败 (${lastStatus}): ${JSON.stringify(lastJson).slice(0, 300)}`);
  const done = await waitTask(base, key, taskId, { label });
  return {
    taskId,
    cost: Number(done.cost || 0),
    urls: done.result?.images?.[0]?.url || [],
  };
}

export async function download(url, outPath, tries = 5) {
  let lastErr;
  let res;
  for (let i = 0; i < tries; i++) {
    try { res = await fetch(url); if (res.ok) break; lastErr = new Error(`下载失败 ${res.status}`); }
    catch (e) { lastErr = e; }
    if (i < tries - 1) await new Promise(r => setTimeout(r, 3000));
  }
  if (!res || !res.ok) throw lastErr || new Error('下载失败');
  const buf = Buffer.from(await res.arrayBuffer());
  fs.writeFileSync(outPath, buf);
  return buf.length;
}
