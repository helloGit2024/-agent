# 目标架构（S1—S5）

> 一句话：**在已有的确定性语义层外面，加三个可替换的壳**——规划壳（LLM/规则可切）、协议壳（MCP/本地可切）、叙述壳（LLM/模板可切）。
> 内核（册子 + 唯一入口 + 权限 + 信封）**一行不改**，这是整套演进的安全边界。

## 1. 全图

```
┌─ 入口 ─────────────────────────────────────────────────────────────┐
│  CLI (v2_dialogue.py)      MCP client (审批/报表平台)      Trae 调试 │
└───────────────┬──────────────────────┬─────────────────────────────┘
                │                      │
┌───────────────▼──────────────────────▼─────────────────────────────┐
│ 准入闸门 ①  token → 角色 → 租户 → access_policy 编译成 scope        │
│            （S1: access/admission.py；无 token 一律 DENIED）         │
└───────────────┬────────────────────────────────────────────────────┘
                │ scope
┌───────────────▼────────────────────────────────────────────────────┐
│ 规划壳（可替换）                                                     │
│   S1: llm/plan.py  ── LLM 规划（temperature=0, schema 强约束）       │
│         │ 失败/超预算/连续 2 次不过 schema                            │
│         └─▶ fallback: core/intent.py（规则版，一行不删）             │
│   闸门 ② 计划：步骤白名单 + 指标在册 + 维度白名单 + 分支一致性        │
└───────────────┬────────────────────────────────────────────────────┘
                │ plan {intent, scope, tools[], clarify}
┌───────────────▼────────────────────────────────────────────────────┐
│ 闸门 ⓿ 词汇闸门（纯规则，跑在规划之前）                              │
│   model_metrics.detect() → model / aggregate / ambiguous / none      │
│   命中即锁分支，两本册子互斥注册                                      │
└───────────────┬────────────────────────────────────────────────────┘
                │
┌───────────────▼────────────────────────────────────────────────────┐
│ 执行层（内核，S0.5 已固化，不改）                                     │
│  tools/registry.call ──┬─ compute_metric ─────▶ catalog.guard_call    │
│                        │                        → build_view → compute│
│                        │                        → post_guard → 信封   │
│                        ├─ compute_model_metric ▶ model_metrics        │
│                        │   三级路由: find_materialized → 实时 → 拒绝   │
│                        ├─ get_model_health（本地，含 base_dist）       │
│                        ├─ summarize_ref（S3，外卸二次读）              │
│                        └─ attribute_change / rank_policies             │
│  闸门 ③ schema/实体  闸门 ④ 预算与熔断（S1）                          │
└───────────────┬────────────────────────────────────────────────────┘
                │ book + envelope + source(MEASURED|MATERIALIZED)
┌───────────────▼────────────────────────────────────────────────────┐
│ 叙述壳（可替换）                                                     │
│   S4: llm/narrate.py（只吃槽位+ref_id，绝不吃原始表）                 │
│         └─▶ fallback: v2_dialogue._r_* 模板渲染（天然带信封）         │
│  闸门 ⑤ 出口：verify_numbers 白名单 → 未登记数字 → 丢弃叙述回退模板   │
└───────────────┬────────────────────────────────────────────────────┘
                │
┌───────────────▼────────────────────────────────────────────────────┐
│ 闸门 ⑥ 审计：AuditChain + llm_call_log + tool_call_log + memory_audit │
└────────────────────────────────────────────────────────────────────┘

旁路（不进主链）：
  记忆：会话结束 → memory/writer.py（规则侧，非 LLM）→ MemoryStore
  物化：scheduler/registry.run_all → 同一函数 → model_health_daily → base_dist 回流
  评估：eval/* / evals/* → 不调 LLM 的 replay 与调 LLM 的对照
```

## 2. 模块依赖方向（只许向下）

```
v2_dialogue.py（编排）
   ├─▶ llm/            （新：call / plan / narrate / context / budget）
   ├─▶ memory/         （新：store / adapters / writer / policy）
   ├─▶ tools/          （壳）
   │     ├─▶ compute_metric ─▶ core/catalog ─▶ core/entities ─▶ core/db
   │     └─▶ compute_model_metric ─▶ core/model_metrics ─▶ core/stats
   ├─▶ scheduler/      （冻结参数，调同一函数）
   └─▶ core/envelope · core/codes

mcp_server/（新，S2）
   └─▶ core/knowledge · core/catalog（只读）· core/db（只读连接）
       ✗ 绝不 import tools/ 或 core/model_metrics（分数列不出域）
```

**三条禁止的依赖**（违反即架构腐化）：
1. `core/*` 不许 import `llm/`、`memory/`、`tools/`、`mcp_server/`（内核不知道壳的存在）。
2. `mcp_server/` 不许 import `core/model_metrics`（否则 `base_dist`/`score_col` 可能随响应出域）。
3. `llm/` 不许 import `tools/`（规划层只产出计划，不执行）。

