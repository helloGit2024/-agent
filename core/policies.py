# -*- coding: utf-8 -*-
"""策略层 —— 四层抽象中"可重放"的地基：阈值参数化 + 纯函数。

与 approval_lab/core/policies.py 的关系：
  策略逻辑一字未改（实名/年龄/证件/反欺诈/信用/额度 12 条），改的是一件事：
  【阈值从写死的字面量变成 config 参数】。这一个改动就是"可重放"的全部：

      审批系统:  if n > 6: reject          # 6 是字面量，改它=改代码=不可重放
      回测系统:  if n > cfg["P_CR_001"]: reject   # cfg 是参数，换 cfg 就能重放任何版本

  为什么这是整个项目的地基：
    回测的本质是"同一批人 × 不同策略配置 = 不同判决"。
    策略函数保持纯函数（输入 data + cfg，输出 PolicyOutcome，不碰 db/图），
    才能离线批量重放历史数据。这是"决策与执行分离"的直接收益。

三值语义（沿用审批系统，回测同样必须坚持）：
    PASS    —— 有证据，且证据支持通过
    FAIL    —— 有证据，且证据不支持
    UNKNOWN —— 没证据（数据源挂了/哨兵值/业务空值）。绝不许当 PASS 放行
"""
from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from .codes import POLICY_NAMES, POLICY_REQUIRES, STAGE_SOURCES

Verdict = Literal["PASS", "FAIL", "UNKNOWN"]
StageStatus = Literal["PASS", "REJECT", "REVIEW", "ERROR"]

# ---------------------------------------------------------------------------
# 策略配置（阈值包）—— 回测的"重放对象"
# ---------------------------------------------------------------------------
# 教学要点：这就是设计文档说的"四层对象"中的 L2（策略配置层）。
#   线上每跑一个版本的策略，这里就对应一个 config 快照。
#   回测 what-if = 换一个 config 重放全部历史申请。

# 默认配置 = "线上当前版本"（回测第 0 步要用它核对判决一致率）
DEFAULT_CONFIG: dict[str, Any] = {
    "P_ADM_001": None,      # 实名核验：布尔判定，无阈值
    "P_ADM_002": None,      # 年龄准入：区间来自产品配置，无策略阈值
    "P_ADM_003": 60,        # OCR 质量分下限
    "P_AF_001": None,       # 反欺诈名单：命中即拒
    "P_AF_002": 5,          # 设备关联账户数上限
    "P_AF_003": 3,          # 24h 申请次数上限
    "P_CR_001": 6,          # 近 3 个月征信查询次数上限 ★ 本次回测的候选调整对象
    "P_CR_002": 30,         # 最大逾期天数上限
    "P_CR_003": None,       # 内部不良：布尔判定
    "P_CR_004": 0.55,       # 负债收入比上限
    "P_LIM_001": None,      # 额度测算：公式型
    "P_LIM_002": None,      # 收入可核性：布尔判定
}

# 演示用"候选配置"：收紧 P_CR_001 到 4（教学故事：5~6 区间坏账率超容忍线）
CANDIDATE_TIGHTEN = {**DEFAULT_CONFIG, "P_CR_001": 4}
# 演示用"候选配置"：放宽 P_CR_001 到 9（教学故事：7~9 区间疑似白拒，但无标签）
CANDIDATE_RELAX = {**DEFAULT_CONFIG, "P_CR_001": 9}


@dataclass
class PolicyOutcome:
    """单策略判决。回测里它是 policy_eval_fact 长表的一行。"""
    policy_id: str
    name: str
    result: Verdict
    reason_code: str | None = None
    detail: str = ""
    evidence: list[str] = field(default_factory=list)
    hit: bool = False                      # 是否真的命中拒绝条件（拒绝率统计用）
    raw_value: Any = None                  # ★ 本次判决实际读到的原始值（回测存快照）
    value_state: str = "OK"                # ★ 五态之一（codes.VALUE_STATE）
    threshold_used: Any = None             # ★ 本次判决用的阈值（config 溯源）

    def as_dict(self) -> dict[str, Any]:
        return {"policy_id": self.policy_id, "name": self.name, "result": self.result,
                "reason_code": self.reason_code, "detail": self.detail,
                "evidence": self.evidence, "hit": self.hit,
                "raw_value": self.raw_value, "value_state": self.value_state,
                "threshold_used": self.threshold_used}


