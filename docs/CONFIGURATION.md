# 配置说明

## 1. 密钥：`后端/config/secrets.json`

从模板复制：

```bash
cp 后端/config/secrets.example.json 后端/config/secrets.json
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `api_key` | 对话/视觉必需 | OpenAI 兼容接口的 key（默认 DeepSeek） |
| `image_api_key` | 可选 | 兜底生图（默认火山方舟）的 key |
| `seamless_api_key` | 图像算法必需 | APIMART（或兼容）图像 API key，用于描稿/接缝修复 |
| `runninghub_api_key` | 可选 | RunningHub 工作流（SAM3 抠图等） |
| `admin_password` | 可选 | 运营后台口令；为空时首次进入后台设置 |
| `trace_api_key` | 可选 | 描稿相关独立 key（未填则回落到 `seamless_api_key`） |

> `secrets.json` 已被 `.gitignore` 排除。不要把真实 key 写进任何其他文件。

## 2. 行为配置：`后端/config/settings.json`

结构（默认值可直接用，按需改）：

```jsonc
{
  "server": { "host": "127.0.0.1", "port": 8787 },
  "admin":  { "host": "127.0.0.1", "port": 8788 },

  "llm": {
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-flash",
    "temperature": 0.6,
    "max_tokens": 16000,
    "timeout": 90,
    "tools_enabled": true,
    "max_tool_rounds": 4
  },

  "vision": { "enabled": true, "model": "...", "timeout": 180 },

  "image": {
    "enabled": true,
    "provider": "volcengine",
    "base_url": "https://ark.cn-beijing.volces.com/api/v3",
    "model": "doubao-seedream-5-0-260128",
    "fallback_models": ["..."],
    "size": "2048x2048"
  },

  "patgen": {
    "dir": "../算法/花型生成",
    "profile": "fast",
    "variants": 4
  },

  "algo": {
    "dir": "../算法/描稿-四方连续-psd分层",
    "node": "../../.runtime/node/bin/node",
    "python": "..."
  }
}
```

几个要点：

- `patgen` 是纯算法生图；默认不需要网络；
- `image` 是纯算法失败/需要写实图时的兜底；
- `algo` 是描稿/四方连续/PSD 流水线，模型与成本在算法目录的 `config/` 里；
- 修改后重启后端生效（或调后台的「重载配置」）。

## 3. 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PATTERNFLOW_PORT` | `8787` | C 端端口 |
| `PATTERNFLOW_ADMIN_PORT` | `8788` | B 端端口 |
| `PATTERNFLOW_PYTHON` | 自动探测 | 启动脚本/桌面端使用的 Python |
| `PATTERNFLOW_OFFLINE` | 未设置 | 设为 `1` 时算法禁用外网（纯本地） |
| `PATTERNFLOW_LOG_LEVEL` | `info` | 日志级别 |
| `HTTP_PROXY` / `HTTPS_PROXY` | 系统代理 | 算法子进程代理；也可由系统代理自动探测 |
| `NO_PROXY` | 自动 | 代理可用时会自动加入 127.0.0.1/localhost |

### 代理行为

算法的 Node 请求默认不读系统代理，因此桥接层会：

1. 读取环境变量 + macOS 系统代理（`urllib.getproxies()`）；
2. 用 0.35s TCP 探测代理端口是否真的可用；
3. 可用 → 注入 `HTTP(S)_PROXY`，Node 追加 `--use-env-proxy`；
4. 不可用 → `NO_PROXY=*` 强制直连，避免死代理拖垮所有请求。

所以 VPN 用「系统代理」或 TUN 模式都不需要额外配置；如果代理端口变了，重启后端即可。

## 4. 算法自己的配置

| 文件 | 作用 |
| --- | --- |
| `算法/花型生成/.env.example` | 生图 provider / key 环境变量（纯算法模式可留空） |
| `算法/描稿-四方连续-psd分层/.env.example` | APIMART / RunningHub 配置 |
| `算法/自动打标签/README.md` | 本地模型路径与下载方式 |
| `算法/全网搜花型/config.json` | 采集配置（默认不提交，需自己建） |

## 5. 端口冲突

- C 端固定 8787；如果被占用，启动脚本会先探测 `/api/health`，确认是不是自己；
- B 端默认 8788；桌面端会自动顺延到 8789/8790/8791；
- 改端口：`PATTERNFLOW_PORT=9000 ./启动后端.sh`，后台同步改 `settings.json`。
