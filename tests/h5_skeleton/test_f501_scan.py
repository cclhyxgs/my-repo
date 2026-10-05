# -*- coding: utf-8 -*-
"""F-501~506 全市场扫描验收测试（H5 后端骨架）。

策略：monkeypatch `engine_bridge.scan_stock_codes` / `scan_chunk_score` / `scan_load_scheme`
注入小池 + 确定性结果（不碰真实 K 线/网络）。worker 状态机、进度、分页、缓存、板块、
导出/暂停/取消 全部走真实 server 代码路径（TestClient + 进程内线程池）。

断言来源：feature-matrix.md §一 F-501~506 行「验收标准」列。
"""

import json
import os
import time

import pytest

from server.adapters import engine_bridge, scan as scan_adapter


def _make_result(code, name, sector, final_score, stars, tier="weak", snapshot=True):
    r = {
        "code": code,
        "name": name,
        "price": 10.0 + final_score / 10.0,
        "stock_score": final_score - 5,
        "final_score": final_score,
        "tech_strength": final_score / 100.0,
        "stars": stars,
        "level": "强势" if final_score >= 80 else "中性",
        "sector": sector,
        "entry_tier": tier,
        "entry_tier_label": "强势" if tier == "strong" else "弱势",
    }
    if snapshot:
        r["tech_snapshot"] = {"tech": {"rsi": 50}, "market": {}, "adx": {"adx": 20}}
    return r


def _poll(client, task_id, timeout=15):
    for _ in range(int(timeout / 0.05)):
        r = client.get(f"/api/scan/{task_id}")
        if r.status_code == 200:
            j = r.json()
            if j["status"] in ("completed", "cancelled", "failed"):
                return j
        time.sleep(0.05)
    return client.get(f"/api/scan/{task_id}").json()


@pytest.fixture
def scan_env(monkeypatch):
    """隔离引擎副作用：方案加载 no-op，板块映射空（chunk_score 被整体 monkeypatch）。"""
    monkeypatch.setattr(engine_bridge, "scan_load_scheme", lambda *a, **k: None)
    monkeypatch.setattr(engine_bridge, "scan_build_sector_map", lambda: {})
    yield


# ============================================================
# F-501 扫描启动
# ============================================================

def test_f501_start_returns_task_id(client, scan_env, monkeypatch):
    monkeypatch.setattr(engine_bridge, "scan_stock_codes", lambda: ["sh600519", "sz000001", "sh600036"])
    monkeypatch.setattr(
        engine_bridge, "scan_chunk_score",
        lambda codes, *a, **k: [_make_result(c, c, "银行", 50, "★★★") for c in codes],
    )
    r = client.post("/api/scan", json={"market": "stock", "use_cache": False})
    assert r.status_code == 200
    body = r.json()
    assert "task_id" in body and isinstance(body["task_id"], str) and body["task_id"]
    assert body["reused"] is False


def test_f501_minute_ktyped_rejected(client, scan_env, monkeypatch):
    r = client.post("/api/scan", json={"market": "stock", "k_type": "1分钟", "use_cache": False})
    assert r.status_code == 422
    assert "扫描不支持" in (r.json().get("error", {}).get("message") or "")


def test_f501_invalid_market_rejected(client, scan_env, monkeypatch):
    r = client.post("/api/scan", json={"market": "xxx", "use_cache": False})
    assert r.status_code == 422


# ============================================================
# F-502 扫描 worker
# ============================================================

def test_f502_completes_with_accurate_count(client, scan_env, monkeypatch):
    pool = [f"sh60050{i}" for i in range(7)]
    monkeypatch.setattr(engine_bridge, "scan_stock_codes", lambda: pool)
    monkeypatch.setattr(
        engine_bridge, "scan_chunk_score",
        lambda codes, *a, **k: [_make_result(c, c, "银行", 50, "★★★") for c in codes],
    )
    tid = client.post("/api/scan", json={"market": "stock", "use_cache": False}).json()["task_id"]
    prog = _poll(client, tid)
    assert prog["status"] == "completed"
    assert prog["total"] == 7
    assert prog["current"] == 7
    res = client.get(f"/api/scan/{tid}/results").json()
    assert res["total"] == 7


def test_f502_abandoned_chunk_still_progresses(client, scan_env, monkeypatch):
    """被放弃分片（整片评分失败返回 []）仍推进至 100%（F-502 验收）。"""
    pool = [f"sh60050{i}" for i in range(7)]
    monkeypatch.setattr(engine_bridge, "scan_stock_codes", lambda: pool)
    monkeypatch.setattr(engine_bridge, "scan_chunk_score", lambda codes, *a, **k: [])  # 全片失败
    tid = client.post("/api/scan", json={"market": "stock", "use_cache": False}).json()["task_id"]
    prog = _poll(client, tid)
    assert prog["status"] == "completed"
    assert prog["total"] == 7
    assert prog["current"] == 7  # 进度仍到 100%，不因失败片倒退
    assert prog["pct"] == 100.0


