# -*- coding: utf-8 -*-
"""F-601 方案管理 —— 验收测试。

断言来源：`docs/feature-matrix.md:37` F-601 行「验收标准」列 +
`docs/app-architecture.md` §3.2 能力 5 端点契约（字段名逐字）。

覆盖：
- ASPECT-1  `POST /api/scheme` 建同名方案返回 **409**
- ASPECT-2  命名含 `-`（以及空/超 50）被拒 → 4xx
- ASPECT-3  **并发写同一 `factor_profile` 一成一败（乐观锁）**
- ASPECT-4  `GET /api/scheme` 字段名逐字对齐 §3.2
- ASPECT-5  `PUT` 版本号 CAS 冲突 → 409 且携带最新版本
- ASPECT-6  `DELETE` 204 / 不存在 404
- ASPECT-7  导入 3 格式兼容（策略库 / 单方案完整对象 / 单方案 flat）
- ASPECT-8  导入失败整体回滚、无残留
- ASPECT-9  导出 JSON 文件流 + 中文文件名 RFC 5987
- ASPECT-10 首次播种（engine 存量 → 两表）

隔离：每个测试用独立 SQLite（`MBULL_DATABASE_URL` 指向 `tmp_path`），
并把 `engine_bridge.quant_model_snapshot` 换成受控桩（不 import engine、确定性）。
"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote

import pytest


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    """独立 SQLite + 重置播种状态 + 受控 engine 快照。"""
    from server.adapters import engine_bridge
    from server.api import scheme as scheme_api
    from server.db import session as db_session

    url = "sqlite:///" + (tmp_path / "mbull_test.db").as_posix()
    monkeypatch.setenv("MBULL_DATABASE_URL", url)
    monkeypatch.setattr(
        engine_bridge, "quant_model_snapshot",
        lambda: {"schemes": {}, "factor_profiles": {}},
    )
    scheme_api._seeded = False
    db_session.dispose()
    yield url
    db_session.dispose()


def _import(client, payload, **form):
    files = {
        "file": (
            "scheme.json",
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json",
        )
    }
    return client.post("/api/scheme/import", files=files, data=form)


# ---------------------------------------------------------------- ASPECT-1
def test_f601_create_duplicate_returns_409(client, db_env):
    r = client.post("/api/scheme", json={"name": "策略A"})
    assert r.status_code == 201, r.text
    assert r.json() == {"name": "策略A"}

    dup = client.post("/api/scheme", json={"name": "策略A"})
    assert dup.status_code == 409
    body = dup.json()
    assert body["error"]["code"] == "SCHEME_EXISTS"
    assert "策略A" in body["error"]["message"]


# ---------------------------------------------------------------- ASPECT-2
@pytest.mark.parametrize("bad_name", ["策略-A", "", "   ", "x" * 51])
def test_f601_name_validation_rejected(client, db_env, bad_name):
    r = client.post("/api/scheme", json={"name": bad_name})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "SCHEME_NAME_INVALID"


def test_f601_name_unicode_and_length_ok(client, db_env):
    r = client.post("/api/scheme", json={"name": "策略四_轮动均值回归"})
    assert r.status_code == 201, r.text
    r2 = client.post("/api/scheme", json={"name": "x" * 50})
    assert r2.status_code == 201, r2.text


# ---------------------------------------------------------------- ASPECT-4
def test_f601_list_contract_fields_exact(client, db_env):
    client.post("/api/scheme", json={
        "name": "策略B", "market": "futures", "direction": "short",
        "period": "日K", "factor_profile": "futures",
    })
    r = client.get("/api/scheme")
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list) and len(rows) == 1
    assert set(rows[0].keys()) == {"name", "factor_profile", "market", "direction", "period"}
    assert rows[0] == {
        "name": "策略B", "factor_profile": "futures",
        "market": "futures", "direction": "short", "period": "日K",
    }


# ---------------------------------------------------------------- ASPECT-5
def test_f601_put_version_cas(client, db_env):
    client.post("/api/scheme", json={"name": "策略C"})
    detail = client.get("/api/scheme/策略C").json()
    assert detail["version"] == 1

    ok = client.put("/api/scheme/策略C", json={"version": 1, "desc": "改一次"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["ok"] is True
    assert ok.json()["version"] == 2
    assert ok.json()["updated_factors"] == []

    stale = client.put("/api/scheme/策略C", json={"version": 1, "desc": "再改"})
    assert stale.status_code == 409
    body = stale.json()
    assert body["error"]["code"] == "VERSION_CONFLICT"
    assert "最新版本=2" in body["error"]["message"]
    assert stale.headers["X-Latest-Version"] == "2"

    # updated_factors 反映本次写入涉及的因子名
    r = client.put("/api/scheme/策略C", json={
        "version": 2, "config": {"factor_configs": {"obv_trend": {}, "rsi": {}}},
    })
    assert r.status_code == 200, r.text
    assert r.json()["updated_factors"] == ["obv_trend", "rsi"]


# ---------------------------------------------------------------- ASPECT-6
def test_f601_detail_404_and_delete(client, db_env):
    assert client.get("/api/scheme/不存在").status_code == 404
    assert client.get("/api/scheme/不存在").json()["error"]["code"] == "SCHEME_NOT_FOUND"

    client.post("/api/scheme", json={"name": "待删"})
    d = client.delete("/api/scheme/待删")
    assert d.status_code == 204 and d.content == b""
    assert client.delete("/api/scheme/待删").status_code == 404
    assert client.get("/api/scheme").json() == []


# ---------------------------------------------------------------- ASPECT-7
def test_f601_import_format1_library(client, db_env):
    payload = {
        "schemes": {
            "库_多单": {"config": {"active_factors": ["obv_trend"]},
                       "_meta": {"market": "futures", "direction": "long", "period": "日K"},
                       "factor_profile": "futures"},
            "含-连字符": {"config": {}},          # 非法名 → skipped
        },
        "factor_profiles": {"futures": {"active_factors": ["obv_trend"], "factor_configs": {}}},
    }
    r = _import(client, payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["imported"] == ["库_多单"]
    assert body["skipped"] and body["skipped"][0]["name"] == "含-连字符"

    rows = client.get("/api/scheme").json()
    assert [x["name"] for x in rows] == ["库_多单"]
    assert rows[0]["factor_profile"] == "futures"


def test_f601_import_format2_single_complete(client, db_env):
    payload = {
        "label": "显示名", "desc": "来自导出",
        "_meta": {"market": "stock", "direction": "long", "period": "周K"},
        "factor_profile": "stock",
        "config": {"active_factors": ["rsi"], "factor_configs": {"rsi": {}}},
    }
    r = _import(client, payload, scheme_name="导入的单方案")
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "导入的单方案"

    detail = client.get("/api/scheme/导入的单方案").json()
    assert detail["period"] == "周K" and detail["direction"] == "long"
    assert detail["desc"] == "来自导出"
    assert detail["config"]["active_factors"] == ["rsi"]


def test_f601_import_format2_auto_name(client, db_env):
    payload = {"_meta": {"market": "stock", "direction": "long", "period": "日K"},
               "config": {"active_factors": [], "factor_configs": {}}}
    r = _import(client, payload)
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "导入_stock_long_日K"

    # 第二次触发去重后缀
    r2 = _import(client, payload)
    assert r2.status_code == 200, r2.text
    assert r2.json()["name"] == "导入_stock_long_日K_1"


def test_f601_import_format3_flat(client, db_env):
    payload = {"active_factors": ["obv_trend"], "factor_configs": {"obv_trend": {}}}
    r = _import(client, payload, market="stock", period="日K", scheme_name="旧格式方案")
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "旧格式方案"
    assert client.get("/api/scheme/旧格式方案").json()["config"]["active_factors"] == ["obv_trend"]


def test_f601_import_invalid_format_422(client, db_env):
    r = _import(client, {"foo": "bar"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "SCHEME_IMPORT_INVALID"

    files = {"file": ("bad.json", b"{not json", "application/json")}
    r2 = client.post("/api/scheme/import", files=files)
    assert r2.status_code == 422
    assert r2.json()["error"]["code"] == "SCHEME_IMPORT_INVALID"


# ---------------------------------------------------------------- ASPECT-8
def test_f601_import_explicit_name_exists_409_no_residue(client, db_env):
    client.post("/api/scheme", json={"name": "已存在"})
    payload = {"_meta": {"market": "stock", "direction": "long", "period": "日K"},
               "config": {"active_factors": [], "factor_configs": {}}}
    r = _import(client, payload, scheme_name="已存在")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "SCHEME_EXISTS"
    # 原方案内容未被改动（无残留）
    assert client.get("/api/scheme/已存在").json()["version"] == 1


def test_f601_import_library_atomic_rollback(app, db_env, monkeypatch):
    """策略库多方案导入中途失败 → 整体回滚，不留半个方案。"""
    from fastapi.testclient import TestClient
    from server.db import repo

    calls = {"n": 0}
    real_create = repo.create_scheme

    def flaky_create(session, name, **fields):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("模拟第二只方案写库失败")
        return real_create(session, name, **fields)

    monkeypatch.setattr(repo, "create_scheme", flaky_create)
    payload = {"schemes": {
        "方案一": {"config": {}, "_meta": {"market": "stock", "direction": "long", "period": "日K"}},
        "方案二": {"config": {}, "_meta": {"market": "stock", "direction": "long", "period": "日K"}},
    }}
    # 默认 TestClient 会把未处理异常抛回测试；此处要的是「服务端 500 + 无残留」
    with TestClient(app, raise_server_exceptions=False) as c:
        r = _import(c, payload)
        assert r.status_code == 500
        # 方案一虽先写入，但整请求事务回滚 → 表里无任何残留
        assert c.get("/api/scheme").json() == []


# ---------------------------------------------------------------- ASPECT-9
def test_f601_export_json_stream_rfc5987(client, db_env):
    client.post("/api/scheme", json={
        "name": "策略A", "desc": "说明", "market": "stock",
        "direction": "long", "period": "日K", "factor_profile": "stock",
        "config": {"active_factors": ["rsi"]},
    })
    r = client.get("/api/scheme/策略A/export")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")

    cd = r.headers["content-disposition"]
    assert cd.startswith("attachment;")
    assert "filename*=UTF-8''" in cd
    assert unquote(cd.split("filename*=UTF-8''", 1)[1]) == "方案_策略A.json"

    payload = json.loads(r.content.decode("utf-8"))
    assert payload["name"] == "策略A"
    assert payload["_meta"] == {"market": "stock", "direction": "long", "period": "日K"}
    assert payload["factor_profile"] == "stock"
    assert payload["config"]["active_factors"] == ["rsi"]


def test_f601_export_404(client, db_env):
    assert client.get("/api/scheme/无此方案/export").status_code == 404


# ---------------------------------------------------------------- ASPECT-3
def test_f601_concurrent_profile_write_one_wins_one_loses(db_env):
    """并发写同一 `factor_profile` 一成一败（乐观锁）。

    两个写者**读到同一版本**后再各自提交 CAS，只有一个受影响行数为 1；
    另一个 → VersionConflict（服务端映射 409 并携带最新版本）。
    """
    from server.db import session as db_session
    from server.db import repo

    setup = db_session.session_scope()
    repo.create_profile(setup, "futures", {"active_factors": ["obv_trend"]})
    setup.commit()
    setup.close()

    barrier = threading.Barrier(2, timeout=10)
    results: list = []
    lock = threading.Lock()

    def writer(tag):
        s = db_session.session_scope()
        try:
            version = repo.get_profile(s, "futures")["version"]
            barrier.wait()  # 保证两个写者持同一版本进入 CAS
            try:
                repo.update_profile_cas(s, "futures", {"active_factors": [tag]}, version)
                s.commit()
                outcome = "ok"
            except repo.VersionConflict:
                s.rollback()
                outcome = "conflict"
        finally:
            s.close()
        with lock:
            results.append(outcome)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(writer, ["writer_A", "writer_B"]))

    assert sorted(results) == ["conflict", "ok"], results

    check = db_session.session_scope()
    final = repo.get_profile(check, "futures")
    check.close()
    # 只落一次写：版本单调 +1，且内容是胜出者的那份
    assert final["version"] == 2
    assert final["body"]["active_factors"] in (["writer_A"], ["writer_B"])


# ---------------------------------------------------------------- ASPECT-10
def test_f601_seed_from_engine_snapshot(client, db_env, monkeypatch):
    """首次 GET 把 engine 存量导入两表（幂等、空表才播种）。"""
    from server.adapters import engine_bridge
    from server.api import scheme as scheme_api

    monkeypatch.setattr(engine_bridge, "quant_model_snapshot", lambda: {
        "schemes": {
            "存量方案": {"config": {"active_factors": ["rsi"]},
                         "_meta": {"market": "stock", "direction": "long", "period": "日K"},
                         "factor_profile": "stock"},
        },
        "factor_profiles": {"stock": {"active_factors": ["rsi"], "factor_configs": {}}},
    })
    scheme_api._seeded = False

    rows = client.get("/api/scheme").json()
    assert [x["name"] for x in rows] == ["存量方案"]

    # 已非空 → 再次 GET 不再重复播种（幂等）
    scheme_api._seeded = False
    assert [x["name"] for x in client.get("/api/scheme").json()] == ["存量方案"]
