# 任务清单（S0.9 → S5）

> **一次只做一个 T-xxx**。做完 `make gate` 绿了再下一个。
> 调研任务（R-xxx）与开发任务可并行，但调研产出只进文档，不动代码（见 `research-semantic-layer.md`）。
> 规模用 S/M/L 表示改动面（S ≤50 行，M ≤200 行，L 跨 3+ 文件），不用小时估——估时永远不准，改动面不会骗人。
> 每个任务的「落点」是**允许改的文件**；改到清单外的文件，先在这里补一行。

## 阶段 S0.9 · 模块一收口（✅ 已交付：gate 6/6，断言 84）

| ID | 任务 | 落点 | 验收 | 依赖 | 规模 |
|---|---|---|---|---|---|
| T-101 | `kind="curve"`：`ks_curve` / `roc_curve` 两个指标，只回摘要 + `ref_id` | `core/model_metrics.py`、`core/stats.py`（加 `curve_points`）、`tools/compute_model_metric.py` | 返回含 `summary{ks_max,at_score,auc_area}` 与 `ref_id`；`artifacts/ref/` 有文件；+2 断言 | — | M |
| T-102 | 双 score 源：`score_source ∈ {model,policy}` | `model_metrics.py`（`_assemble` 分支）、`v1_backtest.py`（导出策略分函数） | 同批借据两种分 KS 不同且各自可复现；enum 只 2 值 | T-101 | M |
| T-103 | `owner` + 模型级定义哈希 | `model_metrics.py`、`model_definition_hashes.json`（新建） | 改 `label_col` 后启动报错；`verify()` 含该检查 | — | S |
| T-104 | `min_sample` 可配置 | `core/settings.py`（新建）、`model_metrics.py` | `MIN_SAMPLE=50` → 演示库返回 `VINTAGE_BLOCKED` 只给 n；改回 5 恢复 | — | S |
| T-105 | 升格队列 + job 下线 | `scheduler/promotion.py`（新建）、`data-model` 的 `promotion_queue` / `job_state` | 5 次同参提问后入队（窗口 `BTA_PROMOTE_WINDOW_DAYS`=30 天，窗口外从 1 重数）；`retired` 不被命中、`run_job` 返 `JOB_RETIRED` | — | M |
| T-106 | 分流四态 `ambiguous` + 反问 | `model_metrics.detect_state`（三态 → 四态）、`intent._parse_model_diag` / `_parse_ambiguous`、`build_clarify` | 4 句探针状态可复现；模糊问句全返 `NEED_CLARIFY slot=branch`，不选默认 | — | M |
| T-107 | 11 工具统一 `registry.call` | `tools/registry.py`、`v2_dialogue._exec_*`（改为调 registry） | `rank_policies` 输出与旧 `_exec_contribution` 逐位一致 | — | M |
| T-108 | `make gate` 加 D 组（语义一致性） | `ci/gate_all.py` | 输出 6 组；D 组 0 失败 | T-101—107 | S |

**S0.9 出口**：`make gate` 6/6（D 组 11 项）；断言 77 → 84（含曲线口径一致、分源签名、定义哈希、阈值配置、四态反问、升格下线）。

> 已交付记录（2026-11-09）：T-101—108 全部完成。补齐时发现并修掉两处「单看都对、合起来错」：① 曲线 `ks_max` 按 ROC 累计算会与标量 `ks` 差 0.04（并列处分母不同）→ `curve_points` 走两遍；② 出口闸门把 `ref_id` 里的日期当未登记数字，导致**每条**曲线回答都报「溯源 ✘」→ 剥掉 `ref_\d{8}_[0-9a-f]{4}`，并加 D-11 钉住。

## 阶段 S1 · LLM 规划接入（不接叙述）

