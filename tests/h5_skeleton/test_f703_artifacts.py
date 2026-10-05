# -*- coding: utf-8 -*-
"""F-703 子进程编排 / 产物 URL —— 验收测试。

断言来源：`docs/feature-matrix.md` F-703 行「验收标准」列：
  - 回测产物通过 URL 访问（非 base64 内联）
  - 取消后任务 `cancelled` 且产物可保留
"""

import json
import time

import pytest
from server.adapters import usage as usage_adapter
from server.worker.executor import get_executor

TERMINAL = {"cancelled", "completed", "failed"}


@pytest.fixture(autouse=True)
def _advanced_usage():
    usage_adapter.set_usage("advanced")
    yield
    usage_adapter.set_usage("basic")


def _submit(client, prefix="bt_factoric", total=4, step_delay=0.005):
    r = client.post("/api/backtest", json={
        "mode": "factoric",
        "run_params": {"prefix": prefix, "total": total, "step_delay": step_delay},
    })
    assert r.status_code == 200, r.text
    return r.json()["task_id"]


def _wait_terminal(task_id, patience=200):
    for _ in range(patience):
        st = get_executor().status(task_id)
        if st["status"] in TERMINAL:
            return st
        time.sleep(0.05)
    return get_executor().status(task_id)


def test_b703_completed_artifacts_by_url(client):
    """完成回测：产物经 URL 访问（非 base64 内联），report.json 可下载解析。"""
    tid = _submit(client)
    st = _wait_terminal(tid)
    assert st["status"] == "completed"

    lst = client.get(f"/api/backtest/{tid}/artifacts")
    assert lst.status_code == 200, lst.text
    names = {a["name"] for a in lst.json()["artifacts"]}
    assert "bt_factoric_report.json" in names
    assert "bt_factoric.csv" in names
    # 每个产物都带可访问 url
    for a in lst.json()["artifacts"]:
        assert a["url"].startswith(f"/api/backtest/{tid}/artifacts/")

    # report.json：直接按 JSON 下载（非 base64 内联），可解析
    rep = client.get(f"/api/backtest/{tid}/artifacts/bt_factoric_report.json")
    assert rep.status_code == 200, rep.text
    parsed = rep.json()
    assert parsed["mode"] == "factoric"
    assert parsed["total"] == 4

    # 产物按 URL 访问，响应非再包一层 base64
    t = rep.headers.get("content-type", "")
    assert "json" in t


def test_b703_cancel_retains_artifacts(client):
    """取消回测：任务 cancelled，且已写产物保留、仍可 URL 访问。"""
    tid = _submit(client, total=100, step_delay=0.02)
    for _ in range(200):
        st = get_executor().status(tid)
        if st["current"] >= 10:
            break
        time.sleep(0.05)
    assert st["current"] >= 10, st
    rc = client.post(f"/api/backtest/{tid}/cancel")
    assert rc.status_code == 200, rc.text
    st = _wait_terminal(tid)
    assert st["status"] == "cancelled"

    lst = client.get(f"/api/backtest/{tid}/artifacts")
    assert lst.status_code == 200, lst.text
    # 取消后产物保留（至少含 progress.csv，且仍可 URL 访问）
    artifacts = lst.json()["artifacts"]
    assert artifacts, "取消后应有保留产物"
    names = {a["name"] for a in artifacts}
    assert any(n.endswith("_progress.csv") for n in names)
    prog = next(a for a in artifacts if a["name"].endswith("_progress.csv"))
    res = client.get(prog["url"])
    assert res.status_code == 200, res.text
    body = res.text
    assert body.startswith("idx,pct")


def test_b703_artifact_404_unknown(client):
    """未知 task_id / 未知产物 → 404。"""
    assert client.get("/api/backtest/nope/artifacts").status_code == 404
    tid = _submit(client, total=2)
    _wait_terminal(tid)
    assert client.get(f"/api/backtest/{tid}/artifacts/nope.json").status_code == 404
    # 目录穿越被防
    assert client.get(f"/api/backtest/{tid}/artifacts/../secret").status_code in (404, 400)