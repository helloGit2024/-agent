# -*- coding: utf-8 -*-
"""记忆层（V2.5 §6 L5）：短期会话 + 长期偏好，硬规则是"只填默认槽位"。

════════════════════════════════════════════════════════════════════
记忆是唯一一个"越有用越危险"的模块
════════════════════════════════════════════════════════════════════
它的价值在"别让我每次都说一遍是现金贷"；它的风险在**同一句话会算出
不同的数**——而这是本方案唯一不能让步的东西（可复现性优先于灵活性，§16.2）。

所以三条硬规则，代码里必须能看到它们被强制执行，而不是写在文档里：
  ① 记忆只填**缺失**的槽位。用户明说的，记忆不许覆盖。
  ② 记忆永不进答案：不进 AnswerBook、不进信封。信封里的产品/窗口必须
     来自本次解析结果，否则"口径可核对"就成了"记忆可核对"。
  ③ **口径相关字段禁止由记忆提供**：metric_id / model_metric / label /
     ref_period / bins / dimension。
     反例：上次你看的是 FPD30，这次问"逾期率"，记忆把 label 填成 fpd30 ——
     数算出来了、信封齐全、完全无法察觉这次问的其实是 FPD7。

════════════════════════════════════════════════════════════════════
现阶段只做接口，不接 LangMem / Zep
════════════════════════════════════════════════════════════════════
S3 才换 PG checkpointer + 长期记忆。现在把形状定下来，是因为
"记忆能改哪些字段"这件事必须在接入之前就写死 —— 否则接入那天一定会
有人顺手让记忆填 metric_id（因为"这样用户体验更好"）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 记忆**允许**填的槽位：只影响"看哪一片数据"，不影响"算什么口径"
FILLABLE = ("product", "window")
# 记忆**永远不许**碰的字段：碰了就是口径被记忆改写
FORBIDDEN = ("metric_id", "model_metric", "label", "ref_period", "bins",
             "dimension", "metrics")


# ---------------------------------------------------------------------------
# 短期：会话状态（S3 换 PostgresSaver，接口不变）
# ---------------------------------------------------------------------------
class ShortTerm:
    """一轮之内的上下文。默认进程内，进程重启即失效（这是特性不是缺陷）。

    为什么失效是特性：S3 之前，"能续接"意味着"有状态跨进程"，而跨进程状态
    一旦被写脏，复现性就没了 —— 在接入真正的持久化之前，宁可不续接。
    """

    def __init__(self) -> None:
        self._turns: dict[str, list[dict]] = {}

    def append(self, sid: str, turn: dict) -> None:
        self._turns.setdefault(sid, []).append(turn)
        # 只留最近 6 轮：上下文越长，越容易把"上一题的口径"带进这一题
        self._turns[sid] = self._turns[sid][-6:]

    def history(self, sid: str) -> list[dict]:
        return list(self._turns.get(sid) or [])

    def clear(self, sid: str) -> None:
        self._turns.pop(sid, None)


# ---------------------------------------------------------------------------
# 长期：偏好（"默认看现金贷"这类）
# ---------------------------------------------------------------------------
@dataclass
class LongTerm:
    """偏好存储。键必须是 FILLABLE 里的字段，值必须过校验（下面 _valid）。"""
    prefs: dict[str, dict] = field(default_factory=dict)

    def remember(self, role: str, key: str, value: str) -> dict:
        if key in FORBIDDEN:
            return {"ok": False, "code": "NOT_APPLICABLE",
                    "reason": f"{key} 属于口径字段，禁止由记忆提供"}
        if key not in FILLABLE:
            return {"ok": False, "code": "NOT_REGISTERED", "allowed": list(FILLABLE)}
        if not _valid(key, value):
            return {"ok": False, "code": "PARAM_OUT_OF_DOMAIN",
                    "param": key, "value": value}
        self.prefs.setdefault(role, {})[key] = value
        return {"ok": True, "role": role, "key": key, "value": value}

    def of(self, role: str) -> dict:
        return dict(self.prefs.get(role) or {})


def _valid(key: str, value: str) -> bool:
    """只接受**能解析成真实枚举值**的偏好，避免把记忆写成第二个语义层。"""
    from core import catalog, knowledge
    if key == "product":
        return value in knowledge.PRODUCTS
    if key == "window":
        return catalog.parse_window(value) is not None
    return False


# ---------------------------------------------------------------------------
# 唯一入口：把偏好灌进解析结果（只补空缺）
# ---------------------------------------------------------------------------
def apply(parsed: dict, prefs: dict) -> tuple[dict, list[str]]:
    """返回 (新 parsed, 实际用上的记忆键)。

    第二个返回值要进回答的"需要你确认的假设"——记忆生效了就必须说，
    否则用户看到的是一个他没说过、但确实影响结果的默认值。
    """
    out = dict(parsed or {})
    slots = dict(out.get("slots") or {})
    used: list[str] = []

    for key in FILLABLE:
        if key not in prefs:
            continue
        if key in FORBIDDEN:                      # 防御性：prefs 可能是外部灌进来的
            continue
        if slots.get(key) in (None, "", [], ()):
            slots[key] = prefs[key]
            used.append(key)
    out["slots"] = slots
    if used:
        notes = list(out.get("notes") or [])
        notes.append("记忆补全：" + "、".join(used) + "（未覆盖你本次说的内容）")
        out["notes"] = notes
    return out, used


def describe() -> dict:
    return {"fillable": list(FILLABLE), "forbidden": list(FORBIDDEN),
            "note": "记忆只填默认槽位；口径字段禁止由记忆提供"}
