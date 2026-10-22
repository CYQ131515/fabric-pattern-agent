# 蓝白花卉图：2×2 inpaint 修复带亮度差形成矩形色块瑕疵

- 日期：2026-10-20 ｜ 来源：user-reported ｜ 状态：open

## 现象
用户放大看到背景里一块矩形区域亮度/颗粒与周围不一致（修复带边缘可见）

## 根因
2×2 画布 inpaint 后把修复带贴回时，修复带内低频亮度与周围差 d_lp≈1.91（阈值1.5），肉眼可见矩形补丁；此前审查只查直线/wrap，查不出这种块级差异

## 修复 / 待办
新增修复带连续性检查 03b-verify-repair.py（带内外低频亮度差/颗粒比）；pipeline 对 AI 缝带候选强制检查；待做：修复带低频匹配/边界羽化

## 回归
reg-06：input10 seamless + bands v:440,36;h:512,36 → 03b 必须 fail

## 标签
repair-band, flat-patch, review-gap
