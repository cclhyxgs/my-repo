# -*- coding: utf-8 -*-
"""F-105 任务队列底座 / 回测子进程协议 验收测试。

验收（feature-matrix.md:17）：
  ① 提交回测返回 `task_id`
  ② SSE `progress` 事件单调递增
  ③ `POST /cancel` 后 ≤1 只检查点内置 `cancelled`
"""

import json
import time

import pytest

from server.adapters import usage as usage_adapter
from server.worker.executor import get_executor

TERMINAL = {"cancelled", "completed", "failed"}


@pytest.fixture(autouse=True)
def _advanced_usage():
    """回测协议测试需在高级模式运行（F-701 初级 403 门控后影响协议测试）。用例后回 basic。"""
    usage_adapter.set_usage("advanced")
    yield
    usage_adapter.set_usage("basic")


def _parse_sse(resp):
    """把 SSE 响应体解析成 [{event, data, id}, ...]。

    用 `resp.read()` 读完整响应体再解析（任务很快完成，SSE 流正常结束）；
    不用 `iter_lines()`/`text` —— httpx 流式响应下二者偶发返回残缺行或抛 ResponseNotRead。
    """
    body = resp.read().decode("utf-8")
    events = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block or block.startswith(":"):  # 跳过 keepalive 注释帧
            continue
        ev = {}
        for line in block.split("\n"):
            if line.startswith("event: "):
                ev["event"] = line[7:]
            elif line.startswith("data: "):
                ev["data"] = json.loads(line[6:])
            elif line.startswith("id: "):
                ev["id"] = int(line[4:])  # `id: ` 前缀 4 字符（i-d-:-空格）
        if ev:
            events.append(ev)
    return events


def _submit(client, total=5, step_delay=0.01):
    r = client.post("/api/backtest", json={"mode": "factoric", "run_params": {"total": total, "step_delay": step_delay}})
    assert r.status_code == 200, r.text
    return r.json()["task_id"]


def test_bt1_submit_returns_task_id(client):
    """验收①：提交回测返回 task_id。"""
    task_id = _submit(client)
    assert isinstance(task_id, str) and task_id


def test_bt2_sse_progress_monotonic(client):
    """验收②：SSE progress.pct 单调递增，末值 100.0。"""
    task_id = _submit(client, total=5, step_delay=0.01)
    pcts = []
    with client.stream("GET", f"/api/backtest/{task_id}/events") as resp:
        for ev in _parse_sse(resp):
            if ev["event"] == "progress":
                pcts.append(ev["data"]["pct"])
    assert len(pcts) >= 1
    assert pcts == sorted(pcts), f"progress 非单调递增：{pcts}"
    assert pcts[-1] == 100.0


def test_bt3_cancel_within_one_checkpoint(client):
    """验收③：cancel 后 ≤1 只检查点内置 cancelled。"""
    task_id = _submit(client, total=100, step_delay=0.05)
    st = get_executor().status(task_id)
    for _ in range(200):
        st = get_executor().status(task_id)
        if st["current"] >= 10:
            break
        time.sleep(0.05)
    cancel_at = st["current"]
    assert cancel_at >= 10, f"任务未推进到检查点：{st}"

    rc = client.post(f"/api/backtest/{task_id}/cancel")
    assert rc.status_code == 200, rc.text

    for _ in range(200):
        st = get_executor().status(task_id)
        if st["status"] in TERMINAL:
            break
        time.sleep(0.05)
    assert st["status"] == "cancelled", st
    # 取消后最多再跑 1 只检查点
    assert st["current"] - cancel_at <= 1, f"取消延迟超 1 只：cancel_at={cancel_at} final={st['current']}"


def test_bt4_event_contract(client):
    """事件名 ⊆ {progress,log,stage,done,error}，负载含 task_id/ts；进度类事件含 pct/status。"""
    task_id = _submit(client, total=3, step_delay=0.01)
    with client.stream("GET", f"/api/backtest/{task_id}/events") as resp:
        events = _parse_sse(resp)
    assert events
    for ev in events:
        assert ev["event"] in ("progress", "log", "stage", "done", "error"), ev
        d = ev["data"]
        assert d["task_id"] == task_id
        assert "ts" in d
        if ev["event"] in ("progress", "done"):
            assert "status" in d and "pct" in d, d


def test_bt5_state_machine_completed(client):
    """正常完成：终态 completed，current == total。"""
    task_id = _submit(client, total=3, step_delay=0.01)
    st = get_executor().status(task_id)
    for _ in range(100):
        st = get_executor().status(task_id)
        if st["status"] in TERMINAL:
            break
        time.sleep(0.05)
    assert st["status"] == "completed"
    assert st["current"] == st["total"] == 3


def test_bt6_errors(client):
    """未知 task_id → 404；非法 mode → 422。"""
    assert client.get("/api/backtest/unknown_id/events").status_code == 404
    r = client.post("/api/backtest", json={"mode": "bad_mode"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_MODE"
