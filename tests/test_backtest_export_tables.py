# -*- coding: utf-8 -*-
"""回归：三张回测产物表（持仓明细 / 交易流水 / 净值曲线）按人工阅读口径重建。

字段口径（用户 2026-09-19 需求）：标的、触发信号、信号触发价、仓位、买卖日期、
买卖价格、买卖数量、盈亏；去掉佣金/印花税/过户费/滑点/calc_check 等引擎内部列
（它们保留在 *_trades.json 里）。
"""
import unittest

import pandas as pd

from engine.backtest_strategy import (
    _build_trade_rows, _build_fill_rows, _build_equity_rows,
    _fill_stats, _pct_from_reason,
)

_NAME = lambda c: {'sh600000': '浦发银行'}.get(c, '')


class _P:
    """Position 的最小替身（只带导出需要的属性）。"""
    def __init__(self, **kw):
        self.stock_code = 'sh600000'
        self.stock_type = 'standard'
        self.entry_action = '关注建仓'
        self.entry_date = pd.Timestamp('2026-01-05')
        self.exit_date = pd.Timestamp('2026-01-20')
        self.entry_price = 10.0
        self.exit_price = 9.0
        self.shares = 0.0
        self.initial_shares = 0.0
        self.total_invested = 0.0
        self.total_recovered = 0.0
        self.total_fees = 0.0
        self.total_slippage_cost = 0.0
        self.pnl_pct = 0.0
        self.nav_pnl_pct = 0.0
        self.bars_held = 0
        self.exit_reason = ''
        self.entry_weight = 0.0
        self.entry_weight_raw = 0.0
        self.max_pnl = 0.0
        self.min_pnl = 0.0
        self.filled_records = []
        self.add_history = []
        self.reduce_history = []
        self.risk_meta = {}
        self.__dict__.update(kw)


def _fills():
    return [
        {'date': '2026-01-05', 'action': 'buy', 'reason': '建仓',
         'price': 10.05, 'price_raw': 10.0, 'shares': 1000.0, 'amount': 10050.0},
        {'date': '2026-01-08', 'action': 'buy', 'reason': '加仓(25%)',
         'price': 12.06, 'price_raw': 12.0, 'shares': 500.0, 'amount': 6030.0},
        {'date': '2026-01-15', 'action': 'sell', 'reason': '减仓30%',
         'price': 10.95, 'price_raw': 11.0, 'shares': 500.0, 'amount': 5475.0},
        {'date': '2026-01-20', 'action': 'sell', 'reason': '清仓: 🔴 触发清仓档，立即清仓',
         'price': 8.96, 'price_raw': 9.0, 'shares': 1000.0, 'amount': 8960.0},
    ]


def _pos():
    return _P(shares=1000.0, initial_shares=1000.0, entry_weight=0.12, entry_weight_raw=0.15,
              total_invested=16080.0, total_recovered=14435.0, total_fees=12.0,
              pnl_pct=-9.375, bars_held=11, exit_reason='清仓: 🔴 触发清仓档，立即清仓',
              filled_records=_fills(), add_history=[{}], reduce_history=[{}, {}])


class TestPctFromReason(unittest.TestCase):
    def test_parses_engine_formats(self):
        self.assertEqual(_pct_from_reason('加仓(25%)'), 25.0)
        self.assertEqual(_pct_from_reason('减仓30%'), 30.0)

    def test_none_when_absent(self):
        self.assertIsNone(_pct_from_reason('清仓: 🔴 触发清仓档，立即清仓'))
        self.assertIsNone(_pct_from_reason(''))
        self.assertIsNone(_pct_from_reason(None))


class TestFillStats(unittest.TestCase):
    def test_share_breakdown(self):
        s = _fill_stats(_pos())
        self.assertEqual(s['init'], 1000.0)
        self.assertEqual(s['add'], 500.0)      # 买入合计 1500 − 建仓 1000
        self.assertEqual(s['reduce'], 500.0)   # 卖出合计 1500 − 平仓 1000
        self.assertEqual(s['close'], 1000.0)


class TestTradeRows(unittest.TestCase):
    def test_fields_and_no_internal_columns(self):
        rows = _build_trade_rows([_pos()], _NAME)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r['股票名称'], '浦发银行')
        self.assertEqual(r['建仓股数'], 1000.0)
        self.assertEqual(r['加仓股数'], 500.0)
        self.assertEqual(r['减仓股数'], 500.0)
        self.assertEqual(r['平仓股数'], 1000.0)
        self.assertEqual(r['信号仓位%'], 15.0)
        self.assertEqual(r['实际仓位%'], 12.0)
        self.assertEqual(r['盈亏金额'], round(14435.0 - 16080.0, 2))
        self.assertEqual(r['收益率%'], -9.38)
        # 引擎内部列不得出现在人读表
        for k in ('佣金', '印花税', '过户费', '滑点损失', 'calc_check_pass',
                  '风控触发时峰值净值', '当前持仓股数'):
            self.assertNotIn(k, r)

    def test_skips_zero_share_positions(self):
        self.assertEqual(_build_trade_rows([_P(shares=0.0, total_invested=100.0)], _NAME), [])
        self.assertEqual(_build_trade_rows([], _NAME), [])


