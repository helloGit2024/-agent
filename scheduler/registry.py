# -*- coding: utf-8 -*-
"""定时任务注册表（V2.5 §8.5）：job = 一次参数被冻结的函数调用。

════════════════════════════════════════════════════════════════════
最容易做错的方式：为报表另写一套计算
════════════════════════════════════════════════════════════════════
那等于承认"两套口径"，而且两套都得维护。正确形态是：
    job 的定义 = (metric_id, 冻结参数, schedule, 输出表, 版本)
    跑 job 的实现 = compute_model_metric(...) → 写表
系统里**不应该存在 report_calc.py 这种文件**。一旦出现"报表专用计算"，
就再也无法回答"报告里的数和 Agent 说的数为什么不一样"。

════════════════════════════════════════════════════════════════════
两个免费收益
════════════════════════════════════════════════════════════════════
① 对账自动化：报告里的任一数字，用相同参数问 Agent 必须逐位复现。
② base_dist 回流：物化表里的基准分布成为实时 PSI 的输入 ——
   "计算逻辑在同一处"从口号变成可验证的事实。

════════════════════════════════════════════════════════════════════
升格与下线（否则物化表会无限膨胀）
════════════════════════════════════════════════════════════════════
同一 (metric_id, params) 被问过 ≥5 次，或进入监控需求 → 升格为定时任务；
某 job 连续 30 天零消费 → 下线，历史表保留（失效不删除）。
"""
from __future__ import annotations

import json

from core import catalog, model_metrics

from . import promotion

MODEL_HEALTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS model_health_daily (
    as_of         TEXT NOT NULL,
    model_id      TEXT NOT NULL,
    product       TEXT,
    metric_id     TEXT,
    score_bin     INTEGER NOT NULL DEFAULT 0,   -- 标量指标为 0（NULL 不能做 PK）
    n             INTEGER,
    bad_rate      REAL,
    woe           REAL,
    iv_contrib    REAL,
    lift          REAL,
    cum_ks        REAL,
    ks_max        REAL,
    auc           REAL,
    psi           REAL,
    base_dist     TEXT,       -- ★ 基准分布：回流给实时 PSI
    param_version TEXT,
    job_id        TEXT,
    signature     TEXT,
    PRIMARY KEY (as_of, model_id, product, metric_id, score_bin)
);
"""

POLICY_MONITOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS policy_monitor_daily (
    as_of         TEXT NOT NULL,
    product       TEXT NOT NULL,
    metric_id     TEXT NOT NULL,
    value         REAL,
    numerator_n   INTEGER,
    denominator_n INTEGER,
    param_version TEXT,
    job_id        TEXT,
    signature     TEXT,
    PRIMARY KEY (as_of, product, metric_id)
);
"""

# 固定监控：策略侧 1 个 + 模型侧 2 个。
# 参数一旦冻结就是"口径"：改 label / 改箱数 = version bump，历史不覆盖。
JOBS: list[dict] = [
    {"job_id": "policy_funnel_daily_v1", "metric_id": "approval_rate",
     "params": {"window": {"label": "近一周", "from": "2026-06-24",
                           "to": "2026-06-30"},
                "product": "P_CASH_001", "dimension": "overall"},
     "schedule": "0 6 * * *", "output_table": "policy_monitor_daily",
     "version": "1.0"},
    {"job_id": "model_health_daily_v1", "metric_id": "ks",
     "params": {"window": {"from": "2026-04-01", "to": "2026-06-30"},
                "product": "P_CASH_001", "dimension": "overall",
                "bins": 5, "label": "fpd30"},
     "schedule": "0 6 * * *", "output_table": "model_health_daily",
     "version": "1.0", "ref_period": "train_2025Q4"},
    {"job_id": "model_psi_daily_v1", "metric_id": "psi",
     "params": {"window": {"from": "2026-04-01", "to": "2026-06-30"},
                "product": "P_CASH_001", "dimension": "overall",
                "bins": 5, "label": "fpd30", "ref_period": "train_2025Q4"},
     "schedule": "0 6 * * *", "output_table": "model_health_daily",
     "version": "1.0"},
]

JOBS_BY_ID = {j["job_id"]: j for j in JOBS}


def ensure_tables(db) -> None:
    db.conn.executescript(MODEL_HEALTH_SCHEMA)
    db.conn.executescript(POLICY_MONITOR_SCHEMA)
    db.conn.commit()


