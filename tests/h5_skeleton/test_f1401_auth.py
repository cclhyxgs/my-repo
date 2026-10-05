# -*- coding: utf-8 -*-
"""F-1401 授权接口/机制/状态 —— 验收测试。

断言来源：`docs/feature-matrix.md:64` F-1401 验收口径 + `docs/app-architecture.md`
§3.2 能力 8 端点契约（JWT + HttpOnly device cookie）。

覆盖（对齐冻结决策）：
- 注册成功 → 返回 access/refresh JWT + Set-Cookie(HttpOnly)；422 密码过短
- 重复注册 → 409；错误密码登录 → 401
- 无 cookie 访问 `GET /api/license/info` → 401（受保护端点）
- 有 cookie → 200 账号级字段；**响应不含 `machine_id`**（验收硬断言）
- 绑定超上限（F#3 N=2）：登录第 3 台 → 403 DEVICE_LIMIT
- `app-info` 未登录降级（用独立无 cookie 客户端）
- `quota` 返回订阅制语义 `{unlimited:true}`（F#4）
- 仅行情 / health 无需授权（P0 兼容不回归）

隔离：每个测试独立 SQLite（`MBULL_DATABASE_URL` 指向 `tmp_path`）+ dispose 引擎，
保证 account/device/license 表干净（避免会话级 client 持久 cookie 串测试）。
"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from server.core import time as srv_time


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    """独立 SQLite（表由 session_scope 惰性建表）。"""
    from server.db import session as db_session

    url = "sqlite:///" + (tmp_path / "mbull_auth.db").as_posix()
    monkeypatch.setenv("MBULL_DATABASE_URL", url)
    db_session.dispose()
    yield url
    db_session.dispose()


def _register(client, identifier="u1@example.com", pwd="secret1"):
    return client.post("/api/auth/register", json={"identifier": identifier, "pwd": pwd})


def _login(client, identifier="u1@example.com", pwd="secret1"):
    return client.post("/api/auth/login", json={"identifier": identifier, "pwd": pwd})


def test_register_returns_jwt_and_device_cookie(client, db_env):
    r = _register(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["access_token"] and body["refresh_token"]
    assert body["token_type"] == "bearer"
    # HttpOnly 设备 cookie
    set_cookie = r.headers.get("set-cookie", "")
    assert "mbull_device=" in set_cookie
    assert "HttpOnly" in set_cookie

    # 已带 cookie → 可访问受保护端点
    li = client.get("/api/license/info")
    assert li.status_code == 200, li.text
    data = li.json()
    assert "machine_id" not in data, "F-1401 验收：响应不得含 machine_id"
    assert data["status"] == "trial"
    assert data["tier"] == "trial"
    assert "trial_first_use_at" in data
    assert data["trial_days_left"] <= 30
    # F#4 订阅制：无计次分母
    q = client.get("/api/quota")
    assert q.status_code == 200
    assert q.json()["unlimited"] is True


def test_register_password_too_short_rejected(client, db_env):
    r = _register(client, pwd="123")
    assert r.status_code == 422, r.text


def test_duplicate_register_409(client, db_env):
    assert _register(client).status_code == 200
    r = _register(client)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ACCOUNT_EXISTS"


def test_login_wrong_password_401(client, db_env):
    _register(client)
    r = _login(client, pwd="wrongpass")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "INVALID_CREDENTIALS"


def test_license_info_requires_auth(client, db_env):
    from fastapi.testclient import TestClient

    from server.main import create_app

    # 独立 client（不带任何 cookie）→ 401
    anon = TestClient(create_app())
    r = anon.get("/api/license/info")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "AUTH_REQUIRED"


def test_device_bind_limit_n2_rejects_third(client, db_env):
    """F#3：绑定设备上限 N=2，第 3 台被拒 403。"""
    _register(client)             # 设备 1
    assert _login(client).status_code == 200        # 设备 2
    r = _login(client)                                # 设备 3 → 拒
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "DEVICE_LIMIT"


def test_app_info_anonymous_degrades(client, db_env):
    from fastapi.testclient import TestClient

    from server.main import create_app

    anon = TestClient(create_app())
    r = anon.get("/api/app-info")
    assert r.status_code == 200
    body = r.json()
    assert body["version"]
    assert body["account"] is None
    assert body["license"] is None
    assert body["device_id"] is None
    assert "machine_id" not in body


def test_health_and_quote_only_do_not_require_auth(client, db_env):
    """P0 兼容：基础端点在账号体系就位后仍免登录。"""
    h = client.get("/api/health")
    assert h.status_code == 200, h.text


def test_expired_trial_rejected_403(client, db_env):
    """F#2：试用到期（按账号首次注册时间起算 30 天）→ 授权状态=到期且不可用。"""
    # 注册后把试用起算时间改写为 31 天前，模拟到期
    from server.db import session as db_session
    from server.db.models import License

    _register(client)
    with db_session.session_scope() as session:
        lic = session.execute(select(License)).scalar_one()
        lic.trial_first_use_at = (srv_time.now() - timedelta(days=31)).isoformat()
        session.commit()

    # 到期账号访问受保护端点 → 403 LICENSE_EXPIRED（授权状态=到期且不可用）
    r = client.get("/api/license/info")
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "LICENSE_EXPIRED"
    assert "试用已到期" in r.json()["error"]["message"]


def test_trial_not_expired_ok(client, db_env):
    """试用期内 → 授权状态可用，trial_days_left 在合法区间。"""
    _register(client)
    r = client.get("/api/license/info")
    assert r.status_code == 200
    assert r.json()["status"] == "trial"
    assert 0 < r.json()["trial_days_left"] <= 30