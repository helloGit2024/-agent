# -*- coding: utf-8 -*-
"""编码映射表 —— 回测系统自己的"字典"，与审批系统对齐后加回测专有编码。

复用判断：审批系统的产品号 / 数据源号 / 拒绝码 / 环节号是跨系统契约，
回测系统的每个数字都要能追溯到这些 code（"P_CR_001 通过率下降"必须知道
P_CR_001 是哪条策略、在哪个环节、吃哪个数据源）。所以这一层直接继承审批
系统的编码，再补两组回测特有的编码：

  1. VALUE_STATE 五态 —— 把"数据缺失 / 业务空值 / 哨兵值 / 未执行"拆开。
     这回测设计文档的第 4 条铁律：把"未执行"算进"缺失"，数据源故障会被
     解释成客群特征，方向性误判。审批系统只用 UNKNOWN 一个词，回测必须拆细，
     因为回测的输入是"历史事实表"，错误的状态归因会污染所有下游统计。

  2. SOURCE_LEVEL 数据来源等级（四标注）—— 实测 / 实验 / 外推 / 不可得。
     回测报告最常见也最致命的问题：把不同可信度的数字混在一张表里不标注。
     报告里每个坏账率数字都必须带这个等级，否则"收紧重放的精确坏账率"和
     "放宽方向的反事实估计"会被读者当成同一种东西。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# 1. 环节（与审批系统完全对齐 —— 回测的对象就是这套审批漏斗）
# ---------------------------------------------------------------------------

STAGES: dict[str, dict] = {
    "S1_ADM": {"name": "准入评估", "order": 1, "critical": True},
    "S2_AF":  {"name": "反欺诈评估", "order": 2, "critical": True},
    "S3_CR":  {"name": "信用评估", "order": 3, "critical": True},
    "S4_LIM": {"name": "额度计算", "order": 4, "critical": False},
}
STAGE_ORDER: list[str] = sorted(STAGES, key=lambda s: STAGES[s]["order"])

# ---------------------------------------------------------------------------
# 2. 数据源（只需要价格与关键级别 —— 回测要算"数据源成本列"）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Source:
    code: str
    name: str
    kind: Literal["external", "internal"]
    unit_cost: float          # 每次调用成本（元）。回测的成本账靠它
    critical: bool


SOURCES: dict[str, Source] = {
    "DS_REALNAME_01": Source("DS_REALNAME_01", "运营商三要素实名核验", "external", 0.35, True),
    "DS_OCR_02":      Source("DS_OCR_02", "身份证 OCR 识别", "external", 0.12, True),
    "DS_AF_LIST_03":  Source("DS_AF_LIST_03", "反欺诈黑名单（联盟）", "external", 0.60, True),
    "DS_DEVICE_04":   Source("DS_DEVICE_04", "设备指纹风险库", "external", 0.20, True),
    "DS_BUREAU_05":   Source("DS_BUREAU_05", "央行征信报告", "external", 2.00, True),
    "DS_FLOW_06":     Source("DS_FLOW_06", "银行流水分析", "external", 1.20, False),
    "INT_USER":       Source("INT_USER", "用户主档", "internal", 0.0, True),
    "INT_APPLY_HIST": Source("INT_APPLY_HIST", "申请行为历史", "internal", 0.0, True),
    "INT_LOAN_HIST":  Source("INT_LOAN_HIST", "内部贷后表现", "internal", 0.0, True),
    "INT_PROD_CFG":   Source("INT_PROD_CFG", "产品参数配置", "internal", 0.0, True),
}

# ---------------------------------------------------------------------------
# 3. 策略与拒绝码（与审批系统对齐；回测归因的"最小分析单元"就是策略）
# ---------------------------------------------------------------------------

POLICY_NAMES = {
    "P_ADM_001": "实名核验", "P_ADM_002": "年龄准入", "P_ADM_003": "证件一致性",
    "P_AF_001": "反欺诈名单", "P_AF_002": "设备关联", "P_AF_003": "申请频次",
    "P_CR_001": "征信查询次数", "P_CR_002": "逾期记录", "P_CR_003": "内部不良",
    "P_CR_004": "负债收入比",
    "P_LIM_001": "额度测算", "P_LIM_002": "收入可核性",
}

REJECT_CODES: dict[str, dict] = {
    "R_ADM_001": {"stage": "S1_ADM", "text": "身份信息核验未通过"},
    "R_ADM_002": {"stage": "S1_ADM", "text": "年龄不符合产品准入范围"},
    "R_ADM_003": {"stage": "S1_ADM", "text": "身份证件信息与填报不一致"},
    "R_ADM_004": {"stage": "S1_ADM", "text": "身份证件已过期"},
    "R_ADM_005": {"stage": "S1_ADM", "text": "证件识别质量不足，需人工核验"},
    "R_AF_001":  {"stage": "S2_AF", "text": "命中反欺诈名单"},
    "R_AF_002":  {"stage": "S2_AF", "text": "设备关联多个可疑账户"},
    "R_AF_003":  {"stage": "S2_AF", "text": "短期申请频次异常"},
    "R_CR_001":  {"stage": "S3_CR", "text": "近期征信查询次数过多"},
    "R_CR_002":  {"stage": "S3_CR", "text": "存在严重逾期记录"},
    "R_CR_003":  {"stage": "S3_CR", "text": "存在未结清的不良贷款"},
    "R_CR_004":  {"stage": "S3_CR", "text": "负债收入比超出产品上限"},
    "R_LIM_001": {"stage": "S4_LIM", "text": "可批额度低于产品起批金额"},
    "R_LIM_002": {"stage": "S4_LIM", "text": "额度需人工复核"},
    "R_SYS_001": {"stage": "SYS", "text": "必需数据源不可用"},
}

# 策略 -> 依赖哪些数据源（回测补数缺口清单的依据）
POLICY_REQUIRES: dict[str, list[str]] = {
    "P_ADM_001": ["DS_REALNAME_01", "INT_USER"],
    "P_ADM_002": ["INT_USER", "INT_PROD_CFG"],
    "P_ADM_003": ["DS_OCR_02", "INT_USER"],
    "P_AF_001":  ["DS_AF_LIST_03"],
    "P_AF_002":  ["DS_DEVICE_04", "INT_APPLY_HIST"],
    "P_AF_003":  ["INT_APPLY_HIST"],
    "P_CR_001":  ["DS_BUREAU_05", "INT_PROD_CFG"],
    "P_CR_002":  ["DS_BUREAU_05"],
    "P_CR_003":  ["INT_LOAN_HIST"],
    "P_CR_004":  ["DS_BUREAU_05", "INT_LOAN_HIST", "INT_USER"],
    "P_LIM_001": ["INT_USER", "INT_LOAN_HIST", "DS_BUREAU_05", "INT_PROD_CFG", "DS_FLOW_06"],
    "P_LIM_002": ["INT_LOAN_HIST"],
}

# 环节 -> 该环节要调用哪些数据源（成本列与补数成本的依据）
STAGE_SOURCES: dict[str, list[str]] = {
    "S1_ADM": ["DS_REALNAME_01", "DS_OCR_02", "INT_USER", "INT_PROD_CFG"],
    "S2_AF":  ["DS_AF_LIST_03", "DS_DEVICE_04", "INT_APPLY_HIST"],
    "S3_CR":  ["DS_BUREAU_05", "INT_LOAN_HIST", "INT_PROD_CFG", "INT_USER"],
    "S4_LIM": ["DS_FLOW_06", "INT_LOAN_HIST", "INT_PROD_CFG", "INT_USER", "DS_BUREAU_05"],
}

# ---------------------------------------------------------------------------
# 4. VALUE_STATE 五态 —— 回测事实表的灵魂列（审批系统的 UNKNOWN 在这里拆细）
# ---------------------------------------------------------------------------
# 教学要点：为什么五态不能合成两态（有值/没值）？
#   MISSING_SOURCE（数据源挂了）→ 该补数、该找数据源负责人，跟客群无关；
#   BIZ_EMPTY（业务上合法的空，如"无贷记卡账户"）→ 是客群特征，有风险含义；
#   SENTINEL（-1/-999 之类哨兵）→ 供应商口径，必须按"查无记录"处理，
#       当成 0 就是"查无记录"和"零次查询"的混淆，回测结论直接错向；
#   NOT_EXECUTED（前面环节拒了、本环节没跑到）→ 漏斗结构造成，不是数据问题。
# 四种东西混成一个 NULL，报告就再也说不清"缺失率为什么高"。

VALUE_STATE = {
    "OK":            "有值（正常执行）",
    "MISSING_SOURCE": "数据源故障/未返回（该补数）",
    "BIZ_EMPTY":     "业务空值（合法的空，有风险含义）",
    "SENTINEL":      "哨兵值（-1/-999 等，按查无记录处理）",
    "NOT_EXECUTED":  "未执行（前面环节拒绝，本环节没跑到）",
}

# ---------------------------------------------------------------------------
# 5. SOURCE_LEVEL 数据来源等级（四标注）—— 每个坏账率数字必须带一个
# ---------------------------------------------------------------------------

SOURCE_LEVEL = {
    "MEASURED":     "实测（线上真实发生，有标签）",
    "EXPERIMENTAL": "实验（AB/回捞获得，有标签但流量受限）",
    "EXTRAPOLATED": "外推（模型/重放估计，仅区间可信）",
    "UNAVAILABLE":  "不可得（无标签，报告只能留白或给区间）",
}

# ---------------------------------------------------------------------------
# 6. 分析结论的方向（收紧/放宽/维持 —— 由诊断输出，不是输入）
# ---------------------------------------------------------------------------

DIRECTION = {
    "TIGHTEN": "收紧（当前通过人群里有高风险，加严阈值）",
    "RELAX":   "放宽（当前拒绝人群风险不高，白拒了）",
    "HOLD":    "维持（无显著信号 / 样本不足）",
}


def describe(code: str) -> str:
    """任何编码 -> 人话。报告叙述层统一走这里，业务代码里不出现中文字面量。"""
    if code in POLICY_NAMES:
        return f"{code} {POLICY_NAMES[code]}"
    if code in REJECT_CODES:
        return f"{code} {REJECT_CODES[code]['text']}"
    if code in SOURCES:
        return f"{code} {SOURCES[code].name}"
    if code in STAGES:
        return f"{code} {STAGES[code]['name']}"
    return code


def source_cost(code: str) -> float:
    """数据源单价。回测成本列 = 调用次数 × 单价，只允许经这个函数取价。"""
    return SOURCES[code].unit_cost


def mask_id(id_no: str) -> str:
    """身份证/手机号脱敏。回测报告虽然面向内部，但《个保法》要求最小可见。"""
    if not id_no:
        return ""
    if len(id_no) <= 6:
        return id_no[0] + "*" * (len(id_no) - 1)
    return f"{id_no[:4]}{'*' * (len(id_no) - 8)}{id_no[-4:]}"


# ---------------------------------------------------------------------------
# 7. 语义层错误码（V2.5）—— "不能用" 与 "算错了" 必须分开
# ---------------------------------------------------------------------------
# 为什么错误码必须是枚举，而不是抛异常 / 返回一句中文：
#   ① 评估器可以断言（"3 天窗口算 FPD7 必须返回 NOT_APPLICABLE"），
#      抛异常只能断言"崩了"，而崩了和答错了是两回事；
#   ② 可归因：NOT_REGISTERED 多 = 指标目录不够；PARAM_OUT_OF_DOMAIN 多 =
#      别名/产品认知不够。两类问题的修法完全不同；
#   ③ 可降级：守卫拒绝时，LLM 仍能拿到一句"我知道你想问什么，但这个口径
#      现在给不了 + 为什么"，比白屏好。
SEMANTIC_ERRORS = {
    "NOT_REGISTERED": "这个口径还没有定义（不在册）",
    "NOT_APPLICABLE": "这个口径在当前条件下不成立（如表现期未到）",
    "VINTAGE_BLOCKED": "到期样本不足，不能给比率；只能给样本量",
    "NEED_CLARIFY": "问法有歧义或缺必填参数，需要先确认",
    "PARAM_OUT_OF_DOMAIN": "参数不在允许取值内（产品/窗口/维度/箱数/label）",
    "NOT_VISIBLE": "本角色没有这个口径（措辞不得暗示'系统里有但不给你'）",
    "DENIED_DIMENSION": "这个口径不允许按该维度拆开看",
    "ROW_FILTERED": "已按行权限过滤，信封必须回显范围",
    "NO_JOIN_PATH": "两个口径之间没有合法的 join 路径，不能放在一起算",
}


def err(code: str, **detail) -> dict:
    """构造一个结构化错误 —— 语义层对外只有这一种失败形态。"""
    return {"ok": False, "code": code, "message": SEMANTIC_ERRORS.get(code, code),
            **detail}
