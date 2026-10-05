# -*- coding: utf-8 -*-
"""期货行情条（B17 清理）验收测试。

端点：`GET /api/market/indices?market=futures` → `{indices[], advance, decline, ts}`，
其中 `advance`/`decline` 恒 `null`（期货不展示涨跌家数）；indices 元素含 `member_count`。
引擎侧 `engine.futures_indices.get_futures_indices` 已上移（AST 等价），本测试用
monkeypatch 注入 mock 数据验服务端 adapter 映射（不碰真实期货逐合约取数）。
"""

from server.adapters import engine_bridge


def test_mf1_futures_returns_sectors(client, monkeypatch):
    """market=futures → 板块数组 + advance/decline 恒 null + ts。"""
    monkeypatch.setattr(
        engine_bridge,
        "futures_indices",
        lambda: {
            "indices": [
                {"name": "黑色系", "code": "sector:黑色系", "change_pct": 1.5, "member_count": 3},
                {"name": "有色金属", "code": "sector:有色金属", "change_pct": -0.8, "member_count": 2},
            ],
            "up": None,
            "down": None,
            "source": "futures",
        },
    )

    r = client.get("/api/market/indices", params={"market": "futures"})
    assert r.status_code == 200
    body = r.json()
    assert body["advance"] is None
    assert body["decline"] is None
    assert body["ts"]
    assert [x["name"] for x in body["indices"]] == ["黑色系", "有色金属"]
    assert body["indices"][0]["member_count"] == 3
    assert body["indices"][0]["change_pct"] == 1.5


def test_mf2_source_unavailable_degrades(client, monkeypatch):
    """源异常 → 仍 200，indices 降级为空，advance/decline 仍 null（不阻塞）。"""
    def _boom():
        raise RuntimeError("源不可用")

    monkeypatch.setattr(engine_bridge, "futures_indices", _boom)
    r = client.get("/api/market/indices", params={"market": "futures"})
    assert r.status_code == 200
    body = r.json()
    assert body["indices"] == []
    assert body["advance"] is None
    assert body["decline"] is None


def test_mf3_invalid_market_422(client):
    """非法 market → 422。"""
    r = client.get("/api/market/indices", params={"market": "crypto"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_MARKET"


def test_mf4_stock_default_unchanged(client):
    """market 缺省 = stock，返回 A 股 4 指数（回归 F-201，行为不变）。"""
    r = client.get("/api/market/indices")
    assert r.status_code == 200
    codes = [x["code"] for x in r.json()["indices"]]
    assert codes == ["sh000001", "sz399001", "sz399006", "sh000300"]
