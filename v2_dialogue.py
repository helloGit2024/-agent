#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""策略回测分析 Agent · 企业版 V2 —— 对话层（自然语言 → 回测 → 回答）。

════════════════════════════════════════════════════════════════════
V2 与 V1 是什么关系？
════════════════════════════════════════════════════════════════════
一句话：**V1 是引擎，V2 是外壳**。V1 的 12 条策略函数、事实长表、
审计链、guard/降级机制一个字没改，V2 只是在它前面加了三样东西：

    ① 语义层（core/catalog.py）       —— 口径注册与封闭
    ② 意图层（core/intent.py）        —— 人话 → 结构化提问单
    ③ 对话图（本文件）                —— 澄清 → 计划 → 执行 → 回答

图结构（ReAct 只允许出现在 understand/clarify 这一步）：
    START → understand ─┬─(缺参数)→ clarify(interrupt) ─┐
                        │                              │
                        └─(参数齐)→ plan → execute → narrate → END
                              ↑__________________│

════════════════════════════════════════════════════════════════════
三条不可让步的纪律（V2 比 V1 更容易违反，因为多了 LLM 的自由度）
════════════════════════════════════════════════════════════════════
  纪律 1：LLM 不碰数字。回答里的每个数字都来自 AnswerBook（登记簿），
          叙述只允许用槽位拼接 —— 评估器会扫回答里的百分数做溯源校验。
  纪律 2：口径不猜。指标必须命中语义层注册表；时间窗口解析不出就反问；
          产品识别不出就反问。宁可不答，不可错答。
  纪律 3：执行路径确定。解析结果立刻变成"计划"，过 validate_plan_v2，
          之后是固定节点，不存在"LLM 自己决定下一步"的空间。

════════════════════════════════════════════════════════════════════
跑法（在本目录下）
════════════════════════════════════════════════════════════════════
    python3 v2_dialogue.py            # 五个示范问题（含一次澄清交互）
    python3 v2_dialogue.py --ask "现金贷近一个月每个策略的通过率"
