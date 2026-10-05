# -*- coding: utf-8 -*-
"""worker 层：任务执行接口（骨架期，不含任何业务任务）。

§2.9 定案 Celery + Redis；本次不接（B5：celery/redis/psycopg 留空待 P1/P2 定版）。
本模块只固化接口，供 F-401 / F-501 / F-701 接入时替换实现。
本层不得 import engine（须经 adapters/engine_bridge.py），也不得 import api。
"""
