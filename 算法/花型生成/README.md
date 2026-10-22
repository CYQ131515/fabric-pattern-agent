# pattern_studio —— 关键词 → 九宫格元素 → 三版无缝花型图稿

一句话：**输入一句需求（如"国风水彩牡丹 疏朗"），产出 3 张可直接印花/做壁纸的无缝花型图稿**，
每张的元素数量、排布方式、元素大小都不同；元素互不重叠；疏密度可调。

```bash
cd 算法/花型生成
python3 -m pattern_studio run "国风水彩牡丹 疏朗"          # 默认走 apimart（.env 里配好 key）
# → out/国风水彩牡丹_疏朗/contact_sheet.png   （三版对比图）
# → out/.../cost.json                         （本次真实花费，精确到每次调用）
```

不配模型也能跑：`--provider mock` 用程序化代笔，验证"拆格 → 去背 → 排版 → 无缝 → 自检"整条链路。

---

## 0.5 节点架构 + 自主学习（2026-10-20 新增）

流水线是 **10 个节点**组成的图，每个节点只做一件事、有独立日志与耗时：

```
knowledge → spec → prompts → assets → background → layout → compose → verify → review → deliver
   ↑ 学习 skill 知识库                                                            ↑ 报告含节点轨迹
```

### 0 号 knowledge 节点：会自己读 skill

```bash
python3 -m pattern_studio learn        # 只学习：解析 skill 的 references/*.md → knowledge/kb.json
python3 -m pattern_studio run "青花缠枝莲 密"   # 自动学习（缺/过期才重学）+ 按知识工作
python3 -m pattern_studio run "..." --no-kb     # 关掉知识节点，纯参数驱动
```

- **学什么**：Markdown 表格/列表/小节 → 结构化知识（骨式、接版比例、开刀法、组合、选型、避坑、验收项、工具映射）。
  当前 1 篇手册 → 骨式 3 · 接版 5 · 开刀 3 · 组合 4 · 选型 4 · 避坑 4 · 验收 6 · 工具映射 14。
- **怎么用**：`kb.plan()` 按关键词+spec 判定 **骨式 → 接版 → 预设 / 旋转 / 层叠**，后续节点照此工作。
  例：`缠枝` → 连缀式 → brick/halfdrop/diagonal、不旋转；`底纹浮花` → 重叠式 → 自动开 `--overlap-mode layered`；
  `几何/团花` → 平接 → grid。
- **自动失效**：按文件 sha256 判断，skill 改了下次 run 自动重学（日志会写"skill 有更新 → 重新学习"）。
- **可追溯**：`report.md` 有「知识节点」小节——判定结论、命中的手册小节、避坑逐条对照、10 号手册验收项结果。
- **用户优先**：显式传 `--presets/--rotate-deg/--mirror-prob/--overlap-mode` 会覆盖知识节点的建议。

### 加节点 / 改节点

节点在 `pattern_studio/nodes.py`，每个是 `def node_xxx(ctx) -> ctx`；注册顺序就是执行顺序：

```python
def default_graph() -> Graph:
    return Graph([Node("knowledge", node_knowledge), ...])
```

节点之间只通过 `ctx` 通信（`ctx["spec"]`、`ctx["drafts"]`、`ctx["summary"]`…），加节点不需要改其他节点。

## 1. 需要什么

| 依赖 | 说明 |
|---|---|
| Python 3.10+ | 只用 `numpy` + `Pillow`（仓库自带 runtime 满足） |
| 生图/对话模型 | 默认用 **apimart** 网关（OpenAI 兼容 + 异步图像任务），一个 key 全搞定 |
| 备选后端 | 任意 OpenAI 兼容 `/v1/images/generations`、或本地 ComfyUI |

### 配置（把 key 放 `.env`，不要写进代码）

```bash
cp .env.example .env
# 编辑 .env：
#   PATTERN_STUDIO_APIMART_BASE=https://api.apimart.ai/v1
#   PATTERN_STUDIO_APIMART_KEY=sk-...
```

CLI 启动时自动读 `.env`（已存在的环境变量优先）。

---

## 2. 模型怎么选（实测，2026-10-20）

