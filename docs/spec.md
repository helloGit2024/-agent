# 功能规格 · S1—S5（对应方案 §8—§13 六个模块）

> 本文件只回答「做到什么程度算完成」。**怎么做**见 `architecture.md`，**什么时候做**见 `tasks.md`。
> 每条 FR 的验收必须能落到 `make gate` 的某一条或某个评估器上——落不到的验收等于没写。

## 0. 起点与终点

| | 现状（S0.5） | 终点（S5） |
|---|---|---|
| 规划 | 规则解析（`intent.parse`） | LLM 规划 + 规则兜底，可切换、可对比 |
| 工具 | 进程内函数（11 个已登记） | 5 个只读工具走独立 MCP server；6 个计算工具留本地 |
| 记忆 | 无（`memory/` 是桩） | MemoryItem 受控写入 + 软删除 + 审计；跨会话偏好生效 |
| 上下文 | AnswerBook + 行数外卸 | token 预算表 + 三级外卸 + 历史压缩 |
| Harness | 5 道有落点，预算/降级未做 | 六道闸门全通 + D0—D4 + 调用审计 |
| 评估 | 18 评估器 + 84 断言（S0.9 后）+ 7 题基线 + D 组 11 项 | 140 用例 + trace-replay + Agent-as-Judge + 成本维度 |

## 1. 模块一 · Function Call（§8）

> S0.9 已交付：FR-101—107 全部落地，`make gate` 6/6、断言 84 条、D 组 11 项。下表「落点」列写的就是能验证它的那条门禁。

| ID | 需求 | 验收 | 落点 |
|---|---|---|---|
| FR-101 ✅ | `kind="curve"` 落地：`ks_curve` / `roc_curve` 只给摘要 + `ref_id`，逐点下采样后落 `artifacts/ref/` | 返回 `{summary:{ks_max,at_score,auc_area}, ref_id, n_points, n_points_out}`，`rows_inline=[]`，文件里的点数 == `n_points_out` | B[15] 曲线断言、D-1 |
| FR-102 ✅ | 「策略变量分」与「模型分」双 score 源：`score_source ∈ {model, policy}` | 同一批借据两种分算 KS 值不同且各自可复现；schema enum 只 2 值；进 `signature` | B[15]、D-8 |
| FR-103 ✅ | `ModelMetricDef` 补 `owner` + 定义哈希 `model_definition_hashes.json`，启动校验 | 改一个 `label_col` 后 `verify_definitions()` 即报错（与 `catalog.verify_definitions` 同款） | B[15]、D-6 |
| FR-104 ✅ | `min_sample` 可配置（`settings.MIN_SAMPLE`，夹具 5 / 生产 50）；定义里只存设计值 `DESIGN_MIN_SAMPLE=50` | `effective_min_sample() == settings.MIN_SAMPLE`；设 50 后演示库 KS 返回 `VINTAGE_BLOCKED` 只给 n | D-5、B[15] |
| FR-105 ✅ | 升格/下线：同 `(metric_id,params)` 在 `BTA_PROMOTE_WINDOW_DAYS`(30 天) 内实时问 ≥5 次 → `promotion_queue`；`retire_job` 后不再命中、不再执行 | 5 次同参提问入队；把 `last_seen` 拨到窗口外 → 计数回到 1；`JOB_RETIRED` | D-9、B[15] |
| FR-106 ✅ | 分流四态：`detect_state()` 给出 `model / ambiguous / aggregate / none`；`ambiguous` 必须反问 | 4 句探针状态可复现；「模型最近表现怎么样」→ `missing=["branch"]` 且给 2 选项，**不选默认** | D-3、D-4、B[15] |
| FR-107 ✅ | 11 工具统一走 `tools.registry.call`（`attribute_change/rank_policies/ask_user` 由编排层 `bind` 注入） | 未 bind → `NOT_APPLICABLE`，`DialogEngine(db)` 后齐备；`rank_policies` 与旧 `_exec_contribution` 逐位一致 | D-7、B[15] |

