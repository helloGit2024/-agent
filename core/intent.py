# -*- coding: utf-8 -*-
"""意图解析层 —— 自然语言 → 结构化的"提问单"。

════════════════════════════════════════════════════════════════════
这一层在 V2 里扮演什么角色（以及为什么默认不用 LLM）
════════════════════════════════════════════════════════════════════
真实产品里，这里应该是 LLM + bind_tools 做 function call（V2.1 的升级点）。
但在回测场景里，它有一个硬约束：**同一个问题问两次，必须解析成同一个计划**。
所以 V2 先用确定性规则解析器（本模块），它同时是 LLM 解析器的**降级兜底**：
LLM 不可用 / 解析出非法计划时，退回规则解析，系统仍能工作。

这是"混合架构"的第一层体现：
    ReAct 的自由度只允许存在于"理解人话"这一段（2~4 步短链），
    解析结果立刻被 validate_plan 接管，后面的执行路径完全确定。

════════════════════════════════════════════════════════════════════
四类意图（对应风控分析师的四类日常问题）
════════════════════════════════════════════════════════════════════
    STATS        统计类：「##产品近一月每个环节/每个策略的通过率、拒绝率」
    ANOMALY      异动归因：「近一周通过率下降 / FPD7 升高的原因」
    CONFIG       配置查询：「##产品当前策略是什么」
    CONTRIBUTION 贡献排序：「哪些策略节点需要调整或进一步 AB test」
    FIELD        字段定位：「FPD30 在哪个库哪个表」（受控查字段字典）

解析输出是一张"提问单"（dict），字段：
    intent / slots{product, window, metrics, dimension} / missing[] / notes[]

★设计要点：missing 不为空 → 对话层必须反问（interrupt），不许猜。
  猜产品、猜时间窗口，是回测 Agent 最容易犯、也最没人发现的两类错误。
"""
from __future__ import annotations

import re
from typing import Any

from . import catalog, knowledge

INTENT_STATS = "STATS"
INTENT_ANOMALY = "ANOMALY"
INTENT_CONFIG = "CONFIG"
INTENT_CONTRIBUTION = "CONTRIBUTION"
INTENT_FIELD = "FIELD"
INTENT_MODEL_DIAG = "MODEL_DIAG"          # V2.5：模型级指标（KS/AUC/PSI…）
INTENT_UNKNOWN = "UNKNOWN"

# 关键词 → 意图。顺序即优先级（越具体越靠前）。
_RULES: list[tuple[str, tuple[str, ...]]] = [
    (INTENT_FIELD, ("哪个表", "哪张表", "哪个库", "字段在哪", "字段字典", "在哪个表")),
    # ★顺序即优先级：CONTRIBUTION 必须排在 CONFIG 前面。
    #   反例（本项目实测）："统计当前策略里所有策略节点的贡献，哪些要 AB"
    #   先命中 CONFIG 的"当前策略"，被误判成配置查询 —— 意图路由的顺序
    #   就是产品语义的优先级，写反了问题不大，答案会牛头不对马嘴。
    (INTENT_CONTRIBUTION, ("贡献", "哪些策略", "需要调整", "ab test", "AB", "调优",
                           "该动哪个", "优化建议")),
    (INTENT_CONFIG, ("策略是什么", "当前策略", "线上策略", "策略流程", "策略长什么样",
                     "现在跑的是什么")),
    (INTENT_ANOMALY, ("原因", "为什么", "为啥", "下降", "升高", "异动", "恶化",
                      "变差", "涨了", "跌了")),
    (INTENT_STATS, ("统计", "通过率", "拒绝率", "多少")),
]


def _hit(text: str, words: tuple[str, ...]) -> str | None:
    t = (text or "").lower()
    for w in words:
        if w.lower() in t:
            return w
    return None


