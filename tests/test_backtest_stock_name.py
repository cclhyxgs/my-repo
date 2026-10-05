# -*- coding: utf-8 -*-
"""回归：回测产物（逐笔汇总 / 逐笔成交明细）显示股票名称。

背景（2026-09-19 用户要求）：`bt_strategy_trades.csv` 的「股票代码」与
`bt_strategy_trades_detail.csv` 的「持仓编号」只有 sh/sz 代码，人工核对不直观，
需增加「股票名称」列。名称来源：`engine.state.code_to_name` 或 cache/stock_list.json。
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from engine import backtest_strategy as bs

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    'engine', 'backtest_strategy.py')


class TestStockNameLookup(unittest.TestCase):
    def setUp(self):
        self._saved = bs._STOCK_NAME_MAP
        bs._STOCK_NAME_MAP = None

    def tearDown(self):
        bs._STOCK_NAME_MAP = self._saved

    def test_returns_name_from_map(self):
        with mock.patch.object(bs, '_load_stock_name_map',
                               return_value={'sh600519': '贵州茅台'}):
            self.assertEqual(bs._stock_name('sh600519'), '贵州茅台')

    def test_case_insensitive_and_unknown(self):
        with mock.patch.object(bs, '_load_stock_name_map',
                               return_value={'sh600519': '贵州茅台'}):
            self.assertEqual(bs._stock_name('SH600519'), '贵州茅台')
            self.assertEqual(bs._stock_name('sh000000'), '')
            self.assertEqual(bs._stock_name(''), '')
            self.assertEqual(bs._stock_name(None), '')

    def test_never_raises(self):
        with mock.patch.object(bs, '_load_stock_name_map', side_effect=RuntimeError('boom')):
            self.assertEqual(bs._stock_name('sh600519'), '')

    def test_map_from_state(self):
        from engine.state import state
        with mock.patch.object(state, 'code_to_name', {'sh600519': '贵州茅台'}):
            m = bs._load_stock_name_map()
        self.assertEqual(m.get('sh600519'), '贵州茅台')

    def test_map_falls_back_to_stock_list_json(self):
        tmp = tempfile.mkdtemp(prefix='bt_names_')
        try:
            os.makedirs(os.path.join(tmp, 'cache'), exist_ok=True)
            with open(os.path.join(tmp, 'cache', 'stock_list.json'), 'w', encoding='utf-8') as f:
                json.dump({'code_to_name': {'sh600519': '贵州茅台'}}, f, ensure_ascii=False)
            from engine.state import state
            with mock.patch.object(state, 'code_to_name', {}), \
                 mock.patch.object(bs, 'get_app_dir', return_value=tmp):
                m = bs._load_stock_name_map()
            self.assertEqual(m.get('sh600519'), '贵州茅台')
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_empty_when_no_source(self):
        from engine.state import state
        tmp = tempfile.mkdtemp(prefix='bt_names_empty_')
        try:
            with mock.patch.object(state, 'code_to_name', {}), \
                 mock.patch.object(bs, 'get_app_dir', return_value=tmp):
                m = bs._load_stock_name_map()
            self.assertEqual(m, {})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestExportContainsNameColumn(unittest.TestCase):
    """轻量守卫：两张人读 CSV 的构建函数都必须带上「股票名称」列（防后续改动误删）。"""

    def test_source_has_name_column_in_builders(self):
        src = open(_SRC, encoding='utf-8').read()
        # 持仓明细 + 交易流水两个 builder 各一处
        self.assertGreaterEqual(src.count("'股票名称': name_fn(p.stock_code)"), 2)
        self.assertIn("'股票代码': p.stock_code", src)
        # JSON（机器可读）也保留名称
        self.assertIn("'stock_name': _stock_name(p.stock_code)", src)


if __name__ == '__main__':
    unittest.main()
