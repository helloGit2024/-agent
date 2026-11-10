#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""语义层评估器 13–18（V2.5 §7.11）+ 反向测试。

════════════════════════════════════════════════════════════════════
这六个评估器的共同点：不做也不会报错
════════════════════════════════════════════════════════════════════
  · 自创指标 / 用率值冒充 KS      → 有数、可溯源、"看起来对"
  · 3 天窗口算 FPD7              → 返回 0.0%，比报错危险得多
  · 删掉口径信封                  → 回答更"干净"，业务三个月后才发现对不上
  · 越权时静默按全量算            → 数是错的，且日志里没有痕迹
  · 物化/实时两个来源不标         → "报告和 Agent 说的不一样"永远查不清

所以每个评估器都必须配**反向测试**：给一个坏输入，断言它必须失败。
只验证"好输入能过"的门禁，百分之百会在关键时刻放行坏东西。

跑法：python3 eval/eval_semantic.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import catalog, envelope, model_metrics, semantic_contract  # noqa: E402
from core.codes import SEMANTIC_ERRORS  # noqa: E402

FORBIDDEN = ("sql", "expression", "formula", "table", "column", "where")


# ---------------------------------------------------------------------------
# 评估器（输入都是一份 trace：计划 / 工具调用 / 回答 / 结果 / 角色）
# ---------------------------------------------------------------------------
def ev13_no_free_form_metric(trace: dict) -> dict:
    """计划与工具入参里不得出现"怎么算"的键；metric_id 必须在册。"""
    bad = []
    for src in ("plan", "tool_args"):
        node = trace.get(src) or {}
        bad += _scan(node)
    mid = (trace.get("result") or {}).get("metric_id")
    if mid and mid not in catalog.CATALOG and mid not in model_metrics.MODEL_CATALOG:
        bad.append(f"未注册指标 {mid}")
    return _r(13, "no_free_form_metric", "轨迹", not bad, bad)


def ev14_param_domain_validity(trace: dict) -> dict:
    """参数在域内；率值必须带分子分母；模型级不许出现 bins 非法值。"""
    res = trace.get("result") or {}
    bad = []
    if res.get("bins_n") is not None and res["bins_n"] not in model_metrics.BINS_ALLOWED:
        bad.append(f"bins={res['bins_n']} 不在 {model_metrics.BINS_ALLOWED}")
    if res.get("label") and res["label"] not in model_metrics.LABELS:
        bad.append(f"label={res['label']} 不在域内")
    # 率值 = 可加指标，必须有分母；模型级指标"无分子分母"是设计如此
    is_model = (res.get("metric_id") in model_metrics.MODEL_CATALOG
                or str(trace.get("branch") or "").split(":")[0] == "model")
    if (res.get("value") is not None and res.get("kind") == "scalar"
            and res.get("denominator_n") is None and not is_model):
        bad.append("给了率值却没有分母")
    code = res.get("code")
    if code and code not in SEMANTIC_ERRORS:
        bad.append(f"未知错误码 {code}")
    return _r(14, "param_domain_validity", "答案", not bad, bad)


def ev15_caliber_metadata_present(trace: dict) -> dict:
    """信封四要素齐全，且与 trace 逐项相等。"""
    ans = trace.get("answer") or ""
    need_extra = bool((trace.get("result") or {}).get("envelope_extra"))
    v = envelope.validate(ans, need_extra=need_extra)
    bad = list(v["missing"])
    f = v["fields"]
    res = trace.get("result") or {}
    win = res.get("window") or {}
    if win and f.get("窗口") and (win.get("from") not in f["窗口"]
                                  or win.get("to") not in f["窗口"]):
        bad.append("信封窗口与 trace 不一致")
    if f.get("截止") and catalog.AS_OF not in f["截止"]:
        bad.append("信封截止日与 AS_OF 不一致")
    if res.get("metric_id") in catalog.CATALOG:
        ver = catalog.CATALOG[res["metric_id"]].version
        if f.get("口径") and f"v{ver}" not in f["口径"]:
            bad.append(f"信封口径版本与 CATALOG v{ver} 不一致")
    return _r(15, "caliber_metadata_present", "答案", not bad, bad)


def ev16_access_scope_enforced(trace: dict) -> dict:
    """越权必须返错码，且回答里不允许出现任何数字。"""
    from core import access
    pol = access.policy(trace.get("role"))
    bad = []
    slot = (trace.get("parsed") or {}).get("slots") or {}
    for mid in (slot.get("metrics") or []) + ([slot["model_metric"]]
                                              if slot.get("model_metric") else []):
        if not pol.check_metric(mid).get("ok") and \
                (trace.get("result") or {}).get("kind") != "denied":
            bad.append(f"{mid} 越权但未拒绝")
    res = trace.get("result") or {}
    if res.get("kind") == "denied":
        import re
        if re.search(r"\d+(\.\d+)?%", trace.get("answer") or ""):
            bad.append("越权回答里出现了数字（静默降级）")
        if res["error"]["code"] not in ("NOT_VISIBLE", "DENIED_DIMENSION",
                                        "ROW_FILTERED"):
            bad.append(f"越权错误码不合法: {res['error']['code']}")
    return _r(16, "access_scope_enforced", "轨迹", not bad, bad)


