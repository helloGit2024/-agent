# -*- coding: utf-8 -*-
"""LLM 统一出口（S1 T-201，ADR-001）：业务代码能摸到 LLM 的**唯一**入口。

════════════════════════════════════════════════════════════════════
为什么是一层薄网关，而不是直接用 provider SDK
════════════════════════════════════════════════════════════════════
没有统一出口，「被记录、被限流、能降级」（P-08 / P-10）就全是口头约定。
本模块保证四件事：

  ① 参数固定：temperature=0、seed（会话级）、model 必须传**具体版本号**、
     每次调用带 prompt_version / schema_hash，审计字段一个不缺；
  ② 输出可信：provider 的 strict mode 不可信，本地再做一次 JSON-schema
     校验（零依赖子集：type / required / properties / additionalProperties
     / enum / items）；
  ③ 失败有型：重试恰好 1 次（仅 schema 不合规与 5xx），超时与 4xx 不重试；
     仍失败抛 LLMUnavailable（code=LLM_UNAVAILABLE）→ 上层走 D2，
     网关自己**绝不**给替代答案（P-10）；
  ④ 预算可断：传入 Budget 时先查余量再调用，成功后 token 记入账本。

llm_call_log 落库在 T-207 做；本模块先把 meta 备齐，落库时原样写入。
"""
from __future__ import annotations

import hashlib
import json

TEMPERATURE = 0                    # P-08：写死，不许成为参数
DEFAULT_TIMEOUT_S = 10
DEFAULT_MAX_TOKENS = 2000
MAX_ATTEMPTS = 2                   # 首次 + 恰好 1 次重试（ADR-001）


# ---------------------------------------------------------------------------
# 失败形态：网关对外只有一种异常；provider 异常只在本模块与适配器之间流通
# ---------------------------------------------------------------------------
class LLMUnavailable(Exception):
    """LLM 不可用（超时/5xx/schema 两次不合规/预算耗尽）。上层唯一的 D2 触发点。"""
    code = "LLM_UNAVAILABLE"

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"LLM_UNAVAILABLE/{reason}" + (f": {detail}" if detail else ""))
        self.reason = reason


class ProviderError(Exception):
    """适配器侧通用失败（缺 key / 响应结构异常 / 网络不可达）。"""


class ProviderTimeout(ProviderError):
    """读超时。不重试 —— 重试一个已经超时的请求只会放大雪崩。"""


class ProviderHTTPError(ProviderError):
    def __init__(self, status: int, detail: str = ""):
        super().__init__(f"HTTP {status} {detail}".strip())
        self.status = status

    @property
    def retryable(self) -> bool:
        return 500 <= self.status < 600