同一个九宫格提示词、同一套评估脚本（`eval_sheets.py`，用真实拆格器打分），单张成本来自接口返回的 `cost` 字段：

| 模型 | $/张 | 实测延迟 | 9 格可拆 | 格线 | 触边 | 结论 |
|---|---|---|---|---|---|---|
| **gpt-image-2** | **0.0085** | 20–100s | 9/9 | 无 | 0（强化 prompt 后） | **最便宜 + 画质最好**；慢，默认档用它 |
| z-image-turbo | 0.010 | 8s | 9/9 | 无 | 0 | 最快；偶尔一格画两个元素；2K 会出米色纸底，建议 1K |
| gemini-3.1-flash-image-preview | 0.015 | 20s | 9/9 | 无 | 0 | 白底最干净、格子最规矩 |
| mai-image-2.6-flash | 0.0159 | 22s | 9/9 | 无 | 0 | 水彩细节最足、元素占格最满 |
| seedream-5-0-flash | 0.0137 | 15s | 9/9 | **会画格线** | 0 | 抠图能去掉格线，但边缘不干净，不推荐 |
| seedream-5-0-lite | 0.028 | 49s | 9/9 | 无 | 0 | 米黄底 + 元素偏小，性价比差 |
| qwen-image-3.0 | 0.0206 | 8s | 9/9 | 无 | 0 | 快且干净 |
| wan2.7-image | 0.0216 | 27s | 9/9 | 无 | 1 | 粗线插画风 |
| flux-2-pro | 0.024 | 24s | 9/9 | 无 | 0 | 干净但偏淡 |
| gemini-nano-banana-2.1 | 0.0322 | 31s | 9/9 | 无 | 0 | 偏贵 |
| gemini-3.1-flash-lite-image | — | 超时(300s) | — | — | — | 不可用 |

结论：**gpt-image-2 在价格和效果上同时最优**（$0.0085 是全表最低），缺点只有慢；
要快就用 z-image-turbo。评估明细在 `out/model_eval/`（含各模型九宫格对比图与 `eval.json`）。

### 三个档位（`--profile`）

| 档位 | 九宫格模型 | 背景模型 | 需求解析 | 实测总价 | 实测耗时 |
|---|---|---|---|---|---|
| `cheapest` | gpt-image-2 | gpt-image-2 | deepseek-v4-flash | ~$0.017 | ~70s |
| **`balanced`（默认）** | gpt-image-2 | z-image-turbo | deepseek-v4-flash | **~$0.0185** | ~50s |
| `fast` | z-image-turbo | z-image-turbo | deepseek-v4-flash | ~$0.020 | ~25s |
| `best` | mai-image-2.6-flash | flux-2-pro | gpt-5-nano | ~$0.040/版 | ~95s |

> 一次生成的实际张数 = 九宫格张数（元素格数 ÷ 9，向上取整）+ 背景张数（默认 1）。
> 元素多的时候会多花一倍的九宫格钱，`cost.json` 里逐条记录，不会算错。

```bash
python3 -m pattern_studio run "青花缠枝 密" --profile fast
python3 -m pattern_studio run "敦煌壁画 菊花" --profile best
python3 -m pattern_studio run "国风牡丹" --model-sheet gemini-3.1-flash-image-preview --model-bg z-image-turbo
```

### 对话模型（需求解析）

`deepseek-v4-flash`（6.9s）/ `gpt-5-nano`（8.6s）/ `qwen3.8-flash`（10.8s）都能稳定输出结构化 JSON；
token 成本相对图像可忽略。**注意：模型只负责"判断该有哪些元素/什么配色"**，
英文生图描述由本地模板生成（避免小模型写英文漂移，也避免它把 describe 写成中文）。

---

## 3. 三张图稿是怎么来的（7 层）

