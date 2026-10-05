#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""授权模块单测：公钥验签 fail-closed、机器绑定、伪造码拒绝。

对应 license/license_manager.py。说明：正向激活需要开发者私钥（不在仓库），
故本测试聚焦「负向/防伪造」与机器绑定确定性——足以锁定核心安全不变量。
"""
import os
import sys
import base64
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import license.license_manager as lm


class TestLicenseManager(unittest.TestCase):
    def test_machine_id_deterministic(self):
        a = lm.get_machine_id()
        self.assertTrue(a)
        self.assertEqual(a, lm.get_machine_id())

    def test_state_hmac_bound_to_machine(self):
        k = lm._get_state_hmac_key()
        self.assertTrue(k)
        self.assertEqual(k, lm._get_state_hmac_key())  # 同机确定性

    def test_verify_rejects_garbage(self):
        self.assertIsNone(lm._verify_code("not-a-real-code"))
        self.assertIsNone(lm._verify_code(""))
        self.assertIsNone(lm._verify_code("onlyonepart"))

    def test_verify_rejects_other_key(self):
        # 用随机私钥签名 -> 与仓库内置公钥不匹配 -> 必须拒绝
        priv = Ed25519PrivateKey.generate()
        payload = b"monthly|2027-01-01|some-mid"
        sig = priv.sign(payload)
        fake = base64.b64encode(payload).decode() + "." + base64.b64encode(sig).decode()
        self.assertIsNone(lm._verify_code(fake))

    def test_activate_rejects_forged(self):
        # 把授权文件指向临时目录，避免触碰真实状态；伪造码必须激活失败且不落盘
        tmp = tempfile.mkdtemp()
        with mock.patch.object(lm, '_LICENSE_FILE', os.path.join(tmp, 'license.json')), \
             mock.patch.object(lm, '_MACHINE_ID_FILE', os.path.join(tmp, 'machine_id')), \
             mock.patch.object(lm, '_BACKUP_FILE', os.path.join(tmp, 'license.bak')), \
             mock.patch.object(lm, '_BACKUP_FILE2', os.path.join(tmp, '.lstate')):
            ok, _msg = lm.activate("forged-code")
            self.assertFalse(ok)
            self.assertFalse(os.path.exists(lm._LICENSE_FILE))

    def test_lifetime_license_activated(self):
        """永久授权激活后：get_license_info 返回 active，无到期日、无天数剩余。"""
        tmp = tempfile.mkdtemp()
        mid = lm.get_machine_id()
        fake_payload = {
            'type': 'lifetime',
            'machine_id': mid,
            'issued_at': '2026-01-01',
            'license_id': 'LIC-TEST',
        }
        with mock.patch.object(lm, '_LICENSE_FILE', os.path.join(tmp, 'license.json')), \
             mock.patch.object(lm, '_MACHINE_ID_FILE', os.path.join(tmp, 'machine_id')), \
             mock.patch.object(lm, '_BACKUP_FILE', os.path.join(tmp, 'license.bak')), \
             mock.patch.object(lm, '_BACKUP_FILE2', os.path.join(tmp, '.lstate')), \
             mock.patch.object(lm, '_verify_code', lambda code: fake_payload):
            ok, msg = lm.activate("fake-lifetime-code")
            self.assertTrue(ok)
            self.assertIn("永久", msg)

            info = lm.get_license_info()
            self.assertEqual(info['status'], 'active')
            self.assertEqual(info['type'], 'lifetime')
            self.assertIsNone(info['expiry'])
            self.assertIsNone(info['days_remaining'])
            self.assertIsNone(info['uses_remaining'])

            text = lm.get_status_text()
            self.assertIn("永久", text)

    def test_trial_days_is_30(self):
        """免费试用天数为 30 天（非 7 天）。"""
        self.assertEqual(lm.FREE_TRIAL_DAYS, 30)


if __name__ == '__main__':
    unittest.main()
