# -*- coding: utf-8 -*-
"""回归：条件单执行模型（卖出向下破位 / 加仓向上突破）+ 印花税现行税率。

背景（2026-09-20 用户拍板）：实盘的卖出是**挂止损单**（价格跌破 X 才成交），加仓是
**挂突破单**（涨破 X 才买），都不该是"次日开盘无条件市价"。原先两种口径都不对：
  - `same_close`：把 X 夹进当日 [L,H] ⇒ 等价于假设"能卖在当日最高价"（偏乐观，且是
    用 T 日收盘算出的 X 在 T 日成交 ⇒ 轻度未来函数）；
  - `next_open` ：无条件以 T+1 开盘成交 ⇒ **丢失触发价语义**（438 笔实测里 5% 的信号
    实盘根本不会触发，却被强行卖掉；高开时还按开盘价卖出，卖在了你并不想卖的价位）。

本文件锁住修正后的行为：
  卖出 `_try_sell_with_limit_check(trigger_price=X)`：
      `low <= X` → 成交价 = **min(X, open)**（跳空低开穿透）；`low > X` → **不成交(None)**。
  加仓 `_try_add_conditional(trigger_price=X)`：
      `high >= X` → 成交价 = **max(X, open)**；`high < X` → 不成交；涨停买不到。
"""
import os
import unittest

import pandas as pd

from engine.backtest_strategy import (
    _try_sell_with_limit_check, _try_add_conditional,
    calculate_trading_cost, STAMP_DUTY_RATE, COMMISSION_RATE, TRANSFER_FEE_RATE,
)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _mk_df(rows):
    """rows: [(date, open, high, low, close), ...]"""
    return pd.DataFrame({
        'trade_time': pd.to_datetime([r[0] for r in rows]),
        'open': [float(r[1]) for r in rows],
        'high': [float(r[2]) for r in rows],
        'low': [float(r[3]) for r in rows],
        'close': [float(r[4]) for r in rows],
        'volume': [1_000_000.0] * len(rows),
    })


# 信号日 T = 2026-01-05（收盘 10.1），T+1 = 2026-01-06
_PRE = [
    ('2026-01-02', 10.0, 10.2, 9.9, 10.0),
    ('2026-01-05', 10.0, 10.3, 9.8, 10.1),
]


def _df_with_next(next_bar):
    return _mk_df(_PRE + [next_bar])


class TestSellConditional(unittest.TestCase):
    """卖出条件单（向下触发）。"""

    def test_gap_down_uses_open(self):
        """跳空低开（open < X）→ 按开盘价成交 = 穿透，不给虚假好价。"""
        df = _df_with_next(('2026-01-06', 9.0, 9.5, 8.8, 9.2))
        px, date, *_ = _try_sell_with_limit_check(df, '2026-01-05', trigger_price=9.6)
        self.assertEqual(px, 9.0)                       # min(9.6, 9.0) = 9.0
        self.assertEqual(str(date)[:10], '2026-01-06')

    def test_high_open_but_touched_uses_trigger(self):
        """高开但盘中回落触及 X → 以 X 成交（不是开盘价）。"""
        df = _df_with_next(('2026-01-06', 10.5, 10.8, 9.9, 10.0))
        px, *_ = _try_sell_with_limit_check(df, '2026-01-05', trigger_price=10.0)
        self.assertEqual(px, 10.0)                      # min(10.0, 10.5) = 10.0

    def test_not_touched_means_no_fill(self):
        """未跌破 X → 不成交（实盘止损单不触发）。这是与旧口径最本质的差别。"""
        df = _df_with_next(('2026-01-06', 11.0, 11.2, 10.5, 11.0))
        px, date, *_ = _try_sell_with_limit_check(df, '2026-01-05', trigger_price=10.0)
        self.assertIsNone(px)
        self.assertIsNone(date)

    def test_unconditional_mode_still_uses_open(self):
        """trigger_price=None（时间止损 / 跌停降级）→ 无条件次日开盘。"""
        df = _df_with_next(('2026-01-06', 11.0, 11.2, 10.5, 11.0))
        px, *_ = _try_sell_with_limit_check(df, '2026-01-05', trigger_price=None)
        self.assertEqual(px, 11.0)

    def test_invalid_trigger_degrades_to_unconditional(self):
        """X 为 0/负/非数（脏数据）→ 退化为无条件，不能因脏数据卡死卖出。"""
        df = _df_with_next(('2026-01-06', 10.5, 10.8, 10.2, 10.6))
        for bad in (0, -1, 'abc', None):
            px, *_ = _try_sell_with_limit_check(df, '2026-01-05', trigger_price=bad)
            self.assertEqual(px, 10.5, f'X={bad!r} 应退化为开盘价成交')

    def test_limit_down_is_delayed(self):
        """T+1 跌停卖不出 → 顺延到下一个非跌停日；条件单仍是「触及才成交」。"""
        # 10.1 → 9.09 跌停（close 恰好 -10%，且 open==low 封死）
        df = _mk_df(_PRE + [
            ('2026-01-06', 9.09, 9.09, 9.09, 9.09),      # 跌停
            ('2026-01-07', 8.80, 9.00, 8.50, 8.90),      # 可交易；low 8.50 <= X
        ])
        px, date, was_delayed, delay_days, forced = _try_sell_with_limit_check(
            df, '2026-01-05', trigger_price=8.60)
        self.assertEqual(str(date)[:10], '2026-01-07')
        self.assertTrue(was_delayed)
        self.assertEqual(delay_days, 1)
        self.assertEqual(px, 8.60)                       # min(8.60, 8.80) = 8.60

    def test_delayed_day_not_touched_no_fill(self):
        """顺延到的那天若未触及 X → 同样不成交。"""
        df = _mk_df(_PRE + [
            ('2026-01-06', 9.09, 9.09, 9.09, 9.09),      # 跌停
            ('2026-01-07', 9.60, 9.90, 9.50, 9.80),      # low 9.50 > X
        ])
        px, *_ = _try_sell_with_limit_check(df, '2026-01-05', trigger_price=9.00)
        self.assertIsNone(px)


