# -*- coding: utf-8 -*-
"""纪律账本 API（能力 6，F-1101/1104/1105）。

契约（app-architecture.md §3.2 能力 6）逐字对齐：
  GET  /api/ledger            `?market&cursor&limit` → `{ok, signals[], cooldowns[], cursor}`
  GET  /api/ledger/summary    → 4 指标（纪律盈亏/执行数/情绪化差合计/未按纪律数）+ 冷却
  POST /api/signal/{id}/execute → `{ok, emotion_diff}`（乐观更新，失败回滚 404）
  POST /api/ledger/prices     `{prices:{code:price}}` → 批量刷新现价（F-1101 单次请求）

冷却全局跨市场（F-1101）；未执行信号涨→记亏、跌→记盈（F-1104）由
`ledger_repo._decorate` 的 emotion_diff 口径保证。
"""

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from server.core.errors import ApiError
from server.db import ledger_repo
from server.db.session import session_scope

router = APIRouter(tags=["ledger"])


class ExecuteBody(BaseModel):
    executed: bool
    exec_price: Optional[float] = None
    exec_ratio: Optional[float] = None


class PricesBody(BaseModel):
    prices: dict = Field(default_factory=dict)


@router.get("/ledger", summary="纪律账本：信号 + 冷却（F-1101）")
def list_ledger(market: str = "all", cursor: Optional[str] = None,
                limit: int = 200) -> dict:
    with session_scope() as session:
        signals = ledger_repo.list_signals(session, market=market, limit=limit, cursor=cursor)
        cooldown = ledger_repo.cooldown_status(session)
    return {"ok": True, "signals": signals, "cooldowns": [cooldown], "cursor": signals[-1]["id"] if signals else None}


@router.get("/ledger/summary", summary="账本统计 4 指标（F-1104）")
def ledger_summary(market: str = "all") -> dict:
    with session_scope() as session:
        data = ledger_repo.ledger_summary(session, market=market)
    return {"ok": True, **data}


@router.post("/signal/{signal_id}/execute", summary="登记执行（F-1105 乐观更新）")
def set_execution(signal_id: str, body: ExecuteBody) -> dict:
    with session_scope() as session:
        res = ledger_repo.set_execution(session, signal_id, body.executed,
                                        body.exec_price, body.exec_ratio)
        if res is None:
            raise ApiError("SIGNAL_NOT_FOUND", f"信号不存在：{signal_id}", status_code=404)
        session.commit()
    return res


@router.post("/ledger/prices", summary="按代码批量刷新现价（F-1101/F-1201 单次请求）")
def refresh_prices(body: PricesBody) -> dict:
    n_signal = 0
    with session_scope() as session:
        from server.db import emotion_repo
        for code, price in (body.prices or {}).items():
            n_signal += ledger_repo.update_price_by_code(session, code, price)
            emotion_repo.update_price_by_code(session, code, price)
        session.commit()
    return {"ok": True, "updated": n_signal}