# -*- coding: utf-8 -*-
"""指标内核 —— 回测分析的全部数学，纯函数，框架无关，可单测。

这一层是设计文档铁律"数字绝不经过 LLM"的物质基础：
  LLM 只产出分析计划（DSL）和文字叙述；所有数值来自本模块；
  报告渲染时每个数字都能溯源到一个 metric 函数调用。

模块里每个函数都对应设计文档的一节：
  consistency_rate      —— 第 0 步：判决一致率（数据可信的前提）
  funnel                —— 整体漏斗：各环节进入/通过/拒绝/通过率
  lmdi_stage_attribution —— 环节归因：LMDI 乘法分解（精确可加）
  first_reject          —— 策略归因①：首拒归因（主口径，天然可加）
  marginal_hit          —— 策略归因②：边际贡献/多命中（收紧方向的"值不值"）
  binstats              —— 单策略诊断：分箱坏账率（缺失/哨兵单独成箱）
  ks_statistic          —— 单策略诊断：KS（ties 校正版）
  threshold_sensitivity —— 单策略诊断：阈值敏感性表（收紧重放的原料）
  direction_judgement   —— 硬判据：拒绝组/通过组坏账比 + z 检验 + 单调性
  downstream_kill_rate  —— 放宽方向：增量人群被下游拦截比例（数据源钱的账）
  relax_pass_gain       —— 放宽方向：通过率增量（精确）+ 坏账可得性标注
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Sequence

from .codes import STAGE_ORDER, SOURCE_LEVEL

EPS = 1e-12


# ---------------------------------------------------------------------------
# 第 0 步：判决一致率
# ---------------------------------------------------------------------------
def consistency_rate(online_facts: list[dict], offline_facts: list[dict]) -> dict[str, Any]:
    """线上 vs 离线：同 (apply_id, stage, policy_id) 的 result 必须一致。

    返回一致率与不一致清单。教学要点：
      - 比对口径是【线上执行到且离线也执行到】的策略；
      - 线上因数据源故障缺值的行（value_state=MISSING_SOURCE）单独归一类：
        这不是"离线算错"，是"数据缺口"，要进补数清单而不是回滚回测；
      - 一致率 < 100% 时，后续所有统计都必须停止 —— 这是第 0 步的意义。
    """
    on = {(f["apply_id"], f["stage"], f["policy_id"]): f for f in online_facts}
    mismatches: list[dict] = []
    gaps: list[dict] = []
    compared = 0
    for f in offline_facts:
        key = (f["apply_id"], f["stage"], f["policy_id"])
        o = on.get(key)
        if o is None:
            continue                            # 线上没执行到（NOT_EXECUTED），不算
        if o["value_state"] == "MISSING_SOURCE":
            gaps.append({"apply_id": f["apply_id"], "stage": f["stage"],
                         "policy_id": f["policy_id"], "reason": "线上数据源故障"})
            continue
        compared += 1
        if o["result"] != f["result"]:
            mismatches.append({"apply_id": f["apply_id"], "stage": f["stage"],
                               "policy_id": f["policy_id"],
                               "online": o["result"], "offline": f["result"],
                               "online_detail": o["detail"], "offline_detail": f["detail"]})
    total = compared + len(mismatches)
    rate = compared / total if total else 0.0
    return {"compared": compared, "mismatch": len(mismatches),
            "data_gap": len(gaps), "gaps": gaps, "mismatches": mismatches,
            "rate": round(rate, 4), "level": SOURCE_LEVEL["MEASURED"]}


# ---------------------------------------------------------------------------
# 整体漏斗
# ---------------------------------------------------------------------------
def funnel(final_status: dict[str, str]) -> list[dict]:
    """各环节进入人数 / 通过 / 拒绝 / 通过率。

    口径铁律（设计文档铁律 3）：环节内通过率的分母是"进入本环节人数"，
    跨环节可比的贡献排名分母是"全量申请"。两个口径混用，排名全错。
    """
    n_total = len(final_status)
    entered: dict[str, int] = defaultdict(int)
    passed: dict[str, int] = defaultdict(int)
    rejected: dict[str, int] = defaultdict(int)
    reviewed: dict[str, int] = defaultdict(int)
    for aid, (stage, status) in final_status.items():
        for s in STAGE_ORDER:
            entered[s] += 1                     # 全量都"尝试进入"S1；短路后不进入后续
            if s == stage:
                break
        if status == "PASS" and stage:
            passed[stage] += 1
        elif status == "REJECT":
            rejected[stage] += 1
        elif status == "REVIEW":
            reviewed[stage] += 1
    rows = []
    for s in STAGE_ORDER:
        e = entered[s]
        p = passed[s]
        rows.append({"stage": s, "entered": e, "pass": p,
                     "reject": rejected[s], "review": reviewed[s],
                     "pass_rate_in_stage": round(p / e, 4) if e else None,
                     "pass_rate_overall": round(p / n_total, 4) if n_total else None})
    return rows


# ---------------------------------------------------------------------------
# 环节归因：LMDI（乘法结构分解，精确可加）
# ---------------------------------------------------------------------------
def _L(a: float, b: float) -> float:
    """对数平均数 L(a,b) = (a-b)/(ln a - ln b)，相同时取 a（LMDI 标准定义）。"""
    if abs(a - b) < EPS:
        return a
    return (a - b) / (math.log(a) - math.log(b))


def lmdi_stage_attribution(before: dict[str, float], after: dict[str, float]) -> dict[str, Any]:
    """把整体通过率变化 ΔP 精确分解到各环节（LMDI-II 加法分解）。

    教学要点（为什么必须 LMDI，不能用"各环节通过率变化直接相加"）：
      漏斗通过率是乘法结构 P = Π p_s。对乘法结构做加法分解，
      各环节"贡献"加起来不等于 ΔP，残差会吃掉一半以上的变化量，
      报告会被业务当场质疑。LMDI 加法分解是精确的：

          ΔP = P1 − P0 = L(P1, P0) · ln(P1/P0)
             = L(P1, P0) · Σ_s ln(p_s1 / p_s0)        （对数恒等式，逐项拆开）
             = Σ_s L(P1, P0) · ln(p_s1 / p_s0)

      其中 L 是对数平均数。注意权重 L(P1,P0) 对每个环节相同、
      不除以环节数（除以 S 的是乘法 Divisia 变体，分解的是比值不是差值
      —— 本项目第一版就写错了这个，自检断言当场抓出，教学价值极高）。
      自检里有一条断言：Σ 贡献 − ΔP == 0（浮点 1e-9 内）。

    参数：before/after = {stage: 环节通过率}（0<rate<=1）。
    """
    stages = [s for s in STAGE_ORDER if s in before and s in after]
    P0 = math.prod(max(before[s], EPS) for s in stages)
    P1 = math.prod(max(after[s], EPS) for s in stages)
    w = _L(P1, P0)
    out = {}
    for s in stages:
        p0, p1 = max(before[s], EPS), max(after[s], EPS)
        # 教学坑：不要在这里 round —— 贡献值一旦舍入，"精确可加"就变成
        # "1e-6 级近似"，残差断言会当场失败。精度只在渲染层丢。
        out[s] = w * math.log(p1 / p0)
    total = P1 - P0
    residual = sum(out.values()) - (P1 - P0)
    return {"before": round(P0, 6), "after": round(P1, 6), "delta": round(total, 6),
            "contributions": {k: round(v, 6) for k, v in out.items()},
            "residual": round(residual, 9),
            "additive_check_passed": abs(residual) < 1e-9}


# ---------------------------------------------------------------------------
# 策略归因①：首拒归因（主口径）
# ---------------------------------------------------------------------------
def first_reject(final_stage: dict[str, str], first_reject_policy: dict[str, str]) -> dict[str, Any]:
    """每条策略的"首拒"占比（分母 = 全量申请）。

    教学要点：为什么首拒是主口径 ——
      漏斗是短路的：一个申请被最早命中的策略拒绝后，后面的策略没执行。
      "P_CR_001 拒了 X 人"在业务上的含义就是"它首拒了 X 人"，
      与分析师看漏斗的直觉一致，且天然可加（Σ 各策略首拒率 = 整体拒绝率）。
    局限（所以还需要边际贡献口径）：
      首拒会把"多命中"的责任全记在排在前面的策略头上 ——
      P_CR_001 首拒贡献大，不代表它是唯一在拦风险的策略（被它抢拒的
      P_CR_002 的真实贡献被掩盖），这正是 Shapley 口径要补的。
    """
    n = len(final_stage) or 1
    counts: dict[str, int] = defaultdict(int)
    for aid, pol in first_reject_policy.items():
        counts[pol] += 1
    return {"denominator": n,
            "items": [{"policy_id": p, "first_reject_n": c,
                       "first_reject_pct": round(c / n, 4)}
                      for p, c in sorted(counts.items(), key=lambda kv: -kv[1])]}


# ---------------------------------------------------------------------------
# 策略归因②：边际贡献 / 多命中（收紧方向"值不值"的账）
# ---------------------------------------------------------------------------
def marginal_hit(facts: list[dict], policy_id: str, bad_by_apply: dict[str, int]) -> dict[str, Any]:
    """该策略命中（FAIL）的人里，有多少已经被其他策略同时命中。

    口径（设计文档四项检查之"边际贡献"）：
      命中集合 A = 该策略 FAIL 的人群（用事实表，不要求首拒）；
      其他策略覆盖 = A 中至少被另一条策略命中的人。
      若 80% 的坏人在别的策略也命中 → 加严这条只会砍通过率、几乎不降坏账。

    返回：
      hit_n / hit_bad_n —— 命中人群及其中有标签的坏人数（有标签=曾放款）
      unique_coverage   —— 仅被本策略命中的坏人比例（边际贡献的分子）
    """
    hit = {f["apply_id"] for f in facts
           if f["policy_id"] == policy_id and f["result"] == "FAIL"}
    others_hit: dict[str, set] = defaultdict(set)
    for f in facts:
        if f["result"] == "FAIL" and f["policy_id"] != policy_id:
            others_hit[f["apply_id"]].add(f["policy_id"])
    covered_by_other = sum(1 for a in hit if others_hit.get(a))
    bad_in_hit = [a for a in hit if bad_by_apply.get(a) == 1]
    unique_bad = sum(1 for a in bad_in_hit if not others_hit.get(a))
    return {"policy_id": policy_id, "hit_n": len(hit),
            "covered_by_other_n": covered_by_other,
            "covered_by_other_pct": round(covered_by_other / len(hit), 4) if hit else None,
            "bad_n_with_label": len(bad_in_hit),
            "unique_bad_n": unique_bad,
            "unique_bad_pct": round(unique_bad / len(bad_in_hit), 4) if bad_in_hit else None,
            "note": "unique_bad_pct 低 → 加严此策略几乎不新增拦截，别动"}


# ---------------------------------------------------------------------------
# 单策略诊断：分箱（缺失/哨兵单独成箱）
# ---------------------------------------------------------------------------
def binstats(pairs: Sequence[tuple[float | None, str, int | None]],
             n_bins: int = 8) -> list[dict]:
    """对 (raw_value, value_state, bad_flag) 做坏账率分箱。

    教学要点（设计文档 05 章②）：
      - 只有 value_state=OK 的值参与等频分箱；
      - MISSING_SOURCE / BIZ_EMPTY / SENTINEL / 无标签 各自单独成箱，
        绝不混进数值箱（哨兵 -1 混进数值箱会拉歪均值，这是真实事故）；
      - 每箱给坏账率 + 95% Wilson 区间，样本 <30 标 INSUFFICIENT。
    参数 pairs：[(raw_value, value_state, bad_flag), ...]
    """
    ok_vals = sorted(v for v, st, _ in pairs if st == "OK" and v is not None)
    bins: list[dict] = []
    if ok_vals:
        # 等频分箱边界（教学坑：int(len*i/k) 在 i==k 时会越界，末边界单独取最大值）
        k = min(n_bins, max(1, len(set(ok_vals))))
        edges = sorted({ok_vals[min(int(len(ok_vals) * i / k), len(ok_vals) - 1)]
                        for i in range(k)} | {ok_vals[-1]})
        for i in range(len(edges) - 1):
            lo, hi = edges[i], edges[i + 1]
            grp = [(y) for v, st, y in pairs
                   if st == "OK" and lo <= v <= hi and y is not None]
            if grp:
                bins.append(_bin_row(f"[{lo:g},{hi:g}]", grp, ok=True))
    for st, label in [("MISSING_SOURCE", "缺失(源故障)"), ("BIZ_EMPTY", "业务空值"),
                      ("SENTINEL", "哨兵值")]:
        grp = [y for v, s, y in pairs if s == st and y is not None]
        if grp:
            bins.append(_bin_row(label, grp, ok=False, state=st))
    return bins


def _bin_row(label: str, ys: list[int], ok: bool, state: str = "OK") -> dict:
    n = len(ys)
    bad = sum(ys)
    rate = bad / n
    lo, hi = _wilson(bad, n)
    return {"bin": label, "value_state": state, "n": n, "bad_n": bad,
            "bad_rate": round(rate, 4),
            "ci95": [round(lo, 4), round(hi, 4)],
            "flag": "INSUFFICIENT" if n < 30 else ("OK" if ok else "SPECIAL")}


def _wilson(bad: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 区间：小样本坏账率比正态近似稳得多。"""
    if n == 0:
        return (0.0, 0.0)
    p = bad / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - spread), min(1.0, center + spread))