| ID | 任务 | 落点 | 验收 | 依赖 | 规模 |
|---|---|---|---|---|---|
| T-201 ✅ | `llm/call.py` 统一出口 + provider 适配 | `llm/call.py`、`llm/providers/__init__.py`+`qwen.py`（新建）、`selftest_v2.py`（§16 +4 断言）、`ci/gate_all.py`（计数 84→88） | 4 条单测（正常/重试/超时/schema 不过）；全库 grep 无 `import openai` | — | M |
| T-202 ✅ | `llm/planner.py`：prompt → plan → `validate_plan` | `llm/planner.py`（原 `plan.py`，T-203 重命名）、`llm/context.py`（均新建）、`selftest_v2.py`（§17 +4 断言）、`ci/gate_all.py`（计数 88→92） | 10 题里 8 题 LLM 计划与规则计划 `intent/scope` 一致 | T-201 | M |
| T-203 ✅ | `BTA_LLM` 开关 + D2 降级 | `llm/__init__.py`、`llm/planner.py`（由 `llm/plan.py` 重命名：同名子模块会遮蔽 `llm.plan()` 函数）、`v2_dialogue.n_understand`、`selftest_v2.py`（§18 +3 断言）、`ci/gate_all.py`（计数 92→95） | 关开关行为与 S0.9 逐字节一致（回归基线过）；开开关时 LLM 失败 → D2 回退规则版且可观测 | T-202 | S |
| T-204 ✅ | 闸门 ② 计划校验增强：跨册引用直接拒 | `v2_dialogue.validate_plan_v2`、`selftest_v2.py`（§19 +2 断言）、`ci/gate_all.py`（计数 95→97） | 造 `branch=model` + `compute_metric` → 拒绝且不改写 | T-202 | S |
| T-205 ✅ | 闸门 ④ 预算/熔断 | `core/admission.py`（新建）、`llm/__init__.py`（plan/narrate 接 admission）、`v2_dialogue.DialogEngine`（持有 Admission 并在 ask() 入口查熔断）、`selftest_v2.py`（§20 +3 断言）、`ci/gate_all.py`（计数 97→100） | 9 次调用第 9 次被拒；连续 3 失败熔断 | T-203 | M |
| T-206 ✅ | 准入闸门（token→角色→scope） | `core/admission.py`、`selftest_v2.py`（§21 +5 断言）、`ci/gate_all.py`（计数 100→105） | 无 token → `DENIED` 且文案不含「查不到」 | T-205 | M |
| T-207 ✅ | `llm_call_log` 落库 + 审计失败即失败 | `core/db.py`、`llm/call.py`、`llm/__init__.py`、`llm/planner.py`、`v2_dialogue.py`、`selftest_v2.py`（§22 +4 断言）、`ci/gate_all.py`（计数 105→109）、`docs/data-model.md` | 关审计写入 → 请求失败 | T-201 | S |
| T-208 ✅ | LLM 版 vs 规则版对照跑（先不评分） | `eval/compare_plans.py`（新建）、`selftest_v2.py`（§23 +2 断言）、`ci/gate_all.py`（计数 109→111）、`docs/00-index.md` | 输出 10 题两边计划差异表 | T-202 | S |

**S1 出口**：`BTA_LLM=on` 10 题全通；`BTA_LLM=off` 回归基线逐字节不变；D2 可观测。

> 已验收（2026-11-10）：`BTA_LLM=on` 10 题全部有答案、信封 4 行齐全、`verify_numbers` 全过；按请求独立预算时 10/10 走 LLM 计划。同一进程连跑 10 题时第 9、10 题因全局 `max_calls=8`（T-205 FR-502）降级 D2 并在 trace 标记 `degrade_reason=budget` —— 这是设计行为，不是 LLM 失败（`llm_call_log` 8 条均为 `ok`）。

## 阶段 S1.5 · 语义层对标调研（与 S1 并行，S2 软门槛）

> 详见 `docs/research-semantic-layer.md`。调研产出**只进文档**（`docs/research/`、`docs/adr/`），
> 要动代码必须先立 T-xxx 走门禁——不允许"看源码看得手痒顺手改"。

| ID | 任务 | 产出 | 验收 | 依赖 | 规模 |
|---|---|---|---|---|---|
| R-001 | 跑通 SuperSonic + 读语义解析源码 | `research/supersonic-notes.md` | Docker 起服务；3 个语义问题实测（含 1 个模型里没有的）；源码三处精读各含"偷/不偷"结论 | 与 S1 并行 | L |
| R-002 ✅ | 精读 WrenAI MDL + wren-core 校验管线 | `research/wrenai-notes.md` | MDL ↔ `MetricDef` 字段映射表 + "我们缺的字段"清单 | 与 S1 并行 | M |
| R-003 ✅ | Cube 代码级细节补课 | `research/cube-notes.md` + `adr/ADR-006-draft.md` | joins / available_dimensions / pre-aggregation 三处源码笔记；落 ADR-006 草稿（join path 建模，写明"为何选人工审批而非自动 pre-agg"） | — | S |
| R-004 ✅ | Snowflake / 帆软公开资料精读 | `research/closed-source-notes.md` | Cortex Analyst 拒答文案对照 `NOT_REGISTERED` 给 1 条建议或明确不改；帆软指标树与红黄绿自动化分级各 1 条风控启示 | — | S |