def parse(text: str, as_of: str = catalog.AS_OF,
          default_window_text: str = "近半年") -> dict[str, Any]:
    """规则解析器 —— 确定性、可单测、可作为 LLM 解析器的兜底。

    ★闸门 ⓿（V2.5）：词汇判定在所有意图规则之前。
      放在规则之后 → 永远不触发（STATS 的关键词"多少"会先命中）；
      与规则平级 → "多少"仍然抢走 KS/AUC（本项目实测三句全部如此）；
      所以必须前置，命中即锁定分支，后续规则不参与投票。
    """
    q = (text or "").strip()

    # ---- 闸门 ⓿：先判"这是不是我能算的那一类"，再判"用户想问什么" ----
    from . import model_metrics
    state, mm = model_metrics.detect_state(q)
    if state == "model":
        return _parse_model_diag(q, mm, as_of)
    if state == "ambiguous":
        # 第三态：说的是模型的事，但没点名指标 → 反问，绝不替他选分支。
        # 两个分支的答案形状完全不同（分箱表 vs 漏斗表），选错等于答另一题。
        return _parse_ambiguous(q)

    intent, trigger = INTENT_UNKNOWN, None
    for name, words in _RULES:
        w = _hit(q, words)
        if w:
            intent, trigger = name, w
            break

    slots: dict[str, Any] = {"product": None, "product_name": None,
                             "window": None, "metrics": [], "dimension": None}
    missing: list[str] = []
    notes: list[str] = []

    # ★ 听不出意图时必须反问，不能静默返回 empty 计划。
    #   但：catalog.resolve 已经命中了一个**策略册**指标时，"统计这个指标"
    #   就是最合理的默认意图 —— 不是反问的 case。模型册指标在 detect_state
    #   闸门 ⓿ 已经锁分支了，这里只管策略册命中的情况。
    if intent == INTENT_UNKNOWN:
        hit = catalog.resolve(q)
        if hit.get("metric_id") and hit["metric_id"] not in model_metrics.MODEL_CATALOG:
            intent = INTENT_STATS
            trigger = f"metric:{hit['metric_id']}"
        else:
            missing.append("intent")

    # ---- 产品 ----
    prod = knowledge.resolve_product(q)
    slots["product"], slots["product_name"] = prod["product"], prod["name"]
    if intent in (INTENT_STATS, INTENT_ANOMALY, INTENT_CONFIG, INTENT_CONTRIBUTION) \
            and not prod["product"]:
        missing.append("product")

    # ---- 时间窗口 ----
    # CONFIG 类不需要窗口（问的是"当下"）；CONTRIBUTION 有默认窗口（近 30 天），
    # 但默认值必须写进回答（"默认按近 30 天算"），不能悄悄用。
    if intent in (INTENT_STATS, INTENT_ANOMALY):
        win = catalog.parse_window(q, as_of)
        if win is None:
            missing.append("window")
        slots["window"] = win
    elif intent == INTENT_CONTRIBUTION:
        slots["window"] = catalog.parse_window(q, as_of) or \
            catalog.parse_window(default_window_text, as_of)
        notes.append(f"未指定时间范围，默认按{default_window_text}统计")

    # ---- 指标 ----
    hit = catalog.resolve(q)
    _ambiguous = False
    if hit["ambiguous"]:
        # 关键词过泛：不猜，交给对话层反问（例如只说"通过率"又提了"环节"）
        slots["metrics"] = []
        missing.append("metric")
        notes.append(f"指标口径歧义：{hit['ambiguous']}")
        _ambiguous = True
    elif hit["metric_id"]:
        slots["metrics"] = [hit["metric_id"]]
        _ambiguous = False
    else:
        if intent in (INTENT_STATS, INTENT_ANOMALY, INTENT_CONTRIBUTION):
            # 统计类问题没提指标 → 给默认（申请通过率），但必须声明默认
            slots["metrics"] = ["approval_rate"]
            notes.append("未指定指标，默认按【申请通过率】口径")
        _ambiguous = False
    # 统计类里出现"每个环节/每个策略" → 追加表指标
    if intent == INTENT_STATS:
        # 统计类问题一律带环节表：没有它就说不出"哪一段最堵"，
        # 而"哪一段最堵"恰恰是"通过率太低"这类问题的第一层答案。
        if "stage_rates" not in slots["metrics"]:
            slots["metrics"].append("stage_rates")
        if "策略" in q and "policy_rates" not in slots["metrics"]:
            slots["metrics"].append("policy_rates")
        # 歧义已被"每个环节/每个策略"这两个更具体的词消解 → 撤销 metric 缺失
        if slots["metrics"]:
            missing = [m for m in missing if m != "metric"]
            if _ambiguous:
                notes = [n for n in notes if not n.startswith("指标口径歧义")]

    # ---- 字段 ----
    if intent == INTENT_FIELD:
        m = re.search(r"(FPD\s?\d+|\w*BAD\w*|QUERY_3M|APPLY_24H)", q, re.I)
        field = m.group(1).replace(" ", "").upper() if m else None
        slots["field"] = field
        if not field:
            missing.append("field")

    # ---- 异动类的目标指标：从意图关键词里判断是"通过率降"还是"FPD 升" ----
    if intent == INTENT_ANOMALY:
        if any(k in q for k in ("fpd", "FPD", "逾期", "坏账")):
            slots["anomaly_target"] = "bad"
            slots["metrics"] = slots["metrics"] or ["fpd7_rate"]
        else:
            slots["anomaly_target"] = "pass"
            slots["metrics"] = ["approval_rate"]

    return {"question": q, "intent": intent, "trigger": trigger, "slots": slots,
            "missing": missing, "notes": notes}


