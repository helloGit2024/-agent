#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""10 题 LLM 版 vs 规则版计划对照（S1 T-208，只展示差异，不评分）。

直接调 plan_llm，而不走 llm.plan 的 D2 回退：否则 LLM 失败时表里会出现
一份看似正常的"LLM 计划"，实际上仍是规则版。所有真实调用仍经统一出口，
并写入内存库的 llm_call_log；审计失败不向普通 LLM 失败降级。

跑法：python eval/compare_plans.py
退出码：调用全部完成为 0（有计划差异也为 0）；任一 LLM 调用失败为 1。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import access, intent  # noqa: E402
from core.db import Database  # noqa: E402
from llm.call import LLMUnavailable  # noqa: E402
from llm.planner import PlanRejected, plan_llm  # noqa: E402
from v2_dialogue import DEMO_QUESTIONS  # noqa: E402

_ABSENT = object()


def _plain(value):
    """把 Window 等语义对象转成可比较、可打印的 JSON 值。"""
    if hasattr(value, "as_dict"):
        return _plain(value.as_dict())
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def comparable_plan(plan: dict) -> dict:
    """只保留语义计划；trigger/source/plan_meta 是路径元数据，不参与差异。"""
    slots = plan.get("slots") or {}
    out = {"intent": plan.get("intent"),
           "missing": _plain(plan.get("missing") or []),
           "notes": _plain(plan.get("notes") or [])}
    out.update({f"slots.{k}": _plain(v) for k, v in slots.items()})
    return out


def diff_plans(rules: dict, llm_plan: dict) -> list[dict]:
    """逐字段列出两边值；不做同义改写、容差或好坏评分。"""
    left, right = comparable_plan(rules), comparable_plan(llm_plan)
    return [{"field": key,
             "rules": left.get(key, _ABSENT),
             "llm": right.get(key, _ABSENT)}
            for key in sorted(set(left) | set(right))
            if left.get(key, _ABSENT) != right.get(key, _ABSENT)]


def compare(questions=DEMO_QUESTIONS, provider=None) -> list[dict]:
    rows = []
    db = Database(":memory:")
    try:
        for i, question in enumerate(questions, 1):
            rules = intent.parse(question)
            row = {"index": i, "question": question, "rules": rules,
                   "llm": None, "error": None, "differences": []}
            try:
                llm_plan = plan_llm(question, role=access.DEFAULT_ROLE,
                                    seed=0, provider=provider,
                                    audit_db=db, session_id=f"T208-{i:02d}")
                row["llm"] = llm_plan
                row["differences"] = diff_plans(rules, llm_plan)
            except (LLMUnavailable, PlanRejected) as e:
                # 失败只如实入表；绝不在这里调用规则版冒充 LLM 结果。
                reason = getattr(e, "reason", None) or "plan_rejected"
                row["error"] = f"{type(e).__name__}({reason}): {e}"
            rows.append(row)
    finally:
        db.close()
    return rows


def _cell(value) -> str:
    if value is _ABSENT:
        text = "〈未提供〉"
    elif value is None:
        text = "—"
    elif isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                          sort_keys=True)
    return text.replace("|", "\\|").replace("\n", "<br>")


def render(rows: list[dict]) -> str:
    lines = ["LLM 版 vs 规则版计划对照（只展示差异，不评分）",
             "| # | 问题 | 字段 | 规则版 | LLM 版 |",
             "|---:|---|---|---|---|"]
    same = failed = 0
    for row in rows:
        prefix = [str(row["index"]), _cell(row["question"])]
        if row["error"]:
            failed += 1
            lines.append("| " + " | ".join(prefix + ["〈调用失败〉", "—",
                                                    _cell(row["error"])]) + " |")
            continue
        diffs = row["differences"]
        if not diffs:
            same += 1
            lines.append("| " + " | ".join(prefix + ["〈无差异〉", "—", "—"]) + " |")
            continue
        for j, diff in enumerate(diffs):
            lead = prefix if j == 0 else ["", ""]
            lines.append("| " + " | ".join(lead + [diff["field"],
                                                    _cell(diff["rules"]),
                                                    _cell(diff["llm"])]) + " |")
    different = len(rows) - same - failed
    lines.append(f"\n共 {len(rows)} 题：{same} 题无差异，{different} 题有差异，"
                 f"{failed} 题调用失败。差异仅供人工核对，不构成评分。")
    return "\n".join(lines)


def main() -> int:
    if len(sys.argv) != 1:
        print("用法：python eval/compare_plans.py")
        return 2
    rows = compare()
    print(render(rows))
    return 1 if any(row["error"] for row in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
