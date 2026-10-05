# -*- coding: utf-8 -*-
"""回测取消信号（被 runner / 各回测模块 / GUI 共享，无循环依赖）。"""
class BacktestCancelled(Exception):
    """由 cancel_event 触发，用于干净地中止回测（不污染全局配置、不抛未捕获异常）。"""
