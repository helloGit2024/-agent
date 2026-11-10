# 接口契约

> 三类契约：**工具 schema**（模型能调什么）、**LLM JSON 契约**（模型必须回什么形状）、**MCP 契约**（跨进程能拿什么）。
> 铁律：所有 schema `additionalProperties: false`；参数只收枚举；任何 schema 里出现 `sql/expression/formula/table/column` 即视为缺陷（P-05）。

## 1. 工具清单（11 个，已实现 + 待实现）

| 工具 | 通道 | 分支 | 状态 | 说明 |
|---|---|---|---|---|
| `list_metrics` | MCP | both | ✅ | 只回 id/名称/是否有表现期；不含公式 |
| `get_metric_definition` | MCP | both | ✅ | 口径/负责人/版本；模型级另给 score_col 名（不给值） |
| `query_policy_flow` | MCP | aggregate | ✅ | 4 环节 + 12 策略 + 阈值 + 版本 |
| `lookup_field` | MCP | aggregate | ✅ | 精确匹配失败给 similar |
| `version_history` | MCP | aggregate | ✅ | 版本链，失效不删 |
| `get_model_health` | local | model | ✅ | 只读物化表；无命中明确说「无物化结果」，**不回落实时** |
| `compute_metric` | local | aggregate | ✅ | 率指标唯一入口壳 |
| `compute_model_metric` | local | model | ✅ | 模型级唯一入口壳，三级路由 |
| `attribute_change` | local | aggregate | ✅（编排层） | S0.9 迁入 `registry.call` |
| `rank_policies` | local | aggregate | ✅（编排层） | 同上 |
| `ask_user` | local | both | ✅（interrupt） | S0.9 统一签名 |
| `summarize_ref` | local | both | ⬜ S3 | 新增：外卸二次读 |

## 2. 计算工具 schema（冻结，改动需 ADR）

### 2.1 `compute_metric`

```json
{
  "name": "compute_metric",
  "description": "计算已注册的率指标（通过率/放款率/FPD7/FPD30/MOB3）。不接收 SQL 或表达式。",
  "parameters": {
    "type": "object",
    "additionalProperties": false,
    "required": ["metric_id"],
    "properties": {
      "metric_id": { "type": "string", "enum": ["approval_rate","stage_rates","policy_rates","fpd7_rate","fpd30_rate","mob3_rate"] },
      "params": {
        "type": "object",
        "additionalProperties": false,
        "properties": {
          "product":   { "type": "string", "enum": ["P_CASH_001","P_INSTALL_001","P_CR_001"] },
          "window":    { "type": "string", "pattern": "^(近[一二三四五六七八九十]+(天|周|个月)|20\\d{2}-\\d{2}(-\\d{2})?)$" },
          "dimension": { "type": "string", "enum": ["overall","product","channel","month","day"] }
        }
      }
    }
  }
}
```

> `metric_id` 的 enum **按角色裁剪**：未授权指标不进 enum（`semantic_contract._allowed`），不是「进了再拒」。

### 2.2 `compute_model_metric`

```json
{
  "name": "compute_model_metric",
  "description": "计算模型级指标（KS/AUC/Gini/IV/PSI/LIFT/WOE）。不接收切点、分数列表达式、label 定义式。",
  "parameters": {
    "type": "object",
    "additionalProperties": false,
    "required": ["metric_id"],
    "properties": {
      "metric_id": { "type": "string", "enum": ["ks","auc","gini","iv","psi","lift","woe_table","ks_curve","roc_curve"] },
      "params": {
        "type": "object",
        "additionalProperties": false,
        "properties": {
          "window":     { "type": "string" },
          "product":    { "type": "string" },
          "dimension":  { "type": "string", "enum": ["overall","product","channel","month","day"] },
          "bins":       { "type": "integer", "enum": [5,10,20] },
          "label":      { "type": "string", "enum": ["fpd7","fpd30","mob3"] },
          "ref_period": { "type": "string", "enum": ["train_2025Q4","train_2026Q1","last_month"] },
          "score_source": { "type": "string", "enum": ["model","policy"] }
        }
      }
    }
  }
}
```

