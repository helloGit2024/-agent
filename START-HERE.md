# START-HERE · 10 分钟上手

## 0. 这是什么

策略回测分析 Agent V2 的**开发文档集**，覆盖 S1—S5（方案 §8—§13 六个模块）。
S0/S0.5 的代码已经跑通，你现在要做的不是从零写，而是**在已有内核外面加三个可替换的壳**：规划壳、协议壳、叙述壳。

## 1. 三分钟确认环境

```bash
cd backtest_agent_lab
make gate      # 必须 6/6 全绿；不绿先别动代码，先修
make demo      # 看 10 题回答长什么样（重点看每题的【口径】五行）
```

记下两件事：`make gate` 最后一行是不是「6/6 全绿」；`make demo` 里问 KS 那题有没有分箱表。这两个是你后续所有改动的基线。

## 2. 十分钟读懂结构（只看这四个文件）

| 顺序 | 文件 | 看什么 |
|---|---|---|
| 1 | `docs/constraints.md` | 哪些线不能碰（比功能更重要） |
| 2 | `docs/spec.md` 第 1 节 | S1—S5 的终点长什么样 |
| 3 | `docs/architecture.md` 第 1、5 节 | 全图 + 降级矩阵 |
| 4 | `docs/tasks.md` | 你明天要做的第一个任务（T-101） |

## 3. 五个批次与验收方式

| 批次 | 内容 | 怎么验收 |
|---|---|---|
| 批 1 | 宪法 / 规格 / 约束 / 假设 / 待确认 | **读**：逐条确认「做什么/不做什么」，过一遍假设台账 |
| 批 2 | 架构 / 数据模型 / 接口契约 / ADR×5 / 安全 | **读**：只确认技术栈、表名、字段名 |
| 批 3 | `.trae/rules/*` + `start-here` + 启动手册 + Makefile | **跑**：握手（见 `TRAE-启动手册.md` P1） |
| 批 4 | 任务 / 门禁 / 风险 / 评估 | **读 + 对抗评审**：四视角见 `risk-register.md` |
| 批 5 | 上线与迭代 | 本阶段不需要（S5 后再补） |

## 4. 每天怎么开工 / 收工

**开工（3 分钟）**

```bash
make gate                    # 起点必须绿
# 打开 docs/tasks.md，只挑一个 T-xxx，确认它的「落点」文件
```

**干活**：只改落点内的文件。想改清单外的，先在 tasks.md 补一行——这不是形式，是防止改动无记录。

**收工（5 分钟）**

```bash
make gate                    # 必须绿
git diff --stat              # 单次 ≤400 行；超了说明任务该拆
```

回填 `tasks.md` 状态；若改了表/接口，同步 `data-model.md` / `api-contract.md`。

## 5. 卡住了怎么办

| 情况 | 动作 |
|---|---|
| 测试红了，但你觉得代码是对的 | 先问「是代码错还是设计变」；设计变走 ADR + P-11 重录，**不许改断言** |
| 不知道某个函数为什么这么写 | 去 `selftest_v2.py` 搜对应断言，断言名往往比注释直白 |
| 改了 3 小时还没绿 | 停下，把现象写进 `OPEN-QUESTIONS.md` 标阻塞，换一个不依赖它的任务 |
| 想加一个「临时」参数把功能跑通 | 停——schema 一开就收不回（`constraints.md` T-03） |

## 6. 三个必须记住的坐标

1. **两个唯一入口**：`compute_model_metric()` / `catalog + compute_metric`。找不到该改哪时，先问「这属于哪个入口」。
2. **两本册子**：率指标在 `catalog.CATALOG`，模型级在 `model_metrics.MODEL_CATALOG`，物理隔离。
3. **一条命令**：`make gate`。它是你唯一需要的"我有没有搞坏东西"的答案。

## 7. 先读哪份、后读哪份（别顺序读）

```
constraints → spec(§1) → architecture(§1/§5) → tasks(第一个任务)
   ↓ 开工后按需
api-contract / data-model / semantic-core.md / eval.md
   ↓ 卡住时
risk-register / assumptions / OPEN-QUESTIONS
```

`00-index.md` 有完整清单与状态。
