#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""7 题演示回归（V2.5 §15 验收门 ⑥：现有 45 条自检 + 7 题演示逐位不变）。

════════════════════════════════════════════════════════════════════
为什么"逐位"要单独做一条，45 条自检不够
════════════════════════════════════════════════════════════════════
自检断言的是**语义**（分母是谁、口径对不对、两次是否一致），它允许叙述
措辞、指标顺序、表格宽度变化。而这类改动累积起来，会让"同一份数据、
同一句问题，昨天和今天给出不一样的报告"——业务对 Agent 的信任是靠
**可复现**攒起来的，靠"逻辑没错"攒不起来。

所以这里做的是**字节级**比对，且只比正文：
  · 信封 4 行不比（V2.2 明确新增，是设计变更而非回归）
  · 旧的单行"口径：…"不比（信息已被信封接管，同一事实不留两处）
其余每一行、每个数字、每个空格都必须和基线一致。

════════════════════════════════════════════════════════════════════
两类失败要能分开
════════════════════════════════════════════════════════════════════
  ❌ 数字变了    → 口径/取数/去重被动了，最严重
  ❌ 只有文字变了 → 叙述模板被改了，通常是文案调整
脚本把差异行原样打出来，由人判断是哪一类 —— 不做自动"容忍"，
因为"自动容忍 1% 差异"就是下一次数据事故的入口。

跑法：
  python eval/regress_demo.py                 # 比对基线
  python eval/regress_demo.py --bootstrap     # 从当前运行重建基线
"""
from __future__ import annotations

import difflib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "demo_v22_baseline.json"

# 这 7 条是 V2.2 定版时就在的演示问题；问题文本本身也是基线的一部分
BASELINE_QUESTIONS = [
    "现金贷近一个月，每个环节、每个策略的通过率和拒绝率统计一下",
    "现金贷近1周 FPD7 升高了，是什么原因？",
    "现金贷近一个月 FPD7 是多少？",
    "现金贷最近三个月通过率下降的原因是什么？",
    "现金贷当前线上策略是什么？",
    "统计现金贷当前策略里所有策略节点的贡献，哪些需要调整或进一步 AB test？",
    "现金贷策略的通过率太低了，帮我回测分析一下",      # ← 缺时间范围，触发反问
]

SKIP_PREFIX = ("【口径】", "【窗口】", "【过滤】", "【截止】", "【分箱】",
               "【状态】", "口径：")


def normalize(answer: str) -> list[str]:
    """抽正文：去掉信封 4+1 行、旧的单行口径、以及回答开头的问题回显。

    只删"设计上被取代的行"与"渲染层自带的问题回显"，不做 strip、
    不做空行合并 —— 一旦开始"宽容地"比较，这条门禁就退化成长度检查。
    """
    out = []
    for line in (answer or "").splitlines():
        if line.startswith(SKIP_PREFIX):
            continue
        out.append(line.rstrip())
    while out and not out[0]:
        out.pop(0)
    # 首行若整行就是一个【…】（问题回显），丢掉它和紧跟的空行
    if out and out[0].startswith("【") and out[0].endswith("】") \
            and out[0].count("】") == 1:
        out.pop(0)
    while out and not out[0]:
        out.pop(0)
    while out and not out[-1]:
        out.pop()
    return out


def main() -> int:
    bootstrap = "--bootstrap" in sys.argv

    from langgraph.checkpoint.memory import InMemorySaver
    import v2_dialogue as V

    art = ROOT / "artifacts"
    try:
        art.mkdir(parents=True, exist_ok=True)
    except FileExistsError:
        pass
    db = V.prepare(art / "v25_regress.db", n_population=2000)
    eng = V.DialogEngine(db)
    graph = eng.build().compile(checkpointer=InMemorySaver())
    V.set_verbosity(False)

    got = {}
    for i, q in enumerate(BASELINE_QUESTIONS, 1):
        out = V.ask(eng, graph, q, sid=f"R-{i}")
        got[str(i)] = {"question": q, "lines": normalize(out.get("answer") or "")}

    if bootstrap:
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(json.dumps(got, ensure_ascii=False, indent=2),
                           encoding="utf-8")
        print(f"已写入基线：{FIXTURE}")
        db.close()
        return 0

    if not FIXTURE.exists():
        print(f"❌ 基线不存在：{FIXTURE}\n   先跑 --bootstrap 生成一次")
        db.close()
        return 2
    base = json.loads(FIXTURE.read_text(encoding="utf-8"))

    print("═" * 78)
    print("7 题演示回归（字节级；信封与旧单行口径不计）")
    print("═" * 78)

    bad = 0
    for i in base:
        old = base[i]
        cur = got.get(i) or {}
        if old["question"] != cur.get("question"):
            print(f"  ✗ Q{i} 问题文本被改了：{cur.get('question')!r}")
            bad += 1
            continue
        diff = [d for d in difflib.unified_diff(old["lines"], cur.get("lines") or [],
                                                fromfile=f"Q{i}/基线",
                                                tofile=f"Q{i}/本次", lineterm="")
                if not d.startswith(("---", "+++"))]
        if not diff:
            print(f"  ✓ Q{i} 逐位一致（{len(old['lines'])} 行）")
            continue
        bad += 1
        nums = [ln for ln in diff if ln[:1] in "+-"
                and any(ch.isdigit() for ch in ln)]
        print(f"  ✗ Q{i} 有差异（{len(diff)} 行，其中含数字 {len(nums)} 行）")
        for ln in diff[:12]:
            print(f"      {ln}")

    print("\n" + "═" * 78)
    print(f"{len(base) - bad}/{len(base)} 题逐位一致"
          + ("  ✅ 无回归" if not bad else "  ❌ 存在回归"))
    db.close()
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
