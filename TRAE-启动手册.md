# Trae 启动手册

> 平台特有的一份。换平台只换这份，`START-HERE.md` 不动。
> Trae 的规则读取机制可能随版本变化（Q-10），本手册按「`.trae/rules/*.md` + frontmatter globs」写；若你的版本不支持按需加载，见第 5 节降级方案。

## 1. 规则文件放哪（放错 = 全部失效且不报错）

```
backtest_agent_lab/
├── .trae/
│   └── rules/
│       ├── project_rules.md      ← 常驻（每次对话都加载，≤180 行）
│       ├── semantic-core.md      ← 按需：动 core/ 与 tools/ 时
│       ├── api-data.md           ← 按需：动 schema/表/MCP/llm 时
│       └── eval.md               ← 按需：动测试/评估/门禁时
├── AGENTS.md                     ← 完整宪法（Trae 不一定自动读，但你自己要能查）
└── docs/ ...
```

**三条硬要求**：

1. 常驻规则必须 ≤180 行（超了会挤占窗口，模型反而更容易忘规则）。
2. 按需规则必须有 frontmatter 的 `description` 与 `globs`；`globs` 用相对仓库根的路径。
3. 目录名是 `.trae/rules/`（不是 `.trae/rule/`，也不是根目录 `rules/`）。**放错不会报错，只是规则不生效**。

验证：

```bash
ls -la .trae/rules/
wc -l .trae/rules/*.md        # project_rules.md 必须 ≤180
```

## 2. 首日话术序列（P1—P5，按顺序发，不要合并成一条）

### P1 · 握手（最重要，唯一能确认规则生效的动作）

> 不要写任何代码。请用你自己的话回答：
> 1. 这个项目要做什么（一句话）？
> 2. 本版本（S1—S5）**明确不做什么**？列 3 条以上。
> 3. 两个「唯一计算入口」分别是哪个函数？
> 4. 你读到了哪些硬规则？只列编号和一句话。

**判断标准只有一条**：它有没有正确说出「不做什么」（明细查询 / 自由 SQL / 前端 / 训练上线）。
说对了 → 继续 P2；说成别的或泛泛而谈 → **停下修规则**，别开工（G6 门）。

### P2 · 让它复述任务边界

> 读 `docs/tasks.md`，告诉我 T-101 要改哪些文件、验收命令是什么、依赖哪个任务。不要写代码。

期望：说出 `core/model_metrics.py`、`core/stats.py`、`tools/compute_model_metric.py` 三个落点，且提到 `+2 断言`。

### P3 · 出计划（仍不写代码）

> 针对 T-101，给出 5 步以内的实施计划，每步说明改哪个函数、加哪条断言。等我确认。

检查两件事：① 它有没有想改 `catalog.py`（不该改）；② 它有没有打算「传切点参数」（P-05 禁止）。

### P4 · 开工

> 按计划执行第 1 步。改完后跑 `make gate`，把最后 5 行贴给我。不要一次改完。

**一次只让它做一步**：Trae 一次改 3 个文件时，回滚成本远高于分步。

### P5 · 收尾三问

> 1. 这次改动有没有新增可失败路径？错误码是哪个？
> 2. 有没有需要重录的基线？为什么？
> 3. `tasks.md` 哪一行需要更新？

## 3. Trae 上的五个坑（都踩过或极易踩）

| 坑 | 表现 | 规避 |
|---|---|---|
| **规则没生效** | 它开始写 `if product == "P_CASH_001"` 这类特判 | 每次新会话先跑 P1 握手；不对劲就重发规则 |
| **一次改太多** | 一次 diff 600 行，测试红了一片分不清哪错 | 明确说「只做第 1 步」，改完看 diff |
| **自己发明表结构** | 新建 `metrics_v2` 表 | 规则里写明「只加表，先改 data-model.md」；让它先读 `docs/data-model.md` |
| **绕过入口图省事** | 在 `tools/` 里 `from core.stats import auc` | `make gate` 会红；红了你**不要**让它「把门禁改宽」 |
| **解释器搞错** | 用系统 python 跑，报 `No module named langgraph` | 所有命令走 `make`，Makefile 里固定了解释器 |

## 4. 每个任务的标准对话模板（可直接复制）

```
做 docs/tasks.md 的 T-xxx。
约束：只改「落点」列出的文件；新增 ≥1 正向 + 1 反向断言；不许改 ci/ 里的门禁规则。
先给我 5 步计划，不要写代码。
```

改完后：

```
跑 make gate，贴最后 10 行。如果有红，先定位是哪个组（A/B/C/D/E），
告诉我原因，不要直接改断言。
```

## 5. 如果你的 Trae 不支持按需规则（Q-10 的降级方案）

1. 把 `semantic-core.md` / `api-data.md` / `eval.md` 的**核心约束**各压成 5 行，并入 `project_rules.md`（总行数仍须 ≤180）。
2. 完整版保留在 `.trae/rules/`，对话里手动 @ 引用：`@.trae/rules/semantic-core.md`。
3. 每次开工前贴一句：「本次任务涉及语义层，请先读 .trae/rules/semantic-core.md」。

## 6. 一分钟验证（换机器/换版本后跑）

```bash
cd backtest_agent_lab
make gate                     # 6/6
ls .trae/rules/               # 4 个文件
wc -l .trae/rules/project_rules.md   # ≤180
```

三项都对，才算环境就绪。