class TestFillRows(unittest.TestCase):
    def test_sequence_and_signal(self):
        rows = _build_fill_rows([_pos()], _NAME)
        self.assertEqual([r['方向'] for r in rows], ['买入', '买入', '卖出', '卖出'])
        self.assertEqual(rows[0]['触发信号'], '关注建仓')
        self.assertEqual(rows[1]['触发信号'], '加仓')
        self.assertEqual(rows[2]['触发信号'], '减仓30%')
        self.assertEqual(rows[3]['触发信号'].startswith('清仓'), True)
        self.assertEqual(rows[0]['信号触发价'], 10.0)   # price_raw
        self.assertEqual(rows[0]['成交价'], 10.05)      # price（含滑点）

    def test_ratio_column(self):
        rows = _build_fill_rows([_pos()], _NAME)
        self.assertEqual(rows[0]['本次比例%'], 12.0)    # 首次建仓 = 实际执行仓位
        self.assertEqual(rows[1]['本次比例%'], 25.0)    # 从 '加仓(25%)' 解析
        self.assertEqual(rows[2]['本次比例%'], 33.33)   # 500 / 1500
        self.assertEqual(rows[3]['本次比例%'], 100.0)   # 全部剩余

    def test_realized_pnl_moving_average(self):
        rows = _build_fill_rows([_pos()], _NAME)
        self.assertEqual(rows[0]['已实现盈亏'], '')
        self.assertEqual(rows[1]['已实现盈亏'], '')
        # 第 1 笔卖出：均价 = (10050+6030)/1500 = 10.72 → (10.95−10.72)×500
        avg1 = (10050.0 + 6030.0) / 1500.0
        self.assertAlmostEqual(rows[2]['已实现盈亏'], round((10.95 - avg1) * 500, 2), places=2)
        self.assertAlmostEqual(rows[2]['已实现盈亏%'], round((10.95 / avg1 - 1) * 100, 2), places=2)
        # 第 2 笔卖出仍按同一均价
        self.assertAlmostEqual(rows[3]['已实现盈亏'], round((8.96 - avg1) * 1000, 2), places=2)

    def test_last_row_carries_position_pnl_and_exit_reason(self):
        rows = _build_fill_rows([_pos()], _NAME)
        self.assertEqual(rows[0]['平仓盈亏%'], '')
        self.assertEqual(rows[3]['平仓盈亏%'], -9.38)
        self.assertTrue(rows[3]['平仓原因'])

    def test_no_fills_skipped(self):
        self.assertEqual(_build_fill_rows([_P(filled_records=[])], _NAME), [])


class TestEquityRows(unittest.TestCase):
    def test_conversions(self):
        curve = [
            {'date': '2026-01-05', 'nav': 1000000.0, 'cash': 500000.0,
             'positions_value': 500000.0, 'daily_return': 0.0, 'drawdown': 0.0, 'open_positions': 5},
            {'date': '2026-01-06', 'nav': 1100000.0, 'cash': 400000.0,
             'positions_value': 700000.0, 'daily_return': 0.1, 'drawdown': 0.0, 'open_positions': 6},
            {'date': '2026-01-07', 'nav': 1045000.0, 'cash': 400000.0,
             'positions_value': 645000.0, 'daily_return': -0.05, 'drawdown': 0.05, 'open_positions': 6},
        ]
        rows = _build_equity_rows(curve, 1000000.0)
        self.assertEqual(list(rows[0].keys()),
                         ['日期', '净值', '当日收益率%', '累计收益率%', '回撤%',
                          '持仓市值', '现金', '持仓数'])
        self.assertEqual(rows[0]['累计收益率%'], 0.0)
        self.assertEqual(rows[1]['当日收益率%'], 10.0)
        self.assertEqual(rows[1]['累计收益率%'], 10.0)
        self.assertEqual(rows[2]['回撤%'], 5.0)
        self.assertEqual(rows[2]['持仓数'], 6)

    def test_empty_and_zero_capital(self):
        self.assertEqual(_build_equity_rows([], 1000000.0), [])
        rows = _build_equity_rows([{'date': '2026-01-05', 'nav': 100.0}], 0)
        self.assertEqual(rows[0]['累计收益率%'], '')


if __name__ == '__main__':
    unittest.main()