```
关键词
 │ ① spec.py      词典分层 + 大模型精修（只让它选元素类型/配色）→ 结构化 Spec
 │ ② prompts.py   九宫格提示词（一格一物、白底、留白≥12%、禁文字/格线/边框/阴影）+ 无缝背景提示词
 │ ③ imagegen.py  apimart 异步任务（gpt-image-2 / z-image-turbo …）；也支持 mock / openai / comfy
 │ ④ sheet.py     九宫格切 9 格 → 透明底直接用 alpha；白底则估底色+软阈值+反预乘+连通域
 │ ⑤ layout.py    排版求解：预设骨架 + 环面泊松 + 非重叠碰撞 + 尺寸反推 + 密度均衡补位（×3 版）
 │ ⑥ compose.py   背景自动判无缝/镜像 + 按"背景花不花"自动压淡 + 元素 3×3 环绕合成
 └ ⑦ verify.py    自检：真实重叠 / 覆盖率 / "平铺 vs 无限画布"逐像素判定
```

**三版的差异**：排版预设不同（halfdrop / phyllotaxis / scatter…）+ 密度 0.92×/1.0×/1.12× +
旋转/镜像强度不同 → 元素数量、排列、大小都不同，但共用同一套元素与背景族。

---

## 3.5 成本与工时（实测 2026-10-20）

**机器成本（API）**

| 环节 | 单价 | 备注 |
|---|---|---|
| 元素九宫格 ×1（9 个元素） | **$0.0085**（gpt-image-2） | 也可 $0.01 z-image-turbo / $0.0159 mai |
| AI 背景 ×1（可选） | $0.01（z-image-turbo） | `--bg-mode flat` 则 $0 |
| 需求解析（LLM） | ~$0.0008（1,462 tokens） | `--spec-file` 则 $0 |
| 重排 / 换密度 / 换预设 / 换配色 | **$0** | `--reuse` |

**一套三版（3 张 tile）总账**

| 做法 | API | 机器耗时（含 LLM 6.3s + 本地 2s） |
|---|---|---|
| 复古类纯色底 + `--spec-file` | **$0.0085** ≈ ¥0.06 | ~59s |
| 标准 balanced（含 AI 背景） | $0.0185 ≈ ¥0.13 | ~59s |
| fast（全 z-image-turbo） | $0.020 ≈ ¥0.14 | ~24s |
| best（mai + flux-2-pro） | $0.0565 ≈ ¥0.41 | ~100s |
| 追加一版（`--reuse`） | **$0** | 0.5s（1024）/ 4.7s（3543px） |

摊到单张 tile：**$0.0028/张**；追加变体 **$0/张**。
本地单版耗时：1024px 0.54s / 2048px 1.61s / 3543px（30cm@300dpi）4.65s。

**人工工时（才是主要成本）**：新风格首次 20–40 min；有配方再做 3–8 min；批量变体 1–2 min/款。
按 ¥100/h 折算：新风格 ≈ ¥35–70、有配方 ≈ ¥5–13、变体 ≈ ¥2–3 —— **API 只占总成本 0.2%–2%**。

## 3.4 AI 审核（默认开启）

生成完不是直接交付，先过一道"眼光"：

```
几何实测（免费）→ 视觉初审（3 版拼图 1 次调用，挑最好版）→ 严格终审（五维打分+问题清单）
                                                              ↓ 不过
                                              问题码 → 改参数 0 成本重排 → 再审（1–2 轮）
```

| 审核维度 | 看什么 |
|---|---|
| 构图 composition | 主次、节奏、留白、元素会不会打架 |
| 美观 beauty | 配色、层次、高级感 |
| 面料适配 fabric | 缩到 30cm 可读、线宽、色数 |
| **重叠 overlap** | 小压大贴边=好；压花心/同尺寸深叠/同明度=坏（几何可算，见下） |
| 原创统一 originality | 元素是否同套、有无串风格 |

重叠"好不好看"是可量的（用真实掩膜算，不靠估）：
- `edge_ok` 小压大、只盖住对方一小部分 → 自然遮挡（好）
- `dead_center` 盖住了主花的视觉中心（花心被挡）→ 坏
- `mutual` 两个尺寸相近的元素互相盖住一大块 → 坏
- `muddy` 重叠两层平均明度差 <10 → 看不出前后
- **重叠面积占比** 9–15% 正常层叠｜>25% 偏多｜>42% 乱叠

这些由本地几何算出再喂给视觉模型——**先给数据，模型才不会只会夸**。
终审还会给"下一步"：**排布类问题**（cluster/void/overlap_*）→ 改参数 `--reuse` 重排（0 成本）；
**元素类问题**（repetition/style_drift/motif_*）→ 改排布没用，要补元素变体或改配色。

