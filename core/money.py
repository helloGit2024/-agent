# -*- coding: utf-8 -*-
"""金额运算的唯一入口 —— 与 approval_lab/core/money.py 同源。

为什么回测系统也要这个文件：
  回测报告里有两类钱：① 数据源调用成本（成本列）；② 件均额度 / 边际利润。
  两类钱只要经过乘除和舍入，float 就会出现"分"级误差。
  回测的特殊敏感性在于：报告的结论是"A 策略比 B 策略多赚 X 元"，
  如果 X 本身有毛刺，业务拿两个版本的报告一对数就穿帮。

三条规矩（照抄审批系统，全项目只允许经它转换金额）：
  1. 计算一律 Decimal，入口用 Decimal(str(x)) —— 不要 Decimal(float)；
  2. 落库一律"分"（int），出参再转回元；
  3. 四舍五入用 ROUND_HALF_UP（银行家舍入会把 2.5 分出成 2 分，对账说不清）。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

CENT = Decimal("0.01")
ZERO = Decimal("0")


def D(x: Any) -> Decimal:
    """任何数字 -> Decimal。float 走 str() 中转，避免把二进制误差带进来。"""
    if isinstance(x, Decimal):
        return x
    if isinstance(x, bool):                     # bool 是 int 的子类，先挡掉
        raise TypeError("金额不接受 bool")
    if isinstance(x, (int, str)):
        return Decimal(x)
    if isinstance(x, float):
        return Decimal(str(x))
    raise TypeError(f"不支持的金额类型: {type(x).__name__}")


def q(x: Any, exp: Decimal = CENT) -> Decimal:
    """量化到分，四舍五入。"""
    return D(x).quantize(exp, rounding=ROUND_HALF_UP)


def to_cents(x: Any) -> int:
    """元 -> 分（整数）。落库只存这个。"""
    try:
        return int(q(x) * 100)
    except InvalidOperation as e:               # NaN / Infinity
        raise ValueError(f"金额非法: {x!r}") from e


def from_cents(c: int | None) -> Decimal | None:
    """分 -> 元（Decimal）。"""
    return None if c is None else q(Decimal(int(c)) / 100)


def as_float(x: Any) -> float | None:
    """出参给前端/JSON 时用。只在边界调用，计算中途不许转 float。"""
    return None if x is None else float(q(x))


def show(x: Any) -> str:
    """展示：整元不带小数，有零头保留两位。"""
    if x is None:
        return "-"
    v = q(x)
    return f"{v:.0f}" if v == v.to_integral_value() else f"{v:.2f}"
