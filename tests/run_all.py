#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""测试运行入口（unittest 发现，无需 pytest 依赖）。

用法：
  python tests/run_all.py            # 运行全部
  python -m pytest tests/ -q        # 若已装 pytest 也可
退出码 0=全部通过，1=有失败。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


def main():
    loader = unittest.TestLoader()
    suite = loader.discover(HERE, pattern='test_*.py')
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main())
