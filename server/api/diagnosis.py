# -*- coding: utf-8 -*-
"""诊断端点（F-401 启动 / F-403 进度与取消，能力 3）。

契约（app-architecture.md:345-348）：
  POST  `/api/diagnosis`             → `{task_id}`
  GET   `/api/diagnosis/{id}`        → `{status, summary{5组计数}, total}`
  GET   `/api/diagnosis/{id}/events` → SSE（progress/done/error 事件流）
  POST  `/api/diagnosis/{id}/cancel` → `{status:'cancelled'}`
"""

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from server.adapters import diagnosis as diag_adapter
from server.core import auth as s_auth, sse, task_store
from server.core.task_store import TERMINAL_STATUSES
from server.worker.executor import get_executor

router = APIRouter(tags=["diagnosis"])


class DiagnosisRequest(BaseModel):
    text: str = Field(..., description="自选列表文本（每行一只，逗号分隔可选字段）")
    market: str = Field("stock", description="市场：stock / futures")
    direction: Optional[str] = Field(None, description="方向过滤：long / short / None（不过滤）")
    scheme_name: Optional[str] = Field(None, description="指定方案名（None = 按市场+方向解析）")
    k_type: str = Field("日K", description="分析周期")


def _get_task_or_404(task_id: str) -> dict:
    task = task_store.store.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    return task


@router.post(
    "/diagnosis",
    summary="诊断启动（F-401）",
    response_description="200：{task_id}；空文本/无有效标的/非法 market 422",
)
def start_diagnosis(body: DiagnosisRequest, _auth: dict = Depends(s_auth.require_license)) -> dict:
    task_id = diag_adapter.start_diagnosis(
        body.text,
        market=body.market,
        direction=body.direction,
        scheme_name=body.scheme_name,
        k_type=body.k_type,
    )
    return {"task_id": task_id}


@router.get(
    "/diagnosis/{task_id}",
    summary="诊断进度（F-403）",
    response_description="200：{status, summary, total}",
)
def diagnosis_progress(task_id: str) -> dict:
    task = _get_task_or_404(task_id)
    return {
        "status": task["status"],
        "total": task.get("total", 0),
        "current": task.get("current", 0),
        "summary": task.get("summary"),
        "results": task.get("results"),
    }


@router.get(
    "/diagnosis/{task_id}/events",
    summary="诊断进度 SSE（F-403）",
    response_description="text/event-stream：progress → done/error",
)
async def diagnosis_events(task_id: str, request: Request):
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

    return sse.sse_response(request, channel=f"diagnosis:{task_id}", messages=messages())


@router.post(
    "/diagnosis/{task_id}/cancel",
    summary="取消诊断（F-403，协作式 ≤1 只）",
    response_description="200：{task_id, status:'cancelling'}；任务不存在/已终态 4xx",
)
def cancel_diagnosis(task_id: str) -> dict:
    task = _get_task_or_404(task_id)
    if task["status"] in TERMINAL_STATUSES:
        raise HTTPException(status_code=409, detail=f"任务已终态（{task['status']}），无法取消")
    get_executor().cancel(task_id)
    return {"task_id": task_id, "status": "cancelling"}
