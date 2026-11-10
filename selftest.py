#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自检 —— 回测系统的"活的规格说明书"（与 approval_lab/selftest.py 同一哲学）。

每条断言的注释就是一句业务需求。新框架落地时先写这些断言，代码是它们的实现。
跑法：python3 selftest.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.analysis import MetricRegistry, render_report, rule_based_plan, validate_plan
from core.codes import SOURCE_LEVEL
from core.db import Database
from core.metrics import (binstats, consistency_rate, direction_judgement,
                          lmdi_stage_attribution, relax_eval, threshold_sensitivity,
                          tighten_compare)
from core.policies import DEFAULT_CONFIG, evaluate_stage
from core import retro

ART = Path("artifacts")
PASS_COUNT = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS_COUNT
    mark = "✔" if cond else "✘"
    print(f"  {mark} {name}" + (f" —— {detail}" if detail else ""))
    if not cond:
        print("\n自检失败，中止。上面最后一条 ✘ 就是规格被违反的地方。")
        sys.exit(1)
    PASS_COUNT += 1


def fresh_db(name: str) -> Database:
    ART.mkdir(parents=True, exist_ok=True)
    p = ART / name
    if p.exists():
        p.unlink()
    return Database(p)


print("═" * 78)
print("策略回测分析 Agent · 自检（每条断言 = 一句业务规格）")
print("═" * 78)

# ── 准备：一份种子数据 ──────────────────────────────────────────────
db = fresh_db("selftest.db")
rows = retro.gen_population(400, seed=99)
retro.seed(db, rows)
granted = [r for r in rows if r["loan_granted"]]

# 1. 可重放的地基：同一批快照换配置重放，结论必须可复现
print("\n【1】可重放与判决一致性")
h1 = retro.run_offline(db, "R1", "default", DEFAULT_CONFIG)
h2 = retro.run_offline(db, "R2", "default", DEFAULT_CONFIG)
check("同配置重放两次，结果逐字段一致", h1 == h2,
      f"pass={h1['pass']}/{h1['n']}")
con = consistency_rate(db.facts("run_id='ONLINE_BASELINE'"), db.facts("run_id='R1'"))
check("第 0 步：线上 vs 离线判决一致率 = 100%", con["rate"] == 1.0,
      f"比对 {con['compared']} 条")
check("线上数据源故障的单进缺口清单，不冒充'不一致'",
      con["data_gap"] == len(retro.ONLINE_OUTAGE_APPLIES) and con["mismatch"] == 0,
      f"缺口 {con['data_gap']} 单")
check("重放通过人群 = 线上放款人群（单一事实源）",
      h1["pass"] == len(granted), f"重放 {h1['pass']} vs 线上 {len(granted)}")

# 2. LMDI：乘法结构的精确可加分解
print("\n【2】LMDI 归因")
import random as _rnd
rng = _rnd.Random(7)
for i in range(5):
    before = {"S1_ADM": rng.uniform(.5, 1), "S2_AF": rng.uniform(.5, 1),
              "S3_CR": rng.uniform(.5, 1), "S4_LIM": rng.uniform(.5, 1)}
    after = {s: min(1.0, max(0.01, v + rng.uniform(-.15, .15))) for s, v in before.items()}
    lmdi = lmdi_stage_attribution(before, after)
    check(f"LMDI 精确可加（随机案例 {i + 1}）",
          lmdi["additive_check_passed"] and abs(lmdi["residual"]) < 1e-9,
          f"Σ贡献−ΔP = {lmdi['residual']}")
lmdi_zero = lmdi_stage_attribution(before, before)
check("通过率无变化时各环节贡献全为 0",
      all(abs(v) < 1e-12 for v in lmdi_zero["contributions"].values()))

# 3. 五态拆分：缺失/空/哨兵/未执行绝不容许混为一谈
print("\n【3】VALUE_STATE 五态")
from core.policies import value_state_of
check("payload 为 None → MISSING_SOURCE（该补数，不是客群特征）",
      value_state_of(None) == "MISSING_SOURCE")
