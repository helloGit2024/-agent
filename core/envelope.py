# -*- coding: utf-8 -*-
"""口径信封（V2.5 §7.8）：每次回答都要能被当场核对。

════════════════════════════════════════════════════════════════════
为什么这是"让业务敢用"的唯一办法
════════════════════════════════════════════════════════════════════
同一个数，在 BI 报表里人会先看口径，觉得不对会去问；而 Agent 给出的数，
天然带着"它算过了"的权威感，没人会去核对。所以 Agent 相对裸 SQL 的差异化
不是"能听懂话"，是**"每句话都自带可核对的口径"**。

四要素：定义+版本 / 时间窗+修正标志 / 过滤+行权限 / 数据截止。
四样缺一不可，且**允许写"不适用"，不许留空**：
  留空 = 拼字符串时漏了一行；不适用 = 有人认真判断过这个回答不该有窗口。
  把两种状态混同，闸门就退化成"长度检查"。

════════════════════════════════════════════════════════════════════
为什么不让 LLM 写
════════════════════════════════════════════════════════════════════
模型会把口径写成"按标准风控口径统计" —— 读起来更专业，实际一个字都不能核对。
信封的价值全在"可核对"，一旦允许改写就归零。所以它由登记簿/注册表生成，
在出口闸门里作为硬条件（缺项 → 丢弃叙述回退模板）。
"""
from __future__ import annotations

from . import catalog

T_AS_OF = "T+0"


def _caliber(scope: dict) -> str:
    """【口径】定义 + 版本。数字与文案都来自 CATALOG，不许手写。"""
    mm = scope.get("model_metric")
    if mm:
        from . import model_metrics
        d = model_metrics.MODEL_CATALOG.get(mm)
        if d:
            return (f"{d.name} v{d.version} ｜ {d.score_col} 的分布统计"
                    f"（无分子分母，不可加）")
    mids = list(scope.get("metrics") or [])
    if not mids:
        return "不适用（本回答不含指标计算）"
    parts = []
    for mid in mids:
        m = catalog.CATALOG.get(mid)
        if m is None:
            continue
        parts.append(f"{m.name} v{m.version} ｜ {m.formula}")
    return "；".join(parts) if parts else "不适用"


def _window(scope: dict) -> str:
    w = scope.get("window")
    if not w:
        return "不适用（查配置/查字段，回答的是时点读数）"
    label = w.get("label") or "自定义窗口"
    span = f"{w.get('from')} ~ {w.get('to')}"
    if scope.get("corrected"):
        lag = scope.get("lag_days")
        tail = f"已按到期批次前移 {lag} 天" if lag else "已做到期口径修正"
    elif scope.get("lag_days"):
        tail = "已满表现期，未做前移"
    else:
        tail = "当期指标，未做到期修正"
    return f"{label} {span}（{tail}）"


def _filter(scope: dict) -> str:
    prod = scope.get("product")
    pname = scope.get("product_name") or prod
    prod_txt = f"{pname}({prod})" if prod else "不适用（未按产品切片）"
    dim = scope.get("dimension") or "全部"
    rf = scope.get("row_filter") or {}
    if rf:
        bits = []
        for k, v in rf.items():
            bits.append(f"{k}∈{list(v)}")
        rtxt = "行权限=" + "，".join(bits)
    else:
        rtxt = "行权限=全量"
    return f"产品={prod_txt}；维度={dim}；{rtxt}"


def render(scope: dict, as_of: str | None = None) -> str:
    """生成 4 行（+ 可选第 5 行）口径信封。固定格式，机器可解析。"""
    as_of = as_of or scope.get("as_of") or catalog.AS_OF
    lines = [f"【口径】{_caliber(scope)}",
             f"【窗口】{_window(scope)}",
             f"【过滤】{_filter(scope)}",
             f"【截止】数据截至 {as_of}（{T_AS_OF} 快照）"]
    extra = scope.get("extra")
    if extra:
        # 模型级指标的第 5 行：分箱与参照期（"10 箱"必须带切点，否则等于没写）
        lines.append(f"【分箱】{extra}")
    return "\n".join(lines)


KEYS = ("【口径】", "【窗口】", "【过滤】", "【截止】")


def parse(text: str) -> dict:
    """把信封解析回结构化字段（评估器 15 用它和 trace 逐项比对）。"""
    out: dict = {}
    for line in (text or "").splitlines():
        for k in KEYS + ("【分箱】",):
            if line.startswith(k):
                out[k.strip("【】")] = line[len(k):].strip()
    return out


def validate(text: str, need_extra: bool = False) -> dict:
    """四要素齐全吗？留空与"不适用"必须能区分。"""
    got = parse(text)
    need = list(KEYS) + (["【分箱】"] if need_extra else [])
    missing = [k.strip("【】") for k in need if not got.get(k.strip("【】"))]
    return {"ok": not missing, "missing": missing, "fields": got}
