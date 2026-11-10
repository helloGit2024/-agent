#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LangSmith 测试与评估 —— 策略回测 Agent 的效果评估（企业版 L1+L2）。

参考：CASE-投顾AI助手（效果评估）/2-langsmith_testing_evaluation.py 的六层结构：
  1. 测试数据集代码化（case_id 稳定主键 + upsert，不删库重建）
  2. 运行批量测试（对回测 Agent：每个用例 = 一份不同 seed 的历史数据集）
  3. 评估器：规则评估器为主 + openevals LLM-as-judge（可选）
  4. 评估器故障归因：judge/evaluator 异常记 unknown，绝不与"答错"混淆
  5. compare_experiments：bootstrap 显著性检验
  6. 质量门禁：不达标 → 非 0 退出码（CI 卡发布）

与投顾案例的关键差异（回测 Agent 的评估哲学）：
  投顾助手评"回答得好不好"（主观为主，LLM judge 挑大梁）；
  回测 Agent 评"算得对不对、诚实不诚实"（客观可断言，规则评估器是主角）：
    - 方向判断是否正确（数据故事是已知的 → 有标准答案）
    - LMDI 残差是否为 0（数学事实，零容忍）
    - 放宽方向是否诚实标注 UNAVAILABLE（诚实性，一格都不许冒充）
    - 报告数字是否全部可溯源（可信度的分水岭）
  LLM judge 只负责一件事：叙述是否"没把数字写错、没把结论说反"。

本地模式（重要）：没有 LANGSMITH_API_KEY 也能跑 —— 规则评估器全部本地执行，
只有上报和跨实验对比需要云端。学习阶段先把本地模式跑绿，再接云端。

退出码约定（CI 用）：
  0 = 通过门禁  1 = 基础设施失败  2 = 质量门禁不通过

跑法：
    python3 evaluation_langsmith.py                    # 本地评估（无需任何 key）
    LANGSMITH_API_KEY=sk-... LANGCHAIN_TRACING_V2=true python3 evaluation_langsmith.py
    ENABLE_LLM_JUDGE=1 DASHSCOPE_API_KEY=sk-... python3 evaluation_langsmith.py
