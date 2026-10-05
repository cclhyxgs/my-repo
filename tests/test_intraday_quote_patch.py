#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""盘中「末根 K 线」修补 + 腾讯实时行情位序 回归测试。

背景（2026-10-05，用户反馈「情绪账本扫描的触发价容易错行，只有部分股票错」）：
触发价 = 信号时刻 K 线末根收盘价。盘中 `_fetch_internal` 为省请求复用昨日缓存，
靠 `_patch_intraday_bar` 用实时行情把末根刷成当日。但逐只实时行情在 WAF 整源
冷却期会整批快速失败 → 那部分股票末根停在昨日 → 触发价错成昨收（差一根）。

本测试锁定：
1. `fetch_tencent` 归一布局：高/低取自腾讯原始 [33]/[34]（不是 [6]/[7]），
   量 手→股 ×100，位序与新浪/通达信一致；
2. `_patch_intraday_bar` 优先走批量快照池（`RealtimeBarPool.get_live`），
   末根 close=现价、volume 为「手」；
3. 池不可用时逐只兜底，且成交量**一律** 股→手 /100（旧逻辑只对「新浪」除，
   通达信被漏掉 → 量能类因子偏 100 倍）。
"""
import unittest
from datetime import datetime as _real_datetime

import pandas as pd

import engine.data_layer as dl
from engine.data_layer import DataAPI, RealtimeBarPool, RealtimeQuoteFetcher


def _mkdf(last_date='2026-10-05', n=3):
    return pd.DataFrame({
        'trade_time': pd.date_range(end=last_date, periods=n, freq='B'),
        'open': [10.0] * n,
        'high': [10.1] * n,
        'low': [9.9] * n,
        'close': [10.0] * n,
        'volume': [1000.0] * n,
    })


class _FakeNow:
    """把 data_layer 模块级 datetime.now() 冻结到交易时段（周二 10:00）。"""

    @staticmethod
    def now():
        return _real_datetime(2026, 10, 6, 10, 0, 0)


class TestTencentFieldLayout(unittest.TestCase):
    def test_high_low_volume_from_verified_positions(self):
        parts = [''] * 40
        parts[1] = '浦发银行'
        parts[2] = '600000'
        parts[3] = '10.50'          # 现价
        parts[4] = '10.00'          # 昨收
        parts[5] = '10.20'          # 今开
        parts[6] = '123456'         # 成交量(手)
        parts[7] = '50000'          # 外盘（旧映射误当「最低」）
        parts[8] = '73456'          # 内盘
        parts[9] = '10.49'          # 买一
        parts[11] = '10.51'         # 卖一
        parts[30] = '20261006100000'
        parts[31] = '10.50'
        parts[33] = '10.60'         # 最高（旧映射误当「成交量」）
        parts[34] = '10.10'         # 最低
        raw = 'v_sh600000="' + '~'.join(parts) + '";'

        class _Resp:
            text = raw
            encoding = 'gbk'

        orig = dl.HTTPClient.request
        dl.HTTPClient.request = staticmethod(lambda *a, **kw: _Resp())
        try:
            fields, src = RealtimeQuoteFetcher.fetch_tencent('sh600000')
        finally:
            dl.HTTPClient.request = orig

        self.assertEqual(src, '腾讯')
        self.assertEqual(fields[0], '浦发银行')
        self.assertEqual(fields[1], '10.20')     # 今开
        self.assertEqual(fields[2], '10.00')     # 昨收
        self.assertEqual(fields[3], '10.50')     # 现价
        self.assertEqual(fields[4], '10.60')     # 最高 ← [33]
        self.assertEqual(fields[5], '10.10')     # 最低 ← [34]
        self.assertEqual(fields[8], '12345600')  # 量 手→股 ×100
        self.assertEqual(fields[30], '20261006100000')


class TestIntradayPatch(unittest.TestCase):
    def setUp(self):
        self._orig_dt = dl.datetime
        dl.datetime = _FakeNow

    def tearDown(self):
        dl.datetime = self._orig_dt

    def test_uses_live_pool_bar(self):
        """池可用：末根 close=现价，volume 为「手」（不再二次换算）。"""
        bar = {'trade_time': pd.Timestamp('2026-10-06'), 'open': 10.20,
               'close': 10.50, 'high': 10.60, 'low': 10.10,
               'volume': 123456.0, 'prev_close': 10.00}
        orig = RealtimeBarPool.get_live
        RealtimeBarPool.get_live = classmethod(lambda cls, code: bar)
        try:
            out = DataAPI._patch_intraday_bar(_mkdf(), 'sh600000', '日K')
        finally:
            RealtimeBarPool.get_live = orig

        last = out.iloc[-1]
        self.assertEqual(pd.Timestamp(last['trade_time']).date(),
                         _real_datetime(2026, 10, 6).date())
        self.assertAlmostEqual(float(last['close']), 10.50)
        self.assertAlmostEqual(float(last['volume']), 123456.0)
        self.assertAlmostEqual(float(last['high']), 10.60)
        self.assertAlmostEqual(float(last['low']), 10.10)

    def test_fallback_divides_volume_for_tdx(self):
        """池不可用：逐只兜底，通达信（股）也要 /100 → 手。"""
        fields = [''] * 32
        fields[0] = '浦发银行'
        fields[1] = '10.20'
        fields[2] = '10.00'
        fields[3] = '10.50'
        fields[4] = '10.60'
        fields[5] = '10.10'
        fields[8] = '12345600'      # 股

        orig_live = RealtimeBarPool.get_live
        orig_rt = DataAPI.get_realtime_quote
        RealtimeBarPool.get_live = classmethod(lambda cls, code: None)
        DataAPI.get_realtime_quote = classmethod(lambda cls, code: (fields, '通达信'))
        try:
            out = DataAPI._patch_intraday_bar(_mkdf(), 'sh600000', '日K')
        finally:
            RealtimeBarPool.get_live = orig_live
            DataAPI.get_realtime_quote = orig_rt

        last = out.iloc[-1]
        self.assertAlmostEqual(float(last['close']), 10.50)
        self.assertAlmostEqual(float(last['volume']), 123456.0)


if __name__ == '__main__':
    unittest.main()