# ---------------------------------------------------------------------------
# 分流未定（T-106）：问的是模型的事，但没说要看哪个指标
# ---------------------------------------------------------------------------
def _parse_ambiguous(q: str) -> dict[str, Any]:
    """返回一张"缺分支"的提问单 → 对话层被迫反问。

    刻意**不**给默认指标。默认 KS 会让"模型最近表现怎么样"变成一句
    "KS=0.21"的断言；默认通过率更糟 —— 答的是另一个问题。
    """
    prod = knowledge.resolve_product(q)
    return {"question": q, "intent": INTENT_UNKNOWN, "trigger": "ambiguous",
            "slots": {"product": prod["product"], "product_name": prod["name"],
                      "window": None, "metrics": [], "dimension": None},
            "missing": ["branch"],
            "notes": ["这句话既可能在问模型指标（KS/AUC/PSI），也可能在问策略指标"
                      "（通过率/拒绝率）；两者答案形状不同，先确认再算"]}


# ---------------------------------------------------------------------------
# 模型级指标分支（V2.5）：与可聚合分支**完全分开**的一份解析
# ---------------------------------------------------------------------------
def _parse_model_diag(q: str, metric_id: str, as_of: str) -> dict[str, Any]:
    """解析模型级提问。

    三个默认值的处理原则（这里是"降级也不能改口径"的地方）：
      · window 缺 → 默认近三个月（模型级指标样本少，窗口太短算不出），**必须声明**；
      · bins   缺 → 默认 5 箱，必须声明；用户说"10 箱"就要能识别；
      · label  缺 → 跟随指标声明（fpd30），但问题里提到 FPD7/MOB3 时以问题为准。
    产品缺失**不兜底** —— 按现金贷兜底会把"分期的问题"答成"现金贷的数"。
    """
    from . import model_metrics
    m = model_metrics.MODEL_CATALOG[metric_id]
    prod = knowledge.resolve_product(q)
    slots: dict[str, Any] = {
        "product": prod["product"], "product_name": prod["name"],
        "window": None, "metrics": [], "model_metric": metric_id,
        "dimension": "overall", "bins": model_metrics.DEFAULT_BINS,
        "label": m.label_col,
    }

    # 维度提取：问题里点名了"每天/每月/按渠道" → 模型级指标按该维度聚合。
    # 没点名 = overall（整体一个值）。这是口径不是偏好，必须从文本显式提取。
    dim_map = (("每天", "day"), ("按天", "day"), ("日度", "day"),
               ("每月", "month"), ("按月", "month"), ("月度", "month"),
               ("按渠道", "channel"), ("渠道", "channel"))
    for cue, dim in dim_map:
        if cue in q and dim in m.comparable_dimensions:
            slots["dimension"] = dim
            break
    missing: list[str] = []
    notes: list[str] = []
    if not prod["product"]:
        missing.append("product")

    win = catalog.parse_window(q, as_of)
    if win is None:
        win = catalog.parse_window("近三个月", as_of)
        notes.append("未指定时间范围，默认按【近三个月】统计")
    slots["window"] = win

    up = q.upper()
    if "FPD7" in up:
        slots["label"] = "fpd7"
    elif "MOB3" in up:
        slots["label"] = "mob3"
    elif "FPD30" in up:
        slots["label"] = "fpd30"

    mb = re.search(r"(\d+)\s*箱", q)
    if m.kind == "curve":
        # 曲线类不分箱：声明"默认 5 箱"是**没用到的声明**，等于撒谎。
        pass
    elif mb:
        slots["bins"] = int(mb.group(1))
    else:
        notes.append(f"未指定箱数，默认按【{model_metrics.DEFAULT_BINS} 箱】等频分箱")
    if m.ref_period == "required":
        slots["ref_period"] = "train_2025Q4"
        notes.append("PSI 需要基准期，默认用【train_2025Q4（2026Q1）】")

    # ---- 归因词检测：模型级指标的"为什么降"链尚未实现，诚实声明 ----
    # （当前模型级归因执行体不存在，ANOMALY 链只处理策略册指标）
    _ATTRIB_WORDS = ("原因", "为什么", "为啥", "下降", "升高", "异动", "恶化",
                     "变差", "涨了", "跌了", "分析", "排查")
    if any(w in q for w in _ATTRIB_WORDS):
        notes.append("模型级指标的归因链尚未实现，当前返回指标值供参考；"
                     "如需归因，请提供同期策略指标（通过率/FPD）做交叉分析")

    return {"question": q, "intent": INTENT_MODEL_DIAG, "trigger": metric_id,
            "slots": slots, "missing": missing, "notes": notes}


