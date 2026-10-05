#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""config_mapper 回归测试：锁定从 web_api.py 收敛到 ui/config_mapper.py 的映射行为。

覆盖：
- 默认 quantData 结构 `_default_quant_data`（关键节存在性）
- config <-> quantData 往返：阈值/技术信号/否决项/建仓/回测/风控/幽灵 保值
- 保存前校验 `_validate_quant_before_save`（skip_factor_check 与阈值递减）
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ui.config_mapper import (
    _default_quant_data, _config_to_quant_data, _quant_data_to_config,
    _validate_quant_before_save,
)
from engine import quant_config


def _sample_config():
    """构造一份含关键分组后端 config（值取自默认语义，仅为往返断言）。"""
    keys = quant_config.VETO_KEYS
    return {
        'active_factors': ['rsi_value'],
        'factor_configs': {
            'rsi_value': {'weight': 0.15, 'direction': 1, 'params': {'period': 14},
                          'stats': {'mean': 50, 'std': 10}, 'ic': 0.05, 'ic_ir': 0.02},
        },
        'thresholds': {'strong': 22, 'standard': 20, 'test': 10, 'pending': -5,
                       'rebound': -2, 'panic_rebound': -8},
        'entry_conditions': {
            'strong': {'tech_signal': 'bull_align', 'veto_on': True,
                       'veto_enabled': {k: True for k in keys}},
            'standard': {'tech_signal': 'none', 'veto_on': False,
                         'veto_enabled': {k: False for k in keys}},
            'test': {'tech_signal': 'above_ma20', 'veto_on': True,
                     'veto_enabled': {k: False for k in keys}},
            'pending': {'tech_signal': 'none', 'veto_on': False,
                        'veto_enabled': {k: False for k in keys}},
            'rebound': {'tech_signal': 'none', 'veto_on': False, 'veto_enabled': {}},
            'panic_rebound': {'tech_signal': 'none', 'veto_on': False, 'veto_enabled': {}},
        },
        'entry_params': {
            'positions': {'strong': 0.2, 'standard': 0.15, 'test': 0.08,
                          'observe': 0.03, 'none': 0.0, 'rebound': 0.05,
                          'panic_rebound': 0.02, 'top_reversal': 0.05},
            'reversal_score_threshold': 4.0, 'panic_reversal_threshold': 5.0,
        },
        'score_scale': {'weight_multiplier': 1.0, 'z_truncate_min': -3.0,
                        'z_truncate_max': 3.0, 'score_min': 0.0, 'score_max': 100.0},
        'add_params': {'vol_mult': 2.0, 'max_add_ratio': 0.3, 'add_tiers': {}},
        'reduce_params': {},
        'risk_params': {'total_position_cap_pct': 0.5, 'single_max_loss_pct': 0.08,
                        'market_crash_index': 'sh000300'},
        'backtest': {'universe': '上证50+创业50+科创50', 'forward_days': 30,
                     'scan_interval': 5, 'output_prefix': 'bt',
                     'fut_pool': 'all', 'fut_dir': 'long', 'fut_period': '日K',
                     'fut_days': 300, 'fut_multi_horizon': False},
        'market_gate': {'enabled': False, 'envs': {}},
        'ghost_rules': {'grace_period_days': 3, 'profit_threshold': 0.5,
                        'rsi_confirm': 45, 'ma_confirm': 'sma_20'},
    }


class TestDefaultQuantData(unittest.TestCase):
    def test_key_sections_present(self):
        d = _default_quant_data()
        self.assertIn('factors', d)
        self.assertIn('thresholds', d)
        keys = {t['key'] for t in d['thresholds']}
        for want in ('strong', 'standard', 'test', 'pending', 'rebound', 'panic_rebound'):
            self.assertIn(want, keys)
        self.assertIn('addTiers', d)
        self.assertIn('reduceTiers', d)
        self.assertIn('risk', d)
        self.assertIn('ghost', d)
        self.assertIn('backtest', d)
        self.assertTrue(d['factors'], '至少内置一组因子行')


