#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""策略回测分析 Agent · 企业版 V1 —— LangGraph 编排层。

═══════════════════════════════════════════════════════════════════
这份代码和 approval_lab/v2_langgraph.py 是什么关系？
═══════════════════════════════════════════════════════════════════
底层设计大量复用（判断依据：两类系统共享同一类线上事故）：

  直接复用的机制（防的事故完全相同）：
    guard 统一收口（异常兜底 + PASSTHROUGH 放行清单）→ 节点炸了不白屏
    工厂生成节点（不子图，避开 operator.add 双写）    → 可复用可单测
    interrupt 拆 prepare/gate（副作用前置）           → 人工闸门零副作用
    快照表是唯一事实                                  → 重放/复用/审计一致
    哈希链审计 + 三件套指纹                           → 可复现可追责
    UNKNOWN 是结论不是异常                            → 缺数据绝不静默放行

  回测新增的机制（审批系统没有的问题）：
    第 0 步判决一致率核对   → 离线重放必须先证明自己复刻了线上
    policy_eval_fact 长表   → 分析单元是"策略×配置版本"，不是"案件"
    收紧/放宽方向分叉       → 收紧重放即精确，放宽只能给通过率账
    LMDI 精确可加归因       → 乘法结构加法分解，残差必须为 0
    数字登记簿 + 槽位渲染   → LLM 永远不碰数字
    两个环各限 2 次失败     → 降级交付，绝不死循环

图结构：
  START → intake → plan → consistency ─┬─(一致)→ diagnose ─┬─→ tighten_replay
                                       └─(不一致)→ report(降级)
         tighten_replay → relax_eval → gap_report → sign_gate(interrupt) → report → END

跑法（在本目录下，用装了 langgraph 的解释器）：
    python3 v1_backtest.py                # 全流程：诊断→重放→评估→人工签字→报告
    python3 v1_backtest.py --fail-diag    # 故障演练：诊断步注入失败，看限流降级
