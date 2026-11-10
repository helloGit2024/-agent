#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键跑全部门禁（V2.5 §15 的三级门禁，落地成一条命令）。

════════════════════════════════════════════════════════════════════
为什么"分级"而不是"一把梭"
════════════════════════════════════════════════════════════════════
  芯（A）  口径数学：LMDI 残差 / 方向 / 五态 / 长表行数
           改 core/ 必跑。挂了说明数字本身错了，其他门禁跑不跑都没意义。
  壳（B）  语义层 + 对话层 + 分流 + 回归
           改 v2_* / llm/ / tools/ / scheduler/ 必跑。
  静（C）  静态门禁：唯一实现（不许有第二套算法）+ 契约封闭
           改任何 import 都要跑。它不跑数据，只读代码，最快。

分级的意义只有一个：**门禁挂了，一眼能看出该找谁**。
混成一条"评估失败"，三次之后所有人都会开始跳过它。

════════════════════════════════════════════════════════════════════
输出约定
════════════════════════════════════════════════════════════════════
每行一个门禁 + 结果 + 耗时。任一失败即整体非 0（CI 靠这个拦）。
失败项的原始输出不吞掉，直接打到 stderr —— 吞日志是门禁失效的第一原因。

跑法：python ci/gate_all.py          （exit 0 = 全部通过）
      python ci/gate_all.py --fast   （跳过 7 题回归，改代码时用）
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (层级, 名字, 脚本, 期望关键句)
GATES = [
    ("A 芯", "V1 内核自检 32 条", "selftest.py", "32 条断言全部满足"),
    ("B 壳", "V2 自检 100 条", "selftest_v2.py", "100 条通过"),
    ("B 壳", "语义层评估器 13–18", "eval/eval_semantic.py", "全部符合预期"),
    ("B 壳", "7 题演示回归", "eval/regress_demo.py", "无回归"),
    ("C 静", "唯一实现 / 契约封闭", "ci/check_imports.py", "两道门禁全绿"),
    # D 组（S0.9 T-108）：两本册子 + 闸门 + 配置 + 工具面是否互相自洽。
    # 它治的病很特别：每一处单看都对，合起来矛盾（曲线注册了但没进 enum、
    # 阈值改了但代码还读常量、三态做了但 detect 还是两态）。
    ("D 语义", "册子 / 闸门 / 配置 / 工具面", "ci/check_semantic.py", "D 组语义一致性全绿"),
]


def run(script: str, needle: str) -> tuple[bool, str, float]:
    t0 = time.time()
    p = subprocess.run([sys.executable, script], cwd=ROOT,
                       capture_output=True, text=True)
    dt = time.time() - t0
    out = (p.stdout or "") + (p.stderr or "")
    ok = p.returncode == 0 and needle in out
    return ok, out, dt


def main() -> int:
    fast = "--fast" in sys.argv
    gates = [g for g in GATES if not (fast and "回归" in g[1])]

    print("═" * 78)
    print(f"门禁总览（{len(gates)} 项{'，--fast 已跳过回归' if fast else ''}）")
    print("═" * 78)

    failed: list[tuple[str, str, str]] = []
    for layer, name, script, needle in gates:
        ok, out, dt = run(script, needle)
        print(f"  {'✓' if ok else '✗'} [{layer}] {name:<22} {dt:5.1f}s"
              f"{'' if ok else '   ← 失败'}")
        if not ok:
            failed.append((name, script, out))

    print("\n" + "═" * 78)
    if not failed:
        print(f"✅ {len(gates)}/{len(gates)} 全绿")
        return 0
    print(f"❌ {len(failed)}/{len(gates)} 失败：{', '.join(n for n, _, _ in failed)}")
    for name, script, out in failed:
        print("\n" + "-" * 78)
        print(f"【{name}】{script} 的最后 25 行：")
        for line in out.splitlines()[-25:]:
            print("  " + line)
    return 1


if __name__ == "__main__":
    sys.exit(main())
