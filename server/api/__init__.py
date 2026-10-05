# -*- coding: utf-8 -*-
"""api 层：HTTP 边界。

规则（§3.4-4「面向 H5 设计，非 1:1 平移」/ 分层约束）：
- 本层只做参数校验与响应封装，不实现业务逻辑，不 import engine（须经 adapters）。
- 内部/非业务端点统一挂 `_` 前缀命名空间（见 settings.INTERNAL_PREFIX）。
"""
