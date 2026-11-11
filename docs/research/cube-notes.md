# Cube 源码笔记（R-003）

> 对象：开源仓库 `github.com/cube-js/cube`
> 固定提交：**`e6c64f6dedb59142c9d8e72466915ad72734b294`**（2026-11-11T00:11:28Z，
> commit message：`docs: embed the Editing published dashboards from chat demo video ...`）
> 取证方式：按上述 SHA 下载源码包 + GitHub Contents/Tree API 交叉核对；下文行号均指该 SHA。
> 精读范围：`packages/cubejs-schema-compiler/src`（joins / 可用维度 / pre-aggregation），
> 以及 `packages/cubejs-client-core/src/Meta.ts`、`packages/cubejs-server-core/src/core/RefreshScheduler.ts`。
>
> 标记约定：**【事实】** = 源码里读得到、可复现；**【推断】** = 我基于事实对 Cube 设计意图
> 或对本项目适配性的判断，未经 Cube 文档/作者确认。

---

## 结论先行

1. **Cube 的 join 不是运行时猜的，是 schema 里显式声明 + 编译期建图 + 查询期最短路。**
   `joins` 必须写 `relationship`（`belongsTo` / `hasOne` / `hasMany`），编译期检查
   「有乘法测度时对端必须有 primary key」，查询期对多路径给出 `joinHints`/`joinPath`，
   找不到路直接抛 `JoinPathNotFoundError`。这三件事正好补我们 `entities.path()` 只做 BFS、
   不校验基数、不记录路径身份的空档。

2. **Cube 服务端没有 `available_dimensions` 这个字段。** 字面量 `availableDimensions`
   只存在于前端 SDK（`packages/cubejs-client-core/src/Meta.ts` 提供 `membersForQuery`）。
   服务端真正决定「哪些维度可用」的是四层叠加：
   cube 成员表 → view 的 `includes`/`excludes`/`joinPath` → meta 的 `isVisible`/连通分量
   → `accessPolicy.memberLevel`。**【推断】** 这四层恰好对应我们缺的「维度可用性单一来源」；
   我们现在的四份可聚合维度白名单（`MetricDef.dimensions`、`GRAIN_DIMENSIONS`、`ROLES[*].dimensions`、
   schema enum），再加上模型级 `DIMENSIONS` 与 `comparable_dimensions` 两处，共六处声明点；
   同一件事抄六遍，漂移只是时间问题。

3. **Cube 开源版的 pre-aggregation 是「人先声明，查询期匹配」，不是「系统自动生成」。**
   自动 rollup（`autoRollup`）在 OSS 里直接抛 `Auto rollups supported only in Enterprise version`
   （`PreAggregations.ts:1044`），且匹配是**按 schema 声明顺序 first-match**
   （`PreAggregations.ts:1053-1066`），匹配条件要求 measure 是子集、维度/过滤可被覆盖。
   **【推断】** 这为 ADR-006 选「人工审批 join path / 物化候选」提供了直接依据：
   连 Cube 自己都不敢在 OSS 里自动决定物化，我们更不该在信贷口径上自动建表。

---

## 0. 对照本项目现状（先摆事实）