@dataclass
class StageResult:
    """环节判决聚合（含可复现三件套）。"""
    stage: str
    status: StageStatus
    outcomes: list[PolicyOutcome]
    reason_codes: list[str]
    detail: str
    data_hash: str = ""
    missing: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"stage": self.stage, "status": self.status,
                "reason_codes": self.reason_codes, "detail": self.detail,
                "data_hash": self.data_hash, "missing": self.missing,
                "outcomes": [o.as_dict() for o in self.outcomes]}


# ---------------------------------------------------------------------------
# 五态判定辅助 —— 把"数据没值"的原因写清楚（审批系统里这只是一个 None）
# ---------------------------------------------------------------------------

def value_state_of(payload: dict | None, field_name: str | None = None) -> str:
    """给一个源 payload（或其中的字段）定五态。

    规则（教学要点：顺序就是判定的优先级）：
      payload 为 None            -> MISSING_SOURCE（源没回来）
      字段值是哨兵(-1/-999/None) -> SENTINEL / BIZ_EMPTY
      其余                       -> OK
    """
    if payload is None:
        return "MISSING_SOURCE"
    if field_name is None:
        return "OK"
    v = payload.get(field_name)
    if v is None:
        return "BIZ_EMPTY"
    if v in (-1, -999, "-1", "-999"):
        return "SENTINEL"
    return "OK"


# ---------------------------------------------------------------------------
# 12 条策略（纯函数）。每条都返回 raw_value / value_state / threshold_used，
# 因为回测事实表 policy_eval_fact 必须存"判决当时读到的原始值 + 用的阈值"，
# 否则 what-if 重放时无法反推"这个人在另一档阈值下会被怎么判"。
# ---------------------------------------------------------------------------

def p_adm_realname(d: dict, cfg: dict) -> PolicyOutcome:
    rn = d["DS_REALNAME_01"]
    st = value_state_of(rn)
    if st != "OK":
        return PolicyOutcome("P_ADM_001", "实名核验", "UNKNOWN", "R_SYS_001",
                             f"实名核验数据不可用（{st}）", ["DS_REALNAME_01"],
                             raw_value=None, value_state=st)
    if rn["matched"]:
        return PolicyOutcome("P_ADM_001", "实名核验", "PASS",
                             detail="三要素一致", evidence=["DS_REALNAME_01"],
                             raw_value=rn["matched"], value_state=st)
    return PolicyOutcome("P_ADM_001", "实名核验", "FAIL", "R_ADM_001",
                         "手机号实名信息与申请人身份不一致", ["DS_REALNAME_01"],
                         hit=True, raw_value=rn["matched"], value_state=st)


def p_adm_age(d: dict, cfg: dict) -> PolicyOutcome:
    user, prod = d["INT_USER"], d["INT_PROD_CFG"]
    st = value_state_of(user, "age")
    if st != "OK":
        return PolicyOutcome("P_ADM_002", "年龄准入", "UNKNOWN", "R_SYS_001",
                             f"年龄字段不可用（{st}）", ["INT_USER"],
                             raw_value=None, value_state=st)
    age = user["age"]
    if prod["age_min"] <= age <= prod["age_max"]:
        return PolicyOutcome("P_ADM_002", "年龄准入", "PASS",
                             detail=f"{age} 岁在产品准入区间内",
                             evidence=["INT_USER", "INT_PROD_CFG"],
                             raw_value=age, value_state=st)
    return PolicyOutcome("P_ADM_002", "年龄准入", "FAIL", "R_ADM_002",
                         f"{age} 岁不在产品准入区间", ["INT_USER", "INT_PROD_CFG"],
                         hit=True, raw_value=age, value_state=st)


