#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""初/高级方案隔离测试（按用法模式解析方案）。

背景：方案 _meta 增加 mode 字段（'basic'/'advanced'），初/高级各自独立方案。
load_market_scheme 默认按当前全局模式（get_usage_mode()）隔离解析：
  - 当前高级 → 只解析 _meta.mode=='advanced' 或未标 mode 的方案（legacy 兜底归高级）；
  - 当前初级 → 只解析 _meta.mode=='basic' 的方案；
  - 未标 mode 的 legacy 方案兜底归属「高级」（沿用旧版量化配置，初级按技术条件独立建档），
    故初级模式下不可解析到 legacy 方案。
本文件不依赖真实行情，仅验证「方案按模式匹配」的解风格。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import quant_config


def _mk_schemes(obj_override):
    """用最小可辨识的 scheme 结构替换 _MODEL，避免依赖真实配置文件。"""
    base = {
        'schemes': {
            '基础-初级': {'desc': '', '_meta': {'market': 'stock', 'period': '日K', 'mode': 'basic'}},
            '精细-高级': {'desc': '', '_meta': {'market': 'stock', 'period': '日K', 'mode': 'advanced'}},
            '通用-无mode': {'desc': '', '_meta': {'market': 'stock', 'period': '日K'}},
        },
        'current_scheme': None,
        'factor_profiles': {},
    }
    base.update(obj_override)
    return base


class TestSchemeModeIsolation(unittest.TestCase):
    def setUp(self):
        self._saved_mode = quant_config.get_usage_mode
        self._saved_model = quant_config._MODEL

    def tearDown(self):
        quant_config.get_usage_mode = self._saved_mode
        quant_config._MODEL = self._saved_model

    def test_advanced_ignores_basic_scheme(self):
        quant_config._MODEL = _mk_schemes({})
        quant_config.get_usage_mode = lambda: 'advanced'
        name = quant_config.resolve_scheme('stock', 'long', '日K', mode='advanced')
        self.assertIsNotNone(name)
        meta = quant_config.get_scheme_meta(name)
        self.assertNotEqual(meta.get('mode'), 'basic', '高级模式不应解析到初级方案')
        self.assertIn(meta.get('mode'), ('advanced', None))

    def test_basic_ignores_advanced_scheme(self):
        quant_config._MODEL = _mk_schemes({})
        quant_config.get_usage_mode = lambda: 'basic'
        name = quant_config.resolve_scheme('stock', 'long', '日K', mode='basic')
        self.assertIsNotNone(name)
        meta = quant_config.get_scheme_meta(name)
        self.assertNotEqual(meta.get('mode'), 'advanced', '初级模式不应解析到高级方案')

    def test_legacy_unmoded_scheme_defaults_to_advanced(self):
        # 未标 mode 的 legacy 方案兜底归属「高级」：高级可解析、初级不可解析。
        quant_config._MODEL = _mk_schemes({'schemes': {
            '通用-无mode': {'desc': '', '_meta': {'market': 'stock', 'period': '日K'}},
        }})
        quant_config.get_usage_mode = lambda: 'basic'
        self.assertIsNone(quant_config.resolve_scheme('stock', 'long', '日K', mode='basic'),
                          '初级模式不应解析到归高级的 legacy 方案')
        quant_config.get_usage_mode = lambda: 'advanced'
        self.assertEqual(quant_config.resolve_scheme('stock', 'long', '日K', mode='advanced'), '通用-无mode')

    def test_load_market_scheme_filters_by_current_mode(self):
        # 高级模式：load_market_scheme 内部按 get_usage_mode()=='advanced' 隔离，不应命中初级方案
        quant_config._MODEL = _mk_schemes({})
        quant_config.get_usage_mode = lambda: 'advanced'
        cfg = quant_config.load_market_scheme('stock', 'long', '日K', force_reload=False)
        used = quant_config.get_current_scheme_name()
        meta = quant_config.get_scheme_meta(used)
        self.assertNotEqual(meta.get('mode'), 'basic')

    def test_no_mode_means_no_isolation(self):
        # 显式传 mode='' 表示不做模式隔离（兼容遗留）
        quant_config._MODEL = _mk_schemes({})
        quant_config.get_usage_mode = lambda: 'basic'
        name = quant_config.resolve_scheme('stock', 'long', '日K', mode='')
        self.assertIsNotNone(name)


class TestBasicSchemeSeed(unittest.TestCase):
    """初级方案新建即带默认建仓档位（否则「切初级→新建方案→个股分析」必卡 entry_params）。

    根因链：TradingPipeline.execute 无条件 require_config(entry_params)，而新建方案的
    config 是空白种子（entry_params={}）。初级定位「打开即用」⇒ 种子预置默认档位。
    """
    def setUp(self):
        import tempfile
        self._tmp = tempfile.mkdtemp(prefix='mbull_basic_seed_')
        self._saved_file = quant_config.QUANT_MODEL_FILE
        self._saved_model = quant_config._MODEL
        quant_config.QUANT_MODEL_FILE = os.path.join(self._tmp, 'config', 'quant_model.json')
        quant_config._MODEL = {
            'schemes': {}, 'current_scheme': None, 'factor_profiles': {},
        }

    def tearDown(self):
        quant_config.QUANT_MODEL_FILE = self._saved_file
        quant_config._MODEL = self._saved_model
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_basic_scheme_gets_default_positions(self):
        quant_config.add_scheme('初级方案', meta={'market': 'stock', 'direction': 'long',
                                                  'period': '日K', 'mode': 'basic'})
        ep = (quant_config.get_schemes()['初级方案']['config'] or {}).get('entry_params') or {}
        pos = ep.get('positions') or {}
        self.assertTrue(pos, '初级方案必须预置建仓档位，否则分析直接被 entry_params 卡住')
        self.assertEqual(pos.get('strong'), 0.20)
        self.assertEqual(pos.get('standard'), 0.15)
        # 预置后 require_config 不再抛「建仓参数」
        quant_config.switch_scheme('初级方案')
        try:
            quant_config.require_config(quant_config._safe_cfg(),
                                        [('建仓参数(entry_params)', 'entry_params')])
        except quant_config.ConfigIncompleteError as e:  # pragma: no cover
            self.fail(f'初级方案仍被判缺配置：{e.missing}')

    def test_advanced_scheme_stays_blank(self):
        quant_config.add_scheme('高级方案', meta={'market': 'stock', 'direction': 'long',
                                                 'period': '日K', 'mode': 'advanced'})
        ep = (quant_config.get_schemes()['高级方案']['config'] or {}).get('entry_params') or {}
        self.assertEqual(ep, {}, '高级方案保持纯空白种子（纯去默认原则不变）')

    def test_explicit_config_wins(self):
        cfg = {'entry_params': {'positions': {'strong': 0.5}}}
        quant_config.add_scheme('指定配置', meta={'market': 'stock', 'mode': 'basic'}, config=cfg)
        got = quant_config.get_schemes()['指定配置']['config']['entry_params']
        self.assertEqual(got['positions']['strong'], 0.5, '显式传入的 config 优先于默认种子')


if __name__ == '__main__':
    unittest.main()