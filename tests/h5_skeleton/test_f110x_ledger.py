# -*- coding: utf-8 -*-
"""纪律账本（F-1101/1104/1105）—— 验收测试。

断言来源：`docs/feature-matrix.md` F-1101/1104/1105「验收标准」列。
覆盖：
- F-1101  GET /api/ledger 返回信号 + 冷却（冷却全局跨市场）；批量现价刷新单次请求
- F-1104  GET /api/ledger/summary 返回 4 指标；未执行信号涨→记亏、跌→记盈
- F-1105  POST /signal/{id}/execute 重算情绪化差；乐观语义；不存在 404
- 冷却：未执行累计方向盈亏 ≤ 阈值时 active（镜像桌面口径）
"""

import os

import pytest

from server.db import ledger_repo
from server.db.session import session_scope


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    """独立 SQLite，seed/断言与 API 共用同一库。"""
    from server.db import session as db_session

    url = "sqlite:///" + (tmp_path / "mbull_ledger.db").as_posix()
    monkeypatch.setenv("MBULL_DATABASE_URL", url)
    db_session.dispose()
    yield url
    db_session.dispose()


@pytest.fixture
def env(db_env, monkeypatch):
    from server.adapters import engine_bridge
    monkeypatch.setattr(engine_bridge, "quant_model_snapshot",
                        lambda: {"schemes": {}, "factor_profiles": {}})
    return db_env


def _seed_buy(code, trigger, latest=None, name="X"):
    with session_scope() as session:
        r = ledger_repo.append_signal(
            session, code=code, name=name, signal_type="buy",
            position_ratio=1.0, price=trigger)
        if latest is not None:
            ledger_repo.update_price_by_code(session, code, latest)
        session.commit()
        return r["signal"]["id"]


def _fresh_state():
    from server.db import session as db_session
    db_session.dispose()


# ---------------------------------------------------------------- F-1101 数据与入口
def test_f1101_ledger_returns_signals_and_cooldown(client, env):
    sid = _seed_buy("sh600001", 10.0, latest=11.0)
    r = client.get("/api/ledger").json()
    assert r["ok"] is True
    assert any(s["id"] == sid for s in r["signals"])
    # 冷却全局跨市场：单例数组
    assert isinstance(r["cooldowns"], list) and len(r["cooldowns"]) == 1
    assert "active" in r["cooldowns"][0]
    assert r["cursor"] == sid


def test_f1101_market_filter(client, env):
    with session_scope() as session:
        ledger_repo.append_signal(session, code="rb0", market="futures",
                                  signal_type="buy", price=3900.0)
        session.commit()
    stock = client.get("/api/ledger?market=stock").json()
    fut = client.get("/api/ledger?market=futures").json()
    assert len(fut["signals"]) == 1
    assert all(s["market"] == "futures" for s in fut["signals"])
    assert all(s["market"] != "futures" for s in stock["signals"])


def test_f1101_prices_batch_refresh(client, env):
    sid = _seed_buy("sh600001", 10.0)
    r = client.post("/api/ledger/prices", json={"prices": {"sh600001": 12.0}})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    got = client.get("/api/ledger").json()["signals"]
    assert any(s["id"] == sid and s["latest_price"] == 12.0 for s in got)


# ---------------------------------------------------------------- F-1104 账本统计
def test_f1104_unexecuted_rise_records_loss(client, env):
    """未执行 buy 信号，现价涨 → 情绪化差为负（记亏）。"""
    _seed_buy("sh600001", 10.0, latest=12.0)
    item = client.get("/api/ledger").json()["signals"][0]
    assert item["executed"] is False
    assert item["emotion_diff"] == -20.0  # -(12-10)/10*100 = -20
    # summary 情绪化差合计为负
    s = client.get("/api/ledger/summary").json()
    assert s["emotion_pct_sum"] == -20.0
    assert s["not_executed_count"] == 1
    assert s["disciple_count"] == 0


def test_f1104_unexecuted_fall_records_gain(client, env):
    """未执行 buy 信号，现价跌 → 情绪化差为正（记盈）。"""
    _seed_buy("sh600001", 10.0, latest=8.0)
    item = client.get("/api/ledger").json()["signals"][0]
    assert item["emotion_diff"] == 20.0


def test_f1104_summary_four_indicators(client, env):
    with session_scope() as session:
        ledger_repo.append_signal(session, code="a", signal_type="buy",
                                  position_ratio=1.0, price=10.0)
        ledger_repo.append_signal(session, code="b", signal_type="add",
                                  position_ratio=0.5, price=20.0)
        session.commit()
    s = client.get("/api/ledger/summary").json()
    assert s["ok"] is True
    # 4 指标键存在
    for k in ("disciple_count", "not_executed_count", "emotion_pct_sum", "bias_pct"):
        assert k in s
    assert s["total_signals"] == 2


# ---------------------------------------------------------------- F-1105 登记执行
def test_f1105_execute_recomputes_emotion_diff(client, env):
    sid = _seed_buy("sh600001", 10.0, latest=12.0)
    assert client.get("/api/ledger").json()["signals"][0]["emotion_diff"] == -20.0
    # 足额执行（exec_price=10, ratio=100）→ emotion_diff 归 0（纪律执行）
    r = client.post(f"/api/signal/{sid}/execute",
                    json={"executed": True, "exec_price": 10.0, "exec_ratio": 100.0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and "emotion_diff" in body
    got = client.get("/api/ledger").json()["signals"]
    assert any(s["id"] == sid and s["executed"] is True and s["emotion_diff"] == 0.0 for s in got)


def test_f1105_execute_missing_404(client, env):
    r = client.post("/api/signal/nonexistent/execute",
                    json={"executed": True, "exec_price": 1.0, "exec_ratio": 50.0})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SIGNAL_NOT_FOUND"


# ---------------------------------------------------------------- 冷却（F-1103 消费面）
def test_cooldown_active_after_big_missed_gain(client, env):
    """未执行 buy 信号，现价大涨（错过了）→ 情绪偏差为负 → 触发冷却（阈值 -8）。"""
    _seed_buy("sh600001", 10.0, latest=14.0)  # +40%，未执行 → 记亏 -40
    cd = client.get("/api/ledger").json()["cooldowns"][0]
    assert cd["active"] is True
    assert cd["bias_pct"] <= cd["threshold_pct"]


def test_cooldown_gate_blocks_new_signal(client, env):
    # 先制造冷却激活（未执行 + 大涨）
    _seed_buy("sh600001", 10.0, latest=14.0)
    assert client.get("/api/ledger").json()["cooldowns"][0]["active"] is True
    # 冷却激活时 append 不再新增信号
    with session_scope() as session:
        res = ledger_repo.append_signal(session, code="sh600099",
                                        signal_type="buy", price=20.0, cooldown_gate=True)
        session.commit()
        assert res["signal"] is None
    assert client.get("/api/ledger").json()["signals"][0]["code"] == "sh600001"