def p_adm_ocr(d: dict, cfg: dict) -> PolicyOutcome:
    ocr, user = d["DS_OCR_02"], d["INT_USER"]
    thr = cfg["P_ADM_003"]
    quality = ocr.get("quality")
    st = value_state_of(ocr, "quality")
    # 教学要点：第一关是识别质量。质量不达标 = 没证据 = UNKNOWN，不能当"通过"。
    if quality is None or (st == "OK" and quality < thr):
        return PolicyOutcome("P_ADM_003", "证件一致性", "UNKNOWN", "R_ADM_005",
                             f"OCR 识别质量 {quality}，无法比对", ["DS_OCR_02", "INT_USER"],
                             raw_value=quality, value_state="BIZ_EMPTY", threshold_used=thr)
    if ocr["name"] != user["name"] or ocr["id_no"] != user["id_no"]:
        return PolicyOutcome("P_ADM_003", "证件一致性", "FAIL", "R_ADM_003",
                             "证件信息与填报信息不一致", ["DS_OCR_02", "INT_USER"],
                             hit=True, raw_value=quality, value_state=st, threshold_used=thr)
    return PolicyOutcome("P_ADM_003", "证件一致性", "PASS", detail="证件信息一致",
                         evidence=["DS_OCR_02", "INT_USER"],
                         raw_value=quality, value_state=st, threshold_used=thr)


def p_af_blacklist(d: dict, cfg: dict) -> PolicyOutcome:
    lis = d["DS_AF_LIST_03"]
    st = value_state_of(lis)
    if st != "OK":
        return PolicyOutcome("P_AF_001", "反欺诈名单", "UNKNOWN", "R_SYS_001",
                             f"黑名单数据不可用（{st}）", ["DS_AF_LIST_03"],
                             raw_value=None, value_state=st)
    if lis["hit"]:
        return PolicyOutcome("P_AF_001", "反欺诈名单", "FAIL", "R_AF_001",
                             f"命中{lis['list_name']}", ["DS_AF_LIST_03"],
                             hit=True, raw_value=True, value_state=st)
    return PolicyOutcome("P_AF_001", "反欺诈名单", "PASS", detail="未命中黑名单",
                         evidence=["DS_AF_LIST_03"], raw_value=False, value_state=st)


def p_af_device(d: dict, cfg: dict) -> PolicyOutcome:
    dev = d["DS_DEVICE_04"]
    thr = cfg["P_AF_002"]
    n = dev.get("linked_accounts")
    st = value_state_of(dev, "linked_accounts")
    if n is None:
        return PolicyOutcome("P_AF_002", "设备关联", "UNKNOWN", "R_SYS_001",
                             f"设备指纹缺失（{st}）", ["DS_DEVICE_04"],
                             raw_value=None, value_state=st, threshold_used=thr)
    if n >= thr:
        return PolicyOutcome("P_AF_002", "设备关联", "FAIL", "R_AF_002",
                             f"设备关联 {n} 个账户", ["DS_DEVICE_04"],
                             hit=True, raw_value=n, value_state=st, threshold_used=thr)
    return PolicyOutcome("P_AF_002", "设备关联", "PASS", detail=f"设备关联 {n} 个账户",
                         evidence=["DS_DEVICE_04"], raw_value=n, value_state=st, threshold_used=thr)


def p_af_freq(d: dict, cfg: dict) -> PolicyOutcome:
    hist = d["INT_APPLY_HIST"]
    thr = cfg["P_AF_003"]
    n = hist.get("apply_24h")
    st = value_state_of(hist, "apply_24h")
    if n is None:
        return PolicyOutcome("P_AF_003", "申请频次", "UNKNOWN", "R_SYS_001",
                             f"申请历史缺失（{st}）", ["INT_APPLY_HIST"],
                             raw_value=None, value_state=st, threshold_used=thr)
    if n > thr:
        return PolicyOutcome("P_AF_003", "申请频次", "FAIL", "R_AF_003",
                             f"24 小时内申请 {n} 次", ["INT_APPLY_HIST"],
                             hit=True, raw_value=n, value_state=st, threshold_used=thr)
    return PolicyOutcome("P_AF_003", "申请频次", "PASS", detail=f"24 小时内申请 {n} 次",
                         evidence=["INT_APPLY_HIST"], raw_value=n, value_state=st, threshold_used=thr)


