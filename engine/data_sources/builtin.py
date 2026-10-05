#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""内置数据源 provider（第一刀：薄委托，行为与原 data_layer 实现完全一致）。

本文件仅把现有硬编码源收口到 ``DataSource`` 接口，不重写任何解析逻辑，
保证「抽象骨架 + 内置三源原样注册」零行为变化。后续 Tushare 等可插源
才在独立文件中真正独立实现（读取 credentials.json 中的 token）。
"""
import logging

from engine.data_layer import (
    KLineFetcher,
    RealtimeQuoteFetcher,
    StockListLoader,
    MarketBreadthFetcher,
)
from engine.data_sources.base import DataSource

logger = logging.getLogger(__name__)


class TencentSource(DataSource):
    """腾讯：日K/周K 前复权，及实时行情备源。

    ⚠️ 本类**不是** K 线的唯一来源：`get_kline` 委托 KLineFetcher.fetch，
    实际链路由 `config/data_sources.json` 的 `kline_source` 决定（默认 "tdx"）。
    选 "tencent" 时它才是日K/周K 的唯一通道（无分钟级）。
    """
    name = "腾讯"

    def get_kline(self, stock_code, k_type, days):
        # 与原 DataAPI.get_kline 底层完全一致（KLineFetcher.fetch）
        return KLineFetcher.fetch(stock_code, k_type, days)

    def get_realtime(self, code):
        return RealtimeQuoteFetcher.fetch_tencent(code)


class SinaSource(DataSource):
    """新浪：股票列表（含东财备源 + 缓存），及实时行情主源。

    get_kline：委托 KLineFetcher.fetch。K 线**实际由 data_sources.json 的
    `kline_source` 决定**（默认 "tdx"：日K 通达信自算前复权 / 周K 通达信自算 /
    分钟 通达信未复权；切 "tencent" 或 "sina" 时另按 `_load_kline_sources()` 映射）。
    本类只负责股票列表与实时行情，**不决定 K 线走哪条链路**。
    """
    name = "新浪"

    def get_stock_list(self):
        # 与原 DataAPI.load_stock_list 完全一致（StockListLoader.load 内含缓存/备源）
        return StockListLoader.load()

    def get_realtime(self, code):
        return RealtimeQuoteFetcher.fetch_sina(code)

    def get_kline(self, stock_code, k_type, days):
        return KLineFetcher.fetch(stock_code, k_type, days)


class EastmoneySource(DataSource):
    """东方财富：市场宽度（涨跌家数），多镜像容错。"""
    name = "东方财富"

    def get_market_breadth(self):
        return MarketBreadthFetcher.fetch()
