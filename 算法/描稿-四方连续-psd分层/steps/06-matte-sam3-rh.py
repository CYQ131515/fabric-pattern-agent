#!/usr/bin/env python3
"""第 6 步（语义分割蒙版 · RunningHub SAM3.1 工作流）

背景（2026-10-21 对标 uniseven）：
    他们 PSD 的「整体前景」是整组花叶的软蒙版；本地试 rembg/SAM vit_b 都不适配
    「整幅花型」（见 L43）。改用 RunningHub 上的 SAM3.1 工作流（id 2109041802081820673）
    —— 该工作流用文本提示词检测实例，individual_masks=true 时每个实例单独输出一张
    「原图 + 该实例 alpha」的 PNG；把同一概念的实例 alpha 并集起来，就是干净的
    单类元素蒙版（花朵/叶子/枝条分别跑，再合并）。

工作流节点（通过 /api/openapi/getJsonApiFormat 拿到）：
    431 LoadImage（image）→ 430 SAM3_Detect（conditioning 来自 434 CR Text → 435 翻译 → 436 CLIP）
    430 threshold / individual_masks / refine_iterations
    440/438 JoinImageWithAlpha(原图, mask)  → 432/437 JoinImageWithAlpha(原图, invert mask)
    ⇒ 实例蒙版取「432」输出的 alpha（440 是补集，几乎全图）

用法:
  RUNNINGHUB_API_KEY=xxx python steps/06-matte-sam3-rh.py --input tile.png --out matte.png \
      --prompts "花朵:64,叶子:32,枝条:32" --threshold 0.5
输出: alpha PNG（与输入同尺寸），stdout 一行 JSON 摘要
"""
import os, sys, json, time, argparse, mimetypes, urllib.request, urllib.error, uuid
from collections import deque
import numpy as np
from PIL import Image, ImageFilter

API = 'https://www.runninghub.cn'
WF_DEFAULT = '2109041802081820673'
UPLOAD = API + '/openapi/v2/media/upload/binary'
RUN = API + '/openapi/v2/run/workflow/'
QUERY = API + '/openapi/v2/query'
CREATE = API + '/task/openapi/create'
GET_JSON = API + '/api/openapi/getJsonApiFormat'

ap = argparse.ArgumentParser()
ap.add_argument('--input', required=True)
ap.add_argument('--out', required=True)
ap.add_argument('--prompts', default='花朵:64,叶子:32,枝条:32,金色叶片:64',
                help='文本提示词列表；该工作流的惯例是「概念:最大实例数」，多个用逗号分隔')
ap.add_argument('--threshold', type=float, default=0.5)
ap.add_argument('--workflow-id', default=os.environ.get('RH_WORKFLOW_ID') or WF_DEFAULT)
ap.add_argument('--api-key-env', default='RUNNINGHUB_API_KEY')
ap.add_argument('--timeout', type=int, default=900)
ap.add_argument('--raw-dir', default='')
ap.add_argument('--min-area', type=float, default=2.5e-4, help='碎块面积阈值（占比）')
ap.add_argument('--feather', type=float, default=0.8)
a = ap.parse_args()
t0 = time.time()

key = (os.environ.get(a.api_key_env) or os.environ.get('RUNNINGHUB_API_KEY') or '').strip()
if not key:
    print(json.dumps({'ok': False, 'error': 'missing RUNNINGHUB_API_KEY'}, ensure_ascii=True))
    sys.exit(3)


def log(*xs):
    if a.raw_dir:
        print(*xs, file=sys.stderr)


def http_json(url, payload=None, headers=None, timeout=60):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method='POST' if payload is not None else 'GET')
    req.add_header('Authorization', 'Bearer ' + key)
    req.add_header('Content-Type', 'application/json')
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def upload(path):
    boundary = '----rh' + uuid.uuid4().hex
    ctype = mimetypes.guess_type(path)[0] or 'application/octet-stream'
    with open(path, 'rb') as f:
        blob = f.read()
    parts = []
    parts.append(('--' + boundary + '\r\n').encode())
    parts.append(('Content-Disposition: form-data; name="file"; filename="%s"\r\n' % os.path.basename(path)).encode())
    parts.append(('Content-Type: %s\r\n\r\n' % ctype).encode())
    parts.append(blob)
    parts.append(('\r\n--' + boundary + '--\r\n').encode())
    body = b''.join(parts)
    req = urllib.request.Request(UPLOAD, data=body, method='POST')
    req.add_header('Authorization', 'Bearer ' + key)
    req.add_header('Content-Type', 'multipart/form-data; boundary=' + boundary)
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.loads(r.read().decode())
    if d.get('code') != 0:
        raise RuntimeError('upload failed: %s' % str(d)[:300])
    return d['data']['fileName']


