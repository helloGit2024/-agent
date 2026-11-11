# ADR-006（草稿）· join path 用「显式路径 id + 人工审批」，不自动做 pre-aggregation

- 状态：**草稿，待评审**（本文件不改变任何代码行为）
- 日期：2026-11-11
- 相关：R-003、S1.5、P-01、P-02、P-03、P-05、P-06、P-10、P-11
- 调研依据：[cube-notes.md](file:///Users/tj/Documents/trae_projects/策略回测agent/docs/research/cube-notes.md)
  （Cube 提交 `e6c64f6dedb59142c9d8e72466915ad72734b294`）

## 背景

现在的 join 图是声明式的（[core/entities.py](file:///Users/tj/Documents/trae_projects/策略回测agent/core/entities.py#L43-L68)），
但「可用维度」没有从图推导，而是散在至少五处（模型级两项分开算则为六处）：

1. `MetricDef.dimensions`
2. `entities.GRAIN_DIMENSIONS`
3. `ROLES[*].dimensions`
4. `semantic_contract` 的 schema enum
5. 模型级 `model_metrics.DIMENSIONS` / `comparable_dimensions`

同一件事多份副本，靠人同步。加一张表或改一条基数时，任何一份漏改都会产生
「schema 允许、权限允许、但 join 走不通」或更糟的「join 走通了、分母被放大」。
`entities.path()` 已有 BFS，但只被自检调用，没有参与维度解析；多路径时静默取第一条。

R-003 读了 Cube 的三处实现后，确认了两条可搬运的经验：

- Cube 的可用维度是「人声明 + 编译期建图 + 查询期最短路」的投影，不是多份白名单；
- Cube 开源版**不做自动 pre-aggregation**，只做「人声明 rollup + 查询期匹配 + 独立调度刷新」。

## 决策

采用 **「显式路径 id + 人工审批 + 运行时可复现」** 的 join path 建模方案。
分四条：

### D1. 每条被使用的 join 路径必须有稳定身份 `path_id`

- `path_id` 由**有序边签名**生成，例如 `Fact->Applicant->Loan`，
  不允许用「第几条 BFS 结果」这种位置身份。
- 路径注册表是唯一事实源；同一对实体存在多条合法路径时，**不自动选**，
  返回新错误码 `AMBIGUOUS_JOIN_PATH`，要求建模人显式指定。
- 无路径继续返回 `NO_JOIN_PATH`，且**零数字**（P-03/P-10）。

### D2. 可用维度从 join 图推导，不再手写新副本

- 每个维度声明「属于哪个实体、哪个字段」；每个指标声明 grain。
- 可用维度 = 从 grain 出发、沿 `N:1`/`1:1` 边可达、且路径不放大基数的维度集合。
- `GRAIN_DIMENSIONS` 降级为**启动期计算出的只读缓存**，不再是手写白名单。
- `MetricDef.dimensions`、`ROLES[*].dimensions`、schema enum 保留为**投影**，
  但启动时必须与推导结果逐项比对；不一致直接让 `make gate` 失败（P-12）。

### D3. 物化（pre-aggregation）只接受人工审批的候选，不自动组合

- `promotion.py` 的命中队列继续只负责「提交候选」，不自动建 job（保持现状）。
- 每个获批候选必须登记：`rollup_id`、维度/测度集合、`path_id`、分区粒度、刷新口径、
  负责人、定义哈希。
- 查询期只做**匹配**：measure 子集 + 维度覆盖 + 时间粒度匹配；
  多个候选同时匹配时报 `AMBIGUOUS_ROLLUP`，不按声明顺序静默选第一个。
- 没有匹配或未构建完成 → 明确降级/错误码，不返回「替代数字」（P-10）。

### D4. 回答信封与 trace 必须能复现路径

可聚合回答的信封新增（不改变四行结构，只加溯源字段）：

- `path_id`：本次用到的 join 路径；
- `grain`：指标 grain；
- `cardinality`：路径上各边基数（如 `N:1,1:1`）；
- `rollup_id`：若命中物化，记录是哪一张；未命中记 `none`。

模型级回答的【分箱】行同样带 `path_id`（若该指标依赖 join）。
`AnswerBook` 登记这些字段，`verify_numbers` 不把它们当业务数字。

## 为什么选人工审批，而不是自动 pre-aggregation

这不是「保守」，而是四条硬约束决定的最优解：

1. **唯一入口不能被绕开（P-01/P-02）。**
   自动 pre-aggregation 意味着系统自己生成一条新的计算路径；而我们的定时任务
   必须是一行 `compute_model_metric(...)`。自动建表 = 新增入口，直接违反 P-01。

2. **Cube 自己在 OSS 里也不做。**
   `PreAggregations.ts:1044` 对 `autoRollup` 直接抛
   `Auto rollups supported only in Enterprise version`；OSS 只有「人声明 + 匹配」。
   连一个通用语义层都不在开源版自动决定物化组合，我们没有理由在信贷口径上更激进。

3. **自动匹配会引入不可见的「数据源切换」。**
   Cube 的匹配是 schema 声明顺序 first-match（`PreAggregations.ts:1053-1066`，顺序来源见 `1113-1116`）。
   如果我们也自动选，改一次文件顺序、加一个维度，就可能静默换一张表；
   数字看起来仍「合理」，但同参不同数，直接违背 P-06/P-11。

4. **审批成本远低于口径事故成本。**
   物化候选来自真实提问（`promotion.py` 的 ≥N 次命中），量很小；
   人工审批一次，换来的是「这张表为什么存在、谁负责、口径是什么」都可查。
   自动化的收益是省几分钟，事故成本是重跑历史报表、解释分母变化。

**结论**：自动 pre-aggregation 解决的是「性能」，我们当前的第一矛盾是「口径可信」。
在 P-01/P-06/P-11 不变的前提下，人工审批是唯一不新增计算路径、又能物化的方案。

## 风险与应对

### 风险 1：越权（P-03）

| 风险 | 应对 |
|---|---|
| 路径经过未授权实体，把不该看的维度带出来 | 路径解析在**权限判定之后**执行；路径的每个实体都必须在角色可见集合内，否则 `NOT_VISIBLE`，零数字 |
| 用「路径可达」替代「角色可见」 | 两者是 AND：`可见 = 角色允许 AND 图可达 AND 非放大`；任一不满足都拒绝 |
| 物化表绕过行级过滤 | 物化候选登记时必须写明过滤口径；查询期若角色过滤与物化口径不一致，禁用该 rollup，回退实时计算并记 `ROLLUP_ACL_MISMATCH` |
| 未知角色 fail-open | 保持现状 fail-closed；新增 `path_id` 不引入任何默认授权 |

**验收断言（未来任务）**：越权角色请求经过未授权实体的维度，响应中数字个数为 0；
即使该维度在 `path_id` 上可达，也不得出现在 schema enum 里。

### 风险 2：口径漂移（P-06/P-11）

| 风险 | 应对 |
|---|---|
| 同一对实体多条路径，分母不同 | 只允许非放大路径；多路径报 `AMBIGUOUS_JOIN_PATH`，必须人工指定 |
| 加表/改基数后，旧 `path_id` 语义变了 | `path_id` + 定义哈希进版本；变更即新 id，旧 id 保留只读，不静默覆盖 |
| `GRAIN_DIMENSIONS` 与 `JOINS` 漂移 | 启动期双向比对，不一致 fail-fast；`make gate` 增加一条门禁 |
| 物化声明顺序改变导致换源 | 显式 `rollup_id` + 唯一匹配；多匹配报 `AMBIGUOUS_ROLLUP` |
| 渲染层偷偷加 `if product == ...` | 维度可用性只来自声明与图，渲染层不得分支（既有第四停） |

**验收断言**：把 `JOINS` 里一条边改成放大基数，`make gate` 必须失败；
把 `GRAIN_DIMENSIONS` 手工改错一项，启动自检必须失败。

### 风险 3：可溯源（P-06 + trace-replay）

| 风险 | 应对 |
|---|---|
| 回答只说「按产品」，不说走哪条路 | 信封与 trace 必带 `path_id/grain/cardinality` |
| 无法复现「为什么这个维度当时可用」 | trace-replay 重放 `path_id` + 定义哈希，而不是重新 BFS |
| 物化命中不可见 | `rollup_id` 进信封；未命中记 `none`，不省略 |
| 错误码被模糊化 | 明确 `NO_JOIN_PATH` / `AMBIGUOUS_JOIN_PATH` / `AMBIGUOUS_ROLLUP` / `ROLLUP_NOT_READY` / `ROLLUP_ACL_MISMATCH` |

**验收断言**：任取一条历史回答，凭 `path_id` + `definition_hash` + `rollup_id` 能重建
同一 join 计划；重建失败即视为 trace 不完整。

## 备选方案

| 方案 | 结论 | 理由 |
|---|---|---|
| A. 保持现状（多份白名单 + BFS 第一条） | 不采用 | 加表即漂移；多路径静默选第一条不可复现 |
| B. 全自动：查询时 BFS 选路 + 自动 pre-aggregation | 不采用 | 绕开 P-01/P-02；Cube OSS 都不做；数据源切换不可见 |
| C. 只加路径 id，不改可用维度来源 | 不采用 | 解决了溯源，没解决多份副本漂移 |
| **D. 显式路径 id + 图推导可用维度 + 人工审批物化** | **采用** | 不新增计算路径；口径可复现；审批成本可控 |

## 后果

- **正面**：
  - 可用维度只有一个推导来源，加表/改基数会自动反映到白名单，漏改变成启动失败；
  - 多路径不再静默，口径争议变成显式审批项；
  - 回答可凭 `path_id` 复现，满足 trace-replay；
  - 物化只做「匹配」，不新增计算入口，P-01/P-02 不变。
- **负面 / 成本**：
  - 需要维护路径注册表与审批记录；
  - 启动期多一次图校验，`make gate` 多一条门禁；
  - 已有回答基线需要补 `path_id` 字段（按 P-11，先跑 `make regress`，按设计变更重录并写明原因）。
- **不做的事**：
  - 不引入自由 SQL/表达式（P-05 不变）；
  - 不做自动 rollup/lattice；
  - 不做成员遮值（masking）；
  - 不改 `core/stats.py` 的导入边界（P-01 不变）。

## 实施落点（仅登记，不在本 ADR 实施）

| 编号（建议） | 内容 | 依赖 |
|---|---|---|
| T-3xx-1 | `path_id` 生成与注册表；`AMBIGUOUS_JOIN_PATH` 错误码 | 无 |
| T-3xx-2 | `GRAIN_DIMENSIONS` 改为启动期从 `JOINS` 推导 + 一致性门禁 | T-3xx-1 |
| T-3xx-3 | 信封/trace 增加 `path_id/grain/cardinality`；回归基线更新 | T-3xx-1 |
| T-3xx-4 | 物化候选登记 `rollup_id` + 匹配层 + `AMBIGUOUS_ROLLUP` | T-3xx-2 |
| T-3xx-5 | 越权/漂移/溯源三组正反向断言 | T-3xx-3、T-3xx-4 |

> 上述编号需在 `docs/tasks.md` 正式落行后才动代码（AGENTS.md 三、四节）。

## 未决问题（评审时需要拍板）

1. `path_id` 用「有序边签名」还是「人工短名（如 `fact_loan_v1`）」？
   前者免维护但改名敏感，后者可读但需人工命名。
2. 物化匹配出现多个候选时，是报错，还是按显式 `priority` 选？
   本文倾向报错（更符合「不静默换源」），但会增加运维负担。
3. 模型级指标的 `path_id`：`Score` 目前只连 `Applicant`，是否需要在信封里体现？
   本文倾向体现，保持与可聚合一致。
4. 已有 7 题回归基线补 `path_id` 后，是否算「按设计变更重录」？
   本文认为是，需在 commit message 写明原因与影响题号（P-11）。

## 附：Cube 证据映射

| 本 ADR 决策 | Cube 对应事实（`e6c64f6`） | 偷 / 不偷 |
|---|---|---|
| D1 路径身份 / 歧义报错 | `JoinGraph.ts:301-325`、`BaseQuery.ts:600-639` | 偷「显式消歧」，不偷「BFS 第一条」 |
| D2 可用维度由声明投影 | `CubeSymbols.ts:979-1068`、`CubeToMetaTransformer.ts:294-391` | 偷「view includes + joinPath」，不偷 `membersForQuery` 的忽略 query |
| D3 人工审批物化 | `PreAggregations.ts:1044`、`1053-1066`、`1113-1116` | 偷「声明 + 匹配 + 调度」，不偷自动 rollup / 顺序即优先级 |
| 越权零数字 | `CubeValidator.ts:1140-1144`、`1147-1158` | 不偷空 memberLevel fail-open、不偷 masking |
| 可溯源 | `RefreshScheduler.ts:153-256` | 偷「未使用要显式返回原因」 |

---

> 本 ADR 为草稿：不修改 `core/`、`tools/`，不执行 git commit/push。
> 评审通过后，先补 `docs/tasks.md` 落点，再按 P-11 跑 `make regress`。
