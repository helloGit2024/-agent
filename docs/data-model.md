# 数据模型（S1—S5 新增 + 现有）

> 命名：表名小写下划线；时间一律 `TEXT ISO8601`（SQLite 兼容）→ PG 上改 `timestamptz`。
> 所有新表都带 `created_at`；**不允许硬删除**（软删 `deleted_at` 或 `status`）。

## 1. 现有表（不动，只作参照）

| 表 | 主键 | 作用 | 备注 |
|---|---|---|---|
| `applicant` | `apply_id` | 申请人（含 `query_3m/device_linked/dti/...`） | 夹具来源 |
| `policy_eval_fact` | `(run_id, apply_id, stage, policy_id)` | 逐环节策略判定事实 | **粒度收敛的验收对象**（复制一份率指标须不变） |
| `loan_perf` | `apply_id` | 放款台账（`fpd7/fpd30/mob3/bad_flag/loan_date`） | 表现期来源 |
| `model_score` | `(model_id, apply_id)` | 模型分（`model_score_v1`） | `score_col` 不出域 |
| `model_health_daily` | `(job_id, as_of, dimension, score_bin)` | 模型级物化 | 含 `base_dist / signature / param_version` |
| `policy_monitor_daily` | `(job_id, as_of, dimension)` | 策略监控物化 | — |
| `audit_chain` | `id` | 哈希链审计 | 一条断全链断 |

## 2. 新表：记忆（§10，S3）

```sql
CREATE TABLE memory_items (
  mem_id      TEXT PRIMARY KEY,          -- sha1(session_id|kind|hash(content))
  namespace   TEXT NOT NULL,             -- "risk_user:u_001" | "product:P_CASH_001"
  kind        TEXT NOT NULL,             -- preference | episodic | procedural
  content     TEXT NOT NULL,             -- JSON，按 kind 校验 schema
  source      TEXT NOT NULL,             -- explicit | inferred
  confidence  REAL NOT NULL DEFAULT 1.0, -- inferred 且 <0.6 不入库
  created_at  TEXT NOT NULL,
  expires_at  TEXT,                      -- 偏好 +90d / 情景 +30d
  deleted_at  TEXT,                      -- 软删除
  schema_ver  INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX ux_mem_idem ON memory_items(mem_id);
CREATE INDEX ix_mem_ns ON memory_items(namespace, kind, deleted_at);

CREATE TABLE memory_audit (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  mem_id    TEXT NOT NULL,
  action    TEXT NOT NULL,               -- write | delete | expire
  actor     TEXT NOT NULL,               -- system:writer | user:<id>
  detail    TEXT,                        -- 不存内容原文，只存 hash 与字段名
  ts        TEXT NOT NULL,
  prev_hash TEXT, hash TEXT NOT NULL      -- 与 audit_chain 同款链式
);
```

**content 的三种 schema（写入前校验，不合规拒收）**

| kind | 允许字段 | 例子 |
|---|---|---|
| `preference` | `default_product?, default_window?, focus_metrics?[]` | `{"default_product":"P_CASH_001"}` |
| `episodic` | `question_summary, intent, conclusion_label, session_id, at` | `{"question_summary":"问过S3_CR通过率","conclusion_label":"未计算"}` |
| `procedural` | `common_path[], uses_clarify: bool` | `{"common_path":["STATS"],"uses_clarify":true}` |

> `episodic` 的 `conclusion_label` 只许是**标签**（如「下降」「未计算」「样本不足」），**不许是数字**——数字进记忆就会绕过 `verify_numbers`（§10.1 红线）。

## 3. 新表：调用审计（§12，S1/S2）

> `llm_call_log` 已实现（S1 T-207），字段以 `core/db.py` 为准；`tool_call_log` 将在 S2 T-303 接入。

```sql
CREATE TABLE llm_call_log (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id      TEXT NOT NULL,
  purpose         TEXT NOT NULL,          -- plan | narrate | judge
  provider        TEXT NOT NULL,
  model           TEXT NOT NULL,          -- 具体版本，禁浮动别名
  temperature     REAL NOT NULL DEFAULT 0,
  seed            INTEGER,
  prompt_version  TEXT NOT NULL,
  schema_hash     TEXT NOT NULL,
  max_tokens      INTEGER NOT NULL,
  tokens_in       INTEGER, tokens_out INTEGER,
  latency_ms      INTEGER NOT NULL,
  output_hash     TEXT NOT NULL,          -- 不存原文，防敏感信息落日志
  status          TEXT NOT NULL,          -- ok | schema_fail | timeout | budget_exhausted | denied
  error_code      TEXT,
  ts              TEXT NOT NULL
);
CREATE INDEX ix_llm_sess ON llm_call_log(session_id, ts);

CREATE TABLE tool_call_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id  TEXT NOT NULL,
  caller      TEXT NOT NULL,              -- cli | mcp:<client> | scheduler
  tool        TEXT NOT NULL,
  args_hash   TEXT NOT NULL,              -- 原文不落库
  args_redacted TEXT,                     -- 仅枚举值，如 {"bins":5,"label":"fpd30"}
  rows        INTEGER,
  latency_ms  INTEGER,
  status      TEXT NOT NULL,              -- ok | TIMEOUT | DENIED | NOT_FOUND
  materialized TEXT,                      -- MATERIALIZED | MEASURED | null
  as_of       TEXT,
  ts          TEXT NOT NULL
);
```

