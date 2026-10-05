# -*- coding: utf-8 -*-
"""回归：回测产物写盘失败**不再静默**，且「旧文件」不会被当成本次结果展示。

背景（2026-09-19 现场）：用户反馈「收益曲线和实际的结果对不上」——
22:56 那次回测的日志里三条 warn：
    [warn] 净值曲线导出失败: [Errno 13] Permission denied: ...bt_strategy_equity.csv
    [warn] 持仓明细CSV导出失败: [Errno 13] ...
    [warn] 交易流水CSV导出失败: [Errno 13] ...
三个 CSV 因**正被 Excel/WPS 打开占用**而写失败（json 全部成功），旧文件残留，
UI 于是画出 21:11 的旧净值曲线（+46%）配新报告的指标卡（+3.21%）。

修法：
  1. `engine.backtest_strategy._safe_write_csv` —— 原子写（.tmp → os.replace）；
     失败**不吞**，收集进 `report['export_errors']`，并打印含操作指引的告警。
  2. `ui.web_api._stale_outputs` —— 按「产物 mtime < 回测启动时间」判定旧文件，
     经 `get_backtest_result` 回传 `stale_files`，前端顶部显红条告警。
  3. `WebAPI._bt_check_outputs_writable` —— 回测**开跑前**预检，被占用提前提示。
"""
import os
import stat
import tempfile
import unittest
from unittest import mock

import pandas as pd

from engine.backtest_strategy import _safe_write_csv
from ui.web_api import WebAPI, _stale_outputs


class TestSafeWriteCsv(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='bt_lock_')
        self.path = os.path.join(self.tmp, 'bt_strategy_equity.csv')
        self.df = pd.DataFrame([{'日期': '2026-01-05', '净值': 1000000.0}])

    def test_success_writes_file_and_no_tmp_left(self):
        errors = []
        ok, err = _safe_write_csv(self.df, self.path, '净值曲线', errors)
        self.assertTrue(ok)
        self.assertEqual(err, '')
        self.assertEqual(errors, [])
        self.assertTrue(os.path.exists(self.path))
        self.assertFalse(os.path.exists(self.path + '.tmp'))   # 临时文件已 replace
        with open(self.path, encoding='utf-8-sig') as f:
            self.assertIn('净值', f.read())

    def test_permission_denied_is_collected_not_swallowed(self):
        """核心回归：写失败必须进 errors 且标记 locked（原来是只 print warn 就放过）。"""
        errors = []
        with mock.patch.object(pd.DataFrame, 'to_csv',
                               side_effect=PermissionError(13, 'Permission denied')):
            ok, err = _safe_write_csv(self.df, self.path, '净值曲线', errors)
        self.assertFalse(ok)
        self.assertIn('PermissionError', err)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]['file'], 'bt_strategy_equity.csv')
        self.assertEqual(errors[0]['what'], '净值曲线')
        self.assertTrue(errors[0]['locked'])

    def test_permission_denied_keeps_old_file_intact(self):
        """写失败时绝不能把旧文件清空/截断 —— 也不留半截 .tmp。"""
        with open(self.path, 'w', encoding='utf-8-sig') as f:
            f.write('旧内容\n')
        with mock.patch.object(pd.DataFrame, 'to_csv',
                               side_effect=PermissionError(13, 'Permission denied')):
            _safe_write_csv(self.df, self.path, '净值曲线', [])
        with open(self.path, encoding='utf-8-sig') as f:
            self.assertEqual(f.read(), '旧内容\n')
        self.assertFalse(os.path.exists(self.path + '.tmp'))

    def test_other_exception_not_marked_locked(self):
        errors = []
        with mock.patch.object(pd.DataFrame, 'to_csv',
                               side_effect=ValueError('boom')):
            ok, _ = _safe_write_csv(self.df, self.path, '净值曲线', errors)
        self.assertFalse(ok)
        self.assertEqual(len(errors), 1)
        self.assertFalse(errors[0]['locked'])

    def test_errors_optional(self):
        """不传 errors 也不能抛（某些调用点只关心流程不中断）。"""
        with mock.patch.object(pd.DataFrame, 'to_csv',
                               side_effect=PermissionError(13, 'denied')):
            ok, err = _safe_write_csv(self.df, self.path, '净值曲线', None)
        self.assertFalse(ok)
        self.assertIn('PermissionError', err)


