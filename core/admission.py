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
import hashlib

from . import access, catalog, model_metrics


_DENIAL_MESSAGE = "请求未通过准入校验，已转人工处理。"


def _denied() -> dict:
    """对外只给同一句话，不泄露 token/角色/租户是否存在。"""
    return {"ok": False, "code": "DENIED", "degrade": "D4",
            "message": _DENIAL_MESSAGE, "answer": _DENIAL_MESSAGE}


def _token_key(token: str) -> str:
    """只存不可逆指纹；token 原文不进对象、history 或审计字段。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _compiled_metrics(meta: dict, policy: access.AccessPolicy) -> tuple[list, dict]:
    """把角色权限与指标自身 access_policy 求交，且两本册子分别编译。"""
    ids, dimensions = [], {}
    for metric_id, definition in meta.items():
        if not policy.check_metric(metric_id).get("ok"):
            continue
        # 策略册声明了 access_policy；模型册当前没有该字段，缺省即无额外限制。
        metric_policy = getattr(definition, "access_policy", ())
        if metric_policy and policy.role not in metric_policy:
            continue
        ids.append(metric_id)
        declared = getattr(definition, "dimensions", None)
        if declared is None:
            declared = definition.comparable_dimensions
        dimensions[metric_id] = ["overall"] + [
            dim for dim in declared
            if policy.check_dimension(metric_id, dim).get("ok")]
    return sorted(ids), dimensions


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
    # sha256(token) -> 服务端登记的身份。请求只能给 token，不能自报角色/scope。
    _identities: dict[str, dict[str, str]] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------
    # 闸门 ①：token → 已注册角色/租户 → 编译后的 scope（T-206）。
    # 这里只实现外部协议入口要调用的准入原语，不给现有本地 CLI 塞默认 token；
    # 默认身份表为空，因此未接入身份配置时一律拒绝，不会悄悄退回 DEFAULT_ROLE。
    # ------------------------------------------------------------------
    def register_token(self, token: str, *, role: str,
                       tenant: str = "default") -> None:
        """登记服务端身份；原始 token 只用于本次哈希，不保存、不返回。"""
        if not isinstance(token, str) or not token.strip():
            raise ValueError("token 不能为空")
        if role not in access.ROLES:
            raise ValueError("身份配置含未知角色")
        if not isinstance(tenant, str) or not tenant.strip():
            raise ValueError("tenant 不能为空")
        self._identities[_token_key(token.strip())] = {
            "role": role, "tenant": tenant.strip()}

    def authorize(self, token: str | None) -> dict:
        """认证并编译权限；任何身份异常都返回同一个 DENIED，答案零数字。"""
        if not isinstance(token, str) or not token.strip():
            return _denied()
        identity = self._identities.get(_token_key(token.strip()))
        if not identity:
            return _denied()
        role, tenant = identity.get("role"), identity.get("tenant")
        # 配置可能被绕过 register_token 注入，所以调用时再判一次（fail-closed）。
        if role not in access.ROLES or not isinstance(tenant, str) or not tenant.strip():
            return _denied()
        policy = access.policy(role)
        aggregate_ids, aggregate_dims = _compiled_metrics(catalog.CATALOG, policy)
        model_ids, model_dims = _compiled_metrics(model_metrics.MODEL_CATALOG, policy)
        scope = {
            "tenant": tenant.strip(),
            "row_filter": {key: list(values)
                           for key, values in policy.row_filter.items()},
            "aggregate": {"metrics": aggregate_ids, "dimensions": aggregate_dims},
            "model": {"metrics": model_ids, "dimensions": model_dims},
        }
        return {"ok": True, "role": role, "scope": scope}

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
