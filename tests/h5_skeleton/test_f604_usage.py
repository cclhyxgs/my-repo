# -*- coding: utf-8 -*-
"""F-604 模式门控（初/高级）—— 验收测试。

断言来源：`docs/feature-matrix.md` F-604 行「验收标准」列。

覆盖：
- GET /api/usage 默认 basic（无 usage.json / 缺省字段）；显式 advanced → advanced（F-1601 只读子集）
- PUT /api/usage 切换 + 非法值 422
- basic 模式：`/api/scan` 默认排序按 `tech_strength*100`（镜像桌面 `_scan_rank`）
- advanced 模式：`/api/scan` 默认排序按 `final_score`
- 显式 sort=tech_strength / sort=price 在 basic 下仍各自独立生效（不被 final_score 门控覆盖）
"""

import json
import os

import pytest

from server.adapters import engine_bridge, usage as usage_adapter


@pytest.fixture
def usage_env(tmp_path, monkeypatch):
    """把 usage.json 指向隔离目录；每用例结束清理，回到 basic 缺省。"""
    # 阻断错误写法：路径隔离，避免写坏 session 级真实 QUANT_SYSTEM_DIR
    monkeypatch.setattr(usage_adapter, "_usage_path", lambda: str(tmp_path / "usage.json"))
    monkeypatch.setattr(engine_bridge, "scan_load_scheme", lambda *a, **k: None)
    monkeypatch.setattr(engine_bridge, "scan_build_sector_map", lambda: {})
    yield tmp_path
    # 尽量清理本用例写入的 usage.json，避免污染后续（回到缺省 basic）
    try:
        os.remove(tmp_path / "usage.json")
    except FileNotFoundError:
        pass


def _set_mode(tmp_path, mode):
    (tmp_path / "usage.json").write_text(
        json.dumps({"usage_mode": mode}, ensure_ascii=False), encoding="utf-8"
    )


def _make(tech, final):
    """构造一条确定性结果：tech_strength 与 final_score 故意错位，用于验证排序依据切换。"""
    return {
        "code": f"code_{tech}_{final}",
        "name": "X",
        "price": 10.0,
        "stock_score": final - 5,
        "final_score": final,
        "tech_strength": tech,
        "stars": "★★★",
        "level": "中性",
        "sector": "银行",
        "entry_tier": "weak",
        "entry_tier_label": "弱势",
        "tech_snapshot": {"tech": {"rsi": 50}, "market": {}, "adx": {"adx": 20}},
    }


def _scan_and_order(client, monkeypatch, pool, by_code):
    runner = monkeypatch.setattr
    import time

    runner(engine_bridge, "scan_stock_codes", lambda: pool)
    runner(engine_bridge, "scan_chunk_score",
           lambda codes, *a, **k: [by_code[c] for c in codes if c in by_code])
    tid = client.post("/api/scan", json={"market": "stock", "use_cache": False}).json()["task_id"]
    for _ in range(300):
        j = client.get(f"/api/scan/{tid}").json()
        if j["status"] in ("completed", "cancelled", "failed"):
            break
        time.sleep(0.05)
    res = client.get(f"/api/scan/{tid}/results").json()
    return res


# ---------------------------------------------------------------- 默认值（F-1601 只读子集）
def test_f604_usage_default_basic(client, usage_env):
    r = client.get("/api/usage").json()
    assert r["ok"] is True
    assert r["usage_mode"] == "basic"
    assert r["is_basic"] is True


def test_f604_usage_advanced_read_and_put(client, usage_env):
    # 显式切换 advanced → 读回 advanced
    put = client.put("/api/usage", json={"usage_mode": "advanced"})
    assert put.status_code == 200, put.text
    assert put.json()["usage_mode"] == "advanced"
    assert put.json()["is_basic"] is False
    assert client.get("/api/usage").json()["usage_mode"] == "advanced"


def test_f604_usage_invalid_mode_422(client, usage_env):
    for bad in ("premium", "", None):
        r = client.put("/api/usage", json={"usage_mode": bad})
        assert r.status_code == 422, bad

    # 缺省文件含非法字段 → 回退 basic
    (usage_env / "usage.json").write_text(json.dumps({"usage_mode": "weird"}), encoding="utf-8")
    assert client.get("/api/usage").json()["usage_mode"] == "basic"


# ---------------------------------------------------------------- 扫描排序门控（F-604 验收）
def test_f604_scan_sorted_by_tech_strength_in_basic(client, usage_env, monkeypatch):
    """basic 下：A(final=90,tech=0.2) vs B(final=10,tech=0.9)。
    tech_strength*100：B(90) > A(20) → [B, A]；final_score：A(90) > B(10) → [A, B]。
    """
    by_code = {
        "sh600001": _make(tech=0.2, final=90),
        "sh600002": _make(tech=0.9, final=10),
    }
    res = _scan_and_order(client, monkeypatch, list(by_code.keys()), by_code)
    assert res["total"] == 2
    # basic（缺省）→ tech_strength*100 排序：tech0.9(→90) 在前
    assert [it["code"] for it in res["items"]] == ["code_0.9_10", "code_0.2_90"]


def test_f604_scan_sorted_by_final_score_in_advanced(client, usage_env, monkeypatch):
    _set_mode(usage_env, "advanced")
    by_code = {
        "sh600001": _make(tech=0.2, final=90),
        "sh600002": _make(tech=0.9, final=10),
    }
    res = _scan_and_order(client, monkeypatch, list(by_code.keys()), by_code)
    assert [it["code"] for it in res["items"]] == ["code_0.2_90", "code_0.9_10"]


def test_f604_scan_explicit_tech_strength_sort_independent(client, usage_env, monkeypatch):
    """basic 下显式 sort=tech_strength 仍按 tech_strength（非×100）排序，且不被门控覆盖。"""
    _set_mode(usage_env, "advanced")
    by_code = {
        "sh600001": _make(tech=0.2, final=90),
        "sh600002": _make(tech=0.9, final=10),
    }
    pool = list(by_code.keys())
    monkeypatch.setattr(engine_bridge, "scan_stock_codes", lambda: pool)
    monkeypatch.setattr(engine_bridge, "scan_chunk_score",
                        lambda codes, *a, **k: [by_code[c] for c in codes if c in by_code])
    tid = client.post("/api/scan", json={"market": "stock", "use_cache": False}).json()["task_id"]
    res = client.get(f"/api/scan/{tid}/results?sort=tech_strength").json()
    assert [it["code"] for it in res["items"]] == ["code_0.9_10", "code_0.2_90"]


def test_f604_scan_explicit_price_sort_independent(client, usage_env, monkeypatch):
    """advanced 下显式 sort=price 按价格排序，独立于 final_score。"""
    _set_mode(usage_env, "advanced")

    def _mk_price(code, p):
        r = _make(tech=0.5, final=50)
        r.update({"code": code, "price": p})
        return r

    by_code = {"sh600001": _mk_price("sh600001", 8.8), "sh600002": _mk_price("sh600002", 12.3)}
    pool = list(by_code.keys())
    monkeypatch.setattr(engine_bridge, "scan_stock_codes", lambda: pool)
    monkeypatch.setattr(engine_bridge, "scan_chunk_score",
                        lambda codes, *a, **k: [by_code[c] for c in codes if c in by_code])
    tid = client.post("/api/scan", json={"market": "stock", "use_cache": False}).json()["task_id"]
    res = client.get(f"/api/scan/{tid}/results?sort=price&order=desc").json()
    assert [it["code"] for it in res["items"]] == ["sh600002", "sh600001"]