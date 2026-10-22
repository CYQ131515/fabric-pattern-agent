# 单色蓝渐变几何花：确定性补缝出现矩形色块 / AI宽band出现贴图块

- 日期：2026-10-19
- 来源：user-reported ｜ 状态：fixed

## 现象
第六张图接缝带出现一块块贴图状纹理与硬直线；确定性补缝则在渐变上留下矩形色块

## 根因
band=240 的硬 mask 让 2.5-ext 复制邻近纹理；确定性补缝在平滑渐变上产生色块；事后融合又因几何错位产生条纹

## 修复 / 待办
AI 补缝改 sunburst + band 120；补缝后不做融合；审查器补上整图离群直线检测

## 回归验证
samples/input6-run-report.json；1:1 边缘检查无贴图块

## 标签
band-boundary, gradient, stamp-patch, blend-ghost
