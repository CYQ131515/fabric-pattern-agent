# 语义/色层 PSD『纯色+蒙版』丢失渐变并产生白边

- 日期：2026-10-19
- 来源：user-reported ｜ 状态：fixed

## 现象
语义分层合成图与原图差距大、渐变消失、边缘白线

## 根因
每层用纯色+蒙版，层内渐变/明暗/颗粒被抹平，边界过渡像素无覆盖

## 修复 / 待办
改为每层保留原始像素+蒙版（Background 根层 + Colors 分组），合成 max_abs_diff=0

## 回归验证
语法/色层两版均验证 max diff=0

## 标签
fidelity, solid-color, halo, psd-layer
