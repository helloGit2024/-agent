# -*- coding: utf-8 -*-
"""Semantic View 语义层 —— V2 的第 0 号工程。

════════════════════════════════════════════════════════════════════
这个模块解决什么问题？（没有它会发生什么）
════════════════════════════════════════════════════════════════════
V1 时代人和引擎"对话"靠代码，口径藏在函数名里，不会错。
V2 让人用自然语言提问之后，"通过率"这个词立刻变成歧义源：
    申请通过率？授信通过率？提款通过率？环节内通过率？
    分母是申请数、进入数、还是放款数？
没有语义层，这些歧义会散落到每个节点里各写一遍，结果是
"同一份数据，报告里的通过率和问答里的通过率不是一个数" —— 这是
回测 Agent 最致命的失败模式（比算错更可怕：错得不一致，无法归因）。

语义层把指标变成**一等公民**，一个指标条目必须注册七个字段：
    ① 分子/分母的精确定义（写成可执行函数，不是文档）
    ② 时间归属语义（申请日 / 到期日 / MOB）
    ③ 表现期要求 lag_days（FPD30 必须放款满 30 天才可算）
    ④ 可下钻维度（声明了才允许 Agent 按维度切）
    ⑤ 别名与歧义表（"通过率"命中谁、什么时候要反问）
    ⑥ 版本 + 负责人（口径改版要能追溯，像 Zep 的"失效不删除"）
    ⑦ 口径单测（selftest_v2.py 里钉死期望值 —— 写了不算数，测过才算数）

════════════════════════════════════════════════════════════════════
口径封闭是怎么实现的（三层闸门）
════════════════════════════════════════════════════════════════════
    自然语言 → resolve() 只能出已注册的 metric_id（未注册 = 直接拒）
    metric_id → compute() 只能按注册的公式算（公式即代码，无双份口径）
    算出来的值 → 登记簿（AnswerBook），回答只能引用登记簿（可溯源校验）

════════════════════════════════════════════════════════════════════
两个真实坑（本模块的注释里都标了 ★坑）
════════════════════════════════════════════════════════════════════
  ★坑 1：FPD30 的分母不是"通过人数"，是"放款且已到期的借据数"。
         通过 ≠ 放款，放款 ≠ 到期。分母错一格，坏账率可以差一倍。
  ★坑 2：问"近 1 周 FPD7"在时间上不成立 —— 近一周放款的还没到观测点。
         正确口径是"近 1 周**到期**的批次"，窗口要整体前移 lag 天。
         这个修正必须由系统做（check_maturity），不能靠 LLM 临场推理。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from . import access, entities, knowledge
from .codes import STAGE_ORDER, err

# 数据截止日（回测快照的"今天"）。真实系统里是 max(快照日期) 或数仓分区日。
AS_OF = "2026-06-30"


# ---------------------------------------------------------------------------
# 一、时间窗口：自然语言 → 具体日期区间
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Window:
    """一个左闭右闭的日期区间 + 人的说法（label 会原样进回答，便于核对）。"""
    label: str
    date_from: str
    date_to: str

    def shift(self, days: int) -> "Window":
        """整体前移/后移 N 天。到期口径修正就靠它（★坑 2）。"""
        d1 = _d(self.date_from) + timedelta(days=days)
        d2 = _d(self.date_to) + timedelta(days=days)
        return Window(f"{self.label}[到期口径{'-' if days < 0 else '+'}{abs(days)}d]",
                      d1.isoformat(), d2.isoformat())

    def contains(self, d: str) -> bool:
        return self.date_from <= d <= self.date_to

    @property
    def days(self) -> int:
        return (_d(self.date_to) - _d(self.date_from)).days + 1

    def as_dict(self) -> dict:
        return {"label": self.label, "from": self.date_from,
                "to": self.date_to, "days": self.days}


def _d(s: str) -> date:
    return date.fromisoformat(s[:10])


# 窗口说法 → 天数。教学要点：这里只认少数几种说法，认不出来就返回 None，
# 由对话层去反问 —— 猜时间窗口是回测 Agent 最不该有的"聪明"。
_WINDOW_PATTERNS: list[tuple[str, int]] = [
    ("近一周", 7), ("近1周", 7), ("最近一周", 7), ("近7天", 7),
    ("近两周", 14), ("近半个月", 15),
    ("近一个月", 30), ("近1个月", 30), ("最近一个月", 30), ("近30天", 30), ("上月", 30),
    ("近两个月", 60), ("近三个月", 90), ("近3个月", 90), ("近一季度", 90),
    ("近半年", 180), ("近六个月", 180),
]


def parse_window(text: str, as_of: str = AS_OF) -> Window | None:
    """把"近一个月"/"2026-05"解析成具体窗口；解析不出返回 None（触发反问）。"""
    t = (text or "").replace(" ", "")
    for key, days in _WINDOW_PATTERNS:
        if key in t:
            end = _d(as_of)
            start = end - timedelta(days=days - 1)
            # label 用命中的说法本身（"近一个月"），不要用整句问题 ——
            # label 会原样进回答，整句问题会让"口径：……"读起来像复读机
            return Window(key, start.isoformat(), end.isoformat())
    m = re.search(r"(20\d{2})-(\d{2})", t)          # 指定月份："2026-05"
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        start = date(y, mo, 1)
        end = (date(y + (mo == 12), mo % 12 + 1, 1) - timedelta(days=1))
        if end.isoformat() > as_of:                 # 未来月份直接拒绝
            return None
        return Window(t, start.isoformat(), end.isoformat())
    return None


# ---------------------------------------------------------------------------
# 二、指标定义（MetricDef）—— 语义层的基本单元
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MetricDef:
    metric_id: str
    name: str
    aliases: tuple[str, ...]
    kind: str                     # scalar | table | decomposition
    unit: str                     # pct | count | ratio
    formula: str                  # 人话口径（会原样出现在回答里，供业务核对）
    numerator: str
    denominator: str
    grain: str                    # APPLY_DATE | LOAN_DUE | MOB3
    lag_days: int                 # 表现期要求（0=当期可算）
    dimensions: tuple[str, ...]   # 允许下钻的维度（未声明的维度不许切）
    version: str
    owner: str
    compute: Callable[["View"], dict] = field(repr=False)
    higher_is_better: bool = True

    # ---- V2.5 新增 7 字段：前 3 个喂"理解"，中间 3 个喂"守卫"，最后 1 个管权限 ----
    # description / typical_questions / disambiguation —— 决定"它选不选得对、
    # 选错时问得对不对"。别名只解决"叫什么"，不解决"什么时候该用"。
    description: str = ""
    typical_questions: tuple[str, ...] = ()
    disambiguation: str = ""
    # not_applicable / requires_params / healthy_range —— 决定"选了之后能不能
    # 算、算完敢不敢下结论"。这三条必须由指标自己声明，不能由代码临时决定。
    not_applicable: str = ""
    requires_params: dict = field(default_factory=dict)
    healthy_range: dict = field(default_factory=dict)
    # access_policy：**空表示所有角色可见**；非空表示只有列出的角色能调。
    # 权限挂在指标上，不挂在表上（见 core/access.py 开头）。
    access_policy: tuple[str, ...] = ()

    def describe(self) -> str:
        """口径声明 —— 回答里必须带这句，否则业务无法核对。"""
        return (f"{self.name}：{self.numerator} / {self.denominator}"
                f"（{self.grain}，lag={self.lag_days}d，口径版本 {self.version}）")


@dataclass
class View:
    """一次提问对应的数据切片 —— 所有 compute 函数只看这个对象。

    为什么要包一层：让"口径"与"取数"分离。
    compute 函数里不出现 SQL、不出现 run_id、不出现日期比较 ——
    切片是 catalog.build_view 一次算好的，口径函数保持纯函数、可单测。
    """
    db: Any
    run_id: str
    window: Window
    as_of: str
    product: str | None
    apps: list[dict]              # 窗口内的申请（已按产品过滤）
    facts: list[dict]             # 这些申请在上述 run 里的事实行
    loans: list[dict]             # 这些申请的放款台账（含 FPD7/FPD30/MOB3 标签）
    # V2.5：行权限的生效结果 + 声明层去重计数（进审计，用来发现上游 join 回归）
    row_filter: dict = field(default_factory=dict)
    dups_removed: int = 0

    # ---- 常用派生集合（挂在 View 上，避免每个 compute 各写一遍）----
    def final_pass_ids(self) -> set[str]:
        """终局通过 = S4_LIM 环节全部策略 PASS（口径同 metrics._stage_pass_set）。"""
        by_apply: dict[str, set[str]] = {}
        for f in self.facts:
            if f["stage"] == "S4_LIM":
                by_apply.setdefault(f["apply_id"], set()).add(f["result"])
        return {a for a, rs in by_apply.items() if rs == {"PASS"}}

    def apps_by_id(self) -> dict[str, dict]:
        return {a["apply_id"]: a for a in self.apps}


def build_view(db, run_id: str, window: Window, product: str | None = None,
               as_of: str = AS_OF, row_filter: dict | None = None) -> View:
    """按窗口/产品/行权限切一份数据视图（取数只在这里发生）。

    教学要点：三个来源各自过滤同一批 apply_id，保证分子分母同源 ——
    报告里出现"通过率的分母和坏账率的分母不是同一批人"，八成就是
    在某个 compute 里自己写了 where 条件。

    V2.5 两处改动（都不动 compute）：
      ① join 与去重从"硬编码 + 每个 compute 各自 set()"改为按
         core/entities.py 的声明生成，并在**声明层**按实体主键收敛；
      ② row_filter 在这里、也就是**取数前**生效（取数后筛 = 越权已经发生）。
    """
    apps, _ = entities.select_applicants(db, window, product, row_filter)
    facts, d1 = entities.select_facts(db, run_id, apps)
    loans, d2 = entities.select_loans(db, apps)
    return View(db=db, run_id=run_id, window=window, as_of=as_of,
                product=product, apps=apps, facts=facts, loans=loans,
                row_filter=dict(row_filter or {}), dups_removed=d1 + d2)


# ---------------------------------------------------------------------------
# 三、指标实现（公式即代码 —— 不存在"文档口径"与"代码口径"两套）
# ---------------------------------------------------------------------------
def _pct(x: float | None) -> float | None:
    return None if x is None else round(x, 4)


def _m_approval_rate(v: View) -> dict:
    n = len(v.apps)
    passed = len(v.final_pass_ids())
    return {"value": _pct(passed / n) if n else None, "numerator_n": passed,
            "denominator_n": n}


def _m_stage_rates(v: View) -> dict:
    """环节表：进入 / 通过 / 拒绝 / 环节内通过率。

    ★口径：环节内通过率的分母是"进入本环节人数"，不是全量申请。
    漏斗是短路的，用全量做分母会把 S4 的通过率算成 5%，业务一眼就知道错了。
    """
    entered: dict[str, set] = {}
    for f in v.facts:
        entered.setdefault(f["stage"], set()).add(f["apply_id"])
    final_pass = v.final_pass_ids()
    rows = []
    for i, stage in enumerate(STAGE_ORDER):
        e = entered.get(stage, set())
        if not e:
            continue
        passed = entered.get(STAGE_ORDER[i + 1], set()) if i + 1 < len(STAGE_ORDER) \
            else final_pass & e
        # 注：S4 之后没有下一环节，"环节通过"就是终局通过的人
        reject = {f["apply_id"] for f in v.facts
                  if f["stage"] == stage and f["result"] == "FAIL"}
        # ★坑 3：通过率 + 拒绝率 常常加起来不到 100%。
        # 差的这一块是 UNKNOWN（数据缺失/哨兵值）—— 它既没通过也没被拒。
        # 不显式给出"其余"这一档，业务会以为表算错了（"80% + 0% 是什么鬼"），
        # 更糟的是有人会拿 80% 当"剩下 20% 都过了"去做决策。
        other = len(e) - len(passed) - len(reject)
        rows.append({"stage": stage, "entered": len(e), "passed": len(passed),
                     "rejected": len(reject), "unknown": max(other, 0),
                     "pass_rate": _pct(len(passed) / len(e)) if e else None,
                     "reject_rate": _pct(len(reject) / len(e)) if e else None,
                     "other_rate": _pct(max(other, 0) / len(e)) if e else None})
    return {"rows": rows, "denominator_n": len(v.apps)}


def _m_policy_rates(v: View) -> dict:
    """策略表：执行数 / 通过 / 拒绝 / 未知 / 通过率 / 拒绝率 / 数据缺失率。

    ★口径：分母是"该策略被真正执行的人数"（条件于走到这里）。
    不同环节的策略不能横向比绝对值 —— S1 的策略执行 100% 的人，
    S4 的策略只执行漏斗末端的人，跨环节比拒绝率等于比两个不同人群。
    """
    executed: dict[str, dict] = {}
    for f in v.facts:
        d = executed.setdefault(f["policy_id"], {"stage": f["stage"], "exec": 0,
                                                 "pass": 0, "fail": 0,
                                                 "unknown": 0, "miss": 0})
        if f["value_state"] == "NOT_EXECUTED":
            continue
        d["exec"] += 1
        if f["result"] == "PASS":
            d["pass"] += 1
        elif f["result"] == "FAIL":
            d["fail"] += 1
        else:
            d["unknown"] += 1
        if f["value_state"] == "MISSING_SOURCE":
            d["miss"] += 1
    rows = []
    for pid, d in sorted(executed.items(), key=lambda kv: (kv[1]["stage"], kv[0])):
        n = d["exec"]
        if not n:
            continue
        rows.append({"policy_id": pid, "stage": d["stage"], "executed": n,
                     "passed": d["pass"], "rejected": d["fail"], "unknown": d["unknown"],
                     "pass_rate": _pct(d["pass"] / n), "reject_rate": _pct(d["fail"] / n),
                     "other_rate": _pct(d["unknown"] / n),
                     "miss_rate": _pct(d["miss"] / n)})
    return {"rows": rows, "denominator_n": len(v.apps)}


def _matured(v: View, lag: int) -> list[dict]:
    """到期筛：放款日 + lag <= as_of 的借据才算"进入观测窗口"。

    ★坑 1 的落地：这就是 FPD 系列指标唯一正确的分母来源。
    """
    cutoff = (_d(v.as_of) - timedelta(days=lag)).isoformat()
    return [l for l in v.loans if str(l["loan_date"])[:10] <= cutoff]


def _m_fpd(lag: int, field_name: str) -> Callable[[View], dict]:
    def _compute(v: View) -> dict:
        due = _matured(v, lag)
        n = len(due)
        bad = sum(int(l[field_name] or 0) for l in due)
        granted = len(v.loans)
        return {"value": _pct(bad / n) if n else None,
                "numerator_n": bad, "denominator_n": n,
                # 这三个数进回答，专门用来解释"为什么分母比通过人数小"
                "granted_n": granted, "matured_n": n,
                "unmatured_n": granted - n}
    return _compute


def _m_mob3(v: View) -> dict:
    labeled = [a for a in v.apps if a.get("bad_flag") is not None]
    bad = sum(int(a["bad_flag"]) for a in labeled)
    return {"value": _pct(bad / len(labeled)) if labeled else None,
            "numerator_n": bad, "denominator_n": len(labeled)}


# ---------------------------------------------------------------------------
# 四、注册表（含别名、时间语义、可用性）
# ---------------------------------------------------------------------------
CATALOG: dict[str, MetricDef] = {}


def _reg(m: MetricDef) -> MetricDef:
    if m.metric_id in CATALOG:                  # 重复注册直接炸，不给"静默覆盖"留门
        raise ValueError(f"指标重复注册: {m.metric_id}")
    CATALOG[m.metric_id] = m
    return m


_reg(MetricDef(
    metric_id="approval_rate", name="申请通过率", kind="scalar", unit="pct",
    aliases=("通过率", "申请通过率", "授信通过率", "整体通过率", "审批通过率"),
    formula="终局通过的申请数 / 申请总数",
    numerator="终局通过申请数（S4_LIM 全部策略 PASS）", denominator="申请总数",
    grain="APPLY_DATE", lag_days=0, dimensions=("product", "channel", "new_vs_old"),
    version="1.2", owner="风控策略组", compute=_m_approval_rate,
    description=("有多少申请最终被全部环节放行。分母是所有申请（含早期就被拒的人），"
                 "回答的是'整体漏斗的放行比例'，不是某个环节的通过情况，"
                 "也不是授信额度层面的提款率。"),
    typical_questions=("现金贷近一个月通过率是多少",
                       "整体审批通过情况怎么样",
                       "申请通过率掉了几个点",
                       "放款率是多少"),                        # ← 像但不是它（见 CONFUSABLE）
    disambiguation=("通过率有多个口径：申请通过率（分母=全部申请）、环节通过率"
                    "（分母=进入该环节）、策略通过率（分母=该策略被执行）。"
                    "请确认要看哪一个。"),
    not_applicable="",
    requires_params={"product": "required", "window": "optional:近一个月",
                     "dimension": "optional:overall"},
    healthy_range={"low": 0.55, "high": 0.90, "direction": "band"}))

_reg(MetricDef(
    metric_id="stage_rates", name="各环节通过率/拒绝率", kind="table", unit="pct",
    aliases=("每个环节", "各环节", "环节通过率", "环节拒绝率", "环节"),
    formula="环节内通过人数 / 进入该环节人数",
    numerator="通过本环节人数（进入下一环节）", denominator="进入本环节人数",
    grain="APPLY_DATE", lag_days=0, dimensions=("product", "channel"),
    version="1.1", owner="风控策略组", compute=_m_stage_rates,
    description=("逐环节看漏斗：每个环节进来多少人、通过多少人、被拒多少人。"
                 "用于定位'哪一段最堵'。分母是进入该环节的人数，不是全量申请；"
                 "通过+拒绝+其余（数据缺失/未判定）三档闭合到 100%。"),
    typical_questions=("每个环节的通过率是多少",
                       "漏斗哪一段最堵",
                       "各环节审批情况咋样",
                       "S3 环节拒绝率高不高"),
    disambiguation="",
    not_applicable="",
    requires_params={"product": "required", "window": "optional:近一个月"},
    healthy_range={}))

_reg(MetricDef(
    metric_id="policy_rates", name="各策略通过率/拒绝率", kind="table", unit="pct",
    aliases=("每个策略", "各策略", "策略通过率", "策略拒绝率", "策略节点"),
    formula="策略通过（拒绝）人数 / 该策略被执行人数",
    numerator="该策略 PASS（FAIL）人数", denominator="该策略被执行人数（非 NOT_EXECUTED）",
    grain="APPLY_DATE", lag_days=0, dimensions=("product", "channel", "new_vs_old"),
    version="1.2", owner="风控策略组", compute=_m_policy_rates,
    description=("逐条策略看执行结果：被执行多少人、通过/拒绝/未知各多少。"
                 "分母是'该策略被执行人数'（被前序策略拒掉的人根本不会执行它），"
                 "所以跨环节比拒绝率等于比两个不同人群，不能横向比。"),
    typical_questions=("每个策略的拒绝率",
                       "哪个策略节点拒得最多",
                       "P_CR_001 现在拒多少人",
                       "各策略执行率"),
    disambiguation="",
    not_applicable="",
    requires_params={"product": "required", "window": "optional:近一个月"},
    healthy_range={}))

_reg(MetricDef(
    metric_id="fpd7_rate", name="FPD7 首期逾期率", kind="scalar", unit="pct",
    aliases=("FPD7", "fpd7", "首期逾期7", "FPD7逾期率", "7天逾期"),
    formula="首期逾期≥7天的借据数 / 已到期借据数",
    numerator="首期逾期 ≥7 天的借据数", denominator="放款且首期应还日已过 7 天的借据数",
    grain="LOAN_DUE", lag_days=7, dimensions=("product", "channel"), version="2.0",
    owner="资产质量组", compute=_m_fpd(7, "fpd7"), higher_is_better=False,
    description=("放款后首期还款日过 7 天仍没还的比例，衡量'早期还款意愿'。"
                 "分母只含已满 7 天的借据，刚放款的批次不进分母；"
                 "所以近一周的窗口必须按'到期批次'前移 7 天。"),
    typical_questions=("现金贷 FPD7 是多少",
                       "近一周首逾有没有变差",
                       "FPD7 涨了是什么原因",
                       "7 天逾期率多少"),
    disambiguation="",
    not_applicable="窗口天数 ≤ 7 天，或已到期借据 < 30 笔",
    requires_params={"product": "required", "window": "required"},
    healthy_range={"high": 0.05, "direction": "lower_better"}))

_reg(MetricDef(
    metric_id="fpd30_rate", name="FPD30 首期逾期率", kind="scalar", unit="pct",
    aliases=("FPD30", "fpd30", "首期逾期30", "FPD30逾期率", "30天逾期"),
    formula="首期逾期≥30天的借据数 / 已到期借据数",
    numerator="首期逾期 ≥30 天的借据数", denominator="放款且首期应还日已过 30 天的借据数",
    grain="LOAN_DUE", lag_days=30, dimensions=("product", "channel"), version="2.0",
    owner="资产质量组", compute=_m_fpd(30, "fpd30"), higher_is_better=False,
    description=("放款后首期还款日过 30 天仍未还的比例。分母是**已满 30 天的借据**，"
                 "不是通过人数 —— 刚放款的批次既不算逾期也不算正常，"
                 "塞进分母等于默认它们表现良好，坏账率会被系统性低估。"),
    typical_questions=("FPD30 是多少",
                       "首期 30 天逾期率",
                       "近一个月 FPD30 有没有升高",
                       "逾期率多少"),                          # ← 歧义（7/30/MOB3）
    disambiguation=("'逾期率'可能指 FPD7（首期 7 天）、FPD30（首期 30 天）"
                    "或 MOB3 坏账率（账龄 3 期）。请指定一个。"),
    not_applicable="窗口天数 ≤ 30 天，或已到期借据 < 30 笔",
    requires_params={"product": "required", "window": "required"},
    healthy_range={"high": 0.08, "direction": "lower_better"}))

_reg(MetricDef(
    metric_id="mob3_bad_rate", name="MOB3 坏账率", kind="scalar", unit="pct",
    aliases=("MOB3", "mob3", "坏账率", "MOB3坏账率"),
    formula="MOB3 DPD30 为坏的借据数 / 已满 MOB3 的放款人数",
    numerator="MOB3 逾期 ≥30 天的借据数", denominator="已满 MOB3 表现期的放款人数",
    grain="MOB3", lag_days=90, dimensions=("product", "channel", "new_vs_old"),
    version="1.0", owner="资产质量组", compute=_m_mob3, higher_is_better=False,
    description=("放款满 3 期（90 天）时点已发生 DPD30 的比例，是资产质量的最终口径。"
                 "lag 最长，近三个月窗口基本不可用 —— 此时必须明确说'算不出'，"
                 "而不是给一个 0%。"),
    typical_questions=("MOB3 坏账率多少",
                       "这批放款的最终坏账怎么样",
                       "近半年 MOB3 有没有恶化"),
    disambiguation="",
    not_applicable="窗口天数 ≤ 90 天，或已满 MOB3 的借据 < 30 笔",
    requires_params={"product": "required", "window": "required"},
    healthy_range={"high": 0.10, "direction": "lower_better"}))


def _m_loan_amount(v: View) -> dict:
    """放款额：窗口内已放款借据的 loan_amount 汇总。

    ★口径：已放款借据的**原始金额**求和，不分阶、不折现、不含手续费。
    分母是放款笔数（不是申请数），因为"放款额"的主语是"放款的借据"。
    """
    rows = []
    total = 0.0
    for ln in v.loans:
        amt = ln.get("loan_amount")
        if amt is None:
            continue
        d = (ln.get("loan_date") or "")[:10]
        rows.append({"loan_date": d,
                     "loan_amount": float(amt),
                     "apply_id": ln.get("apply_id")})
        total += float(amt)
    by_day: dict[str, float] = {}
    for r in rows:
        by_day[r["loan_date"]] = by_day.get(r["loan_date"], 0.0) + r["loan_amount"]
    day_rows = [{"date": k, "loan_amount": round(v, 2)}
                for k, v in sorted(by_day.items()) if k]
    return {"value": round(total, 2),
            "n": len(rows),
            "avg_per_loan": round(total / len(rows), 2) if rows else None,
            "by_day": day_rows}


_reg(MetricDef(
    metric_id="loan_amount", name="放款额", kind="scalar", unit="CNY",
    aliases=("放款额", "放款金额", "放多少钱", "放款规模", "当天放款额"),
    formula="窗口内已放款借据的 loan_amount 求和",
    numerator="已放款借据的原始金额", denominator="已放款借据数",
    grain="LOAN_DATE", lag_days=0, dimensions=("product", "channel", "day"),
    version="1.0", owner="资产质量组", compute=_m_loan_amount,
    description=("窗口内所有已放款借据的原始合同金额求和。"
                 "不含未通过申请、不含未放款授信、不含利息/手续费。"
                 "同时返回按天聚合表，用于看放款节奏。"),
    typical_questions=("近一个月每天的放款额",
                       "这周放了多少钱",
                       "现金贷放款规模是多少"),
    disambiguation="",
    not_applicable="窗口内放款借据 < 1 笔",
    requires_params={"product": "required", "window": "required"},
    healthy_range={}))


# ---------------------------------------------------------------------------
# 五、别名解析（歧义显式化，不猜）
# ---------------------------------------------------------------------------
# ★受控词表里最值钱的部分不是同义词，是**误称**。
# 只存同义词，"放款率"会被当成"通过率"的邻居直接匹配过去；而它俩中间差一个
# 支用环节，业务含义不同。所以误称必须显式登记成"歧义"，命中即反问。
CONFUSABLE: dict[str, tuple[str, ...]] = {
    "放款率": ("approval_rate", "policy_rates"),
    "提款率": ("approval_rate",),
    "审批率": ("approval_rate", "stage_rates"),
    "逾期率": ("fpd7_rate", "fpd30_rate", "mob3_bad_rate"),
    "坏账": ("fpd30_rate", "mob3_bad_rate"),
}


def resolve(text: str) -> dict[str, Any]:
    """在一句话里找指标。返回命中列表 + 是否歧义。

    教学要点：宁可"歧义 → 反问"，不可"歧义 → 猜一个"。
    回测回答错一个口径，下游决策全错，而且没人会发现问题。
    """
    t = (text or "").replace(" ", "")
    hits = []
    for m in CATALOG.values():
        best = None
        for a in m.aliases:
            if a and a in t and (best is None or len(a) > len(best)):
                best = a
        if best:
            hits.append({"metric_id": m.metric_id, "alias": best,
                         "specificity": len(best), "kind": m.kind})
    hits.sort(key=lambda h: -h["specificity"])

    # 误称优先：没有命中任何具体别名、但出现了误称 → 直接判歧义
    if not hits:
        for term, cands in CONFUSABLE.items():
            if term in t:
                return {"hits": [], "metric_id": None, "ambiguous": list(cands),
                        "note": f"「{term}」有多个可能口径，需确认",
                        "confusable": term}

    # "通过率"同时命中 approval_rate 与 policy_rates/stage_rates 的别名时，
    # 由更具体的词（每个策略/每个环节）胜出；否则判为歧义，交对话层处理
    top = hits[0]["specificity"] if hits else 0
    best_hits = [h for h in hits if h["specificity"] == top]
    return {"hits": hits,
            "metric_id": best_hits[0]["metric_id"] if len(best_hits) == 1 else None,
            "ambiguous": [h["metric_id"] for h in best_hits] if len(best_hits) > 1 else [],
            "note": ("关键词过泛，需确认具体口径" if len(best_hits) > 1 else "")}


def describe(metric_id: str) -> str:
    """口径声明（回答的"口径脚注"部分）。未注册的 id 直接异常 —— 口径封闭。"""
    if metric_id not in CATALOG:
        raise KeyError(f"未注册的口径: {metric_id}（语义层拒绝计算）")
    return CATALOG[metric_id].describe()


# ---------------------------------------------------------------------------
# 六、表现期校准（vintage）—— V2 最"智能"的一段，却是纯规则
# ---------------------------------------------------------------------------
def check_maturity(metric_id: str, window: Window,
                   as_of: str = AS_OF) -> dict[str, Any]:
    """判断"这个窗口 + 这个指标"在时间上能不能算；不能算就给出正确口径。

    ★坑 2 的处理逻辑（以"近 1 周 FPD7"为例）：
        请求窗口 2026-06-24 ~ 06-30 的 FPD7
        → 这批人放款后根本没过 7 天，FPD7 还不存在
        → 正确问法是"近 1 周**到期**的批次"，即窗口整体前移 7 天：
          2026-06-17 ~ 06-23 放款、已满 7 天的人
        → 系统必须【自己完成这个换算并把口径写进回答】，
          而不是硬算出一个 0% 或 100% 的假数字。

    返回值：
        ok          —— 能否按原窗口算
        window      —— 修正后的窗口（ok=True 时等于原窗口）
        corrected   —— 是否做了到期口径修正（评估器会检查这个标志）
        reason      —— 给人看的解释
        blocked     —— 完全不可算（例如指标 lag 大于数据跨度）
    """
    m = CATALOG[metric_id]
    if m.lag_days <= 0:
        return {"ok": True, "window": window, "corrected": False,
                "reason": "当期指标，无需表现期", "blocked": False}
    # 窗口结束日 + lag 是否还在数据范围内
    if (_d(window.date_to) + timedelta(days=m.lag_days)).isoformat() > as_of:
        corrected = window.shift(-m.lag_days)
        if corrected.date_from > as_of or corrected.days <= 0:
            return {"ok": False, "window": window, "corrected": False,
                    "reason": f"{m.name} 需要 {m.lag_days} 天表现期，数据不足以支撑该窗口",
                    "blocked": True}
        return {"ok": False, "window": corrected, "corrected": True,
                "reason": (f"{m.name} 有 {m.lag_days} 天表现期：{window.label}放款的借据"
                           f"尚未到观测点，改按【到期批次】口径计算 —— "
                           f"{corrected.date_from} ~ {corrected.date_to} 放款、已满 "
                           f"{m.lag_days} 天的那批"),
                "blocked": False}
    return {"ok": True, "window": window, "corrected": False,
            "reason": f"窗口已满 {m.lag_days} 天表现期", "blocked": False}


def compute(metric_id: str, view: View) -> dict[str, Any]:
    """唯一的计算入口。返回 {value, numerator_n, denominator_n, def(...), ...}。"""
    m = CATALOG[metric_id]
    out = m.compute(view)
    out["metric_id"] = metric_id
    out["definition"] = m.formula
    out["formula_text"] = m.describe()
    out["level"] = "MEASURED"
    # 样本量提示：<30 一律标注（结论可以为空，但不能让读者以为是稳的）
    den = out.get("denominator_n")
    out["sample_warning"] = bool(isinstance(den, int) and 0 < den < 30)
    return out


def list_metrics() -> list[dict]:
    """给 LLM/人的"指标目录"工具（受控查询，不是 RAG）。"""
    return [{"metric_id": m.metric_id, "name": m.name, "aliases": list(m.aliases),
             "kind": m.kind, "formula": m.formula, "lag_days": m.lag_days,
             "dimensions": list(m.dimensions),
             "description": m.description,
             "typical_questions": list(m.typical_questions),
             "version": m.version, "owner": m.owner}
            for m in CATALOG.values()]


# ---------------------------------------------------------------------------
# 七、契约层：给 LLM 看的"能选什么、选错会怎样"（V2.5）
# ---------------------------------------------------------------------------
def contract(metric_id: str) -> dict:
    """导出单个指标的契约片段。

    ★这里**刻意不含 formula 的计算表达式**：契约是"选什么"，不是"怎么算"。
      一旦契约里出现 expression/sql/formula 这类键，模型就会以为自己能改算法。
      要给模型解释口径，用 description（人话）+ get_metric_definition（只读）。
    """
    m = CATALOG[metric_id]
    return {
        "metric_id": m.metric_id,
        "name": m.name,
        "description": m.description,
        "kind": m.kind,
        "params": {
            "product": {"type": "enum", "required": True,
                        "values": sorted(knowledge.PRODUCTS)},
            "window": {"type": "window", "required": m.lag_days > 0,
                       "note": m.requires_params.get("window", "")},
            "dimension": {"type": "enum", "required": False,
                          "values": ["overall", *m.dimensions]},
        },
        "returns": {"scalar": "value + numerator + denominator + n",
                    "table": "rows(带 grain 声明)",
                    "decomposition": "分项 + 残差"}.get(m.kind, m.kind),
        "aliases": list(m.aliases),
        "typical_questions": list(m.typical_questions),
        "disambiguation": m.disambiguation,
        "not_applicable": m.not_applicable,
        "healthy_range": m.healthy_range,
        "version": m.version,
    }


def _norm_window(w) -> Window | None:
    if w is None or isinstance(w, Window):
        return w
    if isinstance(w, dict):
        if w.get("from") and w.get("to"):
            return Window(w.get("label") or "自定义窗口", w["from"], w["to"])
        if w.get("label"):
            return parse_window(w["label"])
        return None
    return parse_window(str(w))


def guard_call(metric_id: str, params: dict | None = None,
               role: str | None = None, as_of: str = AS_OF) -> dict:
    """闸门 ①.5：口径守卫。**不抛异常、不猜默认值**，只返回结构化错误码。

    为什么不做成"抛异常"：异常 LLM 读不懂，只会变成一次白屏或一句"系统错误"；
    而错误码是模型能照抄给用户的话（"这个口径要 7 天表现期，改成 xx~yy 吧"）。

    检查顺序与理由：
      ① 在册吗（NOT_REGISTERED）—— 没定义的东西不能靠模型现编；
      ② 这个角色能看吗（NOT_VISIBLE）—— 措辞不得暴露"系统里有"；
      ③ 维度能拆吗（DENIED_DIMENSION）—— 绝不退化成"按全量算"；
      ④ 必填参数齐吗（NEED_CLARIFY）—— 缺 product 不许按现金贷兜底；
      ⑤ 参数在域内吗（PARAM_OUT_OF_DOMAIN）；
      ⑥ 时间上成立吗（NOT_APPLICABLE）—— 3 天窗口算 FPD7 是本项目最危险的错。
    """
    params = dict(params or {})
    m = CATALOG.get(metric_id)
    if m is None:
        return err("NOT_REGISTERED", metric_id=metric_id,
                   registered=sorted(CATALOG))
    pol = access.policy(role)
    if m.access_policy and pol.role not in m.access_policy:
        return err("NOT_VISIBLE", metric_id=metric_id, role=pol.role)
    chk = pol.check_metric(metric_id)
    if not chk.get("ok"):
        return chk

    dim = params.get("dimension") or "overall"
    chk = pol.check_dimension(metric_id, dim)
    if not chk.get("ok"):
        return chk
    if dim != "overall" and dim not in m.dimensions:
        return err("PARAM_OUT_OF_DOMAIN", metric_id=metric_id, param="dimension",
                   value=dim, allowed=["overall", *m.dimensions])
    # grain 与维度必须相容（grain 决定分母来源，entities 决定哪些行能进分母）
    if dim not in entities.GRAIN_DIMENSIONS.get(m.grain, ()) and dim != "overall":
        return err("NO_JOIN_PATH", metric_id=metric_id, grain=m.grain, dimension=dim)

    product = params.get("product")
    if not product:
        if m.requires_params.get("product") == "required":
            return err("NEED_CLARIFY", slot="product", metric_id=metric_id,
                       options=sorted(knowledge.PRODUCTS))
    elif product not in knowledge.PRODUCTS:
        return err("PARAM_OUT_OF_DOMAIN", param="product", value=product,
                   allowed=sorted(knowledge.PRODUCTS))

    win = _norm_window(params.get("window"))
    if win is None and m.requires_params.get("window", "").startswith("required"):
        return err("NEED_CLARIFY", slot="window", metric_id=metric_id,
                   options=["近一周", "近一个月", "近三个月", "2026-05"])
    if win is not None and m.lag_days > 0 and win.days <= m.lag_days:
        return err("NOT_APPLICABLE", metric_id=metric_id,
                   reason=(f"{win.label}只有 {win.days} 天，而{m.name}需要 "
                           f"{m.lag_days} 天表现期；分子会全是 0，算出来是假数字"),
                   min_days=m.lag_days + 1,
                   suggested_window=Window(f"近{m.lag_days + 1}天",
                                           (date.fromisoformat(as_of)
                                            - timedelta(days=m.lag_days)).isoformat(),
                                           as_of).as_dict())

    return {"ok": True, "metric_id": metric_id, "role": pol.role,
            "params": {"product": product, "window": win, "dimension": dim},
            "row_filter": pol.row_filter_for({"product": product})}


def post_guard(res: dict) -> dict:
    """算完之后的守卫：样本不足时**只给 n，不给率**。

    为什么不在 guard_call 里做：到期样本数要取完数才知道。
    这类"必须看完数据才能判"的条件，一律在这里，不能塞进 compute
    （compute 保持纯函数，只算不判）。
    """
    den = res.get("denominator_n")
    if isinstance(den, int) and 0 < den < 30:
        return err("VINTAGE_BLOCKED", metric_id=res.get("metric_id"),
                   n=den, threshold=30,
                   value=res.get("value"),
                   reason="到期样本不足 30 笔：只给样本量，不给比率")
    return {"ok": True, "result": res}


# ---------------------------------------------------------------------------
# 八、定义哈希：同 id 静默改义是最隐蔽的一种污染（V2.5）
# ---------------------------------------------------------------------------
_HASH_FILE = Path(__file__).with_name("definition_hashes.json")


def definition_hash(metric_id: str) -> str:
    """一个指标"含义"的指纹：分子/分母/口径/粒度/版本/kind 全在里面。"""
    m = CATALOG[metric_id]
    body = json.dumps({"id": m.metric_id, "formula": m.formula,
                       "numerator": m.numerator, "denominator": m.denominator,
                       "grain": m.grain, "lag": m.lag_days, "kind": m.kind,
                       "version": m.version}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def verify_definitions(strict: bool = True) -> dict:
    """启动自检：id 不变而定义变了 → 直接报错。

    允许的唯一情形是**版本号也一起变了**（口径改版 = 新增版本，历史不覆盖）。
    没有这条，最坏的情况是：id 不变、数变了，所有历史对比全部失效，
    而没有人能发现 —— 因为没有任何一处"应该报错"的地方。
    """
    current = {mid: {"version": CATALOG[mid].version,
                     "hash": definition_hash(mid)} for mid in CATALOG}
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
        raise ValueError(f"定义哈希校验失败（同 id 静默改义）: {silent}")
    return out