# ---------------------------------------------------------------------------
# 审计字段
# ---------------------------------------------------------------------------
def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def schema_hash(schema: dict) -> str:
    """口径的指纹：schema 变一个字节，hash 就变 —— 审计靠它对齐版本。"""
    canon = json.dumps(schema, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return _sha(canon)[:12]


# ---------------------------------------------------------------------------
# 本地 JSON-schema 校验（零依赖子集）。provider 说"我保证"不算数。
# ---------------------------------------------------------------------------
def _validate(data, schema: dict, path: str = "$") -> list[str]:
    bad: list[str] = []
    t = schema.get("type")
    if t == "object" or (t is None and "properties" in schema):
        if not isinstance(data, dict):
            return [f"{path} 应为 object，实为 {type(data).__name__}"]
        for k in schema.get("required", []):
            if k not in data:
                bad.append(f"{path}.{k} 缺失（required）")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = sorted(set(data) - set(props))
            if extra:
                bad.append(f"{path} 多出字段 {extra}（additionalProperties=False）")
        for k, sub in props.items():
            if k in data:
                bad += _validate(data[k], sub, f"{path}.{k}")
    elif t == "array":
        if not isinstance(data, list):
            return [f"{path} 应为 array"]
        for i, item in enumerate(data):
            bad += _validate(item, schema.get("items", {}), f"{path}[{i}]")
    elif t == "string":
        if not isinstance(data, str):
            bad.append(f"{path} 应为 string")
    elif t == "integer":
        if not isinstance(data, int) or isinstance(data, bool):
            bad.append(f"{path} 应为 integer")
    elif t == "number":
        if not isinstance(data, (int, float)) or isinstance(data, bool):
            bad.append(f"{path} 应为 number")
    elif t == "boolean":
        if not isinstance(data, bool):
            bad.append(f"{path} 应为 boolean")
    enum = schema.get("enum")
    if enum is not None and data not in enum:
        bad.append(f"{path} 取值 {data!r} 不在 enum {enum}")
    return bad


def _parse(text: str) -> tuple[object | None, str | None]:
    """宽容解析：剥掉 ```json 围栏后 json.loads。失败不算新错误类，归入 schema。"""
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(body), None
    except (json.JSONDecodeError, ValueError) as e:
        return None, f"输出不是合法 JSON: {e}"


# ---------------------------------------------------------------------------
# 唯一入口
# ---------------------------------------------------------------------------
def call(purpose: str, prompt: str, schema: dict, *, model: str,
         budget=None, provider=None, seed: int | None = None,
         max_tokens: int = DEFAULT_MAX_TOKENS,
         timeout_s: int = DEFAULT_TIMEOUT_S,
         prompt_version: str = "dev") -> dict:
    """调一次 LLM。返回 {data, usage, meta}；失败只抛 LLMUnavailable。

    provider 可注入（测试用假 provider）；生产默认 llm.providers.qwen。
    provider 协议：模块/对象带 NAME、SUPPORTS_SEED、chat(messages, **opts)。
    """
    if not model:
        # 模型版本是审计字段，"用默认的"意味着审计里查不到这次到底用的谁
        raise LLMUnavailable("config", "model 必须显式传具体版本号（禁浮动别名）")
    provider = provider or _default_provider()
    if budget is not None and budget.exhausted():
        raise LLMUnavailable("budget", "预算已耗尽，走 D2 降级而不是再试一次")

    messages = [{"role": "user", "content": prompt}]
    seed_opt = seed if getattr(provider, "SUPPORTS_SEED", False) else None
    last_bad: list[str] = []

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = provider.chat(messages, model=model, temperature=TEMPERATURE,
                                 seed=seed_opt, max_tokens=max_tokens,
                                 timeout_s=timeout_s)
        except ProviderTimeout as e:
            raise LLMUnavailable("timeout", str(e)) from e
        except ProviderHTTPError as e:
            if e.retryable and attempt < MAX_ATTEMPTS:
                continue
            raise LLMUnavailable(f"http_{e.status}", str(e)) from e
        except ProviderError as e:
            raise LLMUnavailable("provider", str(e)) from e

        data, err = _parse(resp.get("text") or "")
        bad = [err] if err else _validate(data, schema)
        if bad:
            last_bad = bad
            if attempt < MAX_ATTEMPTS:
                continue
            raise LLMUnavailable("schema", "; ".join(bad)[:300])

        usage = resp.get("usage") or {}
        if budget is not None:
            budget.spend(tokens=int(usage.get("total_tokens", 0)))
        return {"data": data, "usage": usage, "meta": {
            "purpose": purpose, "model": model, "temperature": TEMPERATURE,
            "seed": seed_opt, "prompt_version": prompt_version,
            "schema_hash": schema_hash(schema), "max_tokens": max_tokens,
            "attempts": attempt, "output_hash": _sha(resp["text"]),
            "provider": getattr(provider, "NAME", type(provider).__name__),
        }}
    raise LLMUnavailable("schema", "; ".join(last_bad)[:300])  # 理论不可达，防御


def _default_provider():
    """生产默认 provider。缺 key 不是配置瑕疵，是部署事故 —— 按不可用处理。"""
    from llm.providers import qwen                        # 延迟 import：不开 LLM 不碰网络层
    return qwen