# ---------------------------------------------------------------------------
# 单策略诊断：KS（ties 校正）
# ---------------------------------------------------------------------------
def ks_statistic(good_scores: Sequence[float], bad_scores: Sequence[float]) -> dict[str, Any]:
    """两样本 KS 统计量（D 值与出现位置）。

    教学要点（设计文档 05 章①）：KS 会被大量 ties 骗。
      策略变量常是整数计数（查询次数 0,1,2...），离散值带来大量并列，
      KS 的 D 会在某格集中跳变。所以这里返回 D 和 argmax 位置，
      解释时必须结合分箱表，不能只报一个数。
    """
    if not good_scores or not bad_scores:
        return {"ks": None, "note": "样本不足"}
    xs = sorted(set(good_scores) | set(bad_scores))
    g = sorted(good_scores)
    b = sorted(bad_scores)
    d_best = 0.0
    arg = None

    def cdf(arr, x):
        lo, hi = 0, len(arr)
        while lo < hi:
            mid = (lo + hi) // 2
            if arr[mid] <= x:
                lo = mid + 1
            else:
                hi = mid
        return lo / len(arr)

    for x in xs:
        d = abs(cdf(b, x) - cdf(g, x))
        if d > d_best:
            d_best, arg = d, x
    return {"ks": round(d_best, 4), "argmax": arg}


