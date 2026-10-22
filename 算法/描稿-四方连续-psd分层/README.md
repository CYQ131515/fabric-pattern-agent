# 描稿 · 四方连续 · PSD 分层（服装面料花型算法包）

把一张面料照片/截图（含褶皱、水印、低清）变成**可印刷的 300dpi 分层 PSD**：
**AI 清洗保花型 → 四方连续无缝 → 色层分色 → 300dpi 分层 PSD**（语义分层已封存，见 `archive/semantic-layers/`）。

> 单张图全流程约 **1.5–2 分钟**、**$0.02–0.05**（API 成本），本地处理零成本。

---

## 1. 目录结构

```
描稿-四方连续-psd分层/
├── pipeline.mjs                 # 一键流程（推荐入口）
├── steps/
│   ├── 01-clean.mjs             # ① AI 清洗（gpt-image-2.5）
│   ├── 02-seam-ai.mjs           # ② 无缝方案 A：AI 补缝（渐变/平涂/单色图）
│   ├── 02-seam-deterministic.mjs
│   ├── 02-seam-tile-multi.mjs   # 方案D：多样本重生成 + wrap 硬筛# ② 无缝方案 B：确定性补缝（繁复纹理）
│   ├── 02-seam-regenerate.mjs  # ② 无缝方案 C：原生 seamless 重生成（无周期图兜底）
│   ├── 03-verify-seam.py        # ③ 整图接缝审查（离群直线：贯穿式断裂检测）
│   ├── 03-judge.mjs             # ③ 分色方式判断（最便宜 VLM + 特征）
│   ├── 04-layers-color.py       # ④ 色层分色 PSD（背景根层 + Colors 分组）
│   └── 05-flat-psd.py           # 可选：服务商同款两层 PSD
├── lib/
│   ├── env.mjs                  # .env / API 调用 / 异步任务轮询
│   └── sharp.mjs                # sharp 解析（支持 SHARP_PATH）
├── config/
├── knowledge/                   # 经验库（案例 + 教训 + 策略规则；算法自我迭代用）
│   ├── README.md                # 记录规范
│   ├── lessons.md               # 教训清单 L01..
│   ├── rules.json               # 机器可读策略
│   ├── INDEX.md                 # 案例索引
│   └── cases/<id>/              # case.json + notes.md + 缩略图
├── lib/strategy.mjs             # 经验驱动派发（读 rules.json → 候选路径顺序）
├── lib/planner.mjs              # LLM planner：读经验库+方法说明 → 自选顺序/参数/拒单
├── knowledge/regression.json    # 回归集（每张图必须符合 expect）
├── scripts/record-case.mjs      # 手动录入案例
├── scripts/regress.mjs          # 回归验证（审查器 + 派发护栏）
│   ├── 06-repair-region.py       # 区域框选修复（Poisson 融合；修单条缝线/局部瑕疵）
├── scripts/feedback.mjs         # 录入人工/用户评判（accept / reject）
├── scripts/learn.mjs            # 从反馈学习：特征簇×路径通过率 → learned.json
├── feedback/                    # 反馈数据集（deliveries.jsonl / records.jsonl）
├── archive/semantic-layers/     # 已封存：语义分层脚本与规则（不参与一键流程）
│   ├── 04-layers-semantic.py
│   ├── 04-layers-semantic-photo.py
│   └── semantic-rules.md
├── package.json / requirements.txt
└── .env.example                 # API 配置模板
```

## 2. 安装

```bash
cd 描稿-四方连续-psd分层
npm install                                        # sharp
pip install -r requirements.txt                    # psd-tools==1.17.4 / pillow / numpy
cp .env.example .env && vim .env                   # 填 APIMART_BASE / APIMART_KEY
```

无系统 python 依赖时可用 `uv`（脚本会自动检测）：
`uv run --with psd-tools==1.17.4 --with pillow --with numpy python ...`

