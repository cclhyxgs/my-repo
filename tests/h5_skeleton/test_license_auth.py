# -*- coding: utf-8 -*-
"""能力 8 最小子集：授权强制（F-1404）验收测试。

依据 migration-plan.md 能力 8：业务端点（analyze / diagnosis 提交 / scan 提交 /
backtest 提交）走账号维度授权强制 `require_license`：
  无会话 → 401 AUTH_REQUIRED；登录但试用/订阅到期 → 403 LICENSE_EXPIRED；
  试用/订阅期内 → 放行（不按次扣减）。

F-1404 已把强制从 3 处 adapter 的设备维度 `authorize_query` 迁移到 FastAPI
端点依赖。`require_license` 在路由注册期被 `Depends` 绑定，真实逻辑收敛到模块
全局 `_license_gate`（conftest `_allow_license` 默认替换为放行）。本文件用
`monkeypatch.setattr(s_auth, "_license_gate", _real_gate)` 恢复真实判定，
走真实注册账号 + 真实 DB license，**仅 engine 分析链打桩**以保持离线确定。

仅行情（F-305 `/api/quote-only`）不挂依赖 → 天然免校验（test_quote_only_bypasses_auth）。
"""
import pytest
from datetime import timedelta

from server.core import auth as s_auth
from server.core import time as srv_time


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    """独立 SQLite（表由 session_scope 惰性建表），隔离账号/设备/license 数据。"""
    from server.db import session as db_session

    url = "sqlite:///" + (tmp_path / "mbull_license.db").as_posix()
    monkeypatch.setenv("MBULL_DATABASE_URL", url)
    db_session.dispose()
    yield url
    db_session.dispose()


def _register(client, identifier="lic@example.com", pwd="secret1"):
    return client.post("/api/auth/register", json={"identifier": identifier, "pwd": pwd})


def _rewind_trial_31d(client):
    """把当前账号试用起算时间改写为 31 天前（模拟试用到期，F#2 账号维度 30 天）。"""
    from sqlalchemy import select

    from server.db import session as db_session
    from server.db.models import License

    with db_session.session_scope() as session:
        lic = session.execute(select(License)).scalar_one()
        lic.trial_first_use_at = (srv_time.now() - timedelta(days=31)).isoformat()
        session.commit()


def _real_gate(request):
    """恢复 require_license 的真实判定（物化会话 → 401/到期 403）。"""
    ctx = s_auth._materialize(request)
    if ctx is None:
        raise s_auth.ApiError("AUTH_REQUIRED", "未登录或会话已失效，请先登录。", status_code=401)
    s_auth.verify_valid_license(ctx)
    return ctx


def _mock_analyze_hits(client, monkeypatch, hits):
    """替换 engine 分析链为打桩，避免真实取数。"""
    from server.adapters import engine_bridge

    monkeypatch.setattr(
        engine_bridge,
        "analyze_stock",
        lambda code, k_type="日K", scheme_name=None: hits.append(code) or {
            "reportData": {"status": "观望", "factorScore": -7.8, "env": "极端恐慌"},
            "report_text": "测试报告",
            "used_scheme_name": "1",
            "used_scheme_period": "日K",
        },
    )


def test_license_expired_returns_403(client, db_env, monkeypatch):
    """登录但试用到期 → 403 LICENSE_EXPIRED，且不进入分析链。"""
    _register(client)
    _rewind_trial_31d(client)
    monkeypatch.setattr(s_auth, "_license_gate", _real_gate)

    hits = []
    _mock_analyze_hits(client, monkeypatch, hits)

    r = client.post("/api/analyze", json={"code": "sh600519", "k_type": "日K"})

    assert r.status_code == 403, r.text
    body = r.json()
    assert body["error"]["code"] == "LICENSE_EXPIRED"
    assert "试用已到期" in body["error"]["message"]
    assert hits == [], "授权被拒后仍进入分析链"


def test_license_expired_also_blocks_scan(client, db_env, monkeypatch):
    """扫描提交同样走 `require_license` 强制（抽查另一业务端点）。"""
    _register(client)
    _rewind_trial_31d(client)
    monkeypatch.setattr(s_auth, "_license_gate", _real_gate)

    r = client.post("/api/scan", json={"market": "stock"})
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "LICENSE_EXPIRED"


def test_no_session_returns_401(client, db_env, monkeypatch):
    """无会话访问业务端点 → 401 AUTH_REQUIRED（真实物化）。"""
    from fastapi.testclient import TestClient

    from server.main import create_app

    monkeypatch.setattr(s_auth, "_license_gate", _real_gate)
    anon = TestClient(create_app())  # 独立 client，无 cookie
    r = anon.post("/api/analyze", json={"code": "sh600519", "k_type": "日K"})
    assert r.status_code == 401, r.text
    assert r.json()["error"]["code"] == "AUTH_REQUIRED"


def test_license_ok_proceeds_to_analysis(client, db_env, monkeypatch):
    """登录且试用期内 → 放行，进入分析链（真实物化 + 真实 license 校验）。"""
    _register(client)
    monkeypatch.setattr(s_auth, "_license_gate", _real_gate)

    hits = []
    _mock_analyze_hits(client, monkeypatch, hits)

    r = client.post("/api/analyze", json={"code": "sh600519", "k_type": "日K"})
    assert r.status_code == 200, r.text
    assert r.json()["reportData"]["factorScore"] == -7.8
    assert r.json()["entry_tier"] == "观望"
    assert hits, "授权放行后应进入分析链"


def test_quote_only_bypasses_auth(client, monkeypatch):
    """仅行情（F-305）免授权校验：不触发 `_license_gate`。"""
    from server.adapters.market import quote_only as qo

    calls = []
    monkeypatch.setattr(s_auth, "_license_gate", lambda req: calls.append(1) or {})

    monkeypatch.setattr(
        qo.kline_adapter,
        "fetch_kline",
        lambda code, period, days: {
            "code": code, "period": period, "days": days, "count": 1,
            "bars": [{"t": "2026-09-11", "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 100.0}],
        },
    )
    monkeypatch.setattr(
        qo.quote_adapter,
        "fetch_quote",
        lambda code: {
            "price": 1.5, "change_pct": 0.0, "volume": 100.0, "volume_unit": "手",
            "amount": 1000.0, "time": "2026-09-11 15:00:00", "name": "测试", "source": "mock",
        },
    )

    r = client.get("/api/quote-only", params={"code": "sh600519", "period": "日K", "days": 1})
    assert r.status_code == 200
    assert r.json()["report"] is None
    assert calls == [], "仅行情路径不应触发授权强制"