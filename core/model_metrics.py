# -*- coding: utf-8 -*-
"""模型级指标：第二本册子 + 唯一计算入口（V2.5 §8.2–8.5）。

════════════════════════════════════════════════════════════════════
为什么必须有这第二本册子（没有它会怎样）
════════════════════════════════════════════════════════════════════
风控人员最爱问的一类问题，恰好落在语义层不能做的地方：
    "各策略的 KS 是多少"、"PSI 有没有漂移"、"模型 AUC 现在多少"

KS / AUC / Gini / IV / PSI / LIFT / WOE 的本质是**对一列分数做分布统计**，
没有分子分母、也不可加（KS(A∪B) ≠ KS(A) + KS(B)）。把它们塞进
MetricDef 的结果是：numerator/denominator/grain 全为 None，而
score_col/label_col/bins 对通过率也全为 None —— 一半字段永远是空的。
**字段一旦并表，"字段存在"本身就变成能力声明**，模型看到 bins 就会
试着给通过率传 bins=10。

实测过的真实失败（本项目，改造前）：
    问"现金贷各策略的 KS 是多少" → 命中 STATS（触发词"多少"）
      → 返回各策略/各环节的**通过率表**。全文没有 KS，
        但每个数字都溯源通过 —— 看起来完全正常。
    问"模型的 AUC 现在多少" → 返回"申请通过率 12.3%"，
      并声明"未指定指标，默认按【申请通过率】口径"。
根因不是意图分类不准，而是：**"用户没说"和"用户说了但我没有"被合并成了
同一个分支**。所以分流必须在 resolve 之前，作为一次独立的词汇判定。

════════════════════════════════════════════════════════════════════
唯一计算入口：原语共享 ≠ 口径共享
════════════════════════════════════════════════════════════════════
真正会漂移的不是 ks_statistic，而是调它之前那 20 行：
五态怎么处理、label 用哪个、表现期要不要前移、ties 怎么算、切点怎么生成。
所以日常提问、监控 job、模型分析报告**三者都只准调 compute_model_metric**，
区别只有参数来自对话还是来自注册表。ci/check_imports.py 把这条变成构建失败。

════════════════════════════════════════════════════════════════════
分数从哪来（本地夹具）
════════════════════════════════════════════════════════════════════
真实系统里 score_col 指向数仓的模型分表。本夹具里由特征确定性生成
（哈希噪声，不消耗 V1 的 rng —— 否则 V1 三个教学故事会集体漂移）。
**约定：分数越高风险越高**。方向搞反会得到 1-AUC，而它看起来同样"正常"。
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from . import access, catalog, entities, knowledge, settings, stats
from .codes import err

# ★ 全系统唯一允许 import 统计原语的地方（ci/check_imports.py 门禁 1）
from .metrics import ks_statistic


# ---------------------------------------------------------------------------
# 一、第二本册子
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ModelMetricDef:
    metric_id: str
    name: str
    kind: str                     # scalar | bins | curve
    score_col: str                # 谁在被排序
    label_col: str                # 好坏定义：fpd7 | fpd30 | mob3
    ref_period: str               # required | optional | none
    min_sample: int               # 好坏各多少才算数
    requires_maturity: bool       # label 需表现期 → 复用到期口径
    comparable_dimensions: tuple[str, ...]
    version: str
    owner: str
    aliases: tuple[str, ...] = ()
    description: str = ""
    healthy_range: dict = field(default_factory=dict)


MODEL_CATALOG: dict[str, ModelMetricDef] = {}

# ★ min_sample 的两个值，各有各的用途（混用会出事故）：
#   DESIGN_MIN_SAMPLE —— 写进定义里的**设计要求**：好坏各 ≥50 才算数。
#   settings.MIN_SAMPLE —— **当前部署生效值**：夹具环境默认 5，否则 2000 单
#     的演示库（约 40 个坏样本）永远 VINTAGE_BLOCKED，整个分流演示不了。
#   接真实数仓时 `BTA_MIN_SAMPLE=50` 一行环境变量即可，代码不动。
#   谁生效由 settings 说，audit 里也记 settings —— 不允许"看起来是 50 其实是 5"。
DESIGN_MIN_SAMPLE = 50


def effective_min_sample(m: ModelMetricDef | None = None) -> int:
    """当前生效的最小样本量。metric 参数留给"个别指标需要更严"的未来。"""
    return max(1, int(settings.MIN_SAMPLE))


def _reg(m: ModelMetricDef) -> ModelMetricDef:
    if m.metric_id in MODEL_CATALOG:
        raise ValueError(f"模型级指标重复注册: {m.metric_id}")
    MODEL_CATALOG[m.metric_id] = m
    return m


_reg(ModelMetricDef(
    "ks", "KS 区分度", "scalar", "model_score_v1", "fpd30", "none",
    DESIGN_MIN_SAMPLE, True, ("product", "channel", "month", "day"), "1.1", "模型组",
    aliases=("KS", "ks", "区分度", "K-S"),
    description=("好坏样本累计分布的最大差距，衡量模型把坏人和好人分开的能力。"
                 "取值范围 0~1，越大越好；它是**全分布的累计最大值**，"
                 "不是每箱一个，也不参与任何加法分解。"),
    healthy_range={"low": 0.25, "direction": "higher_better"}))

_reg(ModelMetricDef(
    "auc", "AUC", "scalar", "model_score_v1", "fpd30", "none",
    DESIGN_MIN_SAMPLE, True, ("product", "channel", "month", "day"), "1.1", "模型组",
    aliases=("AUC", "auc", "曲线下面积"),
    description=("随机取一个坏人一个好人，模型给坏人的分更高的概率。"
                 "0.5 等于瞎猜；风控消费贷通常 0.65~0.75 算可用。"),
    healthy_range={"low": 0.62, "direction": "higher_better"}))

_reg(ModelMetricDef(
    "gini", "Gini", "scalar", "model_score_v1", "fpd30", "none",
    DESIGN_MIN_SAMPLE, True, ("product", "channel", "month", "day"), "1.1", "模型组",
    aliases=("GINI", "Gini", "gini", "基尼"),
    description="AUC 的线性变换（2·AUC−1），与 AUC 一一对应，不是独立证据。"))

_reg(ModelMetricDef(
    "iv", "IV 信息值", "bins", "model_score_v1", "fpd30", "none",
    DESIGN_MIN_SAMPLE, True, ("product", "channel", "month", "day"), "1.1", "模型组",
    aliases=("IV", "iv", "信息值"),
    description=("各箱 WOE 的加权和，衡量分数对好坏的区分贡献总量。"
                 "0.02 以下几乎无用，0.3 以上要警惕过拟合或泄漏。"),
    healthy_range={"low": 0.02, "high": 0.50, "direction": "band"}))

_reg(ModelMetricDef(
    "psi", "PSI 稳定性", "scalar", "model_score_v1", "fpd30", "required",
    DESIGN_MIN_SAMPLE, False, ("product", "month", "day"), "1.1", "模型组",
    aliases=("PSI", "psi", "稳定性", "漂移"),
    description=("当期分数分布与基准分布的差异。**没有基准期就算不出** —— "
                 "这不是「算得糙」，是根本没定义；不许拿当期跟自己比。"
                 "0.1 以下稳定，0.25 以上显著漂移。"),
    healthy_range={"high": 0.10, "direction": "lower_better"}))

_reg(ModelMetricDef(
    "lift", "LIFT 提升度", "bins", "model_score_v1", "fpd30", "none",
    DESIGN_MIN_SAMPLE, True, ("product", "channel", "month", "day"), "1.1", "模型组",
    aliases=("LIFT", "lift", "提升度"),
    description=("每箱坏账率相对整体坏账率的倍数。最高分箱的 lift 就是"
                 "「抓最坏的那批人，风险是平均的几倍」。")))

_reg(ModelMetricDef(
    "woe_table", "WOE 分箱表", "bins", "model_score_v1", "fpd30", "none",
    DESIGN_MIN_SAMPLE, True, ("product", "channel", "month", "day"), "1.1", "模型组",
    aliases=("WOE", "woe", "分箱", "单调性", "分箱单调"),
    description=("逐箱的 n / 坏账率 / WOE / IV 贡献 / LIFT / 累计 KS，"
                 "并给出**单调性判定**（坏账率是否随分数单调不减）与破格箱号。"
                 "单调性是结论性指标：它直接决定要不要调阈值。")))

# ---- 曲线类（S0.9 T-101）：曲线本身不进答案，只回摘要 + ref_id ----
# 为什么必须单独一类，而不是让 woe_table 多给一列：
#   曲线有几十上百个点，塞进回答有三个后果：
#   ① 模型会把点当成"多条事实"逐条复述，答案变成一张 csv；
#   ② 出口闸门要逐点溯源，登记簿爆炸；
#   ③ 人真正要看的是"最优点在哪、面积多大"，不是 100 行坐标。
_reg(ModelMetricDef(
    "ks_curve", "KS 曲线", "curve", "model_score_v1", "fpd30", "none",
    DESIGN_MIN_SAMPLE, True, ("product", "channel", "month", "day"), "1.1", "模型组",
    aliases=("KS曲线", "累计KS", "ks曲线"),
    description=("累计好坏分布差随分数变化的整条曲线。只回三个摘要量"
                 "（ks_max / at_score / auc_area），逐点数据写入 ref 文件。"
                 "要看形状请按 ref_id 取，不要把点塞进对话。"),
    healthy_range={"low": 0.25, "direction": "higher_better"}))

_reg(ModelMetricDef(
    "roc_curve", "ROC 曲线", "curve", "model_score_v1", "fpd30", "none",
    DESIGN_MIN_SAMPLE, True, ("product", "channel", "month", "day"), "1.1", "模型组",
    aliases=("ROC", "roc", "roc曲线"),
    description=("以假正率为横轴、真正率为纵轴的曲线。只回摘要"
                 "（auc_area 与最优点），逐点写入 ref 文件。"
                 "AUC 是这条曲线的面积，两者必须能互相印证。"),
    healthy_range={"low": 0.62, "direction": "higher_better"}))


LABELS = {"fpd7": 7, "fpd30": 30, "mob3": 90}
BINS_ALLOWED = (5, 10, 20)
DEFAULT_BINS = 5          # 夹具样本量小，默认 5 箱；用户可显式要 10/20
DIMENSIONS = ("overall", "product", "channel", "month", "day")


# ---------------------------------------------------------------------------
# 二、闸门 ⓿：词汇闸门（必须在意图规则之前）
# ---------------------------------------------------------------------------
# 中文词 + ASCII 词分开匹配：ASCII 用"两侧不能是字母"的边界，
# 避免 "AUC" 命中 "SAUCE" 这类误伤；中文直接子串。
_MODEL_ZH = ("区分度", "分箱", "单调性", "评分分布", "分数段迁移", "模型分",
             "模型评分", "KS值", "PSI值")
_ASCII_MAP = {"KS": "ks", "AUC": "auc", "GINI": "gini", "IV": "iv",
              "PSI": "psi", "LIFT": "lift", "WOE": "woe_table"}

# T-106：介于"问模型"和"问率值"之间的第三类问法 ——
# 说的是模型的事，但没点名任何指标（"模型最近表现怎么样"）。
# 这类话**必须反问**，因为两个分支的答案形状完全不同：
#   选模型 → KS/AUC/PSI + 分箱表；选策略 → 通过率 + 漏斗表。
# 默认任何一个都是替用户做了决定，而且错得很自然（他会以为你要的就是这个）。
_AMBIGUOUS_CUES = ("表现", "效果", "怎么样", "好不好", "区分能力", "区分效果",
                   "稳定性", "质量", "准不准", "靠谱")
_AMBIGUOUS_SUBJECT = ("模型", "评分", "分数")
_AGG_CUES = ("通过率", "拒绝率", "放款率", "逾期", "坏账", "vintage",
             "漏斗", "环节", "策略的通过")


def detect(text: str) -> str | None:
    """命中模型级词表 → 返回对应 metric_id；否则 None。

    这是"能力边界"判定，不是"意图分类"：意图分错只是答得不好；
    边界判错会让模型**用另一类指标的回答冒充**（有数、可溯源、还带免责声明）。
    """
    t = text or ""
    up = t.upper()
    flat = up.replace(" ", "").replace("\u3000", "")
    # 曲线类要先判：否则 "KS曲线" 会先命中 ASCII "KS" 被当成标量 KS，
    # 于是用户要的是曲线，拿到的是一个数 —— 而那个数看起来完全正确。
    if "ROC曲线" in flat or "ROC" in flat:
        i = flat.find("ROC")
        before = flat[i - 1] if i > 0 else " "
        after = flat[i + 3] if i + 3 < len(flat) else " "
        if not (before.isascii() and before.isalpha()) and \
                not (after.isascii() and after.isalpha()):
            return "roc_curve"
    if "KS曲线" in flat or "累计KS" in flat:
        return "ks_curve"
    for w, mid in _ASCII_MAP.items():
        i = up.find(w)
        while i >= 0:
            before = up[i - 1] if i > 0 else " "
            after = up[i + len(w)] if i + len(w) < len(up) else " "
            # ★中文也是 isalpha()=True：边界判定必须只对 ASCII 字母生效，
            #   否则"的KS是"里 KS 两侧都是中文、会被误判成"单词内部"而漏掉。
            if not (before.isascii() and before.isalpha()) and \
                    not (after.isascii() and after.isalpha()):
                return mid
            i = up.find(w, i + 1)
    # "模型分析"里的"模型分"不是模型分指标 —— 这条例外是实测踩出来的
    if "模型分" in t and "模型分析" not in t:
        return "woe_table"
    for w in _MODEL_ZH:
        if w == "模型分" and "模型分析" in t:
            continue          # "模型分析报告"不是在问模型分指标
        if w in t:
            return ("ks" if w in ("区分度", "KS值")
                    else "psi" if w == "PSI值" else "woe_table")
    return None


def detect_state(text: str) -> tuple[str, str | None]:
    """闸门 ⓿ 的四态判定（T-106）：`(state, metric_id)`。

        model      点名了模型级指标      → 锁模型分支，走 compute_model_metric
        ambiguous  说的是模型的事但没点名 → **反问**，两个分支都不选
        aggregate  点名了率值类词         → 锁可聚合分支
        none       都没点名               → 交给意图规则

    ★为什么不把 ambiguous 并进 none：
      none 会走到"默认按申请通过率"，于是"模型最近表现怎么样"被答成
      12.3% 通过率 —— 数字对、可溯源、还带免责声明，但答的是另一个问题。
      这正是 V2.5 整章在治的病：**别替用户选口径**。
    """
    mid = detect(text)
    if mid:
        return "model", mid
    t = text or ""
    if any(s in t for s in _AMBIGUOUS_SUBJECT) and any(c in t for c in _AMBIGUOUS_CUES) \
            and not any(a in t.lower() for a in _AGG_CUES):
        return "ambiguous", None
    if any(a in t.lower() for a in _AGG_CUES):
        return "aggregate", None
    return "none", None


# ---------------------------------------------------------------------------
# 三、分数夹具（确定性，不消耗任何全局随机状态）
# ---------------------------------------------------------------------------
def _noise(apply_id: str) -> float:
    h = hashlib.sha256(("score:" + apply_id).encode()).hexdigest()[:8]
    return int(h, 16) / 0xFFFFFFFF


def score_of(row: dict) -> float:
    """模型分（0~1，越高越坏）。只用特征，不偷看标签。

    教学要点：如果这里用了 bad_flag，KS 会接近 1（完美模型），
    所有评估器都会"通过"，但整个演示失去意义 —— 而这正是外部评审
    最常怀疑的一件事（"你的模型不可能这么准"）。
    """
    q = row.get("query_3m")
    q = 3.0 if q is None or q < 0 else min(float(q), 12.0)
    dev = min(float(row.get("device_linked") or 0), 8.0)
    dti = float(row.get("dti") or 0)
    a24 = float(row.get("apply_24h") or 0)
    z = (0.42 * q + 0.30 * dev + 3.2 * dti + 0.22 * a24
         + 1.6 * _noise(row["apply_id"]) - 3.1)
    return round(1 / (1 + math.exp(-z)), 6)


MODEL_SCORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS model_score (
    apply_id  TEXT NOT NULL,
    model_id  TEXT NOT NULL,
    score     REAL NOT NULL,
    scored_at TEXT,
    PRIMARY KEY (apply_id, model_id)
);
"""

