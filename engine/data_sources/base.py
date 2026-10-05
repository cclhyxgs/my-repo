#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""数据源抽象基类（第一刀）。

设计要点：
- 按数据类别拆接口（K线 / 实时行情 / 股票列表 / 市场宽度），与现有
  ``DataAPI`` 网络方法的产出结构刻意保持一致，使 facade 可零侵入替换底层。
- 各方法默认 ``NotImplementedError``，provider 只重写它实际服务的类别，
  无需为每个类别都实现一遍（单类别 provider 友好）。
- 凭据（如 Tushare token）通过 ``set_credential`` 注入；内置源无需凭据，默认忽略。
"""
import logging

logger = logging.getLogger(__name__)


class DataSource:
    """一个数据源 provider 的接口契约。

    子类只重写它服务的类别方法即可；未重写的类别调用会抛
    ``NotImplementedError``，但 facade 只会对该类别配置的 provider 调用对应方法，
    因此不会误触。
    """

    #: provider 显示名（用于日志 / 后续 UI）
    name = "unnamed"

    # ------------------------------------------------------------------
    # K 线：日K k_type=240 / 周K k_type=1200
    # 返回 (df, err)，df 为带 open/close/high/low/volume/trade_time 列的 DataFrame
    # ------------------------------------------------------------------
    def get_kline(self, stock_code, k_type, days):
        raise NotImplementedError(f"{self.name} 不提供 K 线数据")

    # ------------------------------------------------------------------
    # 实时行情：返回 (fields, source_name)，无数据返回 (None, None)
    # fields 为按现有实时字段顺序的 list（与 RealtimeQuoteFetcher 产出一致）
    # ------------------------------------------------------------------
    def get_realtime(self, code):
        raise NotImplementedError(f"{self.name} 不提供实时行情")

    # ------------------------------------------------------------------
    # 股票列表：加载（写入全局 state）并返回已加载数量 int；失败/无数据返回 0
    # （与 StockListLoader.load 语义一致）
    # ------------------------------------------------------------------
    def get_stock_list(self):
        raise NotImplementedError(f"{self.name} 不提供股票列表")

    # ------------------------------------------------------------------
    # 市场宽度（涨跌家数）：返回 (up, down, source) 或 (None, None, None)
    # ------------------------------------------------------------------
    def get_market_breadth(self):
        raise NotImplementedError(f"{self.name} 不提供市场宽度")

    # ------------------------------------------------------------------
    # 凭据注入（可插源重写）；内置源无需凭据，默认忽略
    # ------------------------------------------------------------------
    def set_credential(self, value):
        """provider 需要的凭据（如 token）。内置源无需，默认忽略。"""
        pass