def ev17_nonaggregate_routing(trace: dict) -> dict:
    """命中模型词表 → 必须走 compute_model_metric，且不许出现率值指标。

    反向判据：**出现率值即失败，数字全对也失败**。
    因为它抓的是"答了一个别的、但完全正确的东西"。
    """
    q = trace.get("question") or ""
    hit = model_metrics.detect(q)
    if not hit:
        if any(c["name"] == "compute_model_metric"
               for c in trace.get("tool_calls") or []):
            return _r(17, "nonaggregate_routing", "轨迹", False,
                      ["非模型级问题却调了 compute_model_metric"])
        return _r(17, "nonaggregate_routing", "轨迹", True, [])
    bad = []
    calls = [c["name"] for c in trace.get("tool_calls") or []]
    if "compute_metric" in calls:
        bad.append("模型级问题却调了 compute_metric")
    if "compute_model_metric" not in calls:
        bad.append("未调用 compute_model_metric")
    ans = trace.get("answer") or ""
    res = trace.get("result") or {}
    name = (model_metrics.MODEL_CATALOG.get(hit) or None)
    if name and name.name not in ans:
        bad.append(f"回答里没有指标名「{name.name}」")
    if "分箱" not in ans and "CI" not in ans:
        bad.append("回答里既没有分箱表也没有置信区间")
    for cat in catalog.CATALOG.values():      # 不许出现可聚合指标的名字
        if cat.name in ans:
            bad.append(f"出现率值指标「{cat.name}」（答非所问）")
    if res.get("ok") and res.get("value") is not None and not res.get("label"):
        bad.append("模型级结果缺 label")
    return _r(17, "nonaggregate_routing", "轨迹", not bad, bad)


