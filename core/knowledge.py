# -*- coding: utf-8 -*-
"""元数据与放款台账 —— V2 的"受控查询工具"层。

════════════════════════════════════════════════════════════════════
为什么需要这一层（没有它会怎样）
════════════════════════════════════════════════════════════════════
对话形态下有两类问题必须"查活的"，不能靠模型记忆、也不能进 RAG：

  ① 「##产品当前策略是什么？」
     策略天天在变。答案必须来自**策略版本表**，且必须带版本号 + 生效日期。
     没有版本戳的策略描述是定时炸弹 —— 三个月后有人拿着旧答案当现状用。

  ② 「FPD30 字段在哪个表、口径是什么？」
     字段位置与口径是**精确匹配**知识，语义检索会召回近似条目
     （把 P_INSTALL_003 的口径答成 P_CASH_001 的，比答不出来更糟）。

所以两类知识都做成确定性查表函数（受控工具三件套：只读、参数白名单、可审计），
而不是让 LLM 写 SQL、也不是做向量检索。

════════════════════════════════════════════════════════════════════
本模块第二部分：放款台账（loan_perf）
════════════════════════════════════════════════════════════════════
为什么放款台账要单独一张表，不和申请档混在一起？
  申请档回答"这个人申请了什么、批没批"；
  放款台账回答"钱什么时候出去、什么时候到期、表现如何"。
  两者的时间轴完全不同（申请日 vs 放款日 vs 到期日），
  合在一张表里必然出现"用申请日去算 FPD30"这种口径灾难。
  ★这也是 V1 里 FPD7/FPD30 根本算不出来的原因：只有 MOB3 一个标签。
"""
from __future__ import annotations

import hashlib
import json

from .codes import POLICY_NAMES, STAGE_ORDER, STAGE_SOURCES
from .policies import DEFAULT_CONFIG

# ---------------------------------------------------------------------------
# 一、策略版本表 —— "当前策略是什么"的唯一事实源
# ---------------------------------------------------------------------------
# 教学要点：版本条目只增不改（失效不删除）。口径/阈值改版时新增一条，
# 老条目保留 —— 否则"三个月前那版是什么"永远查不到，审计链也补不回来。
POLICY_VERSIONS: list[dict] = [
    {"product": "P_CASH_001", "version": "v2.1", "effective": "2026-01-01",
     "config": dict(DEFAULT_CONFIG), "note": "年初基线版本"},
    {"product": "P_CASH_001", "version": "v2.2", "effective": "2026-05-31",
     # 故事 D：6 月通过率下降的真相 —— 5 月底上线了收紧版本
     "config": {**DEFAULT_CONFIG, "P_CR_001": 3}, "note": "P_CR_001 6→3 收紧上线"},
    {"product": "P_INSTALL_003", "version": "v1.4", "effective": "2026-03-15",
     "config": dict(DEFAULT_CONFIG), "note": "分期产品基线"},
]

PRODUCTS = {
    "P_CASH_001": {"name": "现金贷", "aliases": ("现金贷", "P_CASH_001", "现金"),
                   "limit_min": 3_000, "limit_max": 200_000},
    "P_INSTALL_003": {"name": "消费分期", "aliases": ("消费分期", "分期", "P_INSTALL_003"),
                      "limit_min": 1_000, "limit_max": 50_000},
}


def resolve_product(text: str) -> dict:
    """产品别名 → 产品号。认不出来返回 None（触发反问，绝不默认成某个产品）。

    教学要点：默认成"最常见产品"看似贴心，实则是回测里最危险的一类 bug
    —— 用户问的是分期，报告给的是现金贷，且两边都"看起来合理"。
    """
    t = (text or "").replace(" ", "")
    for pid, meta in PRODUCTS.items():
        for a in meta["aliases"]:
            if a in t:
                return {"product": pid, "name": meta["name"], "alias": a}
    return {"product": None, "name": None, "alias": None}


def policy_flow(product: str, at_date: str) -> dict:
    """某产品在某日期生效的完整策略流程（含版本戳）。

    返回结构化流程 + version + effective —— 回答里必须带这三个字段。
    """
    cands = [v for v in POLICY_VERSIONS
             if v["product"] == product and v["effective"] <= at_date]
    if not cands:
        return {"product": product, "found": False,
                "reason": f"{product} 在 {at_date} 之前没有生效版本"}
    ver = max(cands, key=lambda v: v["effective"])
    cfg = ver["config"]
    stages = []
    for stage in STAGE_ORDER:
        from .policies import STAGE_POLICIES            # 延迟导入避免循环
        pols = []
        for pid, _fn in STAGE_POLICIES[stage]:
            thr = cfg.get(pid)
            pols.append({"policy_id": pid, "name": POLICY_NAMES.get(pid, pid),
                         "threshold": thr,
                         "threshold_text": _threshold_text(pid, thr, product)})
        stages.append({"stage": stage, "policies": pols,
                       "sources": STAGE_SOURCES[stage]})
    return {"product": product, "found": True, "version": ver["version"],
            "effective": ver["effective"], "note": ver["note"],
            "stages": stages, "config": cfg}


def _threshold_text(pid: str, thr, product: str) -> str:
    """阈值 → 业务人话（报告里"6"没有意义，"征信查询>6次拒绝"才有）。"""
    if thr is None:
        return "布尔判定（无阈值）"
    if pid == "P_CR_001":
        return f"近 3 个月征信查询次数 > {thr} 次拒绝"
    if pid == "P_AF_003":
        return f"24 小时申请次数 > {thr} 次拒绝"
    if pid == "P_AF_002":
        return f"设备关联账户数 > {thr} 拒绝"
    if pid == "P_CR_002":
        return f"最大逾期天数 > {thr} 天拒绝"
    if pid == "P_CR_004":
        return f"负债收入比 > {thr:.0%} 拒绝"
    if pid == "P_ADM_003":
        return f"OCR 质量分 < {thr} 转人工"
    return f"阈值 {thr}"


