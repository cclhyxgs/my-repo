#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""技术条件 AND 语义回归测试（状态分界/建仓进级复用 _evaluate_tech_conditions）。

背景：全市场扫描的技术指标二级筛选（scan_tech_filter）为高级模式专属、结果态即时过滤，
判定复用 _evaluate_tech_conditions；扫描内核 build_tech_snapshot 负责把技术信号收敛成
JSON 可序列化快照供结果态过滤。此文件覆盖 AND 语义 + 快照类型 + 配置映射三条链路。
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.unified_entry_logic import UnifiedEntryLogic
from engine.market_scan_core import build_tech_snapshot
from ui.config_mapper import _config_to_quant_data, _quant_data_to_config, _default_quant_data


class TestEvaluateScanConditions(unittest.TestCase):
    """技术条件 AND 判定语义（多条件 ALL 命中 / 数值比较 / 缺失即不满足 / 空=放行）。"""

    def test_numeric_hit(self):
        tech = {'rsi_14': 30}
        conds = [{'indicator': 'rsi_14', 'op': '<', 'value': 40}]
        self.assertTrue(UnifiedEntryLogic._evaluate_tech_conditions(
            conds, tech, {}, 100, {}))

    def test_numeric_miss(self):
        tech = {'rsi_14': 50}
        conds = [{'indicator': 'rsi_14', 'op': '<', 'value': 40}]
        self.assertFalse(UnifiedEntryLogic._evaluate_tech_conditions(
            conds, tech, {}, 100, {}))

    def test_all_conditions_must_match(self):
        tech = {'rsi_14': 30, 'bias20': 3}
        conds = [{'indicator': 'rsi_14', 'op': '<', 'value': 40},
                 {'indicator': 'bias20', 'op': '>', 'value': 1}]
        self.assertTrue(UnifiedEntryLogic._evaluate_tech_conditions(
            conds, tech, {}, 100, {}))
        conds_bad = [{'indicator': 'rsi_14', 'op': '<', 'value': 40},
                     {'indicator': 'bias20', 'op': '>', 'value': 5}]
        self.assertFalse(UnifiedEntryLogic._evaluate_tech_conditions(
            conds_bad, tech, {}, 100, {}))

    def test_missing_indicator_blocks(self):
        # 指标缺失视为不满足，避免 AND 下误放行
        tech = {'rsi_14': 30}  # 缺 bias20
        conds = [{'indicator': 'bias20', 'op': '>', 'value': 1}]
        self.assertFalse(UnifiedEntryLogic._evaluate_tech_conditions(
            conds, tech, {}, 100, {}))

    def test_empty_filter_is_pass(self):
        self.assertTrue(UnifiedEntryLogic._evaluate_tech_conditions([], {}, {}, 100, {}))


class TestScanSnapshot(unittest.TestCase):
    """build_tech_snapshot：全部原生 JSON 类型 + 可判定字段齐备（高级二级过滤消费前提）。"""

    @staticmethod
    def _mk_tech():
        try:
            import numpy as np
            return {
                'rsi_14': np.float64(30.0), 'macd_hist': np.float32(0.015),
                'kdj_k': np.int64(20),
                'kdj_signal': 'gold_cross', 'macd_status': 'golden', 'breakout_signal': 'none',
            }
        except Exception:
            return {
                'rsi_14': 30.0, 'macd_hist': 0.015, 'kdj_k': 20,
                'kdj_signal': 'gold_cross', 'macd_status': 'golden', 'breakout_signal': 'none',
            }

    def test_all_native_json_types(self):
        snap = build_tech_snapshot(self._mk_tech(), {'ma_arrangement': 2, 'volume_price': 1}, {'adx': 28})
        # json.dumps 不触发 default=str：numpy 标量会被 stringify → _eval_indicator 判 None → 误剔除
        text = json.dumps(snap, ensure_ascii=False)
        self.assertIn('rsi_14', text)
        self.assertIn('golden', text)

    def test_signal_fields_present(self):
        snap = build_tech_snapshot(self._mk_tech(), {'ma_arrangement': 2, 'volume_price': 1}, {'adx': 28})
        t = snap['tech']
        self.assertEqual(t['kdj_signal'], 'gold_cross')
        self.assertEqual(t['macd_status'], 'golden')
        self.assertEqual(snap['market']['ma_arrangement'], 2)
        self.assertEqual(snap['adx']['adx'], 28)

    def test_numeric_threshold_on_snapshot(self):
        # 快照数值可直接喂给 _evaluate_tech_conditions 做阈值比较
        snap = build_tech_snapshot(self._mk_tech(), {}, {'adx': 28})
        self.assertTrue(UnifiedEntryLogic._evaluate_tech_conditions(
            [{'indicator': 'rsi_14', 'op': '<', 'value': 40}],
            snap['tech'], snap['market'], 100, snap['adx']))


