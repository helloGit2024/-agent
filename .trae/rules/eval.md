---
description: 动测试 / 评估器 / 门禁 / 基线时加载
alwaysApply: false
globs:
  - "selftest.py"
  - "selftest_v2.py"
  - "eval/**"
  - "evals/**"
  - "ci/**"
---

# 测试与评估规则

## 断言的写法（这个仓库的断言就是文档）

一条好断言包含三件事：**名字说为什么、条件能被改坏、detail 能定位**。

```python
check("模型级回答不出现率值（分流失败会复现『问KS答通过率』事故）",
      "%" not in ans and "分子" not in ans,
      ans[:80])
```

反例（不许这样写）：

```python
check("test1", x == 1)          # 名字不说明为什么
check("结果正确", True)          # 永远真
```

## 每个新功能的两条断言

1. **正向**：正常路径返回值正确。
2. **反向**：把实现改坏（或喂坏输入），断言必须失败。
   写完反向断言后**真的去改坏一次**，确认它红——没红过的反向断言等于没有。

## 评估器纪律

- 编号只增不改；废弃标 `retired`，不复用号。
- 评估器自身异常 → `ok=None, unknown=True`，**不算通过**（否则通过率虚高）。
- 评估器不许调 LLM（唯一例外是 judge，且 judge 不计入规则分）。
- 每条评估器配 1 条反向用例（`eval_semantic.py` 的 `rev` 段）。

## 基线（最容易犯的错）

- 7 题基线 `eval/fixtures/demo_v22_baseline.json` 取自**改造前**日志；`make regress` 比字节。
- 140 条 replay fixture 只允许「计划按设计变更」时重录（P-11）。
- **不许**为了让测试绿而改 expected / 删断言 / 放宽 normalize。
  - `normalize()` 只允许删除「设计上被取代的行」（如旧单行口径），不做空行合并、不做 strip 模糊化。

## 门禁

- 新增检查必须进 `ci/gate_all.GATES`，否则等于没加（P-12）。
- 门禁脚本必须**退出码正确**：`| tail` 会让 exit code 失真，用 `; echo exit=$?` 或 `PIPESTATUS`。
- 沙箱里 `mkdir(exist_ok=True)` 可能对已存在目录抛 `EEXIST`：用 `parents=True, exist_ok=True` 且 try/except。

## 跑法

```bash
make sem       # 评估器 + 反向
make regress   # 7 题字节级
make replay    # 140 条（S5）
make gate      # 全部
```