## 4. 新表：外卸与评估（§11/§13，S3/S5）

```sql
CREATE TABLE artifacts (
  ref_id      TEXT PRIMARY KEY,           -- 如 ref_20261109_7f3a
  session_id  TEXT,
  kind        TEXT NOT NULL,              -- table | curve | rows | trace
  path        TEXT NOT NULL,              -- artifacts/ref/<ref_id>.json
  bytes       INTEGER NOT NULL,
  sha256      TEXT NOT NULL,
  n_rows      INTEGER,
  summary     TEXT,                       -- 一句话摘要，进上下文
  created_at  TEXT NOT NULL,
  expires_at  TEXT
);

CREATE TABLE eval_runs (
  run_id        TEXT NOT NULL,
  case_id       TEXT NOT NULL,            -- q1-v3-s2
  plan_json     TEXT,
  book_digest   TEXT,
  answer_digest TEXT,
  evals         TEXT,                     -- JSON: {ev01:pass,...}
  plan_source   TEXT,                     -- llm | rules
  degrade       TEXT,                     -- D0..D4
  tokens_in     INTEGER, tokens_out INTEGER,
  latency_ms    INTEGER, cost_cny REAL,
  created_at    TEXT NOT NULL,
  PRIMARY KEY (run_id, case_id)
);

CREATE TABLE trace_fixtures (
  case_id       TEXT PRIMARY KEY,
  plan_json     TEXT NOT NULL,            -- 固化计划，重放不需要模型
  data_seed     INTEGER NOT NULL,
  expected      TEXT NOT NULL,            -- {book_digest, answer_digest}
  tools         TEXT,                     -- [{name,args,result_digest}]
  model         TEXT, prompt_version TEXT,
  created_at    TEXT NOT NULL
);

-- ★以下两张表已实现（S0.9 T-105，scheduler/promotion.py），字段以代码为准
CREATE TABLE promotion_queue (
  signature   TEXT PRIMARY KEY,           -- metric_id + 归一化参数 + as_of 的哈希
  metric_id   TEXT NOT NULL,
  params      TEXT NOT NULL,              -- 归一化参数 JSON（人可读，便于人工审）
  hits        INTEGER NOT NULL DEFAULT 0, -- 被**真实提问**命中的次数
  first_seen  TEXT NOT NULL,
  last_seen   TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'pending', -- pending | promoted | dismissed
  job_id      TEXT,                       -- 升格后指向的调度任务
  note        TEXT                        -- 人工审批意见（为什么批/为什么不批）
);

CREATE TABLE job_state (
  job_id     TEXT PRIMARY KEY,
  status     TEXT NOT NULL,               -- active | retired
  retired_at TEXT,
  note       TEXT
);
```

> **`hits` 只由工具壳累加**，定时任务不许调 `record_question` —— 否则每个 job 跑 5 天就把自己升格了，队列立刻失去意义。
>
> **`pending → promoted` 不自动建表**：建 job 是变更（多一张生产表、多一个调度、多一份口径），队列只提交候选，批不批由人定（`mark_promoted`）。
>
> **`job_state` 是状态不是数据**：`retire_job` 只写 `status='retired'`，`find_materialized` 与 `run_job` 读到就跳过（返回 `JOB_RETIRED`），历史表**保留**——报告还要能引用当时那张表对账。`activate_job` 可恢复。

## 5. 状态机（三处，别写错）

**记忆项**：`active → (expired) → 不进 apply 但保留`；`active → deleted_at 置位 → 保留 → (retention 到期) → 物理删除`

**job**（`job_state.status`）：`active → retired`；`retired` 不参与 `find_materialized`、`run_job` 直接返回 `JOB_RETIRED`，历史表保留；`retired → active` 可恢复

**升格**（`promotion_queue.status`）：`pending（hits 累加中）→ hits ≥ BTA_PROMOTE_AFTER(5) → should_promote=True → promoted（人工批，绑定 job_id）| dismissed（明确不物化，之后不再进队）`

> 三处状态都不做物理删除；`retired` / `dismissed` 都是终态标记，可被反向操作覆盖。

## 6. 迁移与兼容

| 项 | 现在 | 迁移方式 |
|---|---|---|
| SQLite → PG | `core/db.Database` 抽象 | S3 加 `core/db_pg.py`，接口不变；`SCHEMA` 拆 `schema_sqlite.sql` / `schema_pg.sql` |
| `model_health_daily.base_dist` | TEXT(JSON) | PG 上 `jsonb`，读侧统一 `json.loads` |
| 已有表 | 不改列 | 只加表；加列必须可空 + 有默认值 |

**验收**：同一份夹具在 SQLite 与 PG 上跑 `make gate`，84 条断言结果逐位一致（`T-305`）。
