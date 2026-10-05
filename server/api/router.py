# -*- coding: utf-8 -*-
"""路由聚合。

- `api_router`      → 挂 `/api`，承接 C3 §3.2 业务端点（health + F-201/F-202 market + F-203 kline + F-301 analyze + F-204 search + F-305 quote-only + F-105 backtest + F-401 diagnosis + F-501 scan + F-601 scheme）
- `internal_router` → 挂 `/api/_probe`，内部/非业务（本次仅 SSE 探针）
"""

from fastapi import APIRouter

from server.api import (
    ai,
    analyze,
    auth,
    backtest,
    diagnosis,
    emotion,
    health,
    kline,
    ledger,
    market,
    probe,
    quote,
    quote_only,
    scan,
    scheme,
    search,
    usage,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(market.router)
api_router.include_router(quote.router)
api_router.include_router(quote_only.router)
api_router.include_router(analyze.router)
api_router.include_router(ai.router)
api_router.include_router(kline.router)
api_router.include_router(search.router)
api_router.include_router(backtest.router)
api_router.include_router(diagnosis.router)
api_router.include_router(scan.router)
api_router.include_router(scheme.router)
api_router.include_router(usage.router)
api_router.include_router(ledger.router)
api_router.include_router(emotion.router)

internal_router = APIRouter()
internal_router.include_router(probe.router)
