---
description: 动 schema / 数据表 / MCP / LLM 契约时加载
alwaysApply: false
globs:
  - "tools/**"
  - "mcp_server/**"
  - "llm/**"
  - "core/semantic_contract.py"
  - "core/db.py"
  - "core/db_pg.py"
  - "docs/api-contract.md"
  - "docs/data-model.md"
---

# 接口与数据改动规则

## schema 三条铁律

1. `additionalProperties: false` **每一层都要写**（顶层 + `params` 内层）。只写顶层等于没写。
2. 参数只收**枚举或受限 pattern**。`window` 用 pattern，`bins` 用 enum，`metric_id` 用 enum 且**按角色裁剪**。
3. 新增字段先问：**模型会不会拿它做别的事？**
   - 加 `bins` → 模型会试 `bins=7`（好，被 enum 拦）
   - 加 `edges` → 模型会把单调性调成任意形状（**不许加**）
   - 加 `expression` → 等于把库交出去（**永久禁止**）

## 加工具的检查清单

- [ ] `tools/registry.META` 加一行：`(通道, 分支, 是否启用)`
- [ ] `semantic_contract.tools_for(branch, role)` 能正确下发/隐藏
- [ ] `compute_*` 类工具：`validate_args` 白名单 + guard 第一步权限
- [ ] 返回结构按 `kind` 分支；大表走外卸（行数>8 或 token>800）
- [ ] 失败返回 9 个错误码之一 + `detail`，不抛裸异常
- [ ] 新增 1 条正向 + 1 条反向断言（越权/超域）

## MCP 侧额外四条

1. **不许 import `core/model_metrics`**（`score_col`/`base_dist` 会出域）。
2. 只读连接；`statement_timeout=3s`；单次 ≤200 行，超了截断 + `truncated=true`。
3. 审计只存 `args_hash` + `args_redacted`（枚举值），**不存原文**。
4. 失败三分类 `NOT_FOUND / TIMEOUT / DENIED`——文案不许混用（把超时说成"不存在"会让 LLM 编出"这个产品没有"）。

## 表结构改动

- 只加表/加可空列；**不改已有列名与类型**。
- 时间统一 `TEXT ISO8601`（SQLite）→ PG `timestamptz`；读侧统一解析函数。
- 软删除字段 `deleted_at`；状态字段用 `status` 枚举，不用布尔。
- 改表必须同步 `docs/data-model.md`，并在 `tasks.md` 记一行。

## LLM 契约

- `plan` 的 `tools[].name` 必须在 `tools_for()` 子集内，否则整条计划作废（重试 1 次 → D2）。
- `branch=model` 出现 `compute_metric` → **直接拒，不降级不改写**（改写等于替用户换问题）。
- `narrate` 输入签名里**不许出现 rows/list[dict]**，只接受 `book` 与 `refs`。
- `judge` 只判三件事，输出 `{pass, violations, reasoning}`，不许自由发挥。

## 提交前

```bash
make gate
grep -rn "additionalProperties" tools/ | wc -l   # 每个 params 都要有
grep -rn "import openai\|import dashscope" .     # 必须为空
```