def p_cr_query(d: dict, cfg: dict) -> PolicyOutcome:
    """征信查询次数 —— 本次回测的教学主角。

    哨兵值教学：征信用 -1 表示"查无记录"。
      审批侧：-1 不是 0，必须 UNKNOWN 转人工（把"查无"当"零次"会误批）；
      回测侧：分箱统计时 -1 必须单独成箱（SENTINEL 态），混进数值箱会拉歪均值。
    """
    b = d["DS_BUREAU_05"]
    thr = cfg["P_CR_001"]
    n = b.get("query_3m")
    st = value_state_of(b, "query_3m")
    if n is None or st == "SENTINEL":
        return PolicyOutcome("P_CR_001", "征信查询次数", "UNKNOWN", "R_SYS_001",
                             f"征信返回哨兵值/缺失（{st}），无法判定查询次数",
                             ["DS_BUREAU_05"], raw_value=n, value_state=st, threshold_used=thr)
    if n > thr:
        return PolicyOutcome("P_CR_001", "征信查询次数", "FAIL", "R_CR_001",
                             f"近 3 个月查询 {n} 次，超上限 {thr} 次",
                             ["DS_BUREAU_05", "INT_PROD_CFG"],
                             hit=True, raw_value=n, value_state=st, threshold_used=thr)
    return PolicyOutcome("P_CR_001", "征信查询次数", "PASS", detail=f"近 3 个月查询 {n} 次",
                         evidence=["DS_BUREAU_05", "INT_PROD_CFG"],
                         raw_value=n, value_state=st, threshold_used=thr)


def p_cr_overdue(d: dict, cfg: dict) -> PolicyOutcome:
    b = d["DS_BUREAU_05"]
    thr = cfg["P_CR_002"]
    days = b.get("max_overdue_days")
    st = value_state_of(b, "max_overdue_days")
    if days is None or st == "SENTINEL":
        return PolicyOutcome("P_CR_002", "逾期记录", "UNKNOWN", "R_SYS_001",
                             f"逾期字段哨兵/缺失（{st}）", ["DS_BUREAU_05"],
                             raw_value=days, value_state=st, threshold_used=thr)
    if days > thr:
        return PolicyOutcome("P_CR_002", "逾期记录", "FAIL", "R_CR_002",
                             f"最大逾期 {days} 天", ["DS_BUREAU_05"],
                             hit=True, raw_value=days, value_state=st, threshold_used=thr)
    return PolicyOutcome("P_CR_002", "逾期记录", "PASS", detail=f"最大逾期 {days} 天",
                         evidence=["DS_BUREAU_05"], raw_value=days, value_state=st, threshold_used=thr)


def p_cr_internal(d: dict, cfg: dict) -> PolicyOutcome:
    h = d["INT_LOAN_HIST"]
    bad = h.get("hist_unpaid_bad")
    st = value_state_of(h, "hist_unpaid_bad")
    # 哨兵值教学：内部库用 -999 表示"无信贷历史"（不是 0 笔不良！）
    if bad is None or st == "SENTINEL":
        return PolicyOutcome("P_CR_003", "内部不良", "UNKNOWN", "R_SYS_001",
                             f"内部历史哨兵/缺失（{st}）", ["INT_LOAN_HIST"],
                             raw_value=bad, value_state=st)
    if bad > 0:
        return PolicyOutcome("P_CR_003", "内部不良", "FAIL", "R_CR_003",
                             f"内部存在未结清不良 {bad} 笔", ["INT_LOAN_HIST"],
                             hit=True, raw_value=bad, value_state=st)
    return PolicyOutcome("P_CR_003", "内部不良", "PASS", detail="无内部不良记录",
                         evidence=["INT_LOAN_HIST"], raw_value=bad, value_state=st)


def p_cr_dti(d: dict, cfg: dict) -> PolicyOutcome:
    b, h, u = d["DS_BUREAU_05"], d["INT_LOAN_HIST"], d["INT_USER"]
    thr = cfg["P_CR_004"]
    debt = h.get("existing_debt_monthly")
    income = u.get("income_monthly")
    if debt is None or income in (None, 0):
        return PolicyOutcome("P_CR_004", "负债收入比", "UNKNOWN", "R_SYS_001",
                             "负债或收入字段缺失，无法计算 DTI",
                             ["DS_BUREAU_05", "INT_LOAN_HIST", "INT_USER"],
                             raw_value=None, value_state="BIZ_EMPTY", threshold_used=thr)
    dti = round((debt + b.get("debt_monthly", 0)) / max(income, 1e-6), 4)
    if dti > thr:
        return PolicyOutcome("P_CR_004", "负债收入比", "FAIL", "R_CR_004",
                             f"DTI {dti:.1%} 超上限 {thr:.0%}",
                             ["DS_BUREAU_05", "INT_LOAN_HIST", "INT_USER"],
                             hit=True, raw_value=dti, value_state="OK", threshold_used=thr)
    return PolicyOutcome("P_CR_004", "负债收入比", "PASS", detail=f"DTI {dti:.1%}",
                         evidence=["DS_BUREAU_05", "INT_LOAN_HIST"],
                         raw_value=dti, value_state="OK", threshold_used=thr)


