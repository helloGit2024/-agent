# -*- coding: utf-8 -*-
"""闸门 ④：全局预算 + 熔断（S1 T-205）。

════════════════════════════════════════════════════════════════════
为什么是进程级的，而不是每题一个
════════════════════════════════════════════════════════════════════
预算的意义不是"每题给 8 次 LLM 调用额度" —— 额度用完可以再造一个
Session 继续刷，那预算就是摆设。

预算是**整个进程的开销盖帽**：不管有多少题，累计 LLM 调用上限 8 次
（FR-502）；连续 3 次失败熔断（D4）。一旦熔断，接下来所有 LLM 调用
直接返回 False，让编排层走 D4（"已转人工"模板），不试也不重试。

调用链：
  DialogEngine.__init__ → 持有 Admission（默认单例）
  ask() 入口 → admission.is_open() 为 False → 直接返回 degrade=D4
  llm.plan() / llm.narrate() → admission.acquire() 先过预算闸
                              → 成功后 admission.record("ok"/"fail")

两条闸各管各的：
  · 预算耗尽 → D2（plan() 里回退规则版）
  · 熔断     → D4（ask() 入口就拦住，整轮跳过编排）
"""
from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# 进程内单例：默认共享，测试/影子模式可覆盖
# ---------------------------------------------------------------------------
_DEFAULT: "Admission | None" = None


def default() -> "Admission":
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Admission()
    return _DEFAULT


class _Singleton:
    """测试辅助：fixture 覆盖默认 Admission，用完恢复。"""

    def __init__(self, ad: "Admission"):
        self._ad = ad
        self._prev = None

    def __enter__(self):
        global _DEFAULT
        self._prev = _DEFAULT
        _DEFAULT = self._ad
        return self._ad

    def __exit__(self, *_):
        global _DEFAULT
        _DEFAULT = self._prev


# ---------------------------------------------------------------------------
# 闸门本体
# ---------------------------------------------------------------------------
@dataclass
class Admission:
    """全局预算 + 熔断。两个维度，互不干扰。

    max_calls：累计 LLM 调用上限（FR-502 = 8）。acquire() 超过 → D2。
    consecutive_fail_limit：连续失败阈值（D4 = 3）。record("fail") 达阈值 → 熔断。
    circuit_open：熔断后 True；所有 acquire() 直接 False，直到 reset()。
    """
    max_calls: int = 8
    consecutive_fail_limit: int = 3

    # 运行时状态（__post_init__ 初始化）
    calls_made: int = 0
    consecutive_failures: int = 0
    circuit_open: bool = False
    last_reason: str = ""
    # 每次调用结果，审计用（T-207 落库时原样写）
    history: list[dict] = field(default_factory=list)

    # ------------------------------------------------------------------
    # 入口闸：返回 True = 这次可以调 LLM。False 即"预算耗尽 / 熔断"。
    # ------------------------------------------------------------------
    def acquire(self) -> bool:
        if self.circuit_open:
            self.last_reason = "circuit"
            return False
        if self.calls_made >= self.max_calls:
            self.last_reason = "budget"
            return False
        return True

    # ------------------------------------------------------------------
    # 出口闸：记录一次 LLM 调用结果（成功/失败）。
    #   成功 → consecutive_failures 归零；
    #   失败 → 累计，达阈值熔断；
    #   预算耗尽不算熔断（D2 走了规则版 = 没有调用 LLM = 不计入 consecutive_failures）。
    # ------------------------------------------------------------------
    def record(self, ok: bool, reason: str = "") -> None:
        self.calls_made += 1
        self.history.append({"ok": bool(ok), "reason": reason,
                             "n": len(self.history)})
        if ok:
            self.consecutive_failures = 0
        else:
            self.consecutive_failures += 1
            if self.consecutive_failures >= self.consecutive_fail_limit:
                self.circuit_open = True
                self.last_reason = "circuit"

    def reset(self) -> None:
        self.calls_made = 0
        self.consecutive_failures = 0
        self.circuit_open = False
        self.last_reason = ""
        self.history.clear()

    # ------------------------------------------------------------------
    def as_dict(self) -> dict:
        return {"calls_made": self.calls_made, "max_calls": self.max_calls,
                "consecutive_failures": self.consecutive_failures,
                "consecutive_fail_limit": self.consecutive_fail_limit,
                "circuit_open": self.circuit_open,
                "last_reason": self.last_reason}