# ============================================================
# F-503 结果分页 / 筛选 / 剥离 tech_snapshot
# ============================================================

def test_f503_results_strip_snapshot_and_paginate(client, scan_env, monkeypatch):
    pool = [f"sh60050{i}" for i in range(7)]
    monkeypatch.setattr(engine_bridge, "scan_stock_codes", lambda: pool)
    monkeypatch.setattr(
        engine_bridge, "scan_chunk_score",
        lambda codes, *a, **k: [_make_result(c, c, "银行", 50 + i, "★★★") for i, c in enumerate(codes)],
    )
    tid = client.post("/api/scan", json={"market": "stock", "use_cache": False}).json()["task_id"]
    _poll(client, tid)
    res = client.get(f"/api/scan/{tid}/results?limit=200").json()
    assert res["total"] == 7
    assert len(res["items"]) == 7
    # 剥离 tech_snapshot（F-503 硬要求）
    assert all("tech_snapshot" not in it for it in res["items"])


def test_f503_rating_filter(client, scan_env, monkeypatch):
    pool = ["sh600501", "sh600502"]
    monkeypatch.setattr(engine_bridge, "scan_stock_codes", lambda: pool)
    results = {
        # 两种星号必须都被计数：engine/signal_rating 实际产出 emoji 星 "⭐"*tier（U+2B50），
        # 而早期夹具/历史数据写作 "★"（U+2605）。只认一种会让另一类星数恒为 0 → 过滤整批清空。
        "sh600501": _make_result("sh600501", "A", "银行", 80, "⭐⭐⭐⭐⭐"),
        "sh600502": _make_result("sh600502", "B", "银行", 40, "★★★"),
    }
    monkeypatch.setattr(
        engine_bridge, "scan_chunk_score",
        lambda codes, *a, **k: [results[c] for c in codes if c in results],
    )
    tid = client.post("/api/scan", json={"market": "stock", "use_cache": False}).json()["task_id"]
    _poll(client, tid)
    only5 = client.get(f"/api/scan/{tid}/results?rating=5").json()
    assert only5["filtered_total"] == 1
    assert only5["items"][0]["code"] == "sh600501"
    # 回归守卫：rating=3 时 emoji 星(5) 与 ★(3) 都应留存
    at_least3 = client.get(f"/api/scan/{tid}/results?rating=3").json()
    assert at_least3["filtered_total"] == 2
    # rating=4 时只有 emoji 5 星那只通过
    at_least4 = client.get(f"/api/scan/{tid}/results?rating=4").json()
    assert at_least4["filtered_total"] == 1
    assert at_least4["items"][0]["code"] == "sh600501"


# ============================================================
# F-504 扫描缓存 24h
# ============================================================

def test_f504_fresh_cache_reused(client, scan_env, monkeypatch):
    pool = [f"sh60050{i}" for i in range(5)]
    monkeypatch.setattr(engine_bridge, "scan_stock_codes", lambda: pool)
    monkeypatch.setattr(
        engine_bridge, "scan_chunk_score",
        lambda codes, *a, **k: [_make_result(c, c, "银行", 50, "★★★") for c in codes],
    )
    # 首次真扫（落缓存）
    tid1 = client.post("/api/scan", json={"market": "stock", "use_cache": False}).json()["task_id"]
    _poll(client, tid1)
    # 缓存状态
    cache = client.get("/api/scan/cache?market=stock").json()
    assert cache["exists"] is True
    assert cache["stale"] is False
    # 二次启动（use_cache=True）→ 复用
    r2 = client.post("/api/scan", json={"market": "stock", "use_cache": True})
    assert r2.status_code == 200
    assert r2.json()["reused"] is True
    # 复用任务立即可读、已完成
    tid2 = r2.json()["task_id"]
    prog2 = client.get(f"/api/scan/{tid2}").json()
    assert prog2["status"] == "completed"


def test_f504_stale_no_very_stale():
    """>24h 缓存 stale=true，且响应绝不出现 very_stale（修既有缺陷）。"""
    import datetime

    market = "stock_stale_probe"
    path = scan_adapter._cache_path(market)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    now = scan_adapter.srv_time.now()
    old = (now - datetime.timedelta(hours=25)).isoformat()
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"market": market, "saved_at": old, "results": []}, f)
    try:
        c = scan_adapter.get_scan_cache(market)
        assert c["exists"] is True
        assert c["stale"] is True
        assert "very_stale" not in c
    finally:
        if os.path.exists(path):
            os.remove(path)


