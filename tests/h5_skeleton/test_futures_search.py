# -*- coding: utf-8 -*-
"""期货搜索（B14 清理）验收测试。

端点：`GET /api/search/futures?q=` → 裸数组
`[{name, code, symbol, exchange, multiplier, contract_month}]`。
引擎侧 `engine.futures_search.search_futures` 已由桌面 `search_futures` 上移（AST 等价），
本测试只验端点契约与结构（真实品种池，离线确定，不依赖网络）。
"""


def test_fsf1_symbol_search(client):
    """品种代码：rb → 螺纹钢（主力连续，contract_month=None）。"""
    r = client.get("/api/search/futures", params={"q": "rb"})
    assert r.status_code == 200
    items = r.json()
    assert any(x["symbol"] == "rb" and x["name"] == "螺纹钢" for x in items)


def test_fsf2_name_search(client):
    """品种中文名：螺纹钢 → rb。"""
    r = client.get("/api/search/futures", params={"q": "螺纹钢"})
    items = r.json()
    assert any(x["symbol"] == "rb" for x in items)


def test_fsf3_specific_contract(client):
    """具体合约：jd2609 → 单条，contract_month='2609'。"""
    r = client.get("/api/search/futures", params={"q": "jd2609"})
    items = r.json()
    assert len(items) == 1
    assert items[0]["code"] == "jd2609"
    assert items[0]["symbol"] == "jd"
    assert items[0]["contract_month"] == "2609"


def test_fsf4_chinese_month_name(client):
    """中文名 + 月份紧贴：鸡蛋2609 → jd2609。"""
    r = client.get("/api/search/futures", params={"q": "鸡蛋2609"})
    items = r.json()
    assert items and items[0]["symbol"] == "jd"
    assert items[0]["contract_month"] == "2609"


def test_fsf5_empty_returns_all(client):
    """空查询 → 全部品种（≤30）。"""
    r = client.get("/api/search/futures", params={"q": ""})
    items = r.json()
    assert 0 < len(items) <= 30


def test_fsf6_field_shape(client):
    """元素键严格 ⊆ 契约六字段。"""
    r = client.get("/api/search/futures", params={"q": "rb"})
    allowed = {"name", "code", "symbol", "exchange", "multiplier", "contract_month"}
    for x in r.json():
        assert set(x.keys()) <= allowed, x
