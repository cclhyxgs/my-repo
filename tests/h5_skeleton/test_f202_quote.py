# -*- coding: utf-8 -*-
"""F-202 实时行情 —— 验收测试。

断言来源（纪律 4）：
  feature-matrix.md:19 F-202「验收标准」列 =
    `GET /api/quote/sh600519` 返回 `price/change_pct/volume/amount/time` **且字段映射正确**
  业务规则列 = 按腾讯/新浪未公开字段位序解析（`fields[3]`=现价等）
  输出契约 = app-architecture.md:323 `{price, change_pct, volume, amount, time}`
  H5 方案列 = 「代理 + **短 TTL 缓存**」「**前端渲染结构复用**」

用户已拍板（2026-09-12）：
  B19 = ① 代码格式不可解析 → 404；源不可用 → 200 且字段为 `null`；**不引入 `success` 字段**
  B20 = ① `volume` **统一为「手」**（新浪源实测为「股」，需 /100）+ 显式 `volume_unit: "手"`
  B23 = ① F-203 遗留「日K 实时修补」**不纳入本条**（单列留痕）

字段位序（engine `RealtimeQuoteFetcher` 已把腾讯重排到与新浪同一布局，实测确认）：
  0=name 1=open 2=prev_close 3=price 4=high 5=low 6=买一 7=卖一 8=volume 9=amount 30=date 31=time
"""

import pytest

CONTRACT_FIELDS = ["price", "change_pct", "volume", "amount", "time"]


def _synthetic_fields(volume="3480142", date="2026-09-11", clock="15:34:59"):
    """按归一后位序构造 34 长度的合成字段（新浪口径：volume 单位=股）。"""
    f = [""] * 34
    f[0] = "贵州茅台"
    f[1] = "1285.150"
    f[2] = "1285.130"
    f[3] = "1275.160"
    f[4] = "1286.150"
    f[5] = "1263.010"
    f[6] = "1275.160"
    f[7] = "1276.000"
    f[8] = volume
    f[9] = "4430841445.000"
    f[30] = date
    f[31] = clock
    return f


@pytest.fixture(autouse=True)
def _clear_quote_cache():
    from server.adapters.market import quote as quote_adapter

    quote_adapter.clear_cache()
    yield
    quote_adapter.clear_cache()


# ================================================================
# 验收：契约字段与字段映射
# ================================================================
def test_qs1_real_quote_has_contract_fields(client):
    """QS1（验收逐字）：200 且含 `price/change_pct/volume/amount/time`，类型正确。"""
    r = client.get("/api/quote/sh600519")
    assert r.status_code == 200, r.text
    body = r.json()

    for key in CONTRACT_FIELDS:
        assert key in body, f"缺契约字段 {key}：{body}"
    assert isinstance(body["price"], (int, float)), body
    assert isinstance(body["change_pct"], (int, float)), body
    assert isinstance(body["volume"], (int, float)), body
    assert isinstance(body["amount"], (int, float)), body
    assert isinstance(body["time"], str), body
    assert body["code"] == "sh600519"


def test_qs2_field_mapping_is_correct(client, monkeypatch):
    """QS2（验收「字段映射正确」）：逐位校验映射与派生算式（合成字段，确定性）。"""
    from server.adapters import engine_bridge

    monkeypatch.setattr(engine_bridge, "realtime_quote", lambda code: (_synthetic_fields(), "新浪"))

    body = client.get("/api/quote/sh600519").json()
    assert body["name"] == "贵州茅台"        # fields[0]
    assert body["price"] == pytest.approx(1275.16)       # fields[3]
    assert body["open"] == pytest.approx(1285.15)        # fields[1]
    assert body["prev_close"] == pytest.approx(1285.13)  # fields[2]
    assert body["high"] == pytest.approx(1286.15)        # fields[4]
    assert body["low"] == pytest.approx(1263.01)         # fields[5]
    assert body["amount"] == pytest.approx(4430841445.0)  # fields[9]
    assert body["time"] == "2026-09-11 15:34:59"          # fields[30]+[31]
    assert body["source"] == "新浪"
    # 派生：change = price - prev_close；change_pct = (price-prev_close)/prev_close*100
    assert body["change"] == pytest.approx(1275.16 - 1285.13, abs=0.01)
    assert body["change_pct"] == pytest.approx((1275.16 - 1285.13) / 1285.13 * 100, abs=0.01)


def test_qs3_empty_time_fields_degrade_to_null(client, monkeypatch):
    """QS3：腾讯兜底时 `fields[30]/[31]` 可能为空 → `time` 为 `null` 而非崩溃/脏串。"""
    from server.adapters import engine_bridge

    monkeypatch.setattr(
        engine_bridge, "realtime_quote", lambda code: (_synthetic_fields(date="", clock=""), "腾讯")
    )
    body = client.get("/api/quote/sh600519").json()
    assert body["time"] is None, body["time"]