# ---------------------------------------------------------------------------
# 单策略诊断：阈值敏感性表 + 方向硬判据
# ---------------------------------------------------------------------------
def threshold_sensitivity(pairs: Sequence[tuple[float | None, str, int | None]],
                          current_threshold: float,
                          higher_is_worse: bool = True) -> list[dict]:
    """对每个候选阈值 t：通过率（该档放行比例）、放行坏账率、边际坏账率。

    教学要点（收紧重放的数学本质）：
      收紧方向的坏账率结论是【精确值】，因为候选区间的人在当前策略下
      已经放款、已有标签 —— 这张表就是"重放"的解析形式。
      "边际坏账率"= 新增被拒人群的坏账率；它就是业务容忍线的交点所在。
    """
    ok = [(v, y) for v, st, y in pairs if st == "OK" and v is not None and y is not None]
    n_all = len(ok) or 1
    out = []
    candidates = sorted({current_threshold} | {v for v, _ in ok})
    for t in candidates:
        granted = [(v, y) for v, y in ok if (v <= t if higher_is_worse else v >= t)]
        rejected = [(v, y) for v, y in ok if (v > t if higher_is_worse else v < t)]
        pass_rate = len(granted) / n_all
        bad_rate = (sum(y for _, y in granted) / len(granted)) if granted else None
        marginal = (sum(y for _, y in rejected) / len(rejected)) if rejected else None
        lo, hi = _wilson(int(sum(y for _, y in granted)), len(granted)) if granted else (0, 0)
        out.append({"threshold": t, "pass_rate": round(pass_rate, 4),
                    "granted_n": len(granted), "rejected_n": len(rejected),
                    "granted_bad_rate": round(bad_rate, 4) if bad_rate is not None else None,
                    "granted_ci95": [round(lo, 4), round(hi, 4)],
                    "marginal_bad_rate": round(marginal, 4) if marginal is not None else None})
    return out