## 3. 用法

### 一键（推荐）

```bash
node pipeline.mjs --input 面料.jpg --outdir work --height 9382
# 常用参数
#   --seam auto|ai|deterministic   无缝方式（默认 auto）
#   --layering color               分层方式（语义分层已封存，默认只出色层分色）
#   --model-clean gpt-image-2.5-sunburst
#   --model-seam  gpt-image-2.5-ext
#   --k 8                          色层数量
```

输出（`work/`）：`01-base.png`、`02-seamless.png`、两版 PSD、`run-report.json`（每步耗时/花费/决策）。

### 分步

```bash
node steps/01-clean.mjs --input 面料.jpg --out work/base.png --model gpt-image-2.5-sunburst --size 9:16
node steps/02-seam-ai.mjs --base work/base.png --out work/seamless.png --model gpt-image-2.5-ext --size 9:16
# 或 node steps/02-seam-deterministic.mjs --base work/base.png --out work/seamless.png
node steps/03-judge.mjs work/seamless.png
python steps/04-layers-color.py   work/seamless.png work/color.psd --height 9382 --k 8
```

---

## 4. 算法详解

### ① 清洗（保花型，不重绘）

- 模型：**gpt-image-2.5-sunburst**（布局保真最好）/ **gpt-image-2.5-ext**（最便宜，颜色保真更高）。
- 实测保真度（vs 原图，NCC）：2.5 系列 **布局 0.88–0.99、颜色 0.90–0.99**；早期 gpt-image-1.5 仅 **0.03**（会重画花型，已弃用）。
- 提示词要点：
  1. 明确"同一批花、同位置、同大小、同颜色"；
  2. 只允许去褶皱/阴影/背景/水印，保留印花纹理；
  3. 列出花型解剖细节（花瓣层数、花心、叶形、底色、纹理），越具体越保真。
- 水印：一次生成可能残留，**追加一次 2.5-ext"仅删除水印、其余不变"**（约 $0.0085）。
- 坑：`2.5-ext` 的 size 必须是比例（`1:1`、`2:3`、`9:16`…）不能用 `1024x1024`；`n=2` 偶发 `task_failed`，重试即可。

### ② 四方连续（多路径 + 整图审查 + 失败回退）

| 方案 | 原理 | 适用 | 成本 |
| --- | --- | --- | --- |
| **AI 补缝** `02-seam-ai.mjs` | 图平移到中线 → 原图边缘差移到中心十字 → 十字带作 mask 让 2.5 模型只重画缝带 | 渐变、平涂、单色、规整几何 | $0.009–0.03，约 40–60s |
| **确定性补缝** `02-seam-deterministic.mjs` | 平移 + 逐列/逐行自适应平移补十字带 + Laplacian 多频段融合 | 繁复纹理（人字纹/点阵/霓虹/花丛） | $0，<1s |
| **原生重生成** `02-seam-regenerate.mjs` | 带参考图让 2.5 直接生成"原生无缝 tile"（左侧提示词强调 edge 连续） | **无周期/一次性构图**，前两种补缝都会留痕时 | $0.009–0.04，40–90s |

**流程（2026-10-19 起）**：

```
候选A 快速路径：跳过 AI 清洗 + 确定性补缝
   └─ 整图审查 03-verify-seam.py
        ├─ 通过 → 出色层 PSD
        └─ 不通过 → 删除候选A全部产物 → 候选B
候选B：AI 清洗 + AI 补缝（band 140）
   └─ 审查 → 通过出 PSD；不通过删除 → 候选C
候选C：AI 清洗 + 原生 seamless 重生成
   └─ 审查 → 通过出 PSD；仍不通过 → 中止，不产出任何文件
```

