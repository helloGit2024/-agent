# -*- coding: utf-8 -*-
"""LLM 版提问单（S1 T-202）：prompt → plan JSON → validate_plan。

════════════════════════════════════════════════════════════════════
三件事的分工（为什么归一化不交给模型）
════════════════════════════════════════════════════════════════════
  ① LLM：只做"人话 → 槽位短语"。它选指标 id、摘时间短语、举手 missing；
  ② 本模块 _normalize()：把短语变成**语义层对象**（window_text 走
     catalog.parse_window，默认值/缺失判定照抄规则版的口径 ——
     默认值是口径，口径不许由模型发挥，P-09）；
  ③ validate_plan：把归一化前的原始 JSON 投影成计划形状过白名单
     （跨册引用、自由字段在这里被拒，PlanRejected）。

失败形态只有两种，都交给上层走 D2（T-203 接线）：
  · llm.call.LLMUnavailable —— 网关层面失败（超时/schema 两次不合规）；
  · PlanRejected —— 内容层面失败（跨册引用/自由字段），带 bad 清单。
本模块**不**自己回退规则版：降级是编排层的决定，不是理解层的决定。
"""
from __future__ import annotations

from core import catalog, intent, knowledge, model_metrics
from llm import context
from llm.call import call

PROMPT_VERSION = "plan-v1"
# P-08：模型版本写死。换模型 = 改这里 + 影子模式对照（ADR-001 / §16.1）
PLAN_MODEL = "qwen-plus-2025-12-01"

MISSING_SLOTS = ("intent", "product", "window", "metric", "branch", "field")


class PlanRejected(Exception):
    """LLM 输出了**结构合法但内容越界**的计划（跨册引用/自由字段）。

    与 LLMUnavailable 分开：网关失败是"模型没来"，这里是"模型乱来"，
    审计与熔断要分开计数（T-205）。
    """

    def __init__(self, bad: list[str]):
        super().__init__("PLAN_REJECTED: " + "; ".join(bad))
        self.bad = list(bad)


# ---------------------------------------------------------------------------
# 输出 schema：封闭（additionalProperties=False），枚举全部来自册子实时生成
# ---------------------------------------------------------------------------
def plan_schema() -> dict:
    """schema_hash 随册子变化 —— 册子改一个字，审计指纹就变，这是特性不是 bug。"""
    nullable = lambda vals: {"enum": [*vals, None]}            # noqa: E731
    return {
        "type": "object", "additionalProperties": False,
        "required": ["intent", "product", "window_text", "metrics", "model_metric",
                     "bins", "label", "dimension", "field", "missing", "notes"],
        "properties": {
            "intent": {"enum": [intent.INTENT_STATS, intent.INTENT_ANOMALY,
                                intent.INTENT_CONTRIBUTION, intent.INTENT_CONFIG,
                                intent.INTENT_FIELD, intent.INTENT_MODEL_DIAG,
                                intent.INTENT_UNKNOWN]},
            "product": nullable(sorted(knowledge.PRODUCTS)),
            # window_text 是原文短语（"近一个月"），不是日期；解析只走语义层
            "window_text": {},
            "metrics": {"type": "array",
                        "items": {"enum": sorted(catalog.CATALOG)}},
            "model_metric": nullable(sorted(model_metrics.MODEL_CATALOG)),
            "bins": nullable(list(model_metrics.BINS_ALLOWED)),
            "label": nullable(sorted(model_metrics.LABELS)),
            "dimension": nullable(list(model_metrics.DIMENSIONS)),
            "field": nullable(sorted(knowledge.FIELD_DICT)),
            "missing": {"type": "array", "items": {"enum": list(MISSING_SLOTS)}},
            "notes": {"type": "array", "items": {"type": "string"}},
        },
    }


# ---------------------------------------------------------------------------
# 内容校验：投影成 validate_plan 认识的计划形状（tool_calls 互斥检查才会咬人）
# ---------------------------------------------------------------------------
def _plan_check(data: dict) -> dict:
    calls = [{"tool": "compute_metric", "args": {"metric_id": mid}}
             for mid in data.get("metrics") or []]
    if data.get("model_metric"):
        calls.append({"tool": "compute_model_metric",
                      "args": {"metric_id": data["model_metric"],
                               "params": {"bins": data.get("bins"),
                                          "label": data.get("label")}}})
    return {"intent": data.get("intent"), "steps": [], "tool_calls": calls}