def direction_judgement(pairs: Sequence[tuple[float | None, str, int | None]],
                        threshold: float) -> dict[str, Any]:
    """方向硬判据（设计文档 05 章④，五条规则）。

    返回 direction: TIGHTEN / RELAX / HOLD 及理由。
    教学要点：方向是分析的输出不是输入 —— 本函数就是"让数据说话"的那一步。
    """
    ok = [(v, y) for v, st, y in pairs if st == "OK" and v is not None and y is not None]
    granted = [y for v, y in ok if v <= threshold]
    rejected_v = [v for v, y in ok if v > threshold]
    rejected = [y for v, y in ok if v > threshold]
    if len(granted) < 30 or len(rejected) < 30:
        return {"direction": "HOLD", "ratio": None,
                "reason": f"样本不足（通过组 {len(granted)} / 拒绝组 {len(rejected)} <30）"}
    pg = sum(granted) / len(granted)
    pr = sum(rejected) / len(rejected)
    ratio = pr / max(pg, EPS)
    # z 检验（双比例）
    p_pool = (sum(granted) + sum(rejected)) / (len(granted) + len(rejected))
    se = math.sqrt(p_pool * (1 - p_pool) * (1 / len(granted) + 1 / len(rejected)))
    z = (pr - pg) / max(se, EPS)
    # 单调性：分箱坏账率与箱序的 Spearman
    bins = binstats(pairs)
    ok_bins = [b for b in bins if b["value_state"] == "OK" and b["flag"] == "OK"]
    rho = None
    if len(ok_bins) >= 3:
        rates = [b["bad_rate"] for b in ok_bins]
        n = len(rates)
        r1 = _ranks(rates)
        r2 = _ranks(list(range(1, n + 1)))
        d2 = sum((a - b) ** 2 for a, b in zip(r1, r2))
        rho = round(1 - 6 * d2 / (n * (n * n - 1)), 3)
    if ratio < 1.3:
        # 设计文档 05 章④"过紧"：拒绝组坏账率并不比通过组高多少 → 白拒
        direction, reason = "RELAX", (f"拒绝组坏账率 {pr:.1%} vs 通过组 {pg:.1%}，"
                                      f"比值 {ratio:.2f} <1.3 —— 拒的人风险并不更高，白拒了")
    elif ratio >= 2.0 and len(rejected) < 200:
        # 设计文档 05 章④"过松"：拒绝组风险显著更高但样本太少，
        # 说明大量高风险还留在通过组里没被拦
        direction, reason = "TIGHTEN", (f"比值 {ratio:.2f} ≥2.0 但拒绝组样本仅 {len(rejected)}"
                                        f" —— 大量高风险仍在通过组，看分箱找收紧点")
    elif ratio >= 2.0:
        direction, reason = "HOLD", (f"拒绝组/通过组坏账比 {ratio:.2f}，z={z:.2f}，"
                                     f"阈值有效；能否再松看敏感性表")
    else:
        direction, reason = "HOLD", f"比值 {ratio:.2f} 在 [1.3,2.0) 灰区，z={z:.2f}，维持现状"
    return {"direction": direction, "ratio": round(ratio, 3), "z": round(z, 3),
            "granted_bad_rate": round(pg, 4), "rejected_bad_rate": round(pr, 4),
            "spearman": rho, "reason": reason}


