# -*- coding: utf-8 -*-
"""工具清单与调度（V2.5 §8.6）：11 个工具，全部只读或纯计算。

════════════════════════════════════════════════════════════════════
前 5 个走 MCP，后 6 个走本地 —— 这条分界线不能挪
════════════════════════════════════════════════════════════════════
查目录/查口径（小结果、低延迟、跨系统复用价值高）→ MCP。
取数与重计算（大数据量、不能出域、延迟敏感）→ 本地。
如果把 compute_metric / compute_model_metric 也做成 MCP，等于把"原始申请数据"
和"分数列"送到另一个进程/另一台机器 —— 风控合规上直接过不了。

get_model_health 虽然只读表，也走本地：物化表里存着 base_dist（训练期分布），
那是模型的内部信息，出域同样不合规。

════════════════════════════════════════════════════════════════════
两本册子互斥注册（闸门 8.7 的"分支前置"）
════════════════════════════════════════════════════════════════════
问 KS 时 compute_metric 根本不进 schema；问通过率时 compute_model_metric 不进。
"进了再拒"会让模型反复试探越权/越界参数，把权限和边界都变成一场对话。
"""
from __future__ import annotations

from core import access, catalog, knowledge, model_metrics, semantic_contract
from core.codes import err

from . import compute_metric as _cm
from . import compute_model_metric as _cmm

# name -> (通道, 分支归属, 是否只读)
META: dict[str, tuple[str, str, bool]] = {
    "list_metrics": ("mcp", "both", True),
    "get_metric_definition": ("mcp", "both", True),
    "query_policy_flow": ("mcp", "aggregate", True),
    "lookup_field": ("mcp", "aggregate", True),
    "version_history": ("mcp", "aggregate", True),
    "get_model_health": ("local", "model", True),
    "compute_metric": ("local", "aggregate", True),
    "compute_model_metric": ("local", "model", True),
    "attribute_change": ("local", "aggregate", True),
    "rank_policies": ("local", "aggregate", True),
    "ask_user": ("local", "both", True),
}


# ---------------------------------------------------------------------------
# MCP 侧：目录与元数据（不含一行客户数据）
# ---------------------------------------------------------------------------
def list_metrics(role: str | None = None) -> dict:
    pol = access.policy(role)
    agg = [m for m in catalog.list_metrics()
           if pol.check_metric(m["metric_id"]).get("ok")]
    mod = [{"metric_id": m.metric_id, "name": m.name, "kind": m.kind,
            "label_col": m.label_col, "ref_period": m.ref_period,
            "description": m.description, "version": m.version}
           for m in model_metrics.MODEL_CATALOG.values()
           if pol.check_metric(m.metric_id).get("ok")]
    return {"ok": True, "aggregate": agg, "model": mod}


def get_metric_definition(metric_id: str, role: str | None = None) -> dict:
    if metric_id in model_metrics.MODEL_CATALOG:
        m = model_metrics.MODEL_CATALOG[metric_id]
        return {"ok": True, "kind": "model", "metric_id": metric_id,
                "name": m.name, "score_col": m.score_col,
                "label_col": m.label_col, "ref_period": m.ref_period,
                "description": m.description, "version": m.version,
                "owner": m.owner, "comparable_dimensions":
                    list(m.comparable_dimensions),
                "note": "模型级指标没有分子/分母，不可加，不参与 LMDI 分解"}
    chk = access.policy(role).check_metric(metric_id)
    if not chk.get("ok"):
        return chk
    if metric_id not in catalog.CATALOG:
        return err("NOT_REGISTERED", metric_id=metric_id,
                   registered=sorted(catalog.CATALOG))
    return {"ok": True, "kind": "aggregate", **catalog.contract(metric_id)}


def get_model_health(db, product: str, as_of: str | None = None) -> dict:
    """读物化表。**没有命中就明说"无物化结果"，不回落成实时算。**

    为什么不允许回落：回落会让"报告里的数"和"Agent 的数"来自两条路径，
    而对账时无法区分"口径变了"和"这里用的是另一条计算链"。
    """
    from scheduler import registry
    hit = registry.latest_health(db, product, as_of)
    if not hit:
        return err("NOT_APPLICABLE", product=product, as_of=as_of,
                   reason="该产品还没有物化结果（定时任务未跑或已下线）",
                   hint="可以改用 compute_model_metric 实时算，但会标注 MEASURED")
    rows = hit["rows"]
    return {"ok": True, "as_of": hit["as_of"], "job_id": hit["job_id"],
            "param_version": hit["param_version"], "rows": rows,
            "source": "MATERIALIZED",
            "note": "物化表的参数是冻结的，不接受 bins/window 覆盖"}


