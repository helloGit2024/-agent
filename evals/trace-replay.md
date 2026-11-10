# trace-replay：把生产故障变成回归用例

> 目标：**LLM 系统也能做确定性回归**。做法是把模型输出（计划）当 fixture 存下来，模型就不再是测试的依赖。
> 依据：ADR-004、§13.2、FR-603。

## 1. fixture 格式

`fixtures/traces/<case_id>.json`：

```json
{
  "case_id": "q2-v1-s1",
  "plan": {
    "intent": "STATS", "branch": "aggregate",
    "scope": {"product": "P_CASH_001", "window": "近一周", "dimension": "overall"},
    "tools": [{"name": "compute_metric", "args": {"metric_id": "fpd7_rate"}}],
    "clarify": null, "notes": ["FPD7 需前移 7 天"]
  },
  "data_seed": 3,
  "expected": {
    "book_digest": "sha256:9f2c…",
    "answer_digest": "sha256:41ab…",
    "envelope": ["【口径】…", "【窗口】…", "【过滤】…", "【截止】…"]
  },
  "tools": [
    {"name": "compute_metric", "args": {"metric_id": "fpd7_rate"},
     "result_digest": "sha256:7d01…", "latency_ms": 88}
  ],
  "model": "qwen-plus-2026-05-12",
  "prompt_version": "plan-1.0.0",
  "recorded_at": "2026-11-20T10:00:00+08:00"
}
```

字段纪律：

| 字段 | 要求 |
|---|---|
| `plan` | **必须是当时真实产生的计划**，不许手写理想计划 |
| `data_seed` | 能重建同一份夹具（`make init SEED=3`） |
| `expected.book_digest` | `sha256(canonical_json(book))`，只对数字敏感 |
| `expected.answer_digest` | 对 `normalize(answer)`（剥信封后正文）取 digest |
| `tools[].result_digest` | 用于定位「是工具结果变了还是渲染变了」 |
| `model / prompt_version` | 必填；换模型不自动更新 fixture，要显式重录 |

## 2. 录制（三种来源）

| 来源 | 命令 | 用途 |
|---|---|---|
| 真实模型跑一遍 | `make record CASE=q2-v1-s1` | 新用例初次录制 |
| 生产会话导出 | `make record FROM=artifacts/sessions/<sid>.json` | 把线上故障变成回归用例（最有价值） |
| 规则版跑一遍 | `make record PLAN=rules` | 建立「规则基线」用于对照 |

**录制后必须人工看一眼**：`expected` 是否真的是「我们应该给出的答案」。
录错了等于把 bug 固化成标准答案——这是这套机制唯一的严重风险。

## 3. 回放

```bash
make replay                 # 全部 140 条
make replay CASE=q2-v1-s1   # 单条
make replay DIFF=1          # 失败时打印逐行 diff
```

回放做的事：

```
读 fixture.plan
  → 注入 data_seed 重建夹具（或复用已建库）
  → 按 plan 直接执行工具（跳过 LLM 规划）
  → 模板渲染（跳过 LLM 叙述）
  → 比 book_digest / answer_digest / tools[].result_digest
  → 三项全等 = pass
```

**全程不调 LLM**：秒级、零成本、可在 CI 跑（门禁 H）。

## 4. 失败怎么定位（这是它比「跑模型」强的地方）

| 症状 | 说明 | 去查 |
|---|---|---|
| `book_digest` 变、`tool result_digest` 也变 | 计算或数据变了 | `core/model_metrics` / `stats` / 夹具 seed |
| `book_digest` 变、`tool result_digest` 不变 | 登记逻辑变了 | `AnswerBook.put` / `_exec_*` |
| `book_digest` 不变、`answer_digest` 变 | 渲染变了 | `_r_*` / 信封 |
| 只有 `envelope` 变 | 信封话术变了 | `core/envelope` |
| `plan` 本身不合法 | 录制时就没校验 | 先过 `validate_plan`，再重录 |

## 5. 重录纪律（P-11）

允许重录的唯一理由：**计划或行为按设计变更**（如新增 curve 指标、改信封话术）。
重录必须同时做三件事：

1. commit message 写：`T-xxx: 重录 q2-v1-s1 等 3 条，原因=<设计变更>`；
2. 在 `docs/tasks.md` 对应任务里记一行「影响 fixture：…」；
3. 重录前后各跑一次 `make replay`，把 diff 摘要贴进 commit（证明只变了该变的）。

**禁止**：为了让 replay 变绿而改 `expected`；禁止批量重录「反正都变了」。

## 6. 与其它评估的关系

| 机制 | 调 LLM | 速度 | 管什么 |
|---|---|---|---|
| `make gate` A/B/C | 否 | 秒 | 语义层不变式、评估器自身有效性 |
| `make regress`（7 题） | 否 | 秒 | S0 行为逐字节不变（历史基线） |
| `make replay`（140 条） | 否 | 秒—分 | 计划→执行的确定性、渲染一致性 |
| `make eval-llm` | 是 | 分钟 | 模型能力、prompt 质量、成本 |

四者互补：**replay 管「同样的计划是否给同样的数」，eval-llm 管「模型是否给出好计划」**。
