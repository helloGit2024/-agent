# ADR-001 · LLM 只走统一出口，先接公有云、保留私有化切换点

- 状态：已接受
- 日期：2026-11-09
- 相关：FR-504 / FR-505、P-08、C-03

## 背景

S1 要把 `n_understand` 从规则换成 LLM 规划。摆在面前的是三个选择：
(a) 业务代码直接调 provider SDK；(b) 自建一层薄网关；(c) 引入 LangChain 的 chat model 抽象。
同时合规要求「模型分/基准分布不出域」，而供应商可能是公有云，也可能是内网自托管。

## 决策

**自建 `llm/call.py` 薄网关**，provider 无关：

- 入参：`purpose / prompt / schema / budget`；出参：`{data, usage, meta}`。
- 固定：`temperature=0`、`seed`（会话级）、`model`（**具体版本号**，禁 `qwen-max` 这类浮动别名）、`schema_hash`。
- 重试 1 次（仅对 schema 不合规与 5xx）；仍失败 → 抛 `LLM_UNAVAILABLE` → 上层走 D2。
- 每次调用写 `llm_call_log`（原文不落，只存 `output_hash`）。
- provider 适配器在 `llm/providers/`，各 ≤80 行，**不含任何业务判断**。

不选 (a)：没有统一出口就无法保证「被记录、被限流、能降级」（§12.2）。
不选 (c)：LangChain 的 chat model 会把 retry/流式/工具调用语义都带进来，与本项目「计划是纯数据」的模型冲突，且增加升级面。

## 后果

- 正面：切供应商只改一个适配器；D1/D2 降级有唯一触发点；成本可按 `purpose` 分账。
- 负面：要自己维护参数兼容（如 seed 并非所有 provider 支持）→ 在适配器里声明 `supports_seed`，不支持时记 `seed=null` 并在 `meta` 标注。
- 风险：不同 provider 对 JSON schema 的严格程度不同 → 用「本地 jsonschema 二次校验」兜底，不信任 provider 的 strict mode。
