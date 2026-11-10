---
description: 项目常驻规则（每次对话自动加载）：范围边界、P-01—P-12 硬规则、命令、四个「停」
alwaysApply: true
---

# Trae 常驻规则（每次对话自动加载）

> 完整版见 `AGENTS.md`。本文件是它的**执行摘要**（≤180 行），两份必须同步；冲突以 `AGENTS.md` 为准。

## 这个项目是什么

策略回测分析 Agent V2：把自然语言问题变成**带口径信封、可溯源**的策略/模型指标回答。
本质是**受约束的指标查询与计算网关**，不是聊天机器人。

**本版本（S1—S5）不做什么**（能脱口而出才算规则生效）：

- 不做明细查询（「哪个客户」→ `NOT_VISIBLE`，不给替代数据）
- 不做自由 SQL / 表达式（schema 里没有 sql/expression 字段）
- 不做模型训练、不做策略上线、不做审批决策
- 不做前端界面、不做多租户
- 不替代报表平台，只保证同参同数

## 硬规则（违反即返工）

- **P-01** 两个唯一入口：`compute_model_metric()` / `catalog`+`compute_metric`。`core/stats.py` 只许 `model_metrics.py` 导入。禁止 `report_calc.py` 这类新计算路径。
- **P-02** 定时任务 = 冻结参数调同一函数。`run_job` 必须是一行 `compute_model_metric(...)`。
- **P-03** 权限在调用时判定，未知角色 fail-closed。越权回答**零数字**。新维度同步三处：`GRAIN_DIMENSIONS` / `ROLES[*].dimensions` / schema enum。
- **P-04** 两本册子物理隔离，`tools_for(branch, role)` 必须互斥注册。
- **P-05** schema `additionalProperties: False`；禁 `sql/expression/formula/table/column/where`；`bins∈{5,10,20}`、`label∈{fpd7,fpd30,mob3}`；**切点不接受外部传入**。
- **P-06** 数字先 `AnswerBook.put()` 再 `fmt()`；LLM 叙述必须过 `verify_numbers`，未登记 → 丢弃叙述回退模板。
- **P-07** 回答必带信封：可聚合 4 行，模型级加【分箱】；允许「不适用」，禁止留空。
- **P-08** LLM 只走 `llm/call.py`；禁止业务代码 `import openai`；`temperature=0`、模型版本写死、记录 `prompt_version/schema_hash`。
- **P-09** LLM 不许写记忆、不许碰口径；记忆只填 `product/window`。
- **P-10** 四级降级 D0—D4；算不出给错误码，**不给替代数字**。
- **P-11** 改渲染/意图/信封 → 先跑 `make regress`；基线只能按设计变更重录并写明原因。
- **P-12** 提交前 `make gate` 必须全绿；新门禁必须进 `ci/gate_all.GATES`。

## 允许改的边界

- 只改 `docs/tasks.md` 当前任务「落点」列出的文件；要改清单外文件，先在 tasks 里补一行。
- `core/*` 不许 import `llm/`、`memory/`、`tools/`、`mcp_server/`。
- `mcp_server/` 不许 import `core/model_metrics`（分数列/`base_dist` 会出域）。

## 命令（唯一入口，别自己拼）

```bash
make gate    # 5 道门禁，提交前必跑
make test    # 32 + 77 自检
make demo    # 10 题
make init    # 重建夹具库
make sem     # 评估器 ev13—18 + 反向
```

解释器：`/Users/tj/.workbuddy/binaries/python/envs/default/bin/python`（系统 python 无 langgraph）。

## 写代码时的四个「停」

1. 想让测试变绿 → 先问「是代码错还是设计变」，**不许改断言/基线**。
2. 想在渲染层写 `if product == ...` → 停，去改册子。
3. 想加一个「临时」自由参数（sql/表达式/切点）→ 停，schema 一开就收不回。
4. 想把明细查出来再脱敏 → 停，越权已发生。

## 交付前

- `make gate` 全绿
- `git diff --stat` 单次 ≤400 行（超了拆任务）
- 新增断言 ≥1 正向 + 1 反向
- 新 TODO 必须在 `docs/tasks.md` 有落点
