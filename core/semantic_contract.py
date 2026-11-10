# -*- coding: utf-8 -*-
"""契约导出（V2.5 §7.13）：把语义层变成 LLM 能看见的 schema。

════════════════════════════════════════════════════════════════════
两条硬规则
════════════════════════════════════════════════════════════════════
① **入参里没有任何"怎么算"的字段**。
   compute_metric 只有 metric_id + params{product, window, dimension}；
   出现 expression / sql / formula / column / table 任一键，语义层就没封闭，
   模型立刻会学会自己拼一个"看起来对"的公式。
   （判断标准：模型无法调用它没有的能力 —— 这一条比任何 prompt 都管用。）

② **未授权的指标根本不在 enum 里**。
   权限的最高实现是"看不见"，不是"看得见但被拒绝"。
   "进了再拒"会让模型反复试探越权参数，把权限变成一场对话。

════════════════════════════════════════════════════════════════════
为什么还要"定义哈希 → 启动自检"
════════════════════════════════════════════════════════════════════
最隐蔽的一种污染：metric_id 不变、定义变了、版本号没变。
所有历史对比同时失效，而没有任何一处"应该报错"的地方。
"""
from __future__ import annotations

from . import access, catalog, knowledge, model_metrics

FORBIDDEN_KEYS = {"sql", "expression", "formula", "table", "column", "where",
                  "select", "join"}


def _allowed(meta: dict, role: str) -> list[str]:
    """该角色能看见的指标（看不见的连 enum 都不进）。"""
    pol = access.policy(role)
    out = []
    for mid in meta:
        if pol.metrics is not None and mid not in pol.metrics:
            continue
        m = meta[mid]
        if getattr(m, "access_policy", ()) and pol.role not in m.access_policy:
            continue
        out.append(mid)
    return sorted(out)


def compute_metric_schema(role: str = access.DEFAULT_ROLE) -> dict:
    """可聚合指标的 tool schema（唯一计算入口之一）。"""
    pol = access.policy(role)
    dims: dict[str, list[str]] = {}
    for mid in _allowed(catalog.CATALOG, role):
        m = catalog.CATALOG[mid]
        dims[mid] = ["overall"] + [d for d in m.dimensions
                                   if pol.check_dimension(mid, d).get("ok")]
    return {
        "name": "compute_metric",
        "description": ("按已注册口径计算可聚合指标（通过率/拒绝率/FPD 等）。"
                        "只能选择指标与范围，不能自定义公式。"),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["metric_id", "params"],
            "properties": {
                "metric_id": {"type": "string", "enum": sorted(dims)},
                "params": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["product"],
                    "properties": {
                        "product": {"type": "string",
                                    "enum": sorted(knowledge.PRODUCTS)},
                        "window": {"type": "string",
                                   "description": "自然语言窗口，如 近一个月 / 2026-05"},
                        "dimension": {"type": "string",
                                      "enum": sorted({d for v in dims.values() for d in v})},
                    },
                },
            },
        },
        "_allowed_dimensions": dims,
    }


def compute_model_metric_schema(role: str = access.DEFAULT_ROLE) -> dict:
    """模型级指标的 tool schema（另一条分支，与上面互斥注册）。"""
    ids = _allowed(model_metrics.MODEL_CATALOG, role)
    return {
        "name": "compute_model_metric",
        "description": ("计算模型级指标（KS/AUC/Gini/IV/PSI/LIFT/WOE）。"
                        "分数分布统计，不可加，不参与 LMDI 分解。"),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["metric_id", "params"],
            "properties": {
                "metric_id": {"type": "string", "enum": ids},
                "params": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["product"],
                    "properties": {
                        "product": {"type": "string",
                                    "enum": sorted(knowledge.PRODUCTS)},
                        "window": {"type": "string"},
                        "dimension": {"type": "string",
                                      "enum": list(model_metrics.DIMENSIONS)},
                        # 只收箱数，不收切点：切点是口径
                        "bins": {"type": "integer",
                                 "enum": list(model_metrics.BINS_ALLOWED)},
                        "label": {"type": "string",
                                  "enum": sorted(model_metrics.LABELS)},
                        "ref_period": {"type": "string",
                                       "enum": sorted(model_metrics.REF_PERIODS)},
                        # T-102：分源只收 2 个值。模型分与策略分是两份数据，
                        # 混着报会出现"两边 KS 差不多"的假一致。
                        "score_source": {"type": "string",
                                         "enum": list(model_metrics.SCORE_SOURCES)},
                    },
                },
            },
        },
    }


def catalog_schemas(role: str = access.DEFAULT_ROLE) -> list[dict]:
    return [
        {"name": "list_metrics", "parameters": {"type": "object",
                                                "additionalProperties": False,
                                                "properties": {}}},
        {"name": "get_metric_definition",
         "parameters": {"type": "object", "additionalProperties": False,
                        "required": ["metric_id"],
                        "properties": {"metric_id": {
                            "type": "string",
                            "enum": _allowed(catalog.CATALOG, role)
                            + _allowed(model_metrics.MODEL_CATALOG, role)}}}},
        {"name": "get_model_health",
         "parameters": {"type": "object", "additionalProperties": False,
                        "required": ["product"],
                        "properties": {"product": {"type": "string"},
                                       "as_of": {"type": "string"}}}},
    ]


def tools_for(branch: str, role: str = access.DEFAULT_ROLE) -> list[dict]:
    """按分支裁剪工具集 —— 两本册子**互斥**注册。

    branch="model" 时 compute_metric 根本不出现；反之亦然。
    实测过的失败正是"用一个能调通的工具回答另一个问题"：
    问 KS 答通过率，数字对、可溯源、还带免责声明。
    """
    common = catalog_schemas(role)
    if branch == "model":
        return [compute_model_metric_schema(role)] + common
    if branch == "aggregate":
        return [compute_metric_schema(role)] + common
    return [compute_metric_schema(role), compute_model_metric_schema(role)] + common


def scan_free_form(schema: dict) -> list[str]:
    """扫 schema 里有没有"怎么算"的键（门禁用，返回违规键）。"""
    found: list[str] = []

    def walk(node, path: str = ""):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in FORBIDDEN_KEYS:
                    found.append(f"{path}.{k}" if path else k)
                walk(v, f"{path}.{k}" if path else k)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(schema)
    return found


def export(role: str = access.DEFAULT_ROLE) -> dict:
    """给 LLM 的完整契约（两册 + 目录工具），并做一次自检。"""
    agg = compute_metric_schema(role)
    mod = compute_model_metric_schema(role)
    bad = scan_free_form(agg) + scan_free_form(mod)
    return {"role": role, "schemas": tools_for("both", role),
            "free_form_violations": bad, "ok": not bad}


def verify() -> dict:
    """启动自检：定义哈希（两本册子都查）+ schema 封闭 + 每指标必备字段。"""
    h = catalog.verify_definitions(strict=False)
    hm = model_metrics.verify_definitions(strict=False)
    missing = []
    for mid, m in catalog.CATALOG.items():
        for f in ("description", "typical_questions", "requires_params"):
            if not getattr(m, f):
                missing.append(f"{mid}.{f}")
    for mid, m in model_metrics.MODEL_CATALOG.items():
        for f in ("description", "owner", "version"):
            if not getattr(m, f):
                missing.append(f"model:{mid}.{f}")
    return {"ok": h["ok"] and hm["ok"] and not missing,
            "hash": h, "model_hash": hm, "missing_fields": missing,
            "schema": export()["ok"]}
