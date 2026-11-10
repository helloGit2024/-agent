# -*- coding: utf-8 -*-
"""节点贡献排序与行动建议 —— 回答「哪些策略节点需要调整 / 该开 AB」。

════════════════════════════════════════════════════════════════════
为什么要单独一层（没有它会怎样）
════════════════════════════════════════════════════════════════════
诊断层给的是"每条策略各自怎么样"（KS、分箱、敏感表、方向判据），
但业务要的是"**先动哪一个**"。这两件事之间的差距，恰恰是分析师的
全部价值：不是会不会算，而是会不会排序、敢不敢说"这条别动"。

排序必须可解释、可复算。本模块用三个确定性的分项打分：

    风险信号（50%）  拒绝组/通过组坏账比，越高越值得动
    业务价值（30%）  影响的人数占比（通过率变化空间）
    可行性  （20%）  样本量够不够、数据质量允不允许

对应的动作只有五种（枚举写死，防止 LLM 自由发挥出"再观察观察"这种废话）：

    TIGHTEN          收紧（附候选阈值，走重放验证，实测精确）
    RELAX_AB         放宽 → 只能开 AB（增量人群没标签，离线给不出坏账率）
    FIX_DOWNSTREAM   放宽前先修下游（下游拦截率 >50% 时，放宽等于白花数据源费）
    FIX_DATA         先补数（缺失率过高，任何阈值结论都不可信）
    HOLD             不动（样本不足 / 信号在灰区 / 已经有效）

★与 V1 的关系：本模块不重算任何指标，只做"汇总 + 排序 + 动作映射"。
  这是 V2 的一条纪律：**新模块不许发明新口径，只能消费语义层的结果**。
"""
from __future__ import annotations

import json
from typing import Any

from .codes import POLICY_NAMES, SOURCES
from .metrics import (direction_judgement, marginal_hit,
                      threshold_sensitivity)

BAD_TOLERANCE = 0.20

ACTIONS = ("TIGHTEN", "RELAX_AB", "FIX_DOWNSTREAM", "FIX_DATA", "NEED_AB", "HOLD")
ACTION_TEXT = {
    "TIGHTEN": "调整阈值（收紧）",
    "RELAX_AB": "启动 AB 实验（放宽）",
    "FIX_DOWNSTREAM": "先修下游再放宽",
    "FIX_DATA": "先补数再判断",
    "NEED_AB": "需 AB 实验才能评估",
    "HOLD": "维持现状",
}


def _pairs(facts: list[dict], policy_id: str,
           label_by_apply: dict[str, int]) -> list[tuple]:
    out = []
    for f in facts:
        if f["policy_id"] != policy_id:
            continue
        v = json.loads(f["raw_value"]) if f["raw_value"] is not None else None
        out.append((v, f["value_state"], label_by_apply.get(f["apply_id"])))
    return out


def _policy_stats(facts: list[dict], policy_id: str) -> dict:
    rows = [f for f in facts if f["policy_id"] == policy_id
            and f["value_state"] != "NOT_EXECUTED"]
    n = len(rows) or 1
    return {"executed": len(rows),
            "fail": sum(1 for f in rows if f["result"] == "FAIL"),
            "miss": sum(1 for f in rows if f["value_state"] == "MISSING_SOURCE"),
            "miss_rate": round(sum(1 for f in rows
                                   if f["value_state"] == "MISSING_SOURCE") / n, 4)}


def estimate_downstream_kill(db, run_id: str, cfg: dict,
                             policy_id: str, candidate: float) -> dict:
    """为放宽候选做一次真实重放，算"下游拦截率"（数据源成本账的核心输入）。

    教学要点：这个数不能用估计 —— 放宽放进来的增量人群里有多少被下游拦掉，
    直接决定"放宽值不值"。所以宁可多跑一次重放（秒级），也不给假数。
    """
    from . import retro
    cand_run = f"{run_id}-rk-{policy_id}"
    cfg2 = {**cfg, policy_id: candidate}
    retro.run_offline(db, cand_run, "rank_relax", cfg2)
    cur_s2 = _stage_pass(db, run_id, "S2_AF")
    cand_s2 = _stage_pass(db, cand_run, "S2_AF")
    cur_final = _stage_pass(db, run_id, "S4_LIM")
    cand_final = _stage_pass(db, cand_run, "S4_LIM")
    new_in = cand_s2 - cur_s2
    killed = new_in - cand_final
    return {"candidate": candidate, "new_in_n": len(new_in),
            "killed_n": len(killed),
            "downstream_kill_rate": round(len(killed) / len(new_in), 4)
            if new_in else None,
            "pass_gain": round((len(cand_final) - len(cur_final))
                               / max(len(cur_final) + len(cur_s2), 1), 4)}


