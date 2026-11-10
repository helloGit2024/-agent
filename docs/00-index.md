# 00 · 文档导航与状态

> 这套文档覆盖 **S1—S5**（把方案 §8—§13 六个模块全部实现）。S0/S0.5 已交付，不在本套范围内，只作为基线。
> 判档：L3（6/8：2—5 人 1 + 内部 1 + 金融数据 2 + 长期 2），协作档 C0（单人 + Trae），按「聚焦落地版」裁剪为 22 份。

## 读的顺序（别从第 1 页顺序读）

```
START-HERE.md（10 分钟上手）
   └─ 00-index（本页）
        ├─ constraints（不能碰的线）          ← 先读这个，比 spec 重要
        ├─ spec（要做到什么）                 ← FR-101 ~ NFR-05
        ├─ architecture（怎么做）             ← 图 + 目录 + 降级
        │    ├─ data-model（新表）
        │    ├─ api-contract（工具与 LLM 契约）
        │    └─ adr/*（5 个为什么）
        ├─ tasks（按什么顺序做）              ← 直接对着开工
        ├─ quality-gates（怎么算做完）        ← 每完成一个任务看一次
        ├─ evals/*（怎么证明它对了）
        └─ risk-register（哪些地方会炸）
```

## 文件清单

| 文件 | 作用 | 状态 | 谁维护 |
|---|---|---|---|
| `AGENTS.md` | 项目宪法（P-01—P-12） | ✅ | 你 |
| `START-HERE.md` | 10 分钟上手 + 每日节奏 | ✅ | 你 |
| `TRAE-启动手册.md` | Trae 专属：规则落位、首日话术、坑表 | ✅ | 你 |
| `OPEN-QUESTIONS.md` | 待确认项（阻塞/非阻塞） | ✅ | 你 |
| `REPO_CONVENTION.md` | 单人提交规范、分支、目录 | ✅ | 你 |
| `Makefile` | 唯一命令入口 | ✅ | 你 |
| `docs/constraints.md` | 硬约束（合规/技术/环境） | ✅ | 你 |
| `docs/spec.md` | 功能规格 7 组 FR + 5 条 NFR | ✅ | 你 |
| `docs/architecture.md` | 目标架构、数据流、降级、目录 | ✅ | 你 |
| `docs/data-model.md` | 新表 DDL 与字段说明 | ✅ | 你 |
| `docs/api-contract.md` | 工具 schema、LLM 三种 JSON 契约 | ✅ | 你 |
| `docs/tasks.md` | S1—S5 任务（含文件落点/验收） | ✅ | 你 |
| `docs/research-semantic-layer.md` | S1.5 对标调研计划（SuperSonic/WrenAI/Cube/闭源两家） | ✅ | 你 |
| `docs/quality-gates.md` | 六道门禁 + CI 扩展 + 完成定义 | ✅ | 你 |
| `docs/risk-register.md` | 风险与对抗评审发现 | ✅ | 你 |
| `docs/assumptions.md` | 假设台账（12 条待验证 + 4 条已证伪） | ✅ | 你 |
| `docs/security.md` | 数据边界、脱敏、注入面、审计 | ✅ | 你 |
| `docs/adr/ADR-001..005` | 5 个关键决策 | ✅ | 你 |
| `evals/README.md` | 评估体系总览 + Judge 契约 | ✅ | 你 |
| `evals/golden_set.yaml` | 140 用例生成规则 + 20 条种子 | ✅ | 你 |
| `evals/trace-replay.md` | 回放机制与 fixture 格式 | ✅ | 你 |
| `.trae/rules/project_rules.md` | 常驻规则（≤180 行） | ✅ | 你 |
| `.trae/rules/semantic-core.md` | 按需：动语义层时加载 | ✅ | 你 |
| `.trae/rules/api-data.md` | 按需：动 schema/表/接口时加载 | ✅ | 你 |
| `.trae/rules/eval.md` | 按需：动测试评估时加载 | ✅ | 你 |

## 阶段状态

| 阶段 | 目标 | 出口标准 | 状态 |
|---|---|---|---|
| S0.5 | 语义层 + 两类分流 + 物化 | `make gate` 5/5；77 断言 | ✅ 已交付 |
| S0.9 | 模块一收口（FR-101—107） | 6 条新断言；3 句模糊问句被反问 | ✅ 已交付（gate 6/6，84 断言） |
| S1 | LLM 规划 + D0—D4 + 预算 | D1/D2 可观测；LLM 版与规则版对照可跑 | ⬜ |
| S1.5 | 语义层对标调研（SuperSonic/WrenAI/Cube/闭源） | 四份笔记 + 吸收清单归属 + ADR-006 草稿；与 S1 并行，T-306/T-307 前收口 | ⬜ |
| S2 | MCP server | 关 server 后仍全绿（降级） | ⬜ |
| S3 | 记忆 + 上下文工程 | 20 条记忆契约测试；token 预算 8 条单测 | ⬜ |
| S4 | LLM 叙述 + 出口回退 | 假数字被丢弃并回退模板 | ⬜ |
| S5 | 140 用例 + replay + Judge | `make gate` 含 C 组；replay 0 差异 | ⬜ |

## 三条使用纪律

1. **一次只做一个任务**（`tasks.md` 的 T-xxx），做完跑 `make gate`，绿了再下一个。
2. **任何「让测试变绿」的改动都要先问：是代码错了，还是设计变了？** 设计变了走 `P-11` 重录基线。
3. **卡住超过 30 分钟**：把现象写进 `OPEN-QUESTIONS.md`，标阻塞，先做不依赖它的任务。