def version_history(product: str) -> list[dict]:
    return sorted([{"version": v["version"], "effective": v["effective"],
                    "note": v["note"]}
                   for v in POLICY_VERSIONS if v["product"] == product],
                  key=lambda v: v["effective"])


# ---------------------------------------------------------------------------
# 二、字段字典 —— "FPD30 在哪个库哪个表"的答案来源
# ---------------------------------------------------------------------------
FIELD_DICT: dict[str, dict] = {
    "FPD7": {"field": "FPD7", "database": "risk_dw", "table": "dwd_loan_perf",
             "grain": "借据（放款）", "desc": "首期应还日逾期 ≥7 天（1/0）",
             "owner": "资产质量组", "update": "T+1"},
    "FPD30": {"field": "FPD30", "database": "risk_dw", "table": "dwd_loan_perf",
              "grain": "借据（放款）", "desc": "首期应还日逾期 ≥30 天（1/0）",
              "owner": "资产质量组", "update": "T+1"},
    "BAD_FLAG": {"field": "BAD_FLAG", "database": "risk_dw", "table": "dws_loan_mob",
                 "grain": "借据（放款）", "desc": "MOB3 DPD30 坏账标记（1/0/NULL）",
                 "owner": "资产质量组", "update": "T+1"},
    "QUERY_3M": {"field": "QUERY_3M", "database": "risk_dw", "table": "dwd_bureau",
                 "grain": "申请（征信查询）", "desc": "近 3 个月征信查询次数，-1=查无记录",
                 "owner": "数据平台组", "update": "T+1"},
    "APPLY_24H": {"field": "APPLY_24H", "database": "risk_dw", "table": "dwd_apply_behavior",
                  "grain": "申请（行为）", "desc": "24 小时申请次数（内部行为埋点）",
                  "owner": "数据平台组", "update": "实时"},
}


def search_field(name: str) -> dict:
    """字段查询：精确匹配（大写），匹配不到时给相似项建议，绝不模糊猜。"""
    key = (name or "").strip().upper()
    if key in FIELD_DICT:
        return {"found": True, **FIELD_DICT[key]}
    similar = [k for k in FIELD_DICT if key and (key in k or k in key)]
    return {"found": False, "query": name, "similar": similar,
            "reason": "字段字典无此字段（回测暂不支持该字段的取数）"}


# ---------------------------------------------------------------------------
# 三、放款台账（loan_perf）—— FPD 系列标签的唯一来源
# ---------------------------------------------------------------------------
LOAN_SCHEMA = """
CREATE TABLE IF NOT EXISTS loan_perf (
    apply_id   TEXT PRIMARY KEY,
    loan_date  TEXT NOT NULL,     -- 放款日（到期口径的起点）
    fpd7       INTEGER,           -- 首期逾期>=7 天
    fpd30      INTEGER,           -- 首期逾期>=30 天
    mob3       INTEGER            -- MOB3 DPD30（= applicant.bad_flag，冗余一份便于联表）
);
"""


def _u01(apply_id: str) -> float:
    """确定性"随机数"：从 apply_id 哈希出 [0,1)，保证可复现且不动 V1 的随机序列。

    教学要点：夹具里凡是"故事需要的新随机量"，都不要去动老的 rng ——
    一旦多抽一次，后面所有特征全部错位，V1 的三个教学故事集体漂移。
    """
    h = hashlib.sha256(apply_id.encode()).hexdigest()[:8]
    return int(h, 16) / 0xFFFFFFFF


def ensure_loan_book(db, rows: list[dict], as_of: str) -> int:
    """把"放款了的人"写进台账，并按风险特征生成 FPD7 / FPD30 标签。

    参数 rows 来自 gen_population（含 query_3m 等特征）；只放款的人有台账。
    口径对齐真实业务：FPD7 ⊆ FPD30（同一个借据逾期 30 天必然已经逾期 7 天），
    用同一个 u 做单调映射保证这个包含关系，不做两套独立抽样。
    """
    db.conn.executescript(LOAN_SCHEMA)
    n = 0
    for r in rows:
        if not r.get("loan_granted"):
            continue
        u = _u01(r["apply_id"])
        q = r.get("query_3m") or 0
        p7 = 0.06
        if 5 <= q <= 6:
            p7 = 0.18                       # 与坏账故事 A 同向（高风险客群首逾也高）
        if (r.get("device_linked") or 0) >= 3:
            p7 += 0.03
        p30 = p7 * 1.6                      # 逾期天数越长比例越低 → 30 天只留一部分
        db.conn.execute(
            "INSERT OR REPLACE INTO loan_perf VALUES (?,?,?,?,?)",
            (r["apply_id"], str(r.get("created_at") or "")[:10],
             int(u < p7), int(u < p30), r.get("bad_flag")))
        n += 1
    db.conn.commit()
    return n


def loan_count(db) -> int:
    row = db.conn.execute("SELECT COUNT(*) FROM loan_perf").fetchone()
    return int(row[0])


def dump_version(product: str) -> str:
    """调试用：把某产品的版本历史打成一行 JSON。"""
    return json.dumps(version_history(product), ensure_ascii=False)
