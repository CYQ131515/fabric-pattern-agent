# 学习报告（自动生成）

生成时间：2026-10-19T22:26:40.180Z
最小样本数：1

| 特征簇 | 路径 | 样本 | 通过 | 打回 | 得分 | 高频问题标签 |
| --- | --- | --- | --- | --- | --- | --- |
| lg0-hc2-ed0 | B-clean+ai | 1 | 1 | 0 | 0.667 | structure(1) |
| lg3-hc1-ed0 | B-clean+ai | 1 | 1 | 0 | 0.667 | - |
| lg3-hc1-ed0 | A-fast | 1 | 0 | 1 | 0.333 | auto-rejected(2) A-fast(1) |
| lg2-hc2-ed0 | B-clean+ai | 1 | 0 | 1 | 0.333 | band-boundary(1) lineart(1) feather-ghost(1) |
| lg2-hc2-ed0 | C-clean+regenerate | 1 | 0 | 1 | 0.333 | rect-overlay(1) regenerate(1) review-gap(1) |
| lg2-hc2-ed0 | A-fast | 1 | 0 | 1 | 0.333 | auto-rejected(2) A-fast(1) |
| lg0-hc2-ed1 | B-clean+ai | 2 | 0 | 2 | 0.25 | auto-rejected(2) band-boundary(1) feather-ghost(1) |
| lg0-hc2-ed1 | B-clean+ai(bugfix) | 1 | 0 | 1 | 0.333 | band-boundary(1) cut-seam(1) review-gap(1) |
| lg0-hc2-ed1 | C15 = C2 + donor-strip content-aware fill + Poisson (region repair) | 1 | 1 | 0 | 0.667 | - |
| lg0-hc2-ed1 | unknown | 3 | 0 | 3 | 0.2 | auto-rejected(6) B-clean+det(2) D-clean+tile-multi(1) |
| lg0-hc2-ed1 | A-fast | 2 | 0 | 2 | 0.25 | auto-rejected(4) A-fast(2) |
| lg0-hc2-ed1 | C-clean+regenerate | 1 | 0 | 1 | 0.333 | auto-rejected(2) C-clean+regenerate(1) |
| lg2-hc0-ed0 | 描稿: clean(pass1 style) + flatten(pass2) → AI seam → band fix → PSD | 1 | 0 | 1 | 0.333 | background-dirty(1) style-ok(1) |
| lg1-hc0-ed0 | 描稿: clean(保花型) → AI平绿底 → 2×2 inpaint(低频边缘offset) → PSD | 1 | 1 | 0 | 0.667 | - |
| lg2-hc1-ed1 | 描稿: clean(去手/褶皱/水印) → AI缝带/2×2 inpaint(band 76, safe offset) → PSD | 1 | 1 | 0 | 0.667 | - |
| lg1-hc0-ed1 | 描稿: clean(去褶皱/阴影) → AI缝带(band160,feather14) → 修复带亮度修正 → PSD | 1 | 0 | 1 | 0.333 | warp(1) unwarp(1) optimize(1) |
| lg1-hc0-ed1 | 反向展平(undo warp)@2048 → AI缝带band200 → wrap stitch → PSD (v2) | 1 | 0 | 1 | 0.333 | unwarp-wrong(1) rule(1) |
| lg1-hc0-ed1 | 平铺还原@2048 → AI缝带120 → 不规则混合(色调中性化) → 平滑色调补偿 → PSD (v3) | 1 | 0 | 1 | 0.333 | overprocessed(1) revert(1) |
| lg1-hc0-ed1 | 扭曲面料规则: 想象平铺还原@2048 → AI缝带band120 → PSD | 1 | 1 | 0 | 0.667 | - |
| lg1-hc0-ed1 | 扭曲面料: 想象平铺还原@2048 → 缩1024 → 2×2 inpaint(90,原生分辨率) → PSD | 1 | 1 | 0 | 0.667 | - |
| lg0-hc0-ed1 | B-clean+ai | 1 | 0 | 1 | 0.333 | auto-rejected(2) B-clean+ai(1) |
| lg0-hc0-ed1 | unknown | 2 | 0 | 2 | 0.25 | auto-rejected(4) B-clean+det(1) D-clean+tile-multi(1) |
| lg0-hc0-ed1 | A-fast | 1 | 0 | 1 | 0.333 | auto-rejected(2) A-fast(1) |
| lg0-hc0-ed1 | C-clean+regenerate | 1 | 0 | 1 | 0.333 | auto-rejected(2) C-clean+regenerate(1) |

## 推荐派发（样本≥1 的簇）

- lg0-hc2-ed0: B-clean+ai
- lg3-hc1-ed0: B-clean+ai → A-fast
- lg2-hc2-ed0: B-clean+ai → C-clean+regenerate → A-fast
- lg0-hc2-ed1: C15 = C2 + donor-strip content-aware fill + Poisson (region repair) → B-clean+ai(bugfix) → C-clean+regenerate → B-clean+ai → A-fast → unknown
- lg2-hc0-ed0: 描稿: clean(pass1 style) + flatten(pass2) → AI seam → band fix → PSD
- lg1-hc0-ed0: 描稿: clean(保花型) → AI平绿底 → 2×2 inpaint(低频边缘offset) → PSD
- lg2-hc1-ed1: 描稿: clean(去手/褶皱/水印) → AI缝带/2×2 inpaint(band 76, safe offset) → PSD
- lg1-hc0-ed1: 扭曲面料规则: 想象平铺还原@2048 → AI缝带band120 → PSD → 扭曲面料: 想象平铺还原@2048 → 缩1024 → 2×2 inpaint(90,原生分辨率) → PSD → 描稿: clean(去褶皱/阴影) → AI缝带(band160,feather14) → 修复带亮度修正 → PSD → 反向展平(undo warp)@2048 → AI缝带band200 → wrap stitch → PSD (v2) → 平铺还原@2048 → AI缝带120 → 不规则混合(色调中性化) → 平滑色调补偿 → PSD (v3)
- lg0-hc0-ed1: B-clean+ai → A-fast → C-clean+regenerate → unknown
