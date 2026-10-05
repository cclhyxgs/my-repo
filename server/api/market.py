# -*- coding: utf-8 -*-
"""顶部行情跑马灯端点（F-201 / 能力 1）。

契约（app-architecture.md:322）：`GET /api/market/indices` → `{indices[], advance, decline, ts}`
非交易时段：`change_pct` / `advance` / `decline` 均为 `null`（前端渲染 `--`，B15）。
源不可用：仍 200，失败项为 `null`，不阻塞。
"""

from fastapi import APIRouter, Query

from server.adapters.market import indices as indices_adapter

router = APIRouter(tags=["market"])


@router.get(
    "/market/indices",
    summary="行情跑马灯指数与涨跌家数（F-201）",
    response_description="200：{indices[], advance, decline, ts}；非交易时段各值为 null",
)
def market_indices(
    market: str = Query("stock", description="市场：stock（默认，A 股 4 指数）/ futures（期货 6 板块）"),
) -> dict:
    if market == "futures":
        return indices_adapter.fetch_futures_indices()
    if market != "stock":
        from server.core.errors import ApiError

        raise ApiError("INVALID_MARKET", f"未知 market：{market}（可选 stock/futures）", status_code=422)
    return indices_adapter.fetch_indices()
