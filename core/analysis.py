# -*- coding: utf-8 -*-
"""分析计划 DSL + 报告渲染 —— LLM 与数字之间的两道闸门。

设计文档铁律 3（整个项目可信度的分水岭）：
    数字绝不经过 LLM。LLM 的输出边界被压缩成两样东西：
      1. 分析计划（本模块的 DSL）—— 告诉内核"算什么、按什么口径、什么顺序"；
      2. 报告叙述的连接词 —— 数字本身用槽位注入（render_report），
         且每个数字槽位都带 metric_id，渲染后做"可溯源性校验"。
    DSL 是唯一输出契约：LLM 产出非法计划 → validate_plan 直接拒绝 →
    降级为规则计划（本模块的 rule_based_plan），绝不猜着跑。

为什么 V1 里 LLM 是"可选件"：
    回测的可复现性要求同一份数据跑两次结论完全一致。规则计划器已经能
    覆盖 V1 的标准分析路径；LLM 计划器的价值在 V2 之后（自然语言工单
    → 分析计划）。所以默认走规则计划，配了 DASHSCOPE_API_KEY 才启用 LLM 叙述。
"""
from __future__ import annotations

import json
from typing import Any

# ---------------------------------------------------------------------------
# 分析计划 DSL（schema 即契约）
# ---------------------------------------------------------------------------
VALID_STEPS = {
    "consistency_check",   # 第 0 步：线上 vs 离线判决一致率
    "funnel",              # 漏斗表
    "lmdi",                # 环节归因（需要 before/after 两份漏斗）
    "policy_diag",         # 单策略诊断（KS/分箱/敏感性/方向）
    "tighten_replay",      # 收紧 what-if 重放（候选配置）
    "relax_eval",          # 放宽评估（下游拦截率 + 坏账可得性）
    "gap_report",          # 补数缺口清单
    "report",              # 报告渲染
}

VALID_LAYERS = {"overall", "product", "channel", "new_vs_old", "pre_vs_post"}


def rule_based_plan(scope: dict | None = None) -> dict[str, Any]:
    """规则计划器 —— V1 的标准分析路径（也是 LLM 计划的降级兜底）。

    教学要点：这个计划的顺序本身就是设计文档的方法论：
      第 0 步核对 → 漏斗 → 归因 → 单策略诊断 → 方向分叉（收紧重放/放宽评估）
      → 缺口清单 → 报告。任何一步失败都有条件边处理（见 v1_backtest.py 的图）。
    """
    return {
        "backtest_id": None,                # 由编排层填 run_id
        "mode": "standard",                 # standard / replay_only / relax_only
        "scope": scope or {"layers": ["overall", "product", "channel",
                                      "new_vs_old", "pre_vs_post"]},
        "candidates": {                     # 由诊断结果驱动的候选配置（先空，诊断后填）
            "tighten": None,
            "relax": None,
        },
        "steps": ["consistency_check", "funnel", "policy_diag",
                  "tighten_replay", "relax_eval", "gap_report", "report"],
        "constraints": {"max_step_failures": 2,   # 两个环各限 2 次失败 → 降级交付
                        "min_bin_sample": 30},
    }


def validate_plan(plan: dict) -> tuple[bool, list[str]]:
    """计划合法性校验 —— LLM 计划的第一道闸门。

    教学要点：宁可拒绝计划也不猜着跑。回测报告的每一步都要可复现，
    一个口径含糊的计划跑出来的报告，错起来比没有报告更可怕。
    """
    errors: list[str] = []
    if not isinstance(plan, dict):
        return False, ["计划必须是 JSON 对象"]
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("steps 必须是非空数组")
    else:
        for s in steps:
            if s not in VALID_STEPS:
                errors.append(f"非法步骤: {s}（合法值: {sorted(VALID_STEPS)}）")
        if "consistency_check" in steps and steps.index("consistency_check") != 0:
            errors.append("consistency_check 必须是第一步（第 0 步铁律）")
        if "report" in steps and steps[-1] != "report":
            errors.append("report 必须是最后一步")
    scope = plan.get("scope", {})
    for layer in scope.get("layers", []):
        if layer not in VALID_LAYERS:
            errors.append(f"非法分层维度: {layer}")
    c = plan.get("constraints", {})
    if c.get("max_step_failures", 2) > 2:
        errors.append("max_step_failures 上限为 2（两个环各限 2 次，超过就降级交付）")
    return (len(errors) == 0), errors


