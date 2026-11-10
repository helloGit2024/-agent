# -*- coding: utf-8 -*-
"""历史回测数据生成器 + 离线重放引擎 —— 把"故障与故事做成夹具"。

与 approval_lab/core/sources.py 的关系：
  审批系统模拟"线上取数"（延迟、故障注入、按次计费）；
  回测系统模拟的是"历史已经发生的事"—— 申请已经跑完、快照已经落库、
  放款的人已经过了表现期。所以这里不再有 SourceHub 和计费，
  取而代之的是两件事：

    1. gen_population()：生成一批"带故事的"历史申请（600 单，2026 年 1-6 月）。
       数据里埋了三个业务故事，回测 Agent 的分析必须能把它挖出来——
       这不是巧合装饰，而是"把期望结论做成测试夹具"：
         故事 A（收紧）：P_CR_001 阈值 6，通过人群里 query_3m=5~6 的坏账率
                        显著高于基线 → 分箱能读出"收紧到 4"。
         故事 B（放宽）：P_AF_003 阈值 3，24h 申请 4~5 次的人被拒在 S2，
                        但这批人在其他维度风险不高 → 疑似白拒，但无标签，
                        只能算"放宽的通过率账"和"下游拦截率"，坏账率不可得。
         故事 C（数据质量）：约 3% 流水缺失、部分征信/内部历史是哨兵值、
                        2 单线上判决因当天数据源故障缺失 —— 考验五态拆分
                        和第 0 步"判决一致率核对"。

    2. run_offline()：离线重放引擎。读快照（不花钱）→ 逐环节跑策略 →
       写 policy_eval_fact 长表。同一批快照换 config 重放 N 次，
       就是 what-if 分析的全部成本模型 —— 这就是"可重放"三个字的落地。

  一致性核对（第 0 步）的数据基础：
    seed_online() 会先写入一份"线上判决事实"（模拟审批系统导出的判决表），
    其中故意埋了 2 单数据源故障。离线重放后必须能把这 2 单找出来 ——
    如果一致率不是 100%，回测的每一个数字都不可信，必须先修数据。
"""
from __future__ import annotations

import hashlib
import json
import random
from typing import Any

from .codes import STAGE_ORDER, STAGE_SOURCES, SOURCE_LEVEL
from .policies import DEFAULT_CONFIG, evaluate_stage

# 线上判决事实固定用这个 run_id（模拟审批系统导出的判决表）
ONLINE_RUN = "ONLINE_BASELINE"
# 故意埋的"线上数据源故障日"申请（一致性核对必须把它们找出来）
ONLINE_OUTAGE_APPLIES = {"B_S3_0009", "B_S3_0057"}

PRODUCTS = {
    "P_CASH_001": {"limit_max": 200_000, "limit_min": 3_000, "max_dti": 0.55,
                   "age_min": 22, "age_max": 55, "bureau_query_3m_max": 6,
                   "multiple": 8},
    "P_INSTALL_003": {"limit_max": 50_000, "limit_min": 1_000, "max_dti": 0.65,
                      "age_min": 20, "age_max": 60, "bureau_query_3m_max": 8,
                      "multiple": 6},
}
CHANNELS = ["APP_自然量", "渠道A_贷款超市", "渠道B_信息流"]
MONTHS = ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]