**审查规则（关键）**：平移补缝会把原图边缘移到图片内部，所以**不能只看外缘**。
`03-verify-seam.py` 扫描整幅图，找"贯穿式离群直线"：某列/行的跨线梯度均值显著离群（z≥8 且 ≥1.8×中位），且高梯度跨度 ≥18% 图幅 → 判为接缝。
实测：坏图在 x≈0.499、y≈0.427 处命中；原图/已知好图均通过。

自动判据（`--seam auto`）：平坦插画（`lowGradFrac ≥ 0.35`）先试快速路径，否则直接走 AI 清洗+补缝；无论哪条路径，**都必须通过整图审查**才能出 PSD。
**方案 C 是兜底**：当前两种都检查出硬边/贴图块，且图像没有可复制的重复单元时使用。
**稀疏线稿（涂鸦/线框插画）优先直接走 C**：AI 补缝的缝带边界一定会切断少数描边，
而 C 是整张重生成、没有带边界；代价是排布重排（同风格）。`--seam regenerate` 可直接指定。代价是花型重排（同风格、非逐朵复刻），换来数学无缝；实测 wrap/相邻可到 **1.04 / 1.13**，1:1 看不到界线。
> 反例：平滑渐变图用确定性补缝会出现**矩形色块**（量化后尤其明显），判定不了就人工指定 `--seam ai`。

验收指标：`wrap/相邻像素差` ≈ 1.0（0.9–1.2 合格）；`wrap/far < 0.5`。

**AI 补缝关键参数（踩坑总结）**：
- `--model` 默认 `gpt-image-2.5-sunburst`（细节/边界最干净）；预算紧可用 `gpt-image-2.5-ext`，但要更窄的 band。
- `--band` 默认 **140**（经验最优 120–160）。**不要超过 200**：mask 太宽时模型会把邻近纹理整块复制进缝带，出现“贴图块 + 硬直线边”。
- **不要对 AI 补缝结果再做 Laplacian/Poisson 融合**：AI 输出与原图存在微小几何错位，融合会产生条纹/光晕。正确做法是只用 AI 结果，直接进分层。
- 验收：放大到 100% 沿缝带边界（距中心 ±band/2）检查有无硬边/复制块；2×2 平铺检查是否出现直线。
**不要用镜像**：镜像虽能保证 wrap=0，但会产生明显对称（成对花），不符合原图随机排布。

### ③ 分色方式判断（最便宜可靠组合）

- 模型：**gemini-2.5-flash-lite** @512px，零温度，**2 次自洽**（不一致第 3 次多数表决；失败走特征规则兜底）。
- 关键：把量化特征写进 prompt —— `satMean`（平均饱和度）、`hueConc`（色相集中度）、`edgeMean`（边缘强度）、`lowGradFrac`（平坦区占比）。
- 实测成本 **≈$0.0004 / 3 图**（`claude-sonnet-4-6`/`gpt-4o` 约 $0.016，`gpt-4o-mini` 图像 token 计费高达 $0.023）。
- 输出：`semantic` 或 `color` + confidence，写进 `run-report.json`。

判据规则（模型总结）：
1. 同色系 + 大面积渐变/晕染 → **色层分色**；
2. 多花种、色相差异大、轮廓清晰、需按对象控墨 → **语义分层**；
3. 水彩/低饱和羽化 → 色层；
4. 荧光高饱和撞色 / 图文点阵强对比 → 语义；
5. 单色渐变/矢量平涂 → 色层（或 mono 语义，见下）。

### ④ 色层分色 PSD

`04-layers-color.py`：1024 小图 k-means 取调色板 → 全尺寸逐块分配最近色 → 每色一层。

**PSD 结构（2026-10 起）**：

```
[根]
├── Background            ← 背景层，单独在根层
└── Colors (分组)          ← 所有颜色分层放进一个分组
    ├── Color01 #xxxxxx
    ├── Color02 #xxxxxx
    └── ...
```

参数：`--k 8`（层数）、`--group-name Colors`（分组名）、`--height 9382`。

