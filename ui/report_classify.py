#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""报告分类（薄委托，2026-09-13 上移至 engine/report_classify.py，F-402）。

原逻辑已上移到 engine（单一真相源，桌面与 H5 共用）。本模块保留 `_classify_for_report`
名字以兼容既有 `from ui.report_classify import _classify_for_report` 调用点。
"""

from engine.report_classify import _classify_for_report

__all__ = ["_classify_for_report"]
