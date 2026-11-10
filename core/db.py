# -*- coding: utf-8 -*-
"""落库层 —— 回测系统的"事实来源"。

与 approval_lab/core/db.py 的关系：
  审计哈希链 / 幂等锁 / 快照复用三套机制原样继承（它们防的事故在回测同样存在：
  回测报告同样要能回答"谁在什么时候用哪版配置跑出来的"）。
  核心差异是一张新表：policy_eval_fact（策略评估事实长表）。

为什么回测的事实表是"长表"而不是审批系统的"宽判决表"：
  审批：一案一环节一行判决（stage_decision 主键 (apply_id, stage)）。
  回测：一次回测运行 × 一个申请 × 一个环节 × 一条策略 = 一行。
  原因：回测的分析单元是【策略】而不是【环节】，而且同一个申请会被
  多个配置版本重放多次（DEFAULT_CONFIG / 候选配置 / 补数后重放），
  行数 = 600 申请 × 12 策略 × 2~3 版配置。长表才好做分层统计与归因。

长表的灵魂两列（设计文档铁律 4）：
  raw_value     —— 本条策略实际读到的原始值。what-if 重放靠它反推
                   "这个人换个阈值会被怎么判"，不用重新拉数据源。
  value_state   —— 五态之一（codes.VALUE_STATE）。把"数据缺失/业务空值/
                   哨兵值/未执行"拆开，是所有统计可信的前提。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS applicant (
    apply_id        TEXT PRIMARY KEY,
    product_code    TEXT NOT NULL,
    channel         TEXT,                    -- 渠道（分层维度②）
    is_new_customer INTEGER NOT NULL,        -- 首贷=1 / 复贷=0（分层维度③）
    apply_month     TEXT NOT NULL,           -- vintage 月份（时间稳定性分析用）
    income_monthly  REAL,
    amount_applied  REAL NOT NULL,
    -- 贷后表现（只有放款的人有；拒绝的人为 NULL —— 不是 0！）
    loan_granted    INTEGER,                 -- 是否真实放款（表现期入口）
    bad_flag        INTEGER,                 -- MOB3 DPD30（1=坏，0=好，NULL=无表现）
    created_at      TEXT NOT NULL
);

-- ★ 回测核心事实表：长表（运行 × 申请 × 环节 × 策略 = 一行）
CREATE TABLE IF NOT EXISTS policy_eval_fact (
    run_id        TEXT NOT NULL,             -- 哪次回测运行
    config_name   TEXT NOT NULL,             -- 用哪版策略配置（default / candidate_x）
    apply_id      TEXT NOT NULL,
    stage         TEXT NOT NULL,
    policy_id     TEXT NOT NULL,
    raw_value     TEXT,                      -- 判决读到的原始值（JSON 编码）
    value_state   TEXT NOT NULL,             -- 五态（codes.VALUE_STATE）
    threshold     TEXT,                      -- 本次用的阈值（JSON 编码）
    result        TEXT NOT NULL,             -- PASS / FAIL / UNKNOWN
    reason_code   TEXT,
    detail        TEXT,
    executed      INTEGER NOT NULL DEFAULT 1,-- 是否真的执行到（NOT_EXECUTED=0）
    PRIMARY KEY (run_id, apply_id, stage, policy_id)
);

CREATE TABLE IF NOT EXISTS run (
    run_id       TEXT PRIMARY KEY,
    mode         TEXT NOT NULL,              -- diagnose / replay / relax_eval
    config_name  TEXT NOT NULL,
    config_json  TEXT NOT NULL,              -- 完整配置快照（可复现的根）
    policy_hash  TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    headline     TEXT                        -- 汇总指标 JSON（报告渲染的数字源）
);

CREATE TABLE IF NOT EXISTS ext_snapshot (
    apply_id     TEXT NOT NULL,
    source_code  TEXT NOT NULL,
    ok           INTEGER NOT NULL,
    payload      TEXT,
    payload_hash TEXT,
    fetched_at   TEXT,
    PRIMARY KEY (apply_id, source_code)
);

CREATE TABLE IF NOT EXISTS audit (
    run_id    TEXT NOT NULL,
    seq       INTEGER NOT NULL,
    ts        TEXT NOT NULL,
    event     TEXT NOT NULL,
    payload   TEXT,
    idem_key  TEXT,
    prev_hash TEXT NOT NULL,
    hash      TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
"""