# ---------------------------------------------------------------------------
# 数字登记簿 + 报告渲染（槽位注入）
# ---------------------------------------------------------------------------

class MetricRegistry:
    """数字登记簿：报告里出现的每个数字都必须先登记在这里。

    教学要点：这就是"数字可溯源"的实现机制。
      渲染时报告只写 {metric:xxx} 槽位；渲染完成后 scan 里跑一遍
      正则，报告正文中出现的每个裸数字都必须能对上登记簿 ——
      对不上的数字 = 不可溯源 = 报告作废。
    """

    def __init__(self) -> None:
        self._store: dict[str, Any] = {}

    def put(self, metric_id: str, value: Any, source_level: str = "MEASURED",
            note: str = "") -> None:
        self._store[metric_id] = {"value": value, "level": source_level, "note": note}

    def get(self, metric_id: str) -> Any:
        return self._store[metric_id]["value"]

    def all(self) -> dict[str, dict]:
        return dict(self._store)


def render_report(findings: dict[str, Any], registry: MetricRegistry,
                  narrative: str | None = None) -> dict[str, Any]:
    """把 findings 渲染成三层报告（数字槽位 + 来源等级标注）。

    narrative：可选的 LLM 叙述（只写连接词与解读，不带数字）。
    V1 默认用模板叙述 —— 同样满足"数字不经 LLM"。
    """
    exec_rows = []
    # 管理层版：只放 headline 四个数 + 一句建议
    headline = findings.get("headline", {})
    exec_rows.append({"metric": "整体通过率（当前）", "metric_id": "pass_rate_current",
                      "level": headline.get("pass_rate_level", "MEASURED"),
                      "value": headline.get("pass_rate_current")})
    exec_rows.append({"metric": "整体坏账率（放款人群，实测）", "metric_id": "bad_rate_measured",
                      "level": headline.get("bad_rate_level", "MEASURED"),
                      "value": headline.get("bad_rate_measured")})
    if findings.get("tighten"):
        exec_rows.append({"metric": "收紧候选：通过率损失", "metric_id": "tighten_pass_loss",
                          "level": "MEASURED", "value": findings["tighten"]["pass_loss_pct"]})
        exec_rows.append({"metric": "收紧候选：放行坏账率", "metric_id": "tighten_bad_rate",
                          "level": "MEASURED", "value": findings["tighten"]["bad_rate_candidate"]})
    if findings.get("relax"):
        exec_rows.append({"metric": "放宽候选：下游拦截率", "metric_id": "relax_downstream_kill",
                          "level": "MEASURED", "value": findings["relax"]["downstream_kill_rate"]})
        exec_rows.append({"metric": "放宽候选：坏账标签可得", "metric_id": "relax_bad_level",
                          "level": findings["relax"]["bad_rate_level"], "value": None})
    report = {
        "exec": exec_rows,
        "policy": findings,                    # 策略版：全部 findings（10 页报告的原料）
        "compliance": {"registry": registry.all(),          # 审计版：数字登记簿原文
                       "metric_count": len(registry.all())},
        "narrative": narrative or _template_narrative(findings),
    }
    return report


def _template_narrative(findings: dict[str, Any]) -> str:
    """模板叙述 —— 连接词可以模板化到什么程度的演示。

    注意每句话里都没有具体数字，数字全部在槽位里。LLM 叙述上线后
    也必须遵守这个边界（评估器 traceability 会检查）。
    """
    parts: list[str] = []
    con = findings.get("consistency", {})
    if con.get("rate") == 1.0:
        parts.append("线上与离线判决完全一致，本次回测数据可信。")
    elif con:
        parts.append("线上与离线判决存在不一致，已暂停统计并进入补数流程。")
    if findings.get("lmdi"):
        parts.append("环节归因采用乘法精确分解，各环节贡献之和与整体变化严格相等。")
    for d in findings.get("directions", []):
        parts.append(d["reason"])
    if findings.get("tighten"):
        parts.append("收紧方向结论为实测精确值，可直接进入重放验证与业务评审。")
    if findings.get("relax"):
        parts.append("放宽方向的坏账结论受限于标签可得性，建议按实验设计卡启动小流量验证。")
    return " ".join(parts)
