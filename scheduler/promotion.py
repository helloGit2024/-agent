# -*- coding: utf-8 -*-
"""升格队列与任务下线（S0.9 T-105 / FR-105）。

════════════════════════════════════════════════════════════════════
"物化什么"不该由人拍脑袋
════════════════════════════════════════════════════════════════════
监控任务（`model_health_daily` 那三个）是**写死**的。但长期看，真正的
物化清单应该来自使用行为：同一组参数被反复问，就说明它值得冻结。

判定规则只有一条，且必须简单到能写在文档里：
    同一 (metric_id, signature) 被**真实提问**命中 ≥ N 次 → 进队列
N 默认 5（`BTA_PROMOTE_AFTER`）。

★为什么"进队列"而不是"自动建 job"
  建 job 意味着生产库上会多一张表、多一个调度、多一份要维护的口径。
  这属于变更，不是统计。队列只负责"提交候选"，批不批是人决定的 ——
  一个会自动改生产调度的系统，没有运维敢让它上线。

════════════════════════════════════════════════════════════════════
下线为什么要单独做
════════════════════════════════════════════════════════════════════
物化表一旦建了没人删，三年后会有 40 个 job、没人知道哪个还在用。
`retire_job` 把状态写进库（不是删数据），`find_materialized` 读状态并跳过。
**跳过而不是删表**：历史报告还要能引用当时那张表，删了就没法对账了。
"""
from __future__ import annotations

import json
import time
from datetime import datetime

from core import model_metrics, settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS promotion_queue (
    signature   TEXT PRIMARY KEY,          -- metric_id + 归一化参数 的哈希
    metric_id   TEXT NOT NULL,
    params      TEXT NOT NULL,             -- 归一化参数 JSON（人可读）
    hits        INTEGER NOT NULL DEFAULT 0,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending / promoted / dismissed
    job_id      TEXT,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS job_state (
    job_id     TEXT PRIMARY KEY,
    status     TEXT NOT NULL,              -- active / retired
    retired_at TEXT,
    note       TEXT
);
"""


def ensure(db) -> None:
    db.conn.executescript(SCHEMA)
    db.conn.commit()


def _days_since(ts: str | None) -> float:
    """距上次提问过了多少天。时间戳坏了就当"很久以前"（宁可重置，不要累加错）。"""
    if not ts:
        return 1e9
    try:
        return (datetime.now() - datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S")).total_seconds() / 86400
    except (ValueError, TypeError):
        return 1e9


def record_question(db, metric_id: str, params: dict,
                    as_of: str = model_metrics.catalog.AS_OF) -> dict:
    """记一次真实提问；达到阈值就进队列。返回该签名的当前状态。

    只由**工具壳**调用（用户提问题那条路）。定时任务自己调用会污染计数 ——
    那样每个 job 跑 5 天就把自己升格了，队列立刻失去意义。

    ★计数有**时间窗**（`BTA_PROMOTE_WINDOW_DAYS`，默认 30 天）：
      超过窗口没被问过，这次就从 1 重新数。没有窗口的话，"三年前问过 5 次"
      和"这周问过 5 次"是同一个结论 —— 队列会被历史热度占满，
      当前真正在用的口径反而永远排不进去。
    """
    ensure(db)
    sig = model_metrics.signature(metric_id, params, as_of)
    norm = model_metrics.normalize_params(metric_id, params, as_of)
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    row = db.conn.execute(
        "SELECT hits, status, last_seen FROM promotion_queue WHERE signature=?",
        (sig,)).fetchone()
    if row is None:
        db.conn.execute(
            "INSERT INTO promotion_queue(signature, metric_id, params, hits,"
            " first_seen, last_seen, status) VALUES (?,?,?,?,?,?, 'pending')",
            (sig, metric_id, json.dumps(norm, ensure_ascii=False), 1, now, now))
        hits, status = 1, "pending"
    else:
        stale = _days_since(row["last_seen"]) > settings.PROMOTE_WINDOW_DAYS
        hits = 1 if stale else int(row["hits"]) + 1
        status = row["status"]
        db.conn.execute(
            "UPDATE promotion_queue SET hits=?, last_seen=?,"
            " first_seen=CASE WHEN ? THEN ? ELSE first_seen END WHERE signature=?",
            (hits, now, 1 if stale else 0, now, sig))
    db.conn.commit()
    need = settings.PROMOTE_AFTER
    return {"signature": sig, "hits": hits, "status": status, "threshold": need,
            "window_days": settings.PROMOTE_WINDOW_DAYS,
            "should_promote": hits >= need and status == "pending"}


def queue(db, only_pending: bool = True) -> list[dict]:
    """队列（按命中次数降序）。"""
    ensure(db)
    sql = "SELECT * FROM promotion_queue"
    if only_pending:
        sql += " WHERE status='pending'"
    sql += " ORDER BY hits DESC, last_seen DESC"
    return [dict(r) for r in db.conn.execute(sql).fetchall()]


def mark_promoted(db, signature: str, job_id: str, note: str = "") -> None:
    """标记"已升格成 job"。只改状态，不建表 —— 建表是 DBA 的事。"""
    ensure(db)
    db.conn.execute("UPDATE promotion_queue SET status='promoted', job_id=?, note=?"
                    " WHERE signature=?", (job_id, note, signature))
    db.conn.commit()


def retire_job(db, job_id: str, note: str = "") -> None:
    """下线一个物化任务。**不删数据**，只改状态。"""
    ensure(db)
    db.conn.execute("INSERT OR REPLACE INTO job_state(job_id, status, retired_at, note)"
                    " VALUES (?,?,?,?)",
                    (job_id, "retired", time.strftime("%Y-%m-%dT%H:%M:%S"), note))
    db.conn.commit()


def job_status(db, job_id: str) -> str:
    ensure(db)
    row = db.conn.execute("SELECT status FROM job_state WHERE job_id=?",
                          (job_id,)).fetchone()
    return (row["status"] if row else "active")


def activate_job(db, job_id: str, note: str = "") -> None:
    """重新启用（下线是状态，不是删除；所以也能恢复）。"""
    ensure(db)
    db.conn.execute("INSERT OR REPLACE INTO job_state(job_id, status, retired_at, note)"
                    " VALUES (?,?,?,?)", (job_id, "active", None, note))
    db.conn.commit()


def is_retired(db, job_id: str) -> bool:
    return job_status(db, job_id) == "retired"
