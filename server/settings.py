# -*- coding: utf-8 -*-
"""服务端配置 —— 路径与开关的唯一来源（骨架期）。

依据：
- app-architecture.md §2.12（部署构成）、§2.13（三条全局硬约束）
- 补充约束 1：engine/* 一行不改，QUANT_SYSTEM_DIR 是唯一路径注入口
"""

import os
from pathlib import Path

APP_NAME = "M-Bull"
APP_VERSION = "0.1.0"

# §2.13-2 硬约束：全局固定 Asia/Shanghai（F-203 夜盘归属 / F-302 合约过期判定依赖）
TIMEZONE = "Asia/Shanghai"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# §2.13-1 锁清单：单一真相源就是该文件本身，避免代码内再抄一份导致漂移
LOCK_FILE = PROJECT_ROOT / "requirements-server.txt"

HOST = os.environ.get("MBULL_HOST", "0.0.0.0")
PORT = int(os.environ.get("MBULL_PORT", "8000"))

API_PREFIX = "/api"
# B3：下划线前缀 = 内部/非业务命名空间，与 C3 §3.2 业务端点严格隔离
INTERNAL_PREFIX = f"{API_PREFIX}/_probe"
OPENAPI_URL = f"{API_PREFIX}/openapi.json"

IS_PRODUCTION = os.environ.get("MBULL_ENV", "development").strip().lower() in ("prod", "production")
# 生产由环境变量关闭探针；dev 暴露供 T7 端到端验收
INTERNAL_PROBE_ENABLED = os.environ.get(
    "MBULL_ENABLE_INTERNAL_PROBE",
    "0" if IS_PRODUCTION else "1",
).strip().lower() not in ("0", "false", "no", "off")

QUANT_SYSTEM_DIR_ENV = "QUANT_SYSTEM_DIR"
# 开发态兜底目录：避免服务端读写桌面版真实数据目录 %LOCALAPPDATA%/M-Bull
DEV_QUANT_SYSTEM_DIR = PROJECT_ROOT / ".mbull-h5-dev"

# ── H5 接入桌面壳（分支 A：FastAPI 同源托管 SPA）──
# 开关默认关：纯 API 模式由 nginx/CDN 托管前端，FastAPI 只提供 /api。
# 开启（MBULL_SERVE_SPA=1）时 FastAPI 同时托管 web/dist 静态资源，使桌面壳
# pywebview 可直接加载 http://localhost:8000（无需独立静态服务器）。
SERVE_SPA = os.environ.get("MBULL_SERVE_SPA", "0").strip().lower() in ("1", "true", "yes", "on")
# Vite 构建产物目录（server/main.py 同源托管）
SPA_DIST = PROJECT_ROOT / "web" / "dist"

# ── 能力 5：配置与方案持久化（F-601 / F-602）──
# 生产：postgresql+psycopg://<user>:<pwd>@<host>:5432/mbull（compose 的 postgres 服务，profiles:[p1]）
# 测试/开发：置空 → SQLite 文件兜底（sqlite3 内置）。同一份 SQLAlchemy 仓储代码，
#            业务逻辑（同名校验 / 版本号 CAS / 事务回滚）在 SQLite 上可真实断言。
DATABASE_URL_ENV = "MBULL_DATABASE_URL"
DEV_DATABASE_URL = "sqlite:///" + (DEV_QUANT_SYSTEM_DIR / "mbull.db").as_posix()

# ── 能力 8：授权（F-1401 / P2a）──
# JWT 签名密钥 / 设备 cookie 键 / 有效期（秒）。生产必须用 env 覆盖 SECRET。
AUTH_SECRET_ENV = "MBULL_AUTH_SECRET"
DEV_AUTH_SECRET = "mbull-h5-dev-secret-change-me"
AUTH_ACCESS_TTL = int(os.environ.get("MBULL_AUTH_ACCESS_TTL", "3600"))        # 1h
AUTH_REFRESH_TTL = int(os.environ.get("MBULL_AUTH_REFRESH_TTL", "2592000"))   # 30d
# 设备标识 HttpOnly cookie 名（明文 device token，服务端只存其 SHA-256）
DEVICE_COOKIE = "mbull_device"


def resolve_auth_secret() -> str:
    """JWT 密钥唯一解析入口：显式 env 优先，否则开发态固定串兜底（勿用于生产）。"""
    raw = os.environ.get(AUTH_SECRET_ENV)
    return raw.strip() if raw and raw.strip() else DEV_AUTH_SECRET


def resolve_database_url() -> str:
    """DB DSN 唯一解析入口：显式 env 优先，否则开发态 SQLite 兜底。"""
    raw = os.environ.get(DATABASE_URL_ENV)
    if raw and raw.strip():
        return raw.strip()
    return DEV_DATABASE_URL


def resolve_quant_system_dir() -> Path:
    """engine 唯一路径注入口（补充约束 1）。

    engine/config.py:81 起支持 QUANT_SYSTEM_DIR 覆盖 APP_DIR；容器内由 compose 显式
    指向挂卷（/data/m-bull）。服务端**不修改 engine 任何代码**，只注入该环境变量。
    """
    raw = os.environ.get(QUANT_SYSTEM_DIR_ENV)
    return Path(raw) if raw else DEV_QUANT_SYSTEM_DIR