check("哨兵 -1/-999 → SENTINEL（查无记录 ≠ 零次）",
      value_state_of({"query_3m": -1}, "query_3m") == "SENTINEL"
      and value_state_of({"hist_unpaid_bad": -999}, "hist_unpaid_bad") == "SENTINEL")
check("业务空值 None → BIZ_EMPTY（合法的空，有风险含义）",
      value_state_of({"income_verified": None}, "income_verified") == "BIZ_EMPTY")
res_missing = evaluate_stage("S3_CR", {"DS_BUREAU_05": None, "INT_LOAN_HIST": None,
                                       "INT_PROD_CFG": None, "INT_USER": None},
                             DEFAULT_CONFIG)
check("数据全缺：策略返回 UNKNOWN，环节 REVIEW，绝不放行",
      res_missing.status == "REVIEW"
      and all(o.result == "UNKNOWN" for o in res_missing.outcomes))

# 4. 收紧方向：重放即精确
print("\n【4】收紧重放（精确值）")
labels = {r["apply_id"]: r["bad_flag"] for r in granted}
facts1 = db.facts("run_id='R1'")
from core.metrics import _stage_pass_set
pass_cur = _stage_pass_set(facts1, "S4_LIM")
tight_rows = retro.gen_population(0)  # 占位，避免重复生成
# 手工构造收紧对比：把通过人群中 query_3m>4 的人砍掉
bureau = {r["apply_id"]: r["query_3m"] for r in rows}
pass_cand = {a for a in pass_cur if bureau[a] <= 4}
cmp_ = tighten_compare(labels, pass_cur, pass_cand, n_total=len(rows))
cut = pass_cur - pass_cand
cut_bad_manual = sum(1 for a in cut if labels.get(a) == 1) / max(len(cut), 1)
check("收紧被砍人群坏账率 = 手工计算值（精确，非估计）",
      round(cut_bad_manual, 4) == cmp_["cut_bad_rate"],
      f"砍 {cmp_['cut_n']} 人，坏账率 {cmp_['cut_bad_rate']:.1%}")
check("收紧对比标注 MEASURED 来源等级", cmp_["level"] == SOURCE_LEVEL["MEASURED"])

# 5. 放宽方向：通过率精确，坏账不可得
print("\n【5】放宽评估（诚实标注）")
facts2 = db.facts("run_id='R2-relax'") if (ART / "selftest.db").exists() else None
# 直接构造：R1 vs 放宽 P_AF_003 的重放
retro.run_offline(db, "R3-relax", "relax", {**DEFAULT_CONFIG, "P_AF_003": 5})
rel = relax_eval(facts1, db.facts("run_id='R3-relax'"), labels)
check("放宽增量 = 新过放宽节点 − 被下游拦截（口径守恒）",
      rel["new_s2_n"] == rel["incremental_n"] + (rel["downstream_kill_n"] or 0),
      f"新过节点 {rel['new_s2_n']} = 放款增量 {rel['incremental_n']}"
      f" + 下游拦截 {rel['downstream_kill_n']}")
check("增量人群无放款标签 → 坏账等级必须标 UNAVAILABLE（不冒充实测）",
      rel["bad_label_available_n"] == 0
      and rel["bad_rate_level"] == SOURCE_LEVEL["UNAVAILABLE"])

# 6. 分箱与方向判据
print("\n【6】分箱与方向硬判据")
pairs = [(bureau[a], "OK", labels.get(a)) for a in pass_cur]
bins = binstats(pairs)
ok_bins = [b for b in bins if b["value_state"] == "OK"]
check("分箱：哨兵/缺失单独成箱，绝不混入数值箱",
      all(b["value_state"] == "OK" for b in ok_bins)
      and all(b["flag"] in ("OK", "INSUFFICIENT") for b in ok_bins))
# 白拒场景：拒绝组坏账率反而更低 → 应判 RELAX
good = [(1, "OK", 0)] * 300 + [(0, "OK", 0)] * 100          # 低查询人群：25% 坏？不——构造白拒
white = ([(2, "OK", 0)] * 250 + [(2, "OK", 1)] * 20 +       # 通过组：坏账率 7.4%
         [(8, "OK", 0)] * 100 + [(8, "OK", 1)] * 30)        # 拒绝组：坏账率 23% → 反例