> 唯一留给后续的是 FR-105 的**自动**下线（"job 30 天零消费就自动 retired"）：判断"零消费"要按 job 统计调用次数，而 `tool_call_log` 是 S2 的东西。现在 `job_state` 的状态机与跳过逻辑已就位，S2 拿到调用日志后加一个 `idle_jobs(days)` 即可，不需要改表。

## 2. 模块二 · MCP（§9）

| ID | 需求 | 验收 |
|---|---|---|
| FR-201 | 独立进程 `mcp_server/policy_catalog.py`，暴露 5 个只读工具 | `make mcp` 起 stdio server；`mcp_server/smoke.py` 5 工具全通 |
| FR-202 | 只读账号 + `statement_timeout=3s` + 单次 ≤200 行（超出截断给 `truncated=true`） | 500 行查询 → 200 行 + 标记；慢查询 → `TIMEOUT` 而非空结果 |
| FR-203 | 调用审计 `(caller,tool,args_hash,rows,latency,status,ts)`，**不存 args 原文** | `tool_call_log` 有记录且无敏感原文；同参两次 `args_hash` 相同 |
| FR-204 | 失败三分类 `NOT_FOUND/TIMEOUT/DENIED`，提示语不同 | 三码各 1 条断言；文案不混用「不存在」与「超时」 |
| FR-205 | schema 带 `version`，新增字段不删旧字段 | 响应含 `schema_version`，旧字段全在 |
| FR-206 | 本地 5 个 MCP 工具改 client 调用，失败降级进程内函数并标 `source=local_fallback` | 关 server 后 10 题仍全绿；回答出现降级标记 |

## 3. 模块三 · 记忆（§10）

| ID | 需求 | 验收 |
|---|---|---|
| FR-301 | `MemoryItem` 完整结构（含 kind 三类、confidence、expires_at、deleted_at） | 建表 + 6 条单元断言（幂等键 / namespace 隔离 / 三类 content schema） |
| FR-302 | 只写 `explicit` 或 `confidence≥0.6`；敏感字段入库前脱敏，库里搜不到 `apply_id` | 写入含 `apply_id` 文本 → 落库后正则 0 命中 |
| FR-303 | 软删除 + 留痕：`deleted_at`，物理删除按 `retention_days` 延后；删除进 `memory_audit` 哈希链 | 删除后 `of(role)` 不返回该项但可查 `deleted_at`；`verify_chain()` 通过 |
| FR-304 | 有效期：偏好 90 天 / 情景 30 天；过期不参与 `apply()` 但保留 | `expires_at` 改昨天 → `apply()` 不填该槽 |
| FR-305 | `apply()` 接入编排，补全后回答必须有「记忆补全：…」声明 | 设偏好后问「通过率」→ 含声明行；`ev19` 通过 |
| FR-306 | 两档后端：`LangMemAdapter`（默认）/ `ZepAdapter`（合规），同一 `MemoryStore` 接口 | 两适配器跑同一套 20 条契约测试全绿；切换只改配置 |
| FR-307 | 记忆永不进 AnswerBook/信封；口径字段永久禁止 | `FORBIDDEN` 8 项逐个返回 `NOT_APPLICABLE` |

## 4. 模块四 · 上下文工程（§11）