class TestCustomIndicatorInSnapshot(unittest.TestCase):
    """自定义公式因子（cf_*）并入快照 → 「技术指标筛选」可按自定义指标设阈值。

    数据链：自定义因子原始值随扫描结果落盘（factor_values）→ build_tech_snapshot 只把
    cf_ 前缀键写进 tech_snapshot.tech（判定器 _eval_indicator 对任意键做数值比较）。
    """

    @staticmethod
    def _snap(**kw):
        return build_tech_snapshot({'rsi_14': 30.0}, {}, {'adx': 28}, **kw)

    def test_custom_factor_enters_snapshot(self):
        snap = self._snap(factor_values={'cf_my_rsi': 42.5})
        self.assertEqual(snap['tech']['cf_my_rsi'], 42.5)
        json.dumps(snap, ensure_ascii=False)  # 必须仍可 JSON 序列化

    def test_custom_factor_threshold_hit_and_miss(self):
        snap = self._snap(factor_values={'cf_my_rsi': 42.5})
        tech = snap['tech']
        self.assertTrue(UnifiedEntryLogic._evaluate_tech_conditions(
            [{'indicator': 'cf_my_rsi', 'op': '>=', 'value': 40}], tech, {}, 100, {}))
        self.assertFalse(UnifiedEntryLogic._evaluate_tech_conditions(
            [{'indicator': 'cf_my_rsi', 'op': '>=', 'value': 50}], tech, {}, 100, {}))

    def test_builtin_factor_raw_value_not_injected(self):
        # factor_values 里也含内置因子原始值；只收 cf_ 前缀，避免污染 tech 命名空间/覆盖内置键
        snap = self._snap(factor_values={'cf_ok': 1.0, 'rsi_14': 88.0, 'macd': 9.9})
        self.assertEqual(snap['tech']['cf_ok'], 1.0)
        self.assertEqual(snap['tech']['rsi_14'], 30.0)   # 仍是 tech 原值，未被 factor_values 覆盖
        self.assertNotIn('macd', snap['tech'])

    def test_missing_custom_factor_blocks(self):
        # 未启用/未在快照中 → 条件不满足，AND 下不误放行（旧缓存即此情形，需重扫）
        snap = self._snap(factor_values={'cf_other': 1.0})
        self.assertFalse(UnifiedEntryLogic._evaluate_tech_conditions(
            [{'indicator': 'cf_my_rsi', 'op': '>=', 'value': 0}], snap['tech'], {}, 100, {}))

    def test_nonnumeric_custom_value_blocks(self):
        snap = self._snap(factor_values={'cf_bad': 'nan-ish'})
        self.assertFalse(UnifiedEntryLogic._evaluate_tech_conditions(
            [{'indicator': 'cf_bad', 'op': '>=', 'value': 0}], snap['tech'], {}, 100, {}))

    def test_none_factor_values_is_backward_compatible(self):
        snap = self._snap()
        self.assertNotIn('cf_ok', snap['tech'])
        self.assertEqual(snap['tech']['rsi_14'], 30.0)


class TestScanFilterConfigMapping(unittest.TestCase):
    """scanFilter(前端 quantData) <-> scan_tech_filter(后端 config) 方案级映射闭环。"""

    def test_round_trip(self):
        quant = _default_quant_data()
        quant['scanFilter'] = [{'signal': 'macd golden'}, {'indicator': 'rsi_14', 'op': '<=', 'value': 30}]
        cfg = _quant_data_to_config(quant)
        self.assertEqual(cfg['scan_tech_filter'], quant['scanFilter'])
        back = _config_to_quant_data(cfg, _default_quant_data())
        self.assertEqual(back['scanFilter'], quant['scanFilter'])

    def test_empty_round_trip(self):
        quant = _default_quant_data()
        quant['scanFilter'] = []
        cfg = _quant_data_to_config(quant)
        self.assertEqual(cfg.get('scan_tech_filter'), [])
        back = _config_to_quant_data(cfg, _default_quant_data())
        self.assertEqual(back.get('scanFilter'), [])


if __name__ == '__main__':
    unittest.main()