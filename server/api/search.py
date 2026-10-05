# -*- coding: utf-8 -*-
"""股票搜索端点（F-204 / 能力 1）。

契约（app-architecture.md:326）：`GET /api/search?q=` → **裸数组** `[{code, name}]` ≤20 条。
注意：§3.2 对本题定义的是**裸数组**（非包装对象），故不加 `count`/`q` 等附加字段（B9 仅适用于 F-203）。

匹配口径与上限在 `server/adapters/market/search.py`。
"""

from fastapi import APIRouter, Query

from server.adapters.market import search as search_adapter

router = APIRouter(tags=["market"])


@router.get(
    "/search",
    summary="股票搜索（F-204）",
    response_description="≤20 条的 [{code, name}] 数组；空查询与无匹配均返回 []",
)
def search(
    q: str = Query("", description="名称或代码，如 茅台 / sh600519 / 600519"),
) -> list:
    return search_adapter.search_stocks(q)


@router.get(
    "/search/futures",
    summary="期货品种搜索（B14）",
    response_description="期货品种数组 [{name, code, symbol, exchange, multiplier, contract_month}]；空查询返回全部品种（≤30）",
)
def search_futures(
    q: str = Query("", description="品种代码/名称，如 rb / 螺纹钢 / jd2609 / 鸡蛋2609"),
) -> list:
    return search_adapter.search_futures(q)
