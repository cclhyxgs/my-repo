# -*- coding: utf-8 -*-
"""F-401~404 前端页打通的使能验证：GET /api/diagnosis/{id} 须回传 results。

前端五组结果表与 F-404 导出都需要逐只明细 results=[{code,name,category}]，
而 worker 已将其存入 task；此前 GET 仅返回 summary，前端拿不到明细。
本测试锁定「GET 响应含 results 列表」这一契约。
"""
import time

from server.adapters import engine_bridge

TERMINAL = {"cancelled", "completed", "failed"}


def _wait_terminal(task_id, timeout=10.0):
    deadline = time.time() + timeout
    from server.core import task_store

    while time.time() < deadline:
        st = task_store.store.status(task_id)
        if st["status"] in TERMINAL:
            return st
        time.sleep(0.02)
    raise AssertionError("任务未终态")


def test_get_returns_results(client, monkeypatch):
    """诊断完成后 GET 响应须含 results（每只为 {code,name,category}）。"""
    monkeypatch.setattr(
        engine_bridge,
        "analyze_stock",
        lambda code, **kw: {"reportData": {"status": "观望"}},
    )
    r = client.post(
        "/api/diagnosis",
        json={"text": "sh600000\nsh600001", "market": "stock"},
    )
    task_id = r.json()["task_id"]
    _wait_terminal(task_id)

    pr = client.get(f"/api/diagnosis/{task_id}")
    assert pr.status_code == 200
    body = pr.json()
    assert "results" in body, "GET 响应缺少 results 字段"
    assert isinstance(body["results"], list)
    assert len(body["results"]) == 2
    assert set(body["results"][0].keys()) >= {"code", "name", "category"}
