#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""数据源可配置化包（第一刀：抽象骨架 + 内置三源原样注册）。

对外暴露核心类；registry 内部惰性地导入 builtin，避免 import 期循环。
"""
from engine.data_sources.base import DataSource

__all__ = ["DataSource", "DataSourceRegistry", "get_registry"]

# 延迟导入 registry（其依赖 builtin，builtin 又依赖 data_layer），
# 避免在包顶层 import 时引发顺序问题。
def get_registry():
    from engine.data_sources.registry import DataSourceRegistry
    return DataSourceRegistry
