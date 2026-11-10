#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CI 门禁：把"唯一实现"从纪律变成构建失败（V2.5 §8.3）。

════════════════════════════════════════════════════════════════════
要拦的不是"恶意绕开"，而是"好心人为了快一点，直接调原语"
════════════════════════════════════════════════════════════════════
这种改动在任何一次 code review 里都显得无害，甚至值得表扬：
    "这里不用绕一圈 compute_model_metric，直接 ks_statistic 就行"
而它带来的后果是：口径漂移从原语外那 20 行（五态/label/表现期/ties/切点）
悄悄开始，半年后"报告和 Agent 说的不一样"，没人能判断哪个对。

════════════════════════════════════════════════════════════════════
两道门禁的边界（为什么不是"全库禁止"）
════════════════════════════════════════════════════════════════════
V1 自己就合法使用 core.metrics（v1_backtest 的策略诊断 KS、anomaly 的 LMDI、
ranking 的方向判定）—— 那是 V1 的原语使用者，不在本次收口范围。
V2.5 要收口的是**新增的模型级计算面**：
  门禁 1：core/stats.py（新原语）只允许 core/model_metrics.py import；
          V2 层（对话/工具/调度/评估/语义层）不许直接调 KS/分箱/AUC/PSI 原语。
  门禁 2：scheduler/ 只准调 compute_model_metric / catalog.compute。

跑法：python3 ci/check_imports.py   （exit 0 = 通过）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ALLOW_NEW_PRIMITIVES = {"core/model_metrics.py"}
# V1 的原语使用者：它们用的是 V1 的口径（策略变量诊断），不是模型级指标
V1_USERS = {"v1_backtest.py", "selftest.py", "evaluation_langsmith.py",
            "core/anomaly.py", "core/ranking.py", "core/analysis.py"}

# V2 新增/改造出的面：这些文件里出现"自己算模型级指标"就是分叉
V2_SURFACE = ("v2_dialogue.py", "tools/", "scheduler/", "eval/", "ci/",
              "llm/", "memory/",
              "core/catalog.py", "core/semantic_contract.py", "core/access.py",
              "core/entities.py", "core/envelope.py", "core/model_metrics.py")

MODEL_PRIMITIVES = ("ks_statistic", "binstats", "threshold_sensitivity",
                    "direction_judgement", "auc(", "psi(")


def _files() -> list[Path]:
    return [p for p in ROOT.rglob("*.py")
            if "__pycache__" not in p.parts and "test" not in p.name.lower()]


def _rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


def _in_surface(rel: str) -> bool:
    return any(rel == s or rel.startswith(s) for s in V2_SURFACE)


def gate1() -> list[str]:
    """新原语与模型级原语：只有 model_metrics 能碰。"""
    bad = []
    for p in _files():
        rel = _rel(p)
        if rel in ALLOW_NEW_PRIMITIVES or rel == "ci/check_imports.py":
            continue
        body = p.read_text(encoding="utf-8", errors="ignore")
        # ① core.stats 是新原语：除 model_metrics 外一律不许 import
        if re.search(r"(from\s+\.stats\s+import|from\s+core\.stats\s+import|"
                     r"import\s+core\.stats)", body):
            bad.append(f"{rel}: import core.stats（新原语只允许 model_metrics 用）")
            continue
        # ② V2 面里不许直接调模型级原语
        if _in_surface(rel) and rel not in V1_USERS:
            for sym in MODEL_PRIMITIVES:
                if re.search(r"(from\s+\.\s*metrics\s+import[^\n]*\b" + re.escape(sym)
                             + r"|from\s+core\.metrics\s+import[^\n]*\b"
                             + re.escape(sym) + r"|" + re.escape(sym) + r"\s*\()",
                             body):
                    bad.append(f"{rel}: 直接调用 {sym} → 口径会分叉")
                    break
    return bad


def gate2() -> list[str]:
    """scheduler/ 只准调入口，不准自己算。"""
    bad = []
    sched = ROOT / "scheduler"
    if not sched.exists():
        return bad
    for p in sched.rglob("*.py"):
        rel = _rel(p)
        body = p.read_text(encoding="utf-8", errors="ignore")
        # 允许 model_metrics. / catalog.；不允许裸 metrics. / stats.
        if re.search(r"(?<![\w.])metrics\.\w", body) or \
                re.search(r"(?<![\w.])stats\.\w", body):
            bad.append(f"{rel}: 直接用原语模块（应调 compute_model_metric）")
    return bad


def main() -> int:
    print("═" * 70)
    print("CI 门禁：唯一实现（口径不许有第二套）")
    print("═" * 70)
    b1, b2 = gate1(), gate2()
    print("\n[门禁 1] 模型级统计原语只允许 core/model_metrics.py 调用")
    print("  ✓ 通过" if not b1 else "\n".join("  ✗ " + x for x in b1))
    print("\n[门禁 2] scheduler/ 只调 compute_model_metric，不许自己算")
    print("  ✓ 通过" if not b2 else "\n".join("  ✗ " + x for x in b2))
    ok = not b1 and not b2
    print("\n" + ("✅ 两道门禁全绿" if ok else "❌ 门禁未通过：口径会分叉"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
