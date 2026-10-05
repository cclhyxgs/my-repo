# -*- coding: utf-8 -*-
"""实时行情端点（F-202 / 能力 1）。

契约（app-architecture.md:323）：`GET /api/quote/{code}` → `{price, change_pct, volume, amount, time}`
失败语义（B19）：格式不可解析 → 404；源不可用 → 200 且字段为 `null`（不阻塞）。
"""

from fastapi import APIRouter

from server.adapters.market import quote as quote_adapter

router = APIRouter(tags=["market"])


@router.get(
    "/quote/{code}",
    summary="单只实时行情（F-202）",
    response_description="200：{price, change_pct, volume, amount, time} + 附加字段；源不可用时各值为 null",
)
def get_quote(code: str) -> dict:
    return quote_adapter.fetch_quote(code)
