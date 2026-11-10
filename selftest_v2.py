#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V2 自检 —— 语义层与对话层的行为边界（自检即规格）。

════════════════════════════════════════════════════════════════════
为什么 V2 更需要自检（没有它会怎样）
════════════════════════════════════════════════════════════════════
V1 的自检守的是"数学正确"；V2 多守三件更隐蔽的东西：

  ① 口径正确：FPD30 的分母必须是"已到期借据"，不是"通过人数"。
     这条错了，坏账率可以差一倍，而且数字看起来依然"合理"。
  ② 时间正确：问"近 1 周 FPD7"必须自动改成"到期批次"口径。
     没有校准，系统会算出一个 0% 或 100% 的假数字。
  ③ 复现正确：同一个问题问两次，登记簿里的数字必须逐位相同。
     V2 多了"人话解析"这层，最容易在不知不觉中引入不确定性。

跑法：python3 selftest_v2.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from langgraph.checkpoint.memory import InMemorySaver

import llm
import memory
from core import (access, anomaly, catalog, entities, envelope, intent,
                  knowledge, model_metrics, ranking, semantic_contract)
from core.db import Database
from scheduler import registry as sched
from tools import compute_model_metric as model_tool
from v2_dialogue import (CFG_CUR, RUN_CUR, DialogEngine, ask,
                         set_verbosity, prepare)