**S1.5 出口**：四份笔记 + 吸收清单 5 条逐条有归属（吸收→挂任务 / 拒绝→写明理由 / 待定→挂 OPEN-QUESTIONS）+ ADR-006 草稿；T-306/T-307 开工前必须收口。

> R-001 ✅：源码三处精读 + 3 题实测已完成（`docs/research/supersonic-notes.md`）。
> 关键实测证据：模型里没有的「退款率」在 STRICT 下 `FAILED`，切到 LOOSE 后被就近解释成「访问次数+访问用户数」并返回 `COMPLETED`——放宽匹配 = 拿别的指标冒充答案，正是 P-05/P-10 要堵的。
>
> R-002 ✅：mdl 契约 + 校验管线源码精读已完成（`docs/research/wrenai-notes.md`，锁定 `bc2b06a1950a`）。
> 关键结论：① 我们的册子比 MDL 更完备（它有 expression，缺分子/分母、lag、最小样本、适用性），
> 所以产出是缺口清单而不是照抄；② wren-core 的"权限=看不见"与本项目 `semantic_contract` 一致，
> 但它的 optional ACL 缺失时**静默不过滤**（fail-open），这条不采纳；③ 缺口 3 条（G-1 行过滤字段声明 /
> G-2 指标依赖 + DAG 环检测 / G-3 下钻顺序）与 3 条候选改动均需先立任务才能动代码。
>
> R-004 ✅：Snowflake Cortex Analyst + 帆软公开资料精读已完成（`docs/research/closed-source-notes.md`，访问日期 2026-11-11，**未实测**）。
> 关键结论：① Cortex 没有"指标未注册"一等状态，未命中时返回的是"语义模型能出 SQL 的问法"而不是 id 全集；
> 据此建议把 `NOT_REGISTERED` 带回的 `registered=sorted(CATALOG)` 改为**按角色过滤 + 换成 `typical_questions`**
> （现状会让 `business_viewer` 通过错误信息反查出未授权指标 id，绕开 `NOT_VISIBLE` 的措辞红线），已立 T-608（见下方「吸收项落地」）；
> ② 帆软指标树的前提是"指标资产 + 血缘"，树边必须是册子声明的恒等式——挂 R-002 G-2（`depends_on`），补齐前不建树；
> ③ 帆软"红黄绿"公开依据只到**展示层条件格式 + 单阈值预警**，"三级自动分级"**无公开依据**；
> 风控启示是阈值进册子（`healthy_range` 已有但回答层未用），与吸收清单 ⑤ 合并成立一个 T-xxx；
> ④ 不吸收：Cortex 的自然语言拒答边界（不可断言）、帆软"未命中指标继续找路径"（与 P-10 冲突）。
>
> R-003 ✅：Cube 三处源码精读 + ADR-006 草稿已完成（`docs/research/cube-notes.md`、`docs/adr/ADR-006-draft.md`，锁定 `e6c64f6dedb59142c9d8e72466915ad72734b294`）。
> 关键结论：① joins 是 schema 显式声明 `relationship` + 编译期建图（有乘法测度必须校验对端主键）+ 查询期最短路，无路抛 `JoinPathNotFoundError`；
> 多路径靠显式 `joinPath`/`joinHints` 消歧——可偷「路径身份 + 基数校验」，不偷 `sql` 函数与 `hasMany`；
> ② 服务端**没有** `available_dimensions` 字段；同名 `availableDimensions` 只出现在前端 SDK（`Meta.membersForQuery` 及 React/Vue 组件，且**忽略 query**）；
> 真实机制是 cube 成员 + view `includes/joinPath` + meta `isVisible` + `accessPolicy.memberLevel` 四层叠加，
> 对应我们「多份维度白名单（可聚合四份 + 模型级两处）」的缺口——应改为「一份声明 + 启动期从 `JOINS` 推导 + 三处只读投影」；
> ③ OSS 的 `autoRollup` 直接抛 "Enterprise only"，匹配按 schema 声明顺序 first-match，故 ADR-006 选「人工审批路径/物化」，
> 拒绝自动 pre-aggregation；风险与应对按越权（调用后判权、零数字）、口径漂移（`path_id` + 定义哈希、多路径报 `AMBIGUOUS_JOIN_PATH`）、
> 可溯源（信封/trace 带 `path_id/grain/cardinality/rollup_id`）三组展开。
> 本次只产出文档，未改 `core/`、`tools/`，未做 git commit / push。