```bash
--review off|auto|strict        # 默认 auto：初审+终审+1 轮自动修
--review-triage-model gemini-3-flash-preview   # 初审（快、便宜）
--review-deep-model   gpt-5-mini               # 终审（挑剔）
--review-rounds 2 --review-no-autofix
```

产物：`review.json`（结构化，含自动修轨迹）、`review.md`（人看的表）、`report.md` 里的审核段。
判定：`overall≥8` 且无 high → 通过；`≥7.5` → 可用（有小建议）；`≥6` 或 1 个 high → 需修改；其余打回。
成本/时间：初审 ~$0.0007/5–13s，终审 ~$0.004/15–28s；一次完整审核约 **$0.004–0.012、+45–70s**。
实测（复古水彩）：终审指出"两个花心相撞"→ 自动修改贴边 → **重叠观感 6→9**，剩余问题转为元素重复度。

## 4. 输出目录

```
out/<标题>/
├── report.md                 三版指标表 + 元素清单 + 本次成本
├── contact_sheet.png         三版并排对比图（交付/评审看这张）
├── cost.json                 每次调用的模型与费用（来自接口返回的 cost 字段）
├── spec.json / prompts.json  需求与提示词（可手改后 --reuse 重跑）
├── elements/*.png            透明底元素（可直接复用）
├── sheets/sheet_0.png        九宫格原图（留档；也可作为 --sheets-dir 复用）
├── background/bg_0.png       AI 背景原图
├── drafts/
│   ├── draft_1_tile.png            单版 tile —— 本身无缝，可直接平铺
│   ├── draft_1_preview_2x2.png      2×2 平铺预览（查接缝）
│   └── draft_1_layout.json          排版数据（元素 id/中心/角度/尺寸）
└── verify.json               自检明细
```

---

## 5. 常用命令

```bash
# 标准三版
python3 -m pattern_studio run "国风水彩牡丹 疏朗"

# 指定预设/密度/版数/尺寸
python3 -m pattern_studio run "青花缠枝 密" --presets halfdrop,brick,radial --density 0.55 --prints 3 --tile 2048 2048

# 只解析需求 / 只出提示词（不花钱生图）
python3 -m pattern_studio spec "极简线条玫瑰 中等 灰色"
python3 -m pattern_studio prompts "敦煌壁画 菊花 密"

# 复用已有素材：不再生图
python3 -m pattern_studio run "国风水彩牡丹" --elements-dir out/xxx/elements
python3 -m pattern_studio run "国风水彩牡丹" --sheets-dir  out/xxx/sheets --reuse
python3 -m pattern_studio run "国风水彩牡丹" --reuse --out out/xxx      # 只重排/重渲染，0 成本

# 模型评估工具
python3 probe_images.py                     # 同一提示词跑一遍候选图像模型（花少量钱）
python3 eval_sheets.py /path/to/sheets/*.png  # 用真实拆格器给九宫格打分
```

### 参数速查