# ============================================================
# F-505 板块强度聚合
# ============================================================

def test_f505_sectors_aggregate(client, scan_env, monkeypatch):
    # 银行(4只,均分30→强) / 汽车(2只→不足3剔除) / 医药(3只,均分~10.7→弱)
    bank = [_make_result(f"sh60050{i}", f"B{i}", "银行", s, "★★★", tier="strong")
            for i, s in enumerate([35, 32, 28, 25])]
    car = [_make_result("sh600600", "C1", "汽车", 40, "★★★"),
           _make_result("sh600601", "C2", "汽车", 42, "★★★")]
    medi = [_make_result(f"sh60070{i}", f"M{i}", "医药", s, "★★")
            for i, s in enumerate([12, 11, 9])]
    by_code = {r["code"]: r for r in bank + car + medi}
    pool = list(by_code.keys())
    monkeypatch.setattr(engine_bridge, "scan_stock_codes", lambda: pool)
    monkeypatch.setattr(
        engine_bridge, "scan_chunk_score",
        lambda codes, *a, **k: [by_code[c] for c in codes if c in by_code],
    )
    tid = client.post("/api/scan", json={"market": "stock", "use_cache": False}).json()["task_id"]
    _poll(client, tid)
    sec = client.get(f"/api/sectors?task_id={tid}").json()
    names = {s["sector"] for s in sec["sectors"]}
    assert "汽车" not in names, "不足 3 成分股的板块必须剔除"
    assert "银行" in names and "医药" in names
    bank_s = next(s for s in sec["sectors"] if s["sector"] == "银行")
    assert bank_s["level"] == "强" and bank_s["count"] == 4
    medi_s = next(s for s in sec["sectors"] if s["sector"] == "医药")
    assert medi_s["level"] == "弱"


# ============================================================
# F-506 导出 / 暂停 / 取消
# ============================================================

def test_f506_export_csv_utf8sig(client, scan_env, monkeypatch):
    pool = [f"sh60050{i}" for i in range(3)]
    monkeypatch.setattr(engine_bridge, "scan_stock_codes", lambda: pool)
    monkeypatch.setattr(
        engine_bridge, "scan_chunk_score",
        lambda codes, *a, **k: [_make_result(c, c, "银行", 50, "★★★") for c in codes],
    )
    tid = client.post("/api/scan", json={"market": "stock", "use_cache": False}).json()["task_id"]
    _poll(client, tid)
    r = client.get(f"/api/scan/{tid}/export")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers.get("content-disposition", "")
    body = r.content
    assert body[:3] == b"\xef\xbb\xbf", "CSV 必须 utf-8-sig（EF BB BF）"
    assert "代码".encode("utf-8") in body


def test_f506_cancel_and_pause_contract(client, scan_env, monkeypatch):
    """慢 worker：提交后轮询到 running，暂停→paused，继续→completed；终态取消→409。"""
    pool = [f"sh60050{i}" for i in range(60)]  # 3 片 × 20
    monkeypatch.setattr(engine_bridge, "scan_stock_codes", lambda: pool)
    monkeypatch.setattr(
        engine_bridge, "scan_chunk_score",
        lambda codes, *a, **k: (time.sleep(0.3) or [_make_result(c, c, "银行", 50, "★★★") for c in codes]),
    )
    tid = client.post("/api/scan", json={"market": "stock", "use_cache": False}).json()["task_id"]

    # 等到 running
    for _ in range(100):
        if client.get(f"/api/scan/{tid}").json()["status"] == "running":
            break
        time.sleep(0.05)

    # 暂停
    pr = client.post(f"/api/scan/{tid}/pause")
    assert pr.status_code == 200 and pr.json()["status"] == "paused"
    # 轮询到 paused
    for _ in range(100):
        if client.get(f"/api/scan/{tid}").json()["status"] == "paused":
            break
        time.sleep(0.05)
    assert client.get(f"/api/scan/{tid}").json()["status"] == "paused"

    # 继续
    rr = client.post(f"/api/scan/{tid}/resume")
    assert rr.status_code == 200 and rr.json()["status"] == "running"
    _poll(client, tid)
    assert client.get(f"/api/scan/{tid}").json()["status"] == "completed"

    # 终态取消 → 409
    cr = client.post(f"/api/scan/{tid}/cancel")
    assert cr.status_code == 409
