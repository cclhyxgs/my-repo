# -*- coding: utf-8 -*-
"""H5 接入桌面壳（分支 A）—— FastAPI 同源托管 SPA 的回归测试。

覆盖 server/main.py 的 _mount_spa：
  - 根路径 / 返回 SPA index.html
  - /assets/* 静态资源 200 + 正确 MIME
  - 根级静态文件（非 /assets）正确返回，不被 SPA 吞
  - 前端路由刷新（如 /diagnosis）走 SPA fallback 回 index.html
  - /api 未知路径保留 404 JSON（不被 catch-all 吞）
  - /api 正常端点（/api/health）仍可用
默认（SERVE_SPA 关）时这些路由不应存在 —— 由 settings 开关保证，本测试只验证开启态。
"""

from pathlib import Path


def test_spa_serving_and_api_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("MBULL_DISABLE_LEGACY_MIGRATE", "1")
    qsd = tmp_path / "qsd"
    qsd.mkdir()
    monkeypatch.setenv("QUANT_SYSTEM_DIR", str(qsd))

    # 构造临时 SPA 产物
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(
        '<!DOCTYPE html><html lang="zh-CN"><body><div id="app"></div></body></html>'
    )
    assets = dist / "assets"
    assets.mkdir()
    (assets / "main.js").write_text("console.log('mbull');")
    (dist / "favicon.ico").write_text("ICO")  # 根级静态文件（非 /assets）

    import server.settings as s

    monkeypatch.setattr(s, "SERVE_SPA", True)
    monkeypatch.setattr(s, "SPA_DIST", dist)

    from server.main import create_app
    from starlette.testclient import TestClient

    app = create_app()
    with TestClient(app) as client:
        # 1) 根路径返回 SPA
        r = client.get("/")
        assert r.status_code == 200
        assert "<div id=\"app\">" in r.text

        # 2) /assets 静态资源
        r = client.get("/assets/main.js")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/javascript")

        # 3) 根级静态文件（非 /assets）正确返回，不被 SPA fallback 吞
        r = client.get("/favicon.ico")
        assert r.status_code == 200
        assert r.text == "ICO"

        # 4) 前端路由刷新 -> SPA fallback
        r = client.get("/diagnosis")
        assert r.status_code == 200
        assert "<div id=\"app\">" in r.text

        # 5) /api 未知路径保留 404 JSON（不被 catch-all 吞）
        r = client.get("/api/unknown")
        assert r.status_code == 404
        assert r.headers["content-type"].startswith("application/json")

        # 6) /api 正常端点仍可用
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


def test_spa_off_by_default_keeps_api_only(tmp_path, monkeypatch):
    """默认 SERVE_SPA=False 时，根路径不应被 SPA 路由接管（保持纯 API）。"""
    monkeypatch.setenv("MBULL_DISABLE_LEGACY_MIGRATE", "1")
    qsd = tmp_path / "qsd"
    qsd.mkdir()
    monkeypatch.setenv("QUANT_SYSTEM_DIR", str(qsd))

    import server.settings as s

    monkeypatch.setattr(s, "SERVE_SPA", False)

    from server.main import create_app
    from starlette.testclient import TestClient

    app = create_app()
    with TestClient(app) as client:
        # 纯 API 模式下根路径不应返回 SPA（这里返回 404，因无 SPA 路由也无页面）
        r = client.get("/")
        assert r.status_code == 404
        # 但 API 正常
        assert client.get("/api/health").status_code == 200
