#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""加仓仓位上限单测（回归 bug：总仓位上限对加仓失效）。

对应 backtest_strategy.py 的 _clamp_add_invested。

用户决策 2026-08-07：加仓「豁免总仓位上限」、只受「单只上限」约束。本测试锁定：
  - 单只上限仍夹紧加仓金额；
  - 总仓位上限不再夹紧加仓（即便组合已到上限，加仓仍可动用预留现金）；
  - nav<=0 时返回 0。
"""
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.backtest_strategy import StrategyBacktester


class TestClampAddInvested(unittest.TestCase):
    def setUp(self):
        # 不调用 __init__，仅构造实例并设置两个被 _clamp_add_invested 读取的属性
        self.bt = StrategyBacktester.__new__(StrategyBacktester)
        self.bt.max_single_position = 0.10
        self.bt.total_position_cap_pct = 0.50

    def _pos(self, invested):
        return SimpleNamespace(total_invested=invested)

    def test_single_cap_clamps(self):
        # nav=1e6, 持仓 8% (80k)，加仓比例 1.0 想加 80k -> 16% > 10% 上限
        pos = self._pos(80_000)
        out = self.bt._clamp_add_invested(pos, 1.0, 1_000_000)
        # 夹紧后 (80k+out)/1e6 <= 0.10 -> out <= 20k
        self.assertAlmostEqual(out, 20_000.0)

    def test_single_cap_no_clamp_when_room(self):
        pos = self._pos(50_000)  # 5%
        out = self.bt._clamp_add_invested(pos, 0.1, 1_000_000)  # 想加 5k -> 5.5%
        self.assertAlmostEqual(out, 5_000.0)

    def test_exempt_from_total_cap(self):
        # 即便组合已到 50% 总上限，加仓只受单只上限约束（豁免总上限是用户决策）
        self.bt.total_position_cap_pct = 0.50
        pos = self._pos(40_000)  # 4%
        out = self.bt._clamp_add_invested(pos, 0.5, 1_000_000)  # 想加 20k -> 6%
        self.assertAlmostEqual(out, 20_000.0)

    def test_zero_nav(self):
        self.assertEqual(self.bt._clamp_add_invested(self._pos(10_000), 1.0, 0), 0.0)


if __name__ == '__main__':
    unittest.main()
