"""SAM3.1 分割 → PSD 分层。

和旧算法的根本区别：**模型不重画像素**。
SAM3.1 是分割模型，只回答"哪些像素属于这个概念"，输出的 RGB 就是原图那几个像素。
所以每层内容直接取原图，`合成 ≡ 原图` 由构造成立，不需要任何收敛修复。

工作流（`2109041802081820673`）的节点：
    431 LoadImage          图片输入
    434 CR Text            文本输入（SAM3 检测提示词）
    435 RH_Translator      force_english，中文提示词也能用
    430 SAM3_Detect        threshold 0.5 / refine 2 / individual_masks False
    438 JoinImageWithAlpha 原图 + mask → 目标带透明底
    440 SaveImage          ← 要这个：目标层（透明底）
    437 JoinImageWithAlpha 原图 + 反相 mask
    432 SaveImage          抠掉目标后剩下的（目前没用到）
"""
