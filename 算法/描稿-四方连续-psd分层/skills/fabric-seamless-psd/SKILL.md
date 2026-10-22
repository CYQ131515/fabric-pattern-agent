---
name: fabric-seamless-psd
description: 服装面料描稿流水线：AI 清洗保花型 → 四方连续（带整图接缝审查与失败回退）→ 色层分色 300dpi 分层 PSD。用于把面料照片/花型图做成可印刷的循环花版；内置经验库与自我迭代协议，遇到审查打回或用户指出问题必须记录案例并更新规则。
metadata:
  version: "1"
  algorithm_root: "$PROJECT/算法/描稿-四方连续-psd分层"
---

# 服装面料描稿 · 四方连续 · PSD 分层

算法根目录：`$PROJECT/算法/描稿-四方连续-psd分层`

这是一个**小 agent**：能跑完整流水线，也能从失败中积累经验并迭代规则。
每次任务开始先读 `knowledge/lessons.md` 与 `knowledge/rules.json`；每次失败/被打回必须写案例。

## 何时用

用户要给面料花型做「描稿 / 四方连续 / 分色分层 PSD / 印花版」，或需要把图片变成 300dpi 可印刷循环花版时。

## 流水线（带审查与回退）

```bash
cd "$PROJECT/算法/描稿-四方连续-psd分层"
# 一键（自动：快速路径 → 审查 → 失败删除并回退 AI 清洗+补缝 → 再失败走原生重生成）
node pipeline.mjs --input 图.jpg --outdir work --height 9382

# 指定策略
node pipeline.mjs --input 图.jpg --seam ai          # 强制 AI 清洗 + AI 补缝
node pipeline.mjs --input 图.jpg --seam regenerate  # 稀疏线稿推荐：原生无缝重生成
```

每一步也可单独跑（`steps/01-clean.mjs`、`02-seam-*.mjs`、`03-verify-seam.py`、`04-layers-color.py`）。

> Skill 版本管理：本项目内副本在 `算法目录/skills/fabric-seamless-psd/`；修改后执行
> `node scripts/sync-skills.mjs --push` 推送到 `~/.agents/skills/` 生效（回同步用 `--pull`）。

## 交付目录（硬性，2026-10-20 用户要求）

所有产出**必须**归档到算法项目内，不要留在外部 workspace：

```
算法目录/deliverables/
├── psd/        300dpi 分层 PSD
├── seamless/   无缝 tile 源图
├── previews/   预览/平铺图
├── inputs/     本批输入图
├── MANIFEST.json
└── README.md
```

- `pipeline.mjs` 已自动把 PSD 与无缝图复制进 `deliverables/psd`、`deliverables/seamless`；
- 手工出件用：`node scripts/save-deliverable.mjs <文件...>`（按扩展名自动归档）；
- 命名：`<输入编号>-<内容描述>[-<方案/版本>]-colorlayers-<高度>.psd`。

## 硬性验收（不通过不交付）

1. **整图接缝审查**：`python steps/03-verify-seam.py --image 无缝图` 必须 ok（扫描贯穿式离群直线，**不能只看外缘**）；
2. **PSD 保真**：合成图 vs 无缝图 `max_abs_diff == 0`（色层必须保留原始像素+蒙版）；
3. **结构**：根层 `Background` + 分组 `Colors`（颜色分层在组内）；
4. 1:1 检查缝带边界无切断/错位；2×2 平铺无直线。

## 策略派发与回归

```bash
node scripts/regress.mjs     # 回归护栏：案例库坏图必须被打回、好图必须放行
```

- `lib/strategy.mjs` 读 `knowledge/rules.json`，按特征给出候选路径顺序（经验驱动派发）；
- `knowledge/regression.json` 是回归集：新增案例必须加一条 `expect: fail|pass`；
- 改任何阈值/规则前先跑回归，红了不许上线。

## 反馈可能是最重要的信号（必须采集）

pipeline 结束会写 `feedback/deliveries.jsonl`（含 delivery_id）。之后**必须**记录评判：

```bash
node scripts/feedback.mjs --list                                  # 看哪些交付还没评判
node scripts/feedback.mjs --delivery latest --verdict reject \
  --reason "缝带边界白线错位" --tags band-boundary,lineart --screenshot 截图.png
node scripts/feedback.mjs --delivery <id> --verdict accept
node scripts/learn.mjs        # 评判后重算：特征簇 × 路径通过率 → learned.json
```

- `accept` = 通过（用户说好或不吭声）；`reject` = 打回（用户指出问题）；
- reject 自动建案例 + 进回归集 + 计入学习负样本；
- 同类图积累 ≥2 条反馈后，派发自动按历史成功率排序；若全部失败过 → 优先试没试过的路径；
- 绝不能在用户指出问题后只修图不记录——那等于没学习。

## 经验库（必须维护）

```
knowledge/
├── README.md      # 记录规范
├── lessons.md     # 教训清单（L01..）
├── rules.json     # 机器可读策略（策略选择依据）
├── INDEX.md       # 案例索引
└── cases/<id>/    # case.json + notes.md + 缩略图
```

**触发条件（任一）必须建案例**：
- 用户指出问题（即使还没修好）→ `status: open`
- pipeline 审查打回候选 → 自动写入 `auto-*` 案例
- 修复完成 → 更新为 `fixed` 并补回归验证

手动记录：

```bash
node scripts/record-case.mjs --title "现象" --source user-reported --status open \
  --symptom "..." --root-cause "..." --fix "..." --input 原图.png --rejected 坏输出.png --tags band-boundary,lineart
```

## 成熟度与边界（诚实版）

- 已具备：案例记录、候选回退、经验派发（先验规则）、回归护栏；
- **尚不具备**：语义级判别（涂鸦/水彩/照片需 VLM）、自动规则学习、自动修复算子库；
- 审查器目前只能拦"贯穿直线接缝"，**拦不住矩形 overlay / 边界切断**（见 case-03/04，回归红）；
- 因此不能承诺"完美"，目标是：**明显坏件必须拦截且不交付，失败必回滚+记录，规则可回滚**。

## 自我迭代循环

```
运行 → 审查/用户反馈 → 建案例(open) → 定位根因 → 修复 → 回归验证 → fixed
                                   └→ 可泛化规则写入 rules.json / lessons.md
```

迭代纪律：
- 宁可多记不可漏记；失败全尺寸产物入库缩略图后删除；
- 同一问题第二次出现 → lessons 里升级该条，并在 `rules.json` 增加约束；
- 不允许在未过审查的情况下交付 PSD；三条路径都失败则中止并记录案例。

## 已知关键教训（详见 references/lessons.md）

- L01 只查外缘会漏掉内部接缝（平移把原图边缘移到中间）；
- L02 渐变/平涂图禁用确定性补缝（矩形色块）；
- L03 AI 补缝 band 不要 >200（贴图块）；
- L04 补缝后不要 Laplacian/Poisson 融合（几何错位→条纹/双影）；
- L05 稀疏线稿 AI 带补缝必切断描边 → 优先原生重生成；
- L06 原生重生成可能产生**矩形 overlay**（当前未解决，最高优先级，见 case-20261019-04）；
- L08 分层必须保留原始像素（否则丢渐变/白边）；
- L09 生成模型保花型用 2.5 世代；褶皱图用双参考。
