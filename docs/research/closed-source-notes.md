# 闭源两家公开资料精读（R-004）

> 目的：从 Snowflake Cortex Analyst 与帆软（FineBI / FineReport）的**公开资料**里，
> 找两样东西：① 答不出来时怎么表达、怎么划边界；② 指标分级（指标树 / 红黄绿）怎么做。
> 数据来源：厂商官方文档与官方开发者指南，**无账号、未实测、未看演示视频（无公开文字稿）**。
> 访问日期：**2026-11-11**（全部链接均为当日访问）。
> 标注约定：**【事实】** = 官方文档原文可核对；**【原话】** = 逐字引用（英文保留原文）；
> **【推断】** = 我根据文档结构/代码位置做的判断，**不是产品承诺**；查不到就写「无公开依据」。
>
> 结论口径与 R-002 一致：**吸收 = 可以照着改我们的一处具体落点；不吸收 = 与 P-01/P-03/P-05/P-10 冲突或收益不抵成本，只记录。**

## 0. 一句话对照

| 维度 | Cortex Analyst | 帆软 FineBI / FineReport | 我们的 V2 |
|---|---|---|---|
| 语义层形态 | Semantic View（logical table / dimension / fact / metric / relationship） | 指标中心 / 指标模型（指标 + 维度 + 血缘） | `MetricDef` / `ModelMetricDef` 两本册子 |
| 未命中口径 | **没有「指标未注册」这个一等状态**（推断，见 §1.4），本质仍是 NL→SQL | 未命中标准指标 → 继续在模型里找路径 | `NOT_REGISTERED`，零数字，不降级 |
| 答不出来的出口 | `text` + `suggestion`（建议问题列表） | 指标树归因 / 预警任务 | 错误码 + 能力清单 + 四级降级 |
| 边界由谁定 | 语义模型里的**自然语言指令**（`question_categorization`） | 指标资产与看板配置 | 代码 + 枚举 + schema（可断言） |
| 分级 | 无「红黄绿」概念 | 条件颜色/形状 + 单阈值预警 + 督办 | `healthy_range` 已声明，回答层尚未使用 |

## 1. Snowflake Cortex Analyst

### 1.1 它怎么表示口径【事实】

- 语义层是 **Semantic View**：logical tables（业务实体）、dimensions（分类上下文）、
  facts（行级数值）、metrics（聚合 KPI）、relationships（join）。
  metrics 的定义是"把数据聚合成业务 KPI"，且 join path 预先声明。
  （[Cortex Analyst 概览](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-analyst)，访问 2026-11-11）
