#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""复盘归因（F-1106）Engine 单测：按信号类型聚合「你最不执行哪类信号」。

口径来源：engine/discipline_log.py::DisciplineJournal.attribution
  - 执行率 = 已执行条数 / 该类信号条数
  - 情绪化差合计 = 该类各条 emotion_diff 之和（口径同 ledger_summary）
  - 样本 < min_samples 的类型不下「最容易不执行」结论（诚实收窄）
"""
import tempfile
import unittest

from engine.discipline_log import DisciplineJournal, reset_journal


class TestDisciplineAttribution(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        reset_journal()
        self.j = DisciplineJournal(config_dir=self.dir)

    def _seed(self, st, code, trigger, latest, executed=None, market='stock', ratio=1.0):
        s = self.j.append_signal(code=code, signal_type=st, market=market,
                                 price=trigger, position_ratio=ratio)
        self.j.update_price_by_code(code, latest)
        if executed is not None:
            self.j.set_execution(s['id'], executed, exec_price=trigger, exec_ratio=100)
        return s

    def test_worst_exec_and_costliest(self):
        # 减仓 3 条全不执行（现价跌 → 未执行减仓记亏）；建仓 3 条全执行
        for i in range(3):
            self._seed('reduce', 'r%d' % i, 10.0, 9.0, executed=False)
            self._seed('buy', 'b%d' % i, 10.0, 10.0, executed=True)
        a = self.j.attribution()
        self.assertEqual(a['total'], 6)
        self.assertEqual(a['worst_exec']['signal_type'], 'reduce')
        self.assertEqual(a['worst_exec']['exec_rate'], 0.0)
        self.assertEqual(a['worst_exec']['not_executed'], 3)
        self.assertEqual(a['worst_exec']['emotion_sum'], -30.0)
        self.assertEqual(a['costliest']['signal_type'], 'reduce')
        buy = [r for r in a['by_type'] if r['signal_type'] == 'buy'][0]
        self.assertEqual(buy['exec_rate'], 100.0)
        self.assertTrue(buy['reliable'])
        self.assertIn('减仓', a['headline'])

    def test_min_samples_guard_no_conclusion(self):
        # 只有 2 条减仓（< 3）→ 不给最不执行结论，只展示计数
        self._seed('reduce', 'r0', 10.0, 9.0, executed=False)
        self._seed('reduce', 'r1', 10.0, 9.0, executed=False)
        a = self.j.attribution(min_samples=3)
        self.assertIsNone(a['worst_exec'])
        self.assertIsNone(a['costliest'])
        self.assertEqual(a['by_type'][0]['count'], 2)
        self.assertFalse(a['by_type'][0]['reliable'])
        self.assertIn('样本还不够', a['headline'])

    def test_all_executed_headline(self):
        for i in range(3):
            self._seed('buy', 'b%d' % i, 10.0, 10.0, executed=True)
        a = self.j.attribution()
        self.assertEqual(a['worst_exec']['not_executed'], 0)
        self.assertIn('每次都执行', a['headline'])

    def test_market_filter(self):
        for i in range(3):
            self._seed('buy', 's%d' % i, 10.0, 10.0, executed=True, market='stock')
            self._seed('buy', 'f%d' % i, 10.0, 10.0, executed=True, market='futures')
        a = self.j.attribution(market='stock')
        self.assertEqual(a['total'], 3)
        self.assertEqual(len(a['by_type']), 1)
        self.assertEqual(a['by_type'][0]['signal_type'], 'buy')

    def test_empty_journal(self):
        a = self.j.attribution()
        self.assertEqual(a['by_type'], [])
        self.assertIsNone(a['worst_exec'])
        self.assertEqual(a['total'], 0)
        self.assertIn('还没有信号', a['headline'])

    # ---------- F-1107 模式识别（舒适区） ----------
    def test_pattern_not_ready_below_min_buckets(self):
        # 只有 2 条减仓（< min_samples）→ 无达标桶 → ready False，提示还差几条
        self._seed('reduce', 'r0', 10.0, 9.0, executed=False)
        self._seed('reduce', 'r1', 10.0, 9.0, executed=False)
        p = self.j.pattern()
        self.assertFalse(p['ready'])
        self.assertEqual(p['reliable_n'], 0)
        self.assertIsNone(p['comfort'])
        self.assertIn('数据还不够识别舒适区', p['headline'])
        self.assertIn('还差 1 条', p['headline'])

    def test_pattern_comfort_and_strain(self):
        for i in range(3):
            self._seed('buy', 'b%d' % i, 10.0, 10.0, executed=True)
            self._seed('reduce', 'r%d' % i, 10.0, 9.0, executed=False)
        p = self.j.pattern()
        self.assertTrue(p['ready'])
        self.assertEqual(p['comfort']['key'], 'buy')
        self.assertEqual(p['comfort']['exec_rate'], 100.0)
        self.assertEqual(p['strain']['key'], 'reduce')
        self.assertEqual(p['strain']['exec_rate'], 0.0)
        self.assertIn('舒适区', p['headline'])
        self.assertIn('建仓', p['headline'])

    def test_pattern_stable_rate_no_comfort_claim(self):
        # 全执行 → 各桶执行率相同 → 不硬说舒适区
        for i in range(3):
            self._seed('buy', 'b%d' % i, 10.0, 10.0, executed=True)
        p = self.j.pattern()
        self.assertTrue(p['ready'])
        self.assertIn('差别不大', p['headline'])

    # ---------- F-1108 纪律联动（执行干预） ----------
    def test_linkage_disabled_hint(self):
        self._seed('reduce', 'r0', 10.0, 9.0, executed=False)
        k = self.j.linkage()
        self.assertFalse(k['enabled'])
        self.assertIsNone(k['watch'])
        self.assertEqual(k['pending'], [])
        self.assertIn('还需 2 条', k['hint'])
        self.assertIn('减仓', k['hint'])

    def test_linkage_enabled_with_pending(self):
        for i in range(3):
            self._seed('reduce', 'r%d' % i, 10.0, 9.0, executed=False)
        k = self.j.linkage()
        self.assertTrue(k['enabled'])
        self.assertEqual(k['watch']['signal_type'], 'reduce')
        self.assertEqual(k['watch']['not_executed'], 3)
        self.assertEqual(len(k['pending']), 3)
        self.assertEqual(sorted(p['code'] for p in k['pending']), ['r0', 'r1', 'r2'])
        self.assertIsNotNone(k['pending'][0]['diff_pct'])
        self.assertEqual(k['hint'], '')

    def test_linkage_watch_all_executed(self):
        for i in range(3):
            self._seed('reduce', 'r%d' % i, 10.0, 9.0, executed=True)
        k = self.j.linkage()
        self.assertFalse(k['enabled'])
        self.assertIn('全部已执行', k['hint'])

    def test_linkage_market_filter_empty(self):
        for i in range(3):
            self._seed('reduce', 'f%d' % i, 10.0, 9.0, executed=False, market='futures')
        k = self.j.linkage(market='stock')
        self.assertFalse(k['enabled'])
        self.assertIn('还没有信号', k['hint'])


if __name__ == '__main__':
    unittest.main()