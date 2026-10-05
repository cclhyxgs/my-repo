#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Hardened license enforcement gate.

在 license_manager 之上增加：
  - 自校验(seal)：导入时校验 license_manager + license_guard 自身字节码哈希，
    与打包前生成的 license/_seal.py 指纹比对；不一致即判定被篡改 → fail-closed。
  - enforce()：引擎层授权门禁，拒绝时抛出 LicenseDenied，使「即便 UI 的
    if not allowed 被 patch，引擎也拒绝计算」。

补丁攻击面（仅 PyInstaller 冻结态需防护；Nuitka 已原生编译、dev 为开发环境）：
  - 仅 patch can_use → license_manager 导入即因字节码哈希变化 fail-closed（整 app 锁死）。
  - 仅 patch web_api 的 if not allowed → 引擎 enforce() 仍拒绝（enforce 在受 seal
    保护的本模块内，patch 它同样触发哈希失配 → 锁死）。
  故绕过需同时改 manager/guard/_seal 三处且保持哈希一致，门槛显著提高。

PyArmor 混淆态的特殊处理（重要）：
  PyArmor 混淆后的 code object 在「被 __code__ 深度内省」时（本环境 Python
  3.13 + PyArmor 9 trial 实测）会对多数关键函数触发段错误(SIGSEGV)，导致冻结
  EXE 启动即崩。而混淆代码「正常执行」不受影响。故混淆构建下，verify_self_integrity
  自动跳过内省（_is_obfuscated 检测 pyarmor_runtime 已在 sys.modules），以「混淆本身
  作为屏障」替代 seal 的主动篡改检测。安全含义：混淆构建失去 fail-closed 的主动
  篡改自检，仅依赖混淆带来的逆向成本提升——这是该环境 PyArmor 与 seal 不兼容下的
  权衡。明文(PyInstaller 未混淆)构建仍走完整 seal 自检。
"""
import sys

from license.license_manager import can_use, _dev_mode_enabled


def _is_obfuscated():
    """检测当前 license 包是否已被 PyArmor 混淆（运行期）。

    PyArmor 混淆模块会 import pyarmor_runtime_<licno> 包，故该包一旦进入
    sys.modules 即说明处于混淆态。命名带试用许可号后缀，故用 startswith 匹配。
    """
    return any(k.startswith('pyarmor_runtime') for k in sys.modules)


class LicenseDenied(Exception):
    """授权受限时由 enforce() 抛出。str(e) 形如 '授权受限: <reason>'。"""

    def __init__(self, reason=''):
        self.reason = reason
        super().__init__('授权受限: ' + reason if reason else '授权受限')


def _in_pyinstaller():
    return bool(getattr(sys, '_MEIPASS', None)) and getattr(sys, 'frozen', False)


def verify_self_integrity():
    """Fail-closed：任意受保护模块关键函数字节码被篡改即抛 RuntimeError。

    仅 PyInstaller 冻结态生效（该形态 .pyc 可被反编译 patch）。
    Nuitka(原生编译)/dev 跳过——前者已被原生编译保护，后者为开发环境。

    通过 sys.modules 取已导入的模块对象（不重新 import，避免导入期
    二次执行导致模块对象不一致而误判）。需在模块完全导入后调用
    （应用启动期 / enforce 内），切勿在模块自身导入过程中调用。
    """
    if not _in_pyinstaller():
        return
    # 混淆态：PyArmor 混淆后的 code object 被 __code__ 深度内省会段错误（见模块
    # 顶部说明），此处跳过内省，改以混淆本身为屏障，避免冻结 EXE 启动即崩。
    if _is_obfuscated():
        return
    try:
        from license import _seal
        from license._seal_spec import compute_module_hash
    except Exception:
        # seal / 规格缺失即视为篡改（打包产物不完整）
        raise RuntimeError('授权自校验模块缺失，疑似被篡改')
    import sys as _sys
    lm = _sys.modules.get('license.license_manager')
    lg = _sys.modules.get('license.license_guard')
    for mod_obj in (lm, lg):
        if mod_obj is None:
            raise RuntimeError('授权模块缺失，疑似被篡改')
        mod_name = mod_obj.__name__
        expected = _seal.SEAL.get(mod_name)
        if not expected:
            raise RuntimeError('授权指纹缺失: ' + mod_name)
        actual = compute_module_hash(mod_obj)
        if actual != expected:
            raise RuntimeError('授权模块完整性校验失败：疑似被逆向篡改 (' + mod_name + ')  expected=' + expected[:16] + '... actual=' + actual[:16] + '...')


def enforce(feature='query'):
    """引擎层授权门禁。未授权抛 LicenseDenied。开发模式豁免。不消耗额度。"""
    # 开发模式（源码 + 显式开发标记）免授权，与 license_manager 一致
    if not getattr(sys, 'frozen', False) and _dev_mode_enabled():
        return True
    verify_self_integrity()
    allowed, reason = can_use(feature)
    if not allowed:
        raise LicenseDenied(reason)
    return True


def run_startup_check():
    """应用启动期一次性自校验（模块均已完全导入后调用，避免导入期误判）。

    返回 True 表示通过；冻结态检测到篡改则抛 RuntimeError（由调用方 fail-closed）。
    dev / Nuitka 原生编译态直接返回 True（不校验）。
    """
    if not _in_pyinstaller():
        return True
    verify_self_integrity()
    return True