def slice_detectable(pairs: Sequence[tuple[float | None, str, int | None]],
                     lo: float, hi: float, tolerance: float) -> dict[str, Any]:
    """目标切片的信号统计功效判定 —— 评估期望自适应的基础。

    教学要点（评估设计的进阶课）：
      评估 Agent 时不能拿"裸真值"当期望 —— 切片里坏账率 14.6% 但只有 41 人，
      Wilson 下界只有 6%，任何诚实的系统都不该据此给出收紧建议。
      所以期望必须是条件式的：信号在统计上可检出（Wilson 下界越过容忍线）
      时，Agent 必须给出收紧建议；不可检出时，Agent 必须 HOLD。
      这就是"期望随统计功效自适应"，比固定答案的评估高一个档次。
    """
    slice_ys = [y for v, st, y in pairs
                if st == "OK" and v is not None and y is not None and lo <= v <= hi]
    n = len(slice_ys)
    bad = sum(slice_ys)
    rate = bad / n if n else None
    lb, _ = _wilson(bad, n) if n else (None, None)
    return {"lo": lo, "hi": hi, "n": n, "bad_n": bad, "bad_rate": rate,
            "wilson_lb": round(lb, 4) if lb is not None else None,
            "tolerance": tolerance,
            "detectable": bool(lb is not None and lb > tolerance)}


