# -*- coding: utf-8 -*-
"""LLM 的两个落点（V2.5 §6）：默认关闭，关闭时行为与规则版**逐位一致**。

════════════════════════════════════════════════════════════════════
LLM 只出现在两个位置，且两个位置都不许碰数字
════════════════════════════════════════════════════════════════════
  LLM #1（理解层）：输出是一张**计划 JSON**，不是答案。
  LLM #2（表达层）：输入是**已经算好的槽位**，输出是句子；
                    出口由 verify_numbers 把关，出现未登记数字立刻回退模板。

"不许碰数字"不是洁癖：数字经手 LLM 之后，正确性就从"可证明"变成"大概率"。
风控报告没有"大概率"这个档。

════════════════════════════════════════════════════════════════════
为什么 S0 阶段必须"LLM 默认关闭"
════════════════════════════════════════════════════════════════════
先证明"加了抽象层之后行为一位没变"，再接模型。否则一旦四道题退步，
你分不清是抽象写错了还是模型不行 —— 这是所有"给已有系统接 LLM"的
工程里最常见的事故：一次性改两件事，出问题无法归因。

关闭时的语义不是"报错"，是"什么都不发生"：
  · plan()     直接调用注入的规则版 fallback，返回值里 source="rules"
  · narrate()  直接返回模板文本，返回值里 source="template"
所以接入成本 = 改一个开关，回滚成本 = 改回来。

════════════════════════════════════════════════════════════════════
这个文件在 S1 之前只做三件事
════════════════════════════════════════════════════════════════════
① 把"LLM 是否启用"收敛成**一个**开关（不许散落在各处的 if）
② 定义计划 JSON 的**校验**（validate_plan）—— 这是 L2 治理层的地基，
   它按 tool schema 校验，所以"模型无法调用它没有的能力"这条才成立
③ 定义预算（Budget）：单轮 token / 调用次数 / 每题成本上限，超限走降级
"""
from __future__ import annotations

import os
import zlib
from dataclasses import dataclass

from core import semantic_contract
from llm.call import LLMUnavailable

# 固定版本，升级走"影子模式"（§16.1 第一条）：新模型与旧模型并行跑 140 用例
# None = 跟随 llm/planner.PLAN_MODEL（版本号只有一处，别抄第二份）
MODEL: str | None = None
ENV_FLAG = "BTA_LLM"            # off（默认）/ on

_ENABLED = os.environ.get(ENV_FLAG, "off").lower() in ("1", "on", "true", "yes")


# ---------------------------------------------------------------------------
# 开关
# ---------------------------------------------------------------------------
def enabled() -> bool:
    return _ENABLED


def set_enabled(flag: bool, model: str | None = None) -> None:
    """打开/关闭 LLM。测试与影子模式用；生产走环境变量或配置中心。"""
    global _ENABLED, MODEL
    _ENABLED = bool(flag)
    if model:
        MODEL = model


# ---------------------------------------------------------------------------
# 预算
# ---------------------------------------------------------------------------
@dataclass
class Budget:
    """单轮预算。超限的唯一合法动作是**降级到规则版**，不是"再试一次"。"""
    max_calls: int = 3             # 一次问答最多调几次 LLM（规划/澄清/叙述）
    max_tokens: int = 8000         # 单轮 token 上限
    max_cost_cny: float = 0.1      # 每题成本上限（§16.1 成本失控）
    calls: int = 0
    tokens: int = 0
    cost_cny: float = 0.0

    def spend(self, tokens: int = 0, cost: float = 0.0) -> None:
        self.calls += 1
        self.tokens += tokens
        self.cost_cny += cost

    def exhausted(self) -> bool:
        return (self.calls >= self.max_calls or self.tokens >= self.max_tokens
                or self.cost_cny >= self.max_cost_cny)

    def as_dict(self) -> dict:
        return {"calls": self.calls, "tokens": self.tokens,
                "cost_cny": round(self.cost_cny, 6),
                "exhausted": self.exhausted()}


# ---------------------------------------------------------------------------
# LLM #1：规划
# ---------------------------------------------------------------------------
def plan(question: str, branch: str, fallback, role: str | None = None,
         budget: Budget | None = None, session_id: str | None = None,
         provider=None, model: str | None = None) -> dict:
    """返回一份计划。关闭时 = 规则版结果（逐位一致，这是 S0 的验收方式）。

    fallback 必须是**无参可调用**，且内部就是规则版 intent.parse + 计划生成。
    这样"关闭 LLM"不产生任何额外分支：走的是同一条路。

    打开时：LLM 规划失败（超时 / schema 两次不合规 / 跨册引用）一律 D2 ——
    **回退规则版并打标记**，不抛异常、不给替代计划。降级是编排层能看见的
    一次事件（degrade="D2" + degraded_reason），不是一次静默重试。
    """
    budget = budget or Budget()
    if not _ENABLED:
        out = dict(fallback())
        out.setdefault("source", "rules")
        return out
    if budget.exhausted():
        return _d2(fallback, "budget")
    try:
        from llm.planner import PLAN_MODEL, PlanRejected, plan_llm
        return plan_llm(question, role=role, budget=budget,
                        seed=_seed(session_id), provider=provider,
                        model=model or MODEL or PLAN_MODEL)
    except (LLMUnavailable, PlanRejected) as e:
        return _d2(fallback, getattr(e, "reason", None) or "plan_rejected")


