# -*- coding: utf-8 -*-
"""扫描端点（F-501~506，能力 3+11）。

契约（app-architecture.md §3.2 扫描段）：
  POST  `/api/scan`                       → `{task_id, reused}`（分钟级 k_type 4xx）
  GET   `/api/scan/{id}`                  → `{status, total, current, pct, market, reused}`
  GET   `/api/scan/{id}/results`          → `{total, offset, limit, items[剥离tech_snapshot]}`（F-503）
  GET   `/api/scan/{id}/events`           → SSE（progress/done/error）
  POST  `/api/scan/{id}/cancel`           → `{status:'cancelling'}`
  POST  `/api/scan/{id}/pause`            → `{status:'paused'}`
  POST  `/api/scan/{id}/resume`           → `{status:'running'}`
  GET   `/api/scan/{id}/export`           → text/csv（utf-8-sig，F-506）
  GET   `/api/sectors?market=&task_id=`  → 板块聚合（F-505）
  GET   `/api/scan/cache?market=`         → `{exists, stale, saved_at}`（F-504）
"""

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from server.adapters import scan as scan_adapter
from server.core import auth as s_auth, sse, task_store, time as srv_time
from server.core.task_store import TERMINAL_STATUSES
from server.worker.executor import get_executor

router = APIRouter(tags=["scan"])


class ScanRequest(BaseModel):
    market: str = Field("stock", description="市场：stock / futures")
    direction: Optional[str] = Field(None, description="方向：long / short / None")
    k_type: str = Field("日K", description="周期（仅日K/周K）")
    scheme_name: Optional[str] = Field(None, description="指定方案名")
    use_cache: bool = Field(True, description="24h 内缓存可复用（F-504）")


def _get_task_or_404(task_id: str) -> dict:
    task = task_store.store.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    return task


@router.post(
    "/scan",
    summary="扫描启动（F-501）",
    response_description="200：{task_id, reused}；分钟级 k_type / 非法 market 422",
)
def start_scan(body: ScanRequest, _auth: dict = Depends(s_auth.require_license)) -> dict:
    return scan_adapter.start_scan(
        market=body.market,
        direction=body.direction,
        k_type=body.k_type,
        scheme_name=body.scheme_name,
        use_cache=body.use_cache,
    )


@router.get(
    "/scan/cache",
    summary="扫描缓存状态（F-504）",
    response_description="200：{exists, stale, saved_at}",
)
def scan_cache_status(market: str = "stock") -> dict:
    cache = scan_adapter.get_scan_cache(market)
    return {"market": market, "exists": cache["exists"], "stale": cache["stale"], "saved_at": cache["saved_at"]}


@router.get(
    "/scan/{task_id}",
    summary="扫描进度（F-502）",
    response_description="200：{status, total, current, pct, market, reused}",
)
def scan_progress(task_id: str) -> dict:
    task = _get_task_or_404(task_id)
    return {
        "status": task["status"],
        "total": task.get("total_stocks", task.get("total", 0)),
        "current": task.get("current", 0),
        "pct": task.get("pct", 0.0),
        "market": task.get("payload", {}).get("market", "stock") if "payload" in task else "stock",
        "reused": bool((task.get("payload") or {}).get("reused")),
        "message": task.get("message", ""),
    }


@router.get(
    "/scan/{task_id}/results",
    summary="扫描结果分页（F-503）",
    response_description="200：{total, offset, limit, items}（不含 tech_snapshot）",
)
def scan_results(
    task_id: str,
    offset: int = 0,
    limit: int = 200,
    rating: Optional[int] = None,
    sort: str = "final_score",
    order: str = "desc",
) -> dict:
    return scan_adapter.read_results(
        task_id, offset=offset, limit=limit, rating=rating, sort=sort, order=order
    )


@router.get(
    "/scan/{task_id}/events",
    summary="扫描进度 SSE（F-502/F-503）",
    response_description="text/event-stream：progress → done/error",
)
async def scan_events(task_id: str, request: Request):
    _get_task_or_404(task_id)
    last_event_id = sse.parse_last_event_id(request)

    async def messages():
        last_id = last_event_id or 0
        while True:
            task = task_store.store.get(task_id)
            if task is None:
                return
            for e in task_store.store.events_since(task_id, last_id):
                last_id = e["id"]
                yield sse.SSEMessage(event=e["event"], data=e["data"], id=e["id"])
            if task["status"] in TERMINAL_STATUSES:
                return
            await asyncio.sleep(0.1)

    return sse.sse_response(request, channel=f"scan:{task_id}", messages=messages())


@router.post(
    "/scan/{task_id}/cancel",
    summary="取消扫描（协作式 ≤1 片）",
    response_description="200：{task_id, status:'cancelling'}",
)
def cancel_scan(task_id: str) -> dict:
    task = _get_task_or_404(task_id)
    if task["status"] in TERMINAL_STATUSES:
        raise HTTPException(status_code=409, detail=f"任务已终态（{task['status']}），无法取消")
    get_executor().cancel(task_id)
    return {"task_id": task_id, "status": "cancelling"}


@router.post(
    "/scan/{task_id}/pause",
    summary="暂停扫描（F-506）",
    response_description="200：{task_id, status:'paused'}",
)
def pause_scan(task_id: str) -> dict:
    task = _get_task_or_404(task_id)
    if task["status"] in TERMINAL_STATUSES:
        raise HTTPException(status_code=409, detail=f"任务已终态（{task['status']}），无法暂停")
    get_executor().pause(task_id)
    return {"task_id": task_id, "status": "paused"}


@router.post(
    "/scan/{task_id}/resume",
    summary="继续扫描（F-506）",
    response_description="200：{task_id, status:'running'}",
)
def resume_scan(task_id: str) -> dict:
    task = _get_task_or_404(task_id)
    if task["status"] in TERMINAL_STATUSES:
        raise HTTPException(status_code=409, detail=f"任务已终态（{task['status']}），无法继续")
    get_executor().resume(task_id)
    return {"task_id": task_id, "status": "running"}


@router.get(
    "/scan/{task_id}/export",
    summary="导出扫描 CSV（F-506）",
    response_description="text/csv：utf-8-sig（EF BB BF）",
)
def export_scan(task_id: str) -> Response:
    from urllib.parse import quote

    task = _get_task_or_404(task_id)
    results = list(task.get("results") or [])
    csv_bytes = scan_adapter.build_scan_csv(results)
    market = task.get("payload", {}).get("market", "stock")
    cn_name = scan_adapter.export_filename(market)
    ts = srv_time.now().strftime("%Y%m%d_%H%M%S")
    ascii_name = f"scan_{market}_{ts}.csv"
    # RFC 5987：中文文件名走 filename*=UTF-8''（pct 编码，纯 ASCII，避免 latin-1 头编码错误）
    disp = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(cn_name)}"
    return Response(
        content=csv_bytes,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": disp},
    )


@router.get(
    "/sectors",
    summary="板块强度聚合（F-505）",
    response_description="200：{sectors:[...]}（仅含 ≥3 成分股板块）",
)
def sectors(market: str = "stock", task_id: Optional[str] = None) -> dict:
    if task_id:
        task = _get_task_or_404(task_id)
        results = list(task.get("results") or [])
    else:
        cache = scan_adapter.get_scan_cache(market)
        results = cache.get("results", [])
    return {"market": market, "sectors": scan_adapter.aggregate_sectors(results)}