### 吸收项落地（R-004 产出，须在 S2 对外暴露前完成）

> 编号按全表当前最大值 +1 分配（T-607 → T-608），不代表所属阶段；执行顺序以「依赖」列为准。

| ID | 任务 | 落点 | 验收 | 依赖 | 规模 |
|---|---|---|---|---|---|
| T-608 | `NOT_REGISTERED` 能力清单按角色过滤 + 换成"能答的问法"：堵住用错误信息反查未授权指标 id 的枚举面（现状 `guard_call` 在判定角色前返回 `registered=sorted(CATALOG)`） | `core/catalog.py`（`guard_call` 的 `NOT_REGISTERED` 分支，L699—703）、`tools/registry.py`（`get_metric_definition` 同名分支，口径对齐）、`selftest_v2.py`（+2 断言）、`ci/gate_all.py`（计数更新）、`eval/fixtures/demo_v22_baseline.json`（仅按 P-11 重录时） | 正向：`risk_analyst` 问未注册口径 → 示例全部来自其可见指标，取 `MetricDef.typical_questions` 首条，缺省回退 `name`，不留空。反向：`business_viewer` 问未注册口径 → 响应（message / registered / suggestions）不出现 `fpd30_rate`、`mob3_bad_rate`、`loan_amount` 等未授权 id，且不出现任何指标数值。**边界**：错误码枚举、零数字、现有"这个口径还没有定义（不在册）"文案不变；越权仍走 `NOT_VISIBLE`，不得借"给示例"变相透露未授权指标。改文案/信封先跑 `make regress`，按 P-11 重录基线并写明原因与影响题号 | R-004；先于 T-301/T-305（MCP 对外暴露前） | S |

## 阶段 S2 · MCP server

| ID | 任务 | 落点 | 验收 | 依赖 | 规模 |
|---|---|---|---|---|---|
| T-301 | `mcp_server/policy_catalog.py` 5 工具 | 新建目录 | `make mcp` 起 server；`smoke.py` 5 工具全通 | — | M |
| T-302 | 只读 + 3s 超时 + 200 行截断 | `policy_catalog.py` | 500 行 → 200 + `truncated`；慢查询 → `TIMEOUT` | T-301 | S |
| T-303 | 审计落 `tool_call_log` | `policy_catalog.py`、`db.py` | 有记录、无原文；同参 `args_hash` 相同 | T-301 | S |
| T-304 | 失败三分类文案 | `policy_catalog.py` | 3 码各 1 断言；文案不混用 | T-301 | S |
| T-305 | 本地 5 工具改 client + `local_fallback` | `tools/registry.py` | 关 server 后 10 题全绿；回答有降级标记 | T-301 | M |
| T-306 | CI 门禁 3：`mcp_server/` 不许 import `model_metrics` | `ci/check_imports.py` | 故意加一行 → 门禁失败 | T-305 | S |
| T-307 | PG 兼容验证 | `core/db_pg.py`（新建） | SQLite 与 PG 上 77+ 断言逐位一致 | — | L |

**S2 出口**：`BTA_MCP=off/on` 两条路径都全绿。

## 阶段 S3 · 记忆 + 上下文工程

| ID | 任务 | 落点 | 验收 | 依赖 | 规模 |
|---|---|---|---|---|---|
| T-401 | `MemoryItem` + `memory_items` / `memory_audit` 建表 | `memory/store.py`、`data-model` DDL | 6 条单元断言（幂等键/namespace/三类 content） | — | M |
| T-402 | 写入策略：explicit / confidence≥0.6 / 脱敏 | `memory/writer.py` | 含 `apply_id` 文本落库后正则 0 命中 | T-401 | M |
| T-403 | 软删除 + 有效期 + 哈希链 | `memory/store.py` | 删除后不可见但可查；过期不参与 apply；`verify_chain()` 通过 | T-401 | M |
| T-404 | `apply()` 接入编排 + 「记忆补全」声明 | `v2_dialogue.n_understand`、`_render` | 设偏好后问「通过率」→ 含声明；新增 `ev19` | T-402 | S |
| T-405 | 两适配器 + 20 条契约测试 | `memory/adapters/*`、`memory/contract_test.py` | 两档全绿；`BTA_MEMORY=off` 行为不变 | T-401 | L |
| T-406 | `core/context_budget.py` 预算表 | 新建 | 8 条单测；系统指令永不裁 | — | M |
| T-407 | 外卸双条件（行数/token）+ `summarize_ref` | `tools/compute_model_metric._shape`、`tools/summarize_ref.py` | 3×20 宽表也外卸；三模式各 1 断言 | T-406 | M |
| T-408 | 历史压缩（3 轮全文 + 三元组） | `memory/store.ShortTerm` / `llm/context.py` | 8 轮会话第 4 轮起只剩三元组 | T-406 | S |
| T-409 | 「未穷尽」收尾 | `v2_dialogue.n_narrate` | 超长会话含声明且不报错 | T-406 | S |