"""
from __future__ import annotations

import functools
import json
import re
import sys
import time
from pathlib import Path
from typing import Annotated, Any, TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parent))

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from core import (access, anomaly, catalog, entities, envelope, intent,
                  knowledge, model_metrics, ranking, retro)
from core.codes import STAGE_ORDER
from core.db import Database
from core.policies import DEFAULT_CONFIG
import llm
import memory
from scheduler import registry as sched
from tools import compute_model_metric as model_tool
from tools import registry as tool_registry

ART = Path("artifacts")
DB_PATH = ART / "v2_dialogue.db"

# 线上两个配置版本（对应 knowledge.POLICY_VERSIONS）：
#   v2.1 = 年初基线（P_CR_001=6）；v2.2 = 5/31 上线（P_CR_001=3 收紧）
CFG_PREV = dict(DEFAULT_CONFIG)
CFG_CUR = {**DEFAULT_CONFIG, "P_CR_001": 3}
RUN_PREV = "ONLINE_v2.1"
RUN_CUR = "ONLINE_v2.2"


def _merge(a: dict, b: dict) -> dict:
    if not a:
        return b
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


class DState(TypedDict, total=False):
    session_id: str
    question: str
    role: str                        # 角色（权限挂在指标上，Harness 只认身份）
    parsed: dict
    pending: dict | None
    clarify_round: int
    plan: dict
    result: dict
    answer: str
    book: dict                       # 登记簿快照（数字唯一来源）
    degraded: bool                   # 执行降级标志（True = 没算出结果，只交失败说明）
    trace: Annotated[list[str], lambda a, b: (a or []) + (b or [])]
    findings: Annotated[dict, _merge]


# ---------------------------------------------------------------------------
# 数字登记簿（V2 版）：每个数字带格式、来源等级、口径版本
# ---------------------------------------------------------------------------
class AnswerBook:
    """回答级登记簿 —— V1 的 MetricRegistry 的升级版。

    升级点（为什么 V1 的不够用）：
      V1 报告是固定模板，槽位是写死的；
      V2 回答的结构随问题变化，所以登记簿要能承载"任意 key + 格式化方式
      + 口径版本"，并且要能反向校验回答里的数字是否都来自这里。
    """

    def __init__(self) -> None:
        self._d: dict[str, dict] = {}

    def put(self, key: str, value: Any, unit: str = "pct",
            level: str = "MEASURED", note: str = "", metric_id: str = "") -> None:
        if key in self._d:                     # 同 key 重复登记 = 口径冲突，直接炸
            raise ValueError(f"登记簿重复登记: {key}（不允许静默覆盖）")
        self._d[key] = {"value": value, "unit": unit, "level": level,
                        "note": note, "metric_id": metric_id}

    def get(self, key: str) -> Any:
        return self._d[key]["value"]

    def has(self, key: str) -> bool:
        """渲染层的"这一项有没有"（有才打印，没有就整段不提）。"""
        return key in self._d

    def fmt(self, key: str) -> str:
        v = self._d[key]["value"]
        u = self._d[key]["unit"]
        if v is None:
            return "不可得"
        if u == "pct":
            return f"{v:.1%}"
        if u == "float":
            return f"{v:.2f}" if abs(v) >= 1e-6 else "0.00"
        if u == "money":
            return f"{v:,.0f} 元"
        if u == "int":
            return f"{int(v)}"
        if u == "raw":
            # raw = 已经是渲染好的字符串（如阈值 "55%"、"3"）。
            # 为什么还要登记：阈值来自受控配置表而不是计算，但它照样是
            # 一个出现在正文里的数字 —— 不登记，溯源闸门就会把它当幻觉拦下。
            return str(v)
        return str(v)

    def snapshot(self) -> dict:
        return {k: dict(v) for k, v in self._d.items()}

    def allowed_number_strings(self) -> set[str]:
        """把登记簿里所有数字可能出现在正文里的"写法"都列出来（供溯源校验）。"""
        out: set[str] = set()
        for d in self._d.values():
            v = d["value"]
            if v is None:
                continue
            if d["unit"] == "pct":
                out.add(f"{v:.1%}")
                out.add(f"{v * 100:.1f}%")
                out.add(f"{int(round(v * 100))}%")
            elif isinstance(v, str):
                out.add(v.strip())          # raw
                # raw 是"原样引用的外部文本"（阈值 / 失败原因），
                # 里面自带的数字 token 也要放行，否则失败原因里的日期
                # 会被溯源闸门当幻觉拦掉 —— 那等于"报错信息自己触发报错"
                out.update(m.group(0) for m in _NUM.finditer(v))
            elif isinstance(v, (int, float)):
                out.add(f"{int(v)}" if float(v).is_integer() else f"{v}")
                out.add(f"{v:,.0f}")
        return out


_NUM = re.compile(r"\d+(?:\.\d+)?%|(?<![\d.])\d{2,}(?![\d.%])")


def verify_numbers(answer: str, book: dict) -> dict:
    """回答可溯源性校验：正文里的百分数/大整数必须能在登记簿里找到。

    教学要点：这是"LLM 不碰数字"纪律的**可执行检查**（不是口头约定）。
    允许例外：年份、日期、版本号、样本量说明里的数字 —— 因此只扫
    "百分数"和"≥2 位的整数"，且这些也必须来自登记簿。
    """
    allowed = set()
    for d in book.values():
        v = d["value"]
        if v is None:
            continue
        if d["unit"] == "pct":
            forms = {f"{v:.1%}", f"{v * 100:.1f}%", f"{int(round(v * 100))}%"}
            allowed |= forms | {f[1:] for f in forms if f.startswith("-")}
        elif isinstance(v, str):
            allowed.add(v.strip())          # raw：登记时就是渲染好的字符串
        elif isinstance(v, (int, float)):
            allowed |= {f"{int(v)}", f"{v:,.0f}", f"{v}"}
    # 先把"非结论数字"从正文里剥掉，再扫剩下的：
    #   日期（2026-06-17） / 策略与环节编码（P_ADM_001、S3_CR）
    #   / 版本号（v2.2） / 阈值型参数（"收紧到 3"）
    # 这一步很重要：不做的话校验器会把每个编码里的数字都当成"幻觉"，
    # 变成狼来了——校验器一旦总是误报，就再也没人看它了。
    body = re.sub(r"\d{4}-\d{2}-\d{2}", " ", answer or "")
    # 外卸引用号 ref_20261109_7909：它是**文件名**不是统计结论。
    # 不剥掉的话，曲线类回答每一条都会报"未登记数字 20261109"——
    # 而 ref_id 里带日期是设计（谁都能看出这是哪天的哪条曲线）。
    # 校验器一旦对某一类问题**必然**误报，它的话就没人看了。
    body = re.sub(r"\bref_\d{8}_[0-9a-f]{4}\b", " ", body)
    body = re.sub(r"\b[A-Z]{1,3}_[A-Z]{2,}_\d{3}\b", " ", body)
    body = re.sub(r"\bS\d_[A-Z]{2,4}\b", " ", body)
    body = re.sub(r"\bv\d+(\.\d+)?\b", " ", body)
    # 阈值/规则描述里的参数不是统计结论（"> 55%"、"24 小时"、"3 次"、"60 分"）
    body = re.sub(r"[><]\s*\d+(\.\d+)?%?", " ", body)
    body = re.sub(r"\d+\s*(小时|次|天|分|笔|单)", " ", body)
    stray = []
    for tok in _NUM.findall(body):
        if tok in allowed or tok.lstrip("-") in allowed:
            continue
        if re.fullmatch(r"\d{4}", tok) and 2020 <= int(tok) <= 2030:
            continue                    # 年份（如 2026）不算结论数字
        if tok in ("30", "60", "90", "95", "95%", "7"):   # 口径里的固定参数
            continue
        stray.append(tok)
    return {"ok": not stray, "stray": stray, "allowed_n": len(allowed)}


# ---------------------------------------------------------------------------
# 计划校验（V2 版）：语义层 ID 必须注册 + 步骤必须合法
# ---------------------------------------------------------------------------
VALID_STEPS_V2 = {"understand", "clarify", "consistency_gate", "semantic_compute",
                  "model_compute", "anomaly_attribute", "policy_rank",
                  "config_lookup", "field_lookup", "narrate"}
VALID_SCOPES = {"product", "channel", "new_vs_old"}

# 两本册子的步骤与工具名：跨册引用 = 计划里同时出现两个册子的取数路径
SEMANTIC_STEPS_V2 = {"consistency_gate", "semantic_compute",
                     "anomaly_attribute", "policy_rank"}
MODEL_STEPS_V2 = {"model_compute"}


def validate_plan_v2(plan: dict) -> tuple[bool, list[str]]:
    """LLM 计划（或规则计划）的唯一入口闸门。

    ★与 V1 的区别：V1 校验"步骤"，V2 还要校验"指标必须注册过"。
      这一条让"口径封闭"从口头约定变成结构约束 ——
      LLM 编不出一个叫"综合健康度通过率"的指标，因为语义层里没有。

    ★T-204：跨册引用**直接拒**，且**不改写**。模型级分支（intent=MODEL_DIAG
      或 branch=model）只许出现 model_compute；其余分支只许出现策略步骤。
      改写（"我把 compute_metric 删掉继续跑"）比拒绝更危险：它把一次
      越权尝试变成一次"看起来成功"的回答，审计里再也看不见。
    """
    errs: list[str] = []
    if not isinstance(plan, dict):
        return False, ["计划必须是 dict"]
    for s in plan.get("steps", []):
        if s not in VALID_STEPS_V2:
            errs.append(f"非法步骤: {s}")
    for mid in plan.get("metrics", []):
        if mid not in catalog.CATALOG:
            errs.append(f"未注册的指标: {mid}（口径封闭：语义层拒绝计算）")
    for sc in plan.get("scope", {}).get("dimensions", []):
        if sc not in VALID_SCOPES:
            errs.append(f"非法下钻维度: {sc}")

    steps = set(plan.get("steps") or [])
    tools = {c.get("tool") for c in plan.get("tool_calls") or []
             if isinstance(c, dict)}
    branch_model = (plan.get("intent") == intent.INTENT_MODEL_DIAG
                    or plan.get("branch") == "model")
    if branch_model:
        crossed = sorted(steps & SEMANTIC_STEPS_V2)
        if crossed:
            errs.append(f"跨册引用：模型分支出现策略步骤 {crossed}")
        if plan.get("metrics"):
            errs.append(f"跨册引用：模型分支不许带策略指标 {plan['metrics']}")
        if "compute_metric" in tools:
            errs.append("跨册引用：模型分支出现 compute_metric（两册互斥）")
    else:
        if steps & MODEL_STEPS_V2:
            errs.append("跨册引用：非模型分支出现 model_compute")
        if "compute_model_metric" in tools:
            errs.append("跨册引用：非模型分支出现 compute_model_metric")
        if plan.get("model_metric"):
            errs.append(f"跨册引用：非模型分支不许带模型指标 {plan['model_metric']}")

    if not plan.get("steps"):
        errs.append("steps 不能为空")
    return (not errs), errs


# ---------------------------------------------------------------------------
# 引擎：节点工厂 + guard（与 V1 同款，含同一个 PASSTHROUGH 教训）
# ---------------------------------------------------------------------------
class DialogEngine:
    # GraphBubbleUp 是 interrupt 的载体。吞掉它 = 澄清闸门静默消失，
    # 用户永远等不到提问，系统还"以为"自己跑完了。（V1 踩过一次，这里不能再踩）
    PASSTHROUGH = (GraphBubbleUp,)

    def __init__(self, db: Database, verbose: bool = True,
                 role: str = access.DEFAULT_ROLE):
        self.db = db
        self.verbose = verbose                         # 是否打印过程 trace
        self.clarify_log: list[dict] = []              # 澄清记录（评估用）
        # 角色：Harness 只认"你是谁"，语义层认"你能做什么"（§7.6 分工）
        self.role = role
        # 记忆层（S0 只做接口）：短期会话。默认进程内，重启即失效。
        # 刻意**不**读它来改口径：记忆只允许填默认槽位（memory/__init__.py 硬规则）。
        self.memory = memory.ShortTerm()
        self._bind_tools()

    # ---------------- T-107：11 个工具的统一入口 ----------------
    def _bind_tools(self) -> None:
        """把"由编排层实现"的三个工具注入工具注册表。

        为什么用注入而不是让 tools/ 反向 import 本文件：
        architecture.md §2 第一条禁止 tools/ 依赖编排层。注入之后，
        "谁实现"和"谁调度"分开，但调用仍然只有一个入口（registry.call），
        审计因此只需要挂一处。
        """
        from tools import registry as reg
        reg.bind("rank_policies", self._tool_rank_policies)
        reg.bind("attribute_change", self._tool_attribute_change)

    def _tool_rank_policies(self, db, args: dict, role, as_of) -> dict:
        res, book = self._exec_contribution(args.get("parsed") or {})
        return {"ok": True, "tool": "rank_policies",
                "result": res, "book": book.snapshot()}

    def _tool_attribute_change(self, db, args: dict, role, as_of) -> dict:
        res, book = self._exec_anomaly(args.get("parsed") or {})
        return {"ok": True, "tool": "attribute_change",
                "result": res, "book": book.snapshot()}

    def _call_tool(self, name: str, parsed: dict):
        """经统一入口调工具，再把 (结果, 登记簿) 还原成执行体的返回形状。

        形状不变是刻意的：n_execute 与各 _r_* 渲染器一行都不用改，
        改的只是"从哪个门进来"。
        """
        out = tool_registry.call(self.db, name, {"parsed": parsed}, role=self.role)
        book = AnswerBook()
        book._d = dict(out.get("book") or {})
        return out.get("result"), book

    def guard(self, node: str):
        def deco(fn):
            @functools.wraps(fn)
            def wrapper(state: DState) -> dict:
                sid = state.get("session_id", "-")
                try:
                    return fn(state)
                except self.PASSTHROUGH:
                    raise
                except Exception as e:                  # noqa: BLE001
                    self.db.auditor(sid).append("node.failed", {
                        "node": node, "error": f"{type(e).__name__}: {e}"})
                    return {"trace": [f"[{node}] ✘ {type(e).__name__}: {e}"],
                            "findings": {"error": f"{node}: {e}"}}
            return wrapper
        return deco

    # ---------------- 节点 1：理解 ----------------
    def n_understand(self, state: DState) -> dict:
        # ★ 理解层是 LLM 的落点 #1（V2.5 §6）。S0 阶段开关默认关，
        #   关闭时 fallback 就是**规则版本身** —— 走的是同一条路，不是两条分支。
        #   所以"接模型"这件事的行为差异，只会体现在 plan_source 上。
        degrade = degrade_reason = None
        if state.get("parsed"):
            parsed, plan_source = state["parsed"], "state"
        else:
            res = llm.plan(state["question"], "auto",
                           lambda: intent.parse(state["question"]),
                           role=state.get("role"),
                           session_id=state.get("session_id"))
            plan_source = res.pop("source", "rules")
            # D2 是"发生过的事件"，必须留在 trace/findings 里，不许静默吞掉
            degrade = res.pop("degrade", None)
            degrade_reason = res.pop("degraded_reason", None)
            parsed = res
        pend = intent.build_clarify(parsed) if parsed["missing"] else None
        # 最多两轮澄清：两轮之后仍然缺，就按"可用默认"继续，并在回答里声明
        if pend and (state.get("clarify_round") or 0) >= 2:
            parsed = self._fill_defaults(parsed)
            pend = None
        lines = [f"[understand] 意图={parsed['intent']}"
                 f"（触发词「{parsed.get('trigger')}」）"
                 f" 产品={parsed['slots'].get('product_name') or '未识别'}"
                 f" 指标={parsed['slots'].get('model_metric') or parsed['slots'].get('metrics')}"]
        if pend:
            lines.append(f"[understand] 缺参数 {parsed['missing']} → 需要澄清")
        elif parsed.get("notes"):
            lines.append(f"[understand] 声明：{'；'.join(parsed['notes'])}")
        findings = {"intent": parsed["intent"], "parsed": parsed,
                    "plan_source": plan_source}
        if degrade:
            lines.append(f"[understand] ✘ LLM 规划不可用（{degrade_reason}）"
                         f"→ {degrade} 回退规则版（口径不变）")
            findings["plan_degrade"] = degrade
            findings["plan_degrade_reason"] = degrade_reason
        return {"parsed": parsed, "pending": pend, "trace": lines,
                "findings": findings}

    def _fill_defaults(self, parsed: dict) -> dict:
        """两轮澄清后仍缺参数时的兜底：用显式默认值继续，并在回答中声明。"""
        slots = dict(parsed["slots"])
        notes = list(parsed.get("notes") or [])
        if not slots.get("product"):
            slots["product"] = "P_CASH_001"
            slots["product_name"] = "现金贷"
            notes.append("产品未确认，已按【现金贷】兜底（回答中已声明）")
        if parsed["intent"] in (intent.INTENT_STATS, intent.INTENT_ANOMALY) \
                and not slots.get("window"):
            slots["window"] = catalog.parse_window("近一个月")
            notes.append("时间范围未确认，已按【近一个月】兜底（回答中已声明）")
        return {**parsed, "slots": slots, "missing": [], "notes": notes,
                "defaulted": True}

    # ---------------- 节点 2：澄清（唯一允许 interrupt 的节点） ----------------
    def n_clarify(self, state: DState) -> dict:
        """向用户反问缺失参数。零副作用（resume 会重跑本节点）。"""
        pend = state.get("pending") or {}
        answer = interrupt(pend)
        self.clarify_log.append({"question": pend.get("question"),
                                 "answer": answer})
        merged = intent.apply_answer(state["parsed"], answer or {})
        pend2 = intent.build_clarify(merged) if merged["missing"] else None
        rnd = (state.get("clarify_round") or 0) + 1
        return {"parsed": merged, "pending": pend2, "clarify_round": rnd,
                "trace": [f"[clarify] 用户补充：{answer} → 剩余缺失 {merged['missing']}"]}

    # ---------------- 节点 3：计划 ----------------
    def n_plan(self, state: DState) -> dict:
        parsed = state["parsed"]
        it = parsed["intent"]
        steps = ["understand", "semantic_compute", "narrate"]
        if it == intent.INTENT_STATS:
            steps = ["understand", "consistency_gate", "semantic_compute", "narrate"]
        elif it == intent.INTENT_ANOMALY:
            steps = ["understand", "consistency_gate", "anomaly_attribute", "narrate"]
        elif it == intent.INTENT_CONFIG:
            steps = ["understand", "config_lookup", "narrate"]
        elif it == intent.INTENT_CONTRIBUTION:
            steps = ["understand", "consistency_gate", "policy_rank", "narrate"]
        elif it == intent.INTENT_FIELD:
            steps = ["understand", "field_lookup", "narrate"]
        elif it == intent.INTENT_MODEL_DIAG:
            # 模型级分支：没有 consistency_gate（不涉及漏斗判决重放），
            # 但**唯一**允许的取数工具是 compute_model_metric（两册互斥）
            steps = ["understand", "model_compute", "narrate"]
        plan = {"question": parsed["question"], "intent": it, "steps": steps,
                "metrics": parsed["slots"].get("metrics") or [],
                "scope": {"product": parsed["slots"].get("product"),
                          "window": (parsed["slots"].get("window").as_dict()
                                     if parsed["slots"].get("window") else None),
                          "dimensions": ["product"]}}
        ok, errs = validate_plan_v2(plan)
        if not ok:
            raise ValueError(f"计划非法（LLM 计划会被这里拦下）: {errs}")
        self.db.auditor(state["session_id"]).append("plan.validated", plan)
        return {"plan": plan, "trace": [f"[plan] {' → '.join(steps)}"],
                "findings": {"plan": plan}}

    # ---------------- 节点 4：执行（按意图派发，全部确定性） ----------------
    def n_execute(self, state: DState) -> dict:
        it = state["parsed"]["intent"]
        try:
            res, book = self._dispatch(it, state["parsed"])
        except Exception as e:                                  # noqa: BLE001
            # ★执行体抛错时，**绝不能让它变成 answer=None**。
            #   本项目实测过：注入一个 execute 异常，用户最终拿到的是
            #   answer=None + 一条 "[narrate] ✘ KeyError: 'result'" 的 trace，
            #   界面上就是点了没反应。这比"答得不好"严重得多。
            #   这里的处理原则：失败也要交一份**人话说明书**，并如实说明
            #   "哪一步失败、已完成了什么、可能的原因、下一步怎么办"。
            reason = f"{type(e).__name__}: {e}"
            self.db.auditor(state["session_id"]).append("execute.failed", {
                "intent": it, "reason": reason})
            book = AnswerBook()
            # 失败原因原样登记（raw），否则原因里的日期/编码会被溯源闸门拦
            book.put("degraded.reason", reason, "raw", note="执行失败原因")
            return {"result": {"kind": "degraded", "intent": it, "reason": reason,
                               "question": state["parsed"]["question"]},
                    "book": book.snapshot(), "degraded": True,
                    "trace": [f"[execute] ✘ {reason} → 降级为失败说明（不抛错、不白屏）"],
                    "findings": {"error": reason}}
        return {"result": res, "book": book.snapshot(), "degraded": False,
                "trace": [f"[execute] {it} 执行完成，登记 {len(book.snapshot())} 个数字"],
                "findings": {"result_keys": list(res.keys())}}

    def _dispatch(self, it: str, parsed: dict):
        """按意图选执行体。抽出来是为了让 n_execute 能统一 try/except 兜底。"""
        # 闸门 ⓿ 已经在 intent.parse 里锁定分支：模型级请求**只**走这条路，
        # 可聚合工具在这条分支上根本不注册（两册互斥）。
        if it == intent.INTENT_MODEL_DIAG:
            return self._exec_model_diag(parsed)
        # 权限前置：在**任何取数发生之前**判定（取数后过滤 = 越权已经发生）
        denied = self._access_precheck(parsed)
        if denied:
            return denied, AnswerBook()
        if it == intent.INTENT_STATS:
            return self._exec_stats(parsed)
        if it == intent.INTENT_ANOMALY:
            return self._call_tool("attribute_change", parsed)
        if it == intent.INTENT_CONFIG:
            return self._exec_config(parsed)
        if it == intent.INTENT_CONTRIBUTION:
            return self._call_tool("rank_policies", parsed)
        if it == intent.INTENT_FIELD:
            return self._exec_field(parsed)
        # 意图始终没识别出来（UNKNOWN）：给一张"我能干这些"的清单，成本近乎为零
        return {"kind": "capability", "asked": parsed["question"],
                "supported": ["统计通过率/拒绝率", "异动归因（为什么变差）",
                              "当前策略配置", "策略贡献排序/该调哪个", "字段在哪张表"]}, \
            AnswerBook()

    # ---- 执行体 1：统计类 ----
    def _exec_stats(self, parsed: dict):
        slots = parsed["slots"]
        win, prod = slots["window"], slots["product"]
        book = AnswerBook()
        view = catalog.build_view(self.db, RUN_CUR, win, prod)
        out: dict[str, Any] = {"window": win.as_dict(), "product": prod,
                               "row_filter": view.row_filter, "metrics": {}}
        if not view.apps:
            raise ValueError(f"{win.label} 窗口内没有申请数据（无法统计）")
        for mid in slots["metrics"]:
            m = catalog.CATALOG[mid]
            if m.lag_days > 0:                          # 表指标不需要到期校准
                chk = catalog.check_maturity(mid, win)
                view2 = catalog.build_view(self.db, RUN_CUR, chk["window"], prod)
                res = catalog.compute(mid, view2)
                res["maturity"] = chk
            else:
                res = catalog.compute(mid, view)
            out["metrics"][mid] = res
            if m.kind == "scalar":
                book.put(f"{mid}.value", res["value"], "pct",
                         metric_id=mid, note=m.name)
                book.put(f"{mid}.n", res.get("denominator_n"), "int",
                         metric_id=mid, note="分母")
            else:
                for row in res["rows"]:
                    key = row.get("policy_id") or row.get("stage")
                    for fld in ("pass_rate", "reject_rate", "other_rate", "entered",
                                "executed", "passed", "rejected", "unknown", "miss_rate"):
                        if fld not in row:
                            continue
                        unit = "pct" if fld.endswith("rate") else "int"
                        # 即使值为 None 也登记：格式化时会显示"不可得"，
                        # 避免渲染层因为 KeyError 崩掉（比例类指标天然可能为 None）
                        book.put(f"{mid}.{key}.{fld}", row[fld], unit,
                                 metric_id=mid, note=f"{key}.{fld}")
        # 一致性核对（统计类必做：不一致的数据不允许出统计结论）
        con = self._consistency()
        book.put("consistency.rate", con["rate"], "pct", note="线上 vs 离线")
        book.put("consistency.compared", con.get("compared"), "int", note="比对判决数")
        book.put("consistency.gap", con.get("data_gap"), "int", note="数据缺口单数")
        out["consistency"] = con
        return out, book

    # ---- 执行体 2：异动归因 ----
    def _exec_anomaly(self, parsed: dict):
        slots = parsed["slots"]
        win, prod = slots["window"], slots["product"]
        book = AnswerBook()
        mid = (slots.get("metrics") or ["approval_rate"])[0]
        m = catalog.CATALOG[mid]

        # --- 分支 A：这是带表现期的指标（FPD7 / FPD30 / MOB3）---
        if m.lag_days > 0:
            chk = catalog.check_maturity(mid, win)
            if chk["blocked"]:
                book.put("maturity.blocked", 1, "int", note="不可算")
                return {"kind": "maturity_blocked", "metric": mid,
                        "window": win.as_dict(), "maturity": chk}, book
            v = catalog.build_view(self.db, RUN_CUR, chk["window"], prod)
            res = catalog.compute(mid, v)
            book.put("metric.value", res["value"], "pct", metric_id=mid)
            book.put("metric.n", res.get("denominator_n"), "int")
            book.put("metric.granted", res.get("granted_n"), "int")
            book.put("metric.unmatured", res.get("unmatured_n"), "int")
            return {"kind": "maturity_corrected", "metric": mid,
                    "window": win.as_dict(), "maturity": chk,
                    "compute": res, "product": prod}, book

        # --- 分支 B：通过率异动（本期 vs 上期，跨版本）---
        prev_win = win.shift(-win.days)
        apps_c, facts_c = self._slice(RUN_CUR, win, prod)
        apps_p, facts_p = self._slice(RUN_PREV, prev_win, prod)
        if not apps_c or not apps_p:
            raise ValueError("本期或上期窗口内没有数据，无法做异动分析")
        rec_c = anomaly.records(apps_c, facts_c)
        rec_p = anomaly.records(apps_p, facts_p)
        attr = anomaly.attribute(facts_p, facts_c, rec_p, rec_c, target="pass")
        book.put("prev.rate", attr["period_prev"]["rate"], "pct",
                 note=f"上期 {prev_win.date_from}~{prev_win.date_to}")
        book.put("cur.rate", attr["period_cur"]["rate"], "pct")
        book.put("delta", attr["delta"]["delta"], "pct")
        book.put("prev.n", attr["period_prev"]["n"], "int")
        book.put("cur.n", attr["period_cur"]["n"], "int")
        if attr["delta"]["ci95"]:
            book.put("ci.low", attr["delta"]["ci95"][0], "pct")
            book.put("ci.high", attr["delta"]["ci95"][1], "pct")
        book.put("mix.max_effect", attr.get("mix_max_effect"), "pct",
                 note="客群结构效应（最大维度）")
        book.put("quality.max_jump", attr.get("quality_max_jump"), "pct",
                 note="缺失率最大跳变")
        if attr["lmdi"]:
            for st, c in attr["lmdi"]["contributions"].items():
                if abs(c) >= 0.0005:
                    book.put(f"lmdi.{st}", c, "pct", note=f"{st} 贡献")
            book.put("lmdi.residual", attr["lmdi"]["residual"], "float")
        # 归一化 hypotheses：证据里的数字全部登记到登记簿（叙述只引用 key）
        _FLOAT_KEYS = {"z", "share", "residual", "level_effect", "interaction",
                       "mix_effect", "profit", "score"}
        for i, h in enumerate(attr["hypotheses"], 1):
            book.put(f"hyp.{i}.score", h["score"], "float")
            for k, v in (h.get("numbers") or {}).items():
                if v is None:
                    continue
                book.put(f"hyp.{i}.{k}", v,
                         "float" if k in _FLOAT_KEYS else "pct")
        # 环节级显著性（整体不显著时，这里是唯一能给出真信号的地方）
        for t in attr.get("stage_tests", []):
            if not t["significant"]:
                continue
            book.put(f"stagetest.{t['stage']}.delta", t["delta"], "pct")
            book.put(f"stagetest.{t['stage']}.z", t["z"], "float")
            book.put(f"stagetest.{t['stage']}.n_prev", t["n_prev"], "int")
            book.put(f"stagetest.{t['stage']}.n_cur", t["n_cur"], "int")
        return {"kind": "pass_anomaly", "product": prod,
                "window": win.as_dict(), "prev_window": prev_win.as_dict(),
                "attribute": attr}, book

    # ---- 执行体 3：配置查询 ----
    def _exec_config(self, parsed: dict):
        slots = parsed["slots"]
        flow = knowledge.policy_flow(slots["product"], catalog.AS_OF)
        book = AnswerBook()
        if not flow["found"]:
            return {"kind": "config_missing", "reason": flow["reason"]}, book
        book.put("policy.policy_n", sum(len(s["policies"]) for s in flow["stages"]),
                 "int")
        book.put("policy.stage_n", len(flow["stages"]), "int")
        for i, st in enumerate(flow["stages"], 1):
            book.put(f"stage.{i}.policy_n", len(st["policies"]), "int")
        return {"kind": "config", "flow": flow,
                "history": knowledge.version_history(slots["product"])}, book

    # ---- 执行体 4：贡献排序 ----
    def _exec_contribution(self, parsed: dict):
        slots = parsed["slots"]
        win, prod = slots["window"], slots["product"]
        apps, facts = self._slice(RUN_CUR, win, prod)
        if not apps:
            raise ValueError("窗口内没有数据，无法做贡献分析")
        rk = ranking.rank(self.db, RUN_CUR, facts, apps, CFG_CUR)
        book = AnswerBook()
        book.put("rank.n", len(rk["items"]), "int")
        for it in rk["items"][:8]:
            book.put(f"item.{it['policy_id']}.score", it["score"], "float")
            book.put(f"item.{it['policy_id']}.ratio", it["ratio"] or 1.0, "float")
            # 阈值是配置读数（来自受控查询），不是算出来的 —— 但它会出现在正文里，
            # 一样要登记，否则会被溯源闸门当成幻觉数字拦掉。
            book.put(f"item.{it['policy_id']}.thr",
                     self._thr_text(it["threshold"]), "raw", note="当前阈值")
            book.put(f"item.{it['policy_id']}.rejected", it["rejected"], "int")
            book.put(f"item.{it['policy_id']}.executed", it["executed"], "int")
            book.put(f"item.{it['policy_id']}.band_rate",
                     (it.get("band") or {}).get("bad_rate"), "pct", note="边际档坏账率")
            book.put(f"item.{it['policy_id']}.band_n",
                     (it.get("band") or {}).get("n"), "int", note="边际档样本")
            book.put(f"item.{it['policy_id']}.labeled_rej",
                     it.get("labeled_rej_n"), "int", note="拒绝组有标签人数")
            if it.get("extra", {}).get("downstream", {}).get("downstream_kill_rate") is not None:
                book.put(f"item.{it['policy_id']}.kill",
                         it["extra"]["downstream"]["downstream_kill_rate"], "pct")
        return {"kind": "contribution", "product": prod, "window": win.as_dict(),
                "ranking": rk, "consistency": self._consistency()}, book

    # ---- 执行体 5：字段查询 ----
    def _exec_field(self, parsed: dict):
        f = knowledge.search_field(parsed["slots"].get("field") or "")
        book = AnswerBook()
        if f.get("found"):
            book.put("field.grain_n", 1, "int", note=f["grain"])
        return {"kind": "field", "field": f}, book

    # ---- 权限前置检查（三维：能力 / 粒度 / 范围）----
    def _access_precheck(self, parsed: dict) -> dict | None:
        """越权请求的正确产出是**一句说明**，不是一个数字。

        最恶劣的一种失败是"静默降级"：用户没权限按渠道看，系统"体贴地"
        按全量算了个数给他 —— 数是错的，而且看起来完全正常。
        """
        q = parsed.get("question") or ""
        dim = "channel" if "渠道" in q else "product"
        pol = access.policy(self.role)
        for mid in parsed["slots"].get("metrics") or []:
            chk = pol.check_metric(mid)
            if not chk.get("ok"):
                return {"kind": "denied", "error": chk, "metric_id": mid}
            chk = pol.check_dimension(mid, dim)
            if not chk.get("ok"):
                return {"kind": "denied", "error": chk, "metric_id": mid,
                        "dimension": dim}
        return None

    # ---- 执行体 6：模型级指标（V2.5，分流后的另一条路）----
    def _exec_model_diag(self, parsed: dict):
        """唯一入口是 compute_model_metric —— 日常提问与监控 job 共用它。"""
        slots = parsed["slots"]
        mid = slots.get("model_metric") or "ks"
        win = slots.get("window")
        params = {"window": win, "product": slots.get("product"),
                  "dimension": slots.get("dimension") or "overall",
                  "bins": slots.get("bins"), "label": slots.get("label")}
        if slots.get("ref_period"):
            params["ref_period"] = slots["ref_period"]
        out = model_tool.run(self.db, {"metric_id": mid, "params": params},
                             role=self.role)
        book = AnswerBook()
        if not out.get("ok"):
            book.put("model.error.code", out.get("code") or "ERROR", "raw",
                     note="错误码")
            return {"kind": "model_error", "error": out, "metric_id": mid,
                    "params": {k: (v.as_dict() if k == "window" and v else v)
                               for k, v in params.items()}}, book
        # ---- 登记：只有登记过的数字才允许出现在正文 ----
        if out.get("value") is not None:
            book.put("model.value", out["value"], "float", metric_id=mid,
                     note=out.get("name") or mid)
        book.put("model.n", out.get("n"), "int", metric_id=mid, note="样本量")
        if out.get("bad_n") is not None:
            book.put("model.bad_n", out["bad_n"], "int", metric_id=mid)
            book.put("model.good_n", out["good_n"], "int", metric_id=mid)
        if out.get("ci95"):
            book.put("model.ci_low", out["ci95"][0], "float")
            book.put("model.ci_high", out["ci95"][1], "float")
        book.put("model.bins_n", out.get("bins_n") or 0, "int", note="分箱数")
        for row in (out.get("bins") or {}).get("rows") or []:
            b = row.get("bin")
            for fld, unit in (("n", "int"), ("bad_rate", "pct"), ("woe", "float"),
                              ("iv_contrib", "float"), ("lift", "float"),
                              ("cum_ks", "float"), ("cur", "float"),
                              ("base", "float")):
                if row.get(fld) is not None:
                    book.put(f"model.bin.{b}.{fld}", row[fld], unit,
                             note=f"箱{b}.{fld}")
        if out.get("ks_max") is not None:
            book.put("model.ks_max", out["ks_max"], "float")
        if out.get("iv_total") is not None:
            book.put("model.iv_total", out["iv_total"], "float")
        # 曲线类（S0.9）：只有登记过的摘要才能出现在正文；ref_id 也登记，
        # 否则"外卸到哪"这句会因为含未登记字符串被出口闸门拦下。
        if out.get("kind") == "curve":
            sm = out.get("summary") or {}
            for k in ("ks_max", "at_score", "auc_area"):
                if sm.get(k) is not None:
                    book.put(f"curve.{k}", sm[k], "float", metric_id=mid,
                             note=f"曲线摘要.{k}")
            book.put("curve.ref", out.get("ref_id") or "-", "raw", note="逐点外卸")
            # 只有**真的抽稀过**才登记点数：没抽稀时说"99 个点抽稀为 99 个点"
            # 是噪音；抽稀过却不说，画图的人会把折线拐点当成全量拐点。
            _nr, _no = out.get("n_points"), out.get("n_points_out")
            if _nr and _no and _nr != _no:
                book.put("curve.n_points", _nr, "int", note="不同分值个数")
                book.put("curve.n_points_out", _no, "int", note="抽稀后落盘点数")
        return out, book

    # ---- 公共：切片与一致性核对 ----
    def _slice(self, run_id: str, win: catalog.Window, product: str):
        apps = [a for a in self.db.applicants()
                if win.contains(str(a.get("created_at") or "")[:10])
                and a["product_code"] == product]
        ids = {a["apply_id"] for a in apps}
        facts = [f for f in self.db.facts("run_id=?", (run_id,))
                 if f["apply_id"] in ids]
        return apps, facts

    def _consistency(self) -> dict:
        """第 0 步（V2 版）：当前线上版本重放与线上导出的判决一致率。

        ★为什么每类回答都要跑：统计结论的前提是"离线重放复刻了线上"。
          一致率不是 100% 时，任何数字都只是"离线世界的答案"。
          这里用 ONLINE_BASELINE（=v2.1 导出）比 RUN_PREV（v2.1 重放）。
        """
        from core.metrics import consistency_rate
        on = self.db.facts("run_id=?", (retro.ONLINE_RUN,))
        off = self.db.facts("run_id=?", (RUN_PREV,))
        if not on or not off:
            return {"rate": None, "note": "缺少线上判决或重放事实"}
        return consistency_rate(on, off)

    # ---------------- 节点 5：叙述（唯一"像 LLM"的地方，默认模板） ----------------
    def n_narrate(self, state: DState) -> dict:
        parsed, res, book = state["parsed"], state["result"], state["book"]
        text = self._render(parsed, res, book)
        chk = verify_numbers(text, book)
        self.db.auditor(state["session_id"]).append("answer.rendered", {
            "intent": parsed["intent"], "traceable": chk["ok"],
            "stray": chk["stray"]})
        lines = [f"[narrate] 回答 {len(text)} 字；数字溯源 "
                 f"{'✔ 全部登记' if chk['ok'] else '✘ 存在未登记数字 ' + str(chk['stray'])}"]
        return {"answer": text, "trace": lines,
                "findings": {"verify": chk}}

    def _render(self, parsed: dict, res: dict, book: dict) -> str:
        """模板叙述 —— 每个数字都从登记簿取，句子结构固定。

        生产环境这一层可以换成 LLM（只接收登记簿槽位 + 结论标签），
        但必须保留 verify_numbers 作为出口闸门。
        """
        b = AnswerBook()
        b._d = {k: dict(v) for k, v in book.items()}
        it = parsed["intent"]
        head = f"【{parsed['question']}】\n"
        # 降级交付：执行体失败时也不是白屏，而是一份"人话说明书"。
        # 这一段的存在理由：用户需要知道"是系统没算出来"，而不是"系统没理我"。
        if res.get("kind") == "degraded":
            return head + (
                "这次没能给出结果，失败在取数/计算这一步（技术信息：" + res["reason"] + "）。\n"
                "已完成的部分：识别出问题类型（" + str(res.get("intent")) +
                "），并已通过计划校验。\n"
                "可能的原因：所选时间窗口内没有申请数据、产品或窗口与数据集对不上、"
                "或该口径依赖的源表尚未接入。\n"
                "建议：换一个时间范围、或确认产品后重问；如果重复出现，"
                "把这个问题连同 trace 一起反馈给维护人。"
                "（本回答不含任何统计数字，也不做推测。）"
            ) + self._decl(parsed.get("notes"))
        if res.get("kind") == "denied":
            # 越权请求的正确产出是"一句说明"，不是一个数字（§7.6 最恶劣的失败）
            e = res["error"]
            return head + (
                f"这个问题我给不了：{e.get('message')}。\n"
                f"（错误码 {e.get('code')}；如需该范围的数据，请走权限申请流程。）"
            )
        env = self._envelope(parsed, res) + "\n"
        if it == intent.INTENT_MODEL_DIAG:
            body = self._r_model(parsed, res, b)
        elif it == intent.INTENT_STATS:
            body = self._r_stats(parsed, res, b)
        elif it == intent.INTENT_ANOMALY:
            body = self._r_anomaly(parsed, res, b)
        elif it == intent.INTENT_CONFIG:
            body = self._r_config(res, b)
        elif it == intent.INTENT_CONTRIBUTION:
            body = self._r_contribution(res, b)
        elif it == intent.INTENT_FIELD:
            body = self._r_field(res, b)
        else:
            # 能力清单式回答：不假装听懂，也不白屏。数字一律不出现，
            # 免得被出口闸门当幻觉拦下（这里确实没有任何数据可引）。
            body = ("这个问法我暂时没对应到能做的分析，可能是把业务口语说成"
                    "了别的意思。当前能稳定回答的是下面几类，换个说法就行：\n"
                    "  · 统计：「现金贷近一个月每个环节、每个策略的通过率」\n"
                    "  · 异动归因：「现金贷近一周通过率为什么下降」\n"
                    "  · 配置查询：「消费分期当前策略是什么」\n"
                    "  · 贡献排序：「现金贷近半年哪些策略节点需要调整或做 AB test」\n"
                    "  · 字段定位：「FPD30 在哪个库哪张表」")
        return head + env + body + self._decl(parsed.get("notes"))

    # ---------------- 口径信封（四类回答统一） ----------------
    def _envelope(self, parsed: dict, res: dict) -> str:
        """四要素：口径+版本 / 窗口+修正 / 过滤+行权限 / 截止。

        为什么由登记簿与注册表生成而不是让叙述层写：
        模型会把口径写成"按标准风控口径统计" —— 读起来更专业，一个字都不能核对。
        """
        it = parsed["intent"]
        scope: dict = {"as_of": catalog.AS_OF}
        prod = res.get("product") or parsed["slots"].get("product")
        pname = (knowledge.PRODUCTS.get(prod) or {}).get("name") if prod else None
        if it == intent.INTENT_STATS:
            scope.update(metrics=list(res.get("metrics") or {}),
                         window=res.get("window"), product=prod,
                         product_name=pname, dimension="全部（整体口径）",
                         row_filter=res.get("row_filter"))
        elif it == intent.INTENT_ANOMALY:
            mid = (res.get("metric")
                   or (parsed["slots"].get("metrics") or ["approval_rate"])[0])
            mt = res.get("maturity") or {}
            win = res.get("window")
            if mt.get("corrected") and mt.get("window"):
                win = mt["window"].as_dict()
            m = catalog.CATALOG.get(mid)
            scope.update(metrics=[mid], window=win,
                         corrected=bool(mt.get("corrected")),
                         lag_days=(m.lag_days or None) if m else None,
                         product=prod, product_name=pname,
                         dimension="全部（整体口径）")
        elif it == intent.INTENT_CONTRIBUTION:
            scope.update(metrics=[], window=res.get("window"), product=prod,
                         product_name=pname, dimension="全部（整体口径）")
        elif it == intent.INTENT_MODEL_DIAG:
            scope.update(model_metric=res.get("metric_id"),
                         window=res.get("window"), product=prod,
                         product_name=pname,
                         corrected=res.get("maturity_corrected"),
                         lag_days=model_metrics.LABELS.get(res.get("label")),
                         dimension=res.get("dimension") or "overall",
                         extra=res.get("envelope_extra"))
        else:      # CONFIG / FIELD：回答的是时点读数，窗口显式写"不适用"
            scope.update(metrics=[], window=None, product=prod,
                         product_name=pname, dimension="不适用")
        return envelope.render(scope)

    # ---------------- 模型级指标叙述 ----------------
    def _r_model(self, parsed: dict, res: dict, b: AnswerBook) -> str:
        """模型级回答的三个必须元素：指标名 + 参照期 + 分箱表。

        没有分箱表，读者无法判断"差距是在哪一段拉开的"；
        没有参照期，PSI 这类指标等于没定义；
        没有指标名，回答会变成"一个数字"，而数字看起来都对。
        """
        if res.get("kind") == "model_error":
            e = res.get("error") or {}
            lines = [f"⚠ 这个指标算不出来：{e.get('message')}。"]
            if e.get("reason"):
                lines.append(f"原因：{e['reason']}")
            if e.get("allowed_ref_periods"):
                lines.append("可用的基准期：" + "、".join(e["allowed_ref_periods"]))
            if e.get("code"):
                lines.append(f"（错误码 {e['code']}）")
            return "\n".join(lines)

        mid = res.get("metric_id")
        name = res.get("name") or mid
        src = res.get("source") or "MEASURED"
        if src == "MATERIALIZED":
            src_txt = (f"MATERIALIZED as_of={res.get('as_of')} ｜ job="
                       f"{res.get('job_id')} ｜ 参数 v{res.get('param_version')}"
                       f"（参数与监控任务完全一致，未重算）")
        else:
            src_txt = "MEASURED 实时计算（与监控任务调用同一个函数，只是参数不同）"

        lines = [f"■ {name}（{mid}）｜ label={res.get('label')} ｜ {src_txt}"]
        if res.get("kind") == "curve":
            # 曲线类：只报三个摘要 + 去处。**不列点** —— 列出来就是 100 行坐标，
            # 读者看不到形状，还会以为那就是结论。
            lines.append(f"摘要：ks_max = {b.fmt('curve.ks_max')}，"
                         f"最优点分数 = {b.fmt('curve.at_score')}，"
                         f"曲线面积 auc_area = {b.fmt('curve.auc_area')}"
                         f"（n={b.fmt('model.n')}）")
            if b.has("curve.n_points"):
                lines.append(f"曲线共 {b.fmt('curve.n_points')} 个不同分值，"
                             f"抽稀为 {b.fmt('curve.n_points_out')} 点存盘"
                             f"（抽稀只影响画图，不影响上面三个摘要）")
            lines.append(f"逐点曲线已外卸：{b.fmt('curve.ref')}")
            lines.append("曲线类不参与 LMDI 分解；要看形状请按上面的 ref 取文件，"
                         "不要把点贴回对话。")
            return "\n".join(lines)
        if res.get("value") is not None:
            extra = ""
            if res.get("ci95"):
                extra = (f"（95%CI {b.fmt('model.ci_low')} ~ {b.fmt('model.ci_high')}；"
                         f"n={b.fmt('model.n')}"
                         + (f"，坏 {b.fmt('model.bad_n')} / 好 {b.fmt('model.good_n')}"
                            if b.has("model.bad_n") else "") + "）")
            else:
                extra = f"（n={b.fmt('model.n')}）"
            lines.append(f"{name} = {b.fmt('model.value')}{extra}")
        else:
            lines.append(f"n={b.fmt('model.n')}（该维度下未给单一标量，见下表）")

        rows = (res.get("bins") or {}).get("rows") or []
        if rows:
            is_psi = mid == "psi"
            head = ("  箱  样本      " + ("当期占比   基准占比" if is_psi
                    else "坏账率    WOE     IV贡献   LIFT    累计KS"))
            lines.append(f"\n分箱（等频 {b.fmt('model.bins_n')} 箱）：")
            lines.append(head)
            for row in rows:
                bn = row.get("bin")
                if is_psi:
                    lines.append(f"  {bn:<3} {b.fmt(f'model.bin.{bn}.n') if b.has(f'model.bin.{bn}.n') else '-':<8} "
                                 f"{b.fmt(f'model.bin.{bn}.cur')}     "
                                 f"{b.fmt(f'model.bin.{bn}.base')}")
                else:
                    lines.append(
                        f"  {bn:<3} {b.fmt(f'model.bin.{bn}.n'):<9}"
                        f"{b.fmt(f'model.bin.{bn}.bad_rate'):<9}"
                        f"{b.fmt(f'model.bin.{bn}.woe'):<8}"
                        f"{b.fmt(f'model.bin.{bn}.iv_contrib'):<8}"
                        f"{b.fmt(f'model.bin.{bn}.lift'):<8}"
                        f"{b.fmt(f'model.bin.{bn}.cum_ks')}")
            if not is_psi:
                lines.append("  注：累计KS 是曲线的采样点；**整表只报一个 ks_max**，"
                             "KS 不是每箱一个（每箱一个 KS 没有定义）。")
        if b.has("model.ks_max"):
            lines.append(f"ks_max = {b.fmt('model.ks_max')}（全分布累计最大，不分箱）")
        if mid == "psi":
            # PSI 与 KS **共用同一套已到期借据**（分箱要算每箱坏账率/WOE），
            # 所以窗口尾部还没满表现期的借据不进分布。这件事必须写出来：
            # 不写，用户会以为"近三个月"就是 04-02~06-30 全部借据，
            # 而实际只用到 05-31 之前 —— 同一个"近三个月"，KS 的 n 和 PSI 的 n
            # 不一样，不解释就永远查不清。
            lag = model_metrics.LABELS.get(res.get("label") or "fpd30", 30)
            from datetime import date as _date, timedelta as _td
            cut = (_date.fromisoformat(catalog.AS_OF) - _td(days=lag)).isoformat()
            lines.append(f"\n注：PSI 与 KS 共用同一套「label 已满表现期」的借据"
                         f"（{res.get('label')} = {lag} 天），因此当期实际只含 "
                         f"{cut} 之前放款的借据 —— 窗口尾部未到期的借据不进分布。")
        if b.has("model.iv_total"):
            lines.append(f"IV 合计 = {b.fmt('model.iv_total')}")
        mono = res.get("monotonic") or (res.get("bins") or {}).get("monotonic")
        if mono and mono.get("monotonic") is not None:
            if mono["monotonic"]:
                lines.append("单调性：坏账率随分数单调不减（分箱方向正常）")
            else:
                brk = "、".join(str(x) for x in (mono.get("breaks") or []))
                lines.append(f"单调性：**存在破格箱**（箱 {brk} 的坏账率低于前一箱）—— "
                             f"直接调阈值前必须先查这几箱的样本量与口径")
        lines.append("\n声明：模型级指标不参与 LMDI 分解，也不能与率值混算"
                     "（KS(A∪B) ≠ KS(A)+KS(B)）。")
        if res.get("direction_warning"):
            # KS 对方向无感（CDF 最大距离），AUC 有方向。
            # 两者不一致时最危险：报表看着"有区分度"，实际排序是反的。
            lines.append("\n⚠ 方向自检未通过：坏样本分数反而更低。"
                         "KS 这类指标对方向不敏感，会掩盖这件事 —— "
                         "先确认分数是否已翻转，再谈调阈值。")
        return "\n".join(lines)

    @staticmethod
    def _decl(notes: list[str] | None) -> str:
        """把解析层"我替你用了默认值"的声明原样贴到回答末尾。

        作用：区分「用户明确要的」和「我猜的」。
        没有它会怎样 —— 本项目实测过的真实事故：
        用户问"AL001 产品的通过率"，产品码不认识 → 走兜底按【现金贷】算，
        _fill_defaults 确实往 notes 里写了"已按现金贷兜底"，但渲染层从来没读过
        notes，于是回答里一个字都没提 —— 用户会以为算的就是 AL001。
        回测 Agent 最贵的错误不是算错，是**算的是别的东西却不告诉你**。
        """
        if not notes:
            return ""
        return "\n\n■ 需要你确认的假设\n" + "\n".join(f"· {n}" for n in notes)

    def _r_stats(self, parsed, res, b: AnswerBook) -> str:
        # 口径四要素统一由 envelope 输出（原来这一行只有 3/4，缺"数据截止"）
        lines = [""]
        for mid, r in res["metrics"].items():
            m = catalog.CATALOG[mid]
            lines.append(f"\n■ {m.name}（{m.formula}）")
            if m.kind == "scalar":
                lines.append(f"  值 {b.fmt(mid + '.value')}，"
                             f"分母 {b.fmt(mid + '.n')} 单"
                             f"{'（⚠ 样本不足 30，结论仅供参考）' if r.get('sample_warning') else ''}")
            else:
                has_other = False
                for row in r["rows"]:
                    key = row.get("policy_id") or row.get("stage")
                    line = (f"  {key:<10} 通过率 {b.fmt(f'{mid}.{key}.pass_rate')}"
                            f"  拒绝率 {b.fmt(f'{mid}.{key}.reject_rate')}")
                    # 只有真存在"未知/未判定"时才提，否则表格变噪
                    if b.has(f"{mid}.{key}.other_rate") and b.get(f"{mid}.{key}.other_rate"):
                        has_other = True
                        line += f"  其余 {b.fmt(f'{mid}.{key}.other_rate')}"
                    lines.append(line)
                if has_other:      # 解释只写一次，不要每行都复读
                    lines.append("  注：其余 = 数据缺失/未判定（既不计通过也不计拒绝），"
                                 "所以通过率+拒绝率不一定等于 100%")
        con = res.get("consistency") or {}
        if con.get("rate") is not None:
            lines.append(f"\n数据可信性：线上/离线判决一致率 {b.fmt('consistency.rate')}"
                         f"（比对 {con['compared']} 条判决，缺口 {con['data_gap']} 单）")
        # 瓶颈定位：把"哪一段最堵"直接说出来（数字仍然只从登记簿取）
        st = (res["metrics"].get("stage_rates") or {}).get("rows") or []
        pol = (res["metrics"].get("policy_rates") or {}).get("rows") or []
        if st:
            worst = max(st, key=lambda r: r["reject_rate"] or 0)
            bottleneck = [r for r in pol if r["stage"] == worst["stage"]]
            bottleneck.sort(key=lambda r: -(r["reject_rate"] or 0))
            names = "、".join(f"{r['policy_id']}（拒绝率 "
                              f"{b.fmt('policy_rates.' + r['policy_id'] + '.reject_rate')}）"
                              for r in bottleneck[:2])
            # ★这里必须判空：用户只问了 FPD7 时没取策略明细，
            # 早期版本会渲染成"该环节内拒绝最多的是 。"—— 一个空名字比不写更糟，
            # 业务会以为数据丢了。
            inside = (f"该环节内拒绝最多的是 {names}。"
                      if names else "该环节的策略明细本次未取（可再问「每个策略的通过率」）。")
            lines.append(f"\n瓶颈定位：拒绝率最高的环节是 {worst['stage']}"
                         f"（{b.fmt('stage_rates.' + worst['stage'] + '.reject_rate')}），"
                         f"{inside}"
                         f"要通过率就得动这里，但能不能动取决于这些策略阈值是否还有区分度"
                         f"（见贡献排序）。")
        lines.append("\n来源等级：MEASURED（实测，线上真实发生）；"
                     f"口径版本见各指标注册表（{len(res['metrics'])} 个指标）。")
        return "\n".join(lines)

    def _r_anomaly(self, parsed, res, b: AnswerBook) -> str:
        if res["kind"] == "maturity_blocked":
            return (f"⚠ 这个口径算不出来：{res['maturity']['reason']}。\n"
                    f"建议改问【已到期批次】，或等表现期结束后再分析。")
        if res["kind"] == "maturity_corrected":
            mt = res["maturity"]
            c = res["compute"]
            lines = [f"先纠正一个口径：{mt['reason']}。",
                     f"按到期批次计算：{catalog.CATALOG[res['metric']].name} = "
                     f"{b.fmt('metric.value')}",
                     f"分母构成：放款 {b.fmt('metric.granted')} 笔 → 已到期 "
                     f"{b.fmt('metric.n')} 笔（未到期 {b.fmt('metric.unmatured')} 笔不计入）"]
            if c.get("sample_warning"):
                lines.append("⚠ 已到期样本不足 30 笔，这个数字不能用于下结论；"
                             "建议把窗口扩到 30 天以上，或按月对比两个完整批次。")
            lines.append("来源等级：MEASURED（有真实标签）；但小样本下区间很宽。")
            return "\n".join(lines)

        a = res["attribute"]
        lines = [f"对比口径：本期 = 当前版本（v2.2，2026-05-31 生效）重放；"
                 f"上期 = 上一版本（v2.1）重放 —— 即策略变更前后对比。",
                 f"结论先行：本期 {b.fmt('cur.rate')} vs 上期 {b.fmt('prev.rate')}，"
                 f"变化 {b.fmt('delta')}，95%CI "
                 f"[{b.fmt('ci.low')}, {b.fmt('ci.high')}]"
                 f"（本期 {b.fmt('cur.n')} 单 / 上期 {b.fmt('prev.n')} 单）。"]
        sig = a["delta"]["significant"]
        if sig:
            lines.append("变化在统计上显著，可以进入归因。")
        else:
            lines.append("整体变化在统计上【不显著】—— 但先别急着说'没事'，"
                         "看下面的环节级检验：整体率会被乘法稀释，环节信号可能被掩盖。")
        # 环节级显著性是"整体不显著但确有变化"时的关键证据
        sig_stages = [t for t in a.get("stage_tests", []) if t["significant"]]
        if sig_stages:
            for t in sig_stages:
                st = t["stage"]
                lines.append(f"  环节级检验：{st} 通过率变化 "
                             f"{b.fmt('stagetest.' + st + '.delta')}"
                             f"（z={b.fmt('stagetest.' + st + '.z')}，"
                             f"进入样本 {b.fmt('stagetest.' + st + '.n_prev')} → "
                             f"{b.fmt('stagetest.' + st + '.n_cur')}）—— 显著")
        else:
            lines.append("  环节级检验：没有环节达到显著，支持'无实质异动'的判断。")
        if a["lmdi"]:
            contrib = "，".join(f"{k} {b.fmt('lmdi.' + k)}"
                               for k in a["lmdi"]["contributions"]
                               if f"lmdi.{k}" in b.snapshot())
            lines.append(f"\n环节分解（LMDI，精确可加）：{contrib}；"
                         f"残差 {b.fmt('lmdi.residual')}（=0 表示分解无损）。")
        lines.append("\n假设排序（每条都是可证伪的）：")
        for i, h in enumerate(a["hypotheses"], 1):
            lines.append(f"  {i}. {h['hypothesis']}")
            lines.append(f"     证据：{h['evidence']}")
            # 证据数字一律从登记簿取（h['evidence'] 里刻意不含数字）
            nums = [f"{k}={b.fmt(f'hyp.{i}.{k}')}" for k in (h.get("numbers") or {})
                    if f"hyp.{i}.{k}" in b.snapshot()]
            if nums:
                lines.append("     数字：" + "，".join(nums))
            # share 可能大于 1（例如 1.04）：该环节自身贡献超过了净变化，
            # 差额被其他环节的反向变化抵消掉了。不解释一句，业务会当算错。
            if (h.get("numbers") or {}).get("share", 0) > 1.0:
                lines.append("     （share 大于一时：该环节自身贡献超过净变化，"
                             "差额被其他环节的反向变化抵消 —— 不是算错）")
        # 归因报告必须写"排除了什么"：只列成立的假设，读者无法判断你查过没有
        lines.append(f"\n已排查（未成立）：客群结构漂移 —— 最大维度 "
                     f"{a.get('mix_max_seg')} 的结构效应 "
                     f"{b.fmt('mix.max_effect')}，低于 0.5pt 立项阈值；"
                     f"数据质量 —— 缺失率最大跳变 {b.fmt('quality.max_jump')}，"
                     f"未达 2pt 异常线。")
        # 闭环：把"策略变更历史"对上去（这正是知识层存在的意义）
        hist = knowledge.version_history(res["product"])
        if hist:
            last = hist[-1]
            lines.append(f"\n变更历史交叉验证：{res['product']} 最近一次策略变更是 "
                         f"{last['version']}（{last['effective']} 生效，{last['note']}）。"
                         f"若归因指向策略侧且时间吻合，即可确认；"
                         f"指向客群侧则说明变更不是主因。")
        lines.append("\n来源等级：MEASURED（基于真实判决与标签）；"
                     "客群/策略归因需人工确认业务上下文。")
        return "\n".join(lines)

    def _r_config(self, res, b: AnswerBook) -> str:
        if res["kind"] == "config_missing":
            return f"查不到：{res['reason']}"
        f = res["flow"]
        lines = [f"截至 {catalog.AS_OF}，{f['product']} 生效版本为 "
                 f"{f['version']}（{f['effective']} 生效），"
                 f"共 {b.fmt('policy.stage_n')} 个环节 / "
                 f"{b.fmt('policy.policy_n')} 条策略。"]
        for i, st in enumerate(f["stages"], 1):
            lines.append(f"\n■ {st['stage']}（{b.fmt(f'stage.{i}.policy_n')} 条策略）")
            for p in st["policies"]:
                lines.append(f"  {p['policy_id']} {p['name']}：{p['threshold_text']}")
        lines.append(f"\n版本历史：")
        for h in res.get("history", []):
            lines.append(f"  {h['version']}（{h['effective']}）{h['note']}")
        lines.append("\n⚠ 策略描述必须带版本戳：以上为查询时点读数，"
                     "线上变更后需重新查询。")
        return "\n".join(lines)

    def _r_contribution(self, res, b: AnswerBook) -> str:
        rk = res["ranking"]
        # 注意：window.label 本身就是用户那句说法（"近半年"/"近一个月"），
        # 外面不要再拼"近 "，否则会变成"近 近半年"
        lines = [f"{res['window']['label']}（{res['window']['from']} ~ "
                 f"{res['window']['to']}）共评估 {b.fmt('rank.n')} 条数值型策略，"
                 f"按「风险信号 : 业务价值 : 可行性 = 5 : 3 : 2」加权排序："]
        for it in rk["items"][:6]:
            pid = it["policy_id"]
            lines.append(f"\n{it['rank']}. {pid} {it['name']}"
                         f"（{it['stage']}，阈值 "
                         f"{b.fmt(f'item.{pid}.thr') if b.has(f'item.{pid}.thr') else self._thr_text(it['threshold'])}）"
                         f" → 建议：{it['action_text']}")
            lines.append(f"   分数 {it['score']}（风险 {it['score_parts']['risk']} / "
                         f"价值 {it['score_parts']['volume']} / "
                         f"可行性 {it['score_parts']['feasibility']}）")
            band = it.get("band") or {}
            if band.get("bad_rate") is not None:
                lines.append(f"   边际档（{band['label']}）坏账率 "
                             f"{b.fmt(f'item.{pid}.band_rate')}"
                             f"（n={b.fmt(f'item.{pid}.band_n')}）；"
                             f"拒绝组有标签 {b.fmt(f'item.{pid}.labeled_rej')} 人")
            else:
                lines.append("   边际档无标签（该档当年被拒、从未放款）")
            if f"item.{pid}.kill" in b.snapshot():
                lines.append(f"   下游拦截率 {b.fmt(f'item.{pid}.kill')}"
                             f"（放宽新增人群中被下游拦掉的比例）")
            lines.append(f"   依据：{it['expected_effect']}")
        counts = rk["action_counts"]
        lines.append(f"\n动作分布：" + "，".join(
            f"{k} {v}" for k, v in counts.items() if v))
        lines.append("\n边界说明：TIGHTEN 的重放结果是实测精确值；"
                     "RELAX_AB 的坏账率【不可离线给出】，必须由 AB 实验产出标签；"
                     "所有动作均为建议，最终调整需策略分析师签字。")
        return "\n".join(lines)

    @staticmethod
    def _thr_text(t) -> str:
        """阈值展示：0~1 之间的小数按百分比说（0.55 → 55%），整数原样。

        作用：阈值是给人看的风控语言，"负债收入比 > 55%"比 "> 0.55" 好懂；
        没有它，业务读到 0.55 会先怀疑是不是小数点错了。
        """
        if t is None:
            return "无阈值"
        if isinstance(t, float) and 0 < t < 1:
            return f"{t:.0%}"
        if isinstance(t, float) and t.is_integer():
            return str(int(t))
        return str(t)

    def _r_field(self, res, b: AnswerBook) -> str:
        f = res["field"]
        if not f.get("found"):
            sug = "、".join(f.get("similar") or []) or "无"
            return (f"字段字典里没有 {f.get('query')!r}。"
                    f"相近字段：{sug}。回测暂不支持该字段取数。")
        return (f"{f['field']} → {f['database']}.{f['table']}"
                f"（粒度：{f['grain']}），口径：{f['desc']}；"
                f"负责人 {f['owner']}，更新频率 {f['update']}。")

    # ---------------- 图组装 ----------------
    def build(self):
        g = StateGraph(DState)
        g.add_node("understand", self.guard("understand")(self.n_understand))
        g.add_node("clarify", self.guard("clarify")(self.n_clarify))
        g.add_node("plan", self.guard("plan")(self.n_plan))
        g.add_node("execute", self.guard("execute")(self.n_execute))
        g.add_node("narrate", self.guard("narrate")(self.n_narrate))
        g.add_edge(START, "understand")
        g.add_conditional_edges(
            "understand", lambda s: "clarify" if s.get("pending") else "plan",
            {"clarify": "clarify", "plan": "plan"})
        g.add_conditional_edges(
            "clarify", lambda s: "clarify" if s.get("pending") else "plan",
            {"clarify": "clarify", "plan": "plan"})
        g.add_edge("plan", "execute")
        g.add_edge("execute", "narrate")
        g.add_edge("narrate", END)
        return g


# ---------------------------------------------------------------------------
# 驱动层：脚本化"用户"（真实系统里是对话窗）
# ---------------------------------------------------------------------------
SCRIPTED_ANSWERS = {
    "intent": "统计每个环节的通过率",
    "window": "近一个月",
    "product": "现金贷",
    "metric": "申请通过率",
    "branch": "模型指标（KS/AUC/PSI）",     # T-106：分流未定时的脚本化选择
    "field": "FPD30",
}

VERBOSE = True          # 全局过程输出开关（自检时关掉，免得刷屏）


def set_verbosity(v: bool) -> None:
    global VERBOSE
    VERBOSE = bool(v)


def answer_clarify(payload: dict) -> dict:
    """脚本化的用户回答（演示用）。真实系统里这是对话框的一次输入。"""
    slot = payload.get("slot")
    return {"kind": "clarify_answer", slot: SCRIPTED_ANSWERS.get(slot, "近一个月")}


def ask(engine: DialogEngine, graph, question: str, sid: str | None = None,
        verbose: bool | None = None) -> dict:
    """问一个问题，自动处理澄清轮次（最多 3 轮）。

    verbose=None 表示"跟随全局开关"（set_verbosity 控制）：自检里关掉，
    演示里打开 —— 这样自检输出只剩 ✓/✗，出问题才看得清是哪一条挂了。
    """
    if verbose is None:
        verbose = VERBOSE
    sid = sid or f"Q-{int(time.time() * 1000) % 100000}"
    cfg = {"configurable": {"thread_id": sid}}
    out = graph.invoke({"session_id": sid, "question": question,
                        "role": getattr(engine, "role", access.DEFAULT_ROLE),
                        "clarify_round": 0, "trace": [], "findings": {}}, cfg)
    rounds = 0
    while "__interrupt__" in out and rounds < 3:
        rounds += 1
        payload = out["__interrupt__"][0].value
        if verbose:
            print(f"\n  🤖 Agent 反问：{payload.get('question')}")
            print(f"  👤 用户回答：{SCRIPTED_ANSWERS.get(payload.get('slot'))}")
        out = graph.invoke(Command(resume=answer_clarify(payload)), cfg)
    if verbose:
        for line in out.get("trace", []):
            print("  " + line)
    # 记一轮历史（S3 才用它做上下文；现在只落着，不参与任何计算）
    engine.memory.append(sid, {"q": question,
                               "intent": (out.get("parsed") or {}).get("intent"),
                               "answer": out.get("answer")})
    return out


# ---------------------------------------------------------------------------
# 数据准备
# ---------------------------------------------------------------------------
def prepare(db_path: Path = DB_PATH, n_population: int = 2000,
            keep: bool = False) -> Database:
    """建库：申请档 + 快照 + 放款台账 + 两个配置版本的重放事实。"""
    if db_path.exists() and not keep:
        db_path.unlink()
    db = Database(db_path)
    rows = retro.gen_population(n_population, seed=2026)
    retro.seed(db, rows)                      # 含 ONLINE_BASELINE（v2.1 导出）
    n_loans = knowledge.ensure_loan_book(db, rows, catalog.AS_OF)
    retro.run_offline(db, RUN_PREV, "v2.1", CFG_PREV)
    retro.run_offline(db, RUN_CUR, "v2.2", CFG_CUR)
    # V2.5：模型分（确定性生成，不消耗 V1 的 rng）+ 定时任务物化
    # S0.9：同时写策略分（来自 CUR 版本的策略事实），供 score_source=policy 对照
    n_score = model_metrics.ensure_model_score(db, rows, catalog.AS_OF,
                                               run_id=RUN_CUR)
    jobs = sched.run_all(db, catalog.AS_OF)
    ok_jobs = sum(1 for j in jobs if j.get("ok"))
    print(f"  数据准备：申请 {len(rows)} 单 / 放款台账 {n_loans} 笔 / "
          f"模型分 {n_score} 条 / 物化任务 {ok_jobs}/{len(jobs)} / "
          f"线上版本 {RUN_PREV} + {RUN_CUR}")
    return db


# ---------------------------------------------------------------------------
# 示范问题（覆盖四类意图 + 一次口径陷阱 + 一次澄清交互）
# ---------------------------------------------------------------------------
DEMO_QUESTIONS = [
    "现金贷近一个月，每个环节、每个策略的通过率和拒绝率统计一下",
    "现金贷近1周 FPD7 升高了，是什么原因？",
    "现金贷近一个月 FPD7 是多少？",
    "现金贷最近三个月通过率下降的原因是什么？",
    "现金贷当前线上策略是什么？",
    "统计现金贷当前策略里所有策略节点的贡献，哪些需要调整或进一步 AB test？",
    "现金贷策略的通过率太低了，帮我回测分析一下",      # ← 缺时间范围，触发反问
    # ---- V2.5：模型级指标（分流后走另一条路，回答里不许出现率值）----
    "现金贷近三个月的 KS 是多少",
    "现金贷的 PSI 有没有漂移",
    "模型的 AUC 现在多少",
]


def main() -> None:
    args = sys.argv[1:]
    if not ART.exists():
        ART.mkdir(parents=True)
    print("策略回测分析 Agent · 企业版 V2（对话层 + 语义层）")
    print("图：understand → clarify(interrupt) → plan → execute → narrate\n")
    print("① 准备数据 …")
    db = prepare()
    engine = DialogEngine(db)
    graph = engine.build().compile(checkpointer=InMemorySaver())

    questions = DEMO_QUESTIONS
    if "--ask" in args:
        questions = [args[args.index("--ask") + 1]]

    print(f"\n② 依次回答 {len(questions)} 个问题\n" + "─" * 78)
    for i, q in enumerate(questions, 1):
        print(f"\n【问 {i}】{q}")
        out = ask(engine, graph, q)
        print("\n" + "─" * 78)
        print(out.get("answer", "（无回答）"))
        print("─" * 78)
    db.close()


if __name__ == "__main__":
    main()
