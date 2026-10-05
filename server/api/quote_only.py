# -*- coding: utf-8 -*-
"""仅行情端点（F-305 / 能力 1）。

契约（app-architecture.md:327）：`GET /api/quote-only?code&period&days` → `{kline[], quote{}}`
独立限流桶（按客户端 IP）→ 超限 **429 `RATE_LIMITED`** + `Retry-After`。
本路径不扣额度、不写报告、不走评分链。
"""

from typing import Optional

from fastapi import APIRouter, Query, Request

from server.adapters.market import quote_only as quote_only_adapter
from server.core import ratelimit
from server.core.errors import ApiError

router = APIRouter(tags=["market"])


@router.get(
    "/quote-only",
    summary="仅行情查询：K线 + 实时行情（F-305）",
    response_description="200：{kline[], quote{}}；429：触发独立限流桶；404/422：同 /api/kline 语义",
)
def quote_only(
    request: Request,
    code: str = Query(..., description="标的代码，如 sh600519（本路径仅支持 A 股）"),
    period: str = Query("日K", description="周期：日K / 周K / 分钟级（受数据源限制）"),
    days: Optional[int] = Query(None, description="K线根数，默认与上限：日K/周K 300、分钟 1023"),
) -> dict:
    client_ip = request.client.host if request.client else "unknown"
    allowed, retry_after = ratelimit.allow(
        "quote_only", client_ip, ratelimit.QUOTE_ONLY_LIMIT, ratelimit.QUOTE_ONLY_WINDOW
    )
    if not allowed:
        raise ApiError(
            "RATE_LIMITED",
            f"仅行情请求过于频繁（{ratelimit.QUOTE_ONLY_LIMIT} 次 / {int(ratelimit.QUOTE_ONLY_WINDOW)} 秒），请稍后重试",
            status_code=429,
            headers={"Retry-After": str(max(int(retry_after + 0.999), 1))},
        )
    return quote_only_adapter.fetch_quote_only(code=code, period=period, days=days)