> 语义分层（按对象分组）已按需求**封存**：脚本与规则移到 `archive/semantic-layers/`，
> 一键流程不再输出。需要恢复时把脚本移回 `steps/` 并在 pipeline 中恢复分支即可。
> 封存版含"纯色层"和"保真层（每层原始像素，合成=原图）"两种实现。

### ⑤ PSD 规格（输出统一）

| 项 | 值 |
| --- | --- |
| 色彩模式 | RGB 8 bit / 3 通道 |
| 分辨率 | 300 PPI |
| 尺寸 | 高度默认 **9382 px（≈79.4cm）**，宽度按原图比例；即"高 79.4cm 的印花单元" |
| 图层 | 根层 `Background` + 分组 `Colors`（每色一层，**默认保留原始像素**+蒙版，合成=原图） |
| 写入器 | `psd-tools 1.17.4` + RLE 压缩 |
| 命名 | 英文 ASCII（psd-tools 的 macroman 编码不接受中文层名） |

面积换算：`px / 300 * 2.54 = cm`。例如 6982px ≈ 59.1cm，9382px ≈ 79.4cm。

---

## 5. 成本 / 耗时实测（6 张不同风格图）

| 步骤 | 典型耗时 | 典型花费 |
| --- | --- | --- |
| ① AI 清洗（2.5-sunburst） | 30–60s | $0.013–0.043 |
| ② 无缝（AI 补缝） | 50–90s | $0.0085 |
| ② 无缝（确定性） | <1s | $0 |
| ③ 分色判断（flash-lite×2） | 15–20s | ~$0.0004 |
| ④ 语义 PSD（全尺寸） | 10–20s | $0 |
| ④ 色层 PSD（全尺寸） | 25–35s | $0 |
| **合计** | **≈1.5–2.5 分钟** | **≈$0.02–0.05** |

注：全尺寸 PSD（79cm 高）单份约 **150–220MB**，生成时内存峰值 1–2GB。

## 5.4 反馈驱动学习（人工评判 = 奖励信号）

```
pipeline 交付 → feedback/deliveries.jsonl（待评判）
     ↓ 用户/上线用户：accept（通过）或 reject（打回+原因/截图）
node scripts/feedback.mjs --delivery latest --verdict reject --reason "..." --tags ...
     ↓ reject 自动建 case + 进回归集（坏图必须被打回）
node scripts/learn.mjs
     ↓ 统计「特征簇 × 路径」通过率 → knowledge/learned.json
lib/strategy.mjs 读 learned.json → 下次同类图优先走历史成功率高的路径
     ↓ 若该簇所有路径都失败过 → 优先尝试"没试过的路径"，避免重复踩坑
```

- 冷启动：每类图至少 2 条反馈后才生效（`--min-samples` 可调）；不足时用 `rules.json` 先验。
- 每次 reject 都是"负样本"：同时进入案例库、回归集、学习统计三处。
- 重复错误会持续拉低该簇该路径得分，派发会自动绕开。

## 5.5 Planner（算法自己决定方案）

- `pipeline.mjs` 默认启用 planner：把图像特征、该特征簇历史反馈（通过/打回/问题标签）、
  教训清单（L01–L12）、可用方法说明喂给 LLM，由它输出候选顺序/参数/是否拒单；
- 未试过的路径优先，已失败路径降权；判定无解时**直接拒单**（不产出坏件）；
- 每次决策写入 `knowledge/plans.jsonl`；`--no-planning` 可回退纯规则派发；
- `--dry-run`：只跑特征+planner，不花钱，用来预演决策。

## 5.6 经验库与自我迭代（算法即小 agent）

