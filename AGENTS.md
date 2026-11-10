# AGENTS.md — 项目宪法 · 策略回测分析 Agent V2（S1—S5 开发期）

> **本文件是这套文档体系的最高约束。** 技术栈、硬规则、禁止清单都是锁定的。
> Trae 不会自动读本文件，所以**硬规则同时写进了 `.trae/rules/project_rules.md`**（两份必须同步，改动以本文件为准）。
> 设计依据：《策略回测分析 Agent V2 企业版 · 重设计方案 V2.5》§8—§13。
> 当前基线：S0 + S0.5 + S0.9 已交付，`make gate` 6/6 全绿（84 条自检 + 18 个评估器 + 7 题字节级基线 + D 组语义一致性）。

## 〇、效力与编号

1. 本文件与任何其他文档冲突时，**以本文件为准**；发现冲突改另一份，不搞「两边各改一点」。
2. 硬规则编号 `P-xx`；其他文档**引用编号，不复制原文**（复制即产生第二份会漂移的文本）。
3. 改技术栈必须同时补 `docs/adr/`；改硬规则必须检查所有引用。

## 一、这个项目是什么

- **做什么**：把自然语言问题变成「带口径信封、可溯源的策略/模型指标回答」。核心不是聊天，而是**一个受约束的指标查询与计算网关**。
- **给谁用**：风控策略团队 2—5 人（内部）；数据是真实信贷与模型数据，**不出域**。
- **不做什么**（握手探针，必须能脱口而出）：
  - 不做明细查询（「哪个客户」「这批名单」→ `NOT_VISIBLE`，不给替代数据）；
  - 不做自由 SQL / 自由表达式（schema 里没有 sql/expression 字段）；
  - 不做模型训练、不做策略自动上线、不做审批决策；
  - 不代替报表平台，只保证「同一个问题问它和问报表，数字逐位一致」；
  - **S1—S5 不引入前端界面**（CLI + MCP 接口为准），不做多租户。

## 二、技术栈（锁定）

| 层 | 选型 | 说明 |
|---|---|---|
| 语言 | Python 3.13 | 解释器固定 `/Users/tj/.workbuddy/binaries/python/envs/default/bin/python`（含 langgraph） |
| 编排 | LangGraph（StateGraph + InMemorySaver） | S3 换 `PostgresSaver`，接口不变 |
| 存储 | SQLite（开发/夹具）→ PostgreSQL（S3 起） | 上层只见 `core/db.Database` 接口 |
| 协议 | MCP（stdio / streamable-http） | 只读、超时 3s、单次 ≤200 行 |
| LLM | 公有云 API（qwen / deepseek 二选一），先接口后接 | 只经 `llm/call.py`；temperature=0；版本固定 |
| 测试 | 纯 Python 断言 + 自建评估器 | 不引入 pytest 作为主链路（保持零依赖可跑） |

## 三、硬规则

### P-01 两条唯一入口，不得新增第三条
模型级只能走 `core/model_metrics.compute_model_metric()`；可聚合只能走 `core/catalog` + `tools/compute_metric`。
`core/stats.py` 只许 `core/model_metrics.py` 导入（CI 门禁 1）。
**禁止** `report_calc.py` / `monitor_metrics.py` / `*_fast.py` 这类「报表专用计算」。

### P-02 定时任务 = 参数冻结的同一函数
job 只允许 `{job_id, metric_id, params(冻结), output_table}`；`run_job` 实现必须是一行 `compute_model_metric(...)`。

### P-03 权限在调用时判定
新指标/新工具必须在 `core/access.ROLES` 有归属；未知角色 fail-closed。
新维度必须同步三处：`GRAIN_DIMENSIONS`、`ROLES[*].dimensions`、`semantic_contract` 的 enum。
越权回答**不得出现任何数字**（`ev16` 会抓）。

