# -*- coding: utf-8 -*-
"""实体与 join 图（V2.5 §7.7）：跨表算错的是分母，而且不报错。

════════════════════════════════════════════════════════════════════
现状问题：三表 join 是硬编码的，fan-out 靠每个 compute 里的 set() 兜
════════════════════════════════════════════════════════════════════
旧版 build_view() 把 applicant / policy_eval_fact / loan_perf 三张表按
apply_id 直接 join，关系写死在代码里，没有一处声明"谁对谁是一对多"。
而 facts 对申请本身就是一对多（一条策略一行，一份申请十几行）。

现在之所以没算错，是因为每个 compute 都在内部用 set / dict 去了重
（final_pass_ids、entered.setdefault）。等于把"防止重复计数"写进了
6 个函数各自的实现细节 —— **加第 7 个指标的人不会知道有这个约定**。
他一写 sum(f.fail)，人数就翻十几倍，而且数看起来"更大更合理"，
没人会怀疑。

════════════════════════════════════════════════════════════════════
本模块做三件事
════════════════════════════════════════════════════════════════════
  ① 声明：每个实体的主键 + 与父实体的基数（1:1 / N:1）
  ② 推导：任意两实体的唯一 join 路径；无路径 → NO_JOIN_PATH
  ③ 收敛：凡经过 1:N 关系取回的行，**按父实体键去重**（在声明层，
     不在每个 compute 里）。验收断言：facts 表复制一份，5 个率指标
     必须一个比特都不变。

为什么"去重"要放在这一层而不是 compute 里：
  compute 是纯函数，它拿到的 View 应当**已经是收敛后的语义**。
  让每个 compute 各自记得去重，等于要求每个写新指标的人都背下这条隐规则，
  而这类隐规则一定会失传。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Entity:
    name: str
    key: tuple[str, ...]          # 主键（决定"什么算同一条事实"）
    table: str


@dataclass(frozen=True)
class Join:
    left: str
    right: str
    on: str
    cardinality: str              # "N:1" / "1:1"


# ---------------------------------------------------------------------------
# 声明：唯一事实源
# ---------------------------------------------------------------------------
ENTITIES: dict[str, Entity] = {
    "Applicant": Entity("Applicant", ("apply_id",), "applicant"),
    "Fact": Entity("Fact", ("apply_id", "stage", "policy_id"), "policy_eval_fact"),
    "Loan": Entity("Loan", ("apply_id",), "loan_perf"),
    "Score": Entity("Score", ("apply_id", "model_id"), "model_score"),
    # 未来要接（这里先声明，就是为了让"接新表"变成填一行而不是改 build_view）：
    "Repayment": Entity("Repayment", ("loan_id", "period_no"), "repayment_plan"),
}

JOINS: list[Join] = [
    Join("Fact", "Applicant", "apply_id", "N:1"),
    Join("Loan", "Applicant", "apply_id", "1:1"),
    Join("Score", "Applicant", "apply_id", "1:1"),
    Join("Repayment", "Loan", "loan_id", "N:1"),
]

# 指标 grain → 允许使用的维度（"从 grain 出发能走到、且不改变基数"）
# 这条规则不是白名单的替代品，而是它的**来源**：白名单若与此冲突，
# 说明有人在手写白名单时忘了基数这件事。
GRAIN_DIMENSIONS: dict[str, tuple[str, ...]] = {
    "APPLY_DATE": ("product", "channel", "new_vs_old", "month", "day", "stage"),
    "LOAN_DUE": ("product", "channel", "month"),
    "MOB3": ("product", "channel", "new_vs_old"),
}


def _neighbors(a: str) -> list[tuple[str, Join]]:
    out = []
    for j in JOINS:
        if j.left == a:
            out.append((j.right, j))
        elif j.right == a:
            out.append((j.left, j))
    return out


def path(a: str, b: str, _seen: frozenset[str] = frozenset()) -> list[Join] | None:
    """任意两实体的唯一路径（BFS）。无路径返回 None → 调用方返 NO_JOIN_PATH。

    为什么必须有这个函数：LLM 会"看起来能连"就把两个口径拼在一起，
    连出来是笛卡尔积 —— 跑得通、不报错、数大十几倍。
    """
    if a == b:
        return []
    if a in _seen or a not in ENTITIES or b not in ENTITIES:
        return None
    frontier: list[tuple[str, list[Join]]] = [(a, [])]
    seen = set(_seen) | {a}
    while frontier:
        cur, acc = frontier.pop(0)
        for nxt, j in _neighbors(cur):
            if nxt in seen:
                continue
            if nxt == b:
                return acc + [j]
            seen.add(nxt)
            frontier.append((nxt, acc + [j]))
    return None


def key_of(entity: str) -> tuple[str, ...]:
    return ENTITIES[entity].key


def converge(rows: list[dict], entity: str) -> tuple[list[dict], int]:
    """按实体主键收敛（去重）。返回 (收敛后行, 被去掉的重复行数)。

    为什么要返回"去掉了多少行"而不是静默去重：
    静默去重会让"join 写错"这件事永远不被发现 —— 数是对的，
    但没人知道上游多给了一倍的行。这个计数进审计，用来发现上游回归。
    """
    key = key_of(entity)
    seen: set[tuple] = set()
    out: list[dict] = []
    dups = 0
    for r in rows:
        k = tuple(r.get(c) for c in key)
        if k in seen:
            dups += 1
            continue
        seen.add(k)
        out.append(r)
    return out, dups


# ---------------------------------------------------------------------------
# 取数：build_view 的三段取数从这里走（唯一实现）
# ---------------------------------------------------------------------------
def select_applicants(db, window, product: str | None,
                      row_filter: dict | None = None) -> tuple[list[dict], int]:
    """按窗口/产品/行权限筛申请。row_filter 在**取数前**生效。

    行权限为什么必须在这一层：取数后再筛 = 全量已经进内存，
    越权已经发生，日志里还留不下证据。
    """
    apps = db.applicants()
    rf = row_filter or {}
    allowed_products = set(rf.get("product") or []) or None
    out = []
    for a in apps:
        if not window.contains(str(a.get("created_at") or "")[:10]):
            continue
        if product is not None and a["product_code"] != product:
            continue
        if allowed_products is not None and a["product_code"] not in allowed_products:
            continue
        out.append(a)
    apps = out
    return apps, 0


def select_facts(db, run_id: str, apps: list[dict]) -> tuple[list[dict], int]:
    """取这些申请在本次 run 的事实行，并按 Fact 主键收敛。"""
    ids = {a["apply_id"] for a in apps}
    rows = [f for f in db.facts("run_id=?", (run_id,)) if f["apply_id"] in ids]
    return converge(rows, "Fact")


def select_loans(db, apps: list[dict]) -> tuple[list[dict], int]:
    ids = {a["apply_id"] for a in apps}
    rows = [dict(l) for l in db.conn.execute(
        "SELECT * FROM loan_perf").fetchall() if l["apply_id"] in ids]
    return converge(rows, "Loan")


def select_scores(db, apps: list[dict], model_id: str) -> tuple[list[dict], int]:
    """模型分（若表还没建，返回空 —— 调用方必须当作"不可算"，不许当 0）。"""
    try:
        rows = [dict(r) for r in db.conn.execute(
            "SELECT * FROM model_score WHERE model_id=?", (model_id,)).fetchall()]
    except Exception:                     # noqa: BLE001  表不存在 = 没有分数
        return [], 0
    ids = {a["apply_id"] for a in apps}
    rows = [r for r in rows if r["apply_id"] in ids]
    return converge(rows, "Score")
