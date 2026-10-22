---
name: raster-vector-posterization
description: 位图转矢量/色块化技术手册：把彩色花型做成 3~6 色块或专色印刷稿（含 AI 生成黑白灰立体基稿），并矢量化导出。记录已验证有效的流水线、必踩的坑（明度三色失败、vtracer --max-colors 偏色、PyPI 原生模块签名、无头浏览器截图路径）与验收标准。
metadata:
  version: "1"
  algorithm_root: "$HOME/deepseek herness/服装/算法/位图转矢量图"
---

# 位图 → 矢量 / 色块化 · 技术手册

算法根目录：`$HOME/deepseek herness/服装/算法/位图转矢量图`

用于：花型图 → 黑白灰 N 档 / 专色分色稿 → 矢量（SVG/分色层），面向印花、丝印、数码印。

## 何时用

- 用户要把花型/照片做成「色块化」「黑白灰分层」「专色分色」「矢量化」。
- 需要判断该用哪种分色策略、参数怎么选、为什么某版"型"丢了。

## 先记住三条硬结论（来自实测）

1. **纯明度全局阈值做 3 色一定失败**：同亮度不同颜色的相邻花会粘成一片，"型"必丢。3 色要么走**线稿法**（轮廓压黑），要么走**专色**。
2. **vtracer 的 `--max-colors` 会偏色**（实测 7 色源被量化成 `#345f71/#a3576d/#d98544`）。要控色必须**自己算调色板，再用 `--palette`**。
3. **自动取色（k-means/中位数）在低饱和水彩上会发灰**：生产稿的专色应由人工/色卡指定，自动只做候选。

## 技术路线决策树

```
需要几色？
├─ 3 色（黑白灰）
│   ├─ 要"型"清楚 → 线稿法：Sobel 轮廓压黑 + 中间调压灰 + 白底
│   ├─ 要块面干净   → 色彩分割 + 图着色压 3 档
│   └─ 最好：AI 先画黑白灰立体基稿 → 再确定性 3 档
├─ 3 色（专色）
│   └─ 3 墨 + 纸白，按 OKLab 最近色归并；palette 人工指定
├─ 6 色（黑白灰）
│   └─ OKLab 三维聚类 + 引导滤波 + majority 合并 + 图着色 6 档
└─ 全彩矢量
    └─ 量化调色板 → vtracer --palette（见下）
```

## 通用流水线（确定性部分）

```python
# 1) 明度/颜色空间：OKLab
L,a,b = rgb_to_oklab(img)
# 2) 保边去噪：引导滤波（自实现 box filter + cumsum，O(N)）
L = guided1(L, r=4~6, eps=0.004~0.01)
# 3) 量化：k-means（1D 明度 或 3D OKLab）
# 4) 合并碎块：标签多数投票（majority, 3x3/5x5, 6~12 次）
# 5) 图着色：相邻分割块强制不同档（3/6 档时必做）
# 6) 矢量化：vtracer --palette "<自己算的色>" --hierarchical cutout
#    --filter-speckle 2~8 --simplify 0.8~2.5 --optimize 2
```

## 已验证参数（918×1398 花型）

| 目标 | 参数 | 结果 |
|---|---|---|
| 6 档黑白灰 | OKLab 3D k-means K=8 + majority 10 + graph | 722 path / 226 KB，花可辨 |
| 3 档黑白灰（线稿） | Sobel 梯度 top 3~5% + dilate 0~1 + L 分位 12/55 | 486 path / 243 KB，型最清楚 |
| 3 档黑白灰（块面） | 3D k-means K=8 + graph 3 | 291 path / 164 KB |
| 3 专色 S1 | 玫瑰/橄榄/沙 + 白 | 408 path / 178 KB，最贴近原图气质 |
| 全彩矢量化 | palette 精确 + speckle 8 + simplify 2.5 | 3314 path / 1.66 MB |

## 坑（都踩过）

- **vtracer PyPI wheel 在 macOS 会被代码签名拦住**（`different Team IDs`）：改用 GitHub Release 的 CLI 二进制，`codesign --force --sign -` 后可运行。
- **无头 Edge 截图必须用绝对路径 + `--user-data-dir`**，否则不生成文件。
- **`--max-colors` 偏色**，见上。
- **`watershed` 聚类会过度简化**（29 path 但内容全丢），只适合打底。
- **`--filter-speckle` 开太大吃细节**：细茎/花蕊会消失；细线保护要单独做。
- **AI 图生图很难严格只出 3 个平涂色**：模型会给灰度渐变；正解是 AI 出"黑白灰立体基稿"，再用确定性算法压 3 档。
- **自动 k-means 调色板发灰**：低饱和图不要在 RGB 里平均。

## AI 生成黑白灰基稿（apimart）

- 接口：`POST https://api.apimart.ai/v1/images/generations`（异步）→ `GET /v1/tasks/{id}` 轮询。
- 入参：`model`、`prompt`、`size`（`1:1/4:3/3:4/2:3/3:2/16:9/9:16/auto`）、`n`、`image_urls`（支持 `data:image/png;base64,...`）。
- 成本（2026-10 实测，USD/张）：`gemini-2.5-flash-image-preview` **0.0125**；`gemini-3.1-flash-image-preview` 0.015；`seedream-5-0-flash` 0.0137；`qwen-image-2.0` 0.02；`flux-kontext-pro` 0.032。
- 选型：默认 `gemini-2.5-flash-image-preview`（便宜且编辑强）；要更强对比可试 `gemini-3.1-flash-image-preview`。
- 提示词要点：明确"EXACTLY three flat tones / no gradients"，但仍需后处理保证只有 3 色；强调"keep same floral composition"。
- API Key 走环境变量 `APIMART_KEY`，不要写进脚本。

## 验收标准（交付前）

1. 色数准确：统计 SVG 中唯一 `fill` 数 = 目标色数；
2. 缩略 + 2× 放大都要看，不能只看缩略图；
3. "型"检查：随机挑 5 朵花，能否一眼认出是花；
4. 细线检查：茎/蕊是否保留（3 色下尤其容易丢）；
5. 体积/节点数预算：单图 path 数记录在案；
6. 无缝稿需另做 wrap 处理（见 `seamless-tile-repair` skill）。

## 参考

- `references/lessons.md`：失败案例与规则
- `references/cases.md`：每次任务的输入/参数/结果记录（持续追加）
