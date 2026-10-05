#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""初级模式「技术条件分级」建仓 回归测试。

基准：初级（因子休眠）不再用「触发指标+统一比例」，而是复用 UnifiedEntryLogic.check_entry
的 basic 分支——按「状态分界」技术条件从高到低定档（强→标→探→望），命中哪档用该档比例。
要点：final_score=0（无因子）时仍能按纯技术条件建仓；命中强势档用 strong 比例；全不满足则空仓观望。
"""
import math
import unittest

from engine import quant_config
from engine.trading_context import TradingContext
from engine.stock_classifier import StockClassifier
from engine.unified_entry_logic import UnifiedEntryLogic


def _make_rising_tech():
    """构造一条上升趋势的 K 线序列，latest_price 站上 MA20。返回 (tech, latest_price)。"""
    n = 120
    closes = [10.0 + i * 0.05 for i in range(n)]
    vols = [1000.0] * n
    highs = [c + 0.2 for c in closes]
    lows = [c - 0.2 for c in closes]
    opens = list(closes)
    data_list = [{'open': opens[i], 'high': highs[i], 'low': lows[i],
                  'close': closes[i], 'volume': vols[i]} for i in range(n)]
    rsi_hist = [50.0] * n
    from engine.scoring_core import build_tech
    return build_tech(closes, vols, highs, lows, opens, data_list, rsi_hist, (0.0, '中性')), closes[-1]


class TestPrimaryTechGrading(unittest.TestCase):
    def setUp(self):
        # 备份被替换的 quant_config getter
        self._saved = {
            'mode': quant_config.get_usage_mode,
            'conditions': quant_config.get_entry_conditions,
            'params': quant_config.get_entry_params,
            'thresholds': quant_config.get_thresholds,
        }
        # 全部档位统一用 above_ma20（命中即强档先返回；不命中则四档全不满足→空仓）
        quant_config.get_usage_mode = lambda: 'basic'
        quant_config.get_entry_conditions = lambda: {
            'strong': {'tech_signal': 'above_ma20'},
            'standard': {'tech_signal': 'above_ma20'},
            'test': {'tech_signal': 'above_ma20'},
            'pending': {'tech_signal': 'above_ma20'},
        }
        quant_config.get_entry_params = lambda: {
            'positions': {'strong': 0.30, 'standard': 0.2, 'test': 0.1,
                          'observe': 0.05, 'rebound': 0.05,
                          'panic_rebound': 0.02, 'top_reversal': 0.05},
        }
        quant_config.get_thresholds = lambda: {}

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(quant_config, {'mode': 'get_usage_mode',
                                   'conditions': 'get_entry_conditions',
                                   'params': 'get_entry_params',
                                   'thresholds': 'get_thresholds'}[k], v)

    def test_basic_grades_by_tech_builds_strong_with_no_factor(self):
        """因子休眠（final_score=0）时，命中强档技术条件 → 按 strong 比例建仓。"""
        tech, latest = _make_rising_tech()
        market = {'up_ratio': 0.5, 'ma_arrangement': '多头排列', 'volume_price': '正常'}
        r = UnifiedEntryLogic.check_entry(
            stock_score=0.0, final_score=0.0, tech=tech, market=market,
            up_ratio=0.5, latest_price=latest, adx_state={'state': 'trending_up'})
        self.assertEqual(r['action'], '关注建仓')
        self.assertEqual(r['position'], 0.30)  # strong 档比例（未经过市场门控）

    def test_basic_classifier_grades_by_tech_ignores_factor_score(self):
        """初级下分类器跳过因子分阈值、纯按技术定档（驱动加/减/清按技术档位路由）。"""
        # 覆盖 setUp 的空阈值：分类器直接索引 thresholds['strong'] 等字段
        self._saved['thresholds'] = quant_config.get_thresholds
        quant_config.get_thresholds = lambda: {
            'strong': 60, 'standard': 50, 'test': 40, 'pending': 30,
        }
        tech, latest = _make_rising_tech()
        data_list = [{'high': latest, 'low': latest * 0.98, 'close': latest, 'volume': 1000}]
        ctx = TradingContext('sz000001', 0.5, tech, {'ma_arrangement': '多头排列'}, data_list)
        ctx.stock_score = 0.0
        ctx.final_score = 0.0  # 因子休眠，得分无意义
        ctx.latest_price = latest
        ctx.adx_state = {'state': 'trending_up'}
        r = StockClassifier.classify(ctx)
        self.assertEqual(r['type'], 'strong')  # 纯技术命中则定强档，而非依赖 final_score
        # 与 add/reduce 路由贯通：按技术档位 → strong_standard 档（而非塌缩到默认档）
        from engine.add_engine import _TYPE_TO_ROW
        self.assertEqual(_TYPE_TO_ROW[r['type']], 'strong_standard')

    def test_basic_empty_watch_when_no_tech_matches(self):
        """技术条件全不满足 → 空仓观望，不使用任一档位比例。"""
        tech, _ = _make_rising_tech()
        # latest_price 远低于 MA20 → above_ma20 全部不成立
        market = {'up_ratio': 0.1, 'ma_arrangement': '空头排列', 'volume_price': '正常'}
        r = UnifiedEntryLogic.check_entry(
            stock_score=0.0, final_score=0.0, tech=tech, market=market,
            up_ratio=0.1, latest_price=1.0, adx_state={'state': 'neutral'})
        self.assertEqual(r['action'], '空仓观望')
        self.assertEqual(r['position'], 0)

    def test_basic_rebound_triggered_by_tech_condition(self):
        """初级：博反弹档配置数值指标技术条件后，条件满足 → 按 rebound 比例建仓。"""
        self._saved['conditions'] = quant_config.get_entry_conditions
        quant_config.get_entry_conditions = lambda: {
            'strong': {'tech_signal': [{'indicator': 'volume_ratio', 'op': '<', 'value': 0}]},
            'standard': {'tech_signal': [{'indicator': 'volume_ratio', 'op': '<', 'value': 0}]},
            'test': {'tech_signal': [{'indicator': 'volume_ratio', 'op': '<', 'value': 0}]},
            'pending': {'tech_signal': [{'indicator': 'volume_ratio', 'op': '<', 'value': 0}]},
            'rebound': {'tech_signal': [{'indicator': 'volume_ratio', 'op': '>=', 'value': 0}]},
        }
        tech, latest = _make_rising_tech()
        market = {'up_ratio': 0.5, 'ma_arrangement': '多头排列', 'volume_price': '正常'}
        r = UnifiedEntryLogic.check_entry(
            stock_score=0.0, final_score=0.0, tech=tech, market=market,
            up_ratio=0.5, latest_price=latest, adx_state={'state': 'trending_up'})
        self.assertEqual(r['action'], '博反弹')
        self.assertEqual(r['position'], 0.05)

    def test_basic_panic_rebound_triggered_by_tech_condition(self):
        """初级：恐慌反转档配置技术条件满足 → 按 panic_rebound 比例建仓（恐慌档优先于博反弹）。"""
        self._saved['conditions'] = quant_config.get_entry_conditions
        quant_config.get_entry_conditions = lambda: {
            'strong': {'tech_signal': [{'indicator': 'volume_ratio', 'op': '<', 'value': 0}]},
            'standard': {'tech_signal': [{'indicator': 'volume_ratio', 'op': '<', 'value': 0}]},
            'test': {'tech_signal': [{'indicator': 'volume_ratio', 'op': '<', 'value': 0}]},
            'pending': {'tech_signal': [{'indicator': 'volume_ratio', 'op': '<', 'value': 0}]},
            'panic_rebound': {'tech_signal': [{'indicator': 'volume_ratio', 'op': '>=', 'value': 0}]},
        }
        tech, latest = _make_rising_tech()
        market = {'up_ratio': 0.5, 'ma_arrangement': '多头排列', 'volume_price': '正常'}
        r = UnifiedEntryLogic.check_entry(
            stock_score=0.0, final_score=0.0, tech=tech, market=market,
            up_ratio=0.5, latest_price=latest, adx_state={'state': 'trending_up'})
        self.assertEqual(r['action'], '恐慌反转')
        self.assertEqual(r['position'], 0.02)

    def test_basic_no_rebound_spec_does_not_trigger(self):
        """初级：博反弹/恐慌未配置技术条件 → 默认不触发，回空仓观望。"""
        self._saved['conditions'] = quant_config.get_entry_conditions
        quant_config.get_entry_conditions = lambda: {
            'strong': {'tech_signal': [{'indicator': 'volume_ratio', 'op': '<', 'value': 0}]},
            'standard': {'tech_signal': [{'indicator': 'volume_ratio', 'op': '<', 'value': 0}]},
            'test': {'tech_signal': [{'indicator': 'volume_ratio', 'op': '<', 'value': 0}]},
            'pending': {'tech_signal': [{'indicator': 'volume_ratio', 'op': '<', 'value': 0}]},
        }
        tech, latest = _make_rising_tech()
        market = {'up_ratio': 0.5, 'ma_arrangement': '多头排列', 'volume_price': '正常'}
        r = UnifiedEntryLogic.check_entry(
            stock_score=0.0, final_score=0.0, tech=tech, market=market,
            up_ratio=0.5, latest_price=latest, adx_state={'state': 'trending_up'})
        self.assertEqual(r['action'], '空仓观望')
        self.assertEqual(r['position'], 0)


if __name__ == '__main__':
    unittest.main()