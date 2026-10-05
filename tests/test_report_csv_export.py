# -*- coding: utf-8 -*-
"""回归：report.json → 两列 CSV（指标,值）转换，供「打开报告CSV」按钮使用。

背景（2026-09-19 用户要求）：.json 在用户机器上无默认关联程序，打开必失败；
报告仅有 JSON 产物，故打开时就地转成 CSV。本测试锁定扁平化与中文标签口径。
"""
import csv
import json
import os
import shutil
import tempfile
import unittest

from ui.web_api import _report_json_to_csv


class TestReportJsonToCsv(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='bt_report_')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _convert(self, data):
        jp = os.path.join(self.dir, 'bt_strategy_report.json')
        cp = os.path.join(self.dir, 'bt_strategy_report.csv')
        with open(jp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
        _report_json_to_csv(jp, cp)
        with open(cp, encoding='utf-8-sig', newline='') as f:
            rows = list(csv.reader(f))
        return cp, rows

    def _dict(self, rows):
        return {r[0]: r[1] for r in rows[1:]}

    def test_header_is_metric_value(self):
        _, rows = self._convert({'total_trades': 165})
        self.assertEqual(rows[0], ['指标', '值'])

    def test_flat_fields_use_chinese_labels(self):
        _, rows = self._convert({'total_trades': 165, 'win_rate': 41.2,
                                 'start_date': '2025-08-20'})
        d = self._dict(rows)
        self.assertEqual(d['总交易笔数'], '165')
        self.assertEqual(d['胜率(%)'], '41.2')
        self.assertEqual(d['开始日期'], '2025-08-20')

    def test_nested_dict_flattened_with_dot(self):
        _, rows = self._convert({'compound': {'total_return': 46.42,
                                              'final_nav': 1464245.76}})
        d = self._dict(rows)
        self.assertEqual(d['总收益(%)'], '46.42')
        self.assertEqual(d['最终净值'], '1464245.76')

    def test_unknown_key_falls_back_to_raw_name(self):
        # 引擎将来新增字段 → 未登记标签时原样输出（不丢数据）
        _, rows = self._convert({'some_new_metric': 1})
        self.assertIn('some_new_metric', self._dict(rows))

    def test_list_value_json_encoded(self):
        _, rows = self._convert({'lst': [1, 2]})
        self.assertEqual(self._dict(rows)['lst'], '[1, 2]')

    def test_none_becomes_empty_string(self):
        _, rows = self._convert({'x': None})
        self.assertEqual(self._dict(rows)['x'], '')

    def test_utf8_bom_written(self):
        # 带 BOM，Excel 双击不乱码（与引擎 trades.csv 口径一致）
        cp, _ = self._convert({'total_trades': 1})
        with open(cp, 'rb') as f:
            self.assertEqual(f.read(3), b'\xef\xbb\xbf')


if __name__ == '__main__':
    unittest.main()