class TestStaleOutputs(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='bt_stale_')
        self.old = os.path.join(self.tmp, 'old.csv')
        self.new = os.path.join(self.tmp, 'new.csv')
        for p in (self.old, self.new):
            with open(p, 'w', encoding='utf-8') as f:
                f.write('x')
        # old.csv 设为 1000 秒前，new.csv 保持"现在"
        past = os.stat(self.old).st_mtime - 1000
        os.utime(self.old, (past, past))

    def _iso_start(self, offset_sec=-500):
        """模拟回测「启动时刻」：取 500 秒前。

        真实语义是「产物写于启动之后」⇒ mtime > start_time；所以这里 old.csv（-1000s）
        会被判为旧文件，new.csv（刚写）不会。用「当下」当 start_time 会把刚写的文件
        也误判为旧（测试设计陷阱，已修正）。
        """
        from datetime import datetime, timedelta
        return (datetime.now() + timedelta(seconds=offset_sec)).isoformat()

    def test_old_file_is_flagged_new_is_not(self):
        stale = _stale_outputs({'equity': self.old, 'trades': self.new}, self._iso_start())
        self.assertEqual(stale, ['equity'])

    def test_bad_start_time_means_no_flag(self):
        self.assertEqual(_stale_outputs({'equity': self.old}, 'not-a-date'), [])
        self.assertEqual(_stale_outputs({'equity': self.old}, None), [])

    def test_missing_path_skipped(self):
        stale = _stale_outputs({'equity': os.path.join(self.tmp, 'nope.csv')},
                               self._iso_start())
        self.assertEqual(stale, [])

    def test_empty_map(self):
        self.assertEqual(_stale_outputs({}, self._iso_start()), [])
        self.assertEqual(_stale_outputs(None, self._iso_start()), [])


class TestWritablePrecheck(unittest.TestCase):
    """回测开跑前的产物可写性预检。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='bt_pre_')
        self.reports = os.path.join(self.tmp, 'reports')
        os.makedirs(self.reports, exist_ok=True)
        self.api = object.__new__(WebAPI)     # 只用到 get_app_dir，绕过重量级 __init__

    def _mk(self, *names):
        for n in names:
            with open(os.path.join(self.reports, n), 'w', encoding='utf-8') as f:
                f.write('x')

    def test_all_writable_returns_empty(self):
        self._mk('bt_strategy_equity.csv', 'bt_strategy_report.json')
        with mock.patch('ui.web_api.get_app_dir', return_value=self.tmp):
            locked = self.api._bt_check_outputs_writable('bt_strategy', 'strategy', 30)
        self.assertEqual(locked, [])

    def test_locked_file_is_reported(self):
        self._mk('bt_strategy_equity.csv', 'bt_strategy_trades.csv')
        real_open = open
        target = os.path.join(self.reports, 'bt_strategy_equity.csv')

        def fake_open(path, *a, **kw):
            # 只让 equity.csv 表现得像被 Excel 独占
            if str(path).endswith('bt_strategy_equity.csv'):
                raise PermissionError(13, 'Permission denied', str(path))
            return real_open(path, *a, **kw)

        with mock.patch('ui.web_api.get_app_dir', return_value=self.tmp), \
             mock.patch('builtins.open', side_effect=fake_open):
            locked = self.api._bt_check_outputs_writable('bt_strategy', 'strategy', 30)
        self.assertEqual(locked, ['bt_strategy_equity.csv'])
        self.assertTrue(os.path.exists(target))

    def test_missing_files_not_reported(self):
        """文件还不存在时不算被占用（首次回测）。"""
        self.api._bt_check_outputs_writable  # noqa
        with mock.patch('ui.web_api.get_app_dir', return_value=self.tmp):
            self.assertEqual(self.api._bt_check_outputs_writable('bt_new', 'strategy', 30), [])

    def test_mode_specific_names(self):
        self._mk('bt_factoric.csv', 'bt_factoric.png')
        with mock.patch('ui.web_api.get_app_dir', return_value=self.tmp):
            # 这两个文件可写 → 空；仅验证不因缺 report.json 等报错
            self.assertEqual(self.api._bt_check_outputs_writable('bt_factoric', 'factoric'), [])


if __name__ == '__main__':
    unittest.main()