"""
from __future__ import annotations

import json
import os
import random
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.analysis import MetricRegistry
from core.codes import SOURCE_LEVEL
from core.db import Database
from core.metrics import _stage_pass_set, slice_detectable
from core.policies import DEFAULT_CONFIG
from core import retro

ART = Path("artifacts")
DATASET_VERSION = "v1.0"
# 回测评估的数据集：每个用例是一份"带已知真相的历史数据集"（seed 决定）
# 教学要点：评估数据集必须带"标准答案"。生成器的故事设计（P_CR_001 该收紧）
# 就是答案 —— 换 seed 只换噪声，不换真相，所以每个 seed 的期望结论一致。
EVAL_SEEDS = [2026, 2027, 2028, 2029, 2030]
# 教学要点：评估数据集为什么是 800 单而不是 400 ——
# 收紧读点的合格线要求"新增拒绝人群 ≥30 人"（小样本坏账率不可信），
# 400 单时该格只有 15~25 人，读点会因样本不足而正确地放弃收紧建议。
# 这不是 bug，是"样本不足 → 判 HOLD，绝不硬给建议"规格的正确行为；
# 评估要用足够大的数据集让"真信号"有机会被读出来。
N_APPLICANTS = 800

# 期望结论（数据故事的"标准答案"）—— 规则评估器拿它当判据
EXPECTED = {
    "consistency_rate": 1.0,                      # 第 0 步必须全对
    "tighten_policy": "P_CR_001",                 # 收紧信号必须落在 P_CR_001
    "lmdi_residual_max": 1e-6,                    # LMDI 数学上必须精确
    "relax_bad_level": SOURCE_LEVEL["UNAVAILABLE"],  # 放宽坏账必须诚实标注
    "traceability_min": 1.0,                      # 报告数字 100% 可溯源
}

# ---------------------------------------------------------------------------
# 被测对象：把 v1_backtest 的图跑一遍，产出结构化"评估输出"
# ---------------------------------------------------------------------------

def _reference_tighten(db, granted_flags: dict, tolerance: float = 0.20) -> List[str]:
    """收紧读点规则的参考实现（oracle）—— 与 v1_backtest._n_diagnose 的读点规则逐条对应：
    敏感性表中，阈值 < 当前值、边际坏账率 ≥ 容忍线、新增拒绝样本 ≥30、
    且不越过"最多收紧三档 / 20%"的合理性下限。返回命中这些条件的策略列表。"""
    out = []
    from core.metrics import threshold_sensitivity
    for pid, thr in [("P_CR_001", 6), ("P_CR_002", 30), ("P_CR_003", None),
                     ("P_CR_004", 0.55), ("P_AF_002", 5), ("P_AF_003", 3)]:
        if not isinstance(thr, (int, float)):
            continue
        pairs = []
        for f in db.facts("run_id=? AND policy_id=?", (retro.ONLINE_RUN, pid)):
            v = json.loads(f["raw_value"]) if f["raw_value"] is not None else None
            pairs.append((v, f["value_state"], granted_flags.get(f["apply_id"])))
        floor_t = max(0, thr - 3) if isinstance(thr, int) else thr * 0.8
        elig = [r for r in threshold_sensitivity(pairs, thr)
                if floor_t <= r["threshold"] < thr
                and r["marginal_bad_rate"] is not None
                and r["marginal_bad_rate"] >= tolerance
                and r["rejected_n"] >= 30]
        if elig:
            out.append(pid)
    return out


def run_backtest_case(seed: int, n: int = N_APPLICANTS) -> Dict[str, Any]:
    """单个评估用例 = 一份历史数据集 + 一次完整回测。

    返回结构化结果（评估器只吃这个 dict，不碰 db / 图 —— 评估器可单测的关键）。
    """
    # 延迟导入：让"评估数据结构"不依赖 langgraph 也能被规则评估器单测
    from v1_backtest import Engine, _bad_by_apply, _pass_set
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    ART.mkdir(parents=True, exist_ok=True)
    db_path = ART / f"eval_{seed}.db"
    if db_path.exists():
        db_path.unlink()
    db = Database(db_path)
    try:
        rows = retro.gen_population(n, seed=seed)
        retro.seed(db, rows)
        # 期望候选集：用"与 Agent 相同的声明规则"在事实表上独立重算（oracle 测试）。
        # 教学要点（评估设计的进阶课）：规则本身（容忍线 0.20、样本 ≥30、最多收紧
        # 三档）是【规格】，不是被测对象；评估验证的是 Agent 是否忠实执行了规格。
        # 参考实现必须独立于 v1_backtest 的 Engine —— 否则 Agent 抄自己的答案。
        granted_flags = {r["apply_id"]: r["bad_flag"] for r in rows if r["loan_granted"]}
        expected_tighten = _reference_tighten(db, granted_flags)
        engine = Engine(db)
        graph = engine.build().compile(checkpointer=InMemorySaver())
        rid = f"EVAL-{seed}"
        cfg = {"configurable": {"thread_id": rid}}
        out = graph.invoke({"run_id": rid, "trace": [], "findings": {},
                            "step_failures": 0}, cfg)
        rounds = 0
        while "__interrupt__" in out and rounds < 2:
            rounds += 1
            payload = out["__interrupt__"][0].value
            answer = {"decision": "APPROVE", "approver": "eval-auto",
                      "note": "评估用例自动签字"}
            out = graph.invoke(Command(resume=answer), cfg)

        findings = out.get("findings", {})
        report = out.get("report") or {}
        con = findings.get("consistency", {})
        tighten_dirs = [d for d in findings.get("directions", [])
                        if d["direction"] == "TIGHTEN"]
        reg_ids = set(report.get("compliance", {}).get("registry", {}).keys())
        exec_ids = {row["metric_id"] for row in report.get("exec", [])
                    if row.get("metric_id")}
        traceability = (len(exec_ids & reg_ids) / len(exec_ids)) if exec_ids else None
        return {
            "case_id": f"backtest_seed_{seed}",
            "seed": seed,
            "expected_tighten": expected_tighten,
            "consistency_rate": con.get("rate"),
            "consistency_gap_n": con.get("data_gap"),
            "tighten_policies": [d["policy_id"] for d in tighten_dirs],
            "lmdi_residual": (findings.get("lmdi") or {}).get("residual"),
            "relax_bad_level": (findings.get("relax") or {}).get("bad_rate_level"),
            "traceability_rate": traceability,
            "degraded": bool(out.get("degraded")),
            "error": out.get("trace", []) and any("✘" in t for t in out["trace"]),
            "narrative": report.get("narrative", ""),
            "n": n,
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 评估器健康度（L1：评估器故障 ≠ 答错）
# ---------------------------------------------------------------------------
EVAL_HEALTH: Counter = Counter()


class RuleEvaluator:
    """规则评估器基类：打分 0/1，异常记 unknown（不冒充 0 分）。

    教学要点（L1 核心）：评估器自己挂了，必须记 unknown 并计入健康度，
    绝不能记 0 分 —— 那是"答错"的语义，会把系统问题误判成质量问题。
    """
    key = "base"

    def evaluate(self, out: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return self._score(out)
        except Exception as e:                                  # noqa: BLE001
            EVAL_HEALTH[self.key] += 1
            return {"key": self.key, "score": None,
                    "comment": f"[unknown] 评估器异常: {type(e).__name__}: {e}"}

    def _score(self, out: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError


class ConsistencyGateEvaluator(RuleEvaluator):
    """第 0 步铁律：判决一致率必须 100%。不到 100% 的回测没有资格出报告。"""
    key = "consistency_gate"

    def _score(self, out):
        if out.get("degraded"):
            return {"key": self.key, "score": 0.0,
                    "comment": "[fail] 运行降级（步骤失败）"}
        rate = out.get("consistency_rate")
        ok = rate == EXPECTED["consistency_rate"]
        return {"key": self.key, "score": 1.0 if ok else 0.0,
                "comment": f"一致率 {rate}（期望 {EXPECTED['consistency_rate']}）"}


class DirectionEvaluator(RuleEvaluator):
    """方向判断正确性 —— oracle 测试：Agent 的收紧候选必须与参考实现一致。

    教学要点（评估设计的进阶课）：
      期望不来自"拍脑袋的标准答案"，而来自【同一声明规则的独立参考实现】
      在事实表上的重算。这样评估能同时抓住两类错误：漏掉真信号（参考实现
      说该收紧、Agent 没给）和过度建议（参考实现说样本不足、Agent 硬给）。
      规则本身（容忍线 0.20、n≥30）是规格 —— 规格对不对是业务的事，
      Agent 忠不忠实于规格才是评估的事。
    """
    key = "direction_accuracy"

    def _score(self, out):
        hits = set(out.get("tighten_policies") or [])
        expected = set(out.get("expected_tighten") or [])
        target = EXPECTED["tighten_policy"]
        ok = (target in hits) == (target in expected)
        verdict = ("参考实现与 Agent 一致" if ok else "参考实现与 Agent 不一致")
        return {"key": self.key, "score": 1.0 if ok else 0.0,
                "comment": f"{verdict}：期望含 {target}={target in expected}，"
                           f"实际候选 {sorted(hits) or '无'}"}


class LMDIAdditivityEvaluator(RuleEvaluator):
    """LMDI 残差 = 0（数学事实，零容忍）。这条不过 = 归因分解写错了。
    合法跳过：没有任何收紧候选时 LMDI 不运行（residual=None）—— 记 skipped，
    不记 0 分也不记 unknown（跳过是业务上正确的行为，不是故障也不是答错）。"""
    key = "lmdi_additivity"

    def _score(self, out):
        resid = out.get("lmdi_residual")
        if resid is None and not out.get("tighten_policies"):
            return {"key": self.key, "score": None,
                    "comment": "[skipped] 无收紧候选，LMDI 未运行（合法跳过）"}
        ok = resid is not None and abs(resid) <= EXPECTED["lmdi_residual_max"]
        return {"key": self.key, "score": 1.0 if ok else 0.0,
                "comment": f"残差 {resid}（上限 {EXPECTED['lmdi_residual_max']}）"}


class HonestyEvaluator(RuleEvaluator):
    """诚实性：放宽方向的坏账结论必须标 UNAVAILABLE。
    教学要点：这是回测 Agent 最重要的一条评估 —— 一个会把"不可得"标成
    "估计值"的系统，比一个算得慢的系统危险一百倍。"""
    key = "honesty_label"

    def _score(self, out):
        level = out.get("relax_bad_level")
        ok = level == EXPECTED["relax_bad_level"]
        return {"key": self.key, "score": 1.0 if ok else 0.0,
                "comment": f"放宽坏账等级 {level}（期望 {EXPECTED['relax_bad_level']}）"}


class TraceabilityEvaluator(RuleEvaluator):
    """数字可溯源：报告 exec 行的每个 metric_id 必须在登记簿里。"""
    key = "traceability"

    def _score(self, out):
        tr = out.get("traceability_rate")
        if tr is None:
            return {"key": self.key, "score": 0.0, "comment": "报告无可溯源数字"}
        ok = tr >= EXPECTED["traceability_min"]
        return {"key": self.key, "score": 1.0 if ok else 0.0,
                "comment": f"溯源率 {tr:.0%}"}


ALL_EVALUATORS = [ConsistencyGateEvaluator(), DirectionEvaluator(),
                  LMDIAdditivityEvaluator(), HonestyEvaluator(),
                  TraceabilityEvaluator()]


# ---------------------------------------------------------------------------
# LLM judge（可选，openevals）—— 只评叙述层
# ---------------------------------------------------------------------------

_JUDGE_PROMPT = """你是策略回测报告的质检员。请根据回测输出的叙述文本，判断其作为"报告连接词"是否合格。

