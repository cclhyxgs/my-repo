# -*- coding: utf-8 -*-
"""F-305 仅行情查询 —— 验收测试。

断言来源（纪律 4）：
  feature-matrix.md:26 F-305「验收标准」列 =
    走 `quote_only` 路径返回 **kline+quote** 且不写 `#reportContainer`；
    **该路径独立限流、不扣额度**
  业务规则列 = 只取 K 线+行情，**不扣额度、不写报告、不走评分链**
  H5 方案列   = `/api/kline` + `/api/quote` 组合，**独立于 `/api/analyze`**（无额度/无方案/无因子）
  平台差异列  = **需独立限流桶，避免被分析请求挤占**
  输出契约 = app-architecture.md:327 `{kline[], quote{}}`

用户已拍板（2026-09-12）：
  B33 = ① 按**客户端 IP** 建 `quote_only` 桶（默认 30 次/分钟），**本次不挂 analyze 限流**
  B34 = ① 超限 → **429 `RATE_LIMITED`** + `Retry-After`
  B35 = ① 期货**不纳入**本路径（显式 422 `NOT_IMPLEMENTED`）
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SERVER_DIR = ROOT / "server"


@pytest.fixture(autouse=True)
def _clean_rate_bucket():
    from server.core import ratelimit

    ratelimit.clear()
    yield
    ratelimit.clear()


# ================================================================
# QO1：验收主断言
# ================================================================
def test_qo1_returns_kline_and_quote_together(client):
    """QO1（验收）：200 且同时含 `kline[]`（非空）与 `quote{}`（含契约字段）。"""
    r = client.get("/api/quote-only", params={"code": "sh600519", "period": "日K", "days": 60})
    assert r.status_code == 200, r.text
    body = r.json()

    assert isinstance(body["kline"], list) and body["kline"], "kline 为空"
    for bar in body["kline"][:3]:
        assert {"t", "o", "h", "l", "c", "v"} <= set(bar), f"bar 形状不符 F-203：{bar}"

    quote = body["quote"]
    for key in ("price", "change_pct", "volume", "amount", "time", "volume_unit"):
        assert key in quote, f"quote 缺 {key}：{quote}"


def test_qo2_no_report_and_no_scoring_chain(client, monkeypatch):
    """QO2（业务规则：不写报告、不走评分链）：响应无报告字段，且 `TradingPipeline.execute` 未被调用。"""
    from engine.trading_pipeline import TradingPipeline

    calls = []

    def _spy(*a, **k):
        calls.append(1)
        raise AssertionError("仅行情路径不得触发评分链")

    monkeypatch.setattr(TradingPipeline, "execute", staticmethod(_spy))

    body = client.get(
        "/api/quote-only", params={"code": "sh600519", "period": "日K", "days": 60}
    ).json()
    assert not calls, '评分链被触发（违反「仅行情不评分」）'
    for key in ("reportData", "report_text", "entry_tier", "discipline"):
        assert key not in body, f"响应不应含报告字段 {key}：{sorted(body)}"
    assert body["report"] is None, "本路径显式声明 report=None（前端据此不写 #reportContainer）"


def test_qo3_no_quota_consumption_in_server_code():
    """QO3（业务规则：不扣额度）：服务端代码**不应出现**任何额度扣减调用。"""
    offenders = []
    for path in sorted(SERVER_DIR.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "consume_use" in text or "can_use" in text:
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f"服务端出现额度/授权调用（应仅在 UI 侧与授权条线）：{offenders}"


# ================================================================
# QO4/QO5：独立限流桶
# ================================================================
def test_qo4_rate_limit_is_independent_of_other_endpoints(client, monkeypatch):
    """QO4（平台差异列）：quote-only 超限 → 429；**此时 `/api/quote`、`/api/kline` 仍 200**（桶独立）。"""
    from server.adapters.market import quote_only as qo_adapter
    from server.core import ratelimit

    monkeypatch.setattr(ratelimit, "QUOTE_ONLY_LIMIT", 2)
    monkeypatch.setattr(
        qo_adapter,
        "fetch_quote_only",
        lambda code, period="日K", days=None: {"code": code, "period": period, "kline": [], "quote": {}, "report": None},
    )

    params = {"code": "sh600519", "period": "日K", "days": 60}
    assert client.get("/api/quote-only", params=params).status_code == 200
    assert client.get("/api/quote-only", params=params).status_code == 200

    limited = client.get("/api/quote-only", params=params)
    assert limited.status_code == 429, limited.text
    assert limited.json()["error"]["code"] == "RATE_LIMITED"
    assert int(limited.headers["Retry-After"]) > 0, limited.headers

    # 关键：其它行情端点不受影响（证明是"独立桶"，不是全局限流）
    assert client.get("/api/quote/sh600519").status_code == 200
    assert client.get("/api/kline", params={"code": "sh600519", "period": "日K", "days": 30}).status_code == 200


def test_qo5_limit_boundary(client, monkeypatch):
    """QO5：限额边界 —— 第 N 次放行、第 N+1 次 429。"""
    from server.adapters.market import quote_only as qo_adapter
    from server.core import ratelimit

    monkeypatch.setattr(ratelimit, "QUOTE_ONLY_LIMIT", 3)
    monkeypatch.setattr(
        qo_adapter,
        "fetch_quote_only",
        lambda code, period="日K", days=None: {"code": code, "kline": [], "quote": {}, "report": None},
    )

    params = {"code": "sh600519"}
    codes = [client.get("/api/quote-only", params=params).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429], codes


# ================================================================
# QO6：参数语义复用 F-203
# ================================================================
def test_qo6_parameter_semantics_reuse_f203(client):
    """QO6：周期/根数/代码校验沿用 F-203 语义（422 / 422 / 404）。"""
    assert client.get(
        "/api/quote-only", params={"code": "sh600519", "period": "60m"}
    ).status_code == 422
    assert client.get(
        "/api/quote-only", params={"code": "sh600519", "period": "日K", "days": 500}
    ).status_code == 422
    r = client.get("/api/quote-only", params={"code": "sh999999", "period": "日K"})
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "NOT_FOUND"


def test_qo7_futures_not_supported(client):
    """QO7（B35）：期货 → 422 `NOT_IMPLEMENTED`（不做"K线有值、行情 404"的半吊子状态）。"""
    r = client.get("/api/quote-only", params={"code": "rb0", "period": "日K"})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "NOT_IMPLEMENTED"