| 参数 | 默认 | 说明 |
|---|---|---|
| `--profile` | balanced | `cheapest` / `balanced` / `fast` / `best`（见上表） |
| `--model-sheet` / `--model-bg` / `--model-llm` | 跟随档位 | 单独覆盖模型 |
| `--prints` | 3 | 出几版 |
| `--tile W H` | 1024×1024 | 单版 tile 像素 |
| `--density` | 由 spec 推断 | 0–1 → 6%–38% 覆盖；越高越满 |
| `--presets` | halfdrop,phyllotaxis,scatter | 8 种：grid halfdrop brick diagonal radial stripes phyllotaxis scatter |
| `--gap` | 0.12 | 元素最小间距（相对基准边长） |
| `--evenness` | 0.8 | 排布均匀度 0–1：1=尽量等距（近格排），0=更随意 |
| `--rotate-deg` | 8 | 随机旋转 ±度（0=全正放） |
| `--mirror-prob` | 0.18 | 随机镜像概率 |
| `--bg-mode` | auto | auto=能平铺就 cover，否则镜像强制无缝；也可 cover/mirror/tile |
| `--bg-fade` | auto | 背景压淡比例（auto 按背景"花不花"自动定，实测 0.4–0.78） |
| `--bg-count` | 1 | AI 背景张数；不够的版本用本地镜像/旋转变体（省钱） |
| `--bg-desaturate` | 0.35 | 背景降饱和 |
| `--shadow` | 关 | 元素轻投影 |
| `--svg` | 关 | 额外输出 SVG 包装 |
| `--spec-file` | 无 | 读手写 spec（跳过需求解析，不花 LLM token） |
| `--count` | 由 density 反推 | 元素总数 |
| `--element-scale` | 1.0 | 元素整体尺寸倍率（大花主导风格用 1.6–1.8） |
| `--overlap-mode` | none | `layered`=允许层叠（复古手绘/莫里斯/巴洛克） |
| `--overlap-allow` | 0:0,1:0.28,2:0.62 | 各层允许遮挡比例，如 `"0:0.16,1:0.38,2:0.68"` |
| `--bg-mode flat --bg-color "#f2e6d6"` | — | 纯色底，跳过 AI 背景（省 $0.01） |
| `--gen-size` | 1024 | 生图分辨率 |
| `--seed` | 7 | 全流程种子 |
| `--llm-prompts` | 关 | 用大模型改写提示词 |
| `--no-llm` | 关 | 强制规则解析（0 token 成本） |

---

## 6. 模块地图

| 文件 | 行数 | 职责 |
|---|---|---|
| `spec.py` | ~430 | 关键词 → Spec；词典分层 + 大模型精修（type 枚举 + 本地英文模板） |
| `prompts.py` | ~250 | 九宫格/单元素/背景提示词；元素→9 格分配；可选 LLM 改写 |
| `imagegen.py` | ~440 | 四个 provider：mock / apimart / openai 兼容 / ComfyUI；apimart 含异步任务与 SSE |
| `mockgen.py` | ~380 | 程序化代笔（无模型时跑通全链路） |
| `sheet.py` | ~310 | 切格、透明底直取、多底色估计、软 alpha、反预乘、纸片检测、连通域、质量门 |
| `layout.py` | ~460 | 排版求解（核心）：环面碰撞、尺寸反推、8 预设、泊松、密度均衡 |
| `compose.py` | ~270 | 背景无缝判定/镜像/压淡、元素变换、3×3 环绕合成、对比图 |
| `verify.py` | ~190 | 真实重叠、覆盖率、逐像素无缝判定 |
| `cli.py` | ~420 | 编排、档位/模型选择、成本记账、报告 |
| `probe_images.py` | ~150 | 图像模型横评（成本/延迟/产物） |
| `eval_sheets.py` | ~150 | 九宫格质量评估（可拆数/格线/触边/底色） |

---

## 7. 调参指南

1. **元素本身不像 / 不好看** → 是模型或 prompt 问题，看 `sheets/sheet_0.png`；换 `--model-sheet` 或改 `prompts.json` 后 `--reuse` 重跑。
2. **拆格缺元素 / 带底色** → 看 `elements/*.png`；透明底模型（gpt-image-2）最省事；白底模型靠多底色模型兜底。
3. **太挤 / 太空** → 调 `--density`（先看 `out/density_sweep.png` 那种对照），再调 `--gap`。
4. **太乱 / 排布不平均** → 先看 `--evenness`（默认 0.8，调高更等距）；再 `--rotate-deg 0 --mirror-prob 0`，
   配合 `--presets grid,halfdrop,brick`。`report.md` 里有 `间距CV`（0.12–0.20 表示很均匀）与 `块CV` 可对照。
5. **背景抢主体** → 调 `--bg-fade 0.65`（更淡）或 `--bg-mode mirror`；`auto` 已经会按背景信息量自动压。
6. **背景有接缝** → `--bg-mode mirror` 强制无缝（会带对称感）；或换背景模型。
7. **元素重叠** → 不会发生（求解器保证 + verify 复核）；`verify.json` 里 overlap 不是 0 就是 bug。

---

## 8. 已知限制

