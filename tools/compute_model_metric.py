# -*- coding: utf-8 -*-
"""工具壳：compute_model_metric（模型级指标）。V2.5 §8.6

════════════════════════════════════════════════════════════════════
这个壳比上一个多两件事
════════════════════════════════════════════════════════════════════
① **三级路由**：
     物化命中（参数与某 job 完全一致）→ 看书，标 MATERIALIZED as_of=…
     参数不同                       → 实时算，标 MEASURED
     都没有                         → NOT_REGISTERED / NOT_APPLICABLE
   每个模型级数字必须带来源标注。没有它，"报告和 Agent 说的不一样"永远查不清。

② **按 kind 三种返回形态**：
     scalar → 值 + CI + n（好坏各多少）
     bins   → 分箱表（> MAX_INLINE_ROWS 行则外卸，只留摘要 + ref_id）
     curve  → 只给摘要（极值点 + 面积），曲线外卸
   同一种返回结构对三类指标，模型一定会读错（把 bins 当 scalar 只报一个数）。

════════════════════════════════════════════════════════════════════
入参红线
════════════════════════════════════════════════════════════════════
只收 metric_id + params{window, product, dimension, bins, label, ref_period}。
**不收切点**（切点是口径）、不收 score_col 表达式、不收 label 定义式。
"""
from __future__ import annotations

from core import model_metrics
from core.codes import err

MAX_INLINE_ROWS = 8

_ALLOWED = {"window", "product", "dimension", "bins", "label", "ref_period",
            "score_source"}



def validate_args(args: dict) -> dict:
    if not isinstance(args, dict):
        return err("PARAM_OUT_OF_DOMAIN", reason="args 必须是对象")
    extra = set(args) - {"metric_id", "params"}
    if extra:
        return err("PARAM_OUT_OF_DOMAIN", reason=f"不接受的参数: {sorted(extra)}",
                   allowed=["metric_id", "params"])
    params = args.get("params") or {}
    extra_p = set(params) - _ALLOWED
    if extra_p:
        return err("PARAM_OUT_OF_DOMAIN",
                   reason=f"params 里不接受的键: {sorted(extra_p)}",
                   allowed=sorted(_ALLOWED),
                   hint="切点/表达式不接受：切点是口径，由函数按指标声明生成")
    return {"ok": True}


def run(db, args: dict, role: str | None = None,
        as_of: str = model_metrics.catalog.AS_OF,
        allow_materialized: bool = True) -> dict:
    """工具入口。返回统一形状（含 source 标记与 ref_id）。"""
    bad = validate_args(args)
    if not bad.get("ok"):
        return bad
    # 同上：缺 metric_id 是"参数错"，不是"程序 bug"，必须走错误码
    if not args.get("metric_id"):
        return err("PARAM_OUT_OF_DOMAIN", param="metric_id", reason="缺少 metric_id",
                   allowed=sorted(model_metrics.MODEL_CATALOG))
    metric_id = args["metric_id"]
    params = args.get("params") or {}

    if allow_materialized:
        from scheduler import registry
        hit = registry.find_materialized(db, metric_id, params, as_of)
        if hit:
            return _from_materialized(db, metric_id, params, hit)

    res = model_metrics.compute_model_metric(metric_id, params, role=role,
                                             as_of=as_of, db=db)
    if not res.get("ok"):
        return res
    out = _shape(res, source="MEASURED")
    # T-105：**只有真实提问**才计数。定时任务直接调 compute_model_metric，
    # 不经过这里 —— 否则每个 job 跑 5 天就把自己"升格"了，队列立刻失去意义。
    # 计数失败不该让回答失败（它是遥测），但也不能静默：
    # 出错写进返回的 promotion_error，让它在回答里看得见。
    try:
        from scheduler import promotion
        pr = promotion.record_question(db, metric_id, params, as_of)
        out["promotion"] = {"hits": pr["hits"], "threshold": pr["threshold"],
                            "should_promote": pr["should_promote"]}
    except Exception as e:                                      # noqa: BLE001
        out["promotion_error"] = f"{type(e).__name__}: {e}"
    return out


