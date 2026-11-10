# -*- coding: utf-8 -*-
"""异动归因内核 —— 回答「为什么通过率降了 / FPD 升了」。

════════════════════════════════════════════════════════════════════
为什么"归因"要单独成一个模块（没有它会怎样）
════════════════════════════════════════════════════════════════════
把"本期 vs 上期"的变化直接丢给 LLM，它一定会给你一个听起来很合理的原因
—— 而且很可能是编的。归因必须拆成**四个可证伪的假设**，逐个用数据检验：

    假设 1 策略侧：某环节通过率下降（LMDI 精确分解，贡献可加）
    假设 2 客群侧：客群结构漂移（渠道/首复贷占比变化）导致整体率变化
    假设 3 数据侧：缺失率/未知率上升，把"算不出来"算成了"通过/拒绝"
    假设 4 随机波动：变化在置信区间内，根本没发生异动

每条假设都输出"证据 + 数据来源等级"，最终按证据强度排序。
没有这一层，"原因"就是叙事；有这一层，"原因"是**假设 + 证据 + 可反驳**。

★本模块最重要的一条：**先判显著性，再谈归因**。
  不显著的变化强行归因，等于给噪声编故事 —— 这是分析师最常被业务挑战的地方。
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from .codes import STAGE_ORDER
from .metrics import _L, lmdi_stage_attribution

EPS = 1e-9


# ---------------------------------------------------------------------------
# 一、基础：把事实表压成"环节通过率"与"申请级记录"
# ---------------------------------------------------------------------------
def stage_pass_rates(facts: list[dict]) -> dict[str, float]:
    """环节通过率（分母=进入本环节人数），口径与 catalog.stage_rates 一致。"""
    entered: dict[str, set] = {}
    for f in facts:
        entered.setdefault(f["stage"], set()).add(f["apply_id"])
    final_pass = {a for a, rs in _by_apply_stage(facts, "S4_LIM").items()
                  if rs == {"PASS"}}
    out = {}
    for i, stage in enumerate(STAGE_ORDER):
        e = entered.get(stage, set())
        if not e:
            continue
        if i + 1 < len(STAGE_ORDER):
            passed = entered.get(STAGE_ORDER[i + 1], set()) & e
        else:
            passed = final_pass & e
        out[stage] = len(passed) / len(e)
    return out


def _by_apply_stage(facts: list[dict], stage: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for f in facts:
        if f["stage"] == stage:
            out[f["apply_id"]].add(f["result"])
    return out


def records(apps: list[dict], facts: list[dict],
            label_key: str = "bad_flag") -> list[dict]:
    """申请级记录：每人一行，含通过标记、分层字段、标签。

    教学要点：归因的三个假设（策略/客群/数据）需要同一批记录反复切，
    所以先物化一次，避免每个假设各写一遍 join —— 也避免口径不一致。
    """
    pass_ids = {a for a, rs in _by_apply_stage(facts, "S4_LIM").items()
                if rs == {"PASS"}}
    out = []
    for a in apps:
        out.append({"apply_id": a["apply_id"], "product": a["product_code"],
                    "channel": a.get("channel"), "is_new": a.get("is_new_customer"),
                    "apply_date": str(a.get("created_at") or "")[:10],
                    "passed": 1 if a["apply_id"] in pass_ids else 0,
                    "label": a.get(label_key)})
    return out


# ---------------------------------------------------------------------------
# 二、显著性：先回答"到底有没有异动"
# ---------------------------------------------------------------------------
def delta_with_ci(p0: float, n0: int, p1: float, n1: int) -> dict[str, Any]:
    """率差 + 95% 置信区间 + 双比例 z 检验。

    教学要点（为什么必须做）：
      月申请 300 单时，通过率的标准误约 2.5pt。看到"下降 1.8pt"就归因，
      等于把噪声当信号。先看 CI 是否覆盖 0，再决定要不要归因。
    """
    if not n0 or not n1:
        return {"delta": None, "ci95": None, "z": None, "significant": False}
    delta = p1 - p0
    se = math.sqrt(max(p0 * (1 - p0) / n0 + p1 * (1 - p1) / n1, EPS))
    z = delta / se
    return {"delta": round(delta, 4),
            "ci95": [round(delta - 1.96 * se, 4), round(delta + 1.96 * se, 4)],
            "se": round(se, 4), "z": round(z, 2),
            "significant": abs(z) >= 1.96}


# ---------------------------------------------------------------------------
# 三、假设 2：客群结构漂移（mix shift）
# ---------------------------------------------------------------------------
def mix_shift(prev: list[dict], cur: list[dict], seg_key: str,
              value_key: str = "passed") -> dict[str, Any]:
    """两因子分解：整体率变化 = 结构效应 + 水平效应 + 交互项。

    Δ = Σ(s1·r1 − s0·r0)
      = Σ(s1−s0)·r0      结构效应（客群占比变了）
      + Σ s0·(r1−r0)     水平效应（同一客群内部率变了）
      + Σ(s1−s0)(r1−r0)  交互项

    三项之和恒等于 Δ（残差为 0），可以直接写进报告 —— 这和 LMDI 是同一个
    思路在不同结构上的应用：**分解必须精确可加，不能让"其他"吃掉一半**。
    """
    def agg(rows):
        n = len(rows) or 1
        share, rate, cnt = {}, {}, {}
        by: dict[Any, list] = defaultdict(list)
        for r in rows:
            if r.get(value_key) is None:
                continue
            by[r.get(seg_key)].append(r[value_key])
        for k, vals in by.items():
            cnt[k] = len(vals)
            share[k] = len(vals) / n
            rate[k] = sum(vals) / len(vals)
        return share, rate, cnt

    s0, r0, c0 = agg(prev)
    s1, r1, c1 = agg(cur)
    keys = set(s0) | set(s1)
    mix = sum((s1.get(k, 0) - s0.get(k, 0)) * r0.get(k, 0) for k in keys)
    lvl = sum(s0.get(k, 0) * (r1.get(k, 0) - r0.get(k, 0)) for k in keys)
    inter = sum((s1.get(k, 0) - s0.get(k, 0)) * (r1.get(k, 0) - r0.get(k, 0))
                for k in keys)
    total = mix + lvl + inter
    return {"seg": seg_key, "mix_effect": round(mix, 4),
            "level_effect": round(lvl, 4), "interaction": round(inter, 4),
            "total": round(total, 4), "residual": round(total - (
                sum(s1.get(k, 0) * r1.get(k, 0) for k in keys) -
                sum(s0.get(k, 0) * r0.get(k, 0) for k in keys)), 9),
            "segments": {str(k): {"share_prev": round(s0.get(k, 0), 3),
                                  "share_cur": round(s1.get(k, 0), 3),
                                  "rate_prev": round(r0.get(k, 0), 3),
                                  "rate_cur": round(r1.get(k, 0), 3),
                                  "n_prev": c0.get(k, 0), "n_cur": c1.get(k, 0)}
                         for k in keys}}


# ---------------------------------------------------------------------------
# 四、假设 3：数据质量漂移
# ---------------------------------------------------------------------------
def quality_scan(facts: list[dict]) -> dict[str, Any]:
    """按环节统计缺失率 / 未知率。缺失率突增时，所有率指标都不可信。

    ★五态在这里变现：MISSING_SOURCE（源挂了）和 SENTINEL（查无记录）
      必须分开看 —— 前者是数据事故，后者是客群特征。
    """
    per: dict[str, dict] = {}
    for f in facts:
        if f["value_state"] == "NOT_EXECUTED":
            continue
        d = per.setdefault(f["stage"], {"n": 0, "miss": 0, "unknown": 0,
                                        "sentinel": 0, "biz_empty": 0})
        d["n"] += 1
        if f["value_state"] == "MISSING_SOURCE":
            d["miss"] += 1
        elif f["value_state"] == "SENTINEL":
            d["sentinel"] += 1
        elif f["value_state"] == "BIZ_EMPTY":
            d["biz_empty"] += 1
        if f["result"] == "UNKNOWN":
            d["unknown"] += 1
    out = {}
    for s, d in per.items():
        n = d["n"] or 1
        out[s] = {"n": d["n"], "miss_rate": round(d["miss"] / n, 4),
                  "unknown_rate": round(d["unknown"] / n, 4),
                  "sentinel_rate": round(d["sentinel"] / n, 4),
                  "biz_empty_rate": round(d["biz_empty"] / n, 4)}
    return out


# ---------------------------------------------------------------------------
# 五、归因主函数：四个假设 + 排序
# ---------------------------------------------------------------------------
def stage_tests(prev_facts: list[dict], cur_facts: list[dict]) -> list[dict]:
    """环节级显著性检验 —— 为什么必须在整体之外再做一层。

    教学要点（本项目的一个真实发现）：
      整体通过率的变化会被"乘法稀释"：S2 掉 25pt，整体只掉 3pt，
      因为后面环节把影响压缩了。于是整体 z 检验不显著，
      但环节级 z 检验显著 —— 此时正确结论不是"没变化"，
      而是"整体未达显著、S3 环节显著下降"。只做整体检验会漏掉真信号。
    """
    out = []
    entered_p, entered_c = _entered(prev_facts), _entered(cur_facts)
    rp, rc = stage_pass_rates(prev_facts), stage_pass_rates(cur_facts)
    for st in STAGE_ORDER:
        if st not in rp or st not in rc:
            continue
        test = delta_with_ci(rp[st], len(entered_p.get(st, [])),
                             rc[st], len(entered_c.get(st, [])))
        out.append({"stage": st, "rate_prev": round(rp[st], 4),
                    "rate_cur": round(rc[st], 4),
                    "n_prev": len(entered_p.get(st, [])),
                    "n_cur": len(entered_c.get(st, [])),
                    "delta": test["delta"], "ci95": test["ci95"],
                    "z": test["z"], "significant": test["significant"]})
    return out


def _entered(facts: list[dict]) -> dict[str, set]:
    out: dict[str, set] = defaultdict(set)
    for f in facts:
        out[f["stage"]].add(f["apply_id"])
    return out


def attribute(prev_facts: list[dict], cur_facts: list[dict],
              prev_records: list[dict], cur_records: list[dict],
              target: str = "pass") -> dict[str, Any]:
    """对"本期 vs 上期"做四假设归因，返回排序后的假设清单。

    target: "pass"（通过率）| "bad"（FPD/坏账率）

    ★每条假设都拆成两部分：
        evidence —— 不含数字的自然语言（避免数字绕过登记簿）
        numbers  —— 数字字典（由编排层登记到 AnswerBook 再渲染）
      这是"数字绝不经过 LLM"在归因层的落地：
      连"证据句"都不自己带数字，杜绝"看起来有出处、实则没登记的 2.75%"。
    """
    # ---- 主体指标 ----
    key = "passed" if target == "pass" else "label"
    p0 = sum(r[key] for r in prev_records if r[key] is not None) / max(
        len([r for r in prev_records if r[key] is not None]), 1)
    p1 = sum(r[key] for r in cur_records if r[key] is not None) / max(
        len([r for r in cur_records if r[key] is not None]), 1)
    n0 = len([r for r in prev_records if r[key] is not None])
    n1 = len([r for r in cur_records if r[key] is not None])
    sig = delta_with_ci(p0, n0, p1, n1)

    hypotheses: list[dict] = []
    # 假设 4：先判随机波动（不显著 → 直接把它排第一，阻止后续编故事）
    if not sig["significant"]:
        hypotheses.append({
            "rank": 1, "hypothesis": "随机波动（整体未达显著）",
            "evidence": "变化幅度与置信区间覆盖零值，z 值未达双侧显著阈值，"
                        "整体层面样本量不足以直接归因；需看环节级检验",
            "numbers": {"delta": sig["delta"], "ci_low": sig["ci95"][0],
                        "ci_high": sig["ci95"][1], "z": sig["z"]},
            "level": "MEASURED", "score": 1.0})

    # 假设 1：策略侧（LMDI）
    rates0, rates1 = stage_pass_rates(prev_facts), stage_pass_rates(cur_facts)
    lmdi = lmdi_stage_attribution(rates0, rates1) if rates0 and rates1 else None
    tests = stage_tests(prev_facts, cur_facts)
    sig_stages = [t for t in tests if t["significant"]]
    if lmdi:
        contrib = {k: v for k, v in lmdi["contributions"].items()}
        top_stage = max(contrib, key=lambda k: abs(contrib[k])) if contrib else None
        share = (abs(contrib[top_stage]) / abs(lmdi["delta"])
                 if top_stage and abs(lmdi["delta"]) > EPS else 0)
        if top_stage and share >= 0.6:
            hypotheses.append({
                "rank": len(hypotheses) + 1,
                "hypothesis": f"策略侧：{top_stage} 环节通过率变化",
                "evidence": "LMDI 精确分解显示该环节贡献了整体变化的绝大部分"
                            "（各项相加恰好等于总变化，无分解残差），环节级 z 检验"
                            + ("显著" if any(t["stage"] == top_stage and t["significant"]
                                          for t in tests) else "未达显著，需扩样本"),
                "numbers": {"delta_p": lmdi["delta"],
                            "stage_contrib": contrib[top_stage],
                            "share": round(share, 3),
                            "residual": lmdi["residual"]},
                "level": "MEASURED", "score": round(share, 3)})

    # 假设 2：客群结构漂移
    mix_ch = mix_shift(prev_records, cur_records, "channel", key)
    mix_old = mix_shift(prev_records, cur_records, "is_new", key)
    worst = max([mix_ch, mix_old], key=lambda m: abs(m["mix_effect"]))
    if abs(worst["mix_effect"]) >= 0.005:            # 0.5pt 以上才值得提
        hypotheses.append({
            "rank": len(hypotheses) + 1,
            "hypothesis": f"客群结构漂移：{worst['seg']} 占比变化",
            "evidence": "两因子分解：结构效应、水平效应、交互项三者之和恰好等于"
                        "整体变化（无残差）；结构效应超过半个百分点才列入假设",
            "numbers": {"mix_effect": worst["mix_effect"],
                        "level_effect": worst["level_effect"],
                        "interaction": worst["interaction"]},
            "level": "MEASURED", "score": round(abs(worst["mix_effect"]), 3)})

    # 假设 3：数据质量
    q0, q1 = quality_scan(prev_facts), quality_scan(cur_facts)
    jumps = [(s, q1[s]["miss_rate"] - q0.get(s, {}).get("miss_rate", 0))
             for s in q1 if s in q0]
    jumps = [(s, d) for s, d in jumps if d >= 0.02]
    if jumps:
        s, d = max(jumps, key=lambda x: x[1])
        hypotheses.append({
            "rank": len(hypotheses) + 1,
            "hypothesis": f"数据侧：{s} 数据源缺失率上升",
            "evidence": "该环节缺失率较上期明显上升、已越过立项阈值；缺失期间判决为 "
                        "UNKNOWN，既不计通过也不计拒绝，会把率指标一起拉偏",
            "numbers": {"miss_prev": q0[s]["miss_rate"], "miss_cur": q1[s]["miss_rate"]},
            "level": "MEASURED", "score": round(d, 3)})

    hypotheses.sort(key=lambda h: (h["rank"] if not sig["significant"] else 0,
                                  -h["score"]))
    for i, h in enumerate(hypotheses, 1):
        h["rank"] = i
    return {"target": target, "period_prev": {"rate": round(p0, 4), "n": n0},
            "period_cur": {"rate": round(p1, 4), "n": n1},
            "delta": sig, "lmdi": lmdi, "stage_tests": tests,
            "significant_stages": [t["stage"] for t in sig_stages],
            "mix": {"channel": mix_ch, "new_vs_old": mix_old},
            # 汇总值单独给出：报告要写"排除了什么"，排除项也要有数字
            "mix_max_effect": worst["mix_effect"],
            "mix_max_seg": worst["seg"],
            "quality_max_jump": round(max([d for _s, d in jumps], default=0.0), 4),
            "quality": {"prev": q0, "cur": q1},
            "hypotheses": hypotheses,
            "conclusion_level": "MEASURED" if hypotheses else "UNAVAILABLE"}