def _ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


# ---------------------------------------------------------------------------
# 放宽方向：下游拦截率 + 通过率增量（精确）+ 坏账可得性标注
# ---------------------------------------------------------------------------
def _stage_pass_set(facts: list[dict], stage: str) -> set[str]:
    """环节通过人群 = 该环节【全部策略 PASS】的申请集合。

    教学坑（本文件曾经写错过）：用"存在一条 result=PASS 的行"当环节通过，
    会把同环节其他策略 FAIL 的申请也数进来 —— 放宽的增量人群直接算错。
    UNKNOWN（转人工）同样不算通过。
    """
    by_apply: dict[str, set[str]] = defaultdict(set)
    for f in facts:
        if f["stage"] == stage:
            by_apply[f["apply_id"]].add(f["result"])
    return {a for a, results in by_apply.items() if results == {"PASS"}}


def relax_eval(facts_current: list[dict], facts_candidate: list[dict],
               bad_by_apply: dict[str, int]) -> dict[str, Any]:
    """对比"当前配置"与"放宽候选配置"两份重放事实，算放宽这本账。

    返回三块（设计文档 05 章放宽对称账 + 07 章坑 0）：
      pass_gain     —— 通过率增量（精确值：特征都在，重放即得）
      downstream    —— 增量人群的下游拦截率（数据源钱的账：
                       过了放宽节点但被下游拒的人 = 白花的数据源费）
      bad_availability —— 增量人群坏账标签可得性（不可得 → UNAVAILABLE，
                       报告只能留区间或留白，绝不冒充实测）
    """
    cur_pass = _stage_pass_set(facts_current, "S4_LIM")
    cand_pass = _stage_pass_set(facts_candidate, "S4_LIM")
    n_total = len({f["apply_id"] for f in facts_current})
    # 增量口径分两层（设计文档"放宽对称账"的关键区分）：
    #   new_s2    —— 放宽节点新放行的人（过了 S2，但其中一部分会被下游拦掉）
    #   incremental —— 最终放款漏斗走到头的人（= new_s2 ∩ 最终通过）
    # 口径坑（实测踩过）：环节通过 = 该环节【全部策略 PASS】，
    # 不能写成"存在一条 PASS 行"—— 那会把 FAIL 的申请也数成通过。
    cur_s2 = _stage_pass_set(facts_current, "S2_AF")
    cand_s2 = _stage_pass_set(facts_candidate, "S2_AF")
    new_s2 = cand_s2 - cur_s2
    incremental = cand_pass - cur_pass
    killed_downstream = new_s2 - cand_pass          # 过了放宽节点、被下游拦掉
    # 增量人群在当前配置下的首拒环节/策略（他们原本被谁拦）
    first_reject_of = {}
    for f in facts_current:
        if f["result"] == "FAIL" and f["apply_id"] in new_s2:
            first_reject_of.setdefault(f["apply_id"], (f["stage"], f["policy_id"]))
    bad_available = [a for a in incremental if a in bad_by_apply]
    return {"n_total": n_total,
            "pass_rate_current": round(len(cur_pass) / n_total, 4),
            "pass_rate_candidate": round(len(cand_pass) / n_total, 4),
            "pass_gain_pct": round((len(cand_pass) - len(cur_pass)) / n_total, 4),
            "new_s2_n": len(new_s2),
            "incremental_n": len(incremental),
            "incremental_origin": _count_by(first_reject_of.values()),
            "downstream_kill_n": len(killed_downstream),
            "downstream_kill_rate": round(len(killed_downstream) / len(new_s2), 4)
            if new_s2 else None,
            "bad_label_available_n": len(bad_available),
            "bad_rate_level": SOURCE_LEVEL["MEASURED"] if incremental
            and len(bad_available) == len(incremental) else SOURCE_LEVEL["UNAVAILABLE"],
            "note": "下游拦截率高 → 放宽放进来的钱大半被下游拦掉（数据源费白花），先调下游再放宽"}


