# -*- coding: utf-8 -*-
"""持久层（能力 5：配置与方案持久化，F-601 / F-602）。

分层位置：`server/db/` 是与 `adapters/` 并列的基础设施层，依赖方向
`api → db → core/settings`；**不 import engine**（T8 规定 engine 只允许在
`adapters/engine_bridge.py` 被 import）。

跨库策略（2026-09-13 定版）：
- 同一份 SQLAlchemy 仓储代码，**生产 PG（postgresql+psycopg）/ 测试 SQLite**；
- JSON 体用 `JSON().with_variant(JSONB, "postgresql")` → PG 落 JSONB、SQLite 落 TEXT；
- 乐观锁用 `UPDATE ... WHERE version=:expected` 的受影响行数判冲突（跨库通用）；
- PG 专属 `SELECT ... FOR UPDATE` 走方言分支（本机无 PG，该分支未在本机验证）。
"""
