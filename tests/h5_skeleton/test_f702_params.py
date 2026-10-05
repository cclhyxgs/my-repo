# -*- coding: utf-8 -*-
"""F-702 参数映射 —— 验收测试。

断言来源：`docs/feature-matrix.md` F-702 行「验收标准」列：
  - 选「全市场」提交后后端收到 `pool='full'`
  - `hold=70` 被拒并提示范围 5-60
后端侧（B 双端兜底）：`GET /api/backtest/modes` 提供各模式池/持币/扫描约束；
`POST /api/backtest` 对已显式携带的运行参数做再校验（只映射，不强制缺省）。
"""

import pytest
from server.adapters import usage as usage_adapter


@pytest.fixture(autouse=True)
def _advanced_usage():
    usage_adapter.set_usage("advanced")
    yield
    usage_adapter.set_usage("basic")


def test_b702_get_modes_returns_five_with_constraints(client):
    """GET /api/backtest/modes 返回 5 模式，含 pool/hold/scan 约束（hold 5-60）。"""
    r = client.get("/api/backtest/modes")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    modes = data["modes"]
    assert [m["mode"] for m in modes] == ["strategy", "scoreic", "factoric", "futures", "futuresic"]
    for m in modes:
        assert m["hold"] == {"min": 5, "max": 60, "default": 30}
        assert m["scan"] == {"min": 1, "max": 20, "default": 5}
        assert "pool" in m and "options" in m["pool"]


def test_b702_pool_full_accepted(client):
    """选「全市场」→ 后端收到 pool='full'（futures 模式用 all）。"""
    r = client.post("/api/backtest", json={"mode": "factoric", "run_params": {"pool": "full"}})
    assert r.status_code == 200, r.text
    r2 = client.post("/api/backtest", json={"mode": "futures", "run_params": {"pool": "all"}})
    assert r2.status_code == 200, r2.text


def test_b702_hold_70_rejected_with_range(client):
    """hold=70 被拒（422），提示含范围 5-60。"""
    r = client.post("/api/backtest", json={"mode": "factoric", "run_params": {"hold": 70}})
    assert r.status_code == 422, r.text
    body = r.json()["error"]
    assert body["code"] == "INVALID_RUN_PARAMS"
    assert "5-60" in body["message"]


def test_b702_hold_boundaries_and_scan(client):
    """hold 5/60 边界通过；scan 越界被拒。"""
    for hold in (5, 60):
        r = client.post("/api/backtest", json={"mode": "factoric", "run_params": {"hold": hold}})
        assert r.status_code == 200, f"hold={hold}: {r.text}"
    bad_scan = client.post("/api/backtest", json={"mode": "factoric", "run_params": {"scan": 21}})
    assert bad_scan.status_code == 422
    ok_scan = client.post("/api/backtest", json={"mode": "scoreic", "run_params": {"scan": 20}})
    assert ok_scan.status_code == 200, ok_scan.text


def test_b702_unknown_pool_rejected(client):
    """不在池选项内的 pool 被拒（后端兜底防静默失配落 else 分支）。"""
    r = client.post("/api/backtest", json={"mode": "factoric", "run_params": {"pool": "自选股列表"}})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_RUN_PARAMS"