- YAML 里 `dimensions` / `time_dimensions` / `facts` / `metrics` 都带 `synonyms` 与 `description`；
  facts / metrics 还有 `access_modifier: public_access | private_access`。
  （[Semantic View YAML 规范](https://docs.snowflake.com/en/user-guide/views-semantic/semantic-view-yaml-spec)，访问 2026-11-11）
- `verified_queries` 存"问题 + 已验证 SQL"，可标 `use_as_onboarding_question`；
  官方称其作用是"Sample questions and their SQL answers guide query generation"。
  （同上；[Onboarding questions](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-analyst/suggested-questions-feature)，访问 2026-11-11）

**对照我们**：同义词我们**没有**（`core/` 下 grep `alias|synonym|别名` 无命中）；
`private_access` 是"定义期可见性"，我们 `ROLES` 是"调用期可见性"——后者更严，不改。

### 1.2 答不出来时，它到底返回什么【事实 + 原话】

REST API 的响应内容只有三种 `type`：`text`、`suggestion(s)`、`sql`，且 **`suggestion` 与 `sql` 互斥**。
（注意：同一页文档里内容类型一会儿写 `suggestion`、一会儿写 `suggestions`，JSON 示例用的是
`"type": "suggestions"`——**文档自身不一致**，抄协议时要按示例而不是按散文。）

【原话】"The `suggestion` content type is only included in a response if the user question was ambiguous
and Cortex Analyst could not return a SQL statement for that query."
（[Cortex Analyst REST API](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-analyst/rest-api)，访问 2026-11-11）

【原话】"`suggestions` (string): If SQL cannot be generated, a list of questions the semantic model can generate SQL for."
（同上）

官方给的响应示例（歧义问句）：

【原话】
```
{
 "message": { "role": "analyst", "content": [
   { "type": "text", "text": "Your question is ambiguous, here are some alternatives:" },
   { "type": "suggestions", "suggestions": [
       "which company had the most revenue?",
       "which company placed the most orders?" ] } ] } }
```
（同上）

另外三条边界，也是**文档明说**的：

1. 【原话】"Cortex Analyst is limited to answering questions that can be resolved with SQL.
   It does not generate insights for broader business-related queries, such as 'What trends do you observe?'"
   —— 这就是调研计划里说的"不预测原则"的公开依据（原文列在"多轮对话的 Known limitations"下，
   但说的是整体能力边界）。
   （[Cortex Analyst 概览 · Known limitations in multi-turn conversations](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-analyst)，访问 2026-11-11）
2. 【原话】"Cortex Analyst doesn't have access to results from previous SQL queries."
   —— 多轮只带历史问句，不带上一轮结果；要引用"第二个产品"就答不了。（同上）
3. `warnings` 是**独立通道**：【原话】"List of warnings from the analyst about the user's request."
   流式说明里补了一句【原话】"Warnings do not stop processing."
   （[REST API](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-analyst/rest-api)，访问 2026-11-11）

### 1.3 边界不在代码里，在提示词里【事实 + 原话】

越界拒答靠语义模型里的**自然语言指令**，不是枚举或代码守卫：

【原话】"They tell Cortex Analyst when to reject questions vs. accept them and when to ask clarifying questions."
（[Best Practices for Creating Semantic Views](https://www.snowflake.com/en/developers/guides/best-practices-semantic-views-cortex-analyst/)，访问 2026-11-11）

【原话】"Reject all questions asking about users. Ask users to contact their admin."
—— 文档接着说，Cortex Analyst 会拒掉这类问题，并回一条让用户联系管理员的消息。
（[Custom instructions](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-analyst/custom-instructions)，访问 2026-11-11）

缺参数也是同一条通道，用一个状态词表达：

【原话】"If the question asks for users without providing a product_type, consider this question UNCLEAR
and ask the user to specify product_type."（同上）

【推断】这套做法把"拒答 / 澄清 / 接受"三态交给模型判定，好处是改边界不用改代码，
代价是**不可断言**：同一个越界问句在两次请求间没有"必然拒绝"的保证，只能靠评估集统计命中率。
文档同一页还提示：通过 Cortex Agent 访问时，推荐直接写自然语言，而不是依赖 `UNCLEAR` 这类状态词
（状态词只在直连 Cortex Analyst API 时继续有效）——这说明状态词是提示词约定，不是硬协议。
（推断依据：同页 "Custom instructions through Cortex Agents" 与上文的对比）

### 1.4 与我们的 `NOT_REGISTERED` 逐项对照

【推断】Snowflake **没有把"指标不在册"做成独立响应状态**：文档只定义 `text` / `suggestion` / `sql`
三种内容类型，没有与之对应的语义错误码；无法出 SQL 时统一落到 `suggestion`。
所以它不存在可与 `NOT_REGISTERED` 逐字对照的"拒答文案"，只有"歧义"和"话题被指令禁止"两种退化形态。
（推断依据：REST API 的响应字段表 + custom instructions 的拒答示例，未见"metric not found"类状态）

| 维度 | Cortex Analyst | 我们的 V2 | 判定 |
|---|---|---|---|
| 「指标未注册」状态 | **文档未定义**；响应只有 text/suggestion/sql，未命中时给建议问题 | `NOT_REGISTERED` 一等错误码 | 我们更严，保留 |
| 拒答触发 | 语义模型里的自然语言指令（LLM 判定） | `catalog.guard_call` 第①步，代码判定 | 我们可断言，保留 |
| 拒答后给什么 | 建议问题（模型能出 SQL 的问法） | `message` + `registered=sorted(CATALOG)` | **可吸收**（见 §1.5） |
| 解释"我理解成了什么" | 成功时固定带一条 `text`："We interpreted your question as ..."；歧义时说 ambiguous | 失败走能力清单，开头是"可能是把业务口语说成了别的意思" | 不吸收（见下） |
| 越权 | `private_access` 定义期隐藏 + Snowflake RBAC 执行 SQL | `NOT_VISIBLE` 调用期判定，零数字 | 我们更严，保留 |
| 告警与失败分开 | `warnings` 不阻断；`error` 才终止 | 错误码 + 信封，无 warnings 通道 | 暂不需要 |

**为什么不吸收"解释我理解成了什么"**：Cortex 的 `text` 是模型生成的解释，可能把口径说错且不阻断；
我们的口径只能由册子和登记簿生成（P-06/P-09），多一句模型写的"我理解为……"就多一个漂移源。
同理，Cortex 的"contact your administrator"在 2—5 人内部团队里没有可执行对象，
不如我们直接摆能力清单。

### 1.5 建议（1 条，可落地）

**把 `NOT_REGISTERED` 带回的能力清单，从"全量内部 id 列表"改成"当前角色可见的示例问法"。**

依据不是审美，是两处同时成立：

1. **P-03 的角度**：`core/catalog.py` 在判定角色**之前**就返回了 `registered=sorted(CATALOG)`。
   `business_viewer` 只被授权 4 个指标（`core/access.py` 的 `ROLES`），但它随手问一个不存在的口径，
   就能拿到包含 `fpd30_rate`、`mob3_bad_rate` 在内的全量 id——等于用错误信息反查出"系统里还有什么"。
   `NOT_VISIBLE` 的措辞红线是"不得暗示系统里有"，这条路径绕过了那条红线。
   （泄露点在 [catalog.py](file:///Users/tj/Documents/trae_projects/策略回测agent/core/catalog.py#L699-L703)；
   [tools/registry.py](file:///Users/tj/Documents/trae_projects/策略回测agent/tools/registry.py#L70-L76)
   是 `check_metric` 在前，风险较小，但改造时仍要保证两处口径一致）
2. **Cortex 的角度**：它的 `suggestion` 是"the semantic model can generate SQL for"的**问法**，
   不是内部字段清单；用户拿到的是"下一步能问什么"，不是"系统里还有哪些 id"。

具体做法（**只写方案，不动代码**）：

- `guard_call` 先取 `pol = access.policy(role)`，`registered` 改为
  `[mid for mid in sorted(CATALOG) if pol.check_metric(mid).get("ok")]`；
- 渲染/工具层把 id 映射成 `MetricDef.typical_questions[0]`（册子已有该字段），
  没有 `typical_questions` 的退回 `name`；
- 加 1 条正向 + 1 条反向断言：`business_viewer` 问不存在的口径，返回里**不含**未授权 id；
  `risk_analyst` 仍能看到全量（行为不变）。

成本 S，但不属于 R-004 范围——**需先在 `docs/tasks.md` 立 T-xxx 才能动代码**。
`core/catalog.py`、`tools/registry.py`、`tools/compute_metric.py` 三处都要看，
其中 `tools/registry.get_metric_definition` 的 `check_metric` 在前，风险较小；
`catalog.guard_call` 的路径才是真正需要改的。

### 1.6 明确不改的部分

- **错误码枚举不动**：Cortex 没有"未注册"状态，是因为它允许模型在语义模型范围内继续生成 SQL；
  我们 P-01/P-05 禁止这条路。所以它没有可对照的"拒答文案"，我们不应为了"像它"而软化。
- **文案主体不动**：`"这个口径还没有定义（不在册）"` 保留。它区分了"不是你没权限"和"不是算错"，
  比 Cortex 的"联系管理员"对本项目更有用。
- **不加 warnings 通道**：我们的 `healthy_range` 应该进信封（见 §2.4），不需要再开一个非阻断的平行通道。

## 2. 帆软（FineBI / FineReport）

### 2.1 指标树是什么【事实】

帆软公开文档里有两个容易混的东西，先分开：

| 名称 | 所在版本 | 文档定义 | 实质 |
|---|---|---|---|
| 指标中心 / 指标模型 | 7.0 起 | 统一模型、指标加工、指标管理与指标应用，带**指标血缘**（前溯数据集、后追看板/派生指标） | 指标资产目录 + 语义层 |
| 指标树（经营指标树） | 7.0 叫"经营指标树"，7.5 叫"指标树"分析作品 | 【原话】"以核心指标为起点逐层拆解，用于定位指标异常环节" | 归因/拆解结构，不是目录 |

【原话】"新增「指标树」分析作品，与仪表板同级，以核心指标为起点逐层拆解，用于定位指标异常环节。"
（[FineBI 更新日志索引 · 7.5](https://help.fanruan.com/finebi/doc-view-1236.html)，页面最近更新 2026-10-29，访问 2026-11-11）

【原话】"经营指标树把公司目标拆解成一条清晰的指标路径……当回款金额、合同回款率、复购完成率等指标出现异常时，
问题会在指标树上直接暴露出来。"
（[FineBI 7.0 更新说明](https://help.fanruan.com/finebi/doc-view-2691.html)，页面最近更新 2026-08-12，访问 2026-11-11）

配套三个能力也在 7.5 同一段列出（【原话】）：目标值/实际值/达成率；从指标卡发起智能归因；
归因结果可创建督办任务、预警任务可说明触发原因。（同上更新日志页）

### 2.2 红黄绿到底在哪一层【事实】

公开资料能核到的"红黄绿"有三处，**都不在指标定义里**：

1. **KPI 指标卡的区间着色**：官方示例把销售额分三段——
   【原话】"0-300000000为红色，300000000-350000000为橙色，350000000以上为绿色"，
   并勾选"数值颜色随条件变化"保证数字与条件同色。
   （[KPI指标卡](https://help.fanruan.com/finebi/edition-view-25637-50.html)，访问 2026-11-11）
2. **条件属性/形状**：表格与指标卡可按数值区间或排名配颜色/形状；
   5.1 白皮书里的说法是"用红绿灯的形状突出显示"。
   （[表格属性](https://help.fanruan.com/finebi/doc-view-124.html)；[FineBI V5.1 白皮书](https://www.finebi.com/finebi-white-paper2019.pdf)，访问 2026-11-11）
3. **数据预警任务**：对**组件里的结果字段**设阈值（示例：一次性付款合同金额 > 3000000），
   【原话】"每半小时进行检测，每天通知一次"，通知本人及所有对外查看用户；
   责任人会自动生成待办，处理完在"管理系统>数据预警"里点「解决」。
   （[数据预警示例](https://help.fanruan.com/finebi/edition-view-23704-0.html)，访问 2026-11-11）

【无公开依据】没有找到"按指标值自动判定红/黄/绿三级、并自动分派不同处置级别"的官方能力说明。
公开资料里，"红黄绿"是**展示层的条件格式**，"预警"是**单阈值 + 定时检测 + 通知 + 人工解决**，
两者没有文档化的联动，更没有"三级自动分级"的判定引擎。这一条不猜。

### 2.3 指标树对风控场景的启示（1 条）

**树边必须是册子里声明的指标恒等式，不是前端/模型临时决定的拆解顺序。**

帆软的指标树能成立，是因为它先有"指标中心 + 血缘"：拆解方向来自已登记的资产关系，
而不是让 AI 猜"通过率下降是不是因为某个策略节点"。我们这边：

- `MetricDef` 目前**没有** `depends_on`（R-002 的 G-2 缺口），树边无处声明；
- 模型级指标不可加、不参与 LMDI（`get_metric_definition` 已明说），
  所以 `MOB3` 只能"按产品/渠道拆开看"，**不能**做成"策略节点逐层贡献"的树——
  那不是恒等式，是把相关性画成了因果。

风控场景的落法：只对能写成恒等式的指标建树（如 放款额 = 申请量 × 通过率 × 件均；
通过率 = 1 − 拒绝率），其余指标在树上是"叶子"，只允许按声明维度下钻，不允许继续拆。
`depends_on` 补齐之前，**不建树**——否则会造出一棵看起来专业、但每一层都对不上总数的树。

### 2.4 红黄绿对风控场景的启示（1 条）

**阈值进册子，颜色由代码算，回答必须"数字 + 状态 + 阈值来源"同时出现；算不出来时不允许给颜色。**

帆软把阈值放在**组件**上（"对组件中参与分析的结果字段设置预警条件"），
同一指标换个看板就可以换一套红黄绿——这在 BI 里是灵活，在风控口径里是事故：
两个人看同一个 FPD7，颜色不同，然后各自去汇报。

我们的册子其实已经有 `healthy_range`（`low/high/direction`，如 FPD7 `high: 0.05, lower_better`），
但**回答层没有用它**：目前只在 `catalog.contract()` 里被导出，渲染时既不判定也不声明。
这正好是 `research-semantic-layer.md` 吸收清单第 ⑤ 条"healthy_range 命中 → warning 强制声明"。

具体建议（**只写方案，不动代码**）：

- 在信封里加固定一行：`状态：正常 / 关注 / 异常（阈值来源：册子 vX，direction=lower_better，high=0.05）`；
- 判定由 `core` 的纯函数做，LLM 不得改（P-06/P-08）；数字仍以登记簿为准；
- `VINTAGE_BLOCKED` / `NOT_APPLICABLE` / `NOT_REGISTERED` 时**不出状态行**——
  没有数字就没有颜色，避免"红/绿"替代"算不出来"；
- 断言：FPD7 超阈值 → 有"异常"且阈值来源可核对；`healthy_range={}` 的指标 → 不出现状态行。

成本 S，但同样**需先立 T-xxx**；建议与 ⑤ 合并成一个任务，不要拆两次改渲染。

### 2.5 明确不吸收的部分

- **未命中标准指标就继续找路径**：7.0 文档原话是"未命中指标时，Dora 也可以继续参考语义层中的
  模型结构、维度层级和多表关联，找到更可信的数据路径"。
  这正是我们的 `NOT_REGISTERED` 要堵的口子：路径"更可信"仍是模型选的，不是册子定的。
  保持 fail-closed，不给替代数字（P-10）。
- **预警督办工作流**（责任人、待办、解决状态）：属于工单系统，S1—S5 明确不做；
  我们只做到"回答里带状态"，不做到"派人"。
- **指标卡的形状/图形编码**（四分之三饼图等）：前端表达，不属于本项目的回答协议。

## 3. 结论与归属

| # | 结论 | 类型 | 归属 |
|---|---|---|---|
| 1 | Cortex 没有"指标未注册"一等状态（推断）；未命中给"能答的问法"，不是 id 全集 | 推断 + 事实 → 建议 | **立 T-xxx**：`NOT_REGISTERED` 的能力清单按角色过滤 + 换成 `typical_questions` |
| 2 | 帆软指标树成立的前提是"指标资产 + 血缘"，树边必须是恒等式 | 事实 → 启示 | **挂 R-002 G-2**（`depends_on`）；补齐前不建树 |
| 3 | 红黄绿在帆软是展示层条件格式 + 单阈值预警，未找到三级自动分级的公开依据 | 事实 + 无公开依据 | **立 T-xxx**：`healthy_range` 进信封（吸收清单 ⑤） |
| 4 | Cortex 的拒答边界写在自然语言指令里，不可断言 | 事实 → 拒绝 | 不吸收；我们保持代码 + 枚举 |
| 5 | 帆软未命中指标仍继续找路径 | 事实 → 拒绝 | 不吸收；与 P-10 冲突 |

## 4. 未做的部分（诚实边界）

- **没实测 Cortex Analyst**：无 Snowflake 账号；所有结论来自官方文档与官方开发者指南。
  因此"它到底怎么拒答"只能引用文档与示例，**没有真实响应样本**。
- **没看公开演示视频**：只用了文档里的示例响应与官方教程，未找到带文字稿的演示记录。
- **没实测帆软**：未安装 FineBI / FineReport；指标树与红黄绿的描述来自官方帮助文档与白皮书，
  版本差异（7.0 叫经营指标树、7.5 叫指标树）按文档原样记录，未验证功能细节。
- **"红黄绿三级自动化分级"无公开依据**：这是本次最需要说清楚的一条——公开资料只支持
  "条件着色"与"单阈值预警"，不支持"三级自动判定并分派"。
- **同义词体系只做了字段级确认**：确认 YAML 有 `synonyms`，未展开"同义词如何影响召回"的评估。
