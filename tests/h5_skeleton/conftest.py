# -*- coding: utf-8 -*-
"""H5 后端骨架验收测试 —— 前置环境。

两条关键前置（缺任一则断言无意义）：
1. 项目根进 sys.path，保证 server.* 与 engine.* 都可按 §2.8「直接 import」方式导入。
2. **导入 engine 前先把 QUANT_SYSTEM_DIR 指到隔离目录**。
   原因：engine/config.py 在 import 期即执行 _migrate_legacy_app_dir()，
   该函数在「目标目录为空 + %LOCALAPPDATA%\\QuantSystem 存在」时会
   copytree 后 shutil.rmtree 掉旧目录（见 engine/config.py:137-186）。
   指到隔离目录 + engine_bridge 预写哨兵文件可确保该分支永不触发，
   并避免测试读写用户真实数据目录 %LOCALAPPDATA%\\M-Bull。
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QUANT_SYSTEM_DIR", str(Path(tempfile.gettempdir()) / "mbull_h5_skeleton_dev"))
os.environ.setdefault("MBULL_ENABLE_INTERNAL_PROBE", "1")

DEPLOY_DIR = ROOT / "deploy"
SERVER_DIR = ROOT / "server"
LOCK_FILE = ROOT / "requirements-server.txt"


@pytest.fixture(scope="session")
def root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def deploy_dir() -> Path:
    return DEPLOY_DIR


@pytest.fixture(scope="session")
def server_dir() -> Path:
    return SERVER_DIR


@pytest.fixture(scope="session")
def lock_file() -> Path:
    return LOCK_FILE


@pytest.fixture(scope="session")
def app():
    from server.main import create_app

    return create_app()


@pytest.fixture(scope="session")
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _allow_license(monkeypatch):
    """全局放行授权强制依赖（F-1404 已从 adapter 迁移到账号维度端点依赖）。

    F-1404 后业务端点（analyze / diagnosis / scan / backtest）由
    `server.core.auth.require_license` 强制：无会话 → 401、试用到期 → 403。
    auth 端点（/auth/*、/license/info、/quota 等）用独立的 `_materialize` /
    `verify_valid_license` 路径，不受本 seam 影响（test_f1401_auth 走真实流程）。

    本 seam 替换 `_license_gate`（require_license 内部运行时查表的开关函数），
    默认放行，避免普通业务测试逐个注册账号；401/403 语义由
    `test_license_auth.py` 单独替换 assert。
    """
    from server.core import auth as s_auth

    monkeypatch.setattr(s_auth, "_license_gate", lambda request: {})

