# -*- coding: utf-8 -*-
"""回归：回测在个股「当日无K线（停牌/数据缺口）」的日子不得用过期数据做分析。

背景（2026-09-19 用户实测）：sh688072 在 store 中 2026-06-26 与 07-13 之间有 10 个
交易日缺口，回测却在 07-07 建仓、07-09 触发止损，导致「卖出日期早于买入日期」，
且止损价 832.00 = 06-26 收盘（过期数据）。
根因：_st_compute_full_analysis 的 searchsorted 分支把索引解析到上一根旧 bar。

⛔ 注意：store 里日 K 的 trade_time 带 **15:00 时间戳**（非零点），这是关键前提——
用精确相等判断日期会误杀所有正常日期，用 searchsorted(ts,'right')-1 会取到前一天。
本测试的 DataFrame 一律用 15:00，与真实 store 口径一致。
"""
import unittest
from unittest import mock

import pandas as pd

from engine import backtest_strategy as bs


def _bars(dates, base=100.0):
    """构造带 15:00 时间戳的日 K（与真实 store 一致）。"""
    return pd.DataFrame({
        'trade_time': pd.to_datetime(list(dates)) + pd.Timedelta(hours=15),
        'open':  [base] * len(dates),
        'high':  [base + 1] * len(dates),
        'low':   [base - 1] * len(dates),
        'close': [base] * len(dates),
        'volume': [10000.0] * len(dates),
    })


def _big_df():
    """≥60 根历史 + 缺口：... 2026-06-26 之后直接跳到 2026-07-13。"""
    dates = list(pd.bdate_range('2026-02-02', '2026-06-26')) + \
        [pd.Timestamp('2026-07-13'), pd.Timestamp('2026-07-14')]
    return _bars(dates)


class TestStockHasBarOn(unittest.TestCase):
    def test_true_for_normal_date_with_15h_timestamp(self):
        """15:00 时间戳下，正常交易日必须判为「有 bar」（防误杀）。"""
        df = _big_df()
        self.assertTrue(bs._stock_has_bar_on(df, pd.Timestamp('2026-06-26'), None))
        self.assertTrue(bs._stock_has_bar_on(df, pd.Timestamp('2026-07-13'), None))

    def test_false_for_gap_date(self):
        df = _big_df()
        for d in ('2026-07-07', '2026-07-09', '2026-06-27', '2026-07-10'):
            self.assertFalse(bs._stock_has_bar_on(df, pd.Timestamp(d), None), d)


class TestStaleBarGuard(unittest.TestCase):
    KEY = '__test_no_bar__'

    def setUp(self):
        bs._date_idx_cache.pop(self.KEY, None)

    def tearDown(self):
        bs._date_idx_cache.pop(self.KEY, None)

    def test_gap_date_does_not_reach_scoring(self):
        """缺口日 → 返回 None 且不进入评分（历史充足也会被守卫拦住）。"""
        df = _big_df()
        with mock.patch.object(bs.scoring_core, 'compute_stock_score') as m:
            r = bs._st_compute_full_analysis(df, '2026-07-09', 'code_not_in_cache', 0.5)
        self.assertIsNone(r)
        m.assert_not_called()

    def test_valid_date_reaches_scoring(self):
        """正常交易日 → 守卫放行，进入评分流程（未被误杀）。"""
        df = _big_df()
        with mock.patch.object(bs.scoring_core, 'compute_stock_score',
                               side_effect=RuntimeError('reached')) as m:
            bs._st_compute_full_analysis(df, '2026-06-26', 'code_not_in_cache', 0.5)
        m.assert_called_once()

    def test_gap_date_blocked_via_cache_branch(self):
        """stock_code 在 _date_idx_cache 中但当日无 bar → 同样拦截。"""
        df = _big_df()
        bd = list(pd.bdate_range('2026-02-02', '2026-06-26'))
        idx_map = {pd.Timestamp(d).normalize(): i for i, d in enumerate(bd)}
        bs._date_idx_cache[self.KEY] = idx_map
        with mock.patch.object(bs.scoring_core, 'compute_stock_score') as m:
            r = bs._st_compute_full_analysis(df, '2026-07-09', self.KEY, 0.5)
        self.assertIsNone(r)
        m.assert_not_called()


class TestClampPriceUsesSameDayBar(unittest.TestCase):
    """成交价夹取必须用「当日」bar；15:00 时间戳下不能退化成前一交易日。"""

    def setUp(self):
        self.df = pd.DataFrame({
            'trade_time': pd.to_datetime(['2025-08-28 15:00:00', '2025-08-29 15:00:00']),
            'open':  [19.08, 18.21],
            'high':  [19.49, 18.33],
            'low':   [17.43, 17.62],
            'close': [18.38, 17.87],
            'volume': [261569, 143464],
        })

    def test_absurd_price_clamped_to_same_day_high(self):
        r = bs.StrategyBacktester._clamp_price_to_bar(
            self.df, pd.Timestamp('2025-08-29'), 141.09, None)
        self.assertAlmostEqual(r, 18.33, places=2)   # 当日(8/29)最高，而非 8/28 的 19.49

    def test_price_below_low_clamped_to_same_day_low(self):
        r = bs.StrategyBacktester._clamp_price_to_bar(
            self.df, pd.Timestamp('2025-08-29'), 5.0, None)
        self.assertAlmostEqual(r, 17.62, places=2)

    def test_normal_price_unchanged(self):
        r = bs.StrategyBacktester._clamp_price_to_bar(
            self.df, pd.Timestamp('2025-08-29'), 18.0, None)
        self.assertAlmostEqual(r, 18.0, places=2)

    def test_gap_date_returns_price_unchanged(self):
        r = bs.StrategyBacktester._clamp_price_to_bar(
            self.df, pd.Timestamp('2025-08-30'), 141.09, None)
        self.assertAlmostEqual(r, 141.09, places=2)  # 无当日 bar → 不强夹


if __name__ == '__main__':
    unittest.main()