"""
from __future__ import annotations

import functools
import json
import sys
import time
from pathlib import Path
from typing import Annotated, Any, TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parent))

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from core.analysis import (MetricRegistry, render_report, rule_based_plan,
                           validate_plan)
from core.codes import (SOURCE_LEVEL, STAGE_ORDER, STAGE_SOURCES, SOURCES)
from core.db import Database
from core.metrics import (binstats, consistency_rate, direction_judgement,
                          first_reject, ks_statistic, lmdi_stage_attribution,
                          marginal_hit, relax_eval, threshold_sensitivity,
                          tighten_compare)
from core.policies import (DEFAULT_CONFIG, bundle_hash)
from core import retro

ART = Path("artifacts")
DB_PATH = ART / "backtest.db"
ENGINE = "V1-backtest"
MAX_STEP_FAILURES = 2            # 设计文档：两个环各限 2 次失败 → 降级交付
BAD_TOLERANCE = 0.20             # 业务容忍线：边际坏账率超过它才值得收紧


def _merge(a: dict, b: dict) -> dict:
    """dict 通道的 reducer：后写覆盖先写、同 key 的 dict 浅合并。
    教学要点（LangGraph 第一大坑）：往同一个 key 写东西必须想清楚合并语义，
    忘写 reducer 就是"莫名丢数据"。"""
    if not a:
        return b
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


class BacktestState(TypedDict, total=False):
    run_id: str
    plan: dict
    candidates: dict                      # {"tighten": cfg|None, "relax": cfg|None}
    consistency: dict
    funnel_current: list
    directions: list
    lmdi: dict
    tighten: dict
    relax: dict
    gaps: dict
    headline: dict
    report: dict
    review: dict | None
    review_payload: dict | None
    step_failures: int                    # 两个环共用的失败预算
    degraded: bool
    trace: Annotated[list[str], lambda a, b: (a or []) + (b or [])]
    findings: Annotated[dict, _merge]


# ---------------------------------------------------------------------------
# 内核助手：从事实表抽取分析原料（把 SQL 行变成 metrics 的入参）
# ---------------------------------------------------------------------------

def _bad_by_apply(db) -> dict[str, int]:
    """apply_id -> bad_flag（只有放款的人非空）。整个指标层的"标签真相"。"""
    return {r["apply_id"]: r["bad_flag"]
            for r in db.applicants() if r["bad_flag"] is not None}


def _policy_pairs(db, run_id: str, policy_id: str) -> list[tuple]:
    """事实表 -> [(raw_value, value_state, bad_flag), ...]。

    教学要点：bad_flag 用 LEFT JOIN 的语义 —— 没放款的人是 None（无标签），
    分箱时进 UNLABELED 桶，绝不冒充 0（好客户）。
    """
    labels = _bad_by_apply(db)
    rows = db.facts("run_id=? AND policy_id=?", (run_id, policy_id))
    out = []
    for f in rows:
        v = json.loads(f["raw_value"]) if f["raw_value"] is not None else None
        out.append((v, f["value_state"], labels.get(f["apply_id"])))
    return out


def _pass_set(db, run_id: str) -> set[str]:
    """某次重放里最终通过 S4 的人群（该环节全部策略 PASS，口径见 metrics）。"""
    from core.metrics import _stage_pass_set
    return _stage_pass_set(db.facts("run_id=?", (run_id,)), "S4_LIM")


def _stage_pass_rates(db, run_id: str) -> dict[str, float]:
    """各环节"环节内通过率"（分母=进入人数），LMDI 的输入。"""
    rows = db.facts("run_id=?", (run_id,))
    by_stage: dict[str, set] = {}
    for f in rows:
        by_stage.setdefault(f["stage"], set()).add(f["apply_id"])
    out = {}
    for stage in STAGE_ORDER:
        entered = by_stage.get(stage, set())
        if not entered:
            continue
        passed = {f["apply_id"] for f in rows
                  if f["stage"] == stage and f["result"] == "PASS"}
        # 环节通过 = 本环节全部策略 PASS = 申请人进入了下一环节（或终局通过）
        nxt = STAGE_ORDER[STAGE_ORDER.index(stage) + 1] if stage != STAGE_ORDER[-1] else None
        if nxt:
            entered_next = by_stage.get(nxt, set())
            out[stage] = len(entered_next) / len(entered) if entered else 0.0
        else:
            out[stage] = len(_pass_set(db, run_id)) / len(entered)
    return out


# ---------------------------------------------------------------------------
# 引擎：依赖容器 + 节点工厂（与 approval_lab 同款骨架）
# ---------------------------------------------------------------------------

class Engine:
    """把 db/registry/审计收进一个对象，节点保持"读 state -> 返回 delta"。"""

    # PASSTHROUGH 放行清单（照抄 approval_lab 教训 3）：
    # GraphBubbleUp 是 interrupt() 的载体，被 guard 兜掉 = 人工闸门直接消失、
    # 变成一条"ERROR 降级"。这个坑我们自己刚踩过：V1 初版 PASSTHROUGH=()，
    # sign_gate 一挂起就被兜成 GraphInterrupt 异常，闸门整个失效。
    PASSTHROUGH = (GraphBubbleUp,)

    def __init__(self, db: Database, inject_fail: bool = False):
        self.db = db
        self.inject_fail = inject_fail
        self.registry = MetricRegistry()

    def audit(self, run_id: str):
        return self.db.auditor(run_id)

    def guard(self, node: str):
        """统一收口（与 approval_lab 同款，三不变式少一条就出事故）：
        1. PASSTHROUGH 继续上抛 —— 不吞框架控制流信号；
        2. 其余异常 -> step_failures+1 -> 超过 MAX_STEP_FAILURES 降级；
        3. 失败不冒充成功：trace 里必须留下失败原因。"""
        def deco(fn):
            @functools.wraps(fn)
            def wrapper(state: BacktestState) -> dict[str, Any]:
                rid = state.get("run_id", "-")
                try:
                    return fn(state)
                except self.PASSTHROUGH:
                    raise
                except Exception as e:                      # noqa: BLE001
                    fails = int(state.get("step_failures") or 0) + 1
                    self.audit(rid).append("step.failed", {
                        "node": node, "error": f"{type(e).__name__}: {e}"})
                    degraded = fails >= MAX_STEP_FAILURES
                    return {"step_failures": fails, "degraded": degraded,
                            "trace": [f"[{node}] ✘ {type(e).__name__}: {e}"
                                      f"（失败 {fails}/{MAX_STEP_FAILURES}"
                                      f"{'，降级交付' if degraded else ''}）"]}
            return wrapper
        return deco

    # ---------------- 节点 ----------------
    def n_intake(self, state: BacktestState) -> dict[str, Any]:
        rid = state["run_id"]
        db = self.db
        n = len(db.applicants())
        self.audit(rid).append("backtest.opened", {
            "n_applicants": n, "policy_hash": bundle_hash(DEFAULT_CONFIG)})
        return {"trace": [f"[intake] 回测运行 {rid} 装载：历史申请 {n} 单，"
                          f"策略基线 hash={bundle_hash(DEFAULT_CONFIG)[:8]}…"]}

    def n_plan(self, state: BacktestState) -> dict[str, Any]:
        """分析计划节点。V1 用规则计划器（可复现优先）；
        LLM 计划器在 V2 引入，届时 validate_plan 就是它的输出闸门。"""
        rid = state["run_id"]
        plan = rule_based_plan()
        plan["backtest_id"] = rid
        # 放宽候选来自分析师假设（放宽方向没有标签，候选无法从分箱读出 ——
        # 这本身就是"收紧能算、放宽要实验"的数据侧体现）
        plan["candidates"]["relax"] = {"P_AF_003": 5}
        ok, errors = validate_plan(plan)
        if not ok:
            raise ValueError(f"分析计划非法（LLM 计划会被这里拦下）: {errors}")
        self.audit(rid).append("plan.validated", {"steps": plan["steps"]})
        return {"plan": plan, "candidates": plan["candidates"],
                "trace": [f"[plan] 分析计划校验通过：{' → '.join(plan['steps'])}"]}

    def n_consistency(self, state: BacktestState) -> dict[str, Any]:
        """第 0 步：判决一致率核对。过不了这一步，后面全部统计作废。"""
        rid = state["run_id"]
        if self.inject_fail:
            raise RuntimeError("（演练）离线重放引擎失败：模拟指标层抛异常")
        # 先离线重放一遍基线配置
        offline = retro.run_offline(self.db, rid, "default", DEFAULT_CONFIG,
                                    auditor=self.audit(rid))
        online_facts = self.db.facts("run_id=?", (retro.ONLINE_RUN,))
        offline_facts = self.db.facts("run_id=?", (rid,))
        con = consistency_rate(online_facts, offline_facts)
        self.registry.put("consistency_rate", con["rate"], "MEASURED",
                          "线上判决 vs 离线重放")
        self.registry.put("consistency_gap_n", con["data_gap"], "MEASURED",
                          "线上数据源故障缺口单数")
        self.audit(rid).append("consistency.checked", {
            "rate": con["rate"], "mismatch": con["mismatch"], "gap": con["data_gap"]})
        lines = [f"[check] 判决一致率 {con['rate']:.1%}"
                 f"（比对 {con['compared']} 条策略判决，"
                 f"不一致 {con['mismatch']}，线上数据缺口 {con['data_gap']}）"]
        if con["mismatch"] > 0:
            # 不一致 ≠ 数据缺口：这是致命的，必须降级（缺口可以补，错算必须修）
            lines.append("[check] ✘ 存在判决不一致，回测统计不可信 → 降级交付")
        return {"consistency": con, "degraded": con["mismatch"] > 0,
                "findings": {"consistency": con}, "trace": lines}

    def _n_diagnose(self, state: BacktestState) -> dict[str, Any]:
        """单策略诊断：KS / 分箱 / 敏感性 / 方向判据（对全部数值型策略跑一遍）。"""
        rid = state["run_id"]
        db = self.db
        labels = _bad_by_apply(db)
        stage_rates = _stage_pass_rates(db, rid)
        directions = []
        # 逐策略诊断（教学：只对"阈值型"策略做数值诊断，布尔型策略无敏感性可言）
        NUMERIC_POLICIES = ["P_CR_001", "P_CR_002", "P_CR_003", "P_CR_004",
                            "P_AF_002", "P_AF_003"]
        diagnostics = {}
        for pid in NUMERIC_POLICIES:
            pairs = _policy_pairs(db, rid, pid)
            if not pairs:
                continue
            thr = DEFAULT_CONFIG.get(pid)
            if not isinstance(thr, (int, float)):
                continue
            sens = threshold_sensitivity(pairs, thr)
            # KS（ties 校正版：策略变量多为整数计数，必须看 argmax 而不是只看 D 值）
            ok_pairs = [(v, y) for v, st, y in pairs
                        if st == "OK" and v is not None and y is not None]
            ks = ks_statistic([v for v, y in ok_pairs if y == 0],
                              [v for v, y in ok_pairs if y == 1])
            # 分箱坏账率（缺失/哨兵单独成箱 —— 五态的价值在这里变现）
            bins = binstats(pairs)
            diagnostics[pid] = {"threshold": thr, "sensitivity": sens,
                                "ks": ks, "bins": bins}
            dj = direction_judgement(pairs, thr)
            dj["policy_id"] = pid
            # 收紧候选自动读点：找"边际坏账率越过容忍线"的最松阈值
            # （设计文档 05 章⑤：候选阈值是从敏感性表里读出来的，不是试出来的）
            # 合理性约束：最多收紧三档（整型）或 20%（连续值），且不收紧到 0 以下 ——
            # 噪声也能让边际坏账率偶然超线，无界的"读点"会给出荒谬候选
            if dj["direction"] == "HOLD":
                if isinstance(thr, int):
                    floor_t = max(0, thr - 3)
                else:
                    floor_t = thr * 0.8
                eligible = [r for r in sens
                            if floor_t <= r["threshold"] < thr
                            and r["marginal_bad_rate"] is not None
                            and r["marginal_bad_rate"] >= BAD_TOLERANCE
                            and r["rejected_n"] >= 30]
                if eligible:
                    best = max(eligible, key=lambda r: r["threshold"])
                    dj["direction"] = "TIGHTEN"
                    dj["candidate_threshold"] = best["threshold"]
                    dj["reason"] = (f"敏感性表读点：阈值压到 {best['threshold']} 后，"
                                    f"新增拒绝人群的实测坏账率 {best['marginal_bad_rate']:.1%}"
                                    f" 越过容忍线 {BAD_TOLERANCE:.0%}（样本 {best['rejected_n']}）")
            directions.append(dj)
        # 首拒归因 + 边际贡献（策略层归因双口径）
        facts = db.facts("run_id=?", (rid,))
        first_map = {}
        for f in facts:
            if f["result"] == "FAIL":
                first_map.setdefault(f["apply_id"], f["policy_id"])
        final_stage = {}
        for f in facts:
            final_stage.setdefault(f["apply_id"], f["stage"])
        fr = first_reject(final_stage, first_map)
        if fr["items"]:
            self.registry.put("first_reject_top1", fr["items"][0]["policy_id"], "MEASURED")
        marginal = {pid: marginal_hit(facts, pid, labels)
                    for pid in ["P_CR_001", "P_AF_003"]}
        self.audit(rid).append("diagnose.done", {
            "directions": {d["policy_id"]: d["direction"] for d in directions}})
        lines = [f"[diag] 诊断完成：{len(directions)} 条数值型策略；"
                 f"方向判定 " + "，".join(f"{d['policy_id']}={d['direction']}"
                                          for d in directions)]
        return {"directions": directions, "findings": {
            "directions": directions, "diagnostics": diagnostics,
            "first_reject": fr, "marginal": marginal, "funnel": stage_rates,
        }, "trace": lines}

    def _n_tighten(self, state: BacktestState) -> dict[str, Any]:
        """收紧 what-if：换候选配置重放，全部指标是实测精确值。"""
        rid = state["run_id"]
        db = self.db
        # 从诊断结论里取收紧候选（可能没有 —— 没有信号就不重放，别浪费算力）
        tighten_dirs = [d for d in state.get("directions", [])
                        if d["direction"] == "TIGHTEN" and "candidate_threshold" in d]
        if not tighten_dirs:
            return {"trace": ["[tighten] 无收紧候选，跳过重放"], "findings": {"tighten": None}}
        target = tighten_dirs[0]
        pid = target["policy_id"]
        cand_cfg = {**DEFAULT_CONFIG, pid: target["candidate_threshold"]}
        cand_run = f"{rid}-tighten"
        retro.run_offline(db, cand_run, "candidate_tighten", cand_cfg,
                          auditor=self.audit(rid))
        cur_pass = _pass_set(db, rid)
        cand_pass = _pass_set(db, cand_run)
        labels = _bad_by_apply(db)
        cmp_ = tighten_compare(labels, cur_pass, cand_pass,
                               n_total=len(db.applicants()))
        self.registry.put("tighten_pass_loss", cmp_["pass_loss_pct"], "MEASURED")
        self.registry.put("tighten_bad_rate", cmp_["bad_rate_candidate"], "MEASURED")
        self.registry.put("tighten_cut_bad_rate", cmp_["cut_bad_rate"], "MEASURED")
        # LMDI 环节归因：before/after 两份环节通过率，乘法精确分解
        lmdi = lmdi_stage_attribution(_stage_pass_rates(db, rid),
                                      _stage_pass_rates(db, cand_run))
        self.registry.put("lmdi_delta", lmdi["delta"], "MEASURED")
        self.audit(rid).append("tighten.replayed", {
            "policy": pid, "to": target["candidate_threshold"],
            "result": cmp_, "lmdi": lmdi})
        lines = [f"[tighten] {pid} {DEFAULT_CONFIG[pid]}→{target['candidate_threshold']} 重放完成："
                 f"通过率损失 {cmp_['pass_loss_pct']:.1%}，"
                 f"被砍人群实测坏账率 {cmp_['cut_bad_rate']:.1%}（精确值，非估计）",
                 f"[lmdi] 环节贡献分解：Σ贡献−ΔP = {lmdi['residual']}（精确可加校验"
                 f"{'通过' if lmdi['additive_check_passed'] else '失败✘'}）"]
        return {"tighten": cmp_, "lmdi": lmdi,
                "findings": {"tighten": cmp_, "lmdi": lmdi}, "trace": lines}

    def _n_relax(self, state: BacktestState) -> dict[str, Any]:
        """放宽评估：通过率账算得精确，坏账账只能标注"不可得"。

        这是整个回测系统最重要的一次"诚实"：
        通过率增量是精确值（特征都在，重放即得）；
        增量人群没放过款、没有表现期、没有 bad_flag ——
        报告里这一格必须写 UNAVAILABLE，任何"估计坏账率下降"都是骗人。
        """
        rid = state["run_id"]
        db = self.db
        relax_cfg = (state.get("candidates") or {}).get("relax")
        if not relax_cfg:
            return {"trace": ["[relax] 无放宽候选，跳过"], "findings": {"relax": None}}
        cand_run = f"{rid}-relax"
        retro.run_offline(db, cand_run, "candidate_relax", {**DEFAULT_CONFIG, **relax_cfg},
                          auditor=self.audit(rid))
        labels = _bad_by_apply(db)
        res = relax_eval(db.facts("run_id=?", (rid,)),
                         db.facts("run_id=?", (cand_run,)), labels)
        # 数据源成本账（设计文档"放宽对称账"）：
        # 增量人群每多走一个环节，就多一次该环节数据源调用费
        incremental = res["incremental_n"]
        cost = 0.0
        if incremental:
            # 粗算：增量人群平均会走到 S3（被下游拦的人），按 S2+S3 的外部源单价估算
            cost = sum(SOURCES[s].unit_cost for s in
                       STAGE_SOURCES["S2_AF"] + STAGE_SOURCES["S3_CR"])
        extra_cost = round(cost * incremental, 2)
        self.registry.put("relax_pass_gain", res["pass_gain_pct"], "MEASURED")
        self.registry.put("relax_downstream_kill", res["downstream_kill_rate"], "MEASURED")
        self.registry.put("relax_bad_level", res["bad_rate_level"],
                          res["bad_rate_level"], "增量人群坏账可得性")
        self.registry.put("relax_extra_cost", extra_cost, "MEASURED", "元")
        self.audit(rid).append("relax.evaluated", {"result": res, "extra_cost": extra_cost})
        lines = [f"[relax] 放宽候选 {relax_cfg}：通过率 +{res['pass_gain_pct']:.1%}（精确），"
                 f"增量 {res['incremental_n']} 人，下游拦截率 "
                 f"{(res['downstream_kill_rate'] or 0):.1%}，"
                 f"新增数据源成本 ≈{extra_cost:.0f} 元",
                 f"[relax] 增量人群坏账标签可得性：{res['bad_rate_level']}"
                 f"（{res['bad_label_available_n']}/{res['incremental_n']} 有标签）"
                 f" → 坏账结论不可离线给出，需 AB 实验"]
        return {"relax": res, "findings": {"relax": res}, "trace": lines}

    def _n_gap(self, state: BacktestState) -> dict[str, Any]:
        """补数缺口清单（设计文档第 08 章：人工确认 + 外发邮件的输入）。"""
        rid = state["run_id"]
        con = state.get("consistency", {})
        gaps = con.get("gaps", [])
        items = []
        for g in gaps:
            # 缺口 -> 需要补哪个源（策略依赖反查）+ 补一次的单价
            from core.codes import POLICY_REQUIRES
            srcs = [s for s in POLICY_REQUIRES[g["policy_id"]] if s.startswith("DS_")]
            for s in srcs:
                items.append({"apply_id": g["apply_id"], "policy_id": g["policy_id"],
                              "source": s, "unit_cost": SOURCES[s].unit_cost,
                              "action": f"补拉 {s} 快照后重放该申请"})
        total_cost = round(sum(i["unit_cost"] for i in items), 2)
        self.registry.put("gap_n", len(items), "MEASURED", "待补快照数")
        self.registry.put("gap_cost", total_cost, "MEASURED", "补数成本（元）")
        self.audit(rid).append("gap.reported", {"items": items})
        lines = ([f"[gap] 补数缺口 {len(items)} 项（预估成本 {total_cost:.2f} 元）"
                  f" —— 待人工确认后外发补数邮件"] if items
                 else ["[gap] 无补数缺口"])
        # 放宽方向的"标签缺口"也进清单（但它是实验缺口，不是补数能解决的）
        if state.get("relax") and state["relax"].get("bad_rate_level") == SOURCE_LEVEL["UNAVAILABLE"]:
            lines.append("[gap] ⚠ 放宽增量人群的坏账标签属于实验缺口：补数补不出标签，"
                         "只能走 AB 实验（见实验设计卡）")
        return {"gaps": {"items": items, "total_cost": total_cost},
                "findings": {"gaps": {"items": items, "total_cost": total_cost}},
                "trace": lines}

    def _n_report(self, state: BacktestState) -> dict[str, Any]:
        """报告渲染：数字全部来自登记簿（槽位注入），可溯源校验在这里完成。"""
        rid = state["run_id"]
        db = self.db
        default_head = db.conn.execute(
            "SELECT headline FROM run WHERE run_id=?", (rid,)).fetchone()
        head = json.loads(default_head["headline"]) if default_head and default_head["headline"] else {}
        # 补充放款人群实测坏账率（带来源等级 —— 报告里每个坏账率都要标）
        labels = _bad_by_apply(db)
        bad_rate = round(sum(1 for v in labels.values() if v == 1) / len(labels), 4) if labels else None
        findings = dict(state.get("findings") or {})
        findings["headline"] = {
            "n": head.get("n"), "pass_rate_current": head.get("pass_rate"),
            "pass_rate_level": SOURCE_LEVEL["MEASURED"],
            "bad_rate_measured": bad_rate, "bad_rate_level": SOURCE_LEVEL["MEASURED"],
        }
        self.registry.put("pass_rate_current", head.get("pass_rate"), "MEASURED")
        self.registry.put("bad_rate_measured", bad_rate, "MEASURED")
        report = render_report(findings, self.registry,
                               narrative=state.get("review", {}).get("note"))
        self.audit(rid).append("report.rendered", {
            "metric_count": report["compliance"]["metric_count"],
            "degraded": bool(state.get("degraded"))})
        self.audit(rid).append("backtest.closed", {"degraded": bool(state.get("degraded"))})
        ok, n = self.audit(rid).verify()
        lines = [f"[report] 报告渲染完成：登记簿 {report['compliance']['metric_count']} 个数字全部可溯源；"
                 f"审计链 {n} 条完整性 {'✔' if ok else '✘'}",
                 f"[final] 降级标记：{'是（见失败 trace）' if state.get('degraded') else '否'}"]
        return {"report": report, "findings": {"headline": findings["headline"]},
                "trace": lines}

    # ---------------- 组装（工厂 + 条件边，与 approval_lab 同款） ----------------
    def build(self):
        g = StateGraph(BacktestState)
        g.add_node("intake", self.guard("intake")(self.n_intake))
        g.add_node("plan", self.guard("plan")(self.n_plan))
        g.add_node("consistency", self.guard("consistency")(self.n_consistency))
        g.add_node("diagnose", self.guard("diagnose")(self._n_diagnose))
        g.add_node("tighten_replay", self.guard("tighten_replay")(self._n_tighten))
        g.add_node("relax_eval", self.guard("relax_eval")(self._n_relax))
        g.add_node("gap_report", self.guard("gap_report")(self._n_gap))
        g.add_node("sign_prepare", self.guard("sign_prepare")(self._n_sign_prepare))
        g.add_node("sign_gate", self.guard("sign_gate")(self._n_sign_gate_impl))
        g.add_node("report", self.guard("report")(self._n_report))

        g.add_edge(START, "intake")
        g.add_edge("intake", "plan")
        g.add_edge("plan", "consistency")
        # 第 0 步是总闸：不一致 -> 直接降级到 report（跳过一切统计）
        g.add_conditional_edges(
            "consistency",
            lambda s: "diagnose" if not s.get("degraded") else "report",
            {"diagnose": "diagnose", "report": "report"})
        g.add_edge("diagnose", "tighten_replay")
        g.add_edge("tighten_replay", "relax_eval")
        g.add_edge("relax_eval", "gap_report")
        g.add_edge("gap_report", "sign_prepare")
        g.add_edge("sign_prepare", "sign_gate")
        g.add_edge("sign_gate", "report")
        g.add_edge("report", END)
        return g

    def _n_sign_prepare(self, state: BacktestState) -> dict[str, Any]:
        """签字闸门的 prepare 节点：组 payload、落审计（副作用全部在这，
        interrupt 节点保持零副作用 —— resume 从节点头重放，副作用会执行两遍）。"""
        rid = state["run_id"]
        tighten_dirs = [d for d in state.get("directions", [])
                        if d["direction"] == "TIGHTEN"]
        proposals = [{"policy_id": d["policy_id"],
                      "from": DEFAULT_CONFIG.get(d["policy_id"]),
                      "to": d.get("candidate_threshold")} for d in tighten_dirs]
        payload = {
            "question": "请对阈值调整方案签字（收紧为实测精确值；放宽仅通过率账）",
            "proposals": proposals,
            "relax_candidate": (state.get("candidates") or {}).get("relax"),
            "gaps_pending": len(state.get("gaps", {}).get("items", [])),
        }
        self.audit(rid).append("sign.requested", payload, idem_key=f"sign.requested:{rid}")
        return {"review_payload": payload,
                "trace": [f"[sign] 待签字方案：{proposals or '无收紧项'}；"
                          f"放宽候选 {payload['relax_candidate']}；"
                          f"补数待确认 {payload['gaps_pending']} 项"]}

    def _n_sign_gate_impl(self, state: BacktestState) -> dict[str, Any]:
        """签字闸门：零副作用纯 interrupt（副作用全部在 prepare，resume 不重放）。"""
        rid = state["run_id"]
        answer = interrupt(state.get("review_payload") or {"question": "签字"})
        self.audit(rid).append("sign.answered", answer, idem_key=f"sign.answered:{rid}")
        return {"review": answer,
                "trace": [f"[sign] 人工结论：{answer.get('decision')}"
                          f"（{answer.get('approver', '-')}）"]}


# ---------------------------------------------------------------------------
# 驱动层：幂等 + interrupt 处理（与 approval_lab run_one 同款骨架）
# ---------------------------------------------------------------------------

def auto_signer(payload: dict) -> dict[str, Any]:
    """脚本化的"策略分析师"。真实系统里这是审批工作台的一个确认页。"""
    n = len(payload.get("proposals", []))
    return {"decision": "APPROVE", "approver": "策略分析师-A1024",
            "note": f"收紧方案 {n} 项同意进入重放验证；放宽方案转实验设计卡"}


def run(graph, engine: Engine, db: Database, auto: bool = True) -> dict[str, Any]:
    rid = f"BT-{time.strftime('%Y%m%d-%H%M%S')}"
    cfg = {"configurable": {"thread_id": rid}}
    t0 = time.perf_counter()
    out: dict[str, Any] = graph.invoke({"run_id": rid, "trace": [], "findings": {},
                                        "step_failures": 0}, cfg)
    rounds = 0
    while "__interrupt__" in out and auto and rounds < 2:
        rounds += 1
        payload = out["__interrupt__"][0].value
        answer = auto_signer(payload)
        out = graph.invoke(Command(resume=answer), cfg)
    elapsed = (time.perf_counter() - t0) * 1000
    for line in out.get("trace", []):
        print("  " + line)
    print(f"\n  耗时 {elapsed:.0f}ms ｜ 人工闸门轮次 {rounds}")
    return out


def _pct(v) -> str:
    """None 安全的百分号格式化（报告渲染层的基本功）。"""
    return f"{v:.1%}" if isinstance(v, (int, float)) else "-"


def _print_report(report: dict) -> None:
    print("\n" + "═" * 78)
    print("回测报告（管理层版）")
    print("═" * 78)
    for row in report["exec"]:
        val = "-" if row["value"] is None else (
            _pct(row["value"]) if isinstance(row["value"], float) else str(row["value"]))
        print(f"  {row['metric']:<28} {val:>10}  [{row['level']}]")
    print("\n策略版要点（节选）")
    if report["policy"].get("tighten"):
        t = report["policy"]["tighten"]
        print(f"  收紧：砍 {t['cut_n']} 人，其中实测坏账 {t['cut_bad_n']} 人"
              f"（坏账率 {_pct(t['cut_bad_rate'])}），放行坏账率 "
              f"{_pct(t['bad_rate_current'])} → {_pct(t['bad_rate_candidate'])}")
    if report["policy"].get("relax"):
        r = report["policy"]["relax"]
        print(f"  放宽：增量 {r['incremental_n']} 人（通过率 +{_pct(r['pass_gain_pct'])}），"
              f"下游拦截率 {_pct(r['downstream_kill_rate'])}，"
              f"坏账可得性 {r['bad_rate_level']}")
    if report["policy"].get("lmdi"):
        l = report["policy"]["lmdi"]
        contribs = "，".join(f"{k} {v:+.4f}" for k, v in l["contributions"].items())
        print(f"  LMDI：ΔP={l['delta']:+.4f}；{contribs}；残差 {l['residual']}")
    gaps = report["policy"].get("gaps") or {}
    if gaps.get("items"):
        print(f"  补数缺口：{len(gaps['items'])} 项，预估 {gaps['total_cost']:.2f} 元")


def main() -> None:
    args = sys.argv[1:]
    inject_fail = "--fail-diag" in args
    if not ART.exists():
        ART.mkdir(parents=True)
    if DB_PATH.exists() and "--keep" not in args:
        DB_PATH.unlink()
    db = Database(DB_PATH)
    print("策略回测分析 Agent · 企业版 V1（LangGraph）")
    print("图：intake → plan → consistency → diagnose → tighten_replay"
          " → relax_eval → gap_report → sign_gate(interrupt) → report\n")
    # ---- 数据准备（一次性：生成历史 + 灌快照 + 写线上判决事实）----
    print("① 生成历史回测数据（600 单，含教学故事 A 收紧 / B 放宽 / C 数据质量）…")
    rows = retro.gen_population(600, seed=2026)
    retro.seed(db, rows)
    granted = sum(1 for r in rows if r["loan_granted"])
    print(f"   线上口径：放款 {granted} 单（有表现期标签），"
          f"拒绝 {600 - granted} 单（无标签 —— 这就是回测一切难点的根源）")
    print("② 启动回测图 …\n" + "─" * 78)
    engine = Engine(db, inject_fail=inject_fail)
    graph = engine.build().compile(checkpointer=InMemorySaver())
    out = run(graph, engine, db)
    if out.get("report"):
        _print_report(out["report"])
    db.close()


if __name__ == "__main__":
    main()
