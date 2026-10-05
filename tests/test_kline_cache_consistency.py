#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""K 线前复权缓存一致性单测（回归两类脏缓存 bug）：

1. incremental_merge 的 qfq 缩放比 r 合法域 [0.2, 1.01] 守卫：
   - 未复权脏缓存 / 源异常会让 r>1，必须拒绝(None)以免整段旧序列被错位缩放；
   - 极端拆分 r<0.2 同样拒绝。
   - 合法 r(分红送转 r<1) 须把整段旧 OHLC × r 对齐，再无缝拼接真正新增的交易日。

2. load_qfq_daily(offline=True) 的 union 行为：
   - 优先统一 store(qfq_daily)，缺失/不足才回退扫描内部 store，均缺失返回 None 不联网。

对应 engine/data_layer.py 的 KLineFetcher.incremental_merge / load_qfq_daily。
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
from engine.data_layer import KLineFetcher


def _df(closes, start='2024-01-01', freq='D'):
    """由收盘价序列构造带 trade_time 的 DataFrame（OHLC 近似）。"""
    idx = pd.date_range(start, periods=len(closes), freq=freq)
    closes = np.asarray(closes, dtype=float)
    highs = closes * 1.01
    lows = closes * 0.99
    opens = np.roll(closes, 1)
    opens[0] = closes[0]
    return pd.DataFrame({
        'trade_time': idx,
        'open': opens,
        'high': highs,
        'low': lows,
        'close': closes,
        'volume': np.full(len(closes), 1e6, dtype=float),
    })


def _scale(df, r):
    """返回旧缓存经 qfq 缩放 r 后的副本（仅 OHLC）。"""
    out = df.copy()
    for c in ['open', 'close', 'high', 'low']:
        out[c] = out[c].astype(float) * r
    return out


class TestIncrementalMerge(unittest.TestCase):
    def setUp(self):
        # 旧缓存：100 天，最后一天 = 2024-04-09
        self.old = _df(100 + np.arange(100, dtype=float), start='2024-01-01')

    def _tail(self, r, extra_new=10):
        """构造今日锚点的 qfq 尾巴：覆盖旧末尾 30 天(×r) + 真正新增 extra_new 天。"""
        old = self.old
        last_date = old['trade_time'].max()
        # 重叠区：旧最后 30 天
        overlap = old[old['trade_time'] > (last_date - pd.Timedelta(days=30))]
        scaled = _scale(overlap, r)
        # 新增交易日
        new_start = last_date + pd.Timedelta(days=1)
        new_idx = pd.date_range(new_start, periods=extra_new, freq='D')
        new_part = pd.DataFrame({
            'trade_time': new_idx,
            'open': np.arange(extra_new, dtype=float) + 200,
            'high': np.arange(extra_new, dtype=float) + 201,
            'low': np.arange(extra_new, dtype=float) + 199,
            'close': np.arange(extra_new, dtype=float) + 200,
            'volume': np.full(extra_new, 1e6, dtype=float),
        })
        return pd.concat([scaled, new_part], ignore_index=True)

    def test_seamless_merge_r_one(self):
        # r=1（无分红）：旧序列原样拼接新增交易日，长度相加
        tail = self._tail(1.0)
        merged = KLineFetcher.incremental_merge(self.old, tail)
        self.assertIsNotNone(merged)
        self.assertEqual(len(merged), len(self.old) + 10)
        # 旧末尾收盘价在时间轴上连续（无跳变）
        old_last = self.old['close'].iloc[-1]
        merged_at_old_last = merged[merged['trade_time'] == self.old['trade_time'].max()]['close']
        self.assertAlmostEqual(float(merged_at_old_last.iloc[0]), old_last, places=6)

    def test_dividend_scale_r_below_one(self):
        # r=0.9（分红送转）：旧 OHLC 整体 ×0.9 后再拼接，且边界无缝
        tail = self._tail(0.9)
        merged = KLineFetcher.incremental_merge(self.old, tail)
        self.assertIsNotNone(merged)
        self.assertEqual(len(merged), len(self.old) + 10)
        # 重叠区旧序列缩放后应与尾巴里的缩放值一致（边界连续性）
        bound_date = self.old['trade_time'].max()
        scaled_old_close = self.old['close'].iloc[-1] * 0.9
        merged_close = merged[merged['trade_time'] == bound_date]['close'].iloc[0]
        self.assertAlmostEqual(float(merged_close), scaled_old_close, places=6)

    def test_reject_unadjusted_r_above_one(self):
        # 未复权脏缓存：r=1.05 > 1.01 必须拒绝（否则整段旧序列被错位缩放）
        tail = self._tail(1.05)
        self.assertIsNone(KLineFetcher.incremental_merge(self.old, tail))

    def test_reject_extreme_split_r_below_lower(self):
        # 极端拆分 r=0.1 < 0.2 必须拒绝
        tail = self._tail(0.1)
        self.assertIsNone(KLineFetcher.incremental_merge(self.old, tail))

    def test_reject_no_overlap(self):
        # 尾巴完全在旧数据之后（无重叠）→ 无法对齐 → None
        old = self.old
        last_date = old['trade_time'].max()
        new_start = last_date + pd.Timedelta(days=1)
        new_idx = pd.date_range(new_start, periods=10, freq='D')
        tail = pd.DataFrame({
            'trade_time': new_idx,
            'open': np.arange(10) + 1, 'high': np.arange(10) + 2,
            'low': np.arange(10), 'close': np.arange(10) + 1.0,
            'volume': np.full(10, 1e6, dtype=float),
        })
        self.assertIsNone(KLineFetcher.incremental_merge(old, tail))

    def test_no_new_data_returns_old(self):
        # 尾巴 == 旧缓存（无新增交易日）→ 返回原长
        self.assertEqual(len(KLineFetcher.incremental_merge(self.old, self.old.copy())), len(self.old))