| 本项目位置 | 现在是什么 | 与 Cube 的差距 |
|---|---|---|
| [core/entities.py](file:///Users/tj/Documents/trae_projects/策略回测agent/core/entities.py#L43-L68) | `Join(left,right,on,cardinality)`；5 个实体、4 条边；基数只允许 `N:1`/`1:1` | 没有 join 别名、没有显式路径 id、没有「路径冲突」概念 |
| [core/entities.py](file:///Users/tj/Documents/trae_projects/策略回测agent/core/entities.py#L73-L77) | `GRAIN_DIMENSIONS` 手工白名单，注释说它是白名单的「来源」，但代码里并未从 `JOINS` 推导 | Cube 的可用维度由 join 图 + 基数 + 显式 include 决定；我们是人手维护的第二事实源 |
| [core/entities.py](file:///Users/tj/Documents/trae_projects/策略回测agent/core/entities.py#L90-L111) | `path()` BFS 返回第一条路；只在 [selftest_v2.py](file:///Users/tj/Documents/trae_projects/策略回测agent/selftest_v2.py#L314-L315) 用 | 没有用于维度解析；多路径时静默取第一条，没有 `AMBIGUOUS_JOIN_PATH` |
| [core/catalog.py](file:///Users/tj/Documents/trae_projects/策略回测agent/core/catalog.py#L718-L719) | 维度归属只查 `GRAIN_DIMENSIONS[grain]` | 与 `JOINS` 无联动，加表/改基数不会自动影响可用维度 |
| [core/catalog.py](file:///Users/tj/Documents/trae_projects/策略回测agent/core/catalog.py) `MetricDef.dimensions` | 每个指标再写一份维度 tuple | 第 3 份副本 |
| [core/access.py](file:///Users/tj/Documents/trae_projects/策略回测agent/core/access.py) `ROLES[*].dimensions` | 角色再写一份可用维度 | 第 4 份副本（P-03 要求） |
| [core/semantic_contract.py](file:///Users/tj/Documents/trae_projects/策略回测agent/core/semantic_contract.py#L51-L78) | schema enum 再写一份 | 第 5 份副本（P-05 要求） |
| `core/stats.py` / `core/model_metrics.py` | 模型级 `DIMENSIONS` 是全局元组，`comparable_dimensions` 逐指标声明 | 与实体图完全无关；`day` 维度在模型侧是否可用靠手写 |

**关键判断**：我们的 join 图已经是「声明式」的，但**可用维度还没有从 join 图推导**。
Cube 的参考价值不在「再抄一个 join 图」，而在「把可用维度变成 join 图的投影，并给路径一个身份」。

---

## 1. joins：显式关系 + 编译期校验 + 查询期最短路

### 1.1 源码事实

**(a) schema 只接受三种规范关系（其余是别名），join 条件必须显式写。**

`packages/cubejs-schema-compiler/src/compiler/CubeValidator.ts:1208-1227`：

```ts
// joins 支持对象与数组两种写法；下面是对象写法（数组写法额外支持 alias）
joins: Joi.alternatives([
  Joi.object().pattern(identifierRegex, Joi.object().keys({
    sql: Joi.func().required(),
    relationship: Joi.any().valid(
      'belongsTo', 'belongs_to', 'many_to_one', 'manyToOne',
      'hasMany', 'has_many', 'one_to_many', 'oneToMany',
      'hasOne', 'has_one', 'one_to_one', 'oneToOne'
    ).required()
  })),
  // ... 数组写法：{ name, alias, sql, relationship }
])
```

**【事实】** `relationship` 是必填，规范类型只有 `belongsTo` / `hasOne` / `hasMany` 三类，
其余是别名（`CubeValidator.ts:1211-1215`）；`sql` 是函数，不允许字符串拼接。
这和我们 `Join.cardinality ∈ {"N:1","1:1"}` 的封闭思路一致，但 Cube 的 `hasMany` 在我们这里
被刻意禁止（我们只允许不放大基数的边）。

**(b) 编译期先建边，发现「乘法测度 + 对端无主键」直接报错。**

`JoinGraph.ts:97-155`：

- `buildJoinEdges` 遍历每个 cube 的 `joins`，把 `from → to` 和 `to → from` 都压进 `edges`；
- `joinRequired` 收集 `multipliedMeasures`；
- 若存在乘法测度，对端 cube 必须有 `primaryKey`，否则报
  `primary key for '${v}' is required when join is defined in order to make aggregates work properly`
  （`JoinGraph.ts:110-111`，校验在 `128-138`）。

**【事实】** Cube 把「一对多会导致重复计数」做成**编译期错误**，而不是运行时去重。
**【推断】** 这比我们「在 `build_view` 层按父键去重」更早暴露问题：错误发生在建模时，
而不是某次查询数字变大时。

**(c) 冲突 join 会被丢弃，而不是随便选一条。**

`JoinGraph.ts:157-284` `reportConflictingJoins`：同一对 cube 如果出现多条
`from/to` 相同但 `on` 不同的 join，会收集冲突并在编译期报错/丢弃冲突边。

**【事实】** Cube 不允许多条同名 join 边共存。
**【推断】** 我们的 `_neighbors()` 遇到重复边会全部返回，`path()` 取 BFS 第一条——
今天边少没出事，加第 5 张表时就会出现「同一条路两个 on 条件，静默选一个」。

**(d) 查询期用最短路，找不到路抛专用错误。**

`JoinGraph.ts:301-325` `buildJoin`：

- 对待 join 的 cube 集合求最小生成树（按 join 数量排序，贪心取最短边）；
- 若某个 cube 无法接入，抛 `JoinPathNotFoundError`（`UserError.ts:13`）。

`BaseQuery.ts:584-593` 还提供 `tryJoinTreeForHints`：只在 `JoinPathNotFoundError` 时返回空数组，
其他异常继续抛——**错误类型被用来区分「没路」和「真 bug」**。

**(e) 多路径歧义用 `joinHints` / `joinPath` 显式消解。**

- `BaseQuery.ts:600-639` `joinTreeForHints` 迭代收集 hint，最多 10000 次，检测环；
- `CubeSymbols.ts:236-243` 的 `ViewCubeInclude` 要求 `joinPath: () => ToString`；
- `CubeSymbols.ts:982-993` view 通过 `joinPath` 显式声明「从哪个路径取哪些成员」，
  路径最后一段可以是 join 别名；`includes` 不允许写路径（`CubeSymbols.ts:1010-1013`）。

**【事实】** Cube 的消歧手段是**声明式路径**，不是让运行时猜。
**【推断】** 这正好对应我们需要的 `path_id`：同一对实体有多条合法路径时，
必须由建模人指定走哪条，而不是 BFS 第一条。

### 1.2 偷什么

1. **把基数写进 join 声明，并在启动时校验「非放大」**。
   我们已有 `Join.cardinality`，但 `GRAIN_DIMENSIONS` 没读它。应让「从 grain 出发能到、
   且路径上每条边都是 `N:1`/`1:1`」成为可用维度的**唯一推导规则**。
2. **给路径一个稳定身份（`path_id`）**，并在回答信封里带上。
   Cube 用 `joinPath` 字符串；我们可以用 `Fact→Applicant→Loan` 这种有序边签名，
   或显式注册的 `path_id`。这是可溯源的关键。
3. **区分「无路径」和「路径歧义」两种错误**。
   Cube 只有 `JoinPathNotFoundError`；我们应拆成 `NO_JOIN_PATH` 与
   `AMBIGUOUS_JOIN_PATH`（新增错误码），前者说明不连通，后者说明有多条合法路、需要人工指定。
4. **编译期/启动期自检**：`JOINS` 与 `GRAIN_DIMENSIONS` 不一致时 fail-fast，
   而不是等某次查询返回 `NO_JOIN_PATH` 才发现。

**为什么偷**：我们的 join 图已经是声明式，缺的只是「路径身份」和「基数校验」；
这两样不需要新表、不需要新依赖，改动面小，却能同时补上歧义与重复计数两个漏洞。

### 1.3 不偷什么

1. **不偷 `sql: Joi.func()`**。P-05 明确禁止 `sql/expression/where`；
   Cube 允许任意 SQL 函数，等于把口径写进代码，跟我们「切点/口径不接受外部传入」冲突。
2. **不偷 `hasMany`**。我们的场景只要「不放大分母」的边；允许 `hasMany` 等于允许 fan-out，
   而 P-06 的可溯源和 `AnswerBook` 都没准备好处理重复计数。
3. **不偷「冲突 join 静默丢弃」**。我们可以更严格：冲突直接让 `make gate` 失败，
   而不是丢弃后继续跑。
4. **不偷 `primaryKey` 的隐式约定**。Cube 只在「有乘法测度」时要求主键；
   我们应要求**所有实体**都有主键（现在 `Entity.key` 已有，保持必填即可）。

**为什么不偷**：`sql` / `hasMany` / 隐式主键都会把「口径」重新写回代码或放大分母，
和 P-05 的 schema 封闭、P-06 的可溯源直接冲突；冲突静默丢弃则会让建模错误逃过门禁。

### 1.4 对本项目的缺口

- `GRAIN_DIMENSIONS` 未从 `JOINS` 推导（[core/entities.py](file:///Users/tj/Documents/trae_projects/策略回测agent/core/entities.py#L70-L77)）；
- `path()` 多路径静默取第一条，没有歧义错误（[core/entities.py](file:///Users/tj/Documents/trae_projects/策略回测agent/core/entities.py#L100-L111)）；
- `catalog.compute_metric` 的维度校验只看白名单，不看实际路径（[core/catalog.py](file:///Users/tj/Documents/trae_projects/策略回测agent/core/catalog.py#L718-L719)）；
- 回答信封没有 `path_id` / `grain` / 基数信息，无法在 trace 里复现「为什么这个维度可用」。

---

## 2. available_dimensions：服务端没有这个字段，只有四层叠加

### 2.0 名称澄清（重要）

**【事实】** 按 SHA 全仓 grep `available_dimensions` / `availableDimensions`，命中 6 个文件，
但没有一个是服务端模型字段：

- `packages/cubejs-client-core/src/Meta.ts:113-116` 的 `membersForQuery(_query, memberType)`；
- `packages/cubejs-client-react/src/QueryBuilder.tsx`、`types.ts`、
  `packages/cubejs-client-vue3/src/QueryBuilder.js`（前端组件的 `availableDimensions` 状态/prop）；
- 两份 SDK 文档（`docs-mintlify/reference/javascript-sdk/reference/cubejs-client-react.mdx`、`...vue.mdx`）；
- `packages/cubejs-schema-compiler/test/integration/postgres/cube-views.test.ts:510`
  里的**局部变量** `const availableDimensions = cube.config.dimensions?.map(...)`。

`cubejs-schema-compiler` / `cubejs-api-gateway` / `cubejs-server-core` 的源码中均无此字段。

而且 `Meta.membersForQuery` **完全忽略传入的 `query`**：

```ts
public membersForQuery(
  _query: DeeplyReadonly<Query> | null,
  memberType: MemberType
): (TCubeMeasure | TCubeDimension | TCubeMember | TCubeSegment)[] {
  return unnest(this.cubes.map((c) => c[memberType]))
    .sort((a, b) => (a.title > b.title ? 1 : -1));
}
```

**【事实】** 前端拿到的「可用维度」是**全量扁平列表**，不按当前 query 的 cube/join 过滤。
真正的过滤发生在服务端查询编译和 `accessPolicy`。

**【推断】** 任务里说的「available_dimensions」应理解为「可用维度建模」这一能力，
而不是某个具体字段。下面按服务端真实起作用的四层来记。

### 2.1 源码事实：四层叠加

**(a) cube 成员表：维度属于 cube，meta 全量暴露并带 `isVisible`。**

`CubeToMetaTransformer.ts:294-391`：

- 每个 cube 输出 `measures`、`dimensions`、`segments`；
- 维度条目含 `name/title/type/sql/format/meta` 等，并带 `isVisible`（`CubeToMetaTransformer.ts:314-340`，`isVisible` 在 `339`）；
- 每个 cube 还输出 `connectedComponent`（`CubeToMetaTransformer.ts:303`），
  由 `JoinGraph.connectedComponents()` 计算（`JoinGraph.ts:430-452`）。

**【事实】** meta 是「目录」，不是「当前 query 的可用集」；`connectedComponent` 只告诉前端
哪些 cube 连通，不替前端做裁剪。

**(b) view：用 `joinPath` + `includes`/`excludes` 显式定义「对外暴露哪些成员」。**

`CubeSymbols.ts:979-1068`：

- 每个 `cubeInclude` 必须求值 `joinPath`（`CubeSymbols.ts:983-987`）；
- `includes: '*'` 展开目标 cube 的全部成员（`CubeSymbols.ts:999-1005`）；
- 显式 include 只允许成员名，不允许路径（`CubeSymbols.ts:1010-1013`）；
- `excludes` 从 include 结果里扣掉（`CubeSymbols.ts:1052-1068`）；
- `prefix` / `alias` / `split` 控制重名与拆分视图（`CubeSymbols.ts:993-995, 1070+`）。

**【事实】** view 是「可用维度的显式清单 + 路径绑定」，不是运行时推导。
**【推断】** 这是 Cube 最值得偷的一层：把「哪些维度对外可用」变成一份**可评审的声明**，
而不是四份手写白名单。

**(c) accessPolicy：`memberLevel` 决定角色能看见哪些成员。**

`CubeValidator.ts:1128-1145`：

- `memberLevel` 必须有 `includes` 或 `excludes` 之一；
- **空 `memberLevel` 等于授予全部成员**（源码注释明确警告「the opposite of how it reads」，
  `CubeValidator.ts:1140-1144`）。

`CubeEvaluator.ts:512-576` `prepareAccessPolicy`：

- `memberLevel.includes` / `excludes` 解析成 `includesMembers` / `excludesMembers`；
- `memberMasking` 必须依附 `memberLevel`（`CubeValidator.ts:1177` `.with('memberMasking','memberLevel')`）；
- `rowLevel` 与 `memberLevel` 独立，`filters`/`allowAll` 二选一（`CubeValidator.ts:1160-1163`）。

**【事实】** Cube 的权限是「成员可见性 + 行级过滤」两套；`memberMasking` 是「先看见再遮值」。
**【推断】** 我们的 P-03 是「越权零数字、连 enum 都不进」，比 Cube 的成员级授权更严格；
Cube 的 `memberMasking` 正是我们第四个「停」里禁止的「先查出来再脱敏」，不能偷。

**(d) 查询期：成员属于哪个 cube，决定 join 需求。**

`BaseQuery.ts` 的 `collectJoinHintsFromMembers` / `joinMembersFromJoin` 会从查询成员反推
需要 join 的 cube 集合，再交给 `JoinGraph.buildJoin`。`BaseQuery.ts:600-639` 的迭代
hint 收集会循环到 join 树稳定（最多 10000 次，检测潜在环）。

**【事实】** 「某个维度能不能用」最终由「成员 → cube → join 图能否连通」决定。
**【推断】** 这就是我们该学的：可用维度不是一张静态表，而是「成员归属 + 图可达性 + 权限」的函数。

### 2.2 偷什么

1. **「可用维度 = 声明 + 投影」的两段式**：
   - 第一段（人写）：维度属于哪个实体、哪个字段；
   - 第二段（机器算）：从指标 grain 出发，沿 `N:1`/`1:1` 边可达的维度才可用。
   这样 `GRAIN_DIMENSIONS` 从「手写白名单」降级为「启动时计算出的缓存」。
2. **显式 include/exclude**：如果将来要限制「某指标只对外暴露部分维度」，
   用一份可评审的配置，而不是在渲染层写 `if product == ...`（P-03 的第四停）。
3. **meta 与权限分离**：目录（有哪些维度）和授权（这个角色能用哪些）分开，
   但授权必须在**调用时**算，且未授权维度连 schema enum 都不进——这一点我们已比 Cube 严，保持。

**为什么偷**：我们已经有 join 图和基数声明，只差把它们变成可用维度的推导函数；
这是把六处手写声明收敛成一处声明 + 投影的机会，且不改查询主链路的计算逻辑。

### 2.3 不偷什么

1. **不偷 `membersForQuery` 的「忽略 query」实现**。它是前端 UI 的便利函数，
   不是权限/口径边界；我们若照抄，会把「全量维度」当成「当前可用维度」。
2. **不偷 `memberMasking`**（先返回再遮值）。P-03 要求越权零数字，遮值已经越权。
3. **不偷「空 memberLevel = 全量授权」的默认值**。这是 fail-open，和 P-03 的
   fail-closed 相反；我们的未知角色必须直接拒绝。
4. **不偷 `connectedComponent` 作为可用性判断**。连通 ≠ 不放大基数；
   我们的可用维度还必须满足路径上每条边非 `N:1` 反向放大。

**为什么不偷**：`membersForQuery` 忽略 query 是 UI 便利，`memberMasking` 和空 `memberLevel`
都是 fail-open；照抄会把 P-03 的「越权零数字」退化成「先看见再遮住」。

### 2.4 对本项目的缺口

- 可用维度至少 5 处声明点（可聚合 4 处 + 模型级 2 处；模型级两项分开算为 6 处，见 §0 表），
  Cube 只有「声明 + 计算」两段；
- `MetricDef.dimensions` 与 `GRAIN_DIMENSIONS` 可能互相矛盾，且没有启动期断言；
- 模型级 `DIMENSIONS` 是全局枚举，`comparable_dimensions` 逐指标声明，
  但二者与 `Score` 实体的 join 能力没有关系（例如 `day` 是否真的能从 `Score` 走到，
  现在只靠 `GRAIN_DIMENSIONS` 的 `APPLY_DATE` 一行字）；
- 没有 `AMBIGUOUS_JOIN_PATH` 错误码，多路径时无法告诉用户「需要人工指定」。

---

## 3. pre-aggregation：人声明、查询期匹配、刷新可调度

### 3.1 源码事实

**(a) pre-aggregation 是 schema 里的显式声明，字段封闭。**

`CubeValidator.ts:563-614`（pre-aggregation schema）核心字段：

- `type`：`rollup` / `originalSql` / `rollupJoin` 等；
- `dimensions` / `measures` / `timeDimension` / `granularity`；
- `partitionGranularity`：`hour|day|week|month|quarter|year`（`CubeValidator.ts:608`）；
- `refreshKey`：`{ every, sql, immutable, updateWindow }`（`CubeValidator.ts:543-561`）；
- `indexes`、`scheduledRefresh` 等。

**【事实】** 没有任何「自动推断维度组合」的字段；维度和测度必须人写。
**【推断】** 这与我们 `scheduler/jobs.py` 的「冻结参数调同一函数」同构：
物化清单是声明，不是运行时生成。

**(b) 查询期匹配：measure 必须是子集，维度/过滤必须被覆盖，按声明顺序 first-match。**

`PreAggregations.ts:805-1019` `canUsePreAggregation`：

- `canUsePreAggregationNotAdditive`：查询的 dimensions 必须等于 rollup 的 dimensions，
  或所有过滤维度都在 rollup dimensions 内且单值相等（`PreAggregations.ts:841-870`）；
- `canUsePreAggregationLeafMeasureAdditive`：查询 measures 必须是 rollup measures 的子集
  （`PreAggregations.ts:914-999`）；
- 时间维度粒度必须匹配（`queryGranularityForRollup`）。

`PreAggregations.ts:1053-1066` `findPreAggregationForQuery`：

```ts
this.preAggregationMatch =
  this.rollupMatchResults()
    // Refresh worker can access specific pre-aggregations even in case those hidden by others
    .find(p => p.canUsePreAggregation && (!this.query.options.preAggregationId || p.preAggregationId === this.query.options.preAggregationId)) ?? null;
```

`rollupMatchResults` 的注释明确写了返回顺序：**「in the same order they appear in the schema file」**
（`PreAggregations.ts:1113-1116`），因此这里不排序，`.find()` 取到的就是 schema 里第一个可用的 rollup。

**【事实】** 多个 rollup 都能服务同一查询时，按 schema 声明顺序取第一个，不按成本/新鲜度选。
**【推断】** 「顺序即优先级」意味着**声明顺序本身是口径的一部分**；这必须版本化，
否则改一下文件顺序就会静默换数据源。我们若做物化，应把 `rollup_id` 写进回答信封。

**(c) OSS 不做自动 rollup。**

`PreAggregations.ts:1044`：

```ts
getCubeLattice(...) {
  throw new Error(`Auto rollups supported only in Enterprise version: ${cube}`);
}
```

`PreAggregations.ts:1116-1144` 的 `rollupMatchResults` 只对已声明的 `preAggregations`
做匹配，不生成新组合。

**【事实】** 开源 Cube 的自动预聚合是商业版能力；OSS 只有「人声明 + 匹配 + 调度刷新」。
**【推断】** 这条直接支撑 ADR-006：连 Cube 都不在 OSS 里自动决定物化组合，
我们在信贷口径上更应人工审批。

**(d) 刷新是调度器按 `refreshKey` 驱动的，不是查询时顺手建表。**

`RefreshScheduler.ts:153-256`：

- `refreshQueriesForPreAggregation` 先把 pre-aggregation 编译成 refresh query；
- `expandPartitionsInPreAggregations` 展开分区；
- 若 `preAggregationDescription` 不存在（例如两个同定义但不同分区粒度的 rollup），
  返回 `Unused pre-aggregation`（`RefreshScheduler.ts:198-202`）；
- `originalSql` 类型若无 measure/dimension 直接抛错（`RefreshScheduler.ts:235-239`）。

**【事实】** 物化由独立调度器执行，查询只读已构建的分区；没构建好就是 cache miss 或等待。
**【推断】** 这正是我们 `scheduler/` 的方向：查询路径不负责建表，避免「查一次建一次」的副作用。

### 3.2 偷什么

1. **「声明 → 匹配 → 调度」三段式**：
   - 声明：物化哪些维度/测度（人审批）；
   - 匹配：查询期只做子集/覆盖判断，不生成新组合；
   - 调度：独立 job 按 `refreshKey`/分区刷新。
   我们已有 `scheduler/jobs.py` 和 `promotion.py`，缺的是「匹配」这一层——但先别急着写，
   因为匹配一旦自动，就会绕开 P-01 的唯一入口。
2. **`partitionGranularity` 的枚举化**。我们的 `GRAIN_DIMENSIONS` 已经有 `month`/`day`；
   若要物化，分区粒度也应是封闭枚举，且与授权维度一致。
3. **「未使用/不匹配」要显式返回原因**（Cube 的 `Unused pre-aggregation`），
   而不是静默回退全量计算。对应我们的 D2/D3 降级：必须记录为什么没用物化。

**为什么偷**：声明/匹配/调度三段式能让我们在不新增计算入口的前提下物化，
正好符合 P-01/P-02；匹配层只读，不产生新的业务数字。

### 3.3 不偷什么

1. **不偷自动 rollup / lattice**。P-01/P-02 要求唯一入口和冻结参数；
   自动组合会新增计算路径。
2. **不偷「声明顺序 first-match」作为唯一优先级**。我们可以要求显式 `priority` 或
   唯一匹配；多个匹配时应报 `AMBIGUOUS_ROLLUP`，而不是按文件名排序。
3. **不偷 `originalSql` 类型**。它允许直接写 SQL，违反 P-05。
4. **不偷「查询时构建」**。物化构建必须是独立 job，查询只读。

**为什么不偷**：自动组合、顺序即优先级、`originalSql`、查询时构建，
都会让「同参同数」失去保证；这四条与 P-05/P-06/P-11 正面冲突。

### 3.4 对本项目的缺口

- 没有「物化候选 → 匹配 → 使用」的显式链路；`promotion.py` 只到「进队列」，
  没有 `rollup_id` 和匹配结果；
- 没有把「用了哪张物化表/哪条路径」写进回答信封；
- 没有 `AMBIGUOUS_ROLLUP` / `ROLLUP_NOT_READY` 这类错误码；
- `scheduler` 的 job 参数已冻结，但缺少「job 对应的 join path 版本」记录。

---

## 4. 三处合起来：join path 建模的可搬运结论

把 joins、可用维度、pre-aggregation 放在一起看，Cube 的套路是一条链：

```text
schema 声明关系与基数
  → 编译期建图、校验主键、丢弃/报告冲突
  → view 用 joinPath 显式绑定「哪些成员从哪条路来」
  → 查询期按成员反推 cube 集合、最短路建 join
  → pre-aggregation 人声明、按声明顺序匹配、独立调度刷新
```

**【推断】** 对我们最有价值的三条：

1. **路径要有身份**。Cube 用 `joinPath` 字符串/别名；我们至少要给每条被使用的路径一个
   `path_id`，并写进 trace/信封。没有身份，就无法回答「为什么这个维度和那个指标能一起算」。
2. **可用维度是投影，不是副本**。Cube 的 view `includes` 是唯一对外清单；
   我们应把 `MetricDef.dimensions` / `GRAIN_DIMENSIONS` / `ROLES[*].dimensions` /
   schema enum 收敛成「一份声明 + 启动时计算 + 三处只读投影」。
3. **物化决策必须人工审批**。Cube OSS 已经证明：自动 rollup 是商业版才敢做的事；
   OSS 只做「人声明 + 匹配」。我们的信贷口径更没有理由自动建表。

---

## 5. 明确不采用的方案（供 ADR-006 引用）

| 方案 | 为什么不采用 | 依据 |
|---|---|---|
| 运行时 BFS 自动选路 | 多路径时静默取第一条，口径不可复现 | Cube 在 view/多路径场景用 `joinPath` 显式声明；我们边少，但加表后必然歧义 |
| 自动 pre-aggregation / lattice | OSS 都不提供；会新增计算路径、绕开 P-01/P-02 | `PreAggregations.ts:1044` |
| 声明顺序即优先级 | 改文件顺序会静默换数据源，不可溯源 | `PreAggregations.ts:1053-1066`、`1113-1116` |
| 成员遮值（masking） | 先返回再遮 = 越权已发生 | P-03 + 第四个「停」；`CubeValidator.ts:1147-1158` |
| 空 memberLevel = 全量授权 | fail-open，与未知角色 fail-closed 相反 | `CubeValidator.ts:1140-1144` |

---

## 6. 取证清单（文件 + 行号 + SHA）

| 主题 | 仓库路径（`e6c64f6`） | 关键行 |
|---|---|---|
| join schema | `packages/cubejs-schema-compiler/src/compiler/CubeValidator.ts` | 1208-1227 |
| join 建边/主键校验 | `packages/cubejs-schema-compiler/src/compiler/JoinGraph.ts` | 97-155 |
| join 冲突 | 同上 | 157-284 |
| 最短路/无路径错误 | 同上 | 301-325 |
| 乘法因子/连通分量 | 同上 | 397-452 |
| join hints 迭代 | `packages/cubejs-schema-compiler/src/adapter/BaseQuery.ts` | 584-639 |
| view joinPath/includes | `packages/cubejs-schema-compiler/src/compiler/CubeSymbols.ts` | 979-1068 |
| meta 输出/isVisible | `packages/cubejs-schema-compiler/src/compiler/CubeToMetaTransformer.ts` | 294-391 |
| 前端 membersForQuery | `packages/cubejs-client-core/src/Meta.ts` | 107-116 |
| accessPolicy schema | `packages/cubejs-schema-compiler/src/compiler/CubeValidator.ts` | 1128-1177 |
| accessPolicy 解析 | `packages/cubejs-schema-compiler/src/compiler/CubeEvaluator.ts` | 512-576 |
| pre-agg schema | `packages/cubejs-schema-compiler/src/compiler/CubeValidator.ts` | 543-614 |
| pre-agg 匹配 | `packages/cubejs-schema-compiler/src/adapter/PreAggregations.ts` | 805-1066 |
| OSS 无自动 rollup | 同上 | 1044, 1116-1144 |
| 刷新调度 | `packages/cubejs-server-core/src/core/RefreshScheduler.ts` | 153-256 |

> 本地取证副本（临时目录，不作为交付物）：
> `/tmp/cube-e6c64f6dedb59142c9d8e72466915ad72734b294/`

---

## 7. 待办落点（只记录，不在 R-003 实施）

- `docs/tasks.md` 新增 T-3xx（或 S1.5 内）：
  - 路径身份：`path_id` / `AMBIGUOUS_JOIN_PATH`；
  - 可用维度收敛：`GRAIN_DIMENSIONS` 改为启动期从 `JOINS` 推导；
  - 回答信封加 `path_id` / `grain` / `cardinality`；
  - 物化匹配层：显式 `rollup_id` + `AMBIGUOUS_ROLLUP`，不做自动组合。
- 上述每一条都必须在 `docs/tasks.md` 有落点后才动代码，遵守 AGENTS.md 的三、四节。

---

## 8. 诚实边界

- 本次只读源码，未运行 Cube；所有行为结论均来自静态代码，未做运行时实验。
- Cube 的 `joinPath` 还涉及 `extends`、视图拆分、`include` 别名等细节，本文只覆盖与
  「路径身份/可用维度」直接相关的部分，未展开 `extends` 的合并顺序。
- 「available_dimensions 服务端不存在」这一结论基于该 SHA 的全仓 grep；如果 Cube 后续版本
  新增同名字段，需要重新取证。
