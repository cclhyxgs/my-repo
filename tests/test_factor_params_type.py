#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""因子参数类型守卫单测（回归 bug：JSON 把 period 存成 14.0(float)，底层指标函数
需 int 切片/range 运算，异常被静默吞掉后因子 raw=0、z-score 误判）。

对应 engine/factor_registry.py 的 calc_factor_value：整数值 float 参数需强转 int。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
from engine.factor_registry import calc_factor_value


def make_ctx(n=120, uptrend=True, seed=42):
    np.random.seed(seed)
    idx = pd.date_range('2024-01-01', periods=n, freq='D')
    drift = np.linspace(0, 30.0 if uptrend else -30.0, n)
    close = 100 + drift + np.cumsum(np.random.randn(n) * 0.5)
    high = close + np.abs(np.random.randn(n)) * 0.8
    low = close - np.abs(np.random.randn(n)) * 0.8
    op = close + np.random.randn(n) * 0.3
    vol = 1e6 + np.random.randn(n) * 1e5
    return {
        'closes': close.tolist(),
        'highs': high.tolist(),
        'lows': low.tolist(),
        'volumes': vol.tolist(),
        'data_list': [
            {'open': o, 'high': h, 'low': l, 'close': c, 'volume': v}
            for o, h, l, c, v in zip(op, high, low, close, vol)
        ],
    }


class TestFactorParamsType(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()

    def test_float_period_equals_int(self):
        # 回归核心：float 参数经强转后必须与 int 参数结果一致；否则底层切片/range
        # 用 float 会抛 TypeError 被 try/except 吞掉返回 0.0，z-score 直接失真。
        for name in ['ma_arrangement', 'rsi_value', 'macd_hist_norm', 'adx_trend_strength']:
            v_int = calc_factor_value(name, self.ctx, {'period': 20})
            v_float = calc_factor_value(name, self.ctx, {'period': 20.0})
            self.assertTrue(np.isfinite(v_int), f"{name}: int period 返回非有限值")
            self.assertAlmostEqual(
                v_int, v_float, places=9,
                msg=f"{name}: float period 结果与 int 不一致（整数参数强转回归）",
            )

    def test_float_period_nonzero_on_trend(self):
        # 强趋势下均线排列因子应有非零有效值（回归：float period 不再让 raw=0）
        v = calc_factor_value('ma_arrangement', make_ctx(uptrend=True), {'period': 14.0})
        self.assertTrue(np.isfinite(v))
        self.assertNotEqual(v, 0.0)

    def test_missing_factor_returns_zero(self):
        self.assertEqual(calc_factor_value('not_a_real_factor', self.ctx, {}), 0.0)


if __name__ == '__main__':
    unittest.main()