def run_one(file_name, prompt, threshold):
    body = {
        'addMetadata': True,
        'nodeInfoList': [
            {'nodeId': '431', 'fieldName': 'image', 'fieldValue': file_name},
            {'nodeId': '434', 'fieldName': 'text', 'fieldValue': prompt},
            {'nodeId': '430', 'fieldName': 'individual_masks', 'fieldValue': True},
            {'nodeId': '430', 'fieldName': 'threshold', 'fieldValue': threshold},
        ],
        'instanceType': 'default',
        'usePersonalQueue': 'false',
    }
    d = http_json(RUN + a.workflow_id, body)
    tid = d.get('taskId')
    if not tid:
        raise RuntimeError('run failed: %s' % str(d)[:300])
    deadline = time.time() + a.timeout
    while time.time() < deadline:
        q = http_json(QUERY, {'taskId': tid})
        st = (q.get('status') or '').upper()
        if st == 'SUCCESS':
            return q.get('results') or []
        if st == 'FAILED':
            raise RuntimeError('task failed: %s' % str(q.get('errorMessage') or q)[:300])
        time.sleep(5)
    raise RuntimeError('task timeout %ss (%s)' % (a.timeout, tid))


def fetch_base_nodes():
    """拿工作流原图，复用 LoadImage(431)/CheckpointLoaderSimple(429) 的默认值。"""
    try:
        d = http_json(GET_JSON, {'apiKey': key, 'workflowId': a.workflow_id})
        prompt = d['data']['prompt']
        p = json.loads(prompt) if isinstance(prompt, str) else prompt
        return p.get('431') or {}, p.get('429') or {}
    except Exception:                                # noqa: BLE001
        return ({'class_type': 'LoadImage', 'inputs': {'image': ''}}, {})


def build_workflow(prompts, threshold, image_node, ckpt_node):
    """单次调用搞定多概念：每个概念一条 SAM3 链 + 实例蒙版输出。"""
    wf = {}
    wf['431'] = image_node or {'class_type': 'LoadImage', 'inputs': {'image': ''}}
    wf['429'] = ckpt_node or {'class_type': 'CheckpointLoaderSimple',
                              'inputs': {'ckpt_name': 'sam3.1_multiplex_fp16_.safetensors'}}
    wf['431'].setdefault('inputs', {})['image'] = ''
    for i, concept in enumerate(prompts):
        b = 3000 + i * 10
        wf[str(b)] = {'class_type': 'CR Text', 'inputs': {'text': concept}}
        wf[str(b + 1)] = {'class_type': 'RH_Translator',
                          'inputs': {'seed': 902, 'prompt': [str(b), 0], 'advanced_options': 'force_english'}}
        wf[str(b + 2)] = {'class_type': 'CLIPTextEncode',
                          'inputs': {'text': [str(b + 1), 0], 'clip': ['429', 1]}}
        wf[str(b + 3)] = {'class_type': 'SAM3_Detect',
                          'inputs': {'image': ['431', 0], 'conditioning': [str(b + 2), 0],
                                     'refine_iterations': 2, 'threshold': threshold,
                                     'model': ['429', 0], 'individual_masks': True}}
        wf[str(b + 4)] = {'class_type': 'Mask Invert', 'inputs': {'masks': [str(b + 3), 0]}}
        wf[str(b + 5)] = {'class_type': 'JoinImageWithAlpha',
                          'inputs': {'image': ['431', 0], 'alpha': [str(b + 4), 0]}}
        wf[str(b + 6)] = {'class_type': 'SaveImage',
                          'inputs': {'filename_prefix': 'sam3_%02d_%s' % (i, concept.split(':')[0]),
                                     'images': [str(b + 5), 0]}}
    return wf


def run_custom(file_name, prompts, threshold):
    image_node, ckpt_node = fetch_base_nodes()
    wf = build_workflow(prompts, threshold, image_node, ckpt_node)
    wf['431']['inputs']['image'] = file_name
    body = {'apiKey': key, 'workflowId': a.workflow_id,
            'workflow': json.dumps(wf, ensure_ascii=False),
            'nodeInfoList': [{'nodeId': '431', 'fieldName': 'image', 'fieldValue': file_name}],
            'addMetadata': True}
    d = http_json(CREATE, body)
    if d.get('code') not in (0, 200) or not (d.get('data') or {}).get('taskId'):
        raise RuntimeError('create failed: %s' % str(d)[:300])
    tid = d['data']['taskId']
    deadline = time.time() + a.timeout
    while time.time() < deadline:
        q = http_json(QUERY, {'taskId': tid})
        st = (q.get('status') or '').upper()
        if st == 'SUCCESS':
            return q.get('results') or [], wf
        if st == 'FAILED':
            raise RuntimeError('task failed: %s' % str(q.get('errorMessage') or q)[:300])
        time.sleep(5)
    raise RuntimeError('task timeout %ss (%s)' % (a.timeout, tid))


def collect(results, prompts):
    """按 SaveImage 节点分组，下载每张实例图，并集 alpha。"""
    from collections import defaultdict
    groups = defaultdict(list)
    for r in results:
        groups[str(r.get('nodeId'))].append(r.get('url'))
    union = None
    rows = []
    # 节点 id 顺序与 prompts 对应：3006+10i
    for i, concept in enumerate(prompts):
        nid = str(3006 + i * 10)
        urls = groups.get(nid) or []
        inst = 0
        for j, url in enumerate(urls):
            if not url:
                continue
            p = os.path.join(work, 'mask_%02d_%03d.png' % (i, j))
            if not os.path.exists(p):
                urllib.request.urlretrieve(url, p)
            alpha = np.asarray(Image.open(p).convert('RGBA'))[..., 3]
            if float((alpha > 128).mean()) > 0.9:    # 无检测时是整图
                continue
            inst += 1
            m = np.asarray(Image.fromarray((alpha > 128).astype(np.uint8) * 255).resize((S, S), Image.LANCZOS)) > 128
            union = m if union is None else (union | m)
        rows.append({'prompt': concept, 'instances': inst})
    return union, rows