MODEL_ID = "model_score_v1"
POLICY_SCORE_ID = "policy_score_v1"

# T-102：两个分源。它们回答的是两个不同的问题：
#   model  —— 模型分（机器学习打分）区分度如何
#   policy —— 策略分（规则命中比例）区分度如何
# 混着报会出现"模型 KS 0.21，策略 KS 0.21"这种看似一致、实为两份数据的巧合，
# 所以 score_source 必须显式，且进签名（物化命中不能跨分源）。
SCORE_SOURCES = ("model", "policy")
SCORE_ID = {"model": MODEL_ID, "policy": POLICY_SCORE_ID}


def ensure_model_score(db, rows: list[dict], as_of: str,
                       run_id: str | None = None) -> int:
    """把两种分数写进 model_score（幂等）。返回写入行数。

    · 模型分：由特征确定性生成（score_of），不消耗 V1 的 rng。
    · 策略分：由**策略事实长表**算出来 —— 该申请被执行到的策略里，
      判 FAIL 的比例。它是真的"规则怎么看这个人"，不是模型分的变形。
      没有 run_id 就不写策略分（宁可缺，也不要拿模型分冒充）。
    """
    db.conn.executescript(MODEL_SCORE_SCHEMA)
    n = 0
    for r in rows:
        db.conn.execute(
            "INSERT OR REPLACE INTO model_score VALUES (?,?,?,?)",
            (r["apply_id"], MODEL_ID, score_of(r), as_of))
        n += 1
    if run_id:
        for ap, f, tot in db.conn.execute(
                "SELECT apply_id,"
                " SUM(CASE WHEN result='FAIL' THEN 1 ELSE 0 END) AS f,"
                " COUNT(*) AS n FROM policy_eval_fact WHERE run_id=?"
                " GROUP BY apply_id", (run_id,)).fetchall():
            if not tot:
                continue
            db.conn.execute(
                "INSERT OR REPLACE INTO model_score VALUES (?,?,?,?)",
                (ap, POLICY_SCORE_ID, round(float(f) / float(tot), 6), as_of))
            n += 1
    db.conn.commit()
    return n


