# -*- coding: utf-8 -*-
"""用法模式端点（F-604 / F-1601 只读子集）。

契约（app-architecture.md §3.2 能力 5）：`GET /api/usage` → `{ok, usage_mode, is_basic}`。
默认 basic（F-604 明文）；完整切换（含前端门控 + 其他消费面）归 F-1601（P3）。
"""

from fastapi import APIRouter
from pydantic import BaseModel

from server.adapters import usage as usage_adapter
from server.core.errors import ApiError

router = APIRouter(tags=["usage"])


class UsageUpdate(BaseModel):
    usage_mode: str


@router.get("/usage", summary="读取用法模式（F-604 / F-1601 只读子集）")
def read_usage() -> dict:
    mode = usage_adapter.get_usage()
    return {"ok": True, "usage_mode": mode, "is_basic": mode == "basic"}


@router.put("/usage", summary="切换用法模式（F-1601 完整化前置在 P3；此处先落只读子集的写面）")
def write_usage(payload: UsageUpdate) -> dict:
    mode = payload.usage_mode
    if mode not in usage_adapter.VALID:
        raise ApiError("INVALID_USAGE_MODE", f"非法用法模式：{mode}（可选 {'/'.join(usage_adapter.VALID)}）", 422)
    usage_adapter.set_usage(mode)
    return {"ok": True, "usage_mode": mode, "is_basic": mode == "basic"}