合格标准（全部满足判 1，任何一条不满足判 0）：
1. 只做定性解读与衔接，不引入叙述文本之外的具体数字（数字必须走槽位）；
2. 结论方向明确，不含"可能大概也许"式的模糊表述；
3. 不对放宽方向的坏账率做任何承诺性暗示（该方向标签不可得）；
4. 语言专业简洁，无废话。

回测输出叙述：
{outputs}

请先给出理由，再给出分数（1=合格，0=不合格）。"""

FEW_SHOT_EXAMPLES = [
    {"inputs": {}, "outputs": "线上与离线判决完全一致，本次回测数据可信。收紧方向结论为实测精确值，"
                              "可直接进入重放验证与业务评审。放宽方向的坏账结论受限于标签可得性，"
                              "建议按实验设计卡启动小流量验证。",
     "score": 1.0, "reasoning": "只做定性衔接，无编造数字，放宽方向明确指向实验而非估算（人工标注合格）"},
    {"inputs": {}, "outputs": "回测显示放宽后坏账率预计下降约 2 个百分点，可以放心放宽。",
     "score": 0.0, "reasoning": "编造了不在槽位里的具体数字，且对无标签的放宽方向给出承诺性坏账结论，"
                                "违反诚实性红线（人工标注不合格）"},
]


def _build_llm_judge():
    """构建 LLM judge。依赖或 key 缺失时返回 None（优雅降级为纯规则评估）。"""
    if os.getenv("ENABLE_LLM_JUDGE", "0").strip().lower() in ("0", "false", "no"):
        print("[LLM judge] 未启用（ENABLE_LLM_JUDGE=1 打开；只评叙述层，规则评估器是主角）")
        return None
    if not os.getenv("DASHSCOPE_API_KEY"):
        print("[LLM judge] 未设置 DASHSCOPE_API_KEY，跳过")
        return None
    try:
        from langchain_community.chat_models import ChatTongyi
        from openevals.llm import create_llm_as_judge
    except ImportError as e:
        print(f"[LLM judge] 依赖缺失（{e}），跳过。需要: openevals + langchain-community")
        return None
    judge_model = os.getenv("LLM_JUDGE_MODEL", "qwen-flash")
    eval_llm = ChatTongyi(model=judge_model, temperature=0)
    judge_fn = create_llm_as_judge(prompt=_JUDGE_PROMPT, feedback_key="narrative_quality_llm",
                                   judge=eval_llm, continuous=False, use_reasoning=True,
                                   few_shot_examples=FEW_SHOT_EXAMPLES)
    print(f"[LLM judge] 已启用: model={judge_model}")
    return judge_fn


def run_llm_judge(judge_fn, out: Dict[str, Any]) -> Dict[str, Any]:
    try:
        result = judge_fn(inputs={}, outputs=out.get("narrative", ""))
        score = result.get("score") if isinstance(result, dict) else result
        reasoning = result.get("reasoning", "") if isinstance(result, dict) else ""
        return {"key": "narrative_quality_llm",
                "score": float(score) if score is not None else None,
                "comment": f"[llm_judge] {reasoning[:120]}"}
    except Exception as e:                                      # noqa: BLE001
        EVAL_HEALTH["narrative_quality_llm"] += 1
        return {"key": "narrative_quality_llm", "score": None,
                "comment": f"[unknown] judge 异常: {type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# 本地评估主流程 + 云端上报（有 key 时）
# ---------------------------------------------------------------------------

def run_local_evaluation() -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """本地评估：跑全部 seed 用例 × 全部规则评估器，聚合每个 key 的统计。"""
    rows = []
    for seed in EVAL_SEEDS:
        print(f"  ▶ 用例 {seed}：生成 {N_APPLICANTS} 单历史数据 + 全流程回测 …")
        out = run_backtest_case(seed)
        evals = [ev.evaluate(out) for ev in ALL_EVALUATORS]
        judge = _build_llm_judge()
        if judge is not None:
            evals.append(run_llm_judge(judge, out))
        rows.append({"case": out, "evals": evals})
        for ev in evals:
            mark = "✔" if ev["score"] == 1.0 else ("✘" if ev["score"] == 0.0 else "?")
            print(f"      {mark} {ev['key']}: {ev['comment'][:90]}")
    agg: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"n": 0, "sum": 0.0,
                                                          "unknown": 0, "skipped": 0})
    for row in rows:
        for ev in row["evals"]:
            a = agg[ev["key"]]
            if ev["score"] is None:
                if "[skipped]" in (ev.get("comment") or ""):
                    a["skipped"] += 1
                else:
                    a["unknown"] += 1
            else:
                a["n"] += 1
                a["sum"] += float(ev["score"])
    agg = {k: {"n": v["n"], "mean": v["sum"] / v["n"] if v["n"] else None,
               "unknown": v["unknown"], "skipped": v["skipped"]}
           for k, v in agg.items()}
    return rows, agg


def upsert_and_report_to_langsmith(rows, dataset_name="backtest-agent-eval-dataset"):
    """有 LANGSMITH_API_KEY 时：数据集 upsert + 逐用例上报 feedback。
    教学要点：upsert 语义与投顾案例一致 —— case_id 是稳定主键，
    内容没变的跳过，历史 experiment 保持可比，绝不 delete 重建。"""
    if not os.getenv("LANGSMITH_API_KEY") or \
            os.getenv("LANGCHAIN_TRACING_V2", "").lower() != "true":
        print("[LangSmith] 未配置 API key / TRACING，跳过云端上报（本地结果已完整）")
        return
    try:
        from langsmith import Client
        client = Client()
        try:
            ds = client.read_dataset(dataset_name=dataset_name)
        except Exception:
            ds = client.create_dataset(
                dataset_name=dataset_name,
                description=f"策略回测 Agent 评估数据集 {DATASET_VERSION}")
        for row in rows:
            case = row["case"]
            client.create_example(
                inputs={"case_id": case["case_id"], "seed": case["seed"], "n": case["n"]},
                outputs={k: v for k, v in case.items() if k != "narrative"},
                dataset_id=ds.id)
        print(f"[LangSmith] 数据集 {dataset_name} 已同步 {len(rows)} 个用例")
    except Exception as e:                                      # noqa: BLE001
        print(f"[LangSmith] 上报失败（不影响本地结果）: {e}")


# ---------------------------------------------------------------------------
# 质量门禁
# ---------------------------------------------------------------------------

GATE_MIN = {"consistency_gate": 1.0, "direction_accuracy": 1.0,
            "lmdi_additivity": 1.0, "honesty_label": 1.0, "traceability": 1.0}
GATE_MAX_UNKNOWN_RATE = 0.2


def quality_gate(agg) -> Tuple[bool, List[str]]:
    lines, passed = [], True
    for key, thr in GATE_MIN.items():
        st = agg.get(key)
        if st is None or st["n"] == 0:
            if st and st["skipped"]:
                lines.append(f"  - {key}: 全部合法跳过（{st['skipped']} 例），不计入门禁")
                continue
            lines.append(f"  ✗ {key}: 无有效打分")
            passed = False
            continue
        if st["mean"] < thr:
            lines.append(f"  ✗ {key}: {st['mean']:.2f} < 阈值 {thr}")
            passed = False
        else:
            lines.append(f"  ✓ {key}: {st['mean']:.2f} ≥ {thr}")
        total = st["n"] + st["unknown"]
        if total and st["unknown"] / total > GATE_MAX_UNKNOWN_RATE:
            lines.append(f"  ✗ {key}: 评估器故障率 {st['unknown'] / total:.0%} 超限")
            passed = False
    return passed, lines


def bootstrap_mean_diff_ci(a, b, n_boot=2000, ci=0.95, seed=42):
    """两个实验分数均差的 bootstrap 置信区间（与投顾案例同款，L2）。"""
    if not a or not b:
        return None
    rnd = random.Random(seed)
    diffs = []
    for _ in range(n_boot):
        ma = sum(rnd.choice(a) for _ in range(len(a))) / len(a)
        mb = sum(rnd.choice(b) for _ in range(len(b))) / len(b)
        diffs.append(ma - mb)
    diffs.sort()
    alpha = (1 - ci) / 2
    return (sum(a) / len(a) - sum(b) / len(b),
            diffs[int(alpha * n_boot)], diffs[min(int((1 - alpha) * n_boot), n_boot - 1)])


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main() -> None:
    print("═" * 78)
    print("策略回测 Agent · LangSmith 评估（规则评估器为主，LLM judge 只评叙述）")
    print(f"数据集：{len(EVAL_SEEDS)} 个 seed × {N_APPLICANTS} 单；期望结论 {EXPECTED}")
    print("═" * 78)
    try:
        rows, agg = run_local_evaluation()
    except Exception as e:                                      # noqa: BLE001
        print(f"✘ 基础设施失败: {type(e).__name__}: {e}")
        sys.exit(1)

    print("\n" + "─" * 78)
    print("聚合结果")
    print(f"{'feedback key':<24}{'均分':>8}{'打分':>6}{'故障':>6}")
    for key, st in agg.items():
        mean = f"{st['mean']:.3f}" if st["mean"] is not None else "-"
        print(f"{key:<24}{mean:>8}{st['n']:>6}{st['unknown']:>6}")

    upsert_and_report_to_langsmith(rows)

    passed, gate_lines = quality_gate(agg)
    print("\n质量门禁")
    for line in gate_lines:
        print(line)
    if EVAL_HEALTH:
        print(f"\n注意：评估器自身故障（unknown，未计入均分也未假装答错）：{dict(EVAL_HEALTH)}")

    if passed:
        print("\n✅ 门禁通过（exit 0）")
        sys.exit(0)
    print("\n❌ 门禁不通过（exit 2）")
    sys.exit(2)


if __name__ == "__main__":
    main()
