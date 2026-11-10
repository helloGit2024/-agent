# -*- coding: utf-8 -*-
"""工具壳：compute_metric（可聚合指标）。V2.5 §8.6

════════════════════════════════════════════════════════════════════
这个壳只做四件事，顺序不能变
════════════════════════════════════════════════════════════════════
    入参校验（只认 metric_id + params）→ guard_call → catalog.compute
    → 登记数字 + 生成口径信封 → 返回摘要 + ref_id

为什么 must 走 guard 而不是直接 compute：
  守卫把"不能用"和"算错了"分开。直接算的结果是：3 天窗口的 FPD7 会返回
  0.0%（分子全是 0），业务拿着它去放松策略。

为什么返回 ref_id 而不是全量 rows：
  工具结果 > 3000 token 就要外卸（上下文工程的硬规则）。模型只需要引用，
  不需要把 200 行明细塞回 prompt —— 那既贵又容易读错。
"""
from __future__ import annotations

from core import catalog, envelope, knowledge
from core.codes import err

MAX_INLINE_ROWS = 8


def validate_args(args: dict) -> dict:
    """入参白名单：多一个键都不收。

    ★这一层是"语义层是否封闭"的最后一道：如果这里接受 expression / sql，
      模型就会开始写公式；而一旦某条回答里出现了自拼公式算出的数，
      这个 Agent 在业务侧的可信度就归零了。
    """
    if not isinstance(args, dict):
        return err("PARAM_OUT_OF_DOMAIN", reason="args 必须是对象")
    extra = set(args) - {"metric_id", "params", "role"}
    if extra:
        return err("PARAM_OUT_OF_DOMAIN", reason=f"不接受的参数: {sorted(extra)}",
                   allowed=["metric_id", "params"])
    params = args.get("params") or {}
    extra_p = set(params) - {"product", "window", "dimension"}
    if extra_p:
        return err("PARAM_OUT_OF_DOMAIN",
                   reason=f"params 里不接受的键: {sorted(extra_p)}",
                   allowed=["product", "window", "dimension"])
    return {"ok": True}


def compute_one(db, metric_id: str, params: dict, role: str | None = None,
                run_id: str = "ONLINE_v2.2", as_of: str = catalog.AS_OF) -> dict:
    """guard → 到期校准 → build_view → compute → post_guard。

    返回 {"ok":True, "result":..., "envelope":"...", "numbers":[...]}
    或结构化错误码（不抛异常）。
    """
    chk = catalog.guard_call(metric_id, params, role=role, as_of=as_of)
    if not chk.get("ok"):
        return chk
    p = chk["params"]
    win = p["window"] or catalog.parse_window("近一个月", as_of)
    m = catalog.CATALOG[metric_id]
    corrected = False
    if m.lag_days > 0:
        mt = catalog.check_maturity(metric_id, win, as_of)
        if mt["blocked"]:
            return err("NOT_APPLICABLE", metric_id=metric_id, reason=mt["reason"])
        win, corrected = mt["window"], mt["corrected"]
    view = catalog.build_view(db, run_id, win, p["product"],
                              as_of=as_of, row_filter=chk.get("row_filter"))
    res = catalog.compute(metric_id, view)
    pg = catalog.post_guard(res)
    if not pg.get("ok") and pg["code"] == "VINTAGE_BLOCKED":
        return pg
    res["row_filter"] = chk.get("row_filter") or {}

    scope = {"metrics": [metric_id], "window": win.as_dict(),
             "corrected": corrected, "lag_days": m.lag_days or None,
             "product": p["product"],
             "product_name": knowledge.PRODUCTS[p["product"]]["name"],
             "dimension": p["dimension"], "row_filter": chk.get("row_filter"),
             "as_of": as_of}
    env = envelope.render(scope)
    numbers = _numbers(metric_id, res)
    ref_id = f"metric:{metric_id}:{modelless(win)}"
    return {"ok": True, "metric_id": metric_id, "name": m.name, "kind": m.kind,
            "result": res, "envelope": env, "numbers": numbers,
            "ref_id": ref_id, "rows_inline": _inline(res),
            "source": "MEASURED", "scope": scope}


def modelless(win) -> str:
    return f"{win.date_from}~{win.date_to}"


def _numbers(metric_id: str, res: dict) -> list[dict]:
    """把要进正文的数字列出来（调用方负责登记到 AnswerBook）。"""
    out = []
    if res.get("value") is not None:
        out.append({"key": f"{metric_id}.value", "value": res["value"], "unit": "pct",
                    "metric_id": metric_id})
    if res.get("denominator_n") is not None:
        out.append({"key": f"{metric_id}.n", "value": res["denominator_n"],
                    "unit": "int", "metric_id": metric_id})
    for row in (res.get("rows") or []):
        key = row.get("policy_id") or row.get("stage")
        for fld in ("pass_rate", "reject_rate", "other_rate", "entered",
                    "executed", "passed", "rejected", "unknown"):
            if fld in row:
                out.append({"key": f"{metric_id}.{key}.{fld}", "value": row[fld],
                            "unit": "pct" if fld.endswith("rate") else "int",
                            "metric_id": metric_id})
    return out


def _inline(res: dict) -> list[dict]:
    rows = res.get("rows") or []
    return rows[:MAX_INLINE_ROWS]


def run(db, args: dict, role: str | None = None,
        run_id: str = "ONLINE_v2.2", as_of: str = catalog.AS_OF) -> dict:
    bad = validate_args(args)
    if not bad.get("ok"):
        return bad
    # 缺 metric_id 时返回结构化错误，不许 KeyError 冒到调用方 ——
    # 工具面是"给外部（含 LLM）用的"，任何参数组合都必须有可解释的返回。
    mid = args.get("metric_id")
    if not mid:
        return err("PARAM_OUT_OF_DOMAIN", param="metric_id", reason="缺少 metric_id",
                   allowed=sorted(catalog.CATALOG))
    return compute_one(db, mid, args.get("params") or {}, role, run_id, as_of)