def run_job(db, job_id: str, as_of: str = catalog.AS_OF) -> dict:
    """跑一个 job：就是"用冻结参数调那一个函数"，然后落库。"""
    job = JOBS_BY_ID.get(job_id)
    if job is None:
        return {"ok": False, "code": "NOT_REGISTERED", "job_id": job_id}
    ensure_tables(db)
    # T-105：已下线的 job 不再执行（数据留着，调度停了）。
    # 静默跳过比"继续跑"安全：继续跑会让下线这件事看起来没生效。
    if promotion.is_retired(db, job_id):
        return {"ok": False, "code": "JOB_RETIRED", "job_id": job_id,
                "reason": "该任务已下线（历史数据保留，调度停止）"}
    mid = job["metric_id"]
    params = dict(job["params"])

    if mid in model_metrics.MODEL_CATALOG:
        res = model_metrics.compute_model_metric(
            mid, params, role="system", as_of=as_of, db=db)
        if not res.get("ok"):
            return {"ok": False, "job_id": job_id, **res}
        n = _write_model_health(db, job, res, as_of)
    else:
        # 策略监控也是同一个入口：catalog.compute（口径唯一实现）
        chk = catalog.guard_call(mid, params, role="system", as_of=as_of)
        if not chk.get("ok"):
            return {"ok": False, "job_id": job_id, **chk}
        p = chk["params"]
        win = p["window"] or catalog.parse_window("近一个月", as_of)
        view = catalog.build_view(db, "ONLINE_v2.2", win, p["product"],
                                  row_filter=chk.get("row_filter"))
        res = catalog.compute(mid, view)
        n = _write_policy_monitor(db, job, res, p, as_of)

    return {"ok": True, "job_id": job_id, "metric_id": mid, "rows": n,
            "as_of": as_of, "param_version": job["version"],
            "signature": res.get("signature")
            or model_metrics.signature(mid, params, as_of),
            "source": res.get("source", "MEASURED")}


def _write_model_health(db, job: dict, res: dict, as_of: str) -> int:
    rows = (res.get("bins") or {}).get("rows") or []
    sig = res["signature"]
    base_dist = None
    if job["metric_id"] == "ks":
        # 基准分布随 KS 一起物化：实时 PSI 之后直接读它，不再重算基准期
        edges = (res.get("bins") or {}).get("edges") or []
        shares, _, _ = model_metrics.base_shares(
            db, res["product"], model_metrics.REF_PERIODS[job["ref_period"]], edges)
        if shares:
            base_dist = json.dumps({"shares": [round(s, 6) for s in shares],
                                    "edges": edges})
    if not rows:
        rows = [{"bin": 0}]
    for r in rows:
        db.conn.execute(
            "INSERT OR REPLACE INTO model_health_daily "
            "(as_of, model_id, product, metric_id, score_bin, n, bad_rate, woe, "
            " iv_contrib, lift, cum_ks, ks_max, auc, psi, base_dist, "
            " param_version, job_id, signature) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (as_of, model_metrics.MODEL_ID, res["product"], job["metric_id"],
             int(r.get("bin") or 0), r.get("n"), r.get("bad_rate"),
             r.get("woe"), r.get("iv_contrib"), r.get("lift"), r.get("cum_ks"),
             res.get("ks_max") if r.get("bin") == (rows[0].get("bin")) else None,
             res.get("value") if job["metric_id"] == "auc" else None,
             res.get("value") if job["metric_id"] == "psi" else None,
             base_dist, job["version"], job["job_id"], sig))
    db.conn.commit()
    return len(rows)


def _write_policy_monitor(db, job: dict, res: dict, params: dict, as_of: str) -> int:
    db.conn.execute(
        "INSERT OR REPLACE INTO policy_monitor_daily VALUES (?,?,?,?,?,?,?,?,?)",
        (as_of, params["product"], job["metric_id"], res.get("value"),
         res.get("numerator_n"), res.get("denominator_n"), job["version"],
         job["job_id"], model_metrics.signature(job["metric_id"], params, as_of)))
    db.conn.commit()
    return 1


def run_all(db, as_of: str = catalog.AS_OF) -> list[dict]:
    return [run_job(db, j["job_id"], as_of) for j in JOBS]


# ---------------------------------------------------------------------------
# Agent 的三级路由：命中物化 → MATERIALIZED；参数不同 → MEASURED
# ---------------------------------------------------------------------------
def find_materialized(db, metric_id: str, params: dict,
                      as_of: str = catalog.AS_OF) -> dict | None:
    """参数与某个 job **完全一致**时才算命中（差一个参数就不算）。"""
    sig = model_metrics.signature(metric_id, params, as_of)
    for job in JOBS:
        if job["metric_id"] != metric_id:
            continue
        if job.get("output_table") != "model_health_daily":
            continue
        if model_metrics.signature(job["metric_id"], job["params"], as_of) != sig:
            continue
        if promotion.is_retired(db, job["job_id"]):
            continue                       # 已下线：既不命中，也不回落实时算
        try:
            rows = [dict(r) for r in db.conn.execute(
                "SELECT * FROM model_health_daily WHERE job_id=? AND signature=? "
                "ORDER BY score_bin", (job["job_id"], sig)).fetchall()]
        except Exception:                  # noqa: BLE001  表还没建
            return None
        if not rows:
            return None
        return {"job_id": job["job_id"], "param_version": job["version"],
                "as_of": rows[0]["as_of"], "rows": rows,
                "base_dist": rows[0]["base_dist"],
                "source": "MATERIALIZED"}
    return None


def latest_health(db, product: str, as_of: str | None = None) -> dict | None:
    """读物化表（get_model_health 工具用）。没有命中就说没有，不回落实时算。"""
    try:
        q = ("SELECT * FROM model_health_daily WHERE product=?"
             + (" AND as_of<=?" if as_of else "") + " ORDER BY as_of DESC, metric_id")
        args = (product, as_of) if as_of else (product,)
        rows = [dict(r) for r in db.conn.execute(q, args).fetchall()]
    except Exception:                      # noqa: BLE001
        return None
    if not rows:
        return None
    return {"as_of": rows[0]["as_of"], "rows": rows,
            "job_id": rows[0]["job_id"], "param_version": rows[0]["param_version"]}
