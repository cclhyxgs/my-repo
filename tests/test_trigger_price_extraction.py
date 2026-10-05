#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""触发价提取器单测（回归 bug：加仓触发价误读 triggered 而非 triggered_tiers）。

这些静态方法把引擎返回的「触发阈值」作为回测成交基准价。对应 backtest_strategy.py
的 _extract_reduce_trigger_price / _extract_add_trigger_price。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.backtest_strategy import StrategyBacktester


class TestReduceTriggerPrice(unittest.TestCase):
    def test_empty_triggered_tiers(self):
        self.assertIsNone(StrategyBacktester._extract_reduce_trigger_price({'triggered_tiers': []}))

    def test_none_result(self):
        self.assertIsNone(StrategyBacktester._extract_reduce_trigger_price(None))

    def test_uses_triggered_tiers_price(self):
        res = {'triggered_tiers': [
            {'price': 12.3, 'label': 'MA20', 'action': '减仓20%', 'ratio': 0.2}]}
        self.assertEqual(StrategyBacktester._extract_reduce_trigger_price(res), 12.3)

    def test_skips_zero_price_picks_next(self):
        res = {'triggered_tiers': [
            {'price': 0.0, 'label': 'MACD'}, {'price': 9.5, 'label': '前低'}]}
        self.assertEqual(StrategyBacktester._extract_reduce_trigger_price(res), 9.5)

    def test_legacy_triggered_key_ignored(self):
        # 回归：必须读 triggered_tiers，不能读旧的 'triggered' 键，否则永远回落收盘价
        res = {'triggered': [{'price': 99.9}]}
        self.assertIsNone(StrategyBacktester._extract_reduce_trigger_price(res))


class TestAddTriggerPrice(unittest.TestCase):
    def test_uses_triggered_tiers(self):
        res = {'triggered_tiers': [
            {'price': 12.0, 'label': 'MA5', 'ratio': 0.2}]}
        self.assertEqual(StrategyBacktester._extract_add_trigger_price(res), 12.0)

    def test_legacy_triggered_key_ignored(self):
        # 回归：加仓引擎返回字段名是 triggered_tiers，不是 triggered
        res = {'triggered': [{'price': 5.0}]}
        self.assertIsNone(StrategyBacktester._extract_add_trigger_price(res))

    def test_zero_price_returns_none(self):
        res = {'triggered_tiers': [{'price': 0.0}]}
        self.assertIsNone(StrategyBacktester._extract_add_trigger_price(res))


if __name__ == '__main__':
    unittest.main()