def p_lim_calc(d: dict, cfg: dict) -> PolicyOutcome:
    prod, h, b, u = d["INT_PROD_CFG"], d["INT_LOAN_HIST"], d["DS_BUREAU_05"], d["INT_USER"]
    flow = d.get("DS_FLOW_06")
    st = value_state_of(flow)
    # DS_FLOW_06 非关键源：缺失时降级（用申报收入），但不许静默——五态要记 MISSING_SOURCE
    income = u["income_monthly"]
    if flow is not None and flow.get("inflow_avg") is not None:
        income = min(income, flow["inflow_avg"])
    debt = h.get("existing_debt_monthly", 0) + b.get("debt_monthly", 0)
    multiple = 8 if prod["product"] == "P_CASH_001" else 6
    limit = income * multiple - debt * 3
    if limit < prod["limit_min"]:
        return PolicyOutcome("P_LIM_001", "额度测算", "FAIL", "R_LIM_001",
                             f"可批 {limit:.0f} 元，低于起批 {prod['limit_min']} 元",
                             ["INT_USER", "INT_LOAN_HIST", "DS_BUREAU_05"],
                             hit=True, raw_value=round(limit, 2),
                             value_state="MISSING_SOURCE" if flow is None else "OK")
    return PolicyOutcome("P_LIM_001", "额度测算", "PASS",
                         detail=f"可批 {limit:.0f} 元（收入×{multiple} − 月供扣减）",
                         evidence=["INT_USER", "INT_LOAN_HIST", "DS_BUREAU_05", "DS_FLOW_06"],
                         raw_value=round(limit, 2),
                         value_state="MISSING_SOURCE" if flow is None else "OK")


def p_lim_manual(d: dict, cfg: dict) -> PolicyOutcome:
    """收入未核实时额度需人工确认 —— UNKNOWN 语义的典型使用。"""
    h = d["INT_LOAN_HIST"]
    verified = h.get("income_verified")
    st = value_state_of(h, "income_verified")
    if verified is None:
        return PolicyOutcome("P_LIM_002", "收入可核性", "UNKNOWN", "R_LIM_002",
                             "收入核实状态缺失（{st}）".format(st=st), ["INT_LOAN_HIST"],
                             raw_value=verified, value_state=st)
    if not verified:
        return PolicyOutcome("P_LIM_002", "收入可核性", "UNKNOWN", "R_LIM_002",
                             "收入未经核实，额度需人工确认", ["INT_LOAN_HIST"],
                             raw_value=verified, value_state=st)
    return PolicyOutcome("P_LIM_002", "收入可核性", "PASS", detail="收入来源可核实",
                         evidence=["INT_LOAN_HIST"], raw_value=verified, value_state=st)


STAGE_POLICIES: dict[str, list[tuple[str, Callable[[dict, dict], PolicyOutcome]]]] = {
    "S1_ADM": [("P_ADM_001", p_adm_realname), ("P_ADM_002", p_adm_age), ("P_ADM_003", p_adm_ocr)],
    "S2_AF":  [("P_AF_001", p_af_blacklist), ("P_AF_002", p_af_device), ("P_AF_003", p_af_freq)],
    "S3_CR":  [("P_CR_001", p_cr_query), ("P_CR_002", p_cr_overdue),
               ("P_CR_003", p_cr_internal), ("P_CR_004", p_cr_dti)],
    "S4_LIM": [("P_LIM_001", p_lim_calc), ("P_LIM_002", p_lim_manual)],
}


# ---------------------------------------------------------------------------
# 配置指纹 —— "当时用的哪版阈值"可以被复现
# ---------------------------------------------------------------------------

def bundle_hash(cfg: dict | None = None) -> str:
    """策略包指纹：阈值改一个数字，指纹就变。

    教学要点：指纹的输入是【函数源码 + config】。审批系统只有源码哈希，
    回测多了 config，因为回测的一切结论都要能回答"是哪版阈值跑出来的"。
    """
    cfg = cfg or DEFAULT_CONFIG
    try:
        parts = []
        for st in sorted(STAGE_POLICIES):
            for pid, fn in STAGE_POLICIES[st]:
                src = inspect.getsource(fn)
                parts.append(f"{st}:{pid}:{hashlib.sha256(src.encode()).hexdigest()[:8]}"
                             f":{json.dumps(cfg.get(pid), sort_keys=True)}")
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]
    except (OSError, TypeError):
        return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:16]


