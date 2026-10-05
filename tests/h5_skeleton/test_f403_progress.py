# -*- coding: utf-8 -*-
"""F-403 进度与取消 验收测试。

验收（feature-matrix.md:29）：诊断运行中 SSE 收到 progress 事件；
`POST /diag/{id}/cancel` 后 worker ≤1 只内退出，状态 cancelled。
"""

import json
import time

from server.adapters import engine_bridge
from server.core import task_store

TERMINAL = {"cancelled", "completed", "failed"}


def _parse_sse(resp):
    """解析 SSE 响应体（resp.read() 完整读取，httpx 流式 iter_lines 不可靠）。"""
    body = resp.read().decode("utf-8")
    events = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block or block.startswith(":"):
            continue
        ev = {}
        for line in block.split("\n"):
            if line.startswith("event: "):
                ev["event"] = line[7:]
            elif line.startswith("data: "):
                ev["data"] = json.loads(line[6:])
            elif line.startswith("id: "):
                ev["id"] = int(line[4:])  # `id: ` 前缀 4 字符
        if ev:
            events.append(ev)
    return events


def _wait_terminal(task_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = task_store.store.status(task_id)
        if st["status"] in TERMINAL:
            return st
        time.sleep(0.02)
    raise AssertionError(f"任务未终态：{task_store.store.status(task_id)}")


def test_pr1_progress_endpoint(client, monkeypatch):
    """GET /api/diagnosis/{id} → {status, summary, total}。"""
    monkeypatch.setattr(engine_bridge, "analyze_stock", lambda code, **kw: {"reportData": {"status": "观望"}})
    r = client.post("/api/diagnosis", json={"text": "sh600000\nsh600001", "market": "stock"})
    task_id = r.json()["task_id"]
    _wait_terminal(task_id)

    pr = client.get(f"/api/diagnosis/{task_id}")
    assert pr.status_code == 200
    body = pr.json()
    assert body["status"] == "completed"
    assert body["total"] == 2
    assert body["summary"] is not None
    five = {k: body["summary"][k] for k in ("buy", "watch", "avoid", "position", "error")}
    assert sum(five.values()) == 2


def test_pr2_sse_progress_events(client, monkeypatch):
    """验收①：诊断运行中 SSE 收到 progress 事件（current 单调递增）。"""
    monkeypatch.setattr(engine_bridge, "analyze_stock", lambda code, **kw: {"reportData": {"status": "观望"}})
    r = client.post("/api/diagnosis", json={"text": "sh600000\nsh600001\nsh600002", "market": "stock"})
    task_id = r.json()["task_id"]

    with client.stream("GET", f"/api/diagnosis/{task_id}/events") as resp:
        events = _parse_sse(resp)
    progress = [e for e in events if e["event"] == "progress"]
    assert len(progress) == 3
    currents = [e["data"]["current"] for e in progress]
    assert currents == sorted(currents) == [1, 2, 3]


def test_pr3_cancel_endpoint(client, monkeypatch):
    """验收②：POST /diag/{id}/cancel 后 ≤1 只内退出，状态 cancelled。"""
    import time as _t
    monkeypatch.setattr(
        engine_bridge,
        "analyze_stock",
        lambda code, **kw: (_t.sleep(0.05) or {"reportData": {"status": "观望"}}),
    )
    text = "\n".join(f"sh6000{i:02d}" for i in range(20))
    r = client.post("/api/diagnosis", json={"text": text, "market": "stock"})
    task_id = r.json()["task_id"]

    st = task_store.store.status(task_id)
    for _ in range(200):
        st = task_store.store.status(task_id)
        if st["current"] >= 3:
            break
        time.sleep(0.02)
    cancel_at = st["current"]
    assert cancel_at >= 3

    rc = client.post(f"/api/diagnosis/{task_id}/cancel")
    assert rc.status_code == 200, rc.text
    assert rc.json()["status"] == "cancelling"

    _wait_terminal(task_id)
    st = task_store.store.status(task_id)
    assert st["status"] == "cancelled"
    assert st["current"] - cancel_at <= 1, f"取消延迟超 1 只：cancel_at={cancel_at} final={st['current']}"


def test_pr4_unknown_task_404(client):
    """未知 task_id → 404。"""
    assert client.get("/api/diagnosis/unknown_id").status_code == 404
    assert client.get("/api/diagnosis/unknown_id/events").status_code == 404
    assert client.post("/api/diagnosis/unknown_id/cancel").status_code == 404


def test_pr5_cancel_after_terminal_409(client, monkeypatch):
    """已终态任务 cancel → 409。"""
    monkeypatch.setattr(engine_bridge, "analyze_stock", lambda code, **kw: {"reportData": {"status": "观望"}})
    r = client.post("/api/diagnosis", json={"text": "sh600000", "market": "stock"})
    task_id = r.json()["task_id"]
    _wait_terminal(task_id)

    rc = client.post(f"/api/diagnosis/{task_id}/cancel")
    assert rc.status_code == 409
