#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""加仓引擎单测：触发阈值 price 必须随 triggered_tiers 返回，且 add_ratio 受 max 约束。

对应 engine/add_engine.py。通过 monkeypatch quant_config.get_add_params 注入已知配置，
避免依赖磁盘上的真实方案文件（也避免触发缓存写盘）。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.quant_config as qc
from engine.add_engine import AddEngine

ADD_CFG = {
    'max_add_ratio': 0.30, 'vol_mult': 2.0,
    'type_to_row': {}, 'default_row': 'strong_standard',
    'add_tiers': {
        'strong_standard': {
            't1': {'triggers': ['MA5'], 'ratio': 0.2},
            't2': {'triggers': ['PREV_HIGH'], 'ratio': 0.2},
            't3': {'triggers': ['MACD_GOLD'], 'ratio': 0.2},
        }
    }
}


class Ctx:
    final_score = 80.0
    stock_type = 'strong'
    market = {'ma_arrangement': '多头'}
    latest_price = 12.5
    entry_price = 10.0
    has_position = True
    stock_score = 80.0
    up_ratio = 0.6
    has_real_veto = False
    adx_state = {}
    tech = {
        'sma_5': 12.0, 'sma_10': 11.5, 'sma_20': 11.0, 'rsi': 55.0, 'macd_hist': 0.05,
        'kdj_signal': '金叉', 'kdj_k': 60.0, 'adx': 30.0,
        'obv_data': {'obv_breakout': True}, 'breakout_signal': '触及上轨',
    }
    data_list = [{'open': 12.0, 'high': 12.3, 'low': 11.8, 'close': 12.1, 'volume': 1e6} for _ in range(25)]


class TestAddEngine(unittest.TestCase):
    def setUp(self):
        self._p = mock.patch.object(qc, 'get_add_params', lambda: ADD_CFG)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_triggered_tiers_carries_price(self):
        res = AddEngine.suggest_add(Ctx())
        self.assertTrue(res['can_add'])
        prices = [t['price'] for t in res['triggered_tiers'] if t.get('price', 0)]
        self.assertTrue(prices, "triggered_tiers 必须携带触发阈值 price")
        self.assertIn(12.0, prices)  # MA5=12.0 为价阈值触发

    def test_add_ratio_capped_by_max(self):
        res = AddEngine.suggest_add(Ctx())
        self.assertLessEqual(res['add_ratio'], ADD_CFG['max_add_ratio'])

    def test_indicator_trigger_has_zero_price(self):
        res = AddEngine.suggest_add(Ctx())
        macd = [t for t in res['triggered_tiers'] if 'MACD' in t['label']]
        if macd:
            self.assertEqual(macd[0]['price'], 0.0)  # 指标类触发无价格阈值


if __name__ == '__main__':
    unittest.main()
