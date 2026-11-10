# SuperSonic 语义层调研笔记（R-001）

> 目的：看清 SuperSonic 怎么把一句人话变成可执行查询，判断哪些设计值得吸收、哪些必须拒绝。
> 版本：`e3928d0`（2026-11-06，浅克隆 `--depth 1`）。
> 阅读范围：`headless/chat`（mapping / parsing / query）、`headless/server`（工作流编排）。
> 结论口径：**"偷"= 可以照着改我们的语义层；"不偷"= 与 P-05/P-06/P-10 冲突，只记录不采纳。**

## 0. 一句话对照

| 维度 | SuperSonic | 我们的 V2 |
|---|---|---|
| 输入 | 任意 BI 问题，可多轮 | 风控策略/模型指标问题，单轮 |
| 解析 | 规则解析 + LLM 生成 S2SQL 双路候选 | 规则意图 + LLM 计划（只出计划，不出 SQL） |
| 输出 | S2SQL → 物理 SQL → 结果集 | 登记簿 + 信封，不出 SQL |
| 失败 | `ParseState.FAILED` + 一句英文原因 | `NOT_VISIBLE` / `DENIED` 等错误码 + 零数字 |
| 口径 | 数据集可随时改，靠 prompt 约束 | schema 封闭，枚举固定，切点只由代码生成 |

## 1. 端到端链路（`ChatWorkflowEngine.start`）

`MAPPING → PARSING → S2SQL_CORRECTING → TRANSLATING → PHYSICAL_SQL_CORRECTING`

