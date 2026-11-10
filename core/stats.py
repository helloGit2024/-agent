# -*- coding: utf-8 -*-
"""统计原语（V2.5 §8.3）：只做数学，不知道业务，也不碰数据库。

════════════════════════════════════════════════════════════════════
为什么单独一个文件
════════════════════════════════════════════════════════════════════
本项目的铁律是"口径必须唯一实现"。模型级指标（KS/AUC/PSI/IV/LIFT）的
数学部分必须只有一份，否则"定时任务算的 KS"和"Agent 算的 KS"迟早不一样。

但 **共享原语 ≠ 共享口径**：真正会漂移的不是 ks_statistic 本身，而是调它
之前那 20 行（五态怎么处理、label 用哪个、表现期要不要前移、ties 怎么算、
切点怎么生成）。所以纪律是两条：
  ① 原语只有一份（本文件 + core/metrics.py 里 V1 已有的 ks_statistic）；
  ② 全系统只允许 core/model_metrics.py import 它们（ci/check_imports.py 门禁）。

════════════════════════════════════════════════════════════════════
一个必须提前拧正的口径："分箱之后每一箱的 KS"
════════════════════════════════════════════════════════════════════
WOE / IV 贡献 / 坏账率 / LIFT 可以每箱一个；但 KS 是**全分布的累计最大值**，
不是每箱一个。"每箱一个 KS"做出来会是一个没人能解释的数，
而它看起来特别合理（每箱都有 n、有坏账率，再挂一个"KS"毫不违和）。

正确形态：每箱给 cum_ks（累计值，可画曲线），整表只报一个 ks_max。
"""
from __future__ import annotations

import math
import random
from typing import Sequence

EPS = 1e-9


# ---------------------------------------------------------------------------
# AUC / Gini
# ---------------------------------------------------------------------------
def auc(good_scores: Sequence[float], bad_scores: Sequence[float]) -> float | None:
    """AUC = P(坏样本分数 > 好样本分数)，ties 记 1/2。

    约定（写死在这里，不靠调用方记）：**分数越高风险越高**。
    方向搞反会得到 1-AUC —— 而 1-AUC 看起来同样"像个正常的 AUC"，
    这是模型评估里最常见的静默错误。
    """
    ng, nb = len(good_scores), len(bad_scores)
    if ng == 0 or nb == 0:
        return None
    allv = sorted([(s, 1) for s in bad_scores] + [(s, 0) for s in good_scores])
    # 平均秩（处理 ties）
    ranks = [0.0] * len(allv)
    i = 0
    while i < len(allv):
        j = i
        while j + 1 < len(allv) and allv[j + 1][0] == allv[i][0]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[k] = avg
        i = j + 1
    rank_sum_bad = sum(r for r, (_, y) in zip(ranks, allv) if y == 1)
    return (rank_sum_bad - nb * (nb + 1) / 2) / (nb * ng)


def gini_from_auc(a: float | None) -> float | None:
    return None if a is None else 2 * a - 1


# ---------------------------------------------------------------------------
# 分箱表：n / bad_rate / woe / iv_contrib / lift / cum_ks
# ---------------------------------------------------------------------------
def quantile_edges(values: Sequence[float], k: int) -> list[float]:
    """等频切点（去重）。**切点由函数生成，不接受外部传入**。

    为什么 bins 只收箱数不收切点：切点是口径。允许传断点就等于允许把
    单调性调成任意想要的形状，而单调性直接决定"要不要调阈值"。
    """
    vs = sorted(values)
    if not vs:
        return []
    k = max(2, min(k, len(set(vs))))
    edges = {vs[min(int(len(vs) * i / k), len(vs) - 1)] for i in range(k)}
    edges.add(vs[-1])
    return sorted(edges)


