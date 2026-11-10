---
description: 动语义层（册子/闸门/权限/实体/信封/指标计算）时加载
alwaysApply: false
globs:
  - "core/catalog.py"
  - "core/model_metrics.py"
  - "core/stats.py"
  - "core/access.py"
  - "core/entities.py"
  - "core/envelope.py"
  - "core/intent.py"
  - "core/semantic_contract.py"
  - "core/codes.py"
  - "tools/**"
  - "scheduler/**"
---

# 语义层改动规则

## 改之前先定位「这是哪一层」

| 你想做的事 | 该改哪 | **不该**改哪 |
|---|---|---|
| 加一个率指标 | `catalog.MetricDef` + `_reg` + `compute` 函数 + 断言 | 渲染层、LLM prompt |
| 加一个模型级指标 | `MODEL_CATALOG` + `_compute_one` 分支 + schema enum + 断言 | 第一本册子 |
| 改口径（label/切点/前移天数） | 册子字段 + `definition_hash` 重算 | 在回答里加一句「已修正」了事 |
| 加维度 | `GRAIN_DIMENSIONS` + `ROLES[*].dimensions` + enum + 越权断言 | 只在 `build_view` 里加个 group by |
| 改权限 | `access.ROLES` + 越权断言 | 在 `build_view` 取数后过滤 |
| 改信封话术 | `envelope.render` + `regress` 基线 | 在 `_r_*` 里手拼一行 |

## 五条最容易被"顺手"破坏的不变式

1. **`stats.py` 只被 `model_metrics.py` import**。你想在 `tools/` 里直接调 `auc()`？门禁会拦，而且拦得对——那 20 行准备逻辑就是漂移源。
2. **切点只能由函数生成**。`quantile_edges(values, k)` 的 `values` 来自数据，`k` 来自枚举；**任何 allow 外部传 edges 的参数都是漏洞**（能把单调性调成任意形状）。
3. **fpd/mob 必须前移**。新指标若 `requires_maturity=True`，`check_maturity` 必须在取数**之前**；写完跑一次「3 天窗口」用例，应返回 `NOT_APPLICABLE`。
4. **模型级不许出现率值**。回答里出现 `%` 或"分子/分母"即视为分流失败（`ev17` 反向判据）。
5. **权限 fail-closed**。新角色不注册 = 空权限。新增 `role="xxx"` 时，先加 `ROLES` 再加调用点（`system` 漏注册导致 3 个 job 全拒，是已发生的事故）。

## 改完之后必须做的三件事

```bash
make sem      # ev13—18 + 反向
make test     # 84 条
make gate     # 全量
```

+ 加一条**反向断言**：把新逻辑改坏，测试必须红。
+ 若动了 `MODEL_CATALOG`/`CATALOG` 的字段值 → `definition_hash` 变化是**预期行为**，需在 commit 说明「口径版本 bump」。

## 常见错误（都在本项目真实发生过）

| 错误 | 症状 | 修法 |
|---|---|---|
| `str.isalpha()` 判英文边界 | 中文紧邻的 `KS` 全部漏判 | 只用 `before.isascii() and before.isalpha()` |
| 角色未注册 | 功能"没报错"但一直 `NOT_VISIBLE` | 查 `ROLES`；`prepare()` 看 `物化 k/n` |
| PSI 先判 iv 再判 psi | PSI 返回 `value=None` | `_compute_one` 里 `if metric_id=="psi"` 提前 return |
| `overall` 与 `groups[0]` 不一致 | 维度=overall 时值为空 | 统一从 `groups[0]` 取字段 |
