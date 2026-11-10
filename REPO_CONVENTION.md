# 仓库约定（单人 + AI 协作，C0）

> 一个人开发，不需要分支策略与评审流程；但**提交粒度**与**基线纪律**必须守住，否则「AI 改 AI 写的代码」会失控。

## 1. 提交规范

```
T-101: 新增 ks_curve/roc_curve，只回摘要 + ref_id
```

- 前缀必须是 `docs/tasks.md` 里的任务号；无任务号的提交说明改动没有落点。
- 一次提交 = 一个任务的一步；**单次 diff ≤400 行**（超了拆）。
- 涉及基线重录，必须写：`重录 q2-v1-s1 等 3 条，原因=<设计变更>`（P-11）。

## 2. 分支

- 主干 `main` 可直推（单人）。
- 只有两种情况开分支：① 改动 >400 行且要跨天；② 试验性方案（可能整体丢弃）。
- 分支名：`t101-curve`（任务号 + 关键词），合并后删除。

## 3. 提交前 60 秒

```bash
make gate
git status --short          # 只应有落点内的文件
git diff --stat             # ≤400 行
```

**不通过就不提交**——「先提交回头修」在单人项目里等于永远不修。

## 4. 目录与命名

| 类型 | 约定 | 例 |
|---|---|---|
| 模块 | 小写下划线，按层分目录 | `core/`, `tools/`, `mcp_server/` |
| 类 | PascalCase | `ModelMetricDef` |
| 函数 | 小写下划线，动词开头 | `compute_model_metric` |
| 常量 | UPPER_SNAKE | `BINS_ALLOWED` |
| 私有 | 前缀 `_` | `_assemble` |
| 表 | 小写下划线，复数 | `memory_items` |
| 测试断言名 | 中文，说「为什么」 | `"模型级回答不出现率值（分流失败会复现问KS答通过率）"` |

## 5. 文件放置（别新建"临时"目录）

```
core/        语义层与计算（不许 import 上层）
tools/       工具壳
scheduler/   定时任务与物化
llm/         LLM 出口（call/plan/narrate/context/budget/providers）
memory/      记忆（store/adapters/writer/policy）
mcp_server/  MCP 服务（S2）
ci/          门禁脚本
eval/        评估器与回归
evals/       用例集与回放说明
docs/        开发文档
```

新增顶层目录必须写 ADR——目录是架构的一部分。

## 6. 依赖管理

- 当前零第三方依赖（除环境自带的 `langgraph`）；新增包必须同时：改 `constraints.md` T-07 白名单 + 写 ADR + 在 `README` 记一行。
- 不锁 `requirements.txt` 之外的东西；不装全局包（用 `envs/default`）。

## 7. 不许提交的文件

```
artifacts/*.db            # 夹具库（可 make init 重建）
artifacts/ref/*.json      # 外卸文件
.env / *.key              # 密钥
__pycache__/
```

`.gitignore` 必须包含以上四类。**审计表与 fixture 例外**：`fixtures/traces/*.json` 要提交（回归靠它）。
