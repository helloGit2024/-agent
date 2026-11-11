# WrenAI MDL 调研笔记（R-002）

> 目的：看清 WrenAI 的语义契约（MDL）长什么样、wren-core 在**加载期**校验什么，
> 判断我们的 `MetricDef` / `ModelMetricDef` 缺哪些字段、哪些字段是故意不补的。
> 版本：`main` @ `bc2b06a1950a`（2026-05-06）。浅克隆多次失败（HTTP/2 断流），
> 改为 GitHub API 逐文件抓取，**已按 commit SHA 锁定**，本地副本在 `/tmp/wren-src/`。
> 阅读范围：`wren-core-base/src/mdl/*`、`wren-core-base/manifest-macro/src/lib.rs`、
> `wren-core/core/src/mdl/{mod,context,lineage}.rs`、`wren-core/core/src/logical_plan/analyze/*`。
> 文档口径：[MDL schema reference](https://docs.getwren.ai/oss/reference/mdl)。
> 结论口径：**"偷"= 可以照着改我们的语义层；"不偷"= 与 P-01/P-03/P-05/P-09/P-10 冲突，只记录不采纳。**
> **本次未做实测**：R-002 验收只要求字段映射表 + 缺口清单，跑服务留给需要证据时再做（见 §7）。

## 0. 一句话对照

| 维度 | WrenAI（MDL） | 我们的 V2 |
|---|---|---|
| 契约载体 | YAML 源文件 → 编译成 `target/mdl.json` | Python `dataclass` 册子（`core/catalog.py`） |
| 契约粒度 | 表/列/关系/视图/Cube，**指标是可写 SQL 表达式的 measure** | 指标是一等公民，公式即代码，**没有表达式入口** |
| 消费方 | 任意 SQL 客户端 + Agent，最终出 SQL | 只有 `compute_metric` / `compute_model_metric` 一个出口 |
| 权限 | 列级"看不见" + 行级条件过滤（属性驱动） | 角色驱动、调用时判定、未知角色 fail-closed |
| 失败 | 规划期报错（DataFusion 错误串） | 错误码 + 零数字 + 四级降级 |

## 1. MDL 的对象模型（文档事实）

MDL 用 YAML 描述六类对象，编译成一份 manifest，引擎按 manifest 展开 SQL：

| 对象 | 关键字段 | 作用 |
|---|---|---|
| `model` | `table_reference` 或 `ref_sql`（二选一）、`columns`、`primary_key`、`row_level_access_controls` | 逻辑数据集 |
| `column` | `type`、`is_calculated`+`expression`、`relationship`、`is_hidden`、`column_level_access_control` | 暴露字段 |
| `relationship` | `models[2]`、`join_type`、`condition` | 可复用 join |
| `view` | `statement` | 命名 SQL，查询时递归展开 |
| `cube` | `base_object`、`measures[]`、`dimensions[]`、`time_dimensions[]`、`hierarchies` | 预聚合语义对象 |
| `knowledge/*` | rules / glossary / metrics / caveats / sql 对 | **只给 Agent 读，不进 manifest** |

后两类是分界线：`cube` 进引擎（可被强制），`knowledge/` 不进引擎（只进 prompt）。
我们只有前者的一半，后者完全不做。

## 2. 精读一：manifest 加载与版本门

### 事实

1. **结构体是宏生成的**，字段清单不在 `manifest.rs` 里，而在 `manifest-macro/src/lib.rs`
   （`manifest! / model! / column! / relationship! / cube! / ...` 各生成一份 struct + pyo3 绑定）。
   所以"MDL 有哪些字段"要看两个文件：宏定义字段，`manifest.rs` 定义行为
   （[manifest-macro/src/lib.rs](file:///tmp/wren-src/wren-core-base_manifest-macro_src_lib.rs#L128-L205)、
   [mdl/manifest.rs](file:///tmp/wren-src/mdl_manifest.rs#L39-L56)）。
2. **版本门只挡"未来"**：`MAX_SUPPORTED_LAYOUT_VERSION = 2`，manifest 声明的版本更高就报错，
   文案同时给出**需要多少 / 支持到多少**两个数字
   （[mdl/manifest.rs](file:///tmp/wren-src/mdl_manifest.rs#L96-L125)）。
3. **`table_reference` 与 `ref_sql` 的互斥是"三态"而非异常**：`Model::source()` 返回
   `TableReference / RefSql / Invalid(reason)`，两个都写或都不写都只是 `Invalid`
   （[mdl/manifest.rs](file:///tmp/wren-src/mdl_manifest.rs#L396-L419)）；
   真正报错要等到注册远程表时 `Err(Plan(reason))`
   （[mdl/mod.rs](file:///tmp/wren-src/wren-core_core_src_mdl_mod.rs#L280-L283)）。
4. **hidden 与 physical 是两种"可见"**：`get_visible_columns()` 去掉 `is_hidden`；
   `get_physical_columns()` 再去掉 `relationship` 句柄列
   （[mdl/manifest.rs](file:///tmp/wren-src/mdl_manifest.rs#L337-L364)）。
   文档承诺"句柄列可被计算列穿透"，但**查询里直接用会规划失败**（`#2688`）——
   文档与引擎实现并不同步（[MDL reference](https://docs.getwren.ai/oss/reference/mdl) §Relationship columns）。

### 偷

- **版本门的文案模板**：两个具体数字（manifest 要 X / 引擎到 Y）比一句"版本不匹配"可诊断得多。
  我们的定义哈希自检失败时只报"哈希不一致"，可照这个格式补上**期望哈希 / 实际哈希**。
- **`Invalid(reason)` 带原因**：错误对象里保留"为什么"，而不是只给布尔。

### 不偷

- **延迟报错**：写错 `table_reference/ref_sql` 要等到第一次查询才炸。我们启动自检必须 fail-closed
  （P-06 的哈希自检已经这么做，保持）。
- **文档承诺 ≠ 引擎实现**：schema 里出现的能力必须真的能用，否则 LLM 会反复试探
  （`#2688` 的句柄列就是反例）。

## 3. 精读二：校验管线（注册期 → 族谱 → 计划期）

`AnalyzedWrenMDL::analyze()` 三件事：注册远程表 → 建 lineage → 交给 analyzer 规则链
（[mdl/mod.rs](file:///tmp/wren-src/wren-core_core_src_mdl_mod.rs#L77-L87)）。

### 事实

1. **列的可用性在"注册进 schema"那一刻就定了**：`infer_and_register_remote_table` 逐列问
   `validate_clac_rule(...).0`，为 false 的列**根本不进 Arrow schema**
   （[mdl/mod.rs](file:///tmp/wren-src/wren-core_core_src_mdl_mod.rs#L226-L291)）。
   效果：受限列在规划器眼里等于"不存在"，任何 SQL 都引用不到。
2. **于是"权限不足"和"字段拼错"报的是同一个错**，所以他们做了第二遍：正常规划失败后，
   用 `Mode::PermissionAnalyze` 重新 analyze（该模式下**所有列都注册**）再规划一次；
   这一遍若能规划成功，就把原错误抛回——说明是权限问题
   （[mdl/mod.rs](file:///tmp/wren-src/wren-core_core_src_mdl_mod.rs#L543-L592)）。
3. **lineage 是加载期建的依赖图**：计算列展开到源列，`required_fields_map` 记录
   "这个计算列最终依赖哪些物理列"；依赖图不是 DAG 就在加载期报
   `cyclic dependency detected: {column}`
   （[lineage.rs](file:///tmp/wren-src/wren-core_core_src_mdl_lineage.rs#L233-L235)）。
4. **计划期规则顺序固定**：`ExpandWrenViewRule`（先展开视图）→ `ModelAnalyzeRule` →
   `ModelGenerationRule`（在模型计划顶部加行过滤）
   （[context.rs](file:///tmp/wren-src/mdl_context.rs#L176-L200)）。
5. **行过滤必须早于聚合**：RLAC filter 被 alias 成子查询后加在模型计划最上层，
   再补一个 projection（[model_generation.rs](file:///tmp/wren-src/wren-core_core_src_logical_plan_analyze_model_generation.rs#L88-L129)）。
6. **关系链拼 join 时，缺边是 `break` 不是报错**：
   `with_chain` 找不到 `find_edge` 就跳出循环
   （[relation_chain.rs](file:///tmp/wren-src/relation_chain.rs#L74-L79)）；
   路径本身由上游 DAG 算好，所以"按设计不会发生"，但失败形态是静默截断。

### 偷

- **两段式判定**：先按正常口径判，失败后按"放宽口径"重判，**用放宽后是否成功来区分失败原因**。
  这正好补我们 D3 的短板——现在"口径不存在 / 维度不支持 / 权限不足 / SQL 错误"混在一条说明书里。
  我们不需要重跑 plan，但可以用同构做法：`catalog.resolve()` 未命中时，用"去掉角色过滤"的册子再解析一次，
  命中 ⇒ `DENIED`，仍未命中 ⇒ `NOT_REGISTERED`。零数字，只是把错误码选对（P-10 允许）。
- **加载期依赖图 + 环检测**：口径册一旦允许"指标引用指标"，必须先有 DAG 校验，不能等运行时递归爆栈。
- **过滤先于聚合**：与我们 `build_view` 的"取数前生效"同构，这条继续坚持（我们更强：过滤在取数前，
  它在逻辑计划里）。

### 不偷

- **注册期判权限**：它每个请求都重新 analyze（因为属性随请求变）。我们 2—5 人、角色少，
  调用时判定成本更低且审计点集中（P-03），不改。
- **静默 `break`**：缺 join 边必须给 `NO_JOIN_PATH`，不能截断后当作只有一个表。

## 4. 精读三：行/列级 ACL 的两种"缺省"

### 事实

1. **required 属性缺失 → 规划期报错（fail-closed）**：
   `validate_rule` 对 `required=true` 的属性找不到就 `plan_err!`
   （[access_control.rs](file:///tmp/wren-src/acl.rs#L250-L288)）。
2. **optional 属性缺失且无默认值 → 整条规则不生效（fail-open）**：
   同一函数返回 `Ok(false)`，调用方 `generate_row_level_access_control_filter` 返回 `None`
   ——**没有过滤 = 全部行可见**
   （[model_generation.rs](file:///tmp/wren-src/wren-core_core_src_logical_plan_analyze_model_generation.rs#L294-L310)）。
3. **列级规则同理**：`validate_clac_rule` 在 optional 缺失时返回 `(true, None)`，
   即"列可见、规则不应用"，而不是"列不可见"
   （[access_control.rs](file:///tmp/wren-src/acl.rs#L290-L323)）。
4. **报错文案带规则名和属性名**：
   `session property {p} is required for `{rule}` rule but not found in headers`
   （[access_control.rs](file:///tmp/wren-src/acl.rs#L264-L269)）。
5. **CLAC 阈值只收字面量**：`prevent_invalid_expr` 限制属性值只能是字面量，防止注入
   （[access_control.rs](file:///tmp/wren-src/acl.rs#L214-L245)）。
6. **CLAC 会顺着计算列递归检查依赖列**：计算列本身放行不代表它引用的列放行
   （[access_control.rs](file:///tmp/wren-src/acl.rs#L325-L360)）。

### 偷

- **递归检查派生值的依赖**：我们将来若有派生指标（如"通过率环比"），授权检查必须追到底层指标，
  不能只看最外层。这条直接对应 P-03"新维度同步三处"的盲区。
- **属性值只收字面量**：我们的 `window` 是自然语言串，`product` 是枚举——已经是白名单；
  若将来放开任何自由参数，照这条只收字面量。

### 不偷

- **optional ACL 静默放开**：这是"功能开关"语义，不是"权限"语义。我们 P-03 要求未知身份 fail-closed，
  连"默认不过滤"都不允许出现。
- **CLAC 列掩码（阈值/脱敏）**：等于"先看见再遮住"，越权已经发生（第四个「停」）。
  我们要么不可见，要么不出现在册子里。

## 5. MDL ↔ 我们的字段映射

### 5.1 对象级

| MDL 对象 | 我们最接近的东西 | 判定 |
|---|---|---|
| `model`（表/视图 + 列声明） | `core/entities.py` 的实体 + join 图 | ⚠️ 我们声明关系，不声明列 |
| `column.is_calculated + expression` | `MetricDef.compute` 闭包 | ✅ 更严：表达式即代码，无自由 SQL |
| `relationship`（join_type + condition） | `entities.Join` + `entities.path()` | ✅ 等价（我们多一条 `NO_JOIN_PATH`） |
| `view.statement` | 无（`build_view` 取数，不出 SQL） | ✅ 明确不做 |
| `cube.measures` | `MetricDef`（`numerator/denominator/formula`） | ✅ 更严：无聚合表达式入口 |
| `cube.dimensions` | `MetricDef.dimensions` | ✅ |
| `cube.time_dimensions` | `MetricDef.grain` + `Window` | ✅ 更严：grain 是枚举 |
| `cube.hierarchies` | 无 | ⚠️ 缺（低优先级） |
| `row_level_access_control` | `access` 的行过滤 + `ROLES` | ⚠️ 语义不同：属性驱动 vs 角色驱动 |
| `column_level_access_control` | `access_policy` / `ROLES[*].metrics` | ✅ 更严：直接不可见，不做掩码 |
| `session_property` | `admission` 的 token → 角色 | ✅ 更严：无默认值 |
| `knowledge/rules`、`knowledge/sql` | 无（S3 记忆待做） | ✅ 规则不做；`sql` 对等价于记忆（S3 再评估） |

### 5.2 字段级（`MetricDef` / `ModelMetricDef`）

| MDL 字段 | 我们的字段 | 判定 |
|---|---|---|
| `model.name` | `MetricDef.metric_id` / `name` | ✅ |
| `column.type` | `MetricDef.unit`、`kind` | ✅ |
| `column.expression` | `formula` + `numerator/denominator` + `compute` | ✅ 更严 |
| `column.is_hidden` | — | ⚠️ 我们有"不可见"，没有"存在但隐藏" |
| `column.relationship` | —（指标不声明依赖） | ⚠️ **缺** |
| `column.not_null` | — | ❌ 不需要 |
| `model.primary_key` / `is_primary_key` | `entities.KEY` | ✅ 在实体层 |
| `model.table_reference` / `ref_sql` | `build_view` 的取数来源 | ✅ |
| `model.cached` / `cube.refresh_time` | `scheduler` 的物化任务 | ✅ 在调度层 |
| `model.dialect` | — | ❌ 不需要 |
| `properties.description` | `description` | ✅ |
| `cube.measures[].type` | `unit` | ✅ |
| `cube.hierarchies` | — | ⚠️ 缺（低优先级） |
| `RLAC.required_properties` | `admission` 的 token/角色 | ✅ |
| `RLAC.condition` | `access` 的行过滤映射 | ⚠️ 我们是代码，未声明依赖字段 |
| `SessionProperty.default_expr` | —（P-09 只许补 `product/window` 且须声明） | ⚠️ 语义冲突，不补 |
| `CLAC.operator/threshold` | — | ❌ 不做 |
| — | `grain`、`lag_days`、`requires_params`、`not_applicable`、`healthy_range`、`disambiguation`、`typical_questions`、`access_policy` | ✅ **MDL 没有，我们有** |
| — | `ModelMetricDef.min_sample`、`requires_maturity`、`score_col`、`label_col`、`ref_period` | ✅ **MDL 没有，我们有** |

反向结论先说清楚：**在"风控指标"这个场景，我们的册子比 MDL 更完备**——
MDL 缺分子/分母、表现期、最小样本、适用性声明、歧义说明；它的强项在"表结构 + 自由表达式"，
恰好是 P-01/P-05 明确禁止的部分。所以这次调研的主要产出是"缺口清单"，而不是"照抄计划"。

## 6. 我们缺的字段（缺口清单）

### 6.1 建议补（先立任务，不擅自开工）

| # | 字段 | 落点 | 为什么缺 | 成本 |
|---|---|---|---|---|
| G-1 | `MetricDef.row_filter_fields: tuple[str, ...]` | `core/catalog.py` + 启动自检 | 对应 `RLAC.required_properties`：行权限用到的字段要**显式声明**。我们现在 `row_filter` 是运行时 dict，册子里没有声明，无法审计"过滤字段是否就是设计里的那一个"。 | S |
| G-2 | `MetricDef.depends_on: tuple[str, ...]` | `core/catalog.py` + `definition_hashes` | 对应 lineage：指标 → 底层字段/指标的依赖。不补的后果：派生指标时代理不了"它读了哪些列"，也无法做 DAG 环检测（照 lineage.rs:233 的做法）。 | M |
| G-3 | `MetricDef.drill_order`（或 `hierarchies`） | `core/catalog.py` | 对应 `cube.hierarchies`：下钻顺序目前由前端/调用方决定。**等真有连续下钻需求再补**，不要提前造。 | S（可延后） |

### 6.2 明确不补（写明理由，避免下一轮重新讨论）

| MDL 字段/能力 | 不补理由 |
|---|---|
| `expression` / `ref_sql` / `dialect` | 自由表达式/自由 SQL 是 P-01/P-05 直接禁止项，schema 一开就收不回（第三个「停」）。 |
| `properties`（任意 KV） | 任意元数据 = 第二份会漂移的口径文本；我们需要的是字段，不是挂件。 |
| `not_null`、`cached` | 属数据层/调度层约束，指标层不承担。 |
| `CLAC`（列级阈值掩码） | "看见但遮住"= 越权已发生（第四个「停」）。 |
| `SessionProperty.default_expr` | 静默补全与 P-09"补全必须在回答里声明"冲突；我们的补全只允许 `product/window` 且显式声明。 |
| `knowledge/rules`（自由 Markdown 规则） | 规则不进引擎 = 不可强制；口径必须在代码里。 |

### 6.3 待定（挂 OPEN-QUESTIONS）

- `knowledge/sql`（NL→SQL 对，作为记忆源）与我们 S3 `MemoryItem` 的关系：它是"范例句对"，
  我们是"偏好+口径"。等 T-401 建表时再定是否加一类 `example_pair`。
- 指标级 `refresh_time`：我们物化任务已带版本，是否需要把"指标 → 依赖的物化任务版本"写进册子。
  如果 S5 要做 trace-replay 的一致性校验，可能需要。

## 7. 未做的部分（诚实边界）

- **没起 WrenAI 服务**：R-002 验收是字段映射 + 缺口清单，不含实测；R-001 的 3 题实测已经覆盖
  "语义解析失败形态"这个更关键的问题，R-002 再跑一遍服务属于重复投入。
- **没读 Python 侧**（`wren-core-py`、`wren-cli`）：本次只关心契约与校验，编译/CLI 不在范围。
- **没验证 `#2688`**：句柄列不能直接查是文档自述 + 代码结构（`get_physical_columns` 过滤 relationship）
  的合理推断，未实际跑查询复现。

## 8. 三条候选改动（需先在 `docs/tasks.md` 立任务）

1. **D3 失败原因二段判定**：`catalog.resolve()` 未命中时，用"去掉角色过滤"的册子重解析一次，
   命中 ⇒ `DENIED`，未命中 ⇒ `NOT_REGISTERED`。落点 `core/catalog.py` + `v2_dialogue.py` 的降级文案。
   收益：与 R-001 §3 的"按阶段区分失败"合并成一套可诊断错误码。
2. **册子补 `row_filter_fields` 并在启动自检**（G-1）：落点 `core/catalog.py`、
   `core/definition_hashes.json`（哈希会变，需按设计变更重录并写明原因，P-11）。
3. **定义哈希失败文案补"期望/实际"两个值**（照 `LayoutVersionError` 的格式）：落点
   `core/semantic_contract.py` 的自检出口。

以上三条都需要先立任务才能动代码。