> `bins=7` 会被 schema 拒（`PARAM_OUT_OF_DOMAIN`）；`score_source ∈ {model, policy}` 是双分源（`model_score_v1` 特征分 / `policy_score_v1` 策略 FAIL 占比），**必须进 `signature`**，否则两份结果会互相命中物化表。
>
> `ks_curve` / `roc_curve` 已实现（S0.9 FR-101）：`kind="curve"`，加进 enum 时已同步 `MODEL_CATALOG`、`ci/check_semantic.py` 与断言。曲线有几十上百点，**不回点**，只回 `summary{ks_max, at_score, auc_area}`，逐点外卸到 `artifacts/ref/ref_YYYYMMDD_xxxx.json`。

**三种返回形态（`kind` 决定给模型看多少）**

| kind | 内联 | 外卸 | 关键字段 |
|---|---|---|---|
| `scalar` | 值 + `ci95` + `n/bad_n/good_n` | — | `value` |
| `bins` | 前 8 行（`MAX_INLINE_ROWS`） | 超过 8 行才外卸 | `rows_inline[] / rows_total / summary` |
| `curve` | **0 行**（`rows_inline=[] rows_total=0 offloaded=true`） | 必外卸 | `summary{ks_max,at_score,auc_area}` + `n_points`（不同分值数）/ `n_points_out`（抽稀后落盘点数）+ `ref_id` |

> 曲线**必须** `rows_inline=[]`：把点塞回去等于外卸白做，而且模型会把 100 个坐标当 100 条事实逐点复述（登记簿爆炸）。
>
> `n_points` 与 `n_points_out` 必须都给：只说"给了曲线"不说它被抽稀过，画图的人会把折线上的拐点当成全量拐点。抽稀只影响画图，不影响 `ks_max/at_score/auc_area`（三个都用全量点算）。
>
> 每次实时计算还会返回 `promotion:{hits, threshold, window_days, should_promote}`（该签名在窗口内被真实提问几次、阈值 5、窗口 30 天、是否该进升格队列）；遥测故障不拖垮回答，改回 `promotion_error`。

### 2.3 `summarize_ref`（新，S3）

```json
{
  "name": "summarize_ref",
  "parameters": {
    "type": "object", "additionalProperties": false,
    "required": ["ref_id","mode"],
    "properties": {
      "ref_id":   { "type": "string", "pattern": "^ref_[0-9]{8}_[0-9a-f]{4}$" },
      "mode":     { "type": "string", "enum": ["head","top","columns"] },
      "n":        { "type": "integer", "minimum": 1, "maximum": 20 },
      "order_by": { "type": "string", "enum": ["bin","n","bad_rate","woe","iv_contrib","lift","cum_ks"] },
      "columns":  { "type": "array", "items": { "type": "string" }, "maxItems": 6 }
    }
  }
}
```

## 3. LLM 三种 JSON 契约

### 3.1 规划（`purpose=plan`，S1）

```json
{
  "intent": "STATS | MODEL_DIAG | ANOMALY | CONFIG | CONTRIBUTION | FIELD | CLARIFY | DENY",
  "branch": "aggregate | model | none",
  "scope": { "product": "P_CASH_001 | null", "window": "近三个月 | null", "dimension": "overall" },
  "tools": [ { "name": "compute_metric", "args": { "metric_id": "fpd30_rate" } } ],
  "clarify": { "slot": "product | window | branch | metric", "options": ["…"] },
  "notes": ["为什么这么选，一句"]
}
```

约束：
- `tools[].name` 必须在 `tools_for(branch, role)` 返回的子集里，否则**整条计划作废**（重试 1 次 → D2）。
- `branch=model` 时出现 `compute_metric` → 分支冲突，**不降级不改写，直接拒**（§12.1 闸门 2）。
- `scope.window=null` 且 `intent != CONFIG/FIELD` → 必须给 `clarify`，不许自己补默认。

