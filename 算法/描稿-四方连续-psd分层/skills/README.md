# Skill 文件（随算法项目做版本管理）

本目录是 4 个相关 agent skill 的**项目内副本**（含各自 `references/`）：供版本管理、随项目迁移与审阅。

| Skill | 用途 |
| --- | --- |
| `fabric-seamless-psd/` | 主流程：AI 清洗保花型 → 四方连续（带审查/回退）→ 300dpi 分层 PSD；含经验库与迭代协议 |
| `seamless-tile-repair/` | 无缝方法手册：AI 缝带 / 确定性补缝 / 重生成 / quilt 的适用条件与失败模式 |
| `print-psd-layering/` | 分层 PSD 规范：尺寸/PPI、Background+Colors 结构、像素保真、交付检查 |
| `raster-vector-posterization/` | 色块/矢量化：3~6 色专色稿、vtracer 等 |

## 生效位置（agent 实际加载的）

用户级目录：`~/.agents/skills/<name>/SKILL.md`（即 `~/.agents/skills/`）。

## 双向同步（推荐：改项目副本 → 推送）

```bash
cd "$PROJECT/算法/描稿-四方连续-psd分层"

node scripts/sync-skills.mjs --pull   # 用户级 → 项目（同步别人/历史改动）
node scripts/sync-skills.mjs --push   # 项目 → 用户级（改完项目副本后推送，立即生效）
node scripts/sync-skills.mjs --diff   # 只看差异
```

## 内容分工

- **技能流程/规则**：本目录 skill 文件；
- **经验与数据**：`../knowledge/`（lessons.md / rules.json / learned.json / regression.json / cases/）；
- **交付物**：`../deliverables/`（psd / seamless / previews / inputs / MANIFEST.json）。
