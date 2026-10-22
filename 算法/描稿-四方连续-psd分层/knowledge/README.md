# 经验库（Knowledge Base）

算法不是一次写死的，而是**一个小 agent**：每次跑完、每次被打回、每次用户指出问题，
都必须把「现象 → 根因 → 修复 → 回归验证」记录到这里；下次跑之前先读规则。

## 目录

```
knowledge/
├── README.md          # 本文件：使用与记录规范
├── lessons.md         # 教训清单（人可读，按时间/主题编号）
├── rules.json         # 机器可读策略规则（pipeline 会读取，用于选择路径）
├── INDEX.md           # 案例索引表
└── cases/<id>/        # 单个案例
    ├── case.json      # 结构化元数据（必填）
    ├── notes.md       # 现象/根因/修复/回归记录
    ├── input-*.png    # 问题输入（缩略图即可）
    └── rejected*.png  # 被打回的输出（缩略图，勿存全尺寸）
```

## 什么时候必须记案例

1. **用户指出问题**（不论是否修复完成）→ 立即建案例，状态 `open`；
2. **算法自动审查打回**（候选未通过）→ pipeline 自动写入 `auto-*` 案例；
3. **修复上线** → 更新案例状态为 `fixed`，补「回归验证」；
4. 同一问题第二次出现 → 在案例里记录复发次数，并升级到 `lessons.md`。

## 记录规范（case.json 必填字段）

```json
{
  "id": "case-20261019-01",
  "date": "2026-10-19",
  "title": "一句话现象",
  "source": "user-reported | auto-rejected | regression",
  "status": "open | fixed | mitigated | wontfix",
  "symptom": "用户/审查看到的现象（具体到位置、尺度）",
  "root_cause": "根因（尽量落到算法步骤）",
  "fix": "修复内容；未修复写 next",
  "regression": "如何验证不再复现（命令/指标）",
  "artifacts": ["相对路径列表"],
  "tags": ["band-boundary","gradient","lineart","review-miss", ...]
}
```

## 自我迭代循环（每个案例闭环）

```
发现/被打回 → 建案例(open) → 定位根因 → 修复 → 回归验证 → 状态 fixed
        ↑                                            │
        └── 若规则可泛化：更新 rules.json / lessons.md ┘
```

**原则**：宁可多记，不可漏记；缩略图 ≤800px；全尺寸失败产物在入库后删除。
