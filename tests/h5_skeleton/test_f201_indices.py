# -*- coding: utf-8 -*-
"""F-201 行情跑马灯 —— 验收测试。

断言来源（纪律 4）：
  feature-matrix.md:18 F-201「验收标准」列 =
    ① `GET /api/market/indices` 返回 `{indices[], advance, decline}`
    ② **非交易时段指数字段返回 `--`**
  业务规则列 = 指数 + 涨跌家数每 30s 拉取；**源不可用显示 `--` 不阻塞**
  输出契约 = app-architecture.md:322 `{indices[], advance, decline, ts}`

用户已拍板（2026-09-12）：
  B15 = `--` 在接口中以 **`null`** 表示（`--` 是前端渲染结果；见 `index.html:7749` `pct === null ? '--' : ...`）
  B16 = 交易时段判定**服务端自建**（`server/core/market_session.py`，与监控扫描节拍解耦）
  B17 = **期货行情条不纳入本条**（随 F-302 决策）

⚠️ 实测背景（为什么必须有显式时段判定）：非交易时段源站（腾讯 qt）用「当前价 vs 昨收」，
   会返回**上一交易日涨跌幅而非 None**（周六实测 4 个指数全有值），故无法靠数据判空。
"""

from datetime import datetime

import pytest

from server.core.market_session import TZ, is_a_share_session, is_trading_day

INDEX_CODES = ["sh000001", "sz399001", "sz399006", "sh000300"]


# ================================================================
# 时段判定（B16 自建，确定性单测）
# ================================================================
@pytest.mark.parametrize("moment,expected", [
    # 交易日内的边界（含端点语义，与 ui/monitor.py 的 [start,end] 一致）
    (datetime(2026, 9, 11, 9, 29), False),
    (datetime(2026, 9, 11, 9, 30), True),
    (datetime(2026, 9, 11, 11, 30), True),
    (datetime(2026, 9, 11, 11, 31), False),
    (datetime(2026, 9, 11, 12, 0), False),
    (datetime(2026, 9, 11, 13, 0), True),
    (datetime(2026, 9, 11, 15, 0), True),
    (datetime(2026, 9, 11, 15, 1), False),
    (datetime(2026, 9, 11, 20, 25), False),   # 盘后
    (datetime(2026, 9, 11, 0, 30), False),    # 凌晨
    # 非交易日（无节假日日历 → 仅按工作日判定，已知局限）
    (datetime(2026, 9, 12, 10, 0), False),    # 周六
    (datetime(2026, 9, 13, 10, 0), False),    # 周日
])
def test_ms3_session_boundaries(moment, expected):
    """MS3：A 股时段判定边界（09:30/11:30/13:00/15:00 含端点；周末排除）。"""
    assert is_a_share_session(moment.replace(tzinfo=TZ)) is expected


def test_ms3b_trading_day_is_weekday_only():
    """MS3b：交易日判定 = 工作日（已知局限：无节假日日历）。"""
    assert is_trading_day(datetime(2026, 9, 11).date()) is True    # 周五
    assert is_trading_day(datetime(2026, 9, 12).date()) is False   # 周六
    assert is_trading_day(datetime(2026, 9, 13).date()) is False   # 周日


def test_ms3c_now_defaults_to_shanghai_clock():
    """MS3c：不传时刻时取 Asia/Shanghai 当前时间（§2.13-2）。"""
    assert isinstance(is_a_share_session(), bool)


# ================================================================
# 端点契约
# ================================================================
def test_ms1_contract_shape_and_index_list(client):
    """MS1（验收①）：200 且含 `indices/advance/decline`；`indices` 为 4 个约定指数。"""
    r = client.get("/api/market/indices")
    assert r.status_code == 200, r.text
    body = r.json()

    assert {"indices", "advance", "decline"} <= set(body), body
    assert isinstance(body["indices"], list)
    assert [it["code"] for it in body["indices"]] == INDEX_CODES
    for it in body["indices"]:
        assert isinstance(it["name"], str) and it["name"], it
    # 名称应取友好名（engine IndexFetcher.KNOWN），非裸代码
    assert body["indices"][0]["name"] == "上证指数", body["indices"][0]