### P-04 两本册子物理隔离
`MetricDef` 与 `ModelMetricDef` 不并表、不互借字段；`tools_for(branch, role)` 保证两册互斥注册。
新增模型级指标 = 提案 + `MODEL_CATALOG` + schema enum + 断言，四件事一件不能少。

### P-05 schema 封闭
`additionalProperties: False`；禁止 `sql/expression/formula/table/column/where/select/join`。
参数只收枚举：`bins∈{5,10,20}`、`label∈{fpd7,fpd30,mob3}`、`dimension∈{overall,product,channel,month,day}`。
**切点只能由 `stats.quantile_edges` 生成**，不接受外部传入。

### P-06 数字必须可溯源
先 `AnswerBook.put()` 再 `fmt()`。S4 的 LLM 叙述必须过 `verify_numbers`；
**未登记数字 → 丢弃叙述、回退模板**，不许「标红照发」。

### P-07 答案必带信封
可聚合四行；模型级加第五行【分箱】。允许「不适用」，禁止留空；`envelope.validate()` 失败即不合格。

### P-08 LLM 只有一个出口
禁止业务代码直连 provider；只调 `llm/call.py`。
每次调用记录 `model(具体版本)/temperature=0/seed/prompt_version/schema_hash/max_tokens`。
LLM 输出先过 `validate_plan()` 白名单。

### P-09 LLM 不许写记忆、不许碰口径
记忆仅系统侧写入（会话结束后），**不提供「写记忆」工具**。
可填仅 `product/window`；`FORBIDDEN` 口径字段永久禁止；补全必须在回答里声明。

### P-10 降级四级
D0 正常 / D1 叙述不过闸回退模板 / D2 规划失败回退规则解析 / D3 执行失败给说明书 / D4 熔断转人工。
新功能必须说明 D1—D3 时退化成什么；算不出就给错误码，**不许给替代数字**。

### P-11 改行为必须改基线
动渲染/意图/信封 → 先跑 `eval/regress_demo.py`。基线只允许在「行为按设计变更」时重录，
commit message 写明原因与影响题号；**为让测试变绿而重录 = 违规**。

### P-12 一条命令验全仓
提交前 `make gate` 必须 6/6。新增门禁必须进 `ci/gate_all.GATES`，否则等于没加。

## 四、当前阶段与范围

| 阶段 | 内容 | 状态 |
|---|---|---|
| S0 / S0.5 | 语义层骨架 + 两类指标分流 + 物化 + 77 断言 | ✅ 已交付 |
| S0.9 | 模块一收口（curve / 策略变量分 / owner / 升格下线 / 分流反问） | ⬜ 待做（`docs/tasks.md` T-100 系列） |
| S1 | LLM 规划接入 + 四级降级 + 成本预算 | ⬜ |
| S2 | MCP server 独立进程（5 个只读工具） | ⬜ |
| S3 | 记忆（MemoryItem）+ 上下文工程（token 预算 / 外卸 / 压缩） | ⬜ |
| S4 | LLM 叙述 + 出口闸门回退 | ⬜ |
| S5 | 140 用例 × 18 评估器 + trace-replay + Judge | ⬜ |

## 五、常用命令

```bash
make gate    # 5 道门禁（提交前必跑）
make test    # 两套自检（32 + 77）
make demo    # 10 题演示
make init    # 重建 artifacts/v2_dialogue.db
make sem     # 语义层评估器 ev13—ev18 + 反向测试
```

## 六、目录约定

```
core/        语义层与计算（唯一计算入口所在）
tools/       工具壳（LLM 能摸到的一切）
scheduler/   定时任务与物化
llm/         LLM 出口（S1 起才有实现，当前是桩）
memory/      记忆（S3 起才有实现，当前是桩）
mcp_server/  MCP 服务（S2 新建）
ci/          门禁脚本
eval/        评估器与回归基线
docs/        本套开发文档
.trae/rules/ Trae 常驻/按需规则
```
