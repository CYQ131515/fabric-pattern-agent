# 架构说明

## 1. 进程模型

PatternFlow 是「两个独立服务 + 若干算法子进程」：

```
浏览器 ── HTTP ──> 后端/server.py :8787
                     │
                     ├── /api/chat       对话（LLM + 工具调用）
                     ├── /api/action     执行技能（扣费前必须 confirm）
                     ├── /api/state      前端状态同步
                     ├── /api/file/*     本地生成/上传文件
                     └── 静态页面          前端/*.html
                     │
                     └── subprocess ──> 算法/…（Node / Python）

运营后台 ── HTTP ──> 后台/admin_server.py :8788
                     ├── 独立口令登录
                     ├── 模型与密钥
                     ├── 花型库 / 会话 / 分析
                     └── 独立的静态页面
```

为什么要拆两个进程：

- 后台重启/崩溃不影响 C 端用户对话；
- 后台监听 127.0.0.1，且接口有口令，不暴露在公网；
- 两边可以分别升级、分别排查。

桌面端（`桌面端/`）是一个 WKWebView 壳，负责：

1. 检查 `127.0.0.1:8787` 上是不是 PatternFlow（`/api/health` 返回 200 且带 `ok`）；
2. 没起就 spawn `后端/server.py`，并轮询健康检查；
3. 菜单按需启动 `后台/admin_server.py`（8788 被占用时顺延 8789/8790…）；
4. 退出时只终止自己启动的进程。

## 2. 对话与技能

LLM 不直接操作文件或花钱。流程是：

```
用户消息
  └─ /api/chat
       ├─ 组装 persona（客服助理/花型设计师/织印师）+ 历史 + 需求卡
       ├─ LLM 选择 skill，填写参数（tool call）
       ├─ skills/<name>/handler.py: propose(...)
       │     └─ 返回 need_confirm + 卡片数据（价格、张数、可选方案）
       └─ 前端渲染确认卡片
             └─ 用户点「确认」→ /api/action
                   └─ handler.execute(...) → 真正扣费/调用算法/写文件
```

关键约定：

- **propose 不产生副作用**：只做检查、报价、给方案；
- **execute 必须可重复调用**：前端重试/幂等要由 handler 保证；
- **confirm 闸门**：所有带 `confirm: true` 的技能，未确认前绝不执行；
- **错误要人话**：`core/algo.py::error_summary()` 把 Node/Python 堆栈折叠成一句可行动的话。

技能目录结构：

```
后端/skills/<name>/
├── skill.json   # 描述、参数 schema、价格、是否 confirm
└── handler.py   # propose / execute
```

新增技能不需要改 server.py，放进去即可被注册。详见 [DEVELOPMENT.md](DEVELOPMENT.md)。

## 3. 算法桥（core/algo.py）

所有算法都是子进程，桥接层负责：

| 职责 | 实现 |
| --- | --- |
| 找运行时 | 项目内 `.runtime` 优先，其次系统 Node / Python |
| 环境隔离 | 传递最小环境变量；`PYTHONPATH` / `PATH` 按算法补 |
| 代理 | 探测可用系统代理；可用则注入 `HTTP(S)_PROXY`，Node 加 `--use-env-proxy`；不可用则 `NO_PROXY=*` 强制直连 |
| 超时 | 分步骤超时，失败快速返回，不让用户干等 |
| 错误摘要 | 从 stdout/stderr 提取最后一条可读错误，去掉 `Error:` 前缀 |
| 产物记录 | 读算法产出的 `meta.json`（模型、耗时、成本、尺寸） |

算法进程的 stdout 是给机器读的 JSON，stderr 给日志；不要往 stdout 打调试信息。

## 4. 数据与存储

```
数据/
├── library/       # 花型库（orig 原图 + thumb 缩略图 + index.json）
├── generated/     # 生成结果
├── uploads/       # 用户上传原图
├── sessions/      # 会话记录
├── tmp/           # 中间文件（可清理）
└── psd-cache/     # 分层 PSD 结果缓存（很大，可清）
```

原则：

- `数据/` 不入库（`.gitignore`），用户的图不会离开本机；
- `后端/data/` 放**演示数据**（程序生成的矢量花型、趋势、词表），可以安全提交；
- 算法产物先写临时目录，成功后再原子移动到目标位置，避免半成品。

## 5. 前端

刻意使用单文件 HTML：

- 无构建步骤、无 npm 依赖，服务器直接托管；
- 离线可用，方便发给非技术用户；
- 代价是文件较大（当前 ~240KB，含内嵌图标和样式）。

`前端/…v0.7.html` 与 `前端新版/…v1.0.html` 是同一套代码的两个版本：
**改完 v0.7 后要把 `<style>` 与 `<script>` 整段同步到 v1.0**，只保留标题差异。
同步脚本示例见 [DEVELOPMENT.md](DEVELOPMENT.md)。

## 6. 安全边界

- 服务只绑定 `127.0.0.1`，默认不对外网开放；
- 后台有独立口令，接口除 health/login/session 外都要求会话；
- 密钥只存在 `后端/config/secrets.json`，不进入代码、不返回给前端；
- 上传文件按扩展名/大小限制，路径做了目录穿越防护；
- 桌面端使用 ATS 本地例外，只放行 127.0.0.1 / localhost。

## 7. 可扩展点

| 想改 | 位置 |
| --- | --- |
| 换对话模型 | `后端/config/settings.json` → `llm` |
| 换图像模型 | `settings.json` → `image` / `seamless` |
| 新增技能 | `后端/skills/<name>/` |
| 换生图算法 | `后端/core/patgen.py` + `算法/花型生成/` |
| 换描稿/PSD 流水线 | `后端/core/algo.py` 的引擎路径 + `算法/描稿-四方连续-psd分层/` |
| 改 UI 设计 | `前端` 的 `<style>` token + 组件样式 |
| 改桌面端行为 | `桌面端/Sources/main.swift` |