### 3.2 叙述（`purpose=narrate`，S4）

输入只给槽位与引用，**绝不给原始表**：

```json
{
  "question": "…",
  "book": { "model.ks_max": {"value": 0.2576, "unit": "raw", "label": "KS 区分度"} },
  "envelope": ["【口径】…","【窗口】…","【过滤】…","【截止】…","【分箱】…"],
  "refs": [{"ref_id":"ref_20261109_7f3a","summary":"5 箱分布，单调性破格在 bin3"}],
  "constraints": ["不得出现未在 book 中的数字","不得改变结论方向"]
}
```

输出：`{"text": "…"}`。`text` 必须过 `verify_numbers`；不过 → **丢弃 text，回退模板**（D1）。

### 3.3 Judge（`purpose=judge`，S5）

```json
{
  "pass": false,
  "violations": [
    { "type": "number_mismatch | zero_for_missing | direction_reversed",
      "quote": "原文片段",
      "expected": "应为…" }
  ],
  "reasoning": "一句话"
}
```

> judge **只判这三件事**。它不评「写得好不好」，不参与规则评估器计分（§13.3）。

## 4. MCP 契约（S2）

| 项 | 约定 |
|---|---|
| 传输 | stdio（开发）/ streamable-http（内网） |
| 工具 | 仅上表 5 个只读工具 |
| 连接 | 只读账号；`statement_timeout=3s` |
| 行数 | 单次 ≤200，超出截断 + `truncated: true` + `total_rows` |
| 版本 | 每个响应带 `schema_version: "1.0"` |
| 错误 | `NOT_FOUND` / `TIMEOUT` / `DENIED`（三分类，文案不同） |
| 审计 | `(caller, tool, args_hash, rows, latency_ms, status, ts)`；不存 args 原文 |
| 禁止 | 不接受 SQL/表名/字段名参数；不返回任何行级数据、分数原值、`base_dist` |

响应包封（统一）：

```json
{ "ok": true, "schema_version": "1.0", "data": {}, "meta": { "rows": 12, "truncated": false, "latency_ms": 41 } }
```

## 5. 错误码（9 个，冻结）

| 码 | 何时 | 用户可见文案要点 |
|---|---|---|
| `NOT_REGISTERED` | 指标不在册 | 给能力清单 + 「这个口径还没有定义」 |
| `NOT_APPLICABLE` | 该指标不适用于此参数（3 天算 FPD7）；或编排层工具未注入 | 给 `min_days` + 建议窗口；未绑定工具不当 bug 报 |
| `VINTAGE_BLOCKED` | 样本不足/未到期 | 只给 n，说「样本不足，不下结论」 |
| `NEED_CLARIFY` | 缺槽位或分流模糊 | 给 2—4 个选项，**不选默认** |
| `PARAM_OUT_OF_DOMAIN` | 参数不在枚举内；缺 `metric_id` 也走这里（不抛 `KeyError`） | 给合法域 |
| `NOT_VISIBLE` | 明细请求 | 说「明细不出域」+ 聚合替代 |
| `DENIED_DIMENSION` | 维度越权 | 拒绝，**零数字** |
| `ROW_FILTERED` | 行级过滤生效 | 在【过滤】行注明 |
| `NO_JOIN_PATH` | 实体图无路径 | 说明哪两个实体不连通 |

> 这 9 个是**语义层**错误（`core.codes.SEMANTIC_ERRORS`），`eval_semantic.py` 用它做合法性别校验，**不许往里加调度/运维类状态**。

**调度层状态码（不在上表，形状同 `{ok:false, code, ...}`）**

| 码 | 何时 | 处理 |
|---|---|---|
| `JOB_RETIRED` | 物化任务已下线（`job_state.status='retired'`） | 说「该任务已下线，历史数据保留」；**不回落成实时算**，也不静默跳过（静默会让"下线"看起来没生效） |