def bin_table(scores: Sequence[float], labels: Sequence[int], n_bins: int = 10
              ) -> dict:
    """按分数升序等频分箱，返回分箱表 + 整表统计量。

    返回：
      rows     —— bin / lo / hi / n / bad_n / bad_rate / woe / iv_contrib / lift / cum_ks
      iv_total —— 各箱 iv_contrib 之和
      ks_binmax—— 箱级累计 KS 的最大值（曲线采样点，非精确值）
      monotonic—— 坏账率随分数单调不减？破格箱号？
      edges    —— 切点（必须进信封：等频 10 箱 ≠ 等宽 10 箱）
    """
    pairs = sorted(zip(scores, labels), key=lambda t: t[0])
    n = len(pairs)
    if n == 0:
        return {"rows": [], "iv_total": None, "ks_binmax": None,
                "monotonic": {"monotonic": None, "breaks": []}, "edges": [],
                "n": 0}
    edges = quantile_edges([p[0] for p in pairs], n_bins)
    bad_total = sum(1 for _, y in pairs if y == 1)
    good_total = n - bad_total
    rows: list[dict] = []
    cum_bad = cum_good = 0
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        grp = [(s, y) for s, y in pairs
               if lo <= s < hi or (i == len(edges) - 2 and s == hi)]
        if not grp:
            continue
        g = len(grp)
        b = sum(1 for _, y in grp if y == 1)
        # WOE / IV 用 0.5 连续性修正：某箱 0 坏账时 ln(0) 会把整表打成 inf
        dist_b = (b + 0.5) / (bad_total + 0.5 * len(edges))
        dist_g = (g - b + 0.5) / (good_total + 0.5 * len(edges))
        woe = math.log(dist_b / dist_g)
        iv_c = (dist_b - dist_g) * woe
        cum_bad += b
        cum_good += g - b
        cum_ks = abs(cum_bad / max(bad_total, 1) - cum_good / max(good_total, 1))
        rows.append({
            "bin": i + 1, "lo": round(lo, 6), "hi": round(hi, 6),
            "n": g, "bad_n": b,
            "bad_rate": round(b / g, 4) if g else None,
            "woe": round(woe, 4), "iv_contrib": round(iv_c, 4),
            "lift": round((b / g) / (bad_total / n), 4) if bad_total and g else None,
            "cum_ks": round(cum_ks, 4),
        })
    ok_rows = [r for r in rows if r["bad_rate"] is not None]
    breaks = [rows[i]["bin"] for i in range(1, len(ok_rows))
              if ok_rows[i]["bad_rate"] < ok_rows[i - 1]["bad_rate"]]
    return {"rows": rows,
            "iv_total": round(sum(r["iv_contrib"] for r in rows), 4),
            "ks_binmax": round(max((r["cum_ks"] for r in rows), default=0.0), 4),
            "monotonic": {"monotonic": not breaks, "breaks": breaks},
            "edges": [round(e, 6) for e in edges],
            "n": n}


# ---------------------------------------------------------------------------
# ROC / KS 曲线：整条曲线不能进答案，只能进摘要 + 外卸文件
# ---------------------------------------------------------------------------
def curve_points(good_scores: Sequence[float], bad_scores: Sequence[float],
                 max_points: int = 100) -> dict:
    """按分数从高到低走一遍，返回 ROC/KS 曲线的下采样点与三个摘要量。

    ★为什么必须下采样：千万级借据的曲线有千万个点，塞进回答等于把数据库
      倒给模型；而 100 个点画出来的曲线与千万点画出来的**肉眼无差**。
      下采样只影响"画图好看"，不影响摘要量 —— ks_max / at_score / auc_area
      都用全量点算，不是用下采样点算的。

    ★为什么摘要只有三个数：
        ks_max    最优点处的累计差距（决策用）
        at_score  最优点在哪个分数（定阈值用；只报 ks 不报它，阈值没法落）
        auc_area  曲线下面积（与 AUC 的定义一致，用于交叉校验方向）

    ★方向约定同 auc()：**分数越高越坏**。所以曲线从高分端开始走，
      cum_bad 先涨、cum_good 后涨，ks = tpr - fpr 为正。

    ★ks_max 必须与 ks() / ks_statistic() **同一个数**，所以它单独算一遍：
      KS 的教科书定义是 max |F(x) - G(x)|，F(x)=P(分数 ≤ x)（含 x 处的并列）。
      而在 ROC 里 tpr 是"分数 ≥ s 的坏样本占比"= 1 − F(s−)，
      两者在**并列处分母不同**，会差出 0.04 这种量级。
      本项目实测：同一批数据 ks()=0.2127，按 ROC 累计算=0.1677 ——
      同一个问题两次问到两个 KS，两个都"算对了"。
      所以这里走两遍：上行走 KS，下行走 ROC，各按各自的定义。
    """
    ng, nb = len(good_scores), len(bad_scores)
    if ng == 0 or nb == 0:
        return {"points": [], "n_points": 0, "ks_max": None,
                "at_score": None, "auc_area": None}

    pairs = sorted([(s, 1) for s in bad_scores] + [(s, 0) for s in good_scores],
                   key=lambda t: -t[0])

    # ---- 先按不同分值分组（并列必须整体处理，不许拆开）----
    groups: list[tuple[float, int, int]] = []      # (score, n_bad, n_good)
    i = 0
    while i < len(pairs):
        s = pairs[i][0]
        j, b_in = i, 0
        while j < len(pairs) and pairs[j][0] == s:
            b_in += pairs[j][1]
            j += 1
        groups.append((s, b_in, (j - i) - b_in))
        i = j

    # ---- KS：从低分往高走，F(x)=P(分数 ≤ x) ----
    cum_b = cum_g = 0
    best_ks, best_score = -1.0, None
    for s, nb_i, ng_i in reversed(groups):
        cum_b += nb_i
        cum_g += ng_i
        d = abs(cum_b / nb - cum_g / ng)
        if d > best_ks:
            best_ks, best_score = d, s

    # ---- ROC：从高分往低走，tpr/fpr = 分数 ≥ s 的占比 ----
    pts: list[dict] = []
    cum_b = cum_g = 0
    area = 0.0
    prev_fpr = prev_tpr = 0.0
    for s, nb_i, ng_i in groups:
        cum_b += nb_i
        cum_g += ng_i
        tpr, fpr = cum_b / nb, cum_g / ng
        area += (fpr - prev_fpr) * (tpr + prev_tpr) / 2   # 梯形积分：横轴 fpr
        prev_fpr, prev_tpr = fpr, tpr
        pts.append({"score": round(s, 6), "tpr": round(tpr, 6),
                    "fpr": round(fpr, 6)})

    step = max(1, math.ceil(len(pts) / max_points))
    thin = pts[::step]
    if thin[-1] is not pts[-1]:
        thin.append(pts[-1])
    return {"points": thin, "n_points": len(pts),
            "ks_max": round(best_ks, 4), "at_score": round(best_score, 6),
            "auc_area": round(area, 4)}


