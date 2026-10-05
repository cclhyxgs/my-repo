# -*- coding: utf-8 -*-
"""回归：卖出执行价默认口径 = next_open（T 日收盘决策 → T+1 开盘成交）。

背景（2026-09-19 用户决策）：
  原先 `sell_price_mode` 默认 `'same_close'` —— **卖出**用「信号日触发阈值价」（可能等于
  当日最高），而**建仓**用 T+1 开盘。两头口径不对称，且卖出偏乐观（实盘收盘才知道信号）。
  用户拍板改为买卖对称：默认 `'next_open'`，即 T 日收盘决策、T+1 开盘成交。

同时锁住一个易踩的坑：`ui/config_mapper._quant_data_to_config` 的 `risk_params` 是
**按 UI 控件重建**的，若不做白名单沿用，用户在界面上「保存方案」时会把这个纯后端开关抹掉
（表现为「改了配置但一保存就失效」）。

⛔ 建仓始终走 T+1 开盘，**不受**该开关影响（源码守卫见 TestBuyIsAlwaysNextOpen）。
"""
import os
import re
import unittest
from unittest import mock

from engine import quant_config
from engine.backtest_strategy import StrategyBacktester
from ui.config_mapper import _quant_data_to_config

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(_ROOT, rel), encoding='utf-8') as f:
        return f.read()


def _make_bt(mode=None):
    """绕过 __init__（需读 config/股票池），只装配 _use_same_day_close 依赖的属性。"""
    bt = object.__new__(StrategyBacktester)
    bt.sell_price_mode = mode if mode is not None else 'next_open'
    return bt


class TestDefaultMode(unittest.TestCase):

    def test_use_same_day_close_semantics(self):
        self.assertFalse(_make_bt('next_open')._use_same_day_close())
        self.assertTrue(_make_bt('same_close')._use_same_day_close())

    def test_source_default_is_next_open(self):
        src = _read('engine/backtest_strategy.py')
        self.assertIn("rp.get('sell_price_mode', 'next_open')", src)
        self.assertNotIn("rp.get('sell_price_mode', 'same_close')", src)

    def test_cli_argparse_default_is_next_open(self):
        src = _read('engine/backtest_strategy.py')
        m = re.search(r"add_argument\('--sell-price-mode'.*?default='(\w+)'", src, re.S)
        self.assertIsNotNone(m, '未找到 --sell-price-mode 参数定义')
        self.assertEqual(m.group(1), 'next_open')

    def test_report_echoes_mode(self):
        """报告需回显执行价模式，便于审计（字段名不可改）。"""
        self.assertIn("'sell_price_mode': self.sell_price_mode", _read('engine/backtest_strategy.py'))


class TestBuyIsAlwaysNextOpen(unittest.TestCase):
    """建仓不受 sell_price_mode 影响：始终 T+1 开盘。"""

    def test_build_branch_uses_next_open(self):
        src = _read('engine/backtest_strategy.py')
        # 建仓分支必须调用 _get_next_open_price
        self.assertIn('raw_entry_price, next_date = _get_next_open_price(', src)
        # 且该调用不能被包在 _use_same_day_close 分支里
        m = re.search(r'raw_entry_price, next_date = _get_next_open_price\(', src)
        before = src[:m.start()]
        # 建仓调用点之前最近的缩进块不应是 _use_same_day_close 的 if（保守检查：同段落不含该判据）
        seg = before[-800:]
        self.assertNotIn("if self._use_same_day_close()", seg.split('\n\n')[-1])


class TestMapperKeepsBackendOnlyKeys(unittest.TestCase):
    """UI 保存方案时不得抹掉 risk_params 里的纯后端开关。"""

    _DATA = {
        'factors': [],
        'risk': [{'key': 'max_single_position', 'val': '10', 'pct': True}],
        'circuitBreakerIndex': '沪深300',
    }

    def _convert(self, cur_risk):
        with mock.patch.object(quant_config, '_safe_cfg',
                               return_value={'risk_params': cur_risk}):
            return _quant_data_to_config(dict(self._DATA)) or {}

    def test_sell_price_mode_preserved(self):
        out = self._convert({'sell_price_mode': 'same_close', 'max_single_position': 0.1})
        self.assertEqual((out.get('risk_params') or {}).get('sell_price_mode'), 'same_close')

    def test_ui_fields_still_written(self):
        out = self._convert({'sell_price_mode': 'next_open'})
        rp = out.get('risk_params') or {}
        self.assertEqual(rp.get('max_single_position'), 0.1)   # UI 值照常生效
        self.assertEqual(rp.get('market_crash_index'), 'sh000300')

    def test_not_invented_when_absent(self):
        out = self._convert({'max_single_position': 0.1})
        self.assertNotIn('sell_price_mode', out.get('risk_params') or {})

    def test_whitelist_documented(self):
        """白名单注释要写清「以后新增同类开关需登记」——防后人漏加。"""
        self.assertIn('sell_price_mode', _read('ui/config_mapper.py'))
        self.assertIn('登记到这里', _read('ui/config_mapper.py'))


if __name__ == '__main__':
    unittest.main()