src = Image.open(a.input).convert('RGB')
S = 1024
small = src.resize((S, S), Image.LANCZOS)
work = a.raw_dir or os.path.join(os.path.dirname(os.path.abspath(a.out)), '_rh_raw')
os.makedirs(work, exist_ok=True)
tmp_in = os.path.join(work, 'input.png')
small.save(tmp_in)

file_name = upload(tmp_in)
log('uploaded', file_name)

prompts = [p.strip() for p in a.prompts.split(',') if p.strip()]
union = None
log_rows = []
mode = 'custom'
try:
    results, _wf = run_custom(file_name, prompts, a.threshold)
    union, log_rows = collect(results, prompts)
    log('custom workflow:', log_rows)
except Exception as e:                               # noqa: BLE001
    # 自定义工作流被拒绝时退回「每个概念一次」的老路
    log('custom workflow failed -> per-prompt:', e)
    mode = 'per-prompt'
    log_rows = []
    for prompt in prompts:
        results = run_one(file_name, prompt, a.threshold)
        inst = 0
        inst = 0
        for i, r in enumerate([x for x in results if str(x.get('nodeId')) == '432']):
            url = r.get('url')
            if not url:
                continue
            p = os.path.join(work, 'mask_%02d.png' % i)
            if not os.path.exists(p):
                urllib.request.urlretrieve(url, p)
            alpha = np.asarray(Image.open(p).convert('RGBA'))[..., 3]
            cov = float((alpha > 128).mean())
            if cov > 0.9:                    # 没检测到任何实例时，432 是整图
                continue
            inst += 1
            m = np.asarray(Image.fromarray((alpha > 128).astype(np.uint8) * 255).resize((S, S), Image.LANCZOS)) > 128
            union = m if union is None else (union | m)
        log_rows.append({'prompt': prompt, 'instances': inst})
        log('prompt', prompt, 'instances', inst)
        log_rows.append({'prompt': prompt, 'instances': inst})

if union is None:
    print(json.dumps({'ok': False, 'error': 'no instance masks detected',
                      'prompts': log_rows}, ensure_ascii=True))
    sys.exit(4)

# 清理：去碎块 + 只填小洞
def drop_small(mask, min_area):
    H, W = mask.shape
    seen = np.zeros_like(mask)
    out = mask.copy()
    for y0, x0 in np.argwhere(mask):
        if seen[y0, x0]:
            continue
        st = [(y0, x0)]; seen[y0, x0] = True; pix = []
        while st:
            y, x = st.pop(); pix.append((y, x))
            for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                ny, nx = y + dy, x + dx
                if 0 <= ny < H and 0 <= nx < W and mask[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True; st.append((ny, nx))
        if len(pix) < min_area:
            for y, x in pix:
                out[y, x] = False
    return out

union = drop_small(union, max(8, int(a.min_area * S * S)))
bgm = ~union
seed = np.zeros_like(bgm)
seed[0, :] = bgm[0, :]; seed[-1, :] = bgm[-1, :]; seed[:, 0] = bgm[:, 0]; seed[:, -1] = bgm[:, -1]
seen = seed.copy()
dq = deque(map(tuple, np.argwhere(seed)))
while dq:
    y, x = dq.popleft()
    for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        ny, nx = y + dy, x + dx
        if 0 <= ny < S and 0 <= nx < S and bgm[ny, nx] and not seen[ny, nx]:
            seen[ny, nx] = True; dq.append((ny, nx))
holes = ~seen
hs = np.zeros_like(holes)
for y0, x0 in np.argwhere(holes):
    if hs[y0, x0]:
        continue
    st = [(y0, x0)]; hs[y0, x0] = True; pix = []
    while st:
        y, x = st.pop(); pix.append((y, x))
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ny, nx = y + dy, x + dx
            if 0 <= ny < S and 0 <= nx < S and holes[ny, nx] and not hs[ny, nx]:
                hs[ny, nx] = True; st.append((ny, nx))
    if len(pix) < 0.004 * S * S:
        for y, x in pix:
            union[y, x] = True

alpha = Image.fromarray((union.astype(np.uint8) * 255)).filter(ImageFilter.GaussianBlur(a.feather))
alpha = alpha.resize(src.size, Image.LANCZOS)
alpha.save(a.out)

print(json.dumps({
    'ok': True, 'out': a.out, 'size': list(src.size),
    'coverage': round(float((np.asarray(alpha) > 128).mean()), 4),
    'prompts': log_rows, 'mode': mode, 'seconds': round(time.time() - t0),
}, ensure_ascii=True))
