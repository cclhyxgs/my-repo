#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""授权模块自校验规格（无依赖，供 gen_seal / 运行时校验共用）。

列出每个受保护模块中「一旦被 patch 即可绕过授权」的关键函数。运行时校验与
打包前 gen_seal 用同一份列表、同一哈希算法（对 func.__code__ 的「可序列化投影」
串联后 SHA256），保证指纹一致。

为什么不直接 marshal.dumps(func.__code__)：PyArmor 混淆会在 co_consts 注入内建
函数（如 C_ASSERT_ARMORED_INDEX），而内建函数不可 marshal，导致整体序列化抛
ValueError。故 _code_fingerprint 对 __code__ 各成分分别序列化，遇到不可序列化
的成分（混淆态内建函数）用其稳定名（BI:<name>）占位——该 repr 不含内存地址，
构建期与运行期一致，不会误锁；同时仍能捕获对函数体/常量的篡改。明文态所有成分
均可序列化，行为与「整体 marshal」等效。

若攻击者改动本列表（如清空）使校验跳过，则 compute_module_hash 结果与 _seal
记录值不符 → fail-closed，故本文件本身也无法被悄悄 Neutralize。
"""
import hashlib
import marshal

# 每个受保护模块的关键函数（按固定顺序参与哈希，顺序即契约）
# 注意：自校验函数自身(verify_self_integrity / _verify_self_integrity_at_import)
# 不列入，避免「校验器校验自己」的循环依赖与导入期双重执行导致的误判。
CRITICAL_FUNCS = {
    'license.license_manager': [
        'can_use', '_can_use_locked', 'get_license_info', 'activate',
        '_load_public_key', '_verify_code',
        # 2026-08-16 商用加固：此前缺口——改公钥指纹常量/keys.py 不触发 seal（可换密钥自签）、
        # patch consume_use 不触发（额度永不消耗）。以下函数一并入指纹，改动即 fail-closed：
        '_expected_pubkey_hash', 'consume_use',
        # ⚠️ 以下函数 deliberately removed：_save_state / _load_state —— 它们的
        #   co_consts 内嵌了嵌套 code 对象（嵌套函数/元组里的 lambda），该 code 对象
        #   在「源码态 gen_seal.py 编译」vs「PyInstaller 冻结后运行时编译」的
        #   marshal.dumps(code) 结果存在字节级漂移（与 CPython 编译期帧栈分配优化
        #   时机有关，非代码篡改）。移除这两不会削弱反破译防护：
        #   - patch _load_state → 最多伪造 state 结构，但 can_use 还要对 payload 重
        #     验签（_verify_code → Ed25519 公钥校验），不可能凭空造出有效激活码；
        #   - patch _save_state → 只影响写入格式（不影响 can_use 返回值），
        #     真正决定放行的是 can_use / _can_use_locked / enforce（都在指纹里）。
    ],
    'license.license_guard': [
        'enforce',
    ],
}


def _code_fingerprint(co):
    """对单个 code object 做确定性、可序列化的指纹投影。

    分别序列化 co_code / co_names / co_varnames / co_consts 等成分后拼接哈希。
    遇到不可 marshal 的成分（PyArmor 注入的内建函数等）用稳定名 BI:<name> 占位，
    保证构建期与运行期一致、不误锁，同时仍能捕获函数体/常量篡改。
    """
    h = hashlib.sha256()
    # 字节码本体（篡改主信号）
    h.update(marshal.dumps(co.co_code))
    # 名称表 / 变量表
    h.update(marshal.dumps(co.co_names))
    h.update(marshal.dumps(co.co_varnames))
    h.update(marshal.dumps(co.co_freevars))
    h.update(marshal.dumps(co.co_cellvars))
    # 行表 / 异常表（bytes，可序列化）
    h.update(marshal.dumps(co.co_lnotab))
    h.update(marshal.dumps(co.co_exceptiontable))
    # 头信息
    h.update(str(co.co_argcount).encode())
    h.update(str(co.co_kwonlyargcount).encode())
    h.update(str(co.co_stacksize).encode())
    h.update(str(co.co_flags).encode())
    h.update(str(co.co_firstlineno).encode())
    h.update(co.co_name.encode('utf-8', 'replace'))
    # 常量表：可序列化者正常序列化，不可序列化者（混淆内建函数）用稳定名占位
    for c in co.co_consts:
        try:
            h.update(marshal.dumps(c))
        except Exception:
            h.update(('BI:' + getattr(c, '__name__', type(c).__name__)).encode('utf-8', 'replace'))
    return h.hexdigest()


def compute_module_hash(module_obj):
    """对 module_obj 的关键函数 __code__ 串联做 SHA256。

    任一关键函数缺失（被删）或字节码被改，结果即与 _seal 记录值不符 → fail-closed。
    """
    mod_name = module_obj.__name__
    h = hashlib.sha256()
    for name in CRITICAL_FUNCS.get(mod_name, []):
        func = getattr(module_obj, name, None)
        if func is None or not hasattr(func, '__code__'):
            h.update(b'__MISSING__')
            continue
        h.update(_code_fingerprint(func.__code__).encode())
    return h.hexdigest()
