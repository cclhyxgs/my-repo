#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""期货周期感知因子：窗口缩放 / 周期透传 / 分钟禁年线 / 期货中性门控 烟雾测试。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.quant_config as qc
import engine.score_calculator_v2 as sc
from engine.unified_entry_logic import UnifiedEntryLogic
from engine import unified_scorer as us
from engine.market_gate import MarketGate


def test_window_scaling():
    base = {
        'relative_strength_20d': {'weight': 0.1, 'direction': 1, 'params': {'period': 20}, 'stats': {'mean': 0.02, 'std': 0.15}},
        'rsi_value': {'weight': 0.1, 'direction': 1, 'params': {'period': 14}, 'stats': {'mean': 50, 'std': 15}},
        'chip_concentration': {'weight': 0.1, 'direction': 1, 'params': {'bins': 50}, 'stats': {'mean': 0.3, 'std': 0.2}},
    }
    scaled = qc.get_period_scaled_factor_configs(base, '15分钟')
    assert scaled['relative_strength_20d']['params']['period'] == 320, scaled['relative_strength_20d']
    assert scaled['rsi_value']['params']['period'] == 224, scaled['rsi_value']
    assert scaled['chip_concentration']['params']['bins'] == 50, 'bins 不应缩放'
    daily = qc.get_period_scaled_factor_configs(base, '日K')
    assert daily['relative_strength_20d']['params']['period'] == 20
    print('  [OK] 窗口缩放: 20日->320(15m), 14->224, bins不变, 日线原样')


def test_period_passthrough():
    n = 400
    closes = [100.0 + i * 0.01 for i in range(n)]
    closes[-1] = closes[-2] * 1.05
    data_list = [{'close': c, 'high': c, 'low': c, 'open': c, 'volume': 1000, 'date': None} for c in closes]
    ctx = {'closes': closes, 'highs': closes, 'lows': closes, 'volumes': [1000] * n,
           'opens': closes, 'data_list': data_list, 'latest_price': closes[-1], 'tech': {}, 'market': {}}
    one = {'relative_strength_20d': {'weight': 0.1, 'direction': 1, 'params': {'period': 20}, 'stats': {'mean': 0.02, 'std': 0.15}}}
    orig_af, orig_fc = qc.get_active_factors, qc.get_factor_configs
    qc.get_active_factors = lambda: ['relative_strength_20d']
    qc.get_factor_configs = lambda: one
    try:
        f_daily = sc.ScoreCalculatorV2.extract_factors(ctx['tech'], ctx['market'], closes, closes, closes,
                                                        [1000] * n, closes, data_list, closes[-1], '日K')
        f_min = sc.ScoreCalculatorV2.extract_factors(ctx['tech'], ctx['market'], closes, closes, closes,
                                                      [1000] * n, closes, data_list, closes[-1], '15分钟')
        vd, vm = f_daily['relative_strength_20d'], f_min['relative_strength_20d']
        assert vd != vm, (vd, vm)
        print(f'  [OK] 周期透传: 日线RS={vd:.4f} vs 15分钟RS={vm:.4f} (窗口已缩放)')
    finally:
        qc.get_active_factors, qc.get_factor_configs = orig_af, orig_fc


def test_minute_skip_annual_veto():
    tech = {'ma250_break': True, 'rsi_extreme': True}
    orig_vk, orig_ec = qc.veto_keys_for_level, qc.get_entry_conditions
    qc.veto_keys_for_level = lambda lk, d='long': {'ma250_break', 'rsi_extreme'}
    qc.get_entry_conditions = lambda: {'strong': {'veto_on': True, 'veto_enabled': {'ma250_break': True, 'rsi_extreme': True}}}
    try:
        hit_daily, _, _ = UnifiedEntryLogic.check_level_veto('strong', tech, with_suffix=False, direction='long', period='日K')
        hit_min, _, _ = UnifiedEntryLogic.check_level_veto('strong', tech, with_suffix=False, direction='long', period='15分钟')
        jd, jm = ''.join(hit_daily), ''.join(hit_min)
        assert '年线' in jd and '年线' not in jm, (jd, jm)
        assert 'RSI' in jm, jm
        print(f'  [OK] 分钟禁年线: 日线命中{set(hit_daily)} / 15分钟命中{set(hit_min)} (年线已跳过)')
    finally:
        qc.veto_keys_for_level, qc.get_entry_conditions = orig_vk, orig_ec


def test_futures_neutral_gate():
    # 期货恒中性：即便高 up_ratio 也不套 A 股环境乘子
    env_fut = MarketGate.get_environment(0.95, 'futures')
    env_stock_disabled = MarketGate.get_environment(0.95, 'stock')
    assert env_fut['factor'] == 1.0, env_fut
    res_fut = us.UnifiedScorer.calculate_final_score(50, 0.5, 'futures')
    assert res_fut['market_score'] == 0 and res_fut['final_score'] == 50.0, res_fut
    print(f'  [OK] 期货中性门控: factor={env_fut["factor"]}, final_score={res_fut["final_score"]}, market_score={res_fut["market_score"]}')


if __name__ == '__main__':
    test_window_scaling()
    test_period_passthrough()
    test_minute_skip_annual_veto()
    test_futures_neutral_gate()
    print('ALL_SMOKE_OK')