| ID | 需求 | 验收 |
|---|---|---|
| FR-401 | `core/context_budget.py`：系统 1200 / schema 1500 / 记忆 300 / 历史 1500 / 单次工具 800 / 累计 4000 / 叙述 1500 / 总计 8000 | 8 条单测；超限按规则裁剪且**系统指令永不裁** |
| FR-402 | 外卸双条件：行数 ≤8 **且** 估算 token ≤800，任一超限即外卸 | 3 行 × 20 列大表也触发；`ref_id` 可回查 |
| FR-403 | `summarize_ref(ref_id, mode, n, order_by)`：前 n 行 / 按列 top n / 只取数组 | 三模式各 1 条断言；二次外卸守同一阈值 |
| FR-404 | 历史压缩：最近 3 轮全文 + 更早只留三元组；摘要规则生成不用 LLM | 8 轮会话 → 第 4 轮起只剩 `(问句摘要,意图,结论标签)` |
| FR-405 | 接近 8000 → 提前收尾，用已有结果作答并声明「未穷尽」 | 超长会话 → 含「未穷尽」且不报错 |

## 5. 模块五 · Harness（§12）

| ID | 需求 | 验收 |
|---|---|---|
| FR-501 | 闸门 1：`token→角色→租户`，`access_policy` 编译成 scope；请求预算分配 | 无 token 返回 `DENIED` 且不含「查不到」；超预算被拒并说明 |
| FR-502 | 闸门 4：工具 ≤8 次、单轮 ≤8k token、单问 ≤30s、连续 3 次失败熔断 | 9 次调用第 9 次被拒并收尾；连续失败后新问题直接「已转人工」 |
| FR-503 | 出口闸门硬回退：未登记数字 → **丢弃叙述**回退模板 | 喂含假数字叙述 → 输出为模板版，trace 记 `exit_gate=rollback` |
| FR-504 | `llm/call.py` 统一出口：provider 无关；`temperature=0/seed/schema_hash`；失败重试 1 次再降级 | 4 条单测；全库 grep 不到 `import openai` |
| FR-505 | D0—D4 显式实现，每级有可观测标记 | 人为触发四级各 1 次，回答可见标记与文案 |
| FR-506 | 闸门 6：LLM/工具调用全量入 `llm_call_log`/`tool_call_log`；写审计失败 = 业务失败 | 关审计写入后请求直接失败 |

## 6. 模块六 · 测试与评估（§13）

| ID | 需求 | 验收 |
|---|---|---|
| FR-601 | 140 用例：4 题 × 7 变体 × 5 seed，`case_id` 稳定 | `evals/golden_set.yaml` 生成 140 条；id 唯一 |
| FR-602 | 每用例记计划 + 轨迹 + book 摘要 | `eval_runs` 含 `plan_json/book_digest/answer_digest/evals` |
| FR-603 | trace-replay：`fixtures/traces/<case_id>.json` 固化 plan+seed+expected；重放**不调 LLM** | `make replay` 秒级 140 条 0 差异；改坏数字被抓 |
| FR-604 | Agent-as-Judge 按 §13.3 契约，只判三件事，结构化输出 | 3 条注入违规全被抓；judge 不参与规则评估器计分 |
| FR-605 | 三级门禁 A/B/C，按层统计失败 | `make gate` 按 A/B/C 分组；C 失败退出码非 0 |
| FR-606 | 成本：每用例记 tokens/latency/cost，设单问上限 | 报告出成本分布；超限用例标红 |
| FR-607 | LLM 版 vs 规则版对照，LLM 输则标 `regression_suspect` | 故意变差 prompt → ≥1 条被标记 |

## 7. 全局非功能

| ID | 要求 | 验收 |
|---|---|---|
| NFR-01 | 确定性：同输入同输出 | 同问题连跑 3 次逐字节相同 |
| NFR-02 | 兼容：84 条断言与 7 题基线在 S1—S5 全程不变（除按 P-11 重录） | `make gate` 每次全绿 |
| NFR-03 | 单问延迟（不含 LLM）P95 ≤1.5s | 20 次采样断言 |
| NFR-04 | 数据不出域：`score_col/base_dist/明细行` 不进 MCP 响应与 prompt | 静态扫描：组装 prompt 处禁引 raw rows |
| NFR-05 | 可解释失败：任何失败都是 9 个错误码之一 + detail | 20 个失败路径采样，0 裸异常 |
