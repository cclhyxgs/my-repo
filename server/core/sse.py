# -*- coding: utf-8 -*-
"""SSE 管道原语（骨架期，**只建原语不接业务**）。

依据 migration-plan.md:70 能力 3 SSE 骨架四要素：
  ① 统一事件契约 `progress / log / stage / done / error`
  ② `Last-Event-ID` 回放
  ③ 终态补偿
  ④ 15s keepalive
外加 §2.13-3 / R-12 要求的响应头（`X-Accel-Buffering: no` 等五个）。

约定（补充约束 2）：回放钩子与终态补偿钩子本次返回空/占位即可，
业务接入留给 F-105 / F-401 / F-403 / F-501 / F-502 / F-701 / F-703。
本模块不得 import engine。
"""

import asyncio
import json
from dataclasses import dataclass
from typing import Any, AsyncIterable, AsyncIterator, Callable, Iterable, Optional

from fastapi import Request
from starlette.responses import StreamingResponse

from server.settings import TIMEZONE  # noqa: F401  （保持时区口径显式可见）

# ④ 15s keepalive —— 与 R-12（中间层缓冲导致事件成批到达/超时）配对
KEEPALIVE_SECONDS: float = 15.0
KEEPALIVE_FRAME: bytes = b": keepalive\n\n"

# ① 统一事件契约
SSE_EVENTS = ("progress", "log", "stage", "done", "error")
TERMINAL_EVENTS = ("done", "error")

# 响应头五件（§2.13-3 + R-12）
# Content-Encoding: identity 用于阻断中间层对事件流做二次压缩缓冲
SSE_HEADERS: dict = {
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
    "Content-Encoding": "identity",
}


@dataclass
class SSEMessage:
    """单条 SSE 事件。`id` 由调用方保证单调递增（T7a 断言单调性）。"""

    event: str
    data: Any
    id: Optional[int] = None
    retry: Optional[int] = None

    def __post_init__(self) -> None:
        if self.event not in SSE_EVENTS:
            raise ValueError(f"事件名 {self.event!r} 不在契约 {SSE_EVENTS} 内")

    def encode(self) -> bytes:
        lines = []
        if self.id is not None:
            lines.append(f"id: {self.id}")
        lines.append(f"event: {self.event}")
        if self.retry is not None:
            lines.append(f"retry: {self.retry}")
        payload = self.data if isinstance(self.data, str) else json.dumps(self.data, ensure_ascii=False)
        for line in (payload.splitlines() or [""]):
            lines.append(f"data: {line}")
        return ("\n".join(lines) + "\n\n").encode("utf-8")


def parse_last_event_id(request: Optional[Request]) -> Optional[int]:
    """② `Last-Event-ID` 回放：优先请求头，其次 `?last_event_id=` 查询参数。"""
    if request is None:
        return None
    raw = request.headers.get("last-event-id") or request.query_params.get("last_event_id")
    if raw is None:
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


async def sse_event_stream(
    messages: Optional[AsyncIterable] = None,
    *,
    keepalive: float = KEEPALIVE_SECONDS,
    replay: Optional[Callable[[Optional[int]], Iterable]] = None,
    terminal: Optional[Callable[[], Optional[SSEMessage]]] = None,
    last_event_id: Optional[int] = None,
) -> AsyncIterator[bytes]:
    """SSE 字节流生成器。

    - `messages=None`：只发 keepalive（探针用）。
    - keepalive 用 `asyncio.shield` 保护上游 `__anext__`，超时**不取消**上游任务，
      否则异步生成器会被 CancelledError 终结，后续只剩一条心跳。
    """
    # ③ 终态补偿：连接建立即查终态（占位钩子返回 None 即继续正常流程）
    if terminal is not None:
        done = terminal()
        if done is not None:
            yield done.encode()
            return

    # ② 回放钩子：本次占位返回空序列
    if replay is not None:
        for item in replay(last_event_id):
            yield item.encode()

    if messages is None:
        while True:
            yield KEEPALIVE_FRAME
            await asyncio.sleep(keepalive)

    iterator = messages.__aiter__()
    pending: Optional[asyncio.Future] = None
    while True:
        if pending is None:
            pending = asyncio.ensure_future(iterator.__anext__())
        try:
            msg = await asyncio.wait_for(asyncio.shield(pending), timeout=keepalive)
        except asyncio.TimeoutError:
            yield KEEPALIVE_FRAME
            continue
        except StopAsyncIteration:
            if pending is not None and not pending.done():
                pending.cancel()
            return
        pending = None
        yield msg.encode()


def sse_response(
    request: Optional[Request],
    *,
    channel: str,
    messages: Optional[AsyncIterable] = None,
    keepalive: float = KEEPALIVE_SECONDS,
    replay: Optional[Callable[[Optional[int]], Iterable]] = None,
    terminal: Optional[Callable[[], Optional[SSEMessage]]] = None,
) -> StreamingResponse:
    """把生成器包成 StreamingResponse，并挂上五件响应头。"""
    last_event_id = parse_last_event_id(request)
    stream = sse_event_stream(
        messages,
        keepalive=keepalive,
        replay=replay,
        terminal=terminal,
        last_event_id=last_event_id,
    )
    headers = dict(SSE_HEADERS)
    headers["X-MBull-Channel"] = channel
    if last_event_id is not None:
        headers["X-MBull-Resume-From"] = str(last_event_id)
    return StreamingResponse(stream, headers=headers, status_code=200)