# ---------------------------------------------------------------------------
# 归一化：LLM 槽位 → 与 intent.parse 同形的提问单。默认值照抄规则版口径。
# ---------------------------------------------------------------------------
def _normalize(data: dict, question: str, as_of: str) -> dict:
    it = data["intent"]
    prod = data.get("product")
    slots: dict = {"product": prod,
                   "product_name": (knowledge.PRODUCTS.get(prod) or {}).get("name"),
                   "window": None, "metrics": [], "dimension": None}
    missing: list[str] = []
    # ★ notes 是"口径声明"，只由系统生成。LLM 自写的 notes 不采信 ——
    #   实测它会写"PSI 必须指定基准期，当前未提供"，而系统已经默认了基准期，
    #   两句一起输出就是自相矛盾。口径声明错了比没有更糟（P-09）。
    notes: list[str] = []

    wt = (data.get("window_text") or "").strip() or None
    win = catalog.parse_window(wt, as_of) if wt else None

    if it == intent.INTENT_MODEL_DIAG:
        m = model_metrics.MODEL_CATALOG[data["model_metric"]]  # schema 已保证存在
        slots.update(metrics=[], model_metric=m.metric_id,
                     dimension=data.get("dimension") or "overall",
                     bins=data.get("bins") or model_metrics.DEFAULT_BINS,
                     label=data.get("label") or m.label_col)
        if not prod:
            missing.append("product")
        if win is None:
            win = catalog.parse_window("近三个月", as_of)
            notes.append("未指定时间范围，默认按【近三个月】统计")
        if data.get("bins") is None and m.kind != "curve":
            notes.append(f"未指定箱数，默认按【{model_metrics.DEFAULT_BINS} 箱】等频分箱")
        if m.ref_period == "required":
            slots["ref_period"] = "train_2025Q4"
            notes.append("PSI 需要基准期，默认用【train_2025Q4（2026Q1）】")
    else:
        slots["metrics"] = list(data.get("metrics") or [])
        if it in (intent.INTENT_STATS, intent.INTENT_ANOMALY,
                  intent.INTENT_CONTRIBUTION) and not slots["metrics"]:
            slots["metrics"] = ["approval_rate"]
            notes.append("未指定指标，默认按【申请通过率】口径")
        if it == intent.INTENT_STATS:
            # 口径级默认（与规则版一致）：统计必带环节表，提到策略再带策略表。
            # 没有它就说不出"哪一段最堵" —— 这是口径，不许因走了 LLM 而漂移。
            if "stage_rates" not in slots["metrics"]:
                slots["metrics"].append("stage_rates")
            if "策略" in question and "policy_rates" not in slots["metrics"]:
                slots["metrics"].append("policy_rates")
        if it in (intent.INTENT_STATS, intent.INTENT_ANOMALY, intent.INTENT_CONFIG,
                  intent.INTENT_CONTRIBUTION) and not prod:
            missing.append("product")
        if it in (intent.INTENT_STATS, intent.INTENT_ANOMALY) and win is None:
            missing.append("window")
        if it == intent.INTENT_CONTRIBUTION and win is None:
            win = catalog.parse_window("近半年", as_of)     # 与规则版默认一致
            notes.append("未指定时间范围，默认按近半年统计")
        if it == intent.INTENT_ANOMALY:
            bad_marked = any(mid.startswith(("fpd", "mob3"))
                             for mid in slots["metrics"])
            slots["anomaly_target"] = "bad" if bad_marked else "pass"
        if it == intent.INTENT_FIELD:
            slots["field"] = data.get("field")
            if not slots["field"]:
                missing.append("field")
        if it == intent.INTENT_UNKNOWN:
            # 语义歧义（branch）只许 LLM 举手；其余缺失由系统判定，不信模型
            missing = ["branch"] if "branch" in (data.get("missing") or []) else ["intent"]
            if missing == ["branch"]:
                notes.append("这句话既可能在问模型指标（KS/AUC/PSI），"
                             "也可能在问策略指标（通过率/拒绝率）；先确认再算")

    slots["window"] = win
    trigger = "ambiguous" if missing == ["branch"] else "llm"
    return {"question": question, "intent": it, "trigger": trigger,
            "slots": slots, "missing": missing, "notes": notes}


# ---------------------------------------------------------------------------
# 唯一入口
# ---------------------------------------------------------------------------
def plan_llm(question: str, *, role: str | None = None, budget=None,
             seed: int | None = None, provider=None, model: str = PLAN_MODEL,
             as_of: str = catalog.AS_OF) -> dict:
    """返回与 intent.parse 同形的提问单 + source='llm' + plan_meta（审计字段）。

    抛出：LLMUnavailable（网关失败）/ PlanRejected（内容越界）。上层据此走 D2。
    """
    resp = call("plan", context.plan_prompt(question), plan_schema(),
                model=model, budget=budget, provider=provider, seed=seed,
                prompt_version=PROMPT_VERSION)
    data = resp["data"]

    from llm import validate_plan            # 延迟 import：避免 llm/__init__ 将来成环
    verdict = validate_plan(_plan_check(data), role=role)
    if not verdict["ok"]:
        raise PlanRejected(verdict["bad"])

    parsed = _normalize(data, question, as_of)
    parsed["source"] = "llm"
    parsed["plan_meta"] = resp["meta"]
    return parsed