def data_fingerprint(stage: str, data: dict[str, dict | None]) -> str:
    """本环节实际吃到的数据指纹：同样的数据必须得到同样的哈希。"""
    h = hashlib.sha256()
    for s in STAGE_SOURCES[stage]:
        h.update(s.encode())
        h.update(b"|")
        h.update(json.dumps(data.get(s), sort_keys=True, ensure_ascii=False,
                            default=str).encode())
        h.update(b";")
    return h.hexdigest()[:16]


def _dedup(codes: list[str]) -> list[str]:
    """去重保序：多个策略依赖同一个挂掉的数据源时，拒绝原因只写一次。"""
    seen, out = set(), []
    for c in codes:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


# ---------------------------------------------------------------------------
# 环节评估器 —— 严格语义：没证据 != 通过；同环节全部执行，拒绝原因一次给全
# ---------------------------------------------------------------------------

def evaluate_stage(stage: str, data: dict[str, dict | None],
                   cfg: dict | None = None) -> StageResult:
    """对一个申请执行某环节全部策略。

    参数化的 stage 评估器 = 回测的"重放引擎"。
    同一批数据换 cfg 重放，就是 what-if；用 DEFAULT_CONFIG 重放，
    就是第 0 步的"线上复刻"（判决必须与线上 100% 一致）。
    """
    cfg = cfg or DEFAULT_CONFIG
    missing = [s for s in STAGE_SOURCES[stage] if data.get(s) is None]
    outcomes: list[PolicyOutcome] = []
    for pid, fn in STAGE_POLICIES[stage]:
        lacks = [s for s in POLICY_REQUIRES[pid] if data.get(s) is None]
        if lacks:
            # 依赖缺失 -> UNKNOWN，绝不跳过（跳过 = 放行 = 审批系统 V1 的教学反例）
            outcomes.append(PolicyOutcome(pid, POLICY_NAMES[pid], "UNKNOWN", "R_SYS_001",
                                          f"依赖数据缺失：{','.join(lacks)}", lacks,
                                          value_state="MISSING_SOURCE"))
            continue
        try:
            outcomes.append(fn(data, cfg))
        except Exception as e:               # 策略 bug 不允许炸掉整单 -> UNKNOWN
            outcomes.append(PolicyOutcome(pid, POLICY_NAMES[pid], "UNKNOWN", "R_SYS_001",
                                          f"策略执行异常 {type(e).__name__}: {e}",
                                          value_state="MISSING_SOURCE"))

    fails = [o for o in outcomes if o.result == "FAIL"]
    unknowns = [o for o in outcomes if o.result == "UNKNOWN"]
    if fails:
        return StageResult(stage, "REJECT", outcomes,
                           _dedup([o.reason_code for o in fails if o.reason_code]),
                           "；".join(f"{o.name}: {o.detail}" for o in fails),
                           data_hash=data_fingerprint(stage, data), missing=missing)
    if unknowns:
        return StageResult(stage, "REVIEW", outcomes,
                           _dedup([o.reason_code for o in unknowns if o.reason_code]),
                           "；".join(f"{o.name}: {o.detail}" for o in unknowns),
                           data_hash=data_fingerprint(stage, data), missing=missing)
    return StageResult(stage, "PASS", outcomes, [],
                       "；".join(f"{o.name}: {o.detail}" for o in outcomes),
                       data_hash=data_fingerprint(stage, data), missing=missing)


# import 期断言：环节声明必填的数据源，必须至少被一条策略消费。
# 与审批系统同一条规矩 —— "钱花了、数据拉回来了、没人用"的洞在部署时就炸。
_COVERED = {s for req in POLICY_REQUIRES.values() for s in req}
_ORPHANS = {st: [s for s in srcs if s not in _COVERED] for st, srcs in STAGE_SOURCES.items()}
_ORPHANS = {st: v for st, v in _ORPHANS.items() if v}
if _ORPHANS:
    raise RuntimeError(f"这些数据源被声明为环节必填，却没有任何策略消费：{_ORPHANS}")