# ---------------------------------------------------------------------------
# PSI：两个分布的差异（必须有基准分布，否则根本算不出）
# ---------------------------------------------------------------------------
def shares(scores: Sequence[float], edges: Sequence[float]) -> list[float]:
    """按给定切点算各箱占比（基准期与当期必须用**同一组切点**）。"""
    n = len(scores)
    if n == 0 or len(edges) < 2:
        return []
    cnt = [0] * (len(edges) - 1)
    for s in scores:
        for i in range(len(edges) - 1):
            if edges[i] <= s < edges[i + 1] or (i == len(edges) - 2 and s >= edges[-1]):
                cnt[i] += 1
                break
    return [c / n for c in cnt]


def psi(base_share: Sequence[float], cur_share: Sequence[float]) -> float | None:
    """PSI = Σ (cur - base) · ln(cur / base)，小于 0.1 稳定 / 0.25 以上显著漂移。"""
    if not base_share or not cur_share or len(base_share) != len(cur_share):
        return None
    total = 0.0
    for b, c in zip(base_share, cur_share):
        if b <= 0 and c <= 0:
            continue
        total += (c - b) * math.log((c + EPS) / (b + EPS))
    return abs(total)


# ---------------------------------------------------------------------------
# 置信区间：确定性 bootstrap（固定 seed，不污染任何全局随机状态）
# ---------------------------------------------------------------------------
def bootstrap_ci(stat_fn, scores: Sequence[float], labels: Sequence[int],
                 reps: int = 200, seed: int = 2026) -> list[float] | None:
    """给 KS / AUC 这类"没有干净解析方差"的统计量一个可复现的区间。

    为什么用固定 seed：报告与 Agent 必须给出**同一个区间**。
    每次跑都不一样，等于把不确定性变成了不可复现，评估器也就没法断言。
    """
    n = len(scores)
    if n < 30:
        return None
    rng = random.Random(seed)
    vals = []
    for _ in range(reps):
        idx = [rng.randrange(n) for _ in range(n)]
        s = [scores[i] for i in idx]
        y = [labels[i] for i in idx]
        v = stat_fn(s, y)
        if v is not None:
            vals.append(v)
    if len(vals) < 20:
        return None
    vals.sort()
    return [round(vals[int(0.025 * len(vals))], 4),
            round(vals[int(0.975 * len(vals)) - 1], 4)]