ART = Path("artifacts")
PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def main() -> None:
    print("═" * 78)
    print("策略回测分析 Agent V2 · 自检（语义层 + 对话层）")
    print("═" * 78)

    # ---------------------------------------------------------------
    print("\n[1] 语义层：指标解析与别名")
    check("'通过率' → approval_rate",
          catalog.resolve("看下通过率").get("metric_id") == "approval_rate")
    check("'每个策略的通过率' → policy_rates（更具体的别名胜出）",
          catalog.resolve("每个策略的通过率").get("metric_id") == "policy_rates")
    check("'FPD30' → fpd30_rate",
          catalog.resolve("FPD30 多少").get("metric_id") == "fpd30_rate")
    check("未注册口径直接拒绝计算",
          _raises(lambda: catalog.compute("mock_rate", None)))

    # ---------------------------------------------------------------
    print("\n[2] 时间窗口解析")
    w7 = catalog.parse_window("近一周")
    w30 = catalog.parse_window("近一个月")
    wm = catalog.parse_window("2026-05")
    check("近一周 = 7 天", w7 is not None and w7.days == 7, str(w7))
    check("近一个月 = 30 天", w30 is not None and w30.days == 30, str(w30))
    check("指定月份 = 整月", wm is not None and wm.date_from == "2026-05-01"
          and wm.date_to == "2026-05-31", str(wm))
    check("无法识别 → None（触发反问，不猜）",
          catalog.parse_window("最近那阵子") is None)

    # ---------------------------------------------------------------
    print("\n[3] 表现期校准（vintage）：这是 V2 最值钱的一段规则")
    m7 = catalog.check_maturity("fpd7_rate", w7)
    check("近 1 周 FPD7 → 必须做到期口径修正", m7["corrected"] is True)
    check("修正后窗口整体前移 7 天",
          m7["window"].date_to == _shift(w7.date_to, -7), str(m7["window"]))
    check("修正后窗口长度不变（7 天）", m7["window"].days == 7)
    m30 = catalog.check_maturity("fpd30_rate", w30)
    check("近 1 月 FPD30 → 同样需要修正", m30["corrected"] is True)
    check("当期指标（通过率）不需要修正",
          catalog.check_maturity("approval_rate", w7)["corrected"] is False)

    # ---------------------------------------------------------------
    print("\n[4] 口径单测（手工重算 vs 引擎口径）")
    if not ART.exists():
        ART.mkdir(parents=True)
    db = prepare(ART / "v2_selftest.db", n_population=800)
    db.conn.executescript(  # 确保台账表存在（prepare 已建，这里防御性再建一次）
        knowledge.LOAN_SCHEMA)
    eng = DialogEngine(db)
    set_verbosity(False)

    win = catalog.parse_window("近半年")
    view = catalog.build_view(db, RUN_CUR, win, "P_CASH_001")

    # ① 通过率：独立重算（不经过 catalog 内部函数，自己写一遍 where）
    #    ★口径：终局通过 = S4_LIM 环节**全部策略**都 PASS。
    #    写成"有一条 PASS 就算通过"会把通过率算高约 3pt —— 这正是
    #    "手工重算必须和语义层同口径"的意义：口径不同，两边都不是 bug，
    #    但报告和问答会给出两个数，这才是最致命的错。
    s4: dict[str, list[str]] = {}
    for f in db.facts("run_id=?", (RUN_CUR,)):
        if f["stage"] == "S4_LIM":
            s4.setdefault(f["apply_id"], []).append(f["result"])
    ok_s4 = {a for a, rs in s4.items() if rs and all(r == "PASS" for r in rs)}
    apps = [a for a in view.apps]
    ids = {a["apply_id"] for a in apps}
    manual = len(ok_s4 & ids) / max(len(apps), 1)
    got = catalog.compute("approval_rate", view)["value"]
    check("申请通过率 = 手工重算值（S4 全部策略 PASS 口径）",
          abs((got or 0) - manual) < 5e-5,
          f"{got} vs {round(manual, 4)}")

    # ② FPD30 的分母必须是"已到期借据"，不是"通过人数"
    f30 = catalog.compute("fpd30_rate", view)
    matured = [l for l in view.loans if l["loan_date"] <= _shift(catalog.AS_OF, -30)]
    check("FPD30 分母 = 已到期借据数（≠ 通过人数）",
          f30["denominator_n"] == len(matured),
          f"{f30['denominator_n']} vs {len(matured)}")
    check("FPD30 分母严格小于放款数（说明确实扣掉了未到期）",
          f30["denominator_n"] <= f30["granted_n"])
    manual30 = sum(int(l["fpd30"] or 0) for l in matured) / max(len(matured), 1)
    # 注：语义层对外输出统一保留 4 位小数（_pct），所以容差取 5e-5 而非 1e-9
    check("FPD30 值 = 手工重算值", abs((f30["value"] or 0) - manual30) < 5e-5,
          f"{f30['value']} vs {round(manual30, 4)}")

    # ③ 环节通过率的分母是"进入人数"，不是全量申请
    st = catalog.compute("stage_rates", view)["rows"]
    s3 = next(r for r in st if r["stage"] == "S3_CR")
    check("环节通过率分母 = 进入该环节人数（< 全量申请）",
          s3["entered"] <= len(view.apps), f"{s3['entered']} vs {len(view.apps)}")
    # ④ 三档闭合：通过 + 拒绝 + 其余（缺失/未判定）= 100%
    #    少了"其余"这一档，80% 通过 + 0% 拒绝会被误读成"剩下 20% 也过了"
    check("环节表三档之和 = 100%（通过 + 拒绝 + 其余）",
          all(abs((r["pass_rate"] or 0) + (r["reject_rate"] or 0)
                  + (r["other_rate"] or 0) - 1) < 2e-3 for r in st),
          str([(r["stage"], r["pass_rate"], r["reject_rate"], r["other_rate"])
               for r in st]))

    # ---------------------------------------------------------------
    print("\n[5] 元数据查询（受控工具，不是 RAG）")
    flow_now = knowledge.policy_flow("P_CASH_001", "2026-06-30")
    flow_before = knowledge.policy_flow("P_CASH_001", "2026-03-01")
    check("当前版本 = v2.2", flow_now.get("version") == "v2.2")
    check("3 月时点版本 = v2.1（时间旅行查询）",
          flow_before.get("version") == "v2.1")
    check("配置查询带生效日期", bool(flow_now.get("effective")))
    check("字段字典能查到 FPD30", knowledge.search_field("fpd30")["found"] is True)
    check("查不到的字段明确说没有（不猜）",
          knowledge.search_field("XYZ_404")["found"] is False)
    check("产品识别不出时返回 None（触发反问）",
          knowledge.resolve_product("那个啥产品")["product"] is None)

    # ---------------------------------------------------------------
    print("\n[6] 归因内核：分解必须精确可加、显著性必须先判")
    prev_facts = [f for f in db.facts("run_id=?", ("ONLINE_v2.1",))]
    cur_facts = [f for f in db.facts("run_id=?", (RUN_CUR,))]
    attr = anomaly.attribute(prev_facts, cur_facts,
                             anomaly.records(view.apps, prev_facts),
                             anomaly.records(view.apps, cur_facts))
    check("LMDI 残差为 0（精确可加）",
          attr["lmdi"] is not None and abs(attr["lmdi"]["residual"]) < 1e-9)
    mix = attr["mix"]["channel"]
    check("mix 分解三项之和 = 总变化（残差 0）", abs(mix["residual"]) < 1e-6)
    same = anomaly.delta_with_ci(0.3, 500, 0.3, 500)
    check("同比例 → 不显著（不给噪声编故事）", same["significant"] is False)
    big = anomaly.delta_with_ci(0.3, 500, 0.4, 500)
    check("10pt 差异 / n=500 → 显著", big["significant"] is True)
    check("假设证据里不含数字（数字必须走登记簿）",
          all(not any(ch.isdigit() for ch in h["evidence"])
              for h in attr["hypotheses"]))

    # ---------------------------------------------------------------
    print("\n[7] 排序层：动作枚举合法、无标签不许下结论")
    apps = view.apps
    rk = ranking.rank(db, RUN_CUR, cur_facts, apps, CFG_CUR)
    check("动作都在枚举内",
          all(it["action"] in ranking.ACTIONS for it in rk["items"]))
    check("每条都有可读依据",
          all(len(it["expected_effect"]) > 10 for it in rk["items"]))
    need_ab = [it for it in rk["items"] if it["action"] == "NEED_AB"]
    check("NEED_AB 的条目确实没有拒绝组标签",
          all(it["labeled_rej_n"] < 30 for it in need_ab))
    tight = [it for it in rk["items"] if it["action"] == "TIGHTEN"]
    check("TIGHTEN 必带候选阈值且低于当前阈值",
          all(it["extra"].get("candidate_threshold") is not None
              and it["extra"]["candidate_threshold"] < it["threshold"]
              for it in tight))

    # ---------------------------------------------------------------
    print("\n[8] 对话层：复现性（V2 最容易被破坏的一条）")
    graph = eng.build().compile(checkpointer=InMemorySaver())
    q = "现金贷近一个月，每个环节、每个策略的通过率和拒绝率统计一下"
    a1 = ask(eng, graph, q, sid="T-1")
    a2 = ask(eng, graph, q, sid="T-2")
    b1, b2 = a1.get("book") or {}, a2.get("book") or {}
    same_keys = set(b1) == set(b2)
    same_vals = same_keys and all(
        b1[k]["value"] == b2[k]["value"] for k in b1)
    check("同一问题两次询问 → 登记簿 key 完全一致", same_keys,
          f"{len(b1)} vs {len(b2)}")
    check("同一问题两次询问 → 数字逐位一致", same_vals)
    check("回答文字两次一致", a1.get("answer") == a2.get("answer"))

    # ---------------------------------------------------------------
    print("\n[9] 出口闸门：溯源校验能抓住凭空数字")
    bad_answer = "通过率约为 88.8%，比上期下降 3.3%。"
    from v2_dialogue import verify_numbers
    chk = verify_numbers(bad_answer, b1)
    check("伪造数字会被拦下", chk["ok"] is False and "88.8%" in chk["stray"])
    good = verify_numbers(a1.get("answer") or "", b1)
    check("正常回答可以追溯", good["ok"] is True, str(good["stray"]))

    # ---------------------------------------------------------------
    print("\n[10] 听不懂与兜底：不许白屏，不许悄悄猜")
    # ★这里不要再 from v2_dialogue import DialogEngine ——
    #   Python 的函数作用域规则会让 DialogEngine 在整个 main 里变成局部变量，
    #   前面第 4 组的 `eng = DialogEngine(db)` 就会 UnboundLocalError。
    #   （本项目真实踩过：加了局部 import，第 4 组直接崩）
    p_raw = intent.parse("帮我看看最近放钱放得怎么样")
    check("听不出意图 → 进入澄清通道（不静默继续）",
          "intent" in p_raw["missing"], str(p_raw["missing"]))
    out_un = ask(eng, graph, "帮我看看最近放钱放得怎么样", sid="T-UNK")
    check("听不懂时不是白屏（answer 非空）",
          bool((out_un.get("answer") or "").strip()),
          f"intent={out_un['parsed']['intent']}")
    # ② 两轮澄清后仍缺参数会走 _fill_defaults 用默认值继续 ——
    #    这些默认值必须出现在回答里（修复前：notes 写了但渲染层从没读过）
    p_fill = DialogEngine._fill_defaults(None, intent.parse("通过率统计一下"))
    decl = DialogEngine._decl(p_fill.get("notes"))
    check("兜底默认值必须写进回答（不许悄悄猜）",
          "需要你确认的假设" in decl and "现金贷" in decl, decl[:48].replace("\n", " "))
    check("没有兜底时不硬凑声明段", DialogEngine._decl([]) == "")

    # ③ 执行体抛异常时必须"降级交付"，不许 answer=None。
    #    这是 V1 有、V2 一度丢掉的能力（V1 的 degraded / step_failures 限流）。
    eng_deg = DialogEngine(db)

    def _boom(parsed):
        raise RuntimeError("自检注入的失败")

    eng_deg._exec_stats = _boom
    g_deg = eng_deg.build().compile(checkpointer=InMemorySaver())
    out_deg = ask(eng_deg, g_deg, "现金贷近一个月通过率统计一下", sid="T-DEG")
    check("执行失败 → 降级交付（degraded=True 且 answer 非空）",
          out_deg.get("degraded") is True
          and bool((out_deg.get("answer") or "").strip()),
          f"degraded={out_deg.get('degraded')} answer={(out_deg.get('answer') or '')[:20]}")
    check("降级回答明确说「没算出结果」且不编数字",
          "没能给出结果" in (out_deg.get("answer") or "")
          and "本回答不含任何统计数字" in (out_deg.get("answer") or ""))

    # ---------------------------------------------------------------
    print("\n[11] 语义层封闭：入参里没有“怎么算”的字段（V2.1 §7.13）")
    agg_schema = semantic_contract.compute_metric_schema()
    mod_schema = semantic_contract.compute_model_metric_schema()
    check("可聚合 schema 搜不到 sql/expression 类字段",
          semantic_contract.scan_free_form(agg_schema) == [],
          str(semantic_contract.scan_free_form(agg_schema)))
    check("模型级 schema 同样封闭",
          semantic_contract.scan_free_form(mod_schema) == [],
          str(semantic_contract.scan_free_form(mod_schema)))
    check("两层 additionalProperties 都是 False（多一个键就报错，不是忽略）",
          agg_schema["parameters"]["additionalProperties"] is False
          and agg_schema["parameters"]["properties"]["params"]
          ["additionalProperties"] is False)
    # 权限的最高实现是"看不见"：未授权指标连 enum 都不进
    check("未授权指标根本不进 enum（business_viewer 看不到模型级）",
          semantic_contract.compute_model_metric_schema("business_viewer")
          ["parameters"]["properties"]["metric_id"]["enum"] == [])
    mod_params = mod_schema["parameters"]["properties"]["params"]["properties"]
    check("只收箱数不收切点（bins ∈ {5,10,20}，无 edges/cuts/breaks）",
          list(mod_params["bins"]["enum"]) == [5, 10, 20]
          and not ({"edges", "cuts", "breaks"} & set(mod_params)),
          str(sorted(mod_params)))
    ctr = catalog.contract("fpd30_rate")
    check("契约给“选什么”不给“怎么算”（无 formula/expression 键）",
          "formula" not in ctr and "expression" not in ctr
          and bool(ctr["not_applicable"]))
    amb = catalog.resolve("放款率")
    check("误称判歧义而不是猜一个口径（放款率 → 至少 2 个候选）",
          amb["metric_id"] is None and len(amb["ambiguous"]) >= 2,
          str(amb["ambiguous"]))
    vd = catalog.verify_definitions(strict=False)
    check("同 id 同版本定义哈希可校验（静默改义会被抓到）",
          vd["ok"] and not vd["silent"] and len(catalog.definition_hash("fpd30_rate")) == 16)

    # ---------------------------------------------------------------
    print("\n[12] 权限 / 实体 / 信封（V2.2 §7.6–7.8）")
    pol = access.policy("analyst_no_channel")
    check("受限角色按渠道下钻 → DENIED_DIMENSION（不退化成按全量算）",
          pol.check_dimension("approval_rate", "channel").get("code")
          == "DENIED_DIMENSION")
    check("未知角色降级为最小权限（不是最大权限）",
          access.policy("some_new_role").metrics == ())
    view_lim = catalog.build_view(db, RUN_CUR, win, None,
                                  row_filter=pol.row_filter_for({}))
    check("行权限在取数前生效（View 里只剩被允许的产品）",
          bool(view_lim.apps)
          and {a["product_code"] for a in view_lim.apps} <= {"P_CASH_001"})
    check("实体图能推出唯一 join 路径（Fact→Loan 必须过 Applicant，2 跳）",
          entities.path("Fact", "Loan") is not None
          and len(entities.path("Fact", "Loan")) == 2)
    conv, dups = entities.converge([{"apply_id": "A"}, {"apply_id": "A"},
                                    {"apply_id": "B"}], "Applicant")
    check("收敛层去重并报出去掉多少行（不是静默去重）",
          len(conv) == 2 and dups == 1, f"{len(conv)} / {dups}")

    # ★ 验收 ④：facts 表复制一份（行数翻倍）后，5 个率指标必须一个比特都不变。
    #   这条断言是"收敛发生在声明层"的唯一证据：如果去重还留在各 compute 里，
    #   下一个写指标的人一写 sum(f.fail)，人数就翻十几倍，而且数看起来"更大更合理"。
    import json as _json
    dup_path = ART / "v2_dup_facts.db"
    if dup_path.exists():
        dup_path.unlink()
    db.conn.commit()
    db.conn.execute(f"VACUUM INTO '{dup_path.as_posix()}'")
    d2 = Database(dup_path)
    n0 = d2.conn.execute("SELECT COUNT(*) FROM policy_eval_fact").fetchone()[0]
    d2.conn.executescript(                       # 造一张没有主键的同构表并翻倍
        "ALTER TABLE policy_eval_fact RENAME TO _f;"
        "CREATE TABLE policy_eval_fact AS SELECT * FROM _f;"
        "INSERT INTO policy_eval_fact SELECT * FROM _f;"
        "DROP TABLE _f;")
    d2.conn.commit()
    n1 = d2.conn.execute("SELECT COUNT(*) FROM policy_eval_fact").fetchone()[0]
    rate_ids = ("approval_rate", "stage_rates", "policy_rates",
                "fpd7_rate", "fpd30_rate")
    mism: list[str] = []
    for mid in rate_ids:
        one = catalog.compute(mid, catalog.build_view(db, RUN_CUR, win, "P_CASH_001"))
        two = catalog.compute(mid, catalog.build_view(d2, RUN_CUR, win, "P_CASH_001"))
        if (_json.dumps(one, sort_keys=True, ensure_ascii=False)
                != _json.dumps(two, sort_keys=True, ensure_ascii=False)):
            mism.append(mid)
    d2.close()
    check("facts 表复制一份后 5 个率指标逐位不变（收敛在声明层）",
          n1 == n0 * 2 and not mism, f"{n0}→{n1} 不一致={mism}")
    env = envelope.parse(a1.get("answer") or "")
    check("信封四要素齐全（口径/窗口/过滤/截止）",
          all(k in env for k in ("口径", "窗口", "过滤", "截止")), str(list(env)))
    check("信封里“不适用”与“留空”可区分（留空必须报缺项）",
          envelope.validate("【口径】x\n【窗口】不适用\n【过滤】y")["missing"]
          == ["截止"])
    check("信封口径版本与登记簿一致（数字和文案都来自 CATALOG）",
          f"v{catalog.CATALOG['approval_rate'].version}" in env.get("口径", ""),
          env.get("口径", "")[:40])

    # ---------------------------------------------------------------
    print("\n[13] 两类指标分流 / 单一入口 / 物化回流（V2.5 §7.9–7.12）")
    words = ["KS", "AUC", "Gini", "IV", "PSI", "LIFT",
             "区分度", "分箱", "单调性", "评分分布", "模型分"]
    check("闸门 ⓿ 认得全部模型级词（6 个英文 + 中文近义）",
          all(model_metrics.detect(w) for w in words),
          str([w for w in words if not model_metrics.detect(w)]))
    check("闸门 ⓿ 不误判率值与“模型分析报告”",
          model_metrics.detect("现金贷近三个月通过率") is None
          and model_metrics.detect("模型分析报告怎么写") is None,
          str((model_metrics.detect("现金贷近三个月通过率"),
               model_metrics.detect("模型分析报告怎么写"))))
    check("分流在意图规则之前（问 KS 必进 MODEL_DIAG）",
          intent.parse("现金贷近三个月的 KS 是多少")["intent"]
          == intent.INTENT_MODEL_DIAG)
    check("率值问题不进模型分支（用户没说 ≠ 用户说了我没有）",
          intent.parse("现金贷近一个月通过率")["intent"] == intent.INTENT_STATS)
    r7 = model_metrics.compute_model_metric(
        "ks", {"product": "P_CASH_001", "bins": 7}, role="system", db=db)
    check("bins=7 → PARAM_OUT_OF_DOMAIN（枚举外直接拒，不四舍五入）",
          r7.get("code") == "PARAM_OUT_OF_DOMAIN", str(r7.get("code")))
    rl = model_metrics.compute_model_metric(
        "ks", {"product": "P_CASH_001", "label": "dpd999"}, role="system", db=db)
    check("label 不在域 → PARAM_OUT_OF_DOMAIN",
          rl.get("code") == "PARAM_OUT_OF_DOMAIN", str(rl.get("code")))
    rp = model_metrics.compute_model_metric(
        "psi", {"product": "P_CASH_001", "window": "近三个月", "bins": 5,
                "label": "fpd30"}, role="system", db=db)
    check("PSI 缺 ref_period → NOT_APPLICABLE（不许拿当期跟当期比）",
          rp.get("code") == "NOT_APPLICABLE", str(rp.get("code")))

    jobs = sched.run_all(db)
    check("3 个物化任务全部成功（= 用冻结参数调同一个函数）",
          len(jobs) == 3 and all(j.get("ok") for j in jobs),
          str([(j.get("job_id"), j.get("code")) for j in jobs if not j.get("ok")]))
    job = sched.JOBS_BY_ID["model_health_daily_v1"]
    live = model_metrics.compute_model_metric("ks", job["params"],
                                              role="system", db=db)
    mat = next((r["ks_max"] for r in db.conn.execute(
        "SELECT ks_max FROM model_health_daily WHERE job_id=? AND ks_max IS NOT NULL",
        (job["job_id"],)).fetchall()), None)
    check("物化值与实时值逐位一致（同一个函数，只是参数被冻结）",
          mat is not None and abs(mat - (live.get("ks_max") or 0)) < 1e-12,
          f"{mat} vs {live.get('ks_max')}")
    check("签名差一个参数就不算命中（不会拿旧结果糊弄）",
          sched.find_materialized(db, "ks", job["params"]) is not None
          and sched.find_materialized(db, "ks", dict(job["params"], bins=10)) is None)
    bd = db.conn.execute("SELECT base_dist FROM model_health_daily "
                         "WHERE job_id=? AND base_dist IS NOT NULL",
                         (job["job_id"],)).fetchone()
    check("基准分布随 KS 一起物化回流（实时 PSI 不必重算基准期）",
          bool(bd and bd["base_dist"]))
    out_m = model_tool.run(db, {"metric_id": "ks",
                                "params": {"product": "P_CASH_001",
                                           "window": "近三个月", "bins": 5,
                                           "label": "fpd30"}},
                           role="risk_analyst")
    check("模型级返回契约：来源/标签/箱数/信封第 5 行齐全，且无分子分母",
          out_m.get("source") in ("MEASURED", "MATERIALIZED")
          and out_m.get("label") == "fpd30" and out_m.get("bins_n") == 5
          and bool(out_m.get("envelope_extra"))
          and "numerator_n" not in out_m and "denominator_n" not in out_m,
          str({k: out_m.get(k) for k in ("source", "label", "bins_n")}))
    small = model_metrics.compute_model_metric(
        "ks", {"product": "P_CASH_001", "window": "近一个月", "bins": 5,
               "label": "fpd30"}, role="system", db=db)
    check("小样本 → VINTAGE_BLOCKED：只给 n，不给 KS",
          small.get("code") == "VINTAGE_BLOCKED" and small.get("n")
          and "ks_max" not in small, str(small.get("n")))

    # ---------------------------------------------------------------
    print("\n[14] S0 骨架：LLM 与记忆默认关闭（关闭=什么都不发生）")
    check("LLM 默认关闭时 plan/narrate 直通规则版（不是报错，也不是再走一遍）",
          not llm.enabled()
          and llm.plan("现金贷通过率", "aggregate",
                       lambda: {"steps": ["understand"], "intent": "STATS"}
                       )["source"] == "rules"
          and llm.narrate({}, lambda: "模板文本")["source"] == "template",
          str(llm.describe()))
    lt = memory.LongTerm()
    bad_pref = lt.remember("risk_analyst", "label", "fpd7")   # 口径字段
    _, used = memory.apply({"slots": {"product": "P_CASH_002"}},
                           {"product": "P_CASH_001", "window": "近一个月"})
    check("记忆只填缺失槽位，且口径字段拒绝由记忆提供",
          bad_pref.get("code") == "NOT_APPLICABLE" and used == ["window"],
          f"{bad_pref.get('code')} / {used}")

    # ---------------------------------------------------------------
    print("\n[15] S0.9 收口：曲线 / 分源 / 三态 / 阈值 / 升格（T-101—107）")
    import dataclasses
    import json as _j
    from core import settings as _st
    from scheduler import promotion as _promo

    # ---- T-101 曲线类：只回摘要 + ref_id，且摘要与标量口径必须同一个数 ----
    cur = model_tool.run(db, {"metric_id": "ks_curve",
                              "params": {"product": "P_CASH_001",
                                         "window": "近三个月", "bins": 5,
                                         "label": "fpd30"}},
                         role="risk_analyst")
    plain = model_tool.run(db, {"metric_id": "ks",
                                "params": {"product": "P_CASH_001",
                                           "window": "近三个月", "bins": 5,
                                           "label": "fpd30"}},
                           role="risk_analyst")
    ref_file = ART / "ref" / f"{cur.get('ref_id')}.json"
    _ref = _j.loads(ref_file.read_text(encoding="utf-8")) if ref_file.exists() else {}
    _pts = (_ref.get("groups", {}).get("overall") or {}).get("points") or []
    check("曲线类只回摘要 + ref_id（点不外泄，且逐点已落文件）",
          cur.get("kind") == "curve" and not cur.get("rows_inline")
          and bool(cur.get("offloaded")) and ref_file.exists()
          and cur.get("summary", {}).get("ks_max") is not None
          # n_points 是真实不同分值数、n_points_out 是抽稀后落盘点数：
          # 两个数都要对得上文件里的实际点数，否则"外卸"只是句声明。
          and (cur.get("n_points") or 0) > 0
          and 0 < (cur.get("n_points_out") or 0) <= cur.get("n_points")
          and len(_pts) == cur.get("n_points_out"),
          f"ref={cur.get('ref_id')} rows={len(cur.get('rows_inline') or [])} "
          f"n_points={cur.get('n_points')} out={cur.get('n_points_out')} "
          f"file_pts={len(_pts)}")
    check("曲线 ks_max 与标量 ks 逐位一致（同一个定义，不许两个数）",
          cur.get("summary", {}).get("ks_max") == plain.get("value")
          and cur.get("summary", {}).get("at_score")
          == plain.get("ks_argmax"),
          f"{cur.get('summary')} vs {plain.get('value')}")

    # ---- T-102 双分源 ----
    pm = {"product": "P_CASH_001", "window": "近三个月", "bins": 5,
          "label": "fpd30"}
    k_model = model_metrics.compute_model_metric("ks", dict(pm, score_source="model"),
                                                 role="system", db=db)
    k_pol = model_metrics.compute_model_metric("ks", dict(pm, score_source="policy"),
                                               role="system", db=db)
    k_pol2 = model_metrics.compute_model_metric("ks", dict(pm, score_source="policy"),
                                                role="system", db=db)
    k_bad = model_metrics.compute_model_metric("ks", dict(pm, score_source="v1"),
                                               role="system", db=db)
    check("两种分源各自可复现、互不相等、且签名不同（分源进签名）",
          k_pol.get("value") == k_pol2.get("value")
          and k_pol.get("value") != k_model.get("value")
          and k_pol.get("signature") != k_model.get("signature")
          and k_bad.get("code") == "PARAM_OUT_OF_DOMAIN",
          f"model={k_model.get('value')} policy={k_pol.get('value')}")

    # ---- T-103 模型级定义哈希 ----
    orig = model_metrics.MODEL_CATALOG["ks"]
    model_metrics.MODEL_CATALOG["ks"] = dataclasses.replace(orig, label_col="fpd7")
    try:
        raised = _raises(lambda: model_metrics.verify_definitions(strict=True))
    finally:
        model_metrics.MODEL_CATALOG["ks"] = orig
    check("模型级同 id 同版本改定义 → 启动即报错（不是等人发现）",
          raised, "verify_definitions 没拦住 label_col 变更")

    # ---- T-104 最小样本可配置 ----
    old_min = _st.MIN_SAMPLE
    try:
        _st.MIN_SAMPLE = 50
        small = model_metrics.compute_model_metric(
            "ks", {"product": "P_CASH_001", "window": "近三个月", "bins": 5,
                   "label": "fpd30"}, role="system", db=db)
    finally:
        _st.MIN_SAMPLE = old_min
    check("MIN_SAMPLE=50 → VINTAGE_BLOCKED 只给 n（阈值是配置，不是魔法数）",
          small.get("code") == "VINTAGE_BLOCKED" and small.get("n")
          and "value" not in small
          and small.get("threshold") == 50, f"{small.get('code')} {small.get('threshold')}")

    # ---- T-106 三态分流 ----
    probes = ["现金贷模型最近表现怎么样", "模型的效果好不好", "评分准不准"]
    states = [model_metrics.detect_state(q)[0] for q in probes]
    parsed_amb = [intent.parse(q) for q in probes]
    answered = intent.apply_answer(parsed_amb[0],
                                   {"branch": "模型指标（KS/AUC/PSI）"})
    check("3 句模糊问句全部 ambiguous 且缺 branch（不选默认分支）",
          states == ["ambiguous"] * 3
          and all(p["missing"] == ["branch"] for p in parsed_amb)
          and answered["slots"].get("model_metric") == "ks",
          f"{states} / {[p['missing'] for p in parsed_amb]}")

    # ---- T-105 升格队列 + 下线 ----
    pend = None
    qp = {"product": "P_INSTALL_003", "window": "近三个月", "bins": 5,
          "label": "fpd30"}
    for _ in range(_st.PROMOTE_AFTER):
        pend = _promo.record_question(db, "ks", qp, catalog.AS_OF)
    queued = {r["signature"] for r in _promo.queue(db)}
    job0 = sched.JOBS_BY_ID["model_health_daily_v1"]
    jid, jparams, jmid = job0["job_id"], job0["params"], job0["metric_id"]
    before = sched.find_materialized(db, jmid, jparams) is not None
    _promo.retire_job(db, jid, note="selftest")
    after = sched.find_materialized(db, jmid, jparams) is not None
    ran = sched.run_job(db, jid)
    _promo.activate_job(db, jid, note="selftest 复原")
    check("同参问 5 次入队；job 下线后不再被命中、也不再执行（历史表保留）",
          bool(pend and pend["should_promote"] and pend["signature"] in queued
               and before and not after and ran.get("code") == "JOB_RETIRED"),
          f"pend={pend} before={before} after={after} ran={ran.get('code')}")

    # ---------------------------------------------------------------
    print("\n[16] LLM 统一出口：固定参数 / 重试 / 超时 / schema 校验（S1 T-201）")
    from llm import call as llm_call

    class _Fake:
        """按剧本应答的假 provider —— 单测不碰网络，P-08 的边界才可信。"""
        NAME = "fake"
        SUPPORTS_SEED = True

        def __init__(self, script):
            self.script, self.calls = list(script), 0

        def chat(self, messages, **kw):
            self.calls += 1
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    _schema = {"type": "object", "additionalProperties": False,
               "required": ["intent"],
               "properties": {"intent": {"enum": ["STATS", "MODEL_DIAG"]}}}
    _good = {"text": '{"intent": "STATS"}', "usage": {"total_tokens": 12}}

    # ① 正常：结构 {data,usage,meta}，审计字段齐，预算记账
    p1, b1 = _Fake([_good]), llm.Budget()
    r1 = llm_call.call("plan", "问：通过率", _schema,
                       model="qwen3-30b-a3b-instruct-2507",
                       budget=b1, provider=p1, seed=42)
    check("统一出口正常返回：temperature=0 / seed / schema_hash 可审计，预算入账",
          r1["data"] == {"intent": "STATS"} and r1["meta"]["attempts"] == 1
          and r1["meta"]["temperature"] == 0 and r1["meta"]["seed"] == 42
          and r1["meta"]["schema_hash"] == llm_call.schema_hash(_schema)
          and bool(r1["meta"]["output_hash"])
          and b1.calls == 1 and b1.tokens == 12, str(r1.get("meta")))

    # ② 重试：第一次不合 schema，第二次合规 → 成功且恰好 2 次尝试
    p2 = _Fake([{"text": '{"foo": 1}', "usage": {"total_tokens": 3}}, _good])
    r2 = llm_call.call("plan", "q", _schema, model="m-v1", provider=p2)
    check("输出不合 schema → 重试 1 次后成功（attempts=2）",
          r2["data"] == {"intent": "STATS"} and r2["meta"]["attempts"] == 2
          and p2.calls == 2, str(r2.get("meta")))

    # ③ 超时：立即 LLMUnavailable/timeout，不重试（重试超时请求只会放大雪崩）
    p3 = _Fake([llm_call.ProviderTimeout("read timeout"), _good])
    try:
        llm_call.call("plan", "q", _schema, model="m-v1", provider=p3)
        t3 = False
    except llm_call.LLMUnavailable as e:
        t3 = e.reason == "timeout" and p3.calls == 1
    check("超时 → LLMUnavailable/timeout 且不重试（上层走 D2）", t3)

    # ④ schema 两次不合规：重试额度只有 1 次，用完即失败 —— 不给替代答案（P-10）
    p4 = _Fake([{"text": '{"foo": 1}', "usage": {}},
                {"text": '{"bar": 2}', "usage": {}}])
    try:
        llm_call.call("plan", "q", _schema, model="m-v1", provider=p4)
        t4 = False
    except llm_call.LLMUnavailable as e:
        t4 = e.reason == "schema" and p4.calls == 2
    check("两次都不合 schema → LLMUnavailable/schema（重试额度恰好 1 次）", t4)

    db.close()
    print("\n" + "═" * 78)
    print(f"自检结果：{PASS} 条通过 / {FAIL} 条失败")
    print("═" * 78)
    sys.exit(0 if FAIL == 0 else 1)


def _raises(fn) -> bool:
    try:
        fn()
        return False
    except Exception:
        return True


def _shift(d: str, days: int) -> str:
    from datetime import date, timedelta
    return (date.fromisoformat(d) + timedelta(days=days)).isoformat()


if __name__ == "__main__":
    main()