def gen_population(n: int = 600, seed: int = 2026) -> list[dict]:
    """生成历史申请人群（含源 payload 与贷后标签）。

    风险生成模型（教学要点：先造"真相"，再造"数据"）：
      每人有一个潜在风险分 risk（0~1），坏账概率由它决定；
      特征与 risk 相关但带噪声 —— 现实里没有完美变量，
      所以 KS 不会是 0.5，分箱坏账率也不会完美单调。
    """
    rng = random.Random(seed)
    rows: list[dict] = []
    for i in range(n):
        aid = f"B_S3_{i:04d}"
        product = rng.choices(list(PRODUCTS), weights=[0.6, 0.4])[0]
        prod = PRODUCTS[product]
        channel = rng.choices(CHANNELS, weights=[0.5, 0.3, 0.2])[0]
        is_new = rng.random() < 0.45
        month = rng.choice(MONTHS)
        # ---- V2 新增：申请日（供"近一周/近一月"级时间窗口使用）----
        # 教学要点（夹具的向后兼容纪律）：这里【不能消耗 rng】。
        # 一旦多抽一次随机数，后面所有特征的随机序列全部错位，
        # V1 的三个教学故事（A 收紧 / B 放宽 / C 数据质量）会集体漂移，
        # V1 自检与评估全部失效。所以日期用 apply_id 的序号做确定性散列：
        #   13 与 28 互质 ⇒ (i*13)%28 能均匀铺满 1~28 日，且完全可复现。
        day = (int(aid[-4:]) * 13) % 28 + 1
        income = round(rng.gauss(16000, 6000), 0)
        income = max(4000, income)
        amount = min(prod["limit_max"], rng.choice([5_000, 10_000, 20_000, 50_000, 100_000]))

        # ---- 潜在风险与特征（相关但不完美）----
        risk = rng.random()
        query_3m = max(0, int(rng.gauss(2 + 2.8 * risk, 1.8)))
        if rng.random() < 0.06:
            query_3m = -1                      # 征信查无记录（哨兵）
        overdue_days = max(0, int(rng.gauss(5 + 30 * risk * risk, 8)))
        if rng.random() < 0.10:
            overdue_days = -1                  # 无信贷记录（哨兵）
        debt_monthly = round(max(0, rng.gauss(1500 + 4000 * risk, 1500)), 2)
        existing_debt = round(max(0, rng.gauss(2000 + 5000 * risk, 2000)), 2)
        device_linked = max(0, int(rng.gauss(1 + 2.5 * risk, 1.2)))
        apply_24h = rng.choices([0, 1, 2, 3, 4, 5, 7], weights=[.35, .25, .15, .1, .07, .05, .03])[0]
        hist_unpaid_bad = 0 if (is_new or rng.random() < 0.92) else 1
        if is_new and rng.random() < 0.30:
            hist_unpaid_bad = -999             # 内部库哨兵：无信贷历史
        income_verified = rng.random() < (0.6 if is_new else 0.92)
        ocr_quality = None if rng.random() < 0.02 else round(rng.uniform(62, 99), 1)
        flow_missing = rng.random() < 0.03     # 故事 C：流水源偶发缺失
        inflow_avg = None if flow_missing else round(income * rng.uniform(0.85, 1.15), 2)

        # ---- 线上漏斗判决：直接调用策略函数（单一事实源）----
        # 教学要点（本项目自己踩过的夹具坑）：第一版这里手写了一段"模拟线上
        # 判决"的独立逻辑，结果它和 policies.py 的 S3/S4 公式不完全一致 ——
        # 表面上第 0 步一致率 100%（因为线上事实也出自策略函数），
        # 实际上放款标签挂在了另一套判决的人群上。教训：
        # 【线上判决和离线重放必须共用同一套策略代码】，这正是"可重放"的
        # 全部意义 —— 线上引擎就是策略函数 + 配置，不存在第二套真相。
        age = rng.randint(20, 60)
        dti = round((debt_monthly + max(0, debt_monthly * 0.3)) / max(income, 1), 4)
        realname_ok = rng.random() < 0.97
        blacklist_hit = rng.random() < 0.03

        r = {
            "apply_id": aid, "product_code": product, "channel": channel,
            "is_new_customer": 1 if is_new else 0, "apply_month": month,
            "created_at": f"{month}-{day:02d}T10:00:00",   # V2：申请日期（不消耗 rng）
            "income_monthly": income, "amount_applied": amount,
            "age": age, "ocr_quality": ocr_quality, "realname_ok": realname_ok,
            "blacklist_hit": blacklist_hit, "device_linked": device_linked,
            "apply_24h": apply_24h, "query_3m": query_3m,
            "overdue_days": overdue_days, "hist_unpaid_bad": hist_unpaid_bad,
            "dti": dti, "existing_debt": existing_debt,
            "debt_monthly": debt_monthly, "inflow_avg": inflow_avg,
            "flow_missing": flow_missing, "income_verified": income_verified,
        }
        payloads = _payloads(r)
        stage_out: dict[str, str] = {}
        granted = 0
        for stage in STAGE_ORDER:
            data = {s: payloads.get(s) for s in STAGE_SOURCES[stage]}
            res = evaluate_stage(stage, data, DEFAULT_CONFIG)
            stage_out[stage] = res.status
            if res.status != "PASS":
                break
        else:
            granted = 1
        # 只有真实放款的人才可能有标签 —— 这是回测一切难点的根源
        loan_granted: int | None = granted
        bad_flag: int | None = None
        if loan_granted:
            # 坏账模型：基线 8%，query_3m 5~6 区间拉到 ~26%（故事 A），
            # 设备关联 / 高 DTI / 高频申请小幅加成
            p_bad = 0.08
            if 5 <= query_3m <= 6:
                p_bad = 0.26
            elif query_3m in (3, 4):
                p_bad = 0.12
            p_bad += 0.05 if device_linked >= 3 else 0
            p_bad += 0.04 if dti > prod["max_dti"] * 0.9 else 0
            bad_flag = 1 if rng.random() < p_bad else 0

        rows.append({
            **r,
            "loan_granted": loan_granted, "bad_flag": bad_flag,
            "_stage_out": stage_out,             # 线上各环节判决（不入 applicant 表）
        })
    return rows


