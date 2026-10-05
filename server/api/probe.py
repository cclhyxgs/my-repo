# -*- coding: utf-8 -*-
"""SSE 管道探针（B3 授权新增，`_` 前缀 = 内部/非业务）。

用途：让 T7 能端到端验收 `core/sse.py` 的六件（事件契约 / 回放钩子 / 终态补偿钩子 /
15s keepalive / 五响应头 / 单调 id），而不必等 F-403 落地。

边界（B3）：这是**唯一一次**为「验收需要」新增端点；
生产由环境变量 `MBULL_ENABLE_INTERNAL_PROBE=0` 关闭（nginx 亦可再屏蔽一层）。
本模块不含任何业务语义，接业务请走 §3.2 清单内的正式端点。
"""

from fastapi import APIRouter, HTTPException, Request

from server import settings
from server.core import sse

router = APIRouter(tags=["_internal"])


async def _probe_source():
    """两条契约内的帧，随后静默 —— 由 15s keepalive 接管。"""
    yield sse.SSEMessage(
        event="stage",
        data={"stage": "probe", "channel": "_probe", "contract": list(sse.SSE_EVENTS)},
        id=1,
    )
    yield sse.SSEMessage(
        event="progress",
        data={"current": 1, "total": 1, "channel": "_probe"},
        id=2,
    )


@router.get(
    "/sse",
    summary="SSE 管道探针（内部，生产屏蔽）",
    response_description="text/event-stream：stage(id=1) → progress(id=2) → 15s keepalive",
)
async def sse_probe(request: Request):
    if not settings.INTERNAL_PROBE_ENABLED:
        raise HTTPException(status_code=404, detail="internal probe disabled")
    return sse.sse_response(request, channel="_probe", messages=_probe_source())
