#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""方案导出 / 导入往返测试（v2 自包含格式 + 旧格式兼容）。

背景（2026-09-21 修复）：引用 factor_profile 的方案，其因子三键只存在
factor_profiles 里、内联 config 为空；旧导出直接吐 config，导入端又强制要求
config 含 active_factors/factor_configs ⇒ **自己导出的方案自己导不进来**。

本文件锁死契约：
  1. 导出文件自包含（因子段内联展开 + 携带被引用的 profile 快照）；
  2. 导入后方案可正常加载（_merge_scheme_config 能取回因子）；
  3. 4 种历史格式（v2 / 旧库 / 旧单方案完整对象 / 旧 flat config）都能导入；
  4. 不静默覆盖已有方案（除非 overwrite=True）。
"""
import copy
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# engine 在 import 期就按 QUANT_SYSTEM_DIR 定数据目录：必须在导入前设置，
# 否则测试会往项目 cache/ 甚至 %LOCALAPPDATA% 写盘。
_TMP_ROOT = tempfile.mkdtemp(prefix='mbull_scheme_test_')
os.environ['QUANT_SYSTEM_DIR'] = _TMP_ROOT
os.environ['MBULL_DISABLE_LEGACY_MIGRATE'] = '1'

from engine import quant_config  # noqa: E402
from ui import web_api  # noqa: E402

QUANT_MODEL_FILE = os.path.join(_TMP_ROOT, 'config', 'quant_model.json')
quant_config.QUANT_MODEL_FILE = QUANT_MODEL_FILE


def _profile_body(n=3):
    return {
        'active_factors': ['ma_trend', 'rsi_value', 'volume_ratio'][:n],
        'factor_configs': {
            'ma_trend': {'enabled': True, 'weight': 2.0, 'params': {'period': 20}},
            'rsi_value': {'enabled': True, 'weight': 1.5, 'params': {'period': 14}},
            'volume_ratio': {'enabled': True, 'weight': 1.0, 'params': {}},
        },
        'score_scale': {'buy': 60, 'sell': -20},
    }


def _base_model():
    """当前真实配置的形态：'趋势' 引用 stock profile（内联 config 无因子三键）。"""
    return {
        'schemes': {
            '趋势': {
                'desc': '主方案',
                '_meta': {'market': 'stock', 'direction': 'long', 'period': '日K'},
                'factor_profile': 'stock',
                'config': {
                    'thresholds': {'buy': 60, 'sell': -20},
                    'risk_params': {'stop_loss_pct': 6, 'sell_price_mode': 'next_open'},
                    'add_params': {'tiers': [{'pct': 3, 'ratio': 0.3}]},
                },
            },
            '期货_多单': {
                'desc': '期货',
                '_meta': {'market': 'futures', 'direction': 'long', 'period': '日K'},
                'factor_profile': 'futures',
                'config': {'thresholds': {'buy': 55}},
            },
        },
        'current_scheme': '趋势',
        'factor_profiles': {'stock': _profile_body(), 'futures': _profile_body(2)},
    }


class _SchemeApiCase(unittest.TestCase):
    def setUp(self):
        self.api = web_api.WebAPI.__new__(web_api.WebAPI)  # 不走 __init__（不需要窗口）
        self._write_model(_base_model())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_TMP_ROOT, ignore_errors=True)

    def _write_model(self, model):
        os.makedirs(os.path.dirname(QUANT_MODEL_FILE), exist_ok=True)
        with open(QUANT_MODEL_FILE, 'w', encoding='utf-8') as f:
            json.dump(model, f, ensure_ascii=False, indent=2)
        quant_config._MODEL = None
        quant_config._CURRENT = None
        quant_config._cache = None
        quant_config.load_config(force_reload=True)

    def _model(self):
        with open(QUANT_MODEL_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)

    def _export(self, name, mode='current'):
        r = self.api.export_scheme(mode=mode, scheme_name=name)
        self.assertTrue(r.get('success'), r.get('error'))
        return r

    def _merged(self, name):
        return quant_config._merge_scheme_config(name) or {}


class TestExportPayload(_SchemeApiCase):
    def test_current_scheme_export_is_self_contained(self):
        """引用 profile 的方案：导出文件必须内联携带因子三键 + profile 快照。"""
        r = self._export('趋势')
        doc = json.loads(r['json'])
        self.assertEqual(doc['format'], 'mbull-scheme')
        self.assertEqual(doc['version'], 2)
        self.assertEqual(doc['kind'], 'scheme')
        body = doc['schemes']['趋势']
        self.assertEqual(body['factor_profile'], 'stock')
        self.assertEqual(body['config']['active_factors'], ['ma_trend', 'rsi_value', 'volume_ratio'])
        self.assertIn('score_scale', body['config'])
        self.assertIn('stock', doc['factor_profiles'])
        # 身份与非因子段不丢
        self.assertEqual(body['_meta']['market'], 'stock')
        self.assertEqual(body['config']['thresholds'], {'buy': 60, 'sell': -20})

    def test_library_export_contains_all_schemes(self):
        r = self._export(None, mode='all')
        doc = json.loads(r['json'])
        self.assertEqual(doc['kind'], 'library')
        self.assertEqual(set(doc['schemes'].keys()), {'趋势', '期货_多单'})
        self.assertEqual(set(doc['factor_profiles'].keys()), {'stock', 'futures'})


class TestImportRoundTrip(_SchemeApiCase):
    def test_export_then_import_restores_factors(self):
        """核心回归：自己导出的方案必须能自己导回来，且因子不丢。"""
        r = self._export('趋势')
        res = self.api.import_scheme(r['json'], scheme_name='备份_趋势')
        self.assertTrue(res.get('success'), res.get('error'))
        self.assertEqual(res['switched_to'], '备份_趋势')

        merged = self._merged('备份_趋势')
        self.assertEqual(merged.get('active_factors'), ['ma_trend', 'rsi_value', 'volume_ratio'])
        self.assertEqual(merged.get('factor_configs', {}).get('ma_trend', {}).get('weight'), 2.0)
        self.assertEqual(merged.get('thresholds'), {'buy': 60, 'sell': -20})
        # 因子段回写到 profile（方案引用 profile，内联不含因子键）
        self.assertEqual(self._model()['schemes']['备份_趋势']['factor_profile'], 'stock')

    def test_legacy_single_object_without_factor_keys(self):
        """旧导出格式（config 里没有因子三键）也必须能导入 —— 历史 bug 现场。"""
        legacy = json.dumps({
            'desc': '旧导出',
            '_meta': {'market': 'stock', 'direction': 'long', 'period': '日K'},
            'factor_profile': 'stock',
            'config': {'thresholds': {'buy': 60}},
        }, ensure_ascii=False)
        res = self.api.import_scheme(legacy, scheme_name='旧格式方案')
        self.assertTrue(res.get('success'), res.get('error'))
        # 本机已有 stock profile 且导入内容无因子段 → 沿用本地 profile
        merged = self._merged('旧格式方案')
        self.assertEqual(merged.get('active_factors'), ['ma_trend', 'rsi_value', 'volume_ratio'])

    def test_legacy_flat_config(self):
        flat = json.dumps({
            'active_factors': ['rsi_value'],
            'factor_configs': {'rsi_value': {'enabled': True, 'weight': 1.0}},
            'score_scale': {'buy': 50},
            'thresholds': {'buy': 50},
        }, ensure_ascii=False)
        res = self.api.import_scheme(flat, scheme_name='平铺方案')
        self.assertTrue(res.get('success'), res.get('error'))
        merged = self._merged('平铺方案')
        self.assertEqual(merged.get('active_factors'), ['rsi_value'])

    def test_legacy_library_format(self):
        lib = json.dumps({'current_scheme': '趋势', 'schemes': {
            '趋势': {'desc': '', '_meta': {'market': 'stock', 'direction': 'long', 'period': '日K'},
                     'factor_profile': 'stock', 'config': {'thresholds': {'buy': 60}}},
            '期货_多单': {'desc': '', '_meta': {'market': 'futures', 'direction': 'long', 'period': '日K'},
                          'factor_profile': 'futures', 'config': {}},
        }}, ensure_ascii=False)
        res = self.api.import_scheme(lib)
        self.assertTrue(res.get('success'), res.get('error'))
        self.assertEqual(res['mode'], 'multi')
        self.assertEqual(res['overwritten'], 2)  # 库导入默认覆盖同名

    def test_bad_file_reports_readable_error(self):
        res = self.api.import_scheme('{"foo": 1}')
        self.assertFalse(res['success'])
        self.assertIn('无法识别', res['error'])
        res2 = self.api.import_scheme('not json at all')
        self.assertFalse(res2['success'])
        self.assertIn('JSON 解析失败', res2['error'])

    def test_no_silent_overwrite_and_overwrite_flag(self):
        r = self._export('趋势')
        res = self.api.import_scheme(r['json'], scheme_name='趋势')
        self.assertTrue(res.get('success'), res.get('error'))
        self.assertEqual(res['switched_to'], '趋势_1', '同名不得静默覆盖，应自动改名')
        self.assertIn('趋势_1', self._model()['schemes'])

        res2 = self.api.import_scheme(r['json'], scheme_name='趋势', overwrite=True)
        self.assertTrue(res2.get('success'), res2.get('error'))
        self.assertEqual(res2['overwritten'], 1)
        self.assertEqual(res2['switched_to'], '趋势')

    def test_profile_conflict_uses_derived_name(self):
        """跨机导入：本机 profile 与文件不同 → 派生名，不得污染共享 profile。"""
        doc = json.loads(self._export('趋势')['json'])
        doc['schemes']['趋势']['config']['factor_configs']['ma_trend']['weight'] = 9.9
        res = self.api.import_scheme(json.dumps(doc, ensure_ascii=False), scheme_name='外来方案')
        self.assertTrue(res.get('success'), res.get('error'))
        model = self._model()
        fp = model['schemes']['外来方案']['factor_profile']
        self.assertEqual(fp, 'stock@外来方案')
        # 原共享 profile 未被改动
        self.assertEqual(model['factor_profiles']['stock']['factor_configs']['ma_trend']['weight'], 2.0)
        self.assertEqual(model['factor_profiles'][fp]['factor_configs']['ma_trend']['weight'], 9.9)

    def test_failed_import_rolls_back(self):
        """保存失败时不得留下空壳方案（save_current_scheme 打桩返回 False）。"""
        r = self._export('趋势')
        orig_save = quant_config.save_current_scheme
        quant_config.save_current_scheme = lambda cfg: False
        try:
            res = self.api.import_scheme(r['json'], scheme_name='半截方案')
        finally:
            quant_config.save_current_scheme = orig_save
        self.assertFalse(res['success'])
        self.assertNotIn('半截方案', self._model()['schemes'])


if __name__ == '__main__':
    unittest.main()