def _count_by(pairs) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for p in pairs:
        out[f"{p[0]}/{p[1]}"] += 1
    return dict(out)


# ---------------------------------------------------------------------------
# 收紧方向：what-if 重放的精确对比（headline 级）
# ---------------------------------------------------------------------------
def tighten_compare(bad_by_apply: dict[str, int],
                    pass_current: set[str], pass_candidate: set[str],
                    n_total: int | None = None) -> dict[str, Any]:
    """收紧前后：通过率损失 + 放行人群坏账率变化（均为精确值）。

    教学要点：收紧 = 取子集。候选区间的人已放款有标签，
    所以"收紧后坏账率下降多少"不是估计，是算术。这是放宽永远给不了的。
    """
    total = n_total or max(len(pass_current | pass_candidate), 1)
    bad_cur = [a for a in pass_current if bad_by_apply.get(a) == 1]
    bad_cand = [a for a in pass_candidate if bad_by_apply.get(a) == 1]
    lost = pass_current - pass_candidate
    lost_bad = [a for a in lost if bad_by_apply.get(a) == 1]
    return {"pass_rate_current": round(len(pass_current) / total, 4),
            "pass_rate_candidate": round(len(pass_candidate) / total, 4),
            "pass_loss_pct": round((len(pass_current) - len(pass_candidate)) / total, 4),
            "bad_rate_current": round(len(bad_cur) / len(pass_current), 4) if pass_current else None,
            "bad_rate_candidate": round(len(bad_cand) / len(pass_candidate), 4) if pass_candidate else None,
            "cut_n": len(lost), "cut_bad_n": len(lost_bad),
            "cut_bad_rate": round(len(lost_bad) / len(lost), 4) if lost else None,
            "level": SOURCE_LEVEL["MEASURED"],
            "note": "收紧方向全部指标为实测精确值，无需实验"}