def query_policy_flow(product: str, at_date: str | None = None) -> dict:
    return {"ok": True, **knowledge.policy_flow(product, at_date or catalog.AS_OF)}


def lookup_field(name: str) -> dict:
    return {"ok": True, **knowledge.search_field(name)}


def version_history(product: str) -> dict:
    return {"ok": True, "history": knowledge.version_history(product)}


# ---------------------------------------------------------------------------
# 统一调度
# ---------------------------------------------------------------------------
def tools_for(branch: str, role: str | None = None) -> list[dict]:
    """按分支 + 角色裁剪工具集（未授权的指标不在 enum 里）。"""
    return semantic_contract.tools_for(branch, role or access.DEFAULT_ROLE)


# T-107：三个"编排层工具"由对话层实现，通过 bind 注入。
# 为什么用注入而不是让 tools/ import v2_dialogue：
#   架构禁止 llm/tools 反向依赖编排层（docs/architecture.md §2 第一条）。
#   注入让"谁实现"与"谁调度"解耦，同时保住"11 个工具只有一个入口"。
_HANDLERS: dict[str, object] = {}


def bind(name: str, fn) -> None:
    """把一个 handler 绑到工具名上。handler 签名：
        fn(db, args: dict, role: str | None, as_of: str) -> dict
    返回值必须是 dict；需要带登记簿的工具把 book 放在 out["book"]。
    """
    if name not in META:
        raise ValueError(f"不能绑未注册的工具：{name}")
    _HANDLERS[name] = fn


def bound() -> list[str]:
    return sorted(_HANDLERS)


def unbind(name: str) -> None:
    _HANDLERS.pop(name, None)


def _ask_user(db, args: dict, role: str | None, as_of: str) -> dict:
    """默认的 ask_user：只产出澄清请求，不在这里 interrupt。

    为什么不在工具里 interrupt：工具必须可被 MCP 客户端调用，而 interrupt
    是图的状态语义。工具只负责"该问什么"，问的动作留给编排层。
    """
    from core import intent
    parsed = args.get("parsed") or {}
    if not parsed:
        return err("NEED_CLARIFY", slot="intent", reason="没有可澄清的提问单")
    payload = intent.build_clarify(parsed)
    return {"ok": True, "clarify": payload, "tool": "ask_user"}


bind("ask_user", _ask_user)


def call(db, name: str, args: dict | None = None, role: str | None = None,
         as_of: str = catalog.AS_OF) -> dict:
    """**唯一**的工具入口：11 个工具都从这里过。

    统一入口的价值不在于少写 if，而在于：审计、超时、权限、trace
    只需要在这一个地方挂一次 —— 挂了 11 处，一定有一处漏。
    """
    args = dict(args or {})
    if name not in META:
        return err("NOT_REGISTERED", tool=name, known=sorted(META))
    if name == "compute_metric":
        return _cm.run(db, args, role=role, as_of=as_of)
    if name == "compute_model_metric":
        return _cmm.run(db, args, role=role, as_of=as_of)
    if name == "list_metrics":
        return list_metrics(role)
    if name == "get_metric_definition":
        return get_metric_definition(args.get("metric_id"), role)
    if name == "get_model_health":
        return get_model_health(db, args.get("product"), args.get("as_of"))
    if name == "query_policy_flow":
        return query_policy_flow(args.get("product"), args.get("at_date"))
    if name == "lookup_field":
        return lookup_field(args.get("name"))
    if name == "version_history":
        return version_history(args.get("product"))
    fn = _HANDLERS.get(name)
    if fn is not None:
        return fn(db, args, role, as_of)               # type: ignore[operator]
    return err("NOT_APPLICABLE", tool=name,
               reason="该工具需要在编排层 bind 之后才能调用（attribute_change / "
                      "rank_policies 由对话层实现）")
