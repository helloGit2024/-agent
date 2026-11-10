# -*- coding: utf-8 -*-
"""规划用的受控上下文（S1 T-202）：prompt 里**只**放册子元数据，不放任何数据。

════════════════════════════════════════════════════════════════════
为什么上下文要自己建，而不是把问题直接丢给模型
════════════════════════════════════════════════════════════════════
LLM #1（理解层）的职责是把人话翻成提问单，它能"看见"的世界必须只有：
  · 两本册子的指标 id / 名称 / 别名（它能选什么）；
  · 枚举域（产品 / dimension / bins / label / field）；
  · 输出契约与三条 few-shot（它该长成什么样）。
**没有明细数据、没有数字、没有 SQL** —— 上下文中一旦出现数据，
理解层就有机会"顺手回答"，P-06（数字可溯源）立刻失效。

清单全部从册子实时生成（catalog.CATALOG / MODEL_CATALOG / PRODUCTS），
不在这里手抄一份 —— 册子改了 prompt 自动跟着改，两处口径不会漂移。
"""
from __future__ import annotations

from core import catalog, knowledge, model_metrics

# few-shot 的答案侧是**手写死**的：例子本身就是契约的一部分，不能从册子生成
_FEW_SHOT = [
    ("现金贷近一个月，每个环节、每个策略的通过率和拒绝率统计一下",
     '{"intent":"STATS","product":"P_CASH_001","window_text":"近一个月",'
     '"metrics":["approval_rate","stage_rates","policy_rates"],"model_metric":null,'
     '"bins":null,"label":null,"dimension":null,"field":null,"missing":[],"notes":[]}'),
    ("现金贷最近三个月通过率下降的原因是什么？",
     '{"intent":"ANOMALY","product":"P_CASH_001","window_text":"最近三个月",'
     '"metrics":["approval_rate"],"model_metric":null,'
     '"bins":null,"label":null,"dimension":null,"field":null,"missing":[],"notes":[]}'),
    ("现金贷近三个月的 KS 是多少",
     '{"intent":"MODEL_DIAG","product":"P_CASH_001","window_text":"近三个月",'
     '"metrics":[],"model_metric":"ks",'
     '"bins":null,"label":null,"dimension":"overall","field":null,"missing":[],"notes":[]}'),
    ("模型最近表现怎么样",
     '{"intent":"UNKNOWN","product":null,"window_text":null,'
     '"metrics":[],"model_metric":null,'
     '"bins":null,"label":null,"dimension":null,"field":null,"missing":["branch"],'
     '"notes":["既可能在问模型指标（KS/AUC/PSI），也可能在问策略指标（通过率/FPD），先确认"]}'),
]


def _metric_lines() -> list[str]:
    out = []
    for mid in sorted(catalog.CATALOG):
        m = catalog.CATALOG[mid]
        out.append(f"- {m.metric_id}　{m.name}（别名：{'、'.join(m.aliases)}）")
    return out


def _model_lines() -> list[str]:
    out = []
    for mid in sorted(model_metrics.MODEL_CATALOG):
        m = model_metrics.MODEL_CATALOG[mid]
        ref = "；必须指定基准期" if m.ref_period == "required" else ""
        out.append(f"- {m.metric_id}　{m.name}（别名：{'、'.join(m.aliases)}；"
                   f"默认标签 {m.label_col}{ref}）")
    return out


def _product_lines() -> list[str]:
    return [f"- {pid}　{meta['name']}（别名：{'、'.join(meta['aliases'])}）"
            for pid, meta in sorted(knowledge.PRODUCTS.items())]


def plan_prompt(question: str) -> str:
    """拼出规划 prompt。结构固定，段落顺序即优先级（纪律在最前）。"""
    sections = [
        "你是策略回测分析 Agent 的「理解层」。唯一产出是一张提问单 JSON："
        "你不算数、不查数、不输出任何数字。",
        "【硬性纪律】\n"
        "1. 只能用下面能力清单里的指标 id；清单没有的指标就是不具备的能力，不许编造\n"
        "2. 拿不准就举手：把缺的槽位写进 missing，不许猜产品、不许猜时间\n"
        "3. 时间窗口原样摘录问题里的短语（如“近一个月”），由语义层解析成日期，"
        "你不许自己算日期\n"
        "4. 输出只有一个 JSON 对象，不要 Markdown 围栏，不要任何解释文字",
        "【能力清单 · 策略指标（可聚合，两本册子互斥之一）】\n" + "\n".join(_metric_lines()),
        "【能力清单 · 模型指标（模型级，与策略指标互斥）】\n" + "\n".join(_model_lines()),
        "【产品】\n" + "\n".join(_product_lines()),
        "【枚举域】\n"
        f"- dimension ∈ {'|'.join(model_metrics.DIMENSIONS)}\n"
        f"- bins ∈ {'|'.join(str(b) for b in model_metrics.BINS_ALLOWED)}\n"
        f"- label ∈ {'|'.join(model_metrics.LABELS)}",
        "【意图分类】\n"
        "- STATS　统计通过率/拒绝率/FPD「是多少」\n"
        "- ANOMALY　异动归因（为什么下降/升高/变差）\n"
        "- CONTRIBUTION　策略贡献排序、该调哪个、要不要 AB\n"
        "- CONFIG　当前线上策略配置\n"
        "- FIELD　字段在哪张表（field 填字段名）\n"
        "- MODEL_DIAG　模型指标（KS/AUC/Gini/IV/PSI/LIFT/WOE/曲线）\n"
        "- UNKNOWN　听不懂；或既像模型指标又像策略指标且没点名 → missing 填 [\"branch\"]",
        "【输出契约】JSON 对象，键固定为：\n"
        "intent / product / window_text / metrics / model_metric / "
        "bins / label / dimension / field / missing / notes\n"
        "- metrics 与 model_metric 互斥：MODEL_DIAG 填 model_metric 且 metrics=[]；"
        "其余意图反之\n"
        "- missing 只许从 intent|product|window|metric|branch|field 里选\n"
        "- 不需要的键填 null 或 []；notes 填需要向用户声明的默认（没有就 []）",
        "【示例】\n" + "\n\n".join(f"问：{q}\n答：{a}" for q, a in _FEW_SHOT),
        f"【问题】\n{question}",
    ]
    return "\n\n".join(sections)
