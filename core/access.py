# -*- coding: utf-8 -*-
"""权限：挂在指标上，不挂在表上（V2.5 §7.6）。

════════════════════════════════════════════════════════════════════
为什么不是"表级 RBAC + SQL 改写式行级安全"
════════════════════════════════════════════════════════════════════
表级 GRANT 拦的是"人写 SQL 去查这张表"。而本方案的入参里**根本没有表名**
—— LLM 只能给 metric_id + 枚举参数。所以：
  · 表权限对它既拦不住、也不需要（没有可触发的入口）；
  · 行级安全常见的实现（解析 SQL 改写 WHERE）在这里同样无处施力，
    因为取数是参数化的 Python，没有一段 SQL 文本可以改写。

结论：权限必须**编译进"一次指标调用"**，位置和 Cube 的 access_policy 一样
—— 调用时判定（compile-time），不是取数后过滤。
取数后再过滤，等于先把全量读进内存再挑：越权已经发生，日志里也留不下证据。

════════════════════════════════════════════════════════════════════
三维，而不是一个布尔值
════════════════════════════════════════════════════════════════════
  metrics    —— 能不能调这个口径      （能力）
  dimensions —— 能不能把总量切开看    （粒度）
  row_filter —— 能看到哪些行          （范围）

本场景真实的越权风险不是"看不到坏账率"，而是"看到了不该看的那一层"：
渠道维度能反推合作方，产品维度能反推资方。所以"能看总量"与"能按渠道拆"
必须是两个独立的授权决定。

════════════════════════════════════════════════════════════════════
现阶段的落地策略：字段先留、行为不变
════════════════════════════════════════════════════════════════════
默认只有一个角色 risk_analyst（全部 6 个指标、各指标已声明维度、空 filter），
所以行为与旧版逐位一致。但字段位、判定位、审计位从第一天就在 ——
接真实身份系统那天只填值，不改架构。**这就是"现在补"和"以后补"的全部区别。**
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .codes import err


@dataclass(frozen=True)
class AccessPolicy:
    """一个角色能做什么。挂在 MetricDef 上，不是一份全局角色配置。

    metrics=None 表示"全部在册指标"（system 角色）。其余一律显式列举 ——
    新增指标默认**不可见**，最小权限才是默认态。
    """
    role: str
    metrics: tuple[str, ...] | None = None
    # metric_id -> 允许下钻的维度；缺省表示"该指标声明的 dimensions 全允许"
    dimensions: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # 声明式谓词：{"product": ("P_CASH_001",)}。空 = 不过滤（全量）。
    row_filter: dict[str, tuple[str, ...]] = field(default_factory=dict)
    note: str = ""

    # ---- 三个判定：分别对应三个错误码 ----
    def check_metric(self, metric_id: str) -> dict:
        if self.metrics is None or metric_id in self.metrics:
            return {"ok": True}
        # 措辞红线：不能说"你没权限"（那等于告诉对方"系统里有，只是不给你"）
        return err("NOT_VISIBLE", metric_id=metric_id, role=self.role)

    def check_dimension(self, metric_id: str, dimension: str | None) -> dict:
        if not dimension or dimension == "overall":
            return {"ok": True}          # "整体"永远是允许的
        allowed = self.dimensions.get(metric_id)
        if allowed is None:
            return {"ok": True}          # 未单独声明 → 跟随 MetricDef.dimensions
        if dimension in allowed:
            return {"ok": True}
        # 绝不退化成"按全量算完再回答"—— 那是本方案里最恶劣的一种失败
        return err("DENIED_DIMENSION", metric_id=metric_id,
                   dimension=dimension, allowed=list(allowed))

    def row_filter_for(self, params: dict) -> dict:
        """把 policy 的 row_filter 与用户参数求交，返回**实际生效**的过滤条件。

        返回 {} 表示不过滤；返回 {"product": [...]} 表示只允许这些行。
        取数前的最后一次机会：build_view 拿着它去 where，而不是算完再筛。
        """
        if not self.row_filter:
            return {}
        out: dict[str, list[str]] = {}
        for key, allowed in self.row_filter.items():
            asked = params.get(key)
            if asked is None or asked == "*":
                out[key] = list(allowed)
            elif isinstance(asked, str) and asked in allowed:
                continue                 # 用户只要一个且有权 → 无需额外过滤
            elif isinstance(asked, str):
                out[key] = ["__EMPTY__"]  # 越界：不报错，但也取不到任何行
            else:
                out[key] = list(allowed)
        return out


# ---------------------------------------------------------------------------
# 角色表：默认角色刻意与旧行为逐位一致
# ---------------------------------------------------------------------------
ALL_METRICS = ("approval_rate", "stage_rates", "policy_rates",
               "fpd7_rate", "fpd30_rate", "mob3_bad_rate")
ALL_MODEL_METRICS = ("ks", "auc", "gini", "iv", "psi", "lift", "woe_table")

ROLES: dict[str, AccessPolicy] = {
    # 默认角色：6 个可聚合 + 7 个模型级，维度跟随各自声明，不过滤
    "risk_analyst": AccessPolicy(
        role="risk_analyst", metrics=None,
        note="单人演示阶段的唯一角色；行为与 V2.2 逐位一致"),
    # 受限角色（自检与评估器用）：看得到总量，看不到渠道；只看现金贷
    "analyst_no_channel": AccessPolicy(
        role="analyst_no_channel",
        metrics=ALL_METRICS + ALL_MODEL_METRICS,
        dimensions={"approval_rate": ("product",),
                    "policy_rates": ("product",),
                    "stage_rates": ("product",)},
        row_filter={"product": ("P_CASH_001",)},
        note="渠道可反推合作方 → 该角色不允许按渠道拆"),
    # 定时任务：参数由注册表冻结（不是人给的），所以不做维度/行过滤，
    # 但仍走同一个 guard —— "system 也绕不过口径守卫"这件事本身很重要。
    "system": AccessPolicy(
        role="system", metrics=None,
        note="监控 job 专用：参数冻结，不接用户输入"),
    # 只给聚合指标、不给模型级（模型分是模型内部信息）
    "business_viewer": AccessPolicy(
        role="business_viewer",
        metrics=("approval_rate", "stage_rates", "policy_rates", "fpd7_rate"),
        dimensions={"policy_rates": ("product",)},
        note="业务只看漏斗，不看分数分布"),
}

DEFAULT_ROLE = "risk_analyst"


def policy(role: str | None) -> AccessPolicy:
    """角色名 → policy。未注册角色一律降级为最小权限（不是最大）。"""
    return ROLES.get(role or DEFAULT_ROLE) or AccessPolicy(role=role or "unknown",
                                                           metrics=())