# ---------------------------------------------------------------------------
# 四、参数归一化（长尾提问与定时任务共用同一组枚举）
# ---------------------------------------------------------------------------
REF_PERIODS = {
    "train_2025Q4": {"from": "2026-01-01", "to": "2026-03-31"},
    "2026Q1": {"from": "2026-01-01", "to": "2026-03-31"},
    "2026Q2": {"from": "2026-04-01", "to": "2026-06-30"},
}


def _window_of(v, as_of: str):
    if v is None:
        return None
    if isinstance(v, catalog.Window):
        return v
    if isinstance(v, dict):
        if v.get("from") and v.get("to"):
            return catalog.Window(v.get("label") or "自定义", v["from"], v["to"])
        return catalog.parse_window(v.get("label") or "", as_of)
    return catalog.parse_window(str(v), as_of)


def normalize_params(metric_id: str, params: dict,
                     as_of: str = catalog.AS_OF) -> dict:
    """把各种写法归一成一份**可比较**的参数（物化命中靠它做逐项相等）。"""
    p = dict(params or {})
    win = _window_of(p.get("window"), as_of)
    m = MODEL_CATALOG.get(metric_id)
    return {
        "metric_id": metric_id,
        "window": {"from": win.date_from, "to": win.date_to} if win else None,
        "product": p.get("product") or "*",
        "dimension": p.get("dimension") or "overall",
        "bins": int(p.get("bins") or DEFAULT_BINS),
        "label": p.get("label") or (m.label_col if m else "fpd30"),
        "ref_period": p.get("ref_period"),
        # 分源必须进签名：同名指标、不同分源的两份结果不能互相命中
        "score_source": p.get("score_source") or "model",
    }


