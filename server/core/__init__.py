# -*- coding: utf-8 -*-
"""core 层：与业务无关的横切原语（时区 / SSE / 日志 / 错误 / 锁清单校验）。

分层规则：core 不得 import api，也不得 import engine（后者只允许 adapters/engine_bridge.py）。
"""
