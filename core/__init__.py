# -*- coding: utf-8 -*-
"""backtest_agent_lab.core —— 回测系统的"内核"包，框架无关。

设计立场（与 approval_lab 一脉相承）：
  LangGraph 只做最外层编排（按什么顺序调、失败怎么办、什么时候找人），
  指标计算、策略重放、归因分解必须是可单测的纯函数。
  内核不可测试，Agent 只会让错误更快地生产出来。
"""
