# -*- coding: utf-8 -*-
"""qwen（阿里云百炼 DashScope · OpenAI 兼容模式）适配器（S1 T-201，ADR-001）。

纪律：只做协议翻译，不含任何业务判断（重试/校验/预算都在 llm/call.py）。
密钥只从环境变量读（BTA_QWEN_API_KEY 或 DASHSCOPE_API_KEY），
不入码、不入日志、不进异常文案（异常只带 HTTP 状态与截断响应）。
"""
from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request

from llm.call import ProviderError, ProviderHTTPError, ProviderTimeout

NAME = "qwen"
SUPPORTS_SEED = True
ENDPOINT = os.environ.get(
    "BTA_QWEN_BASE_URL",
    "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions")


def chat(messages, *, model, temperature, seed, max_tokens, timeout_s, **_):
    """发一次 chat completion，返回 {"text": 内容, "usage": {...}}。"""
    key = os.environ.get("BTA_QWEN_API_KEY") or os.environ.get("DASHSCOPE_API_KEY")
    if not key:
        raise ProviderError("缺少 API key（BTA_QWEN_API_KEY / DASHSCOPE_API_KEY）")
    body: dict = {"model": model, "messages": messages,
                  "temperature": temperature, "max_tokens": max_tokens,
                  "stream": False,
                  # 要求 JSON 输出；但"provider 答应了"不算数，出口由本地校验把守
                  "response_format": {"type": "json_object"}}
    if seed is not None:
        body["seed"] = seed
    req = urllib.request.Request(
        ENDPOINT, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        snippet = ""
        try:
            snippet = e.read().decode("utf-8", errors="ignore")[:200]
        except Exception:                                     # noqa: BLE001
            pass
        raise ProviderHTTPError(e.code, snippet) from e
    except (socket.timeout, TimeoutError) as e:
        raise ProviderTimeout(f"read timeout（{timeout_s}s）") from e
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), (socket.timeout, TimeoutError)):
            raise ProviderTimeout(f"read timeout（{timeout_s}s）") from e
        raise ProviderError(f"网络不可达: {e.reason}") from e
    try:
        choice = payload["choices"][0]
        usage = payload.get("usage") or {}
        return {"text": choice["message"]["content"] or "",
                "usage": {"prompt_tokens": usage.get("prompt_tokens", 0),
                          "completion_tokens": usage.get("completion_tokens", 0),
                          "total_tokens": usage.get("total_tokens", 0)}}
    except (KeyError, IndexError, TypeError) as e:
        raise ProviderError(f"响应结构异常: {e}") from e
