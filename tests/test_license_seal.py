#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""授权模块自校验（seal）测试：验证「完整代码不误杀、patch 关键函数→fail-closed」。

真实场景模拟：PyInstaller EXE 启动时 sys._MEIPASS 已存在（在导入 license 模块之前），
故本测试在导入 license 模块「之前」伪造 _MEIPASS。不触碰任何授权文件/网络。

⚠️ 运行需先 `python license/gen_seal.py` 生成 license/_seal.py（基于当前源码字节码）。

⚠️ 隔离性：假 engine.config / _MEIPASS / frozen 只在「上下文内」生效，退出即恢复真实
    engine.config，避免污染共用解释器状态——否则紧随其后的测试模块（如 backtest_strategy
    → engine.stock_pool → `from engine.config import WATCHLIST_FILE`）会因读到假 config
    （缺 WATCHLIST_FILE/CONFIG_DIR 等）而 ImportError。
"""
import sys
import os
import types
import contextlib

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

# 捕获真实 engine.config（若尚未导入则不强制预导入，留空由下面的恢复逻辑处理）
_REAL_CONFIG = sys.modules.get('engine.config')


@contextlib.contextmanager
def _seal_context():
    """按需注入假 engine.config / _MEIPASS / frozen，退出时恢复现场。"""
    injected = False
    if 'engine.config' not in sys.modules:
        # 备份一个空的占位以便恢复时剔除
        prev = None
    else:
        prev = sys.modules['engine.config']
    fc = types.ModuleType('engine.config')
    fc.get_app_dir = lambda: 'C:/tmp/mbull_fake_appdir'
    sys.modules['engine.config'] = fc
    injected = True
    had_meipass = hasattr(sys, '_MEIPASS')
    had_frozen = hasattr(sys, 'frozen')
    old_meipass = getattr(sys, '_MEIPASS', None)
    old_frozen = getattr(sys, 'frozen', None)
    sys._MEIPASS = 'fake_mei'
    sys.frozen = True
    try:
        yield
    finally:
        # 恢复真实 engine.config（原值若存在则还原，否则剔除假模块）
        if prev is not None:
            sys.modules['engine.config'] = prev
        else:
            sys.modules.pop('engine.config', None)
        # 恢复 _MEIPASS / frozen
        if had_meipass:
            sys._MEIPASS = old_meipass
        else:
            sys.__dict__.pop('_MEIPASS', None)
        if had_frozen:
            sys.frozen = old_frozen
        else:
            sys.__dict__.pop('frozen', None)


def test_intact_no_false_lock():
    with _seal_context():
        import license.license_manager as lm
        import license.license_guard as lg
        # 完整代码：导入应成功，且显式校验不抛异常（不误杀正版）
        lg.run_startup_check()
        lg.verify_self_integrity()
        print('[ok] 完整代码：导入成功且自校验通过（未误杀）')
        return lm, lg


def test_tamper_can_use_locks(lm, lg):
    with _seal_context():
        orig = lm.can_use
        lm.can_use = lambda feature='query': (True, 'patched')  # 模拟 patch can_use
        try:
            lg.verify_self_integrity()
            print('[FAIL] patch can_use 未触发锁死')
            raise SystemExit(1)
        except RuntimeError as e:
            print('[ok] patch can_use 触发 fail-closed:', str(e)[:48])
        finally:
            lm.can_use = orig


def test_tamper_enforce_locks(lm, lg):
    with _seal_context():
        orig = lg.enforce
        lg.enforce = lambda feature='query': True  # 模拟 patch enforce 放行
        try:
            lg.verify_self_integrity()
            print('[FAIL] patch enforce 未触发锁死')
            raise SystemExit(1)
        except RuntimeError as e:
            print('[ok] patch enforce 触发 fail-closed:', str(e)[:48])
        finally:
            lg.enforce = orig


def test_tamper_seal_empty_locks(lm, lg):
    with _seal_context():
        import license._seal as seal_mod
        orig = seal_mod.SEAL
        seal_mod.SEAL = {}  # 模拟 _seal 被清空/篡改
        try:
            lg.verify_self_integrity()
            print('[FAIL] _seal 空指纹未触发锁死')
            raise SystemExit(1)
        except RuntimeError as e:
            print('[ok] _seal 空指纹触发 fail-closed:', str(e)[:48])
        finally:
            seal_mod.SEAL = orig


if __name__ == '__main__':
    lm, lg = test_intact_no_false_lock()
    test_tamper_can_use_locks(lm, lg)
    test_tamper_enforce_locks(lm, lg)
    test_tamper_seal_empty_locks(lm, lg)
    print('\n[ALL PASS] license seal 自校验按预期工作（完整不误杀 / 篡改即锁死）')