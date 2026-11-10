# -*- coding: utf-8 -*-
"""环境配置（S0.9 T-104）：把"会随部署变的阈值"集中成一行，并且**可被环境变量覆盖**。

════════════════════════════════════════════════════════════════════
为什么 min_sample 必须可配置，而不能写死在指标定义里
════════════════════════════════════════════════════════════════════
设计值是"好坏各 ≥50、每箱 ≥30"。但本仓库的夹具只有 2,000 单、约 40 个坏样本，
按 50 卡会**永远** VINTAGE_BLOCKED —— 于是分流功能一个都演示不了，
所有人第一反应都是"把这个判断删掉"，那才是真正的损失。

正确做法：阈值是一个声明式的值，夹具环境调小，接真实数仓时用环境变量
改回 50，代码一行不动：
    BTA_MIN_SAMPLE=50 python3 v2_dialogue.py

★为什么不放在 ModelMetricDef 里
  ModelMetricDef.min_sample 是对外契约的一部分（schema 描述/文档会引用它），
  而"部署环境用哪个值"是运维事实。两者混在一起，就会出现
  "演示库跑通了，生产忘了改"的经典事故。定义里存**设计默认值**，
  本文件存**当前生效值**，谁生效由 settings 说，审计里也记 settings。
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ROOT / "artifacts"


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        # 配错了不要静默用默认值 —— 那会让"我明明设了 50"变成一个查不出的问题
        raise ValueError(f"环境变量 {name} 必须是整数，收到 {raw!r}")


# 好坏各需多少才算数（设计要求 50；夹具默认 5，见模块 docstring）
MIN_SAMPLE: int = _int("BTA_MIN_SAMPLE", 5)

# 每箱至少要多少人，少于此的箱在报告里要标注（不改数值，只提示）
MIN_BIN_N: int = _int("BTA_MIN_BIN_N", 30)

# 曲线类指标的外卸目录与最大点数
REF_DIR: Path = Path(os.environ.get("BTA_REF_DIR") or (ARTIFACTS / "ref"))
CURVE_MAX_POINTS: int = _int("BTA_CURVE_POINTS", 100)

# 升格队列阈值（T-105）：同参数被问多少次后进入"该物化"队列
PROMOTE_AFTER: int = _int("BTA_PROMOTE_AFTER", 5)

# 计数的时间窗：只有**窗口内**的提问才累加。
# 没有这个窗，"三年前问过 5 次"和"这周问过 5 次"是同一个结论 ——
# 队列会被历史热度占满，而当前真正在用的口径永远排不进去。
PROMOTE_WINDOW_DAYS: int = _int("BTA_PROMOTE_WINDOW_DAYS", 30)


def describe() -> dict:
    """给审计与 trace 用的一行状态。"""
    return {"min_sample": MIN_SAMPLE, "min_bin_n": MIN_BIN_N,
            "curve_points": CURVE_MAX_POINTS, "promote_after": PROMOTE_AFTER,
            "promote_window_days": PROMOTE_WINDOW_DAYS,
            "ref_dir": str(REF_DIR)}