# ---------------------------------------------------------------------------
# 源 payload 装配：把申请的"真相"翻译成审批系统同构的数据源结构
# ---------------------------------------------------------------------------

def _payloads(r: dict) -> dict[str, dict]:
    prod = PRODUCTS[r["product_code"]]
    return {
        "DS_REALNAME_01": {"matched": r["realname_ok"], "provider": "运营商三要素"},
        "DS_OCR_02": {"quality": r["ocr_quality"], "name": "申请人",
                      "id_no": "ID" + r["apply_id"][-4:],
                      "valid_to": "2028-01-01"},
        "DS_AF_LIST_03": {"hit": r["blacklist_hit"],
                          "list_name": "联盟黑名单" if r["blacklist_hit"] else "",
                          "hit_reason": "多头借贷团伙" if r["blacklist_hit"] else ""},
        "DS_DEVICE_04": {"linked_accounts": r["device_linked"],
                         "device_risk": min(99, r["device_linked"] * 15)},
        "DS_BUREAU_05": {"query_3m": r["query_3m"],
                         "max_overdue_days": r["overdue_days"],
                         "debt_monthly": r["debt_monthly"]},
        "DS_FLOW_06": {"inflow_avg": r["inflow_avg"]},
        "INT_USER": {"age": r["age"], "name": "申请人", "id_no": "ID" + r["apply_id"][-4:],
                     "income_monthly": r["income_monthly"]},
        "INT_APPLY_HIST": {"apply_24h": r["apply_24h"]},
        "INT_LOAN_HIST": {"hist_unpaid_bad": r["hist_unpaid_bad"],
                          "income_verified": r["income_verified"],
                          "existing_debt_monthly": r["existing_debt"]},
        "INT_PROD_CFG": {"product": r["product_code"], **prod},
    }


def seed(db, rows: list[dict]) -> None:
    """把历史申请 + 快照 + 线上判决事实灌进库（一次性，模拟"回测数据准备"）。"""
    db.add_applicants([{k: v for k, v in r.items() if not k.startswith("_")}
                       for r in rows])
    for r in rows:
        for src, payload in _payloads(r).items():
            db.put_snapshot(r["apply_id"], src, True, payload)
        # 线上判决事实（ONLINE_BASELINE）：含 2 单"当天数据源故障"的缺口
        outage = r["apply_id"] in ONLINE_OUTAGE_APPLIES
        _write_online_facts(db, r, outage)
    # 线上事实登记为一个正式 run，审计链可追溯
    db.start_run(ONLINE_RUN, "online_export", "online_engine", DEFAULT_CONFIG, "imported")