def _stage_pass(db, run_id: str, stage: str) -> set[str]:
    from .metrics import _stage_pass_set
    return _stage_pass_set(db.facts("run_id=?", (run_id,)), stage)


def rank(db, run_id: str, facts: list[dict], apps: list[dict],
         cfg: dict, policy_ids: list[str] | None = None) -> dict[str, Any]:
    """产出"节点贡献 + 建议动作"清单（按综合分降序）。

    参数 policy_ids 为空时，自动挑出所有"有数值阈值"的策略。
    """
    label_by_apply = {a["apply_id"]: a["bad_flag"] for a in apps
                     if a.get("bad_flag") is not None}
    n_total = len(apps) or 1
    ids = policy_ids or [p for p, v in cfg.items() if isinstance(v, (int, float))]
    avg_amount = (sum(a["amount_applied"] for a in apps) / n_total) if apps else 0.0

    items = []
    for pid in ids:
        thr = cfg.get(pid)
        if not isinstance(thr, (int, float)):
            continue
        pairs = _pairs(facts, pid, label_by_apply)
        if not pairs:
            continue
        st = _policy_stats(facts, pid)
        # ---- 方向：先看"边际档"而不是只看整体比值 ----
        # 教学要点（V1 的判据在这里被修正）：整体比值"拒绝组 vs 通过组"只能说明
        # 阈值有没有作用，不能回答"该不该动"。真正的问题是**紧挨着阈值的那一档**
        # 风险如何：
        #   边际档（刚被放行的那一档）坏账率 > 容忍线 → 该收紧；
        #   边际档 ≈ 通过组水平 → 已经到位，别动；
        #   拒绝组整体不比通过组高 → 白拒，该放宽。
        band = _marginal_band(pairs, thr)
        dj = direction_judgement(pairs, thr)
        # 反事实标签：拒绝组里有多少人"从未放款"（= 离线根本没法评估）
        unlabeled_rej = sum(1 for v, st, y in pairs
                            if st == "OK" and v is not None and v > thr and y is None)
        labeled_rej = sum(1 for v, st, y in pairs
                          if st == "OK" and v is not None and v > thr and y is not None)
        labeled_pass = sum(1 for v, st, y in pairs
                           if st == "OK" and v is not None and v <= thr and y is not None)
        mh = marginal_hit(facts, pid, label_by_apply)

        # ---- 分项打分（全部 0~1，透明可复算）----
        ratio = dj.get("ratio") or 1.0
        risk = min(max(ratio - 1.0, 0.0), 2.0) / 2.0          # 比值 1→3 映射 0→1
        volume = st["fail"] / n_total if st["executed"] else 0.0
        feas = min(st["executed"] / 30.0, 1.0)
        if dj["direction"] == "HOLD":
            volume *= 0.5          # 不动的人，业务价值折半（但保留可见性）
        score = round(0.5 * risk + 0.3 * volume + 0.2 * feas, 4)

        action, effect, extra = "HOLD", "无显著信号，暂不建议动作", {}
        # 规则顺序即优先级：数据问题 > 无标签 > 边际证据 > 观望
        if st["miss_rate"] >= 0.05:
            action = "FIX_DATA"
            effect = ("该策略执行样本缺失率偏高（≥5%），缺失期间判 UNKNOWN，"
                      "阈值结论不可信；先补数再判断")
        elif (band["n"] >= 30 and band["bad_rate"] is not None
              and band["bad_rate"] >= BAD_TOLERANCE):
            # 收紧只需要"边际档"的标签（这一档现在是放款的、有标签），
            # 所以即使拒绝组完全没标签，收紧方向依然可以离线评估 ——
            # 这正是"收紧好算、放宽难算"在排序层的体现。
            action = "TIGHTEN"
            cand = thr - 1 if isinstance(thr, int) else round(thr * 0.9, 4)
            extra = {"candidate_threshold": cand}
            effect = (f"紧邻阈值的边际档（{band['label']}）坏账率越过容忍线，"
                      f"收紧到 {cand} 可砍掉这一档；重放后为实测精确值")
        elif labeled_rej < 30 and unlabeled_rej > 0:
            # ★离线回测最硬的一条约束：阈值没变过的策略，拒绝组当年就被拒、
            #   从未放款 → 永远没有标签。放宽方向任何离线结论都是编的，
            #   只能走 AB / 回捞。
            action = "NEED_AB"
            effect = ("拒绝组从未放款（无表现期标签），离线无法评估该策略阈值；"
                      "若要判断，需 AB 实验或小流量回捞取得真实标签")
        elif labeled_pass >= 30 and labeled_rej >= 30:
            if dj["direction"] == "RELAX":
                action = "RELAX_AB"
                effect = ("拒绝组整体风险不高于通过组 —— 疑似白拒；"
                          "放宽后的增量人群没有标签，坏账率只能由 AB 实验给出")
                cand = _read_relax_candidate(pairs, thr)
                if cand is not None:
                    kill = estimate_downstream_kill(db, run_id, cfg, pid, cand)
                    extra = {"candidate_threshold": cand, "downstream": kill}
                    if (kill["downstream_kill_rate"] or 0) > 0.5:
                        action = "FIX_DOWNSTREAM"
                        effect += ("；但下游拦截率超过一半 —— 增量人群大半被下游拦掉，"
                                   "放宽只白花数据源费，应先调下游环节")
                    else:
                        effect += "；下游拦截率可接受，可开 AB"
            else:
                action = "HOLD"
                effect = ("边际档坏账率未越过容忍线、拒绝组风险确实更高 —— "
                          "当前阈值已到位，不建议再动")
        elif dj["direction"] == "HOLD":
            effect = "标签样本不足（通过/拒绝组均需 ≥30 例），暂不建议动作"

        items.append({
            "policy_id": pid, "name": POLICY_NAMES.get(pid, pid),
            "stage": next((f["stage"] for f in facts if f["policy_id"] == pid), "-"),
            "threshold": thr, "executed": st["executed"], "rejected": st["fail"],
            "miss_rate": st["miss_rate"],
            "ratio": dj.get("ratio"), "direction": dj["direction"],
            "score": score, "score_parts": {"risk": round(risk, 3),
                                            "volume": round(volume, 3),
                                            "feasibility": round(feas, 3)},
            "action": action, "action_text": ACTION_TEXT[action],
            "expected_effect": effect, "marginal_hit": mh, "extra": extra,
            # 边际档证据（叙述层从这里取数字，不自己算）
            "band": band, "labeled_pass_n": labeled_pass,
            "labeled_rej_n": labeled_rej, "unlabeled_rej_n": unlabeled_rej,
        })
    items.sort(key=lambda x: -x["score"])
    for i, it in enumerate(items, 1):
        it["rank"] = i
    return {"items": items, "n": len(items), "config_version_hash": _hash(cfg),
            "avg_amount": round(avg_amount, 2),
            "action_counts": {a: sum(1 for x in items if x["action"] == a)
                              for a in ACTIONS}}