class TestRoundTrip(unittest.TestCase):
    def test_key_fields_survive_round_trip(self):
        cfg = _sample_config()
        qd = _config_to_quant_data(cfg, direction='long')
        out = _quant_data_to_config(qd, direction='long')

        # 阈值
        for k, v in cfg['thresholds'].items():
            self.assertEqual(out['thresholds'][k], v, f'threshold {k}')

        # 技术信号
        self.assertEqual(out['entry_conditions']['strong']['tech_signal'], 'bull_align')
        self.assertEqual(out['entry_conditions']['test']['tech_signal'], 'above_ma20')
        self.assertEqual(out['entry_conditions']['strong']['veto_on'], True)
        self.assertEqual(out['entry_conditions']['strong']['veto_enabled']['rsi_extreme'], True)

        # 建仓仓位（0-1 小数）
        self.assertEqual(out['entry_params']['positions']['strong'], 0.2)

        # 风控（0-1 小数）
        self.assertEqual(out['risk_params']['total_position_cap_pct'], 0.5)
        self.assertEqual(out['risk_params']['single_max_loss_pct'], 0.08)
        self.assertEqual(out['risk_params'].get('market_crash_index'), 'sh000300')

        # 回测设置不写入方案（2026-09-19 决策）：_quant_data_to_config 不再产出 backtest 段。
        # 回测池/年数/前瞻/扫描间隔等属「运行面板」临时选择，由前端 startBacktest 经
        # web_api.start_backtest 的 _run_params 直传子进程，不随方案持久化。
        self.assertNotIn('backtest', out)

        # 幽灵规则：数值字段保值；非数字的 ma_confirm 目前在 _quant_data_to_config
        # 中会被 _norm_num→None→continue 丢弃（原 web_api 遗留问题，本单测仅锁定现行为）
        self.assertEqual(out['ghost_rules']['grace_period_days'], 3)
        self.assertNotIn('ma_confirm', out['ghost_rules'])


class TestValidateBeforeSave(unittest.TestCase):
    def _data(self, enabled=True, thresholds=None):
        thr = thresholds or [
            {'key': 'strong', 'val': 22, 'signal': '无要求', 'veto_on': False, 'veto_enabled': {}},
            {'key': 'standard', 'val': 20, 'signal': '无要求', 'veto_on': False, 'veto_enabled': {}},
            {'key': 'test', 'val': 10, 'signal': '无要求', 'veto_on': False, 'veto_enabled': {}},
            {'key': 'pending', 'val': -5, 'signal': '无要求', 'veto_on': False, 'veto_enabled': {}},
            {'key': 'rebound', 'val': -2, 'signal': '无要求', 'veto_on': False, 'veto_enabled': {}},
            {'key': 'panic_rebound', 'val': -8, 'signal': '无要求', 'veto_on': False, 'veto_enabled': {}},
        ]
        return {
            'factors': [{'name': 'rsi_value', 'enabled': enabled}],
            'thresholds': thr,
        }

    def test_skip_factor_check_allows_no_factor(self):
        self.assertIsNone(_validate_quant_before_save(self._data(enabled=False),
                                                      skip_factor_check=True))

    def test_requires_factor_without_skip(self):
        self.assertIn('至少启用一个因子', _validate_quant_before_save(self._data(enabled=False)))

    def test_ok_when_factor_enabled_and_valid(self):
        self.assertIsNone(_validate_quant_before_save(self._data(enabled=True)))

    def test_decreasing_boundary_enforced(self):
        bad = [{'key': 'strong', 'val': 5, 'signal': '无要求', 'veto_on': False, 'veto_enabled': {}},
               {'key': 'standard', 'val': 10, 'signal': '无要求', 'veto_on': False, 'veto_enabled': {}},
               {'key': 'test', 'val': 10, 'signal': '无要求', 'veto_on': False, 'veto_enabled': {}},
               {'key': 'pending', 'val': -5, 'signal': '无要求', 'veto_on': False, 'veto_enabled': {}},
               {'key': 'rebound', 'val': -2, 'signal': '无要求', 'veto_on': False, 'veto_enabled': {}},
               {'key': 'panic_rebound', 'val': -8, 'signal': '无要求', 'veto_on': False, 'veto_enabled': {}},
        ]
        err = _validate_quant_before_save(self._data(enabled=True, thresholds=bad))
        self.assertIsNotNone(err)
        self.assertIn('递减', err)


if __name__ == '__main__':
    unittest.main()