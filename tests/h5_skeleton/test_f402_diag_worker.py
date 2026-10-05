# -*- coding: utf-8 -*-
"""F-402 诊断 worker 验收测试。

验收（feature-matrix.md:28）：50 只诊断完成时 summary 五组计数之和=50；全程仅 1 次额度扣减。
（B42：F#4 订阅制下「1 次扣减」退化为「不扣减」——授权准入已在 F-401 启动时做 1 次。）

测试用 monkeypatch `engine_bridge.analyze_stock` 返回 mock reportData（不同 status），
worker 在后台线程跑，测试轮询 task_store 至终态后断言 summary。
"""

import time

import pytest

from server.adapters import engine_bridge
from server.core import task_store

TERMINAL = {"cancelled", "completed", "failed"}


def _wait_terminal(task_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = task_store.store.status(task_id)
        if st["status"] in TERMINAL:
            return st
        time.sleep(0.02)
    raise AssertionError(f"任务未在 {timeout}s 内终态：{task_store.store.status(task_id)}")


def _mock_analyze_by_status(status_map, entry_action="空仓观望"):
    """按 code 返回不同 status 的 mock analyze_stock。"""
    def _mock(code, **kwargs):
        status = status_map.get(code, "观望")
        return {
            "reportData": {
                "status": status,
                "entry_action": "博反弹" if status == "博反弹" else entry_action,
                "factorScore": -5,
            }
        }
    return _mock


def _submit(client, text, market="stock", direction=None):
    return client.post("/api/diagnosis", json={"text": text, "market": market, "direction": direction})


def test_dw1_summary_five_groups_sum_to_total(client, monkeypatch):
    """验收①：五组计数之和 == total（4 只不同分类）。"""
    status_map = {
        "sh600000": "回避",   # avoid
        "sh600001": "关注",   # buy
        "sh600002": "博反弹",  # buy（entry_action=博反弹）
        "sh600003": "观望",   # watch
    }
    monkeypatch.setattr(engine_bridge, "analyze_stock", _mock_analyze_by_status(status_map))

    r = _submit(client, "sh600000\nsh600001\nsh600002\nsh600003", market="stock")
    assert r.status_code == 200, r.text
    task_id = r.json()["task_id"]

    _wait_terminal(task_id)
    summary = task_store.store.get(task_id)["summary"]
    five = {k: summary[k] for k in ("buy", "watch", "avoid", "position", "error")}
    assert sum(five.values()) == 4, five
    assert five == {"buy": 2, "watch": 1, "avoid": 1, "position": 0, "error": 0}, five


def test_dw2_classify_correct(client, monkeypatch):
    """分类正确：回避→avoid、关注→buy、博反弹+确认→buy、博反弹+未确认→watch、其他→watch。"""
    status_map = {
        "sh600000": "回避",
        "sh600001": "关注",
        "sh600002": "博反弹",   # entry_action=博反弹 → buy
        "sh600003": "博反弹",   # entry_action=空仓观望 → watch（未确认）
        "sh600004": "其他",
    }
    def _mock(code, **kwargs):
        status = status_map[code]
        return {
            "reportData": {
                "status": status,
                "entry_action": "博反弹" if code == "sh600002" else "空仓观望",
            }
        }
    monkeypatch.setattr(engine_bridge, "analyze_stock", _mock)

    r = _submit(client, "\n".join(status_map.keys()), market="stock")
    task_id = r.json()["task_id"]
    _wait_terminal(task_id)
    results = task_store.store.get(task_id)["results"]
    by_code = {it["code"]: it["category"] for it in results}
    assert by_code["sh600000"] == "avoid"
    assert by_code["sh600001"] == "buy"
    assert by_code["sh600002"] == "buy"
    assert by_code["sh600003"] == "watch"
    assert by_code["sh600004"] == "watch"


def test_dw3_progress_events(client, monkeypatch):
    """逐只 progress：events 里 progress 事件数 == total，current 单调递增。"""
    monkeypatch.setattr(engine_bridge, "analyze_stock", lambda code, **kw: {"reportData": {"status": "观望"}})
    r = _submit(client, "sh600000\nsh600001\nsh600002", market="stock")
    task_id = r.json()["task_id"]
    _wait_terminal(task_id)

    progress = [e for e in task_store.store.get(task_id)["events"] if e["event"] == "progress"]
    assert len(progress) == 3
    currents = [e["data"]["current"] for e in progress]
    assert currents == sorted(currents) == [1, 2, 3]


def test_dw4_no_quota_consumption(client, monkeypatch):
    """B42：订阅制不扣减 —— worker 逐只不触发 consume_use。"""
    import license.license_manager as lm
    called = []
    monkeypatch.setattr(lm, "consume_use", lambda: called.append(1) or True)
    monkeypatch.setattr(engine_bridge, "analyze_stock", lambda code, **kw: {"reportData": {"status": "观望"}})

    r = _submit(client, "sh600000\nsh600001", market="stock")
    _wait_terminal(r.json()["task_id"])
    assert called == [], "诊断 worker 不应扣减额度（订阅制）"


def test_dw5_cancel_within_one(client, monkeypatch):
    """取消：≤1 只内置 cancelled。"""
    import time as _t
    monkeypatch.setattr(
        engine_bridge,
        "analyze_stock",
        lambda code, **kw: (_t.sleep(0.05) or {"reportData": {"status": "观望"}}),
    )
    r = _submit(client, "\n".join(f"sh6000{i:02d}" for i in range(20)), market="stock")
    task_id = r.json()["task_id"]

    # 等跑到 ≥3 只
    st = task_store.store.status(task_id)
    for _ in range(200):
        st = task_store.store.status(task_id)
        if st["current"] >= 3:
            break
        time.sleep(0.02)
    cancel_at = st["current"]
    # 诊断取消端点属 F-403，本条直接调执行器 cancel 验证 worker 协作式中断
    from server.worker.executor import get_executor
    get_executor().cancel(task_id)

    _wait_terminal(task_id)
    st = task_store.store.status(task_id)
    assert st["status"] == "cancelled"
    assert st["current"] - cancel_at <= 1, f"取消延迟超 1 只：cancel_at={cancel_at} final={st['current']}"


def test_dw6_error_tolerated(client, monkeypatch):
    """错误容错：单只 analyze 抛异常 → 归 error，不中断整批。"""
    def _flaky(code, **kw):
        if code == "sh600001":
            raise RuntimeError("取数失败")
        return {"reportData": {"status": "观望"}}
    monkeypatch.setattr(engine_bridge, "analyze_stock", _flaky)

    r = _submit(client, "sh600000\nsh600001\nsh600002", market="stock")
    task_id = r.json()["task_id"]
    _wait_terminal(task_id)

    summary = task_store.store.get(task_id)["summary"]
    assert summary["error"] == 1
    assert sum(summary[k] for k in ("buy", "watch", "avoid", "position", "error")) == 3
