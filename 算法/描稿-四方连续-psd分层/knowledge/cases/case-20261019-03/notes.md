# 星空涂鸦：AI带补缝的缝带边界亮度台阶 + 白线错位

- 日期：2026-10-19
- 来源：user-reported ｜ 状态：mitigated

## 现象
卫星网格/星星轮廓跨缝带边界处被切断、背景有直线亮度台阶

## 根因
AI 重画带内内容与带外原图在 mask 硬边界对不上；羽化大→双影，羽化小→硬切断；自动偏移无效（被切内容固定在原图边缘区）

## 修复 / 待办
该图改走原生重生成（无带边界）；通用规则入 L05：稀疏线稿优先 regenerate

## 回归验证
regenerate 版审查通过，但随后暴露新问题（见 case-04）

## 标签
band-boundary, lineart, feather-ghost, auto-shift-failed
