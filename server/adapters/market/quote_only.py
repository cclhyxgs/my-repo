# -*- coding: utf-8 -*-
"""能力 1：仅行情查询（F-305）。

契约（app-architecture.md:327）：
  `GET /api/quote-only?code&period&days` → `{kline[], quote{}}`
  **独立限流桶、不扣额度、不写报告**

与桌面对齐：桌面 `doQueryQuoteOnly()`（`index.html:4962`）= `get_kline` + `get_realtime_quote`，
**不调 `analyze`** → 不扣额度、不写报告、不走评分链。本模块就是这两者的组合，
取数逻辑完全复用 F-203（kline）与 F-202（quote），不另立一套。

范围（B35）：只做 A 股 —— `/api/quote` 目前未含期货（F-202 按 B14/B17 未纳入），
故期货代码在此显式 422 `NOT_IMPLEMENTED`，避免"K线能出、行情 404"的半吊子状态。
"""

from server.adapters import engine_bridge
from server.adapters.market import kline as kline_adapter
from server.adapters.market import quote as quote_adapter
from server.core.errors import ApiError

QUOTE_KEYS = ("price", "change_pct", "volume", "volume_unit", "amount", "time")


def fetch_quote_only(code: str, period: str = "日K", days=None) -> dict:
    """一次请求同时返回 K 线与实时行情（不含任何报告/评分字段）。"""
    code = (code or "").strip()
    if not code:
        raise ApiError("INVALID_CODE", "缺少代码参数 code", status_code=422)

    # B35：期货行情未交付 → 显式拒绝，而不是"K线有值、行情 404"
    if kline_adapter.resolve_market(code) == "futures":
        raise ApiError(
            "NOT_IMPLEMENTED",
            "仅行情暂不支持期货（/api/quote 尚未含期货）；期货分析请走 /api/analyze",
            status_code=422,
        )

    kline = kline_adapter.fetch_kline(code, period, days)      # 404/422 沿用 F-203 语义
    quote = quote_adapter.fetch_quote(code)                    # 200+null 沿用 F-202 语义

    return {
        "code": kline["code"],
        "period": kline["period"],
        "days": kline["days"],
        "count": kline["count"],
        "kline": kline["bars"],
        "quote": {k: quote.get(k) for k in QUOTE_KEYS} | {"name": quote.get("name"), "source": quote.get("source")},
        # 显式声明本路径不含报告（前端据此不写 #reportContainer）
        "report": None,
    }
