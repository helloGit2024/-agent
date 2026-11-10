#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D 组门禁：语义一致性（S0.9 T-108）。

════════════════════════════════════════════════════════════════════
D 组和 A/B/C 三组的分工
════════════════════════════════════════════════════════════════════
  A 芯  数学对不对（LMDI 残差、五类判决）
  B 壳  行为对不对（自检、评估器、7 题回归）
  C 静  实现是否唯一（不许有第二套算法）、schema 是否封闭
  D 语义 **两本册子 + 闸门 + 配置 + 工具面是否互相自洽**

D 组治的是一种特别的病：每一处单看都对，合起来矛盾。
  例：`ks_curve` 注册了但 schema 里没有 → 问它得到 NOT_REGISTERED，
      而文档说支持；
  例：`settings.MIN_SAMPLE` 改了但阈值还读定义里的常量 → "我明明设了 50"；
  例：三态分流做了，但 `detect()` 还是两态 → 模糊问句仍被默认口径答掉。

这些都不会让任何单测失败，只会让系统在某一天开始"答得很像但不是"。
跑法：python ci/check_semantic.py   （exit 0 = 全绿）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import (catalog, intent, model_metrics, semantic_contract,  # noqa: E402
                  settings)
from core.db import Database                                          # noqa: E402
from scheduler import promotion                                      # noqa: E402
from scheduler import registry as sched                              # noqa: E402
from tools import registry as tools                                  # noqa: E402

FAILED: list[str] = []
TOTAL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global TOTAL
    TOTAL += 1
    print(f"  {'✓' if cond else '✗'} {name}" + ("" if cond else f"  ← {detail}"))
    if not cond:
        FAILED.append(name)