def ev18_source_provenance(trace: dict, pair: dict | None = None) -> dict:
    """模型级数字必须标来源；MATERIALIZED 可反查 job；两来源逐位一致。"""
    res = trace.get("result") or {}
    bad = []
    if res.get("metric_id") in model_metrics.MODEL_CATALOG:
        src = res.get("source")
        if src not in ("MEASURED", "MATERIALIZED"):
            bad.append(f"来源标记缺失或不合法: {src}")
        if src == "MATERIALIZED" and not (res.get("job_id")
                                          and res.get("param_version")):
            bad.append("标了 MATERIALIZED 但查不到 job_id / param_version")
    if pair:
        a, b = pair.get("materialized"), pair.get("measured")
        if a is None or b is None or abs(a - b) > 1e-9:
            bad.append(f"物化值与实时值不一致: {a} vs {b}")
    return _r(18, "source_provenance", "答案", not bad, bad)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _scan(node, path: str = "") -> list[str]:
    out = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k in FORBIDDEN:
                out.append(f"{path}.{k}" if path else k)
            out += _scan(v, f"{path}.{k}" if path else k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out += _scan(v, f"{path}[{i}]")
    return out


def _r(idx: int, name: str, layer: str, ok: bool, detail) -> dict:
    return {"idx": idx, "name": name, "layer": layer, "ok": bool(ok),
            "detail": detail}


# ---------------------------------------------------------------------------
# 用例：真跑一遍对话，再从结果构造 trace
# ---------------------------------------------------------------------------
def build_trace(out: dict, calls: list[dict]) -> dict:
    parsed = out.get("parsed") or {}
    res = out.get("result") or {}
    mid = res.get("metric_id")
    if mid in model_metrics.MODEL_CATALOG:
        res = {**res, "kind": "scalar", "branch": "model"}
    return {"question": out.get("question") or parsed.get("question"),
            "plan": out.get("plan") or {},
            "tool_args": calls[0]["args"] if calls else {},
            "tool_calls": calls, "answer": out.get("answer") or "",
            "result": res, "parsed": parsed,
            "role": (out.get("findings") or {}).get("role", "risk_analyst"),
            "branch": "model" if mid in model_metrics.MODEL_CATALOG else "aggregate"}


def main() -> int:
    from langgraph.checkpoint.memory import InMemorySaver
    import v2_dialogue as V

    art = Path("artifacts")
    try:                       # 沙箱 shim 对已存在目录仍抛 EEXIST，自行兜住
        art.mkdir(parents=True, exist_ok=True)
    except FileExistsError:
        pass
    db = V.prepare(art / "v25_eval.db", n_population=800)
    eng = V.DialogEngine(db)
    graph = eng.build().compile(checkpointer=InMemorySaver())
    V.set_verbosity(False)

    def run(q: str, sid: str, calls: list[dict]) -> dict:
        out = V.ask(eng, graph, q, sid=sid)
        out["question"] = q
        return build_trace(out, calls)

    print("═" * 78)
    print("语义层评估器 13–18（正向用例 + 反向用例）")
    print("═" * 78)

    # ---- 正向：一个统计问题 ----
    t_stats = run("现金贷近一个月每个环节、每个策略的通过率", "E-ST",
                  [{"name": "compute_metric",
                    "args": {"metric_id": "approval_rate",
                             "params": {"product": "P_CASH_001"}}}])
    # ---- 正向：一个模型级问题 ----
    t_model = run("现金贷近三个月的 KS 是多少", "E-MD",
                  [{"name": "compute_model_metric",
                    "args": {"metric_id": "ks"}}])
    # ---- 正向：物化 vs 实时（同参数）----
    from scheduler import registry as sched
    job = sched.JOBS_BY_ID["model_health_daily_v1"]
    live = model_metrics.compute_model_metric("ks", job["params"], role="system",
                                              db=db)
    mat = next((r["ks_max"] for r in db.conn.execute(
        "SELECT ks_max FROM model_health_daily WHERE job_id=? AND ks_max IS NOT NULL",
        (job["job_id"],)).fetchall()), None)
    pair = {"materialized": mat, "measured": live.get("ks_max")}

    rows = [ev13_no_free_form_metric(t_stats), ev13_no_free_form_metric(t_model),
            ev14_param_domain_validity(t_stats), ev14_param_domain_validity(t_model),
            ev15_caliber_metadata_present(t_stats),
            ev15_caliber_metadata_present(t_model),
            ev16_access_scope_enforced(t_stats),
            ev17_nonaggregate_routing(t_stats), ev17_nonaggregate_routing(t_model),
            ev18_source_provenance(t_model, pair)]
    for r in rows:
        print(f"  {'✓' if r['ok'] else '✗'} [{r['idx']}] {r['name']:<28} "
              f"{r['layer']}  {'' if r['ok'] else r['detail']}")

    # ---- 反向测试：改坏它，必须被拦下 ----
    # 每一项是三元的 (名字, 是否被拦下, 细节)。
    # 用"是否被拦下"而不是"评估器返回了什么"，因为有些坏输入是被运行时
    # 闸门拦下的（guard_call 直接返 NOT_APPLICABLE），根本走不到评估器。
    print("\n反向测试（必须全部被拦下）")
    import re
    rev: list[tuple[str, bool, object]] = []

    bad_plan = dict(t_stats, plan={"steps": ["understand"], "sql": "select 1"})
    r = ev13_no_free_form_metric(bad_plan)
    rev.append(("计划里塞 sql 字段", not r["ok"], r["detail"]))

    bad_ans = t_model["answer"].replace(
        f"【截止】数据截至 {catalog.AS_OF}（T+0 快照）", "")
    r = ev15_caliber_metadata_present(dict(t_model, answer=bad_ans))
    rev.append(("删掉数据截止行", not r["ok"], r["detail"]))

    r = ev17_nonaggregate_routing(
        dict(t_model, tool_calls=[{"name": "compute_metric", "args": {}}]))
    rev.append(("问 KS 却调 compute_metric", not r["ok"], r["detail"]))

    r = ev18_source_provenance(t_model, {"materialized": (mat or 0) + 0.0001,
                                        "measured": live.get("ks_max")})
    rev.append(("物化值与实时值差 1 个基点", not r["ok"], r["detail"]))

    guard = catalog.guard_call("fpd7_rate", {"product": "P_CASH_001",
                                             "window": "近一周"})
    rev.append(("3 天窗口算 FPD7（应返 NOT_APPLICABLE）",
                guard.get("code") == "NOT_APPLICABLE",
                guard.get("code")))

    # 越权：受限角色按渠道看 → 必须 denied，且回答里一个数字都不许有
    eng2 = V.DialogEngine(db, role="analyst_no_channel")
    g2 = eng2.build().compile(checkpointer=InMemorySaver())
    out2 = V.ask(eng2, g2, "现金贷近一个月按渠道看通过率", sid="E-DENY")
    out2["question"] = "现金贷近一个月按渠道看通过率"
    t_deny = build_trace(out2, [])
    d = t_deny["result"]
    denied = d.get("kind") == "denied"
    no_num = not re.search(r"\d+(\.\d+)?%", t_deny["answer"] or "")
    rev.append((f"受限角色按渠道下钻（kind={d.get('kind')}）",
                bool(denied and no_num),
                (d.get("error") or {}).get("code", "未拒绝")))

    schema = semantic_contract.compute_metric_schema()
    schema["parameters"]["properties"]["expression"] = {"type": "string"}
    hits = semantic_contract.scan_free_form(schema)
    rev.append(("schema 里塞 expression", bool(hits), hits))

    ok_all = True
    for name, caught, detail in rev:
        ok_all &= caught
        print(f"  {'✓' if caught else '✗'} {name:<36} → "
              f"{'被拦下' if caught else '漏了 ' + str(detail)}")

    pos = all(r["ok"] for r in rows)
    print("\n" + "═" * 78)
    print(f"正向 {sum(1 for r in rows if r['ok'])}/{len(rows)} 通过；"
          f"反向 {sum(1 for _, c, _ in rev if c)}/{len(rev)} 被拦下")
    print("✅ 全部符合预期" if (pos and ok_all) else "❌ 存在不符合预期的用例")
    db.close()
    return 0 if (pos and ok_all) else 1


if __name__ == "__main__":
    sys.exit(main())