def _marginal_band(pairs, thr) -> dict:
    """紧邻阈值、仍然被放行的那一档（整数策略取 thr-1 < v <= thr）。

    为什么是这一档：收紧阈值只会影响"刚好卡在线上"的人。
    看整体拒绝组只会得出"阈值有效"（几乎永远成立），
    看边际档才能回答"再紧一档值不值"。
    """
    if isinstance(thr, int):
        lo, label = thr - 1, f"{thr - 1} < v <= {thr}"
    else:
        lo, label = thr * 0.9, f"{(thr * 0.9):.2f} < v <= {thr:.2f}"
    ys = [y for v, st, y in pairs
          if st == "OK" and v is not None and y is not None and lo < v <= thr]
    return {"n": len(ys), "bad_n": sum(ys),
            "bad_rate": round(sum(ys) / len(ys), 4) if ys else None, "label": label}


def _read_candidate(pairs, thr) -> float:
    """收紧候选读点：边际坏账率越过容忍线的最松阈值（最多收紧三档）。"""
    floor_t = max(0, thr - 3) if isinstance(thr, int) else thr * 0.8
    sens = threshold_sensitivity(pairs, thr)
    elig = [r for r in sens if floor_t <= r["threshold"] < thr
            and r["marginal_bad_rate"] is not None
            and r["marginal_bad_rate"] >= BAD_TOLERANCE
            and r["rejected_n"] >= 30]
    if elig:
        return max(elig, key=lambda r: r["threshold"])["threshold"]
    return thr - 1 if isinstance(thr, int) else round(thr * 0.9, 4)


def _read_relax_candidate(pairs, thr):
    """放宽候选：往松一档（整数策略）或 20%（连续策略）。"""
    if isinstance(thr, int):
        return thr + 1 if thr < 10 else None
    return round(thr * 1.2, 4)


def _hash(cfg: dict) -> str:
    import hashlib
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:8]