def _seed(session_id: str | None) -> int | None:
    """会话级种子：同一会话可复现。用 crc32 而不是 hash() —— 后者每个进程加盐。"""
    return None if not session_id else zlib.crc32(session_id.encode("utf-8"))


def _d2(fallback, reason: str) -> dict:
    """D2：规划失败回退规则版。标记必须带出去，否则降级等于没发生。"""
    out = dict(fallback())
    out["source"] = "rules"
    out["degrade"] = "D2"
    out["degraded_reason"] = reason
    return out


# ---------------------------------------------------------------------------
# LLM #2：叙述
# ---------------------------------------------------------------------------
def narrate(slots: dict, fallback, budget: Budget | None = None) -> dict:
    """槽位 → 句子。关闭时 = 模板文本。

    注意签名里**只有 slots**，没有 db、没有 metric_id：
    LLM #2 拿不到取数能力，所以它无法"顺手再算一个数"。
    """
    budget = budget or Budget()
    if not _ENABLED:
        return {"text": fallback(), "source": "template"}
    if budget.exhausted():
        return {"text": fallback(), "source": "template",
                "degraded_reason": "budget"}
    raise RuntimeError(f"S1 才实现：用 {MODEL} 叙述")


# ---------------------------------------------------------------------------
# 计划校验：L2 治理层的第一道闸
# ---------------------------------------------------------------------------
ALLOWED_STEPS = ("understand", "clarify", "consistency_gate", "semantic_compute",
                 "model_compute", "anomaly_attribute", "contribution", "narrate")
ALLOWED_INTENTS = ("STATS", "ANOMALY", "CONTRIBUTION", "CONFIG", "FIELD",
                   "MODEL_DIAG", "UNKNOWN")


def validate_plan(plan_json: dict, role: str | None = None) -> dict:
    """按 tool schema 校验计划。不合法就**拒绝执行**，不"尽量理解"。

    三件必查：
      ① 有没有"怎么算"的键（自拼公式的入口）；
      ② steps 是否都在枚举内（模型自己发明的步骤不许执行）；
      ③ 工具入参是否符合该分支的 schema（两本册子互斥：问 KS 时不该有
         compute_metric 这一步）。
    """
    bad: list[str] = []
    if not isinstance(plan_json, dict):
        return {"ok": False, "bad": ["计划不是 JSON 对象"]}

    free = semantic_contract.scan_free_form(plan_json)
    if free:
        bad.append(f"计划里出现自由字段: {free}")

    steps = plan_json.get("steps") or []
    unknown = [s for s in steps if s not in ALLOWED_STEPS]
    if unknown:
        bad.append(f"未知步骤: {unknown}")

    intent = plan_json.get("intent")
    if intent and intent not in ALLOWED_INTENTS:
        bad.append(f"未知意图: {intent}")

    # 分支互斥：模型级问题不许出现 compute_metric，反之亦然
    names = {s.get("tool") for s in plan_json.get("tool_calls") or [] if isinstance(s, dict)}
    if intent == "MODEL_DIAG" and "compute_metric" in names:
        bad.append("MODEL_DIAG 分支出现 compute_metric（两本册子必须互斥）")
    if intent and intent != "MODEL_DIAG" and "compute_model_metric" in names:
        bad.append(f"{intent} 分支出现 compute_model_metric（答非所问）")

    # 入参键必须落在该工具的 key 白名单内
    allowed = {"product", "window", "dimension", "bins", "label", "ref_period",
               "metric_id", "params"}
    for call in plan_json.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        extra = set(call.get("args") or {}) - allowed
        if extra:
            bad.append(f"工具入参越界: {sorted(extra)}")

    return {"ok": not bad, "bad": bad}


def describe() -> dict:
    """给运维看的一行状态（不许包含任何密钥/地址）。"""
    return {"enabled": _ENABLED, "model": _model_name(),
            "env_flag": f"{ENV_FLAG}={os.environ.get(ENV_FLAG, 'off')}"}


def _model_name() -> str:
    if MODEL:
        return MODEL
    try:
        from llm.planner import PLAN_MODEL
        return PLAN_MODEL
    except Exception:                                       # noqa: BLE001
        return "unset"
