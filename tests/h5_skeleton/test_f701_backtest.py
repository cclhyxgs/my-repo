# -*- coding: utf-8 -*-
"""F-701 回测模式 —— 验收测试。

断言来源：`docs/feature-matrix.md` F-701 行「验收标准」列：
  - `POST /api/backtest {mode:'factoric'}` 返回 `task_id`
  - 非法 mode 4xx
  - 初级账号 403（基本：5 模式 strategy/scoreic/factoric/futures/futuresic 皆可提；
    初级模式门控禁用回测）
"""

import pytest
from server.adapters import usage as usage_adapter

VALID_MODES = ("strategy", "scoreic", "factoric", "futures", "futuresic")


@pytest.fixture
def advanced_usage():
    """切换高级模式；用例结束后回到 basic 缺省。"""
    usage_adapter.set_usage("advanced")
    yield
    usage_adapter.set_usage("basic")


@pytest.fixture
def basic_usage():
    """显式 basic（确保不残留 advanced）。"""
    usage_adapter.set_usage("basic")
    yield


def test_b701_factoric_returns_task_id(client, advanced_usage):
    """验收①：提交 mode='factoric' 返回 task_id。"""
    r = client.post("/api/backtest", json={"mode": "factoric", "run_params": {}})
    assert r.status_code == 200, r.text
    task_id = r.json().get("task_id")
    assert isinstance(task_id, str) and task_id


def test_b701_all_five_modes_accepted(client, advanced_usage):
    """验收②：5 个模式按钮皆可提交，返回 task_id。"""
    for mode in VALID_MODES:
        r = client.post("/api/backtest", json={"mode": mode, "run_params": {}})
        assert r.status_code == 200, f"{mode}: {r.text}"
        assert r.json()["task_id"]


def test_b701_invalid_mode_4xx(client, advanced_usage):
    """验收③：非法 mode → 4xx（422）。"""
    r = client.post("/api/backtest", json={"mode": "bad_mode"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_MODE"


def test_b701_basic_account_forbidden_403(client, basic_usage):
    """验收④：初级账号 → 403（回测禁用）。"""
    r = client.post("/api/backtest", json={"mode": "factoric", "run_params": {}})
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "MODE_FORBIDDEN"


def test_b701_basic_account_invalid_mode_still_422(client, basic_usage):
    """初级下非法 mode 仍先报 422（校验优先于门控）。"""
    r = client.post("/api/backtest", json={"mode": "bad_mode"})
    assert r.status_code == 422