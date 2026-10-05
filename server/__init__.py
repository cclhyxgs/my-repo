# -*- coding: utf-8 -*-
"""M-Bull H5 后端服务（骨架期）。

分层：api / core / adapters / worker（详见 docs/app-architecture.md §2.8~§2.13）。
engine/* 一行不改，仅经 adapters/engine_bridge.py 直接 import。
"""

from server.settings import APP_VERSION

__version__ = APP_VERSION