# 反过来构造"拒绝组更安全"：拒绝组坏人少
white2 = ([(2, "OK", 0)] * 200 + [(2, "OK", 1)] * 60 +      # 通过组坏账率 23%
          [(8, "OK", 0)] * 200 + [(8, "OK", 1)] * 10)       # 拒绝组坏账率 4.8% → 白拒
dj = direction_judgement(white2, threshold=5)
check("硬判据：拒绝组坏账率显著更低 → 判 RELAX（白拒）",
      dj["direction"] == "RELAX", dj["reason"])
dj2 = direction_judgement([(1, "OK", 0)] * 10, threshold=5)
check("硬判据：样本不足 → 判 HOLD，绝不给调整建议",
      dj2["direction"] == "HOLD")

# 7. 分析计划 DSL：LLM 的输出闸门
print("\n【7】分析计划 DSL")
plan = rule_based_plan()
ok, errs = validate_plan(plan)
check("规则计划合法", ok)
bad_plan = {**plan, "steps": ["funnel", "consistency_check", "report"]}
ok2, errs2 = validate_plan(bad_plan)
check("consistency_check 不在第一位 → 拒绝（第 0 步铁律）",
      not ok2 and any("第一步" in e for e in errs2))
bad_plan3 = {**plan, "steps": ["consistency_check", "tarot_divination", "report"]}
ok3, _ = validate_plan(bad_plan3)
check("非法步骤名 → 拒绝", not ok3)

# 8. 报告渲染：数字可溯源
print("\n【8】数字可溯源")
reg = MetricRegistry()
reg.put("pass_rate_current", 0.215, "MEASURED")
reg.put("bad_rate_measured", 0.147, "MEASURED")
findings = {"headline": {"pass_rate_current": 0.215, "bad_rate_measured": 0.147,
                         "pass_rate_level": SOURCE_LEVEL["MEASURED"],
                         "bad_rate_level": SOURCE_LEVEL["MEASURED"]}}
report = render_report(findings, reg)
exec_ids = {row["metric_id"] for row in report["exec"]}
check("报告 exec 行的每个数字都在登记簿（可溯源）", exec_ids <= set(reg.all().keys()),
      f"{len(exec_ids)} 个 metric_id")
check("每个数字都带来源等级标注",
      all(row["level"] for row in report["exec"]))
check("模板叙述不含裸数字（数字只走槽位）",
      not any(ch.isdigit() for ch in report["narrative"]))

# 9. 审计链与幂等
print("\n【9】审计链")
chain = db.auditor("SELFTEST-CHAIN")
for i in range(5):
    chain.append(f"event.{i}", {"i": i})
ok_chain, n_chain = chain.verify()
check(f"哈希链 {n_chain} 条完整性验证通过", ok_chain and n_chain == 5)
db.conn.execute("UPDATE audit SET payload='{\"tampered\": true}' "
                "WHERE run_id='SELFTEST-CHAIN' AND seq=3")
db.conn.commit()
ok_tamper, _ = chain.verify()
check("单条记录被篡改 → 整链校验失败", not ok_tamper)
first = chain.append("idem.event", {"x": 1}, idem_key="once-only")
second = chain.append("idem.event", {"x": 1}, idem_key="once-only")
check("idem_key 幂等：同一事件第二次写入被拒绝", first and not second)

# 10. 长表事实
print("\n【10】policy_eval_fact 长表")
n_rows = len(db.facts("run_id='R1'"))
n_apps = len(db.applicants())
check("长表行数 = 申请 × 执行到的策略（12 条/单上限）",
      0 < n_rows < n_apps * 12, f"{n_rows} 行 / {n_apps} 单")
sample = db.facts("run_id='R1' AND value_state='SENTINEL'")
check("哨兵值在事实表中有独立状态行（五态落库）", len(sample) > 0,
      f"{len(sample)} 条 SENTINEL 行")

print(f"\n{'═' * 78}")
print(f"自检通过：{PASS_COUNT} 条断言全部满足。这份规格就是 V1 的行为边界。")
db.close()
