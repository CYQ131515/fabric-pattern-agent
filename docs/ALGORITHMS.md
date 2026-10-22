# 算法模块

三个算法都是**独立子进程**，可以单独启用、替换或关闭。
它们不 import 后端代码，只通过命令行参数 + JSON 输出 + 文件交换。

---

## 1. 花型生成：`算法/花型生成`

纯算法生图。输入需求规格（风格/元素/配色/场景/版式），输出可直接平铺的无缝单元。

- **技术栈**：Python 3.13、numpy、Pillow；本地 `.venv`，**不依赖网络**
- **入口**：`pattern_studio` CLI（由 `后端/core/patgen.py` 调用）
- **产物**：PNG 花版 + 平铺预览 + `meta.json`（尺寸、耗时、成本、seed）
- **优点**：免费、稳定、可控、可离线；适合快速出候选与批量打样
- **局限**：写实照片级质感不如生成模型；复杂构图需要更多规则

安装：

```bash
cd 算法/花型生成
./安装.command          # 创建 .venv（优先从已有 venv 复制，失败再 pip）
```

常用参数：

```bash
.venv/bin/python -m pattern_studio --help
# 关键项：--profile fast|quality  --variants 4  --review on|off
```

后端配置段：`settings.json → patgen`。

---

## 2. 描稿 / 四方连续 / 分层 PSD：`算法/描稿-四方连续-psd分层`

一条完整的「面料照片 → 可印刷花版」流水线，步骤可单独调用，也可由 `pipeline.mjs` 串起来。

### 步骤

| 步骤 | 作用 |
| --- | --- |
| `01-clean.mjs` | AI 清洗：去褶皱、阴影、透视，尽量保留花型 |
| `02-seam-ai.mjs` | AI 生成缝带，修复左右/上下接缝 |
| `02-seam-deterministic.mjs` | 确定性接缝对齐（快、免费，兜底） |
| `02-seam-regenerate.mjs` | 生成式重排（结构复杂时更自然） |
| `02-seam-quilt.py` | 图割/拼贴式修复（局部内容） |
| `03-verify-seam.mjs` | 接缝逐像素校验，产出报告 |
| `03-judge.mjs` | 自动判断该用哪条路线 / 是否通过 |
| `04-layers-color.py` | 按颜色分色，生成 300dpi 分层 |
| `05-flat-psd.py` / `05-layers-elements.py` | 合成 PSD（根层 + Colors 分组） |
| `06/07-*.py` | 背景清理、局部修复、PSD 校验、wrap blend |
| `09-seam-postfix.py` | 最后一道接缝修补 |

### 运行时

- Node 24（脚本与 sharp）
- Python 3.13 + uv（PSD / 图像处理）
- 可选：APIMART 图像 API（01/02/06 步骤）、RunningHub（SAM3 抠图）
- 仓库不包含 `.runtime/`、模型权重与大图；按目录内 README 安装

### 配置

复制 `算法/描稿-四方连续-psd分层/.env.example` → `.env`：

```bash
APIMART_BASE=https://api.apimart.ai
APIMART_KEY=...
RUNNINGHUB_KEY=...
```

后端会通过 `core/algo.py` 注入代理、超时和错误摘要；算法自己的 stdout 必须是 JSON。

### 关键设计

- **先检测再生成**：先判断图片是否适合做四方连续（内容够不够、接缝能不能对齐），不适合就直接告诉用户，不浪费 API；
- **多路线回退**：AI 缝带 → 确定性对齐 → 生成式重排 → 图割，任何一步失败会换下一条，而不是直接报错；
- **审查是硬门槛**：接缝审查不过就不交付，宁可失败也不给一张「远看还行」的图；
- **成本可见**：每次调用记录模型、耗时、费用到 `meta.json`。

后端配置段：`settings.json → algo`。

---

## 3. 自动打标签：`算法/自动打标签`

给花型库自动打标签（风格/元素/颜色/场景），用于入库与检索。

- 技术栈：Python + 本地小模型（Qwen 系列，MLX / transformers 取决于机器）
- **模型权重不随仓库分发**，见该目录 `README.md` 的下载说明
- 默认在「入库」时作为闸门：标签缺失时不允许静默入库

后端调用：`core/atag.py` / 技能 `auto_tag`。

---

## 4. 旧版算法：`算法/四方连续` / `算法/psd分层`

早期实现，保留用于兼容旧数据与对照测试：

- `算法/四方连续`：sifang 引擎
- `算法/psd分层`：rhpsd 引擎

新项目建议直接用上面的 `描稿-四方连续-psd分层` 流水线。

---

## 5. 可选：`算法/全网搜花型`

用 Playwright 做花型采风/检索，默认不参与主流程，且需要自备站点配置（`config.json` 不入库）。

---

## 6. 失败模式速查

| 现象 | 大概率原因 | 处理 |
| --- | --- | --- |
| `连不上生图网关` | 代理/VPN 不通 | 打开代理；后端会自动探测并注入 |
| `AI 清洗失败了` | 图像 API 拒绝/额度 | 看 `~/Library/Logs/PatternFlow/` 或算法 stderr |
| 接缝审查不过 | 原图缺少可对齐内容 / 花型被切断 | 换「生成式重排」或让用户换图 |
| PSD 层数不对 | 分色阈值 / 图像颜色过多 | 看 `04-layers-color.py` 报告，调阈值 |
| 算法找不到 node/python | 运行时未装 | 跑对应目录的 `安装.command`，或设 `PATTERNFLOW_PYTHON` / `.runtime` |

更多排查见 [DEVELOPMENT.md](DEVELOPMENT.md)。