- `pipeline.mjs` 的候选被审查打回时，会**自动**把「审查证据 + 缩略图」写入 `knowledge/cases/auto-*`，然后删除全尺寸产物；
- 用户指出问题 / 修复完成 → 用 `node scripts/record-case.mjs ...` 录入并更新状态；
- 可泛化的结论写入 `knowledge/lessons.md` 与 `knowledge/rules.json`，下次运行优先读取；
- 规则闭环：**发现/打回 → 案例(open) → 定位 → 修复 → 回归 → fixed → 规则升级**。
- 当前最高优先级未解决问题：`case-20261019-04`（原生重生成可能产生矩形 overlay）。
- **回归护栏**：`node scripts/regress.mjs` —— 案例库里的坏图必须被审查器打回、好图必须放行；
  任何规则/阈值改动都要先跑回归（当前结果：3 绿 1 红，红即 case-04 漏检）。
- **派发**：`lib/strategy.mjs` 读 `rules.json`，按特征（lowGradFrac / hueConc / satMean / edgeMean）
  给出候选路径顺序，pipeline 已接入；规则可人工累积，后续可按案例统计自动调权。

## 6. 质量控制清单

1. `run-report.json` 里 `wrapXvsAdj / wrapYvsAdj` 在 0.9–1.2 之间；
2. 2×2 平铺目检无可见十字/矩形/重复；
3. 与原图对比：花型位置、数量、颜色不能明显变化（AI 清洗允许轻度清理，不允许重绘）；
4. 分层 PSD 合成后与无缝图一致（合成预览检查）；
5. 放大 100% 看边缘：语义层硬边明显时，可后续接引导滤波细化（未内置）。

## 7. 常见问题（坑位）

1. 生成模型会"重画"：只有 2.5/2 代能保住花型，1.5 及更早不行。
2. 带褶皱的服装照片：2.5 会保留褶皱纹理，需要"干净版 + 原图"**双参考**提示词。
3. 去水印：必须单独一次生成明确"只删水印"，否则会残留。
4. mask_url 不是所有模型都严格遵守：2.5 仍会重绘全图，只是保真度高。
5. 确定性补缝在渐变/平涂图上会出矩形色块 → 换 AI 补缝。
6. 镜像无缝 = 明显对称，慎用。
7. PSD 层名中文会报 `macroman codec` 错误 → 用英文/色值。
8. 大图 PSD 写入内存约 1–2GB，需 8GB+ 内存。
9. 生成任务偶发 `task_failed`，重试一次即可。
10. `gpt-image-2.5-ext` 尺寸参数用比例；`gpt-image-2` 返回分辨率可能不是请求值，需按实际尺寸重算。
11. 分色判断先用便宜模型，但**必须做自洽 + 兜底**，否则单次调用会判错。
12. 语义分层阈值与图相关：换风格图先改 `--preset` 或调 `config/semantic-rules.md` 里的阈值。

## 8. 版本

- 2026-10 整理：6 张实测（人字纹/深蓝金斑/荧光霓虹/浅色水彩/黑驼图文/单色蓝渐变），流程已固化为本算法包。
- 2026-10-19 修订：AI 补缝默认改为 `2.5-sunburst + band 140`；移除"补缝后再融合"的错误做法（会产生条纹），并补充贴图块/硬边的排查清单。

## 工作区与会话（2026-10-20 迁移）

- 服装工作区：`$PROJECT`（今后所有沟通/产出都在这里）
- 会话迁移包：`../会话迁移_REA/`（完整对话记录 + 记忆总纲 + node/sharp 环境 + REA 清理脚本）
- 交付物：`deliverables/{psd,seamless,previews,inputs}`（pipeline 自动归档）
- 运行环境（迁移后的便携依赖）：
  ```bash
  export NODE="$PROJECT/会话迁移_REA/环境备份/tools/node/bin/node"
  export SHARP_PATH="$PROJECT/会话迁移_REA/环境备份/webdeps/node_modules/sharp"
  export PATH="$HOME/.local/bin:$PATH"
  ```

## 9. 后端集成（PatternFlow，2026-10-20 算法替换后）

