# -*- coding: utf-8 -*-
"""回归：回测产物「打开」按钮在扩展名无默认关联程序时的回退。

背景（2026-09-19 用户实测）：点击「打开报告JSON」报
    [WinError -2147221003] 找不到应用程序: '...\\bt_strategy_report.json'
即该机器未给 .json 注册默认程序，os.startfile 直接抛 OSError。
本测试锁定 ui.web_api._open_path_smart 的回退行为（不真正启动外部程序）。
"""
import unittest
from unittest import mock

from ui.web_api import _open_path_smart


class TestOpenPathSmart(unittest.TestCase):
    def test_default_app_used_when_startfile_ok(self):
        with mock.patch('os.startfile', create=True) as m_sf, \
             mock.patch('subprocess.Popen') as m_pop:
            ok, how = _open_path_smart(r'C:\tmp\a.json')
        self.assertTrue(ok)
        self.assertEqual(how, 'default')
        m_sf.assert_called_once()
        m_pop.assert_not_called()

    def test_json_falls_back_to_notepad(self):
        with mock.patch('os.startfile', create=True,
                        side_effect=OSError(-2147221003, 'no associated app')), \
             mock.patch('subprocess.Popen') as m_pop:
            ok, how = _open_path_smart(r'C:\tmp\bt_strategy_report.json')
        self.assertTrue(ok)
        self.assertEqual(how, 'notepad')
        self.assertEqual(m_pop.call_args[0][0][0], 'notepad.exe')

    def test_csv_falls_back_to_notepad(self):
        with mock.patch('os.startfile', create=True, side_effect=OSError('x')), \
             mock.patch('subprocess.Popen'):
            ok, how = _open_path_smart(r'C:\tmp\a.csv')
        self.assertEqual(how, 'notepad')

    def test_png_falls_back_to_mspaint(self):
        with mock.patch('os.startfile', create=True, side_effect=OSError('x')), \
             mock.patch('subprocess.Popen') as m_pop:
            ok, how = _open_path_smart(r'C:\tmp\a.png')
        self.assertEqual(how, 'mspaint')
        self.assertEqual(m_pop.call_args[0][0][0], 'mspaint.exe')

    def test_unknown_ext_falls_back_to_explorer_select(self):
        with mock.patch('os.startfile', create=True, side_effect=OSError('x')), \
             mock.patch('subprocess.Popen') as m_pop:
            ok, how = _open_path_smart(r'C:\tmp\a.bin')
        self.assertEqual(how, 'explorer')
        args = m_pop.call_args[0][0]
        self.assertEqual(args[0], 'explorer.exe')
        self.assertTrue(args[1].startswith('/select,'))

    def test_text_fallback_failure_then_explorer(self):
        # notepad 启动失败 → 仍回退到资源管理器定位
        with mock.patch('os.startfile', create=True, side_effect=OSError('x')), \
             mock.patch('subprocess.Popen',
                        side_effect=[FileNotFoundError('notepad'), mock.DEFAULT]):
            ok, how = _open_path_smart(r'C:\tmp\a.json')
        self.assertTrue(ok)
        self.assertEqual(how, 'explorer')


if __name__ == '__main__':
    unittest.main()
