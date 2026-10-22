---
name: print-psd-layering
description: 服装印花 300dpi 分层 PSD 的制作与验收规范：尺寸/PPI、图层结构（根层 Background + Colors 分组）、每层保留原始像素+蒙版（合成必须逐像素等于源图）、命名与交付检查。用于把无缝花版输出成可印刷的 PSD 时加载。
metadata:
  version: "1"
  algorithm_root: "$PROJECT/算法/描稿-四方连续-psd分层"
---

# 印花分层 PSD 规范

## 规格

- 颜色模式 RGB 8bit，**300 PPI**；
- 高度固定 **9382 px ≈ 79.4 cm**，宽度按源图宽高比（`--width` 缺省自动）；
- 图层名只用 ASCII（中文会触发 psd-tools `macroman` 错误）。

## 结构（必须）

```
[根]
├── Background          ← 背景层，单独在根（不要放进分组）
└── Colors (分组)        ← 所有颜色分层放在一个分组里
    ├── Color01 #xxxxxx
    ├── Color02 #xxxxxx
    └── ...
```

## 保真要求（硬性）

- 每层 = **原始像素 + 该色域蒙版**（`--preserve-pixels` 默认开启），不要用"纯色+蒙版"，否则丢渐变、出白边；
- 验收：用 psd-tools 合成后与源无缝图比对，**max_abs_diff 必须 = 0**。

## 命令

```bash
# 色层分色（7–8 层，背景根层 + Colors 分组）
python steps/04-layers-color.py 无缝图.png 输出.psd --height 9382 --k 8 --group-name Colors

# 验收
python - <<'PY'
from psd_tools import PSDImage
import numpy as np
p = PSDImage.open('输出.psd')
print([(l.name, l.kind) for l in p])
comp = np.asarray(p.composite().convert('RGB'))
print('max diff:', int(np.abs(comp.astype(int) - 源.astype(int)).max()))
PY
```

## 交付前额外检查（修复带）

```bash
# 修复带（inpaint/quilt/donor 区域）与周围亮度/颗粒是否一致
python steps/03b-verify-repair.py --image 无缝图.png --bands "v:cx,w;h:cy,w"
```

- 带内外低频亮度差 >1.5 或颗粒比 >1.35 → 判为可见补丁（L20）；
- 修法：对修复带做低频亮度匹配（带内外均值差反向补偿，边缘羽化，避开外缘 14px 保 wrap）。

## 常见问题

- 合成有白边/渐变消失 → 用了纯色层，改 preserve-pixels；
- 分组错位 → Background 必须 append 在根，颜色层 `create_group`；
- 中文层名报错 → 改 ASCII；
- 文件过大（>800MB）→ 蒙版外填黑提高压缩率，或降低层数。

> 产出归档：所有 PSD/无缝图/预览必须存到算法项目 `deliverables/`（psd/seamless/previews/inputs），见 fabric-seamless-psd skill 的“交付目录”。