def _from_materialized(db, metric_id: str, params: dict, hit: dict) -> dict:
    """物化命中：只读表，不重算。**读不到就说不存在，不回落成实时算**。"""
    rows = hit["rows"]
    first = rows[0]
    value = None
    if metric_id == "ks":
        value = next((r["ks_max"] for r in rows if r.get("ks_max") is not None), None)
    elif metric_id == "psi":
        value = next((r["psi"] for r in rows if r.get("psi") is not None), None)
    elif metric_id == "auc":
        value = next((r["auc"] for r in rows if r.get("auc") is not None), None)
    bins = {"rows": [{"bin": r["score_bin"], "n": r["n"],
                      "bad_rate": r["bad_rate"], "woe": r["woe"],
                      "iv_contrib": r["iv_contrib"], "lift": r["lift"],
                      "cum_ks": r["cum_ks"]} for r in rows],
            "edges": []}
    out = {"ok": True, "metric_id": metric_id,
           "name": model_metrics.MODEL_CATALOG[metric_id].name,
           "kind": model_metrics.MODEL_CATALOG[metric_id].kind,
           "value": round(value, 4) if value is not None else None,
           "ci95": None,
           "n": sum(r["n"] or 0 for r in rows) or first.get("n"),
           "bad_n": None, "good_n": None,
           "bins": bins, "bins_n": len(rows),
           "label": (params.get("label")
                     or model_metrics.MODEL_CATALOG[metric_id].label_col),
           "window": params.get("window"),
           "source": "MATERIALIZED", "as_of": hit["as_of"],
           "job_id": hit["job_id"], "param_version": hit["param_version"],
           "base_dist": hit.get("base_dist"),
           "notes": ["物化结果：参数与监控任务完全一致，未重算",
                     "模型级指标不参与 LMDI 分解，也不能与率值混算"]}
    out["envelope_extra"] = (f"等频 {out['bins_n']} 箱（切点见物化表 job="
                             f"{hit['job_id']}）；参照期 不适用（单分布内部比较）；"
                             f"label={out['label']}")
    out["ref_id"] = f"materialized:{hit['job_id']}:{hit['as_of']}"
    out["signature"] = model_metrics.signature(metric_id, params)
    return _shape(out, source="MATERIALIZED")


def _shape(res: dict, source: str) -> dict:
    """按 kind 决定"给模型看多少"：大表外卸，只留摘要 + ref_id。"""
    kind = res.get("kind")
    rows = (res.get("bins") or {}).get("rows") or []
    out = dict(res)
    out["source"] = source
    if kind == "curve":
        # 曲线：本来就只回摘要，这里**不再**往回塞点 —— 一旦塞回来，
        # 前面"外卸"就白做了，而且模型会把 100 个坐标当 100 条事实复述。
        out["rows_inline"] = []
        out["rows_total"] = 0
        out["offloaded"] = True
        out["summary"] = res.get("summary") or {}
        out["ref_id"] = res.get("ref_id") or f"model:{res.get('metric_id')}:none"
        out["note"] = "曲线逐点已外卸，答案里只允许出现 summary 的三个数"
        return out
    if kind == "bins" or res.get("metric_id") in ("iv", "woe_table", "lift"):
        out["rows_inline"] = rows[:MAX_INLINE_ROWS]
        out["rows_total"] = len(rows)
        out["ref_id"] = (res.get("ref_id")
                         or f"model:{res.get('metric_id')}:"
                            f"{res.get('signature')}")
        if len(rows) > MAX_INLINE_ROWS:
            out["offloaded"] = True
            out["summary"] = {"ks_max": res.get("ks_max"),
                              "iv_total": res.get("iv_total"),
                              "monotonic": res.get("monotonic")}
    else:
        out["rows_inline"] = rows[:MAX_INLINE_ROWS]
        out["ref_id"] = (res.get("ref_id")
                         or f"model:{res.get('metric_id')}:{res.get('signature')}")
    return out
