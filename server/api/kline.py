# -*- coding: utf-8 -*-
"""K 线查询端点（F-203 / 能力 1）。

契约（app-architecture §3.2）：
  GET /api/kline?code&period&days → 200 `{code, period, bars[{t,o,h,l,c,v}]}` ∥ 404 / 422
错误体统一由 `server.core.errors` 产出（`{"error":{"code","message"}}`）。

本端点只做参数透传与错误码映射，业务判定在 `server/adapters/market/kline.py`。
"""

from typing import Optional

from fastapi import APIRouter, Query

from server.adapters.market import kline as kline_adapter

router = APIRouter(tags=["market"])


@router.get(
    "/kline",
    summary="K 线查询（F-203）",
    response_description="200=序列；404=未知代码/无数据；422=周期不受支持或根数超限",
)
def get_kline(
    code: str = Query(..., description="标的代码：sh600519 / sz000001 / rb0（主力连续）/ jd2609"),
    period: str = Query("日K", description="周期：日K / 周K / 60分钟 / 30分钟 / 15分钟 / 5分钟 / 1分钟"),
    days: Optional[int] = Query(
        None,
        description="返回根数。日K·周K 默认与上限 300；分钟默认与上限 1023",
    ),
) -> dict:
    return kline_adapter.fetch_kline(code=code, period=period, days=days)
