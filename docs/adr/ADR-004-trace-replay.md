# ADR-004 · 用「计划固化 + 回放」做 LLM 系统的确定性回归

- 状态：已接受
- 日期：2026-11-09
- 相关：FR-603、NFR-01、§13.2

## 背景

LLM 输出不可复现，导致「改了 prompt 之后测试全红，但不知道是改坏了还是模型抽风」。
传统做法是每次跑真实模型，慢、贵、不可比。需要一个能在 CI 里秒级跑的回归机制。

## 决策

**把模型输出（计划）当 fixture 存下来，模型不再是测试的依赖。**

```
fixtures/traces/<case_id>.json
{ plan: {...},              ← 固化
  data_seed: 3,             ← 数据快照标识
  expected: {book_digest, answer_digest},
  tools: [{name,args,result_digest}],
  model, prompt_version }
```

- `make replay`：重放 plan + seed → 必须复现 `book_digest`；**全程不调 LLM**，秒级、零成本。
- 真实模型只在 `make eval-llm` 里跑（140 用例），用于「LLM 版 vs 规则版」对照与成本统计。
- fixture 只在**计划按设计变更**时重录，commit message 写明原因。

## 为什么 digest 而不是全文

- 全文 diff 噪音大（措辞变化会淹没数字错误）。
- `book_digest = sha256(canonical_json(book))` 只对**数字**敏感——正是我们要守的东西。
- 需要看内容时，fixture 里另存 `answer` 原文供人读，但断言只比 digest。

## 后果

- 正面：CI 里能跑「行为一致性」；prompt 回归有基线；故障可复现（生产会话可导出成 fixture）。
- 负面：plan 固化后，模型升级不会自动反映 → 必须定期跑 `eval-llm` 刷新 fixture，否则测的是「旧模型的行为」。
- 纪律：**不许为了让 replay 变绿而改 expected**（等价于改答案）。改 expected 必须同时改 plan 或数据 seed，并写明原因。
