# -*- coding: utf-8 -*-
"""回归：回测数据入口统一 trade_time 口径 = 【T 日收盘决策、T+1 开盘成交】。

背景（2026-09-19）：store 里日 K 的 trade_time 两种口径混用（`qfq_daily_sh688277.pkl` 带
15:00:00、`qfq_daily_sh688072.pkl` 是 00:00:00），导致 `searchsorted(date_ts, ...)` 的解析
随口径漂移一天：15:00 口径会用到「前一日收盘」做决策与估值，且与 `_date_idx_cache` 分支
（含当日 bar）结果不一致 —— 回测结果随 symbol 漂移、且 cache 命中与否会静默位移。

修复：`engine/backtest.normalize_daily_dates()` 在 `_validate_qfq`（回测数据唯一入口）
把 trade_time 归一到零点；`_st_compute_full_analysis` 改为单一确定性 searchsorted 口径。
"""
import unittest
from unittest import mock

import pandas as pd

from engine import backtest as bt
from engine import backtest_strategy as bs


def _df(n=80, hour=None, base_close=100.0, base_open=200.0):
    """n 根日 K；close/open 各不相同，便于断言「取的是哪一天」。"""
    dates = pd.bdate_range('2026-01-05', periods=n)
    tt = pd.to_datetime(list(dates)) + (pd.Timedelta(hours=hour) if hour is not None else pd.Timedelta(0))
    return pd.DataFrame({
        'trade_time': tt,
        'open':  [base_open + i for i in range(n)],
        'high':  [base_close + i + 2 for i in range(n)],
        'low':   [base_close + i - 2 for i in range(n)],
        'close': [base_close + i for i in range(n)],
        'volume': [10000.0] * n,
    })


class TestNormalizeDailyDates(unittest.TestCase):
    def test_strips_time_component(self):
        df = _df(5, hour=15)
        bt.normalize_daily_dates(df)
        self.assertTrue((df['trade_time'].dt.hour == 0).all())
        self.assertTrue((df['trade_time'].dt.minute == 0).all())

    def test_idempotent_and_tolerates_bad_input(self):
        df = _df(3, hour=0)
        before = df['trade_time'].tolist()
        bt.normalize_daily_dates(df)
        self.assertEqual(before, df['trade_time'].tolist())
        bt.normalize_daily_dates(None)          # 不应抛异常
        bt.normalize_daily_dates(pd.DataFrame({'x': [1]}))  # 无 trade_time 列


class TestValidateQfqNormalizes(unittest.TestCase):
    def test_backtest_validate_qfq_normalizes(self):
        ok, vdf, reason = bt._validate_qfq(_df(30, hour=15))
        self.assertTrue(ok)
        self.assertTrue((vdf['trade_time'].dt.hour == 0).all())

    def test_strategy_validate_qfq_normalizes(self):
        ok, vdf, reason = bs._validate_qfq(_df(30, hour=15))
        self.assertTrue(ok)
        self.assertTrue((vdf['trade_time'].dt.hour == 0).all())

    def test_both_conventions_converge(self):
        """15:00 与 00:00 两种口径归一后必须完全一致（消除 symbol 间漂移）。"""
        _, v15, _ = bs._validate_qfq(_df(30, hour=15))
        _, v00, _ = bs._validate_qfq(_df(30, hour=0))
        self.assertEqual(v15['trade_time'].tolist(), v00['trade_time'].tolist())


class TestDecisionUsesSameDayClose(unittest.TestCase):
    def _captured_closes(self, df, date):
        with mock.patch.object(bs.scoring_core, 'compute_stock_score',
                               side_effect=RuntimeError('reached')) as m:
            bs._st_compute_full_analysis(df, date, 'code_x', 0.5)
        return m.call_args[0][0]

    def test_close_cutoff_includes_same_day_bar(self):
        """决策收盘序列必须包含「当日」bar（T 收盘决策）。"""
        df = _df(80, hour=15)
        _, vdf, _ = bs._validate_qfq(df)          # 走真实入口（会归一）
        d = vdf.iloc[69]['trade_time']            # 第 70 根
        closes = self._captured_closes(vdf, d)
        self.assertEqual(closes[-1], 100.0 + 69)  # 当日收盘，而非前一日

    def test_next_open_is_t_plus_1(self):
        """成交价必须是「次日开盘」（T+1 开盘成交）。"""
        df = _df(80, hour=15)
        _, vdf, _ = bs._validate_qfq(df)
        d = vdf.iloc[69]['trade_time']
        price, nd = bs._get_next_open_price(vdf, d, None)
        self.assertEqual(price, 200.0 + 70)       # 第 71 根的开盘价
        self.assertEqual(pd.Timestamp(nd).normalize(), vdf.iloc[70]['trade_time'])


if __name__ == '__main__':
    unittest.main()