**S3 出口**：记忆两档 + 预算 8 单测 + 外卸双条件全绿；`make gate` 含新组。

## 阶段 S4 · LLM 叙述 + 出口回退

| ID | 任务 | 落点 | 验收 | 依赖 | 规模 |
|---|---|---|---|---|---|
| T-501 | `llm/narrate.py`：只吃槽位 + refs | 新建 | 输入签名里无 rows（代码评审 + 类型） | T-202 | M |
| T-502 | 出口闸门硬回退 | `v2_dialogue.n_narrate` | 喂假数字 → 输出模板版；trace `exit_gate=rollback` | T-501 | M |
| T-503 | D0—D4 显式标记与文案 | `v2_dialogue`、`core/codes.py` | 四级各触发 1 次，回答可见标记 | T-501 | M |
| T-504 | 模板与 LLM 版对照（人读 10 题） | `eval/compare_narrate.py`（新建） | 10 题差异表 + 结论「LLM 版是否更好」写进报告 | T-502 | S |

**S4 出口**：假数字 100% 被拦；D1 触发可见；10 题人工对比通过。

## 阶段 S5 · 测试与评估体系

| ID | 任务 | 落点 | 验收 | 依赖 | 规模 |
|---|---|---|---|---|---|
| T-601 | 140 用例生成器 | `evals/golden_set.yaml`、`eval/gen_cases.py` | 140 条、`case_id` 唯一稳定 | — | M |
| T-602 | `eval_runs` 落库 + 指标聚合 | `eval/runner.py`（新建）、`db.py` | 每条含 plan/book_digest/evals/cost | T-601 | M |
| T-603 | trace-replay | `eval/replay.py`、`fixtures/traces/` | `make replay` 140 条 0 差异、不调 LLM | T-602 | M |
| T-604 | Agent-as-Judge 契约 | `eval/judge.py` | 3 条注入违规全被抓；不参与规则计分 | T-602 | M |
| T-605 | 三级门禁 A/B/C | `ci/gate_all.py` | 按 A/B/C 分组；C 失败非 0 退出 | T-603 | S |
| T-606 | 成本维度 + 上限 | `eval/runner.py` | 成本分布可出；超限标红不阻断 | T-602 | S |
| T-607 | LLM vs 规则对照标记 | `eval/compare_plans.py` | 变差 prompt → ≥1 条 `regression_suspect` | T-601 | S |

**S5 出口**：`make gate` 三组全绿；`make replay` 秒级 140 条；成本报告可读。

## 依赖图（只画跨阶段硬依赖）

```
T-101..107 ─▶ T-108（D 组门禁）
      │
      ├─▶ T-202（规划要用 curve/双分）──▶ T-501（叙述要用计划）
      │
      ├─▶ T-301（MCP 与语义层无关，可并行；R-001..004 与 S1 并行）
      │
      ├─▶ T-608（`NOT_REGISTERED` 清单按角色过滤）⚠ 必须先于 T-301/T-305 对外暴露
      │
      └─▶ T-306/T-307 ⚠ 开工前 R-001..004 必须收口（S1.5 出口）
T-401..405 ─▶ T-404（记忆接入编排，需要稳定 n_understand）
T-406..409 ─▶ T-501（叙述要用预算器）
T-601..607 依赖前面全部（评估要跑完整链路）
```

## 每个任务的固定动作（别省）

1. 开工前：`make gate` 确认起点全绿（不绿先修，不在红上叠加改动）。
2. 改完：`make gate`；涉及渲染/意图/信封再单跑 `make regress`。
3. 新增断言：至少 1 条正向 + 1 条反向（改坏必须被抓）。
4. 提交信息：`T-xxx: 一句话` + 若重录基线，写明**原因与影响题号**（P-11）。
5. 回填本文件「状态」列（✅/⬜）与 `docs/00-index.md` 阶段表。