# ================================================================
# B19 失败语义
# ================================================================
def test_qs4a_unparsable_code_returns_404(client):
    """QS4a（B19）：代码格式不可解析 → 404（非 200）。"""
    r = client.get("/api/quote/notacode")
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "NOT_FOUND"


def test_qs4b_source_unavailable_returns_200_with_nulls(client, monkeypatch):
    """QS4b（B19）：源不可用 → **200**，字段为 `null`，不阻塞、不 5xx。"""
    from server.adapters import engine_bridge

    monkeypatch.setattr(engine_bridge, "realtime_quote", lambda code: (None, None))

    r = client.get("/api/quote/sh600519")
    assert r.status_code == 200, f"源不可用不得阻塞，实际 {r.status_code}"
    body = r.json()
    for key in ("price", "change_pct", "volume", "amount", "time"):
        assert body[key] is None, f"{key} 应为 null：{body}"
    assert "success" not in body, "B19 已定：不引入 success 字段"


# ================================================================
# B20 成交量单位
# ================================================================
def test_qs6a_sina_volume_converted_to_lots(client, monkeypatch):
    """QS6a（B20）：新浪源 volume 单位是**股** → 统一为**手**（/100）。"""
    from server.adapters import engine_bridge

    monkeypatch.setattr(
        engine_bridge, "realtime_quote", lambda code: (_synthetic_fields(volume="3480142"), "新浪")
    )
    body = client.get("/api/quote/sh600519").json()
    assert body["volume"] == pytest.approx(34801.42), body["volume"]
    assert body["volume_unit"] == "手", body


def test_qs6b_tencent_volume_kept_as_lots(client, monkeypatch):
    """QS6b（B20）：腾讯源 volume 本就是**手** → 原样返回，不得再除。"""
    from server.adapters import engine_bridge

    monkeypatch.setattr(
        engine_bridge, "realtime_quote", lambda code: (_synthetic_fields(volume="34801"), "腾讯")
    )
    body = client.get("/api/quote/sh600519").json()
    assert body["volume"] == pytest.approx(34801.0), body["volume"]
    assert body["volume_unit"] == "手"


# ================================================================
# 短 TTL 缓存（H5 方案列要求）
# ================================================================
def test_qs5a_cache_hit_calls_source_once(client, monkeypatch):
    """QS5a：「短 TTL 缓存」—— 连续两次请求只打源站 1 次。"""
    from server.adapters import engine_bridge

    calls = []

    def _counted(code):
        calls.append(code)
        return _synthetic_fields(), "新浪"

    monkeypatch.setattr(engine_bridge, "realtime_quote", _counted)
    first = client.get("/api/quote/sh600519").json()
    second = client.get("/api/quote/sh600519").json()

    assert len(calls) == 1, f"TTL 内重复请求打到源站 {len(calls)} 次"
    assert first == second, "缓存命中应返回一致结果"


def test_qs5b_ttl_expiry_refetches(client, monkeypatch):
    """QS5b：TTL 过期后重新取数（把 TTL 置 0 模拟过期）。"""
    from server.adapters import engine_bridge
    from server.adapters.market import quote as quote_adapter

    calls = []
    monkeypatch.setattr(engine_bridge, "realtime_quote",
                        lambda code: (calls.append(code), _synthetic_fields(), "新浪")[1:])
    monkeypatch.setattr(quote_adapter, "CACHE_TTL_SECONDS", 0.0)

    client.get("/api/quote/sh600519")
    client.get("/api/quote/sh600519")
    assert len(calls) == 2, f"TTL=0 时应每次取数，实际 {len(calls)} 次"


# ================================================================
# QS7 真实数据自洽性
# ================================================================
def test_qs7_real_values_self_consistent(client):
    """QS7：真实数据下 `price/prev_close/change/change_pct` 自洽（防符号/位序取反）。

    不设 skip：QS1 已要求真实端点返回数值，若此处拿不到值即说明取数或映射有问题。
    """
    body = client.get("/api/quote/sh600519").json()
    price, prev = body["price"], body["prev_close"]
    assert price is not None and prev is not None, f"真实取数未返回价格：{body}"

    assert price > 0 and prev > 0, body
    assert body["change"] == pytest.approx(price - prev, abs=0.02), body
    assert body["change_pct"] == pytest.approx((price - prev) / prev * 100, abs=0.02), body
    # 高低价夹住现价
    assert body["low"] <= price <= body["high"], body
