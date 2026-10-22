# 密集大花接缝：AI 补缝加后处理从拒单到 A 级

- 日期：2026-10-20
- 来源：user-reported ｜ 状态：fixed

## 现象
02-seam-ai 输出的 tile 在 2×2 平铺时 tile 边界对不上（wrapY 1.51）、带内亮度差 4.7 → 03/03b 都过不了

## 根因
竖带全高，模型重画的竖带在图像上下边缘不自我衔接；带内重画内容低频与环带不一致

## 修复 / 待办
steps/09-seam-postfix.py：AI贡献收敛 k=0.7 + 四边16px wrap强制还原 + 03b口径带均值匹配5轮；产品候选 ai2 与 pipeline ai 均接上

## 回归验证
(待补)

## 标签
band-boundary, wrap, ai-seam
