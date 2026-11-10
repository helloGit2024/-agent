# 语义层对标调研（S2 前置）

> 目的：在动手写 MCP server（S2）之前，把市面上"语义层 + 注册指标 + 口径守卫"的现成实现看一遍，
> 每一项产出必须**落回本仓库的一个具体决策**（吸收清单 5 条 / ADR / 文案）——
> 看完只写"人家做得真好"等于没看。任务编号 R-xxx，与 T-xxx 并行不冲突。

## 0. 背景结论（已查证，2026-11）

| 项目 | 四件套①②③④ | 开源 | 路线 |
|---|---|---|---|
| SuperSonic（腾讯音乐） | 语义层✅ 注册指标✅ 守卫◐（词典+权限） 未定义态◐ | ✅ AGPL？看 LICENSE | ChatBI + Headless BI 融合 |
| WrenAI（Canner） | 语义层✅（MDL/YAML） 注册指标✅ 守卫◐（编译期约束） 未定义态✘ | ✅ AGPL | 语义编译约束 SQL |
| Cube | 语义层✅ 注册指标✅ 守卫✅（AI API 出不了域） 未定义态✅ | ✅ Apache 2.0 | 纯语义层，无对话 |
| Snowflake Cortex Analyst | 四件全✅ | ✘ 闭源 | 受约束 NL2SQL + 拒答兜底 |
| 帆软 DORA / FineBI Next | 语义层✅ 指标中心✅（owner/血缘） 守卫◐ 未定义态✘（未命中→**降级找路径**） | ✘ 闭源 | **NL2DSL + 确定性引擎（与我们同路线）** |

四件套 = ①语义层 ②注册指标 ③口径守卫 ④"这个口径还没定义"是一等状态。
我们的差异化立场：**未定义 → 拒绝（不降级）；命中 → 确定性函数（不是生成）**。调研时逐家对照这条立场。

## 1. 任务清单

| ID | 任务 | 产出 | 验收 | 规模 |
|---|---|---|---|---|
| R-001 | 跑通 SuperSonic + 读语义解析源码 | `research/supersonic-notes.md` | Docker 起服务；3 个语义问题（含 1 个模型里没有的）实测它的回应形态；源码三处精读（语义解析 / "不可答"状态 / 查询模式复用），每处笔记含**偷什么/不偷什么**结论 | L（约一周） |
| R-002 | 精读 WrenAI 的 MDL 与 wren-core 校验管线 | `research/wrenai-notes.md` | MDL YAML schema 字段级精读；产出 **MDL ↔ `MetricDef` 15 字段映射表** + "我们缺的字段"清单（重点：joins / available_dimensions / synonyms） | M |
| R-003 | Cube 代码级细节补课（报告已做过六控制对照） | `research/cube-notes.md` + `adr/ADR-006-draft.md` | 只补三处源码细节：joins 声明式写法、available_dimensions、pre-aggregation（对照我们的升格队列，写明"为何选人工审批"）；落成 **ADR-006：join path 与可用维度建模** 草稿 | S |
| R-004 | 闭源两家只读公开资料（Snowflake / 帆软） | `research/closed-source-notes.md` | Snowflake：语义模型 YAML schema、同义词体系、"不预测原则"的拒答文案——对照我们 `NOT_REGISTERED` 文案给 1 条改进建议或明确"不改"；帆软：指标树归因、**红黄绿三级自动化边界**——各写 1 条"对风控场景的启示"，落到风险登记或 ADR | S |

**S1.5 出口**：四份笔记 + 吸收清单 5 条逐条有归属（吸收→挂任务 / 拒绝→写明理由 / 待定→挂 OPEN-QUESTIONS）+ ADR-006 草稿。
**时间约束**：R-001 与 S1（T-201..208）**并行**；全部完成是 S2 开工的**软门槛**——不完成也可以开工 T-301（MCP 与语义层无关），但 T-306/T-307 前必须收口。

## 2. 调研必须回答的五个问题（每份笔记的公共骨架）

1. 它怎么表示"口径"？字段有哪些，哪些我们 `MetricDef`/`ModelMetricDef` 没有？
2. 它怎么守"口径"？是**事前约束**（schema/编译）还是**事后校验**？我们能搬哪一层？
3. "模型里没有"时它给用户什么？（拒绝 / 降级 / 建议问题列表）——与我们 `NOT_REGISTERED` 文案逐句对比。
4. 它怎么处理"高频长尾"？（自动 pre-aggregation / 人工审批 / 无此概念）
5. 权限在哪一层？（编译进指标 / RBAC 外挂 / 行级策略）

## 3. 与吸收清单的对应（调研完成后回填归属）

| 吸收清单条目 | 主要参考 | 预期归属 |
|---|---|---|
| ①计算下推接口隔离（SQLite→ClickHouse 双跑门禁） | WrenAI（wren-core 多源编译） | 待定 → S2 后评估 |
| ②dbt 式指标级声明式断言（assertions 挂 MetricDef） | WrenAI MDL + dbt MetricFlow | 吸收 → T-1xx（S3 后） |
| ③join path / available_dimensions 显式建模 | Cube（R-003）+ SuperSonic | 吸收 → **ADR-006** |
| ④golden plans 检索库（澄清过的问法回流） | Vanna + SuperSonic 查询复用 | 待定 → S3 记忆一起做 |
| ⑤healthy_range 命中 → warning 强制声明 | Cube rolling window / Snowflake | 吸收 → 小改动，S1 期间顺手 |

> 纪律：调研产出**只进文档**（`docs/research/`、`docs/adr/`），不直接改 `core/`——
> 任何要动的代码必须先变成任务（T-xxx）走 `make gate`，不允许"看源码看得手痒顺手改"。
