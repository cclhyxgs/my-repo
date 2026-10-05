#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""情绪化操作记录 Engine 单测：聚焦「最容易犯的错」只统计情绪化亏损。"""
import os
import tempfile
import unittest

from engine.emotion_log import EmotionJournal, reset_emotion_journal


class TestEmotionLossSemantic(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        reset_emotion_journal()
        self.j = EmotionJournal(config_dir=self.dir)

    def tearDown(self):
        self.j._data_path_cleanup = None  # 无需清理，tempfile 自动回收

    def test_top_tag_only_counts_loss_records(self):
        # 买入被套(亏损, 追涨)
        self.j.add_record(code='a', action='buy', tag='追涨', op_price=100, latest_price=80)
        # 买入赚(非亏损, 怕踏空) —— 不应计入 top_tag
        self.j.add_record(code='b', action='buy', tag='怕踏空', op_price=50, latest_price=60)
        # 清仓卖早少赚(亏损, 拿不住)
        self.j.add_record(code='c', action='clear', tag='拿不住', op_price=90, latest_price=100)
        # 清仓后跌(非亏损, 恐慌) —— 不应计入
        self.j.add_record(code='d', action='clear', tag='恐慌', op_price=95, latest_price=90)
        s = self.j.summary()
        self.assertEqual(s['total'], 4)
        self.assertEqual(s['loss_n'], 2)
        # 亏损记录里 追涨 与 拿不住 各 1 票（怕踏空、恐慌不计入）
        self.assertEqual(s['top_tag_count'], 1)
        self.assertIn(s['top_tag'], ('追涨', '拿不住'))
        self.assertEqual(s['tags'], {'追涨': 1, '拿不住': 1})

    def test_loss_n_reflects_direction_per_action(self):
        # 加仓亏
        self.j.add_record(code='a', action='add', tag='摊平', op_price=100, latest_price=95)
        # 减仓卖早（亏损方向：现价>卖出价）
        self.j.add_record(code='b', action='reduce', tag='怕回调', op_price=90, latest_price=95)
        s = self.j.summary()
        self.assertEqual(s['loss_n'], 2)
        self.assertEqual(s['tags'], {'摊平': 1, '怕回调': 1})

    def test_empty_journal(self):
        s = self.j.summary()
        self.assertEqual(s, {'total': 0, 'sum_diff': 0.0, 'diff_n': 0, 'loss_n': 0,
                             'top_tag': '', 'top_tag_count': 0, 'tags': {}})


if __name__ == '__main__':
    unittest.main()