def main() -> int:
    print("═" * 78)
    print("D 组 · 语义一致性（两本册子 / 闸门 / 配置 / 工具面）")
    print("═" * 78)

    # ---- 1. 第二本册子：9 个指标，其中 2 个曲线类，且都在 schema enum 里 ----
    ids = sorted(model_metrics.MODEL_CATALOG)
    curves = sorted(mid for mid, m in model_metrics.MODEL_CATALOG.items()
                    if m.kind == "curve")
    enum = semantic_contract.compute_model_metric_schema()[
        "parameters"]["properties"]["metric_id"]["enum"]
    check("模型级 9 个指标 / 2 个曲线类，且全部进 schema enum",
          len(ids) == 9 and curves == ["ks_curve", "roc_curve"]
          and set(ids) == set(enum),
          f"{len(ids)} 个 / curves={curves} / enum 差集={set(ids) ^ set(enum)}")

    # ---- 2. 曲线类不许出现"切点"这类可调口径的入参 ----
    params = (semantic_contract.compute_model_metric_schema()["parameters"]
              ["properties"]["params"]["properties"])
    check("计算入参里没有 cut/edge/expression 之类字段（切点只能由函数生成）",
          not any(k in params for k in ("cut", "cuts", "edges", "cut_points",
                                        "expression", "sql"))
          and not semantic_contract.scan_free_form(
              semantic_contract.compute_model_metric_schema()))

    # ---- 3. 闸门 ⓿ 四态：model / ambiguous / aggregate / none ----
    probes = {
        "现金贷近三个月的 KS 是多少": "model",
        "现金贷模型最近表现怎么样": "ambiguous",
        "现金贷近一个月通过率": "aggregate",
        "现金贷当前策略是什么": "none",
    }
    got = {q: model_metrics.detect_state(q)[0] for q in probes}
    bad = {q: (got[q], want) for q, want in probes.items() if got[q] != want}
    check("闸门 ⓿ 四态判定可复现（model / ambiguous / aggregate / none）",
          not bad, str(bad))

    # ---- 4. 三态必须落到"反问"，不是"默认" ----
    amb = intent.parse("现金贷模型最近表现怎么样")
    check("ambiguous → missing=['branch']，且澄清项里有 branch 选项",
          amb["missing"] == ["branch"]
          and intent.build_clarify(amb)["slot"] == "branch"
          and len(intent.build_clarify(amb)["options"]) == 2,
          str(amb["missing"]))

    # ---- 5. 配置生效：阈值读 settings，不读定义里的设计值 ----
    check("MIN_SAMPLE 由 settings 生效（定义里的 50 只是设计值）",
          model_metrics.effective_min_sample() == settings.MIN_SAMPLE
          and all(m.min_sample == model_metrics.DESIGN_MIN_SAMPLE
                  for m in model_metrics.MODEL_CATALOG.values()),
          f"effective={model_metrics.effective_min_sample()} "
          f"settings={settings.MIN_SAMPLE}")

    # ---- 6. 两本册子的定义哈希都可校验 ----
    v = semantic_contract.verify()
    check("两本册子定义哈希 + schema 封闭 + 必备字段（verify 全绿）",
          v["ok"] and v["hash"]["ok"] and v["model_hash"]["ok"]
          and not v["missing_fields"], str(v)[:200])

    # ---- 7. 工具面：11 个工具，只有一个入口，且都能返回 dict ----
    db = Database(":memory:")
    keys = sorted(tools.META)
    # 前 8 个是数据/元数据工具，空参也必须给**结构化错误**而不是 KeyError
    data_tools = [k for k in keys
                  if k not in ("attribute_change", "rank_policies", "ask_user")]
    outs = {}
    for name in data_tools:
        try:
            out = tools.call(db, name, {})
            outs[name] = isinstance(out, dict) and bool(out.get("code") or out.get("ok"))
        except Exception as e:                              # noqa: BLE001
            outs[name] = f"{type(e).__name__}: {e}"
    check(f"{len(data_tools)} 个数据/元数据工具空参也返回结构化结果（不抛异常）",
          all(v is True for v in outs.values()),
          str({k: v for k, v in outs.items() if v is not True}))
    # 另外 3 个由编排层实现：**未 bind 时**必须是 NOT_APPLICABLE，
    # bind 之后才可调用 —— 这条同时验证"注入"是真的生效，不是写在文档里
    unbound = tools.call(db, "rank_policies", {})
    from v2_dialogue import DialogEngine                  # 只在 init 里 bind，不碰数据
    DialogEngine(db)
    check("编排层工具未 bind 时拒绝调用、bind 后齐备（注入生效）",
          unbound.get("code") == "NOT_APPLICABLE"
          and {"ask_user", "rank_policies", "attribute_change"} <= set(tools.bound()),
          f"unbound={unbound.get('code')} bound={tools.bound()}")

    # ---- 8. 分源进签名：同名指标不同分源不能互相命中 ----
    p = {"product": "P_CASH_001", "window": "近三个月", "bins": 5, "label": "fpd30"}
    s1 = model_metrics.signature("ks", dict(p, score_source="model"))
    s2 = model_metrics.signature("ks", dict(p, score_source="policy"))
    check("score_source 进签名（模型分与策略分的结果不会互相命中物化）",
          s1 != s2 and model_metrics.SCORE_SOURCES == ("model", "policy"))

    # ---- 9. 升格队列：计数到阈值入队；下线后不再命中；窗口外要重新数 ----
    promo_ok = False
    try:
        for _ in range(settings.PROMOTE_AFTER):
            st = promotion.record_question(db, "ks", p)
        promo_ok = st["should_promote"] and any(
            r["signature"] == st["signature"] for r in promotion.queue(db))
        # 时间窗：连续问 5 次永远落在同一秒内，所以**不把 last_seen 拨出去，
        # 窗口写没写都看不出来**。拨到 60 天前后这一问必须从 1 重新数。
        db.conn.execute(
            "UPDATE promotion_queue SET last_seen='2000-01-01T00:00:00', hits=?"
            " WHERE signature=?", (settings.PROMOTE_AFTER, st["signature"]))
        db.conn.commit()
        reset = promotion.record_question(db, "ks", p)
        promo_ok = (promo_ok and reset["hits"] == 1 and not reset["should_promote"]
                    and settings.PROMOTE_WINDOW_DAYS == reset["window_days"])
        job = sched.JOBS_BY_ID["model_health_daily_v1"]
        promotion.retire_job(db, job["job_id"], "gate")
        promo_ok = promo_ok and sched.run_job(db, job["job_id"]).get("code") == "JOB_RETIRED"
        promotion.activate_job(db, job["job_id"], "gate 复原")
    finally:
        db.close()
    check(f"同参问 {settings.PROMOTE_AFTER} 次入队（窗口外重新数）；job 下线后不再执行",
          promo_ok)

    # ---- 10. 曲线外卸 × 出口闸门：ref_id 不是数字断言 ----
    # 这条治的是"两处都对、合起来错"：曲线外卸没问题，ref_id 带日期也没问题，
    # 但出口闸门会把 ref_20261109_7909 里的 20261109 当成未登记数字 ——
    # 于是**每一条曲线回答**都报"数字溯源 ✘"。校验器对一个必答问题必然误报，
    # 它就再也没人看了。所以这里两个方向都要钉住：
    #   带 ref_id 的正文 → 放行；真正的野数字 → 仍要拦。
    from v2_dialogue import verify_numbers                 # noqa: PLC0415
    ok_ref = verify_numbers("逐点曲线已外卸：ref_20261109_7909", {}).get("ok")
    fake = verify_numbers("模型 KS 是 37，比上期好", {})
    check("出口闸门：ref_id 里的日期放行，真幻觉数字仍被拦",
          ok_ref is True and fake.get("ok") is False and "37" in fake.get("stray", []),
          f"ref_ok={ok_ref} fake={fake}")

    print("\n" + "═" * 78)
    if FAILED:
        print(f"❌ D 组失败 {len(FAILED)}/{TOTAL} 项：{', '.join(FAILED)}")
        return 1
    print(f"✅ D 组语义一致性全绿（{TOTAL} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
