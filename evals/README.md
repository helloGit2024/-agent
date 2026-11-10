# 评估体系（模块六 / §13）

> 评估要回答三件事：**答得对不对、数对不对、退化了没有**。
> 不追求「一个分数」，追求「失败可定位」——每条用例都要能说出「哪一层错了」。

## 1. 六层结构（沿用 V1，只换内容）

| 层 | 做法 | 落地文件 |
|---|---|---|
| 1 数据集 | 代码化 + 稳定 `case_id` + upsert | `golden_set.yaml` + `eval/gen_cases.py` |
| 2 批量运行 | 每用例记计划 JSON + 轨迹 + book，不只记答案 | `eval/runner.py` |
| 3 规则评估器 | 18 个（ev01—ev18），纯规则，不调 LLM | `eval/eval_semantic.py`（13—18）+ `evaluation_langsmith.py`（1—12） |
| 4 故障记 unknown | 评估器自身异常 → `unknown`，不静默算通过 | `eval/runner.py` |
| 5 对照 | 规则版 vs LLM 版；bootstrap 置信区间 | `eval/compare_plans.py` |
| 6 三级门禁 | A 芯 / B 壳 / C 发布，按层统计 | `ci/gate_all.py` |

## 2. 18 个评估器（编号即契约，不许重排）

| # | 名称 | 判什么 | 层 |
|---|---|---|---|
| ev01 | tool_selection | 选对工具（问 KS 不调 `compute_metric`） | 轨迹 |
| ev02 | param_extraction | 参数抽对（product/window/label） | 计划 |
| ev03 | clarify_behavior | 缺槽位必须澄清，不许默认 | 对话 |
| ev04 | number_traceability | 数字全在 AnswerBook | 答案 |
| ev05 | envelope_presence | 信封齐全（4/5 行） | 答案 |
| ev06 | vintage_correction | FPD/MOB 窗口已前移且写明 | 答案 |
| ev07 | three_tier_closure | 三档闭合 100% | 答案 |
| ev08 | ldi_residual | LMDI 残差 = 0 | 计算结果 |
| ev09 | significance_language | 显著/不显著说法与检验一致 | 答案 |
| ev10 | no_detail_leak | 无明细行 | 答案 |
| ev11 | deterministic | 同输入同输出 | 全链路 |
| ev12 | latency_budget | 单问 ≤1.5s（不含 LLM） | 运行 |
| ev13 | no_free_form_metric | 轨迹无 sql/expression/formula/table/column | 轨迹 |
| ev14 | param_domain_validity | 参数在域内；率值带分母 | 答案 |
| ev15 | caliber_metadata_present | 信封四要素 + 模型级第五行 | 答案 |
| ev16 | access_scope_enforced | 越权必拒且零数字 | 答案 |
| ev17 | nonaggregate_routing | 模型级回答**不含任何率值**（反向判据） | 答案 |
| ev18 | source_provenance | MATERIALIZED/MEASURED 标对；两者逐位一致 | 答案 |
| ev19 | memory_disclosure | 记忆补全必须声明（S3 新增） | 答案 |
| ev20 | exit_gate_rollback | 未登记数字 → 走了回退（S4 新增） | 轨迹 |

> 18 → 20 是因为 S3/S4 各加一条。**编号只增不改**；废弃的评估器标 `retired`，不复用编号。

## 3. 每个评估器的统一返回

```python
{"idx": 17, "name": "nonaggregate_routing", "layer": "答案",
 "ok": True|False, "detail": "…", "severity": "high|med|low"}
```

- 评估器自身异常 → `{"ok": None, "unknown": True, "error": "…"}`，**不算通过**（层 4）。
- 每条评估器必须配 1 条**反向用例**：故意改坏，必须 `ok=False`。反向用例不过 = 评估器失效。

## 4. Agent-as-Judge（S5，只判三件事）

输入是完整 trace（不是 question/answer 二元组）：

```json
{ "question": "...", "plan": {...},
  "tool_calls": [{"name":"compute_model_metric","args":{...},"result_summary":"ks=0.26","latency_ms":120}],
  "book": {"model.ks_max": {"value":0.2576,"unit":"raw"}},
  "answer": "..." }
```

只回答：

1. 叙述里有没有数字与 `book` 不一致？
2. 有没有把「缺失/未判定」说成「0/全部」？
3. 有没有把结论方向说反（显著说成不显著、下降说成上升）？

输出 `{pass, violations:[{type,quote,expected}], reasoning}`。
**judge 不参与规则评估器计分**，只作补充信号；judge 自己挂了记 `unknown`。

## 5. 成本与延迟（S5）

| 指标 | 采集 | 上限 |
|---|---|---|
| `tokens_in / tokens_out` | `llm_call_log` | — |
| `latency_ms` | 每步 | 单问 P95 ≤1.5s（不含 LLM） |
| `cost_cny` | tokens × 单价（配置） | 单问 ≤0.05 元，超限标红不阻断 |
| 用例总成本 | 140 用例汇总 | <20 元/轮 |

## 6. 怎么跑

```bash
make sem       # 18 个评估器（当前 ev13—18 + 反向）
make replay    # 140 条回放（不调 LLM，秒级）
make eval-llm  # 真模型跑 140 条（慢、花钱，按需）
make report    # 生成成本/失败分布表
```

## 7. 评估结果怎么读（别只看通过率）

1. **先看 unknown 数**：评估器自身故障比用例失败更危险（它会让通过率虚高）。
2. **再看反向捕获率**：反向全抓才算评估器有效。
3. **最后看失败分布**：集中在某一层（如全在 ev05 信封）说明是模板问题，不是模型问题。
4. **LLM 版输给规则版** → 先怀疑 prompt，不要先调 temperature。