## 3. 新增目录与文件

```
llm/
  call.py        唯一出口：provider 适配、重试、超时、schema 校验、记账
  plan.py        规划：prompt 组装 → 调用 → validate_plan → 返回 plan
  narrate.py     叙述：只吃槽位 + ref_id → 文本
  context.py     prompt 组装（唯一允许拼 prompt 的地方，禁传 rows）
  budget.py      token 预算器与裁剪规则
  providers/     qwen.py / deepseek.py（薄适配，无业务逻辑）
memory/
  store.py       MemoryStore 接口 + MemoryItem
  adapters/      langmem.py / zep.py（二选一，同一契约）
  writer.py      会话结束后的规则侧写入（脱敏、置信度、有效期）
  policy.py      FILLABLE/FORBIDDEN 校验（现有逻辑迁入）
mcp_server/
  policy_catalog.py   5 个只读工具
  smoke.py            client 冒烟
core/
  admission.py   准入闸门（token→角色→scope）
  context_budget.py （或放 llm/budget.py，二选一，不要两处）
  settings.py    环境配置（MIN_SAMPLE / provider / 预算上限）
scheduler/
  promotion.py   升格队列与下线判定（FR-105）
```

## 4. 三条演进路径（每一处都可单独回滚）

| 维度 | 现在 | 目标 | 回滚开关 |
|---|---|---|---|
| 规划 | 规则 | LLM + 规则兜底 | `BTA_LLM=off` 立即回规则（已有） |
| 协议 | 进程内函数 | MCP client + 本地降级 | `BTA_MCP=off` 回进程内 |
| 存储 | SQLite | PostgreSQL | `BTA_DB_URL` 未设则用 SQLite |
| 记忆 | 无 | LangMem / Zep | `BTA_MEMORY=off` 回无记忆 |
| 叙述 | 模板 | LLM + 模板回退 | 闸门 ⑤ 自动回退，无需开关 |

> 四个开关**必须彼此独立**。任一开关关掉，`make gate` 仍须全绿——这是「壳可换、内核不动」的可验证定义。

## 5. 降级矩阵（D0—D4，对应 FR-505）

| 级别 | 触发 | 规划 | 叙述 | 用户看到 | 可观测标记 |
|---|---|---|---|---|---|
| D0 | 正常 | LLM | LLM | 完整回答 | `degrade=D0` |
| D1 | 叙述过不了闸门 ⑤ | LLM | 模板 | 完整回答，措辞机械 | `degrade=D1, exit_gate=rollback` |
| D2 | 规划失败/超预算/schema 连挂 2 次 | 规则 | 模板 | 完整回答 + 「本次未使用智能解析」 | `degrade=D2, plan_source=rules` |
| D3 | 执行失败（错误码 ≠ OK） | — | — | 说明书：哪步失败/已完成什么/下一步 | `degrade=D3, code=…` |
| D4 | 连续 3 次失败 | 熔断 | — | 「已转人工，工单号 xxx」 | `degrade=D4` |

**D3 的硬规则**：说明书中**不含任何统计数字**。给不了数就给错误码与人话原因，不许给替代数字（P-10）。

## 6. 失败与边界处理（写代码前先想清这五种）

| 场景 | 行为 | 错误码 |
|---|---|---|
| 指标不在册（含 VIF/SHAP 均值） | 返回能力清单 + 「这个口径还没有定义」 | `NOT_REGISTERED` |
| 参数超域（bins=7 / label=mob6） | 拒绝并给合法域 | `PARAM_OUT_OF_DOMAIN` |
| 窗口不满足表现期（3 天算 FPD7） | 拒绝 + `min_days` + 建议窗口 | `NOT_APPLICABLE` |
| 样本不足（分母 <30 / 每箱 <30） | 只给 n，不给值 | `VINTAGE_BLOCKED` |
| 明细请求 | 说清「明细不出域」+ 聚合替代 | `NOT_VISIBLE` |
| 越权维度 | 拒绝，不带任何数字 | `DENIED_DIMENSION` |
| 产品缺失/分流模糊 | 反问，不选默认 | `NEED_CLARIFY` |

## 7. 性能与容量（现在够用，写清边界）

- 夹具 2,000 单：单问（不含 LLM）P95 ≤ 1.5s；`compute_model_metric` 单次 ≤ 300ms。
- 生产 10^7 借据：`build_view` 的「取回内存再算」不可行 → 计算下推 SQL 或预聚合宽表（S5 后单独立项，不进本阶段）。
- 物化表按 `as_of` 分区；`find_materialized` 走 `(metric_id, signature, as_of)` 索引。
