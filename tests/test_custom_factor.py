#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""自定义公式因子回归测试（全中文公式语言 + 注册/配置/API 集成）。

覆盖：
- 词法/解析：全角归一化、注释、保留字、未定义变量、重复定义、未知函数、参数个数、长度/语句数上限
- 算子正确性：均线/指数均线/平滑/最低N日/最高N日/前值/求和/标准差/上穿/绝对值/最大/最小
- 安全性：无 eval/exec，未知函数与属性访问被拒
- 预设模板全部可编译且可求值
- factor_registry：自定义因子注册/求值/移除（与内置因子同构）
- quant_config：保存/列表/删除/重载（含落盘与注册表同步）
- config_mapper：自定义因子在前端 quantData 往返中保留公式与 custom 标记
- WebAPI：meta/validate/list/save/delete/preview/try（取数打桩，不联网）
"""
import os
import shutil
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import custom_factor as cf
from engine import factor_registry
from engine import quant_config


def _ctx(closes, highs=None, lows=None, opens=None, volumes=None):
    n = len(closes)
    return {
        'closes': [float(x) for x in closes],
        'highs': [float(x) for x in (highs if highs is not None else closes)],
        'lows': [float(x) for x in (lows if lows is not None else closes)],
        'opens': [float(x) for x in (opens if opens is not None else closes)],
        'volumes': [float(x) for x in (volumes if volumes is not None else [1] * n)],
    }


def _series(formula, ctx):
    return cf.compile_formula(formula).eval_series(ctx)


def _last(formula, ctx):
    return cf.compile_formula(formula).eval_scalar(ctx)


# ============================================================
# 词法 / 解析
# ============================================================

class TestLexerParser(unittest.TestCase):
    def test_fullwidth_normalized(self):
        self.assertEqual(cf.normalize_fullwidth('（１，２）'), '(1,2)')
        # 全角标点 + 全角数字 + 全角比较符
        r = cf.validate_formula('输出 := 收盘价 ＞ 均线（收盘价，５）;')
        self.assertTrue(r['ok'], r['errors'])

    def test_comment_stripped(self):
        r = cf.validate_formula('输出 := 收盘价; # 这是注释')
        self.assertTrue(r['ok'], r['errors'])

    def test_empty_raises(self):
        self.assertFalse(cf.validate_formula('')['ok'])
        self.assertFalse(cf.validate_formula('   ')['ok'])

    def test_unknown_function_raises(self):
        r = cf.validate_formula('输出 := 不存在的函数(收盘价);')
        self.assertFalse(r['ok'])
        self.assertIn('未知函数', r['errors'][0]['msg'])

    def test_undefined_variable_raises(self):
        r = cf.validate_formula('输出 := 未定义变量 + 1;')
        self.assertFalse(r['ok'])
        self.assertIn('未定义', r['errors'][0]['msg'])

    def test_duplicate_variable_raises(self):
        r = cf.validate_formula('A := 1; A := 2; 输出 := A;')
        self.assertFalse(r['ok'])
        self.assertIn('重复定义', r['errors'][0]['msg'])

    def test_reserved_name_raises(self):
        r = cf.validate_formula('收盘价 := 1;')
        self.assertFalse(r['ok'])
        self.assertIn('保留字', r['errors'][0]['msg'])

    def test_output_name_is_legal_variable(self):
        r = cf.validate_formula('输出 := 收盘价;')
        self.assertTrue(r['ok'], r['errors'])
        self.assertEqual(r['output'], '输出')

    def test_default_output_is_last_assignment(self):
        r = cf.validate_formula('A := 收盘价; B := A * 2;')
        self.assertTrue(r['ok'], r['errors'])
        self.assertEqual(r['output'], 'B')

    def test_arity_error(self):
        r = cf.validate_formula('输出 := 均线(收盘价);')
        self.assertFalse(r['ok'])
        self.assertIn('参数', r['errors'][0]['msg'])

    def test_unknown_char_raises(self):
        self.assertFalse(cf.validate_formula('输出 := 收盘价 @ 1;')['ok'])

    def test_length_limit(self):
        formula = '输出 := ' + ('1+' * 2000) + '1;'
        r = cf.validate_formula(formula)
        self.assertFalse(r['ok'])
        self.assertIn('长度', r['errors'][0]['msg'])

    def test_statement_limit(self):
        formula = '\n'.join(f'变量{i} := 1;' for i in range(61)) + '\n输出 := 变量0;'
        r = cf.validate_formula(formula)
        self.assertFalse(r['ok'])
        self.assertIn('语句数', r['errors'][0]['msg'])


# ============================================================
# 算子正确性
# ============================================================

class TestOperators(unittest.TestCase):
    def test_ma(self):
        s = _series('输出 := 均线(收盘价, 3);', _ctx([1, 2, 3, 4, 5]))
        self.assertTrue(np.isnan(s[0]) and np.isnan(s[1]))
        np.testing.assert_allclose(s[2:], [2.0, 3.0, 4.0])

    def test_ema_recursion(self):
        s = _series('输出 := 指数均线(收盘价, 3);', _ctx([1, 2, 3, 4, 5]))
        np.testing.assert_allclose(s, [1.0, 1.5, 2.25, 3.125, 4.0625], rtol=1e-9)

    def test_sma_wilder(self):
        s = _series('输出 := 平滑(收盘价, 3, 1);', _ctx([1, 2, 3, 4, 5]))
        self.assertAlmostEqual(s[0], 1.0)
        self.assertAlmostEqual(s[1], 4.0 / 3.0, places=9)
        self.assertAlmostEqual(s[2], (3 + 2 * (4.0 / 3.0)) / 3.0, places=9)

    def test_llv_hhv(self):
        ctx = _ctx([1, 2, 3, 4, 5])
        llv = _series('输出 := 最低N日(收盘价, 3);', ctx)
        hhv = _series('输出 := 最高N日(收盘价, 3);', ctx)
        np.testing.assert_allclose(llv[2:], [1.0, 2.0, 3.0])
        np.testing.assert_allclose(hhv[2:], [3.0, 4.0, 5.0])

    def test_ref(self):
        s = _series('输出 := 前值(收盘价, 2);', _ctx([1, 2, 3, 4, 5]))
        np.testing.assert_allclose(s[2:], [1.0, 2.0, 3.0])

    def test_sum_std(self):
        ctx = _ctx([1, 2, 3, 4, 5])
        s = _series('输出 := 求和(收盘价, 3);', ctx)
        np.testing.assert_allclose(s[2:], [6.0, 9.0, 12.0])
        d = _series('输出 := 标准差(收盘价, 3);', ctx)
        self.assertAlmostEqual(d[4], float(np.std([3, 4, 5])), places=9)

    def test_cross(self):
        ctx = _ctx([1, 2, 3, 4], highs=[2, 2, 2, 2])
        s = _series('输出 := 上穿(收盘价, 最高价);', ctx)
        np.testing.assert_allclose(s, [0.0, 0.0, 1.0, 0.0])

    def test_abs_max_min(self):
        self.assertEqual(_last('输出 := 绝对值(收盘价 - 3);', _ctx([1, 2, 3, 4, 5])), 2.0)
        ctx = _ctx([1, 5, 3], highs=[4, 2, 6])
        mx = _series('输出 := 最大(收盘价, 最高价);', ctx)
        mn = _series('输出 := 最小(收盘价, 最高价);', ctx)
        np.testing.assert_allclose(mx, [4.0, 5.0, 6.0])
        np.testing.assert_allclose(mn, [1.0, 2.0, 3.0])

    def test_english_aliases(self):
        s = _series('输出 := MA(CLOSE, 3);', _ctx([1, 2, 3, 4, 5]))
        np.testing.assert_allclose(s[2:], [2.0, 3.0, 4.0])

    def test_boolean_condition(self):
        ctx = _ctx([1, 2, 3, 4, 5])
        s = _series('输出 := 收盘价 > 3 且 收盘价 < 5;', ctx)
        np.testing.assert_allclose(s, [0.0, 0.0, 0.0, 1.0, 0.0])


# ============================================================
# 安全性
# ============================================================

class TestSecurity(unittest.TestCase):
    def test_no_dunder_call(self):
        r = cf.validate_formula('输出 := __import__("os");')
        self.assertFalse(r['ok'])

    def test_no_attribute_access(self):
        r = cf.validate_formula('输出 := 收盘价.__class__;')
        self.assertFalse(r['ok'])

    def test_no_builtin_names(self):
        for bad in ('输出 := eval("1");', '输出 := exec("1");', '输出 := open("x");'):
            self.assertFalse(cf.validate_formula(bad)['ok'], bad)


# ============================================================
# 预设模板
# ============================================================

class TestTemplates(unittest.TestCase):
    def test_all_templates_compile_and_eval(self):
        ctx = _ctx(list(range(1, 61)), highs=list(range(2, 62)), lows=list(range(0, 60)))
        self.assertTrue(cf.TEMPLATES, '至少有一个模板')
        for name, formula in cf.TEMPLATES.items():
            r = cf.validate_formula(formula)
            self.assertTrue(r['ok'], f'{name}: {r["errors"]}')
            val = _last(formula, ctx)
            self.assertTrue(np.isfinite(val), f'{name} 末值非有限：{val}')

    def test_get_templates_shape(self):
        tpls = cf.get_templates()
        self.assertTrue(tpls and all('name' in t and 'formula' in t for t in tpls))

    def test_operator_meta_shape(self):
        meta = cf.get_operator_meta()
        for key in ('operators', 'data', 'logic', 'output', 'comment'):
            self.assertIn(key, meta)
        self.assertTrue(any(o['name'] == '均线' for o in meta['operators']))


# ============================================================
# factor_registry 集成
# ============================================================

class TestFactorRegistryIntegration(unittest.TestCase):
    def tearDown(self):
        factor_registry.unregister_custom_factor('cf_测试因子')

    def test_register_and_calc(self):
        formula = '输出 := 收盘价 - 前值(收盘价, 1);'
        errs = factor_registry.sync_custom_factors({
            'cf_测试因子': {'custom': True, 'label': '测试因子', 'formula': formula,
                          'weight': 0.3, 'direction': -1, 'stats': {'mean': 0, 'std': 1}},
        })
        self.assertEqual(errs, {})
        self.assertIn('cf_测试因子', factor_registry.get_custom_factor_names())
        fdef = factor_registry.REGISTRY['cf_测试因子']
        self.assertEqual(fdef.category, 'custom')
        self.assertEqual(fdef.default_direction, -1)
        val = factor_registry.calc_factor_value('cf_测试因子', _ctx([1, 2, 3]))
        self.assertAlmostEqual(val, 1.0)

    def test_sync_removes_missing(self):
        factor_registry.sync_custom_factors({
            'cf_测试因子': {'custom': True, 'label': '测试因子',
                          'formula': '输出 := 收盘价;', 'weight': 1, 'direction': 1},
        })
        self.assertIn('cf_测试因子', factor_registry.REGISTRY)
        factor_registry.sync_custom_factors({})
        self.assertNotIn('cf_测试因子', factor_registry.REGISTRY)

    def test_bad_formula_reported_not_raised(self):
        errs = factor_registry.sync_custom_factors({
            'cf_坏因子': {'custom': True, 'label': '坏因子',
                        'formula': '输出 := 未知函数(收盘价);'},
        })
        self.assertIn('cf_坏因子', errs)
        factor_registry.unregister_custom_factor('cf_坏因子')


# ============================================================
# quant_config 读写（隔离到临时文件）
# ============================================================

class TestConfigRoundtrip(unittest.TestCase):
    FORMULA = '牛熊线 := 均线(收盘价, 5);\n输出 := 收盘价 - 牛熊线;'

    def setUp(self):
        self._orig_path = quant_config.QUANT_MODEL_FILE
        self.tmpdir = tempfile.mkdtemp(prefix='cf_test_')
        quant_config.QUANT_MODEL_FILE = os.path.join(self.tmpdir, 'quant_model.json')
        self._reset()
        quant_config.add_scheme('测试方案', meta={'market': 'stock', 'direction': 'long', 'period': '日K'})
        quant_config.switch_scheme('测试方案')

    def tearDown(self):
        quant_config.QUANT_MODEL_FILE = self._orig_path
        self._reset()
        factor_registry.unregister_custom_factor('cf_测试指标')
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @staticmethod
    def _reset():
        quant_config._MODEL = None
        quant_config._cache = None
        quant_config._CURRENT = None

    def test_save_list_and_registry(self):
        ok, err = quant_config.save_custom_factor('测试指标', self.FORMULA, weight=0.25, direction=-1)
        self.assertTrue(ok, err)
        rows = quant_config.list_custom_factors()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r['label'], '测试指标')
        self.assertEqual(r['formula'], self.FORMULA)
        self.assertAlmostEqual(r['weight'], 0.25)
        self.assertEqual(r['direction'], -1)
        self.assertTrue(r['enabled'])
        # 注册表已同步，且与内置因子同构可求值
        self.assertIn('cf_测试指标', factor_registry.REGISTRY)
        val = factor_registry.calc_factor_value('cf_测试指标', _ctx([1, 2, 3, 4, 5, 6]))
        self.assertAlmostEqual(val, 6 - float(np.mean([2, 3, 4, 5, 6])))

    def test_delete(self):
        quant_config.save_custom_factor('测试指标', self.FORMULA)
        ok, err = quant_config.delete_custom_factor('测试指标')
        self.assertTrue(ok, err)
        self.assertEqual(quant_config.list_custom_factors(), [])
        self.assertNotIn('cf_测试指标', factor_registry.REGISTRY)

    def test_persist_and_reload_resync(self):
        quant_config.save_custom_factor('测试指标', self.FORMULA)
        # 模拟重启：清空内存后从磁盘重载
        self._reset()
        cfg = quant_config.load_config(force_reload=True)
        self.assertIsNotNone(cfg)
        self.assertIn('cf_测试指标', cfg.get('factor_configs', {}))
        self.assertIn('cf_测试指标', factor_registry.REGISTRY)

    def test_bad_formula_rejected(self):
        ok, err = quant_config.save_custom_factor('坏指标', '输出 := 均线(收盘价);')
        self.assertFalse(ok)
        self.assertIn('参数', err)

    def test_empty_name_rejected(self):
        ok, err = quant_config.save_custom_factor('', self.FORMULA)
        self.assertFalse(ok)

    def test_mapper_roundtrip_preserves_custom(self):
        from ui.config_mapper import _config_to_quant_data, _quant_data_to_config
        quant_config.save_custom_factor('测试指标', self.FORMULA, weight=0.2, direction=1)
        cfg = quant_config._safe_cfg()
        qd = _config_to_quant_data(cfg, direction='long')
        row = next((f for f in qd['factors'] if f['name'] == 'cf_测试指标'), None)
        self.assertIsNotNone(row)
        self.assertEqual(row['cat'], 'custom')
        self.assertEqual(row['formula'], self.FORMULA)
        self.assertTrue(row['enabled'])

        out = _quant_data_to_config(qd, direction='long')
        fc = out['factor_configs'].get('cf_测试指标')
        self.assertIsNotNone(fc)
        self.assertTrue(fc.get('custom'))
        self.assertEqual(fc.get('formula'), self.FORMULA)
        self.assertEqual(fc.get('label'), '测试指标')
        self.assertIn('cf_测试指标', out['active_factors'])


# ============================================================
# WebAPI 层（取数打桩，不联网）
# ============================================================

class TestWebApiCustomFactor(unittest.TestCase):
    def setUp(self):
        from ui.web_api import WebAPI
        self._orig_path = quant_config.QUANT_MODEL_FILE
        self.tmpdir = tempfile.mkdtemp(prefix='cf_api_')
        quant_config.QUANT_MODEL_FILE = os.path.join(self.tmpdir, 'quant_model.json')
        TestConfigRoundtrip._reset()
        quant_config.add_scheme('API方案', meta={'market': 'stock', 'direction': 'long', 'period': '日K'})
        quant_config.switch_scheme('API方案')
        self.api = WebAPI()
        self._ctx = _ctx(list(range(1, 41)))
        self._dates = [f'D{i:02d}' for i in range(1, 41)]
        self.api._custom_factor_ctx = lambda code, k_type='日K', days=250: (self._ctx, None, self._dates)

    def tearDown(self):
        quant_config.QUANT_MODEL_FILE = self._orig_path
        TestConfigRoundtrip._reset()
        factor_registry.unregister_custom_factor('cf_API指标')
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_meta(self):
        r = self.api.get_custom_factor_meta()
        self.assertTrue(r['success'])
        self.assertTrue(r['data']['templates'])

    def test_validate(self):
        self.assertTrue(self.api.validate_custom_formula('输出 := 收盘价;')['data']['ok'])
        self.assertFalse(self.api.validate_custom_formula('输出 := 均线(收盘价);')['data']['ok'])

    def test_save_list_delete(self):
        r = self.api.save_custom_factor('API指标', '输出 := 收盘价 - 前值(收盘价, 1);', 0.15, 1, False)
        self.assertTrue(r['success'], r.get('error'))
        lst = self.api.list_custom_factors()
        self.assertTrue(lst['success'])
        self.assertTrue(any(x['label'] == 'API指标' for x in lst['data']))
        d = self.api.delete_custom_factor('API指标')
        self.assertTrue(d['success'], d.get('error'))
        self.assertFalse(any(x['label'] == 'API指标' for x in self.api.list_custom_factors()['data']))

    def test_preview(self):
        r = self.api.preview_custom_formula('输出 := 收盘价 - 前值(收盘价, 1);', 'sh600519', '日K', 40)
        self.assertTrue(r['success'], r.get('error'))
        d = r['data']
        self.assertEqual(d['code'], 'sh600519')
        self.assertEqual(len(d['values']), len(d['closes']))
        self.assertEqual(d['latest'], 1.0)

    def test_try_multi(self):
        r = self.api.try_custom_formula('输出 := 收盘价;', ['a', 'b'], 20)
        self.assertTrue(r['success'], r.get('error'))
        self.assertEqual(len(r['data']), 2)
        self.assertEqual(r['data'][0]['latest'], 40.0)


if __name__ == '__main__':
    unittest.main()