def signature(metric_id: str, params: dict, as_of: str = catalog.AS_OF) -> str:
    n = normalize_params(metric_id, params, as_of)
    return hashlib.sha256(
        json.dumps(n, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:12]


# ---------------------------------------------------------------------------
# 五、唯一计算入口
# ---------------------------------------------------------------------------
def compute_model_metric(metric_id: str, params: dict | None = None,
                         role: str | None = None,
                         as_of: str = catalog.AS_OF, db=None) -> dict:
    """guard → assemble → compute → register。四步之外没有第五条路。

    返回一份**带来源标记**的结果；失败一律返回结构化错误码（不抛异常）。
    """
    params = dict(params or {})
    m = MODEL_CATALOG.get(metric_id)
    if m is None:
        # ★T-608 扩展：模型册同样先按角色过滤 —— 否则受限角色能从错误信息
        # 反查出整套模型指标（P-03）。过滤与取名规则只实现一次，在 catalog。
        examples = catalog.examples_for_role(role, MODEL_CATALOG)
        detail = {"metric_id": metric_id, "registered": examples}
        if examples:
            # 一句提示也带目录：只有真的能看到模型指标的角色才配拿到它
            detail["hint"] = "KS/AUC/Gini/IV/PSI/LIFT/WOE 之外的模型指标尚未定义"
        return err("NOT_REGISTERED", **detail)

    # ---- ① guard ----
    pol = access.policy(role)
    chk = pol.check_metric(metric_id)
    if not chk.get("ok"):
        return chk
    dim = params.get("dimension") or "overall"
    if dim not in DIMENSIONS:
        return err("PARAM_OUT_OF_DOMAIN", param="dimension", value=dim,
                   allowed=list(DIMENSIONS))
    chk = pol.check_dimension(metric_id, dim)
    if not chk.get("ok"):
        return chk
    if dim != "overall" and dim not in m.comparable_dimensions:
        return err("PARAM_OUT_OF_DOMAIN", param="dimension", value=dim,
                   allowed=["overall", *m.comparable_dimensions])
    bins = int(params.get("bins") or DEFAULT_BINS)
    if bins not in BINS_ALLOWED:
        # 只收箱数，不收切点：切点是口径，允许传断点就能把单调性调成任意形状
        return err("PARAM_OUT_OF_DOMAIN", param="bins", value=params.get("bins"),
                   allowed=list(BINS_ALLOWED))
    label = params.get("label") or m.label_col
    if label not in LABELS:
        return err("PARAM_OUT_OF_DOMAIN", param="label", value=label,
                   allowed=sorted(LABELS))
    product = params.get("product")
    if not product:
        return err("NEED_CLARIFY", slot="product", metric_id=metric_id,
                   options=sorted(knowledge.PRODUCTS))
    if product not in knowledge.PRODUCTS:
        return err("PARAM_OUT_OF_DOMAIN", param="product", value=product,
                   allowed=sorted(knowledge.PRODUCTS))
    if m.ref_period == "required" and not params.get("ref_period"):
        # PSI 的命门：没有基准分布不是"算得糙"，是根本算不出。
        # 也不许拿"当期跟当期比"（那会永远得到 0，看起来最稳定）
        return err("NOT_APPLICABLE", metric_id=metric_id,
                   reason="PSI 需要基准分布；缺少 ref_period，不能拿当期跟当期比",
                   allowed_ref_periods=sorted(REF_PERIODS))

    win = _window_of(params.get("window"), as_of)
    if win is None:
        return err("NEED_CLARIFY", slot="window", metric_id=metric_id,
                   options=["近一周", "近一个月", "近三个月"])

    # T-102：分源。默认 model，但**必须显式出现在返回里** ——
    # "这 0.26 是模型分的还是策略分的"是报告里第一个会被追问的问题。
    score_source = params.get("score_source") or "model"
    if score_source not in SCORE_SOURCES:
        return err("PARAM_OUT_OF_DOMAIN", param="score_source",
                   value=params.get("score_source"),
                   allowed=list(SCORE_SOURCES))

    # ---- ② assemble（表现期复用同一套到期口径）----
    lag = LABELS[label]
    corrected = False
    if m.requires_maturity and (date.fromisoformat(win.date_to)
                                + timedelta(days=lag)).isoformat() > as_of:
        win = win.shift(-lag)
        corrected = True
        if win.days <= 0:
            return err("NOT_APPLICABLE", metric_id=metric_id,
                       reason=f"窗口前移 {lag} 天后已无数据")

    db = db or params.get("_db")
    if db is None:
        return err("NOT_REGISTERED", metric_id=metric_id,
                   reason="内部错误：compute_model_metric 缺少数据连接")
    triples, meta = _assemble(db, win, product, label, dim, as_of, score_source)
    if not triples:
        return err("NOT_APPLICABLE", metric_id=metric_id,
                   reason=f"{win.label} 内没有可用的分数与标签"
                          f"（放款 {meta['loans']} 笔，已到期 {meta['matured']} 笔）")

    # ---- ③ compute ----
    groups: list[dict] = []
    if dim == "overall":
        groups = [{"group": "overall",
                   "pairs": [(s, y) for s, y, _ in triples]}]
    else:
        buckets: dict[str, list] = {}
        for s, y, g in triples:
            buckets.setdefault(g or "未知", []).append((s, y))
        groups = [{"group": k, "pairs": v} for k, v in sorted(buckets.items())]

    for g in groups:
        g.update(_compute_one(metric_id, g["pairs"], bins))
        g["n"] = len(g["pairs"])
        g["bad_n"] = sum(1 for _, y in g["pairs"] if y == 1)
        g["good_n"] = g["n"] - g["bad_n"]
        g.pop("pairs", None)

    # ---- 曲线类：点外卸，只留摘要 + ref_id（T-101）----
    curve_ref = None
    if m.kind == "curve":
        curve_ref = _write_curve_ref(metric_id, params, win, label, product,
                                     score_source, groups)
        for g in groups:
            g.pop("curve", None)          # 点已经落文件，不许再随结果回流

    total_n = sum(g["n"] for g in groups)
    total_bad = sum(g["bad_n"] for g in groups)
    out: dict = {"ok": True, "metric_id": metric_id, "name": m.name,
                 "kind": m.kind, "label": label, "bins_n": bins,
                 "n": total_n, "bad_n": total_bad, "good_n": total_n - total_bad,
                 "window": win.as_dict(), "maturity_corrected": corrected,
                 "product": product, "dimension": dim,
                 "score_source": score_source, "score_col": SCORE_ID[score_source],
                 "version": m.version, "param_version": m.version,
                 "source": "MEASURED", "groups": groups if dim != "overall" else [],
                 "ref_period": None,
                 "notes": ["模型级指标不参与 LMDI 分解，也不能与率值混算"],
                 "assemble": meta}

    # 小样本闸门：阈值取**当前生效值**（settings），不是定义里的设计值
    thr = effective_min_sample(m)
    if min(total_bad, total_n - total_bad) < thr:
        return err("VINTAGE_BLOCKED", metric_id=metric_id, n=total_n,
                   bad_n=total_bad, good_n=total_n - total_bad,
                   threshold=thr, design_threshold=m.min_sample,
                   reason=(f"好坏样本不足（坏 {total_bad} / 好 {total_n - total_bad}，"
                           f"各需 ≥{thr}）：只给样本量，不给指标值"))
    if dim == "overall":
        out.update({k: v for k, v in groups[0].items()
                    if k not in ("group", "n", "bad_n", "good_n")})
        out["groups"] = []
    else:
        out["value"] = None
        if m.kind == "curve":
            out["summary"] = {g["group"]: g.get("summary") for g in groups}

    if curve_ref:
        out["ref_id"] = curve_ref["ref_id"]
        out["n_points"] = curve_ref["n_points"]
        out["n_points_out"] = curve_ref["n_points_out"]
        out["summary"] = (curve_ref["summary"] if dim == "overall"
                          else out.get("summary"))

    # 方向自检：AUC<0.5 说明坏样本分数反而更低。KS 对此**无感**
    # （它取的是两条 CDF 的最大距离，方向无关），所以必须单独判 ——
    # 否则会出现"KS 0.32 看着不错、AUC 0.41"这种自相矛盾的报表，
    # 而两边都"算对了"。
    if metric_id in ("auc", "gini") and out.get("value") is not None:
        thr = 0.5 if metric_id == "auc" else 0.0
        if out["value"] < thr:
            out["direction_warning"] = True
            out["notes"].append(
                f"{m.name} 低于随机水平（{out['value']:.4f} < {thr}）："
                f"坏样本的分数反而更低，说明排序方向反了。"
                f"要么分数已做过翻转没同步，要么这个分数不该直接用于决策 —— "
                f"先别调阈值。")

    if metric_id == "psi":
        ref = params.get("ref_period")
        ref_win = REF_PERIODS.get(ref) if isinstance(ref, str) else ref
        if not ref_win:
            return err("PARAM_OUT_OF_DOMAIN", param="ref_period", value=ref,
                       allowed=sorted(REF_PERIODS))
        out["ref_period"] = ref_win
        edges = (out.get("bins") or {}).get("edges") or []
        cur = [s for s, _, _ in triples]
        cur_share = stats.shares(cur, edges) if edges else []
        base_share, base_src, job_id = _base_dist(db, product, ref_win, edges,
                                                  score_source)
        if not base_share:
            return err("NOT_APPLICABLE", metric_id=metric_id,
                       reason="基准期没有可用分数，无法算 PSI")
        val = stats.psi(base_share, cur_share)
        out["value"] = None if val is None else round(val, 4)
        out["ci95"] = None
        out["base_dist"] = {"shares": [round(x, 6) for x in base_share],
                            "source": base_src, "job_id": job_id}
        ns = [r["n"] for r in (out.get("bins") or {}).get("rows") or []]
        out["bins"] = {"edges": [round(e, 6) for e in edges],
                       "rows": [{"bin": i + 1, "cur": round(c, 4),
                                 "base": round(b, 4),
                                 "n": ns[i] if i < len(ns) else None}
                                for i, (c, b) in enumerate(zip(cur_share, base_share))]}

    out["envelope_extra"] = _envelope_extra(m, out)
    out["signature"] = signature(metric_id, params, as_of)
    return out


def _compute_one(metric_id: str, pairs: list[tuple[float, int]], bins: int) -> dict:
    """单个分组的统计量。**原语只在这里被调用** —— 一条路，没有分支副本。"""
    scores = [s for s, _ in pairs]
    labels = [y for _, y in pairs]
    good = [s for s, y in pairs if y == 0]
    bad = [s for s, y in pairs if y == 1]
    if not good or not bad:
        return {"value": None, "ci95": None, "bins": None,
                "note": "单一类别（全好或全坏），指标无定义"}

    # T-101：曲线类。这里**只**返回摘要与 curve 载荷；
    # 落文件与脱钩（不留点）由 compute_model_metric 统一做 —— 原语不碰 IO。
    if metric_id in ("ks_curve", "roc_curve"):
        c = stats.curve_points(good, bad, settings.CURVE_MAX_POINTS)
        # n_points 是**真实不同分值个数**，n_points_out 是下采样后落文件的点数。
        # 两个数都要给：只说"给了曲线"而不说它被抽稀过，画图的人会以为
        # 看到的是全量 —— 折线的拐点会莫名消失。
        return {"value": c["ks_max"] if metric_id == "ks_curve" else c["auc_area"],
                "ci95": None, "bins": None, "curve": c,
                "n_points": c["n_points"], "n_points_out": len(c["points"]),
                "summary": {"ks_max": c["ks_max"], "at_score": c["at_score"],
                            "auc_area": c["auc_area"]}}

    if metric_id == "ks":
        r = ks_statistic(good, bad)
        ci = stats.bootstrap_ci(
            lambda ss, yy: ks_statistic([s for s, y in zip(ss, yy) if y == 0],
                                        [s for s, y in zip(ss, yy) if y == 1]).get("ks"),
            scores, labels)
        tbl = stats.bin_table(scores, labels, bins)
        return {"value": r["ks"], "ks_argmax": r.get("argmax"), "ci95": ci,
                "bins": tbl, "ks_max": r["ks"]}

    if metric_id in ("auc", "gini"):
        a = stats.auc(good, bad)
        ci = stats.bootstrap_ci(
            lambda ss, yy: stats.auc([s for s, y in zip(ss, yy) if y == 0],
                                     [s for s, y in zip(ss, yy) if y == 1]),
            scores, labels)
        return {"value": a if metric_id == "auc" else stats.gini_from_auc(a),
                "ci95": ci, "bins": None}

    tbl = stats.bin_table(scores, labels, bins)
    if metric_id == "psi":
        # PSI 的值要用基准分布才能算，这里只负责给出"当期分布用的切点/分箱"
        return {"value": None, "ci95": None, "bins": tbl}
    if metric_id == "iv":
        return {"value": tbl["iv_total"], "ci95": None, "bins": tbl,
                "monotonic": tbl["monotonic"], "iv_total": tbl["iv_total"]}
    if metric_id == "lift":
        top = tbl["rows"][-1] if tbl["rows"] else {}
        return {"value": top.get("lift"), "ci95": None, "bins": tbl,
                "monotonic": tbl["monotonic"],
                "note": "value = 最高分箱的 LIFT（不是全表平均）"}
    # woe_table
    return {"value": tbl["iv_total"], "ci95": None, "bins": tbl,
            "monotonic": tbl["monotonic"], "iv_total": tbl["iv_total"],
            "note": "WOE 表：value 记 IV 总量，结论看 monotonic 与逐箱 woe"}


def _envelope_extra(m: ModelMetricDef, out: dict) -> str:
    """模型级指标的信封第 5 行：分箱与参照期。

    只写"10 箱"等于没写 —— 等频 10 箱与等宽 10 箱会给出不同的箱坏账率排序，
    也会给出不同的单调性结论。所以切点必须出现在回答里。
    """
    tbl = out.get("bins") or {}
    edges = tbl.get("edges") or []
    if m.kind == "curve":
        # 曲线类：说清"点去哪了"。不写这句，读者会以为"没给曲线 = 没算曲线"。
        # 抽稀比例也要写：只说外卸不说抽稀，画图的人会把折线拐点当成真实拐点。
        n_raw, n_out = out.get("n_points"), out.get("n_points_out")
        thin = (f"（{n_raw} 个不同分值，抽稀为 {n_out} 点存盘）"
                if n_raw and n_out and n_raw != n_out else "")
        bin_txt = (f"曲线类：逐点数据{thin}已外卸到 {out.get('ref_id')}，"
                   f"只回摘要 ks_max / at_score / auc_area")
    elif edges:
        bin_txt = (f"等频 {out['bins_n']} 箱，切点 "
                   f"[{', '.join(f'{e:.2f}' for e in edges)}]")
    else:
        # AUC/Gini 是标量指标，JSON 里仍会带一个 bins_n 默认值。
        # 这时若写"等频 5 箱，切点 不适用"，读者会以为分了箱却没给切点 ——
        # 两句都是真的，合起来是假的。宁可只说"不分箱"。
        bin_txt = "不分箱（标量指标，全分布一个数）"
    ref = out.get("ref_period")
    ref_txt = (f"{ref['from']} ~ {ref['to']}" if ref
               else "不适用（单分布内部比较）")
    return f"{bin_txt}；参照期 {ref_txt}；label={out['label']}"


def _write_curve_ref(metric_id: str, params: dict, win, label: str,
                     product: str, score_source: str, groups: list[dict]) -> dict:
    """把逐点曲线写进 artifacts/ref/ref_YYYYMMDD_xxxx.json，返回 ref_id 与摘要。

    ★为什么落文件而不是"截断前 20 个点"：
      截断会让人以为曲线到头了，而画图的人拿不到后半段 —— 错得没有痕迹。
      落文件 + 给 ref_id 是"数据在那儿，只是不进对话"的诚实做法。

    ★ref_id 里带签名前 4 位：同一个问题重复问会命中同一个文件（幂等），
      参数不同则必然不同 —— 靠它就能判断"这条曲线是不是那次那条"。
    """
    sig = signature(metric_id, params, catalog.AS_OF)
    ref_id = f"ref_{date.today():%Y%m%d}_{sig[:4]}"
    payload = {
        "ref_id": ref_id, "metric_id": metric_id,
        "params": normalize_params(metric_id, params, catalog.AS_OF),
        "window": win.as_dict(), "label": label, "product": product,
        "score_source": score_source, "kind": "curve",
        "created_on": date.today().isoformat(),
        "groups": {g["group"]: (g.get("curve") or {}) for g in groups},
    }
    d = settings.REF_DIR
    # ★注意：沙箱里对已存在目录 mkdir(exist_ok=True) 会抛 EEXIST，
    #   所以先判 exists 再建。这不是洁癖，是本项目实测过的坑。
    if not d.exists():
        d.mkdir(parents=True, exist_ok=True)
    (d / f"{ref_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    summary = {g["group"]: g.get("summary") for g in groups}
    n_points = sum(int(g.get("n_points") or 0) for g in groups)
    n_out = sum(int(g.get("n_points_out") or 0) for g in groups)
    return {"ref_id": ref_id, "n_points": n_points, "n_points_out": n_out,
            "summary": summary.get("overall") or summary}


def _assemble(db, win, product: str, label: str, dim: str, as_of: str,
              score_source: str = "model"):
    """组装 (score, label, group)。与 FPD 指标共用同一套"已到期借据"集合。

    这是最容易分叉的地方：如果这里自己写一遍"哪些借据算到期"，
    模型级指标的样本集合就会和 FPD7/FPD30 悄悄不一致 ——
    于是"报告里 FPD30 分母 830"和"KS 的样本 812"永远对不上，
    而两边都"算对了"。

    score_source 只改"读哪一列分"，**不动样本集合** —— 两个分源必须能
    在同一批借据上直接比（否则 KS 的差异里混着样本差异，无法归因）。
    """
    apps, _ = entities.select_applicants(db, win, product)
    scores, _ = entities.select_scores(db, apps, SCORE_ID[score_source])
    by_id = {r["apply_id"]: r["score"] for r in scores}
    loans, _ = entities.select_loans(db, apps)
    lag = LABELS[label]
    cutoff = (date.fromisoformat(as_of) - timedelta(days=lag)).isoformat()
    app_by_id = {a["apply_id"]: a for a in apps}
    triples = []
    matured = 0
    for l in loans:
        if str(l["loan_date"])[:10] > cutoff:
            continue                       # 未到期：不进样本（不是当好人）
        matured += 1
        y = l.get(label)
        if y is None:
            continue                       # 无标签：不进样本（不是当 0）
        s = by_id.get(l["apply_id"])
        if s is None:
            continue
        a = app_by_id.get(l["apply_id"])
        g = None
        if a is not None:
            g = {"product": a["product_code"], "channel": a["channel"],
                 "month": a["apply_month"],
                 "day": str(a["created_at"])[:10]}.get(dim)
        triples.append((float(s), int(y), g))
    return triples, {"apps": len(apps), "scored": len(by_id),
                     "loans": len(loans), "matured": matured,
                     "score_source": score_source,
                     "score_col": SCORE_ID[score_source]}



def _base_dist(db, product: str, ref_win: dict, edges: list[float],
               score_source: str = "model"):
    """基准分布：优先读物化表（回流），没有才回算基准期。

    为什么优先读物化表：训练期的分箱占比不该在实时路径里重算 ——
    那是另一条数据链路，口径迟早分叉。**物化表反过来成为实时计算的输入**，
    "计算逻辑在同一处"才从口号变成可验证的事实。

    ★物化表里只存**模型分**的基准分布，所以 policy 分源必须自己回算：
      否则"策略分的 PSI"会拿模型分的基准去比，得到一个既非 A 也非 B 的数，
      而它看起来完全正常（PSI 0.08，稳定）。
    """
    if score_source == "model":
        try:
            row = db.conn.execute(
                "SELECT base_dist, job_id FROM model_health_daily "
                "WHERE product=? AND base_dist IS NOT NULL "
                "ORDER BY as_of DESC LIMIT 1", (product,)).fetchone()
            if row and row["base_dist"]:
                d = json.loads(row["base_dist"])
                if isinstance(d, dict) and d.get("shares"):
                    return d["shares"], "MATERIALIZED", row["job_id"]
        except Exception:                  # noqa: BLE001  表还没建
            pass
    if not edges:
        return None, "NONE", None
    w = catalog.Window("基准期", ref_win["from"], ref_win["to"])
    apps, _ = entities.select_applicants(db, w, product)
    scores, _ = entities.select_scores(db, apps, SCORE_ID[score_source])
    if not scores:
        return None, "NONE", None
    return (stats.shares([r["score"] for r in scores], edges), "MEASURED", None)


def base_shares(db, product: str, ref_win: dict, edges: list[float]):
    """基准期的分箱占比（物化 job 用它把 base_dist 落表）。

    单独暴露而不是让 scheduler 自己算：基准分布的口径也必须唯一实现，
    否则"定时任务写的 base_dist"和"实时算的 base_dist"会是两个东西。
    """
    if not edges:
        return None, "NONE", None
    w = catalog.Window("基准期", ref_win["from"], ref_win["to"])
    apps, _ = entities.select_applicants(db, w, product)
    scores, _ = entities.select_scores(db, apps, MODEL_ID)
    if not scores:
        return None, "NONE", None
    return stats.shares([r["score"] for r in scores], edges), "MEASURED", None


# ---------------------------------------------------------------------------
# 七、定义指纹与启动自检（T-103）
# ---------------------------------------------------------------------------
# 第一本册子有 definition_hashes.json，第二本一直没有 —— 这个不对称本身就是隐患：
# 改一个 label_col（fpd30 → fpd7），所有历史 KS 立刻不可比，
# 而 id、版本、报告标题全都没变，没有任何一处会报错。
_HASH_FILE = Path(__file__).with_name("model_definition_hashes.json")


def definition_hash(metric_id: str) -> str:
    """模型级指标"含义"的指纹：分数列 / 标签列 / 参照期 / 表现期 / 维度 / 版本。"""
    m = MODEL_CATALOG[metric_id]
    body = json.dumps({"id": m.metric_id, "kind": m.kind,
                       "score_col": m.score_col, "label_col": m.label_col,
                       "ref_period": m.ref_period,
                       "requires_maturity": m.requires_maturity,
                       "dims": list(m.comparable_dimensions),
                       "version": m.version},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def verify_definitions(strict: bool = True) -> dict:
    """启动自检：同 id 同版本但定义变了 → 报错（与第一本册子同款纪律）。

    owner 不进指纹：换负责人不该让历史指标失效。
    """
    current = {mid: {"version": MODEL_CATALOG[mid].version,
                     "owner": MODEL_CATALOG[mid].owner,
                     "hash": definition_hash(mid)} for mid in MODEL_CATALOG}
    if not _HASH_FILE.exists():
        _HASH_FILE.write_text(json.dumps(current, indent=2, sort_keys=True),
                              encoding="utf-8")
        return {"ok": True, "bootstrapped": True, "changed": [], "silent": []}
    base = json.loads(_HASH_FILE.read_text(encoding="utf-8"))
    silent, changed = [], []
    for mid, cur in current.items():
        old = base.get(mid)
        if old is None:
            changed.append({"metric_id": mid, "why": "新增指标"})
            continue
        if old["hash"] == cur["hash"]:
            continue
        if old["version"] != cur["version"]:
            changed.append({"metric_id": mid, "why": "版本已 bump",
                            "from": old["version"], "to": cur["version"]})
        else:
            silent.append({"metric_id": mid,
                           "why": "同 id 同版本，但定义变了（禁止）"})
    out = {"ok": not silent, "bootstrapped": False, "changed": changed,
           "silent": silent}
    if silent and strict:
        raise ValueError(f"模型级定义哈希校验失败（同 id 静默改义）: {silent}")
    return out