class TestLoadQfqDailyOffline(unittest.TestCase):
    def setUp(self):
        self.code = 'sh600000'
        self.days = 60

    def _make_df(self, n):
        return _df(np.arange(n, dtype=float) + 10, start='2024-01-01')

    def test_prefers_unified_store(self):
        full = self._make_df(200)
        with patch.object(KLineFetcher, '_load_qfq_daily_disk', return_value=full) as m1, \
             patch.object(KLineFetcher, '_load_kline_disk', return_value=None) as m2:
            out = KLineFetcher.load_qfq_daily(self.code, self.days, offline=True)
        self.assertEqual(len(out), self.days)  # 切片到最近 days
        m2.assert_not_called()  # 命中统一 store 不回退

    def test_fallback_to_scan_store(self):
        short = self._make_df(120)  # 统一 store 缺失
        scan = self._make_df(200)
        with patch.object(KLineFetcher, '_load_qfq_daily_disk', return_value=None) as m1, \
             patch.object(KLineFetcher, '_load_kline_disk',
                          return_value=scan) as m2:
            out = KLineFetcher.load_qfq_daily(self.code, self.days, offline=True)
        self.assertEqual(len(out), self.days)
        m1.assert_called_once()

    def test_offline_missing_returns_none(self):
        with patch.object(KLineFetcher, '_load_qfq_daily_disk', return_value=None), \
             patch.object(KLineFetcher, '_load_kline_disk', return_value=None):
            out = KLineFetcher.load_qfq_daily(self.code, self.days, offline=True)
        self.assertIsNone(out)  # 离线且无缓存 → 不联网，返回 None

    def test_offline_short_returns_as_is(self):
        # 统一 store 命中但不足 days，离线模式原样返回（不补抓）
        short = self._make_df(30)
        with patch.object(KLineFetcher, '_load_qfq_daily_disk', return_value=short), \
             patch.object(KLineFetcher, '_load_kline_disk', return_value=None):
            out = KLineFetcher.load_qfq_daily(self.code, self.days, offline=True)
        self.assertEqual(len(out), 30)


if __name__ == '__main__':
    unittest.main()