def test_ms5_non_session_returns_nulls_without_source_calls(client, monkeypatch):
    """MS5（验收②）：非交易时段 → 全 `null`，且**不触源站**（省请求，R-08）。"""
    from server.adapters import engine_bridge
    from server.core import market_session

    calls = []
    monkeypatch.setattr(market_session, "is_a_share_session", lambda *a, **k: False)
    monkeypatch.setattr(engine_bridge, "index_daily_return",
                        lambda code: calls.append(("index", code)) or ("x", 0.01))
    monkeypatch.setattr(engine_bridge, "market_breadth",
                        lambda: calls.append(("breadth",)) or (1, 2, "t"))

    body = client.get("/api/market/indices").json()
    assert all(it["change_pct"] is None for it in body["indices"]), body
    assert body["advance"] is None and body["decline"] is None, body
    assert calls == [], f"非交易时段不应触发源站请求，实际 {calls}"


def test_ms4_in_session_returns_real_values(client, monkeypatch):
    """MS4（验收①②的反面）：交易时段内 → `change_pct` 为数值、涨跌家数为整数。"""
    from server.adapters import engine_bridge
    from server.core import market_session

    monkeypatch.setattr(market_session, "is_a_share_session", lambda *a, **k: True)
    monkeypatch.setattr(engine_bridge, "index_daily_return", lambda code: ("上证指数", -0.01176545))
    monkeypatch.setattr(engine_bridge, "market_breadth", lambda: (619, 4619, "沪深合计"))

    body = client.get("/api/market/indices").json()
    assert body["indices"][0]["change_pct"] == pytest.approx(-1.18), body["indices"][0]
    assert body["advance"] == 619 and body["decline"] == 4619


def test_ms6_source_failure_does_not_block(client, monkeypatch):
    """MS6（业务规则「源不可用显示 `--` 不阻塞」）：源抛异常 → 仍 200，该项 `null`。"""
    from server.adapters import engine_bridge
    from server.core import market_session

    monkeypatch.setattr(market_session, "is_a_share_session", lambda *a, **k: True)

    def _boom(code):
        raise RuntimeError("source down")

    monkeypatch.setattr(engine_bridge, "index_daily_return", _boom)
    monkeypatch.setattr(engine_bridge, "market_breadth", lambda: (_ for _ in ()).throw(RuntimeError("x")))

    r = client.get("/api/market/indices")
    assert r.status_code == 200, f"源不可用不得阻塞，实际 {r.status_code}"
    body = r.json()
    assert all(it["change_pct"] is None for it in body["indices"])
    assert body["advance"] is None and body["decline"] is None


def test_ms2_ts_is_shanghai_iso8601(client):
    """MS5b/§2.13-2：`ts` 为带 `+08:00` 的 ISO8601。"""
    import re

    ts = client.get("/api/market/indices").json()["ts"]
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?\+08:00$", ts), ts


def test_ms7_real_non_session_returns_nulls(client):
    """MS7（验收②真实数据）：**当前真实时刻**若非交易时段，端到端返回全 `null`。

    本条不 mock 时钟，直接用真实时间与真实源站 —— 若运行在交易时段内，
    则改断言「至少结构完整」，避免该条在盘中误报失败。
    """
    body = client.get("/api/market/indices").json()
    if is_a_share_session():
        # 盘中：真实取数，允许个别源失败，但字段形态必须正确
        for it in body["indices"]:
            assert it["change_pct"] is None or isinstance(it["change_pct"], (int, float)), it
    else:
        assert all(it["change_pct"] is None for it in body["indices"]), (
            f"非交易时段应全为 null（验收②），实际 {body['indices']}"
        )
        assert body["advance"] is None and body["decline"] is None


def test_ms8_futures_not_included(client):
    """MS8（B17 已拍板）：期货行情条不纳入本条 —— 不得出现板块/期货条目。"""
    body = client.get("/api/market/indices").json()
    for it in body["indices"]:
        assert it["code"] in INDEX_CODES, f"出现契约外条目：{it}"
        assert not it["code"].startswith("sector:"), f"不应含期货板块：{it}"