class TestAddConditional(unittest.TestCase):
    """加仓条件单（向上突破）。"""

    def test_breakout_uses_trigger(self):
        df = _df_with_next(('2026-01-06', 10.0, 10.6, 9.9, 10.5))
        px, date = _try_add_conditional(df, '2026-01-05', 10.5)
        self.assertEqual(px, 10.5)                       # max(10.5, 10.0) = 10.5
        self.assertEqual(str(date)[:10], '2026-01-06')

    def test_gap_up_uses_open(self):
        """跳空高开（open > X）→ 按开盘价成交（穿透，不给虚假低价）。"""
        df = _df_with_next(('2026-01-06', 11.0, 11.5, 10.9, 11.2))
        px, _ = _try_add_conditional(df, '2026-01-05', 10.6)
        self.assertEqual(px, 11.0)                       # max(10.6, 11.0) = 11.0

    def test_no_breakout_means_no_fill(self):
        df = _df_with_next(('2026-01-06', 10.0, 10.6, 9.9, 10.5))
        px, date = _try_add_conditional(df, '2026-01-05', 11.0)
        self.assertIsNone(px)
        self.assertIsNone(date)

    def test_limit_up_cannot_buy(self):
        """T+1 涨停买不到（无卖方挂单）→ 不成交。"""
        # 10.1 → 11.11 涨停（+10%，open==high 封死）
        df = _df_with_next(('2026-01-06', 11.11, 11.11, 11.11, 11.11))
        px, _ = _try_add_conditional(df, '2026-01-05', 11.00)
        self.assertIsNone(px)

    def test_no_trigger_degrades_to_open(self):
        df = _df_with_next(('2026-01-06', 10.4, 10.8, 10.2, 10.6))
        px, _ = _try_add_conditional(df, '2026-01-05', None)
        self.assertEqual(px, 10.4)


class TestStampDuty(unittest.TestCase):
    """印花税：法定千1，2023-08-28 起减半为万5（仅卖出）。"""

    def test_rate_is_halved_value(self):
        self.assertAlmostEqual(STAMP_DUTY_RATE, 0.0005, places=8)

    def test_sell_cost_includes_halved_stamp_duty(self):
        c = calculate_trading_cost(100000, 'sh600519', is_buy=False)
        self.assertAlmostEqual(c['stamp_duty'], 50.0, places=2)      # 10万 × 万5
        self.assertAlmostEqual(c['commission'], 25.0, places=2)      # 万2.5
        self.assertAlmostEqual(c['transfer_fee'], 1.0, places=2)     # 万0.1（沪市）
        self.assertAlmostEqual(c['total'], 76.0, places=2)

    def test_buy_has_no_stamp_duty(self):
        c = calculate_trading_cost(100000, 'sh600519', is_buy=True)
        self.assertEqual(c['stamp_duty'], 0)

    def test_sz_has_no_transfer_fee(self):
        c = calculate_trading_cost(100000, 'sz000001', is_buy=False)
        self.assertEqual(c['transfer_fee'], 0)

    def test_rates_are_consistent(self):
        """常量之间不应互相冲突（防手改成倒挂）。"""
        self.assertLess(STAMP_DUTY_RATE, 0.001)
        self.assertLess(COMMISSION_RATE, 0.001)
        self.assertLess(TRANSFER_FEE_RATE, COMMISSION_RATE)


class TestWiring(unittest.TestCase):
    """源码守卫：四个卖出点里三处传触发价、时间止损不传；加仓走条件单。"""

    def setUp(self):
        with open(os.path.join(_ROOT, 'engine/backtest_strategy.py'), encoding='utf-8') as f:
            self.src = f.read()

    def test_sell_call_sites_pass_trigger(self):
        self.assertEqual(self.src.count('trigger_price=_tp_arg'), 3)   # 清仓/减仓/单笔止损
        self.assertEqual(self.src.count('_try_sell_with_limit_check(\n                            df, date, stock_code=pos.stock_code)'), 1)  # 时间止损

    def test_add_uses_conditional_helper(self):
        self.assertIn("_try_add_conditional(df, date, tp_add,", self.src)
        self.assertIn("if self._use_same_day_close():\n                            # 触发价夹到当日 K 线区间内", self.src)

    def test_stop_loss_fills_on_same_day(self):
        """单笔止损线是**静态**的（成本×(1−亏损%)）⇒ 实盘挂单当天即成交，不该等 T+1。

        实测（2026-09-20，142 只核心票 40 日样本）：等一天会让离场从设定 −6% 恶化到
        −11%~−17%；改为当日成交后总收益 −20.41% → −17.30%、最深单笔 −17.6% → −11.4%。
        """
        self.assertIn('_hit_sl = (_sl_price is not None and _t_idx >= 0', self.src)
        self.assertIn('sell_price_raw = min(_sl_price, float(df.iloc[_t_idx][\'open\']))', self.src)

    def test_helpers_documented(self):
        self.assertIn('min(X, open)', self.src)
        self.assertIn('max(X, open)', self.src)


if __name__ == '__main__':
    unittest.main()