一个 `while` 循环按状态推进（[ChatWorkflowEngine.java](file:///tmp/supersonic/headless/server/src/main/java/com/tencent/supersonic/headless/server/utils/ChatWorkflowEngine.java#L38-L94)）。
两个"提前终止"点都在循环里，而不是抛异常：

- `MAPPING` 后 `mapInfo.isEmpty()` → `FAILED`，原因 `"No semantic entities can be mapped against user question."`
- `PARSING` 后 `candidateQueries.isEmpty()` → `FAILED`，原因 `"No semantic queries can be parsed out."`

## 2. 精读一：语义解析（SchemaMapper + Parser）

### 事实

1. **匹配器是责任链，异常静默吞掉**：`BaseMapper.map()` 里 `try { doMap() } catch (Exception e) { log.error(...) }`
   （[BaseMapper.java](file:///tmp/supersonic/headless/chat/src/main/java/com/tencent/supersonic/headless/chat/mapper/BaseMapper.java#L39-L44)）。
   一个匹配器崩了，链路照常往下走，只是少了一批候选。
2. **向量匹配默认不跑**：`EmbeddingMapper.accept()` 只在 `MapMode.LOOSE` 或 `Text2SQLType.LLM_OR_RULE` 时才为真
   （[EmbeddingMapper.java](file:///tmp/supersonic/headless/chat/src/main/java/com/tencent/supersonic/headless/chat/mapper/EmbeddingMapper.java#L28-L33)）。
3. **匹配阈值按模式逐级下调**：`STRICT=0`、`MODERATE=2`、`LOOSE=4`、`ALL=6`；
   实际阈值 = `threshold - mode.threshold * (threshold - minThreshold)/4`，地板是 `minThreshold`；
   `STRICT` 直接返回 `1.0`（[BaseMatchStrategy.java](file:///tmp/supersonic/headless/chat/src/main/java/com/tencent/supersonic/headless/chat/mapper/BaseMatchStrategy.java#L113-L120)、
   [MapModeEnum.java](file:///tmp/supersonic/headless/api/src/main/java/com/tencent/supersonic/headless/api/pojo/enums/MapModeEnum.java#L3-L10)）。
4. **同一元素多次命中只留最高相似度**：`addToSchemaMap()` 用 `removeIf` 替换更低分的同元素匹配
   （[BaseMapper.java](file:///tmp/supersonic/headless/chat/src/main/java/com/tencent/supersonic/headless/chat/mapper/BaseMapper.java#L57-L80)）。
5. **LLM 解析失败不向上抛**：`LLMSqlParser.parse()` 整个 `try` 包住，失败只 `log.error`
   （[LLMSqlParser.java](file:///tmp/supersonic/headless/chat/src/main/java/com/tencent/supersonic/headless/chat/parser/llm/LLMSqlParser.java#L25-L46)）。
6. **重试时故意升温**：首轮 `temperature=0`，失败后改成 `0.5` 再试，注释写的是"报错时增加随机性，减少无效重试"
   （[LLMSqlParser.java](file:///tmp/supersonic/headless/chat/src/main/java/com/tencent/supersonic/headless/chat/parser/llm/LLMSqlParser.java#L74-L81)）。

### 偷

- **"同元素取最高分"的收敛规则**：我们现在的意图解析对同一指标多次命中没有排序，只是"命中即用"。
  加一条"同 key 取最高置信度"能减少歧义，且不改口径。
- **匹配器分级 + 最低分地板**：我们的 `catalog.resolve()` 只有硬阈值。可拆成"严格/宽松"两档，
  但宽松档只能用于**提示澄清**，不能用于直接给数（否则违反 P-10）。

### 不偷

- **向量检索默认参与**：引入 embedding 就把"同问同数"变成"同问大概率同数"，与 P-11 回归基线冲突。
- **失败静默吞异常**：我们的 D0—D4 要求每级降级可观测（P-10），静默 catch 会让"算不出"伪装成"没问对"。
- **失败后升温重试**：我们的 P-08 固定 `temperature=0`。升温换来的召回不可复现，风控答案不接受。

## 3. 精读二："不可答"状态怎么表达

### 事实

1. 只有两种终止语义，都挂在 workflow 状态机上：**映射不出实体**、**解析不出查询**（见 §1）。
2. 没有错误码枚举，原因是中英文自然语言字符串，直接进 `ParseResp.errorMsg`。
3. LLM 路径的"不可答"是**隐式**的：SQL 不合法被过滤（`SqlValidHelper.isValidSQL`），
   全被过滤后 `sqlRespMap` 为空，`tryParse()` 直接 `return`，候选集合仍为空 → 最终落到 PARSING 的 FAILED
   （[LLMResponseService.java](file:///tmp/supersonic/headless/chat/src/main/java/com/tencent/supersonic/headless/chat/parser/llm/LLMResponseService.java#L68-L88)）。
4. 映射阶段失败与解析阶段失败**文案不同**，但结构相同，调用方只能读字符串区分。

### 偷

- **按阶段区分失败原因**：我们目前"算不出"统一走 D3 说明书。可细化为
  "口径不存在 / 维度不支持 / 窗口不可得 / 数据为空"四类，仍然零数字，但可诊断性更好（P-10 允许）。

### 不偷

- **用一句话字符串表达失败**：我们要错误码（`NOT_VISIBLE` 等），字符串无法被 `ev16` 之类的评估器断言。
- **"没听懂"与"没数据"共用 FAILED**：我们明确区分 `NEED_CLARIFY`（可以反问）与 `NOT_VISIBLE`（不给替代数据）。
  合并会让系统在应该反问时给出"没有数据"，用户会误以为查过了。

## 4. 精读三：查询模式复用（QueryManager）

### 事实

1. **构造函数里自注册**：`RuleSemanticQuery` 的构造器调用 `QueryManager.register(this)`，
   把当前实例塞进静态 `ConcurrentHashMap`（[RuleSemanticQuery.java](file:///tmp/supersonic/headless/chat/src/main/java/com/tencent/supersonic/headless/chat/query/rule/RuleSemanticQuery.java#L32-L35)）。
2. **注册的是模板，用时反射新建**：`createRuleQuery()` 用 `getClass().getDeclaredConstructor().newInstance()`，
   反射失败返回 `null`，异常被吞（[QueryManager.java](file:///tmp/supersonic/headless/chat/src/main/java/com/tencent/supersonic/headless/chat/query/QueryManager.java#L34-L53)）。
3. **"什么算一次合法提问"写成了数据**：`QueryMatcher` 用 `QueryMatchOption`
   （`REQUIRED / OPTIONAL / UNUSED` + `AT_LEAST / AT_MOST` + 数量）描述每种元素类型的出现约束；
   任一约束不满足即整条查询不成立（[QueryMatcher.java](file:///tmp/supersonic/headless/chat/src/main/java/com/tencent/supersonic/headless/chat/query/rule/QueryMatcher.java#L49-L109)）。
4. **查询模式由"元素类型共现"决定**：`RuleSemanticQuery.resolve()` 遍历所有已注册模式，
   逐个 `match()`，命中即产出候选；同一问题可产出多条候选（[RuleSemanticQuery.java](file:///tmp/supersonic/headless/chat/src/main/java/com/tencent/supersonic/headless/chat/query/rule/RuleSemanticQuery.java#L195-L211)）。
5. **上下文只在同模式下继承**：`fillDateConfByInherited()` 要求上一轮的 `queryMode` 与本轮同为 detail 或同为 metric，
   才继承时间窗（[RuleSemanticQuery.java](file:///tmp/supersonic/headless/chat/src/main/java/com/tencent/supersonic/headless/chat/query/rule/RuleSemanticQuery.java#L74-L91)）。

### 偷

- **把"合法提问组合"写成声明式约束**：我们现在只校验"指标注册过"和"维度在枚举里"，
  没有校验"这个维度对该指标是否可用、是否必须成对出现"。可以给 `MetricDef` 加一份
  `required / optional / forbidden` 元素约束，仍由 `validate_plan_v2` 执行（不新增计算入口，符合 P-01）。

### 不偷

- **构造器副作用注册**：隐式全局状态让测试无法隔离，也绕过了"注册必须显式"的审计要求。
- **反射 `newInstance()` 失败返回 `null`**：等于把"模式不可用"降级成"NPE 或空指针"，我们要 fail-closed。

## 5. 实测（3 个语义问题）

**状态：已完成（2026-11-10）。**

### 环境

- 镜像：`supersonicbi/supersonic:latest`（digest `sha256:89f708be…`，linux/arm64，2.58GB）+ `pgvector/pgvector:pg17`。
- 启动：`docker/docker-compose.yml`，`BTA` 侧无改动；两个坑记在这里备查：
  1. Docker Hub 直连超时，须走镜像源并从 Docker Desktop 的 **No proxy** 模式拉取（内置 `http.docker.internal:3128` 只放行 Hub 域名，会把镜像源卡死，实测 10 分钟 0 字节）。
  2. compose 用 bind mount `./supersonic_pg/data`，macOS 上 uid 不匹配导致 postgres 起不来；改用命名卷（`docker-compose.override.yml`）即可。
- 演示数据：standalone 启动时由 `S2*demo` 自动写入，共 3 个数据集 / 7 个模型。
  测试用数据集 **1「超音数数据集」**（model 2：指标 `访问次数 / 访问用户数 / 人均访问次数`，维度 `数据日期 / 用户名`）。
- 登录：`admin`。库里种子密码哈希与文档密码不一致，按 `AESEncryptionUtil` 的规则（PBKDF2WithHmacSHA256, 65536 轮, salt=SHA256(user)[:16], AES/CBC/`supersonic@bicom`）重算后写回才登得进去。
- 解析方式：`POST /api/semantic/query/chat/parse`，`text2SQLType=ONLY_RULE`（未配 LLM，正好只看规则路径）。

### 结果

| # | queryText | mapMode | ParseState | queryMode | 解析出的元素 | 时间 |
|---|---|---|---|---|---|---|
| 1 | 近7天访问次数 | STRICT | `COMPLETED` | `METRIC_MODEL` | 指标：访问次数 | 2026-11-03 ~ 11-10（`detectWord=近7天`） |
| 2 | 按用户名统计访问人数 | STRICT | `COMPLETED` | `METRIC_GROUPBY` | 指标：访问用户数；维度：用户名 | 2026-11-03 ~ 11-10（**`detectWord=None`，窗口是系统补的**） |
| 3 | 昨天的退款率是多少 | STRICT | `FAILED` | — | 无 | `errorMsg="No semantic entities can be mapped against user question."` |
| 3' | 昨天的退款率是多少 | **LOOSE** | `COMPLETED` | `METRIC_GROUPBY` | 指标：**访问次数 + 访问用户数**；维度：数据日期 | 昨天 |

### 结论

- **第 3' 行是最有价值的一条证据**：模型里根本没有"退款率"，把模式从 STRICT 放到 LOOSE 之后，
  它没有报错，而是**就近解释成另外两个指标并返回成功态**。风控场景下这等于"问坏账给你答访问量"，
  正是 P-05（schema 封闭）和 P-10（算不出给错误码）要堵死的路径。§2 里"不偷嵌入/放宽阈值"由推测变成实测。
- **失败文案不可诊断**：第 3 行只说"映射不到实体"，无法区分"指标不存在 / 维度不支持 / 窗口不可得"。
  我们 D3 细分四类是有实际收益的（§3 的"偷"）。
- **缺省窗口由系统猜**：第 2 行没有时间词，系统直接补近 7 天。我们的 P-09 只允许补全 `product/window`
  且必须在回答里声明，比它严格，这条继续坚持。
- **可登录性耦合在种子数据上**：文档密码登不进，必须重算哈希。这类"演示可用、交付不可用"的坑不值得学。

## 6. 对我们项目的三条候选改动（先记着，不擅自开工）

1. `catalog` 增加"指标-维度-元素"共现约束（对应 P-05 封闭），落点 `core/catalog.py`。
2. D3 失败原因细分四类（口径/维度/窗口/空数据），落点 `v2_dialogue.py` 的降级文案。
3. 明确禁止构造器副作用注册，作为 P-01 的补充检查项，可进 `ci/gate_all.py`。

以上三条都需要先在 `docs/tasks.md` 立任务才能动代码。