产品侧（`后端/core/algo.py`）不再直接跑 `pipeline.mjs`，只复用它的两样东西：
**零成本体检**（`--dry-run`）和各个 step 脚本；色层 PSD 与描稿也各调对应 step。
这样产品每次调用**不会**往 `deliverables/` 里堆 600MB 的 PSD（那是研发交付目录）。

### 运行时：算法自带 `.runtime/`

`node v24.21.0 + sharp + uv` 已拷进本目录 `.runtime/`（约 198MB，已 gitignore），
不再依赖 `会话迁移_REA/环境备份`。手动跑算法时用：

```bash
cd "$PROJECT/算法/描稿-四方连续-psd分层"
export SHARP_PATH="$PWD/.runtime/node_modules/sharp"
export PATH="$PWD/.runtime/uv:$PATH"
./.runtime/node/bin/node pipeline.mjs --input 图.jpg --outdir work --height 9382
```

### 后端怎么调（三个功能各用哪一步）

| 产品功能 | 调用的步骤 | 说明 |
| --- | --- | --- |
| 描稿仿稿 | `steps/01-clean.mjs` ×2 | ①「想象平铺还原」`--prompt-file`（undo 褶皱/透视，R1）→ ② 默认忠实复刻 + 高清（2048²）；`--note "用户额外要求"` 只作用于第②步 |
| 四方连续 | `pipeline --dry-run` 体检 → `02-seam-*` 候选 → `03-verify-seam.py`（AI 候选再加 `03b-verify-repair.py`） | AI 候选 = `02-seam-ai` + **`09-seam-postfix.py`**（wrap 强制还原 + 带均值匹配，L39）；产品侧最多 3 条候选、`tile-multi` 默认关；底图长边先归一到 1024（算法调参尺度） |
| 分层 PSD | `steps/04-layers-color.py` | 色层 PSD：`Background` 根层 + `Colors` 分组，图层名 `Color01 #rrggbb`，合成逐像素等于源图；不需要任何 key |

### 环境变量优先级（后端注入 key）

`lib/env.mjs` 现在**进程环境优先、`.env` 只补缺**。后端会把后台配置的
APIMART 网关/key 以 `APIMART_BASE` / `APIMART_KEY` 注入子进程，所以后台改 key
立即生效；单独跑算法（没有这些变量）时行为不变，仍然读 `.env`。

### 保留的研发入口

`pipeline.mjs` 仍是研发用的一键流程（planner → 候选 → 审查回退 → 色层 PSD →
归档 `deliverables/`、写 `feedback/deliveries.jsonl`），产品侧不使用。

### 描稿路线 / PSD 分层模式（2026-10-21 新增）
- `trace.route`：`route1`（默认，两遍：平铺还原→清洗，抗扭曲）/ `route2`（单遍 2048 + 构图保完整 + 保留浮雕，更锐更快）。
- `psdlayer.mode`：`color`（默认，k-means 色层，适合印刷分色）/ `elements`（`steps/05-layers-elements.py`：整组元素软蒙版 + 干净底布，关背景后无毛边）。

### PSD 元素分层的前景来源（2026-10-21）
- `psdlayer.matte = flood`（默认）：本地区域生长，完整但含深色织纹；
- `psdlayer.matte = sam3`：调用 RunningHub SAM3.1 工作流做语义分割（干净但当前只覆盖约 1/3 元素，失败自动退回 flood）；
  需要 `config/secrets.json → runninghub_api_key`（或 `RUNNINGHUB_API_KEY`）。

### 接缝审查开关（2026-10-22）
- `settings.json → seamless.review`：默认 `true`（03/03b 审查 + A/B 分级）；设为 `false` 时跳过审查、跑通第一条候选即交付，
  结果标 `reviewed=false / grade=未审`，产品话术不得声称「通过审查/保证无缝」。
- 算法侧对应 `pipeline.mjs --no-review`（或 `NO_REVIEW=1`）。
