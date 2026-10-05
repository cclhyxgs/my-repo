# -*- coding: utf-8 -*-
"""回归：减仓引擎 ATR 移动止损不得用「全量历史」极值当持仓最高点。

背景（2026-09-19 用户实测）：回测交易明细里 sh688277 在 2025-08-29 出现成交价
140.38（当日真实区间仅 17.62~18.33），导致该笔收益被算成 +208%。
根因：TDX 接入后统一 store 含全历史，减仓引擎 ATR 分支用 max(全量 data_list 的 close)
当「持仓最高点」→ 取到 2021 年高位 141.6 → 止损价 141.09。
修复：改用「近 lookback 日」极值（recent_high/recent_low）。
"""
import unittest
from unittest import mock

import pandas as pd

from engine import quant_config
from engine.reduce_engine import ReduceEngine


def _ctx(latest_price, entry_price, data_list, tech=None):
    return type('obj', (), {
        'latest_price': latest_price,
        'entry_price': entry_price,
        'tech': tech or {'atr': 1.03, 'sma_5': 19.6, 'sma_10': 19.5,
                         'sma_20': 19.0, 'rsi': 40.0, 'macd_hist': -0.1},
        'data_list': data_list,
        'stock_type': 'strong',
        'has_position': True,
    })()


def _bars(rows):
    return [{'open': o, 'high': h, 'low': l, 'close': c, 'volume': v, 'date': d}
            for (d, o, h, l, c, v) in rows]


_PARAMS = {
    'reduce_tiers': {
        'strong_standard': {
            't1': {'triggers': ['ATR_LOWER'], 'action': 'reduce', 'ratio': 0.5},
        }
    },
    'type_to_row': {'strong': 'strong_standard'},
    'default_row': 'strong_standard',
    'atr_mult': 0.5,
    'lookback_days': 20,
    'recent_low_mult': 0.99,
}


class TestReduceAtrTrailing(unittest.TestCase):
    def _run(self, ctx):
        with mock.patch.object(quant_config, 'get_reduce_params', return_value=_PARAMS):
            return ReduceEngine.suggest_reduce(ctx)

    def _data_with_old_spike(self):
        # 前 40 根：多年前的高位（含 141.6）
        # 后 20 根：近期真实价格 ~18-21
        old = [(f'2021-01-{i % 28 + 1:02d}', 140, 141.6, 139, 141.0, 1000) for i in range(40)]
        recent = [(f'2025-08-{i + 1:02d}', 19.0, 20.85 if i == 5 else 20.0,
                   18.5, 19.0 + (i % 3) * 0.3, 1000) for i in range(20)]
        return _bars(old + recent)

    def test_atr_trailing_ignores_distant_history_high(self):
        """有多年以前的高位时，触发价必须落在近期价格区间附近（旧实现会给出 ~141）。"""
        ctx = _ctx(latest_price=18.38, entry_price=19.6,
                   data_list=self._data_with_old_spike())
        r = self._run(ctx)
        tt = r.get('triggered_tiers') or []
        self.assertTrue(tt, 'ATR_LOWER 应触发（现价已跌破移动止损）')
        price = tt[0]['price']
        self.assertLess(price, 30, f'触发价被历史高位污染: {price}')
        self.assertAlmostEqual(price, 20.34, delta=0.6)

    def test_no_trigger_when_price_above_trailing(self):
        """现价高于移动止损时不触发。"""
        ctx = _ctx(latest_price=25.0, entry_price=19.6,
                   data_list=self._data_with_old_spike())
        r = self._run(ctx)
        self.assertFalse(r.get('triggered_tiers'))


class TestClampPriceToBar(unittest.TestCase):
    def _df(self):
        return pd.DataFrame({
            'trade_time': pd.to_datetime(['2025-08-28', '2025-08-29']),
            'open':  [19.08, 18.21],
            'high':  [19.49, 18.33],
            'low':   [17.43, 17.62],
            'close': [18.38, 17.87],
            'volume': [261569, 143464],
        })

    def _clamp(self, price):
        from engine.backtest_strategy import StrategyBacktester
        return StrategyBacktester._clamp_price_to_bar(
            self._df(), pd.Timestamp('2025-08-29'), price, None)

    def test_absurd_price_clamped_to_bar_high(self):
        self.assertAlmostEqual(self._clamp(141.09), 18.33, places=2)

    def test_price_below_low_clamped_to_low(self):
        self.assertAlmostEqual(self._clamp(5.0), 17.62, places=2)

    def test_normal_price_unchanged(self):
        self.assertAlmostEqual(self._clamp(18.0), 18.0, places=2)


if __name__ == '__main__':
    unittest.main()
