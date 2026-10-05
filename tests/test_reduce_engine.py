#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""减仓引擎单测：triggered_tiers 必须携带触发阈值 price（清仓档 ratio=1.0）。

对应 engine/reduce_engine.py。通过 monkeypatch quant_config.get_reduce_params 注入已知配置。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.quant_config as qc
from engine.reduce_engine import ReduceEngine

RED_CFG = {
    'max_add_ratio': 0.30, 'vol_mult': 2.0, 'atr_mult': 0.5,
    'recent_low_mult': 0.99, 'lookback_days': 20, 'atr_fallback': 0.02,
    'type_to_row': {}, 'default_row': 'strong_standard',
    'reduce_tiers': {
        'strong_standard': {
            't1': {'triggers': ['MA20'], 'ratio': 0.2, 'action': 'reduce'},
            't2': {'triggers': ['MA10'], 'ratio': 0.3, 'action': 'reduce'},
            't3': {'triggers': ['DROP_PCT'], 'ratio': 1.0, 'action': 'clear',
                   'params': {'DROP_PCT': 0.03}},
        }
    }
}


class RCtx:
    latest_price = 9.0
    entry_price = 10.0
    stock_type = 'strong'
    has_position = True
    tech = {
        'sma_5': 9.2, 'sma_10': 9.5, 'sma_20': 11.0, 'rsi': 35.0,
        'macd_hist': -0.03, 'atr': 0.3,
    }
    data_list = [{'high': 10.5, 'low': 8.8, 'close': 9.0, 'volume': 1e6} for _ in range(25)]


class TestReduceEngine(unittest.TestCase):
    def setUp(self):
        self._p = mock.patch.object(qc, 'get_reduce_params', lambda: RED_CFG)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_triggered_tiers_carries_price(self):
        res = ReduceEngine.suggest_reduce(RCtx())
        self.assertGreater(res['reduce_ratio'], 0)
        for t in res['triggered_tiers']:
            self.assertIn('price', t)
        # MA20=11.0，当前价 9.0 <= 11.0 触发，阈值价 = 11.0
        self.assertEqual(res['triggered_tiers'][0]['price'], 11.0)

    def test_clear_tier_ratio_is_one(self):
        res = ReduceEngine.suggest_reduce(RCtx())
        clear = [t for t in res['triggered_tiers'] if t['action'] == '清仓']
        self.assertTrue(clear)
        self.assertEqual(clear[0]['ratio'], 1.0)


if __name__ == '__main__':
    unittest.main()