# ---------------------------------------------------------------------------
# 反问文案（interrupt 的 payload）—— 只问真正缺的，最多两轮
# ---------------------------------------------------------------------------
CLARIFY_TEXT = {
    # 意图都听不出时，不要假装听懂，直接把能力清单摆出来让用户挑
    "intent": "没太听懂这句话要怎么分析，你是想问下面哪一类？",
    "product": "要分析哪个产品？（可选：现金贷 / 消费分期）",
    "window": "统计的样本时间范围是？（例如：近一个月 / 近一周 / 2026-05）",
    "metric": "您说的指标具体指哪个口径？（申请通过率 / 环节通过率 / 策略通过率）",
    "branch": "这句要按哪一类指标来算？（模型指标：KS/AUC/PSI；策略指标：通过率/拒绝率/FPD）",
    "field": "要查哪个字段？（例如：FPD30 / QUERY_3M）",
}


def build_clarify(parsed: dict) -> dict | None:
    """按 missing 生成一个澄清请求；没有缺失返回 None（不需要打断用户）。"""
    if not parsed["missing"]:
        return None
    first = parsed["missing"][0]
    return {
        "kind": "clarify",
        "slot": first,
        "question": CLARIFY_TEXT.get(first, f"请补充 {first}"),
        "missing": list(parsed["missing"]),
        "options": _options_for(first),
        "original_question": parsed["question"],
    }


def _options_for(slot: str) -> list[str]:
    if slot == "intent":
        return ["统计通过率/拒绝率", "异动归因（为什么变差）",
                "当前策略配置是什么", "策略贡献排序/该调哪个", "字段在哪张表"]
    if slot == "product":
        return [f"{meta['name']}（{pid}）" for pid, meta in knowledge.PRODUCTS.items()]
    if slot == "window":
        return ["近一周", "近一个月", "近三个月", "2026-05"]
    if slot == "metric":
        return ["申请通过率", "环节通过率", "策略通过率"]
    if slot == "branch":
        return ["模型指标（KS/AUC/PSI）", "策略指标（通过率/拒绝率/FPD）"]
    if slot == "field":
        return list(knowledge.FIELD_DICT)
    return []