class AuditChain:
    """哈希链审计 —— 与审批系统同款（防"事后不认账"）。

    回测为什么也要审计链：回测报告是要拿去拍板调策略的。三个月后有人问
    "当时的归因结论是哪版数据、哪版配置跑的"，审计链就是答案。
    """

    def __init__(self, conn: sqlite3.Connection, run_id: str):
        self.conn = conn
        self.run_id = run_id

    def append(self, event: str, payload: Any = None, idem_key: str | None = None) -> bool:
        if idem_key:                          # 幂等键：同一业务事件只允许落一次
            row = self.conn.execute(
                "SELECT 1 FROM audit WHERE run_id=? AND idem_key=? AND idem_key IS NOT NULL",
                (self.run_id, idem_key)).fetchone()
            if row:
                return False
        seq_row = self.conn.execute(
            "SELECT COALESCE(MAX(seq),0) FROM audit WHERE run_id=?",
            (self.run_id,)).fetchone()
        seq = seq_row[0] + 1
        prev = self.conn.execute(
            "SELECT hash FROM audit WHERE run_id=? AND seq=?",
            (self.run_id, seq - 1)).fetchone()
        prev_hash = prev[0] if prev else "GENESIS"
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        body = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        h = hashlib.sha256(f"{prev_hash}|{seq}|{ts}|{event}|{body}".encode()).hexdigest()
        self.conn.execute(
            "INSERT INTO audit VALUES (?,?,?,?,?,?,?,?)",
            (self.run_id, seq, ts, event, body, idem_key, prev_hash, h))
        self.conn.commit()
        return True

    def verify(self) -> tuple[bool, int]:
        rows = self.conn.execute(
            "SELECT seq, ts, event, payload, prev_hash, hash FROM audit "
            "WHERE run_id=? ORDER BY seq", (self.run_id,)).fetchall()
        prev = "GENESIS"
        for seq, ts, event, payload, prev_hash, h in rows:
            if prev_hash != prev:
                return False, len(rows)
            expect = hashlib.sha256(
                f"{prev_hash}|{seq}|{ts}|{event}|{payload}".encode()).hexdigest()
            if expect != h:
                return False, len(rows)
            prev = h
        return True, len(rows)


class Database:
    """回测库。线程锁简单化：回测是离线批处理，单写者即可。"""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._lock = threading.Lock()

    def close(self) -> None:
        self.conn.close()

    def auditor(self, run_id: str) -> AuditChain:
        return AuditChain(self.conn, run_id)

    # ---------------- 申请主档 ----------------
    def add_applicants(self, rows: Iterable[dict]) -> None:
        for r in rows:
            self.conn.execute(
                "INSERT OR REPLACE INTO applicant VALUES (?,?,?,?,?,?,?,?,?,?)",
                (r["apply_id"], r["product_code"], r.get("channel"),
                 int(r["is_new_customer"]), r["apply_month"], r["income_monthly"],
                 r["amount_applied"], r.get("loan_granted"), r.get("bad_flag"),
                 r.get("created_at") or time.strftime("%Y-%m-%dT%H:%M:%S")))
        self.conn.commit()

    def applicants(self, where: str = "", params: tuple = ()) -> list[dict]:
        sql = "SELECT * FROM applicant" + (f" WHERE {where}" if where else "")
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    # ---------------- 外部数据快照（回测只读线上快照，不再付费调用） ----------------
    def put_snapshot(self, apply_id: str, source: str, ok: bool, payload: Any) -> None:
        body = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        self.conn.execute(
            "INSERT OR REPLACE INTO ext_snapshot VALUES (?,?,?,?,?,?)",
            (apply_id, source, int(ok), body if ok else None,
             hashlib.sha256(body.encode()).hexdigest()[:16],
             time.strftime("%Y-%m-%dT%H:%M:%S")))

    def get_snapshots(self, apply_id: str) -> dict[str, dict]:
        rows = self.conn.execute(
            "SELECT * FROM ext_snapshot WHERE apply_id=?", (apply_id,)).fetchall()
        out = {}
        for r in rows:
            out[r["source_code"]] = {
                "ok": bool(r["ok"]),
                "payload": json.loads(r["payload"]) if r["payload"] else None,
            }
        return out

    # ---------------- 回测运行登记 ----------------
    def start_run(self, run_id: str, mode: str, config_name: str,
                  config: dict, policy_hash: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO run VALUES (?,?,?,?,?,?,?,?)",
            (run_id, mode, config_name, json.dumps(config, sort_keys=True),
             policy_hash, time.strftime("%Y-%m-%dT%H:%M:%S"), None, None))
        self.conn.commit()

    def finish_run(self, run_id: str, headline: dict) -> None:
        self.conn.execute(
            "UPDATE run SET finished_at=?, headline=? WHERE run_id=?",
            (time.strftime("%Y-%m-%dT%H:%M:%S"),
             json.dumps(headline, ensure_ascii=False, default=str), run_id))
        self.conn.commit()

    # ---------------- policy_eval_fact 长表 ----------------
    def put_facts(self, run_id: str, config_name: str, apply_id: str,
                  result) -> None:
        """把一个环节的 StageResult 展开成长表行。

        教学要点：NOT_EXECUTED 行也要写。漏斗越往后人越少，"没跑到"本身就是
        重要的统计事实 —— 后面环节的通过率分母是"进入人数"，这些行就是证据。
        """
        rows = []
        rejected_before = any(r.result == "FAIL" for r in result.outcomes)
        for o in result.outcomes:
            executed = 1 if o.result != "UNKNOWN" or o.value_state != "NOT_EXECUTED" else 0
            rows.append((
                run_id, config_name, apply_id, result.stage, o.policy_id,
                json.dumps(o.raw_value, ensure_ascii=False, default=str)
                if o.raw_value is not None else None,
                o.value_state,
                json.dumps(o.threshold_used, ensure_ascii=False, default=str)
                if o.threshold_used is not None else None,
                o.result, o.reason_code, o.detail, executed))
        self.conn.executemany(
            "INSERT OR REPLACE INTO policy_eval_fact VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        self.conn.commit()

    def facts(self, where: str = "1=1", params: tuple = ()) -> list[dict]:
        sql = "SELECT * FROM policy_eval_fact WHERE " + where
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]
