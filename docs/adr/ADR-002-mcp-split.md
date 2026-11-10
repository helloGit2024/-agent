# ADR-002 · 5 个只读工具独立成 MCP server，计算工具留本地

- 状态：已接受
- 日期：2026-11-09
- 相关：FR-201—206、C-02、§9

## 背景

11 个工具当前都是进程内函数。方案 §8.6 要求「前 5 个走 MCP、后 6 个留本地」。
需要决定：是否真的拆进程？拆哪些？拆了之后失败怎么办？

## 决策

**拆 5 个只读元数据工具**（list_metrics / get_metric_definition / query_policy_flow / lookup_field / version_history）成独立进程 `mcp_server/policy_catalog.py`：

- 只用**只读连接**；`statement_timeout=3s`；单次 ≤200 行（超出截断 + `truncated`）。
- 每次调用写 `tool_call_log`（`args_hash`，不存原文）。
- 失败三分类 `NOT_FOUND / TIMEOUT / DENIED`。
- 本地留一份**同签名降级实现**：client 连不上时改调进程内函数，并在回答里标 `source=local_fallback`。

**计算工具（compute_metric / compute_model_metric / get_model_health / attribute_change / rank_policies / ask_user）永不拆**：
它们要么需要 `base_dist`/分数列（出域即违规），要么是大数据量重计算（拆出去等于把数据搬到另一个进程）。

## 后果

- 正面：审批系统/报表平台可复用元数据查询；权限收敛在一个可审计进程；目录实现可替换（Excel→配置中心）。
- 负面：多一个进程要管；stdio 模式下调试略麻烦。
- 必须守的线：`mcp_server/` **不许 import `core/model_metrics`**（否则 `score_col`/`base_dist` 有出域路径）——加进 `ci/check_imports.py` 作为门禁 3。
- 验收：`BTA_MCP=off` 时 `make gate` 仍全绿（降级路径可用）。