def _write_online_facts(db, r: dict, outage: bool) -> None:
    """把线上判决翻译成 policy_eval_fact（ONLINE_BASELINE 行）。

    教学要点：线上导出的判决表只会有"执行到"的策略 —— 后面环节根本没跑。
    所以 NOT_EXECUTED 的行在这里天然缺失，离线重放必须自己补齐。
    """
    for stage, status in r["_stage_out"].items():
        for pid, fn in _stage_policies(stage):
            if stage == "S3_CR" and outage and pid == "P_CR_001":
                # 埋的故障：当天征信源故障，线上这一策略没有值
                db.conn.execute(
                    "INSERT OR REPLACE INTO policy_eval_fact VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
                    (ONLINE_RUN, "online_engine", r["apply_id"], stage, pid,
                     None, "MISSING_SOURCE", "6", "UNKNOWN", "R_SYS_001",
                     "当日征信数据源故障，未取得值"))
                continue
            data = {s: _payloads(r)[s] for s in _stage_sources(stage)}
            out = fn(data, DEFAULT_CONFIG)
            db.conn.execute(
                "INSERT OR REPLACE INTO policy_eval_fact VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
                (ONLINE_RUN, "online_engine", r["apply_id"], stage, pid,
                 json.dumps(out.raw_value, ensure_ascii=False, default=str)
                 if out.raw_value is not None else None,
                 out.value_state,
                 json.dumps(out.threshold_used, ensure_ascii=False, default=str)
                 if out.threshold_used is not None else None,
                 out.result, out.reason_code, out.detail))
    db.conn.commit()


# 延迟导入避免循环依赖
def _stage_policies(stage: str):
    from .policies import STAGE_POLICIES
    return STAGE_POLICIES[stage]


def _stage_sources(stage: str):
    from .codes import STAGE_SOURCES
    return STAGE_SOURCES[stage]


# ---------------------------------------------------------------------------
# 离线重放引擎 —— 回测的成本模型：读快照（0 元）× 换配置重放
# ---------------------------------------------------------------------------

def run_offline(db, run_id: str, config_name: str, cfg: dict,
                auditor=None, rows: list[dict] | None = None) -> dict[str, Any]:
    """全量重放：读快照 -> 逐环节评估 -> 写长表 -> 返回汇总。

    教学要点（对照审批系统）：
      - 没有 SourceHub、没有计费、没有重试熔断 —— 外部数据是历史快照；
      - 前面环节拒绝时，后面环节写 NOT_EXECUTED 事实（线上没有这些行，
        这是离线重放对漏斗结构的"补全"，也是补数缺口分析的输入）；
      - 放宽方向的可信边界在这里体现：重放可以精确算通过率（特征都在），
        但增量人群的 bad_flag 是 NULL —— 坏账率不可得，只能标注等级。
    """
    if rows is None:
        rows = db.applicants()
    db.start_run(run_id, "replay", config_name, cfg,
                 hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:16])
    audit = auditor or db.auditor(run_id)
    audit.append("replay.started", {"config_name": config_name,
                                    "config": cfg, "n": len(rows)})
    n_pass = n_reject = n_review = 0
    for r in rows:
        final = "REJECT"
        for stage in STAGE_ORDER:
            # 读快照组装环节输入 —— "快照表是唯一事实，节点间不传大对象"
            snaps = db.get_snapshots(r["apply_id"])
            data = {s: (snaps[s]["payload"] if snaps.get(s, {}).get("ok") else None)
                    for s in STAGE_SOURCES[stage]}
            res = evaluate_stage(stage, data, cfg)
            db.put_facts(run_id, config_name, r["apply_id"], res)
            if res.status != "PASS":
                final = res.status
                break                       # 漏斗短路：拒绝后环节不再执行
        else:
            final = "PASS"
        n_pass += final == "PASS"
        n_reject += final == "REJECT"
        n_review += final == "REVIEW"
    headline = {"n": len(rows), "pass": n_pass, "reject": n_reject,
                "review": n_review,
                "pass_rate": round(n_pass / len(rows), 4)}
    # 坏账口径：只有真实放款的人有标签（bad_flag 非空）。报告必须标注来源等级。
    granted = [r for r in rows if r["bad_flag"] is not None]
    if granted:
        bad = sum(1 for r in granted if r["bad_flag"] == 1)
        headline["bad_rate_measured"] = round(bad / len(granted), 4)
        headline["bad_rate_level"] = SOURCE_LEVEL["MEASURED"]
    audit.append("replay.finished", headline)
    db.finish_run(run_id, headline)
    return headline


def stage_data(db, apply_id: str, stage: str) -> dict[str, dict | None]:
    """给指标层用的环节输入快照（只读）。"""
    snaps = db.get_snapshots(apply_id)
    return {s: (snaps[s]["payload"] if snaps.get(s, {}).get("ok") else None)
            for s in STAGE_SOURCES[stage]}
