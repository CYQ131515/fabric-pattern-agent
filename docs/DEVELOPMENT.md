# 开发指南

## 1. 前端：两个页面必须同步

仓库里有两份前端：

```
前端/PatternFlow花型智造-交互原型v0.7.html
前端新版/PatternFlow花型智造-新版v1.0.html
```

它们是同一套代码的两个入口，**只有 `<title>` 不同**。改完 v0.7 后同步：

```bash
python3 - <<'PY'
import re
src = open('前端/PatternFlow花型智造-交互原型v0.7.html', encoding='utf-8').read()
dst = open('前端新版/PatternFlow花型智造-新版v1.0.html', encoding='utf-8').read()
style = re.search(r'<style>.*?</style>', src, re.S).group(0)
script = re.search(r'<script>.*?</script>', src, re.S).group(0)
dst = re.sub(r'<style>.*?</style>', lambda m: style, dst, count=1, flags=re.S)
dst = re.sub(r'<script>.*?</script>', lambda m: script, dst, count=1, flags=re.S)
open('前端新版/PatternFlow花型智造-新版v1.0.html', 'w', encoding='utf-8').write(dst)
PY
```

校验两页样式/脚本一致：

```bash
python3 - <<'PY'
import re, hashlib
for p in ['前端/PatternFlow花型智造-交互原型v0.7.html',
          '前端新版/PatternFlow花型智造-新版v1.0.html']:
    s = open(p, encoding='utf-8').read()
    st = re.search(r'<style>.*?</style>', s, re.S).group(0)
    sc = re.search(r'<script>.*?</script>', s, re.S).group(0)
    print(p, hashlib.sha256(st.encode()).hexdigest()[:12],
          hashlib.sha256(sc.encode()).hexdigest()[:12])
PY
```

### UI 约定

- 设计 token 在 `<style>` 顶部的 `:root`，不要散落魔法色值；
- 字体：标题宋体（Songti SC），正文苹方（PingFang SC）；
- 控件圆角 4px、发丝线边框、极少阴影；
- 所有文案用中文短句，先给结论再给细节；
- 改 UI 后用 Playwright 截图对比（见 `桌面端/README.md` 的 snapshot 模式）。

## 2. 后端：新增一个技能

```
后端/skills/my_skill/
├── skill.json
└── handler.py
```

`skill.json` 最小示例：

```json
{
  "name": "my_skill",
  "title": "我的技能",
  "description": "一句话说明什么时候用",
  "confirm": true,
  "cost": 30,
  "parameters": {
    "type": "object",
    "properties": { "prompt": { "type": "string" } },
    "required": ["prompt"]
  }
}
```

`handler.py` 需要：

```python
def propose(ctx, args):
    # 只做检查/报价，不产生副作用
    return {"ok": True, "need_confirm": True,
            "summary": "一句话结论", "hint": "给模型的回复提示"}

def execute(ctx, args):
    # 真正干活；返回 ok/error/文件列表
    return {"ok": True, "files": []}
```

约定：

- `propose` 不扣费、不写文件、不调用贵接口；
- `execute` 幂等或至少可安全重试；
- 错误信息要用户能看懂；
- 有外部依赖的，先检查可用性再报价。

## 3. 测试

```bash
# 后端
cd 后端
for t in 测试/test_algo_bridge.py 测试/test_trace_skill.py 测试/test_psd_cache.py; do python3 "$t"; done

# 前端（Node + jsdom）
cd ..
node 后端/测试/test_v07_client.mjs
PF_CLIENT_HTML=前端新版/PatternFlow花型智造-新版v1.0.html node 后端/测试/test_v07_client.mjs
```

要求：

- 改前端交互 → 两页都要过；
- 改算法桥/技能 → 对应 Python 测试先红后绿；
- 改桌面端 → `./构建.command` 必须无 error（warning 可接受）。

## 4. 调试

### 后端

```bash
python3 后端/server.py --port 8787
# 打开 http://127.0.0.1:8787/，接口日志在终端
```

### 算法

```bash
cd 算法/描稿-四方连续-psd分层
node steps/01-clean.mjs --input x.png --out y.png --model ...
# stderr 是真错误，stdout 是 JSON 结果
```

### 桌面端

```bash
cd 桌面端
./构建.command
./PatternFlow.app/Contents/MacOS/PatternFlow --snapshot=/tmp/shot.png
```

日志：`~/Library/Logs/PatternFlow/backend-8787.log`、`admin-*.log`。

## 5. 提交前检查

- [ ] 没有把 `后端/config/secrets.json`、`.env`、`数据/`、模型权重加进 git；
- [ ] 两个前端页面的 style/script 哈希一致；
- [ ] 新增文案没有泄露内部实现（路线名、feature 数字、分层名）；
- [ ] 截图里没有真实用户面料照片；
- [ ] `git diff` 里没有 `/Users/<name>` 这类个人路径。
