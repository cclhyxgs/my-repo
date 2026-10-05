# -*- coding: utf-8 -*-
"""F-704 应用因子 IC 结果 —— 验收测试。

断言来源：`docs/feature-matrix.md` F-704 行「验收标准」列：
  - 提交因子IC结果后 `factor_profiles[fp].factor_configs[f]` **实际更新**
  - 返回 `updated` = 真实写入因子数
  - 并行写冲突返回 409
后端直接更新 `factor_profiles[fp].factor_configs[f]`，走 DB 事务 + 版本号 CAS（乐观锁）。
"""

import copy

import pytest

from server.db import repo
from server.db.session import session_scope


@pytest.fixture(autouse=True)
def db_env(tmp_path, monkeypatch):
    """独立 SQLite + 重置引擎，隔离 profile 数据。"""
    from server.db import session as db_session

    url = "sqlite:///" + (tmp_path / "mbull_ic.db").as_posix()
    monkeypatch.setenv("MBULL_DATABASE_URL", url)
    db_session.dispose()
    yield url
    db_session.dispose()


def _mk_profile(name, body):
    with session_scope() as s:
        if repo.get_profile(s, name) is None:
            repo.create_profile(s, name, copy.deepcopy(body))
            s.commit()
    return name


def _get_profile(name):
    with session_scope() as s:
        return repo.get_profile(s, name)


def _apply(client, profile, version, factors):
    return client.post(
        "/api/backtest/ic-apply",
        json={"profile": profile, "version": version, "factors": factors},
    )


def test_b704_applies_factors_updates_profile(client):
    """提交因子IC结果后 factor_configs[f] 实际更新，返回 updated=写入数。"""
    p = _mk_profile("p1", {"factor_configs": {"rsi": 1}})
    r = _apply(client, p, 1, {"rsi": 0.55, "macd": -0.02})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "updated": 2, "version": 2}
    # 真实落盘：factor_configs.rsi/macd 已更新，版本递增
    row = _get_profile(p)
    assert row["version"] == 2
    assert row["body"]["factor_configs"]["rsi"] == 0.55
    assert row["body"]["factor_configs"]["macd"] == -0.02


def test_b704_updated_is_real_written_count(client):
    """返回 updated=真实写入因子数；只有提交的键被写入，其余不动。"""
    p = _mk_profile("p2", {"factor_configs": {"a": 1, "b": 2}})
    r = _apply(client, p, 1, {"a": 9})
    assert r.status_code == 200
    assert r.json()["updated"] == 1
    row = _get_profile(p)
    assert row["body"]["factor_configs"]["a"] == 9
    assert row["body"]["factor_configs"]["b"] == 2  # 未提交键不被触碰

    # factors 为空 → updated=0，版本仍递增（CAS 仍发生）
    r0 = _apply(client, p, row["version"], {})
    assert r0.status_code == 200
    assert r0.json()["updated"] == 0


def test_b704_profile_not_found_404(client):
    r = _apply(client, "no_such_profile", 1, {"a": 1})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "PROFILE_NOT_FOUND"


def test_b704_concurrent_write_conflict_409(client):
    """两写者持同一版本并发 CAS：先者成功，后者 409 VERSION_CONFLICT。"""
    p = _mk_profile("p3", {"factor_configs": {}})
    r1 = _apply(client, p, 1, {"x": 1})
    assert r1.status_code == 200
    assert r1.json()["version"] == 2
    # 第二写者仍用旧版本 1（读到的过期版本）→ 409
    r2 = _apply(client, p, 1, {"y": 2})
    assert r2.status_code == 409
    body = r2.json()["error"]
    assert body["code"] == "VERSION_CONFLICT"
    assert "最新版本=2" in body["message"]
    assert r2.headers.get("x-latest-version") == "2"