def apply_answer(parsed: dict, answer: dict) -> dict:
    """把用户的补充回答合并回提问单，并重新判定缺失项。

    answer 形如 {"window": "近一个月"} / {"product": "P_CASH_001"} / {"text": "..."}。
    教学要点：合并后再走一遍 resolve/parse_window —— 用户可能答"上个月"
    或"现金贷那个"，仍然走同一套语义层，不允许在这里出现第二套解析逻辑。
    """
    slots = dict(parsed["slots"])
    text = answer.get("text") or ""

    # ★用户答的是"走哪一类指标"（T-106 分流未定）：
    #   这不是"默认一个口径"，是用户**显式选了分支**，所以可以继续；
    #   但仍然回到 parse / _parse_model_diag，不在这里另写一套解析。
    if answer.get("branch"):
        want = str(answer["branch"])
        if "模型" in want or want.lower() in ("model", "model_diag"):
            p = _parse_model_diag(parsed["question"], "ks", catalog.AS_OF)
            p["notes"].append("分流未定，按你选的【模型指标】继续：默认看 KS"
                              "（口径已写进信封，可改问 AUC/PSI）")
            return p
        p = parse(f"{parsed['question']} 通过率")
        p["question"] = parsed["question"]
        p["notes"].append("分流未定，按你选的【策略指标】继续：默认口径=申请通过率")
        return p

    # ★ 用户答的是"要问哪一类"（原问题意图没听出来）：
    #   把【原问题 + 用户的补充】拼起来重新丢给 parse，而不是在这里另写一套
    #   意图判定 —— 否则规则会分两处维护，改一处忘一处，行为就开始漂移。
    if answer.get("intent") and parsed["intent"] == INTENT_UNKNOWN:
        reparsed = parse(f"{parsed['question']} {answer['intent']}")
        if reparsed["intent"] != INTENT_UNKNOWN:
            parsed = {**reparsed, "question": parsed["question"],
                      "notes": list(parsed.get("notes") or []) + list(reparsed["notes"])}
            # 已确认过的槽位不能因为重解析而丢失
            merged = dict(parsed["slots"])
            for k, v in slots.items():
                if v and not merged.get(k):
                    merged[k] = v
            slots = merged

    for key, val in answer.items():
        if key in ("text", "kind", "intent"):
            continue
        if key == "product":
            p = knowledge.resolve_product(str(val))["product"] or val
            slots["product"] = p
            slots["product_name"] = knowledge.PRODUCTS.get(p, {}).get("name")
        elif key == "window":
            slots["window"] = catalog.parse_window(str(val)) or parsed["slots"]["window"]
        elif key == "metric":
            hit = catalog.resolve(str(val))
            slots["metrics"] = [hit["metric_id"]] if hit["metric_id"] else slots["metrics"]

    # 兜底：回答是自由文本时，再整体解析一遍（仍走语义层）
    if text:
        p = knowledge.resolve_product(text)
        if p["product"] and not slots.get("product"):
            slots["product"], slots["product_name"] = p["product"], p["name"]
        w = catalog.parse_window(text)
        if w and not slots.get("window"):
            slots["window"] = w

    missing = []
    if parsed["intent"] in (INTENT_STATS, INTENT_ANOMALY, INTENT_CONFIG,
                            INTENT_CONTRIBUTION) and not slots.get("product"):
        missing.append("product")
    if parsed["intent"] in (INTENT_STATS, INTENT_ANOMALY) and not slots.get("window"):
        missing.append("window")
    if parsed["intent"] == INTENT_STATS and not slots.get("metrics"):
        missing.append("metric")
    return {**parsed, "slots": slots, "missing": missing}
