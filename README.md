# 策略回测分析 Agent V2

把自然语言问题变成**带口径信封、可溯源**的策略/模型指标回答。本质是受约束的指标查询与计算网关。

- 当前代码基线：**S0 + S0.5**（语义层骨架 + 两类指标分流 + 定时物化）
- 当前文档范围：**S1—S5**（方案 §8—§13 六个模块）
- 开发工具：Trae（规则在 `.trae/rules/`）

## 30 秒上手

```bash
make gate    # 6 道门禁，应输出 6/6 全绿
make demo    # 10 题演示（重点看每题的【口径】【窗口】【过滤】【截止】）
```

解释器已固定在 `Makefile`（系统 python 无 langgraph）。

## 先读哪三份

1. `START-HERE.md` — 10 分钟上手与每日节奏
2. `docs/constraints.md` — 不能碰的线
3. `docs/tasks.md` — 明天要做的第一个任务

## 结构

```
core/        语义层与计算（两本册子、唯一入口、权限、实体、信封）
tools/       工具壳（11 个，5 MCP + 6 本地）
scheduler/   定时任务与物化（参数冻结的同一函数）
llm/         LLM 出口（S1 起实现，当前是桩）
memory/      记忆（S3 起实现，当前是桩）
mcp_server/  MCP 服务（S2 新建）
ci/ eval/    门禁与评估
docs/        开发文档（22 份，见 docs/00-index.md）
```

## 三条不能破的规则

1. **两个唯一入口**：`compute_model_metric()` 与 `catalog + compute_metric`，不许第三条路径。
2. **两本册子物理隔离**：率指标与模型级指标不并表、不互借字段。
3. **一条命令验全仓**：提交前 `make gate` 必须全绿。

完整规则见 `AGENTS.md`（P-01—P-12）。