- **元素是位图**：不做矢量化。要矢量稿再过 `算法/位图转矢量图/`（vtracer）。
- **九宫格依赖模型能力**：gpt-image-2/gemini/mai 基本能 9/9；z-image-turbo 偶尔一格两物。
- **元素无方向语义**：随机旋转可能让花头朝下（`--rotate-deg 0` 可关闭）。
- **多张九宫格之间会有色差**（元素多到需要 2 张时）。
- **不做分色/专色**：接 `print-psd-layering` / `raster-vector-posterization`。

---

## 8.5 设计方法论 / 知识库

「做什么样的花型、为什么这么做」不在本工具里，在 skill 里：
`~/.agents/skills/pattern-design-atlas/`（工作区索引：`花型设计知识库_索引.md`）。

- `references/00-taxonomy.md` 5 维分类 + 规格表模板
- `references/10-repeat-engineering.md` 8 种循环结构、接缝三类病因、密度语义
- `references/40-style-recipes.md` 16 个风格配方（元素/排布/配色/密度/坑/提示词/参数）
- `references/50-quality-gates.md` 几何/视觉/工艺三张验收表
- `references/70-failure-atlas.md` 27 条症状→原因→修法

工具出图，知识库决定"该出什么图"。两者配套用：先按配方定 spec，再 `run`。

## 9. 与仓库其它环节的衔接

```
关键词/AI 生图（本工具 ③）
   └→ 九宫格拆元素 + 自动拼版 + 无缝（④⑤⑥）
        ├→ drafts/draft_*_tile.png  → 直接用于印花/壁纸
        ├→ elements/*.png           → 投喂 算法/位图转矢量图/ → 专色矢量稿
        └→ draft_*_tile.png         → 投喂 fabric-seamless-psd 流水线 → 300dpi 分层 PSD / 分色
```

---

## 10. 产品接入（PatternFlow 后端，2026-10-20）

花型设计师的「生成花型」已换成这套算法（替代老的火山方舟文生图）：

- 后端适配层：`后端/core/patgen.py`（子进程走 `python -m pattern_studio run`，
  复用 `core/algo.py` 的 uv/超时/进程组终止；主程序仍零依赖）。
- 口径：
  - 关键词由 `core/patgen.py::build_keywords()` 从需求卡四维 + 识图标签 + 用户原话映射而来；
    算法自己再做规则解析 + 大模型精修，不再由后端拼英文提示词。
  - 回传的 `drafts/draft_*_tile.png` 就是产品里展示的「每版图」，每版本身可平铺。
  - 点数仍按「每版 60 点」的旧口径（`settings.patgen.cost`），与真实 API 成本无关。
- 产品默认档位（`settings.json → patgen`）：**`profile=fast` + `review=off` + 4 版**，
  实测约 20–50 秒、$0.02；聊天场景优先保证出图速度。要更高画质/质检就把 `profile`
  改成 `balanced`/`best`、`review` 改成 `auto`/`strict`（会更慢，约 +60~150 秒）。
- 密钥：用后端 `secrets.seamless_api_key`（APIMART），不再读算法目录的 `.env`
  （研发单跑要保持原行为，把 `patgen.use_algo_env` 设成 true 即可）。
- 运行环境：先跑一次 `./安装.command` 建本地 `.venv`（numpy + Pillow）。后端会优先用
  `.venv/bin/python` 直接起 `python -m pattern_studio`（**请求路径零网络**）。
  没建 venv 时才退回 `uv run --offline`；离线还缺依赖才联网一次。
  以前每次请求都让 uv 联网解析依赖，外网一断就会报
  `花型生成没跑通（退出码 2）。cause: operation timed out` —— 那不是生图超时。

### 2026-10-20 修的两个算法 bug（产品接入时暴露）

1. **审核提示词写死「三个版本」**：只出 1~2 版时模型会编造 #2/#3，下游按版号取图
   `StopIteration`。现在 `review.triage_sheet()` 按实际版数生成提示词，`review_flow`
   会丢弃不存在的版号并兜底到第一版。
2. **`--prints` 超过 3 时 `IndexError`**：旋转/镜像列表只有 3 项。现在超过 3 版会循环
   复用前三档旋转/镜像（排布仍然不同），4 版可直接跑。

