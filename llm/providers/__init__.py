# -*- coding: utf-8 -*-
"""provider 适配器目录（ADR-001）：每个适配器 ≤80 行、不含任何业务判断。

适配器只做两件事：把请求翻译成 HTTP、把 HTTP 翻译回 dict。
重试 / 超时策略 / schema 校验 / 预算，全部在 llm/call.py。
"""
