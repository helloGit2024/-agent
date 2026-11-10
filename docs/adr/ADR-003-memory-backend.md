# ADR-003 · 记忆两档后端：LangMem 默认 / Zep 合规，接口同一

- 状态：已接受
- 日期：2026-11-09
- 相关：FR-301—307、P-09、§10.2

## 背景

记忆需要「跨会话偏好」与「历史提问摘要」。候选：LangMem（LangGraph 原生）、Zep（时间图谱）、自建表。
合规侧关心「三个月前为什么这么判」，这要求**失效不删除**；开发期关心零运维。

## 决策

定义 `MemoryStore` 接口（`put / get / of / forget / history`），两个适配器：

| 档 | 实现 | 用途 | 关键差异 |
|---|---|---|---|
| 默认 | `memory/adapters/langmem.py` | 开发/内网 | 无时间维度，删除即消失 |
| 合规 | `memory/adapters/zep.py` | 生产/审计 | 每条边带有效期，失效不删 |

**但两条硬规则不交给后端实现**，一律在 `memory/policy.py` 里前置执行：
1. `FILLABLE = (product, window)`，`FORBIDDEN` 口径字段直接拒（返回 `NOT_APPLICABLE`）。
2. 写入只能来自 `writer.py`（系统侧规则），**不提供任何写记忆工具给 LLM**。

## 为什么不用「让后端自己管 schema」

LangMem 与 Zep 的 schema 自由度都很大，一旦让它们自由存，`metric_id` / `label` 会被「贴心」记住（§10 反例：这次问 FPD7，记忆把 label 填成 fpd30，数算出来了、信封齐全、完全无法察觉）。
所以 **schema 校验与字段白名单必须在我们这边**，后端只当存储。

## 后果

- 正面：切换只改配置；两档跑同一套 20 条契约测试；合规档可回答「当时为什么这么判」。
- 负面：Zep 需要自托管运维；抽象层要做「最小公共子集」，`procedural` 类记忆在 LangMem 上表达力弱 → 记 `unsupported` 而非造假。
- 回滚：`BTA_MEMORY=off` 回到无记忆，`apply()` 直通，回答不变（只是少了「记忆补全」声明）。
