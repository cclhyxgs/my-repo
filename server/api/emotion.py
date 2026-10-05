# -*- coding: utf-8 -*-
"""情绪账本 API（能力 6，F-1201/1202/1203/1204）。

契约（app-architecture.md §3.2 能力 6）逐字对齐：
  GET  /api/emotion     `?cursor&limit` → `{records[], cursor}`（默认 limit=200，F-1203「加载更多」）
  POST /api/emotion     `{code, action, tag, note, op_price, ...}` → `{id}`（**op_price 服务端锁定**）
  DELETE /api/emotion/{id} → `204`（前端二次确认）
  GET  /api/emotion/summary → `{total, sum_diff, top_tag, ...}`（F-1202）
  GET  /api/emotion/tags    → 标签常量单一来源（F-1204）
"""

from typing import Optional

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from server.core.errors import ApiError
from server.db import emotion_repo
from server.db.session import session_scope

router = APIRouter(tags=["emotion"])


class EmotionBody(BaseModel):
    code: str
    name: Optional[str] = ""
    action: str = "buy"
    tag: Optional[str] = "其他"
    note: Optional[str] = ""
    op_price: Optional[float] = 0.0
    latest_price: Optional[float] = None


@router.get("/emotion", summary="情绪记录列表（F-1203）")
def list_emotion(cursor: Optional[str] = None, limit: int = 200) -> dict:
    with session_scope() as session:
        records = emotion_repo.list_records(session, limit=limit, cursor=cursor)
    return {"ok": True, "records": records,
            "cursor": records[-1]["id"] if records else None}


@router.post("/emotion", summary="记一笔情绪操作（F-1201，op_price 锁定）")
def create_emotion(body: EmotionBody) -> dict:
    with session_scope() as session:
        rec = emotion_repo.add_record(
            session,
            code=body.code.strip(), name=body.name or "", action=body.action,
            tag=body.tag or "其他", note=body.note or "",
            op_price=body.op_price, latest_price=body.latest_price,
        )
        if "error" in rec:
            raise ApiError("EMOTION_INVALID", rec["error"], status_code=422)
        session.commit()
    return {"ok": True, "id": rec["id"], "record": rec}


@router.delete("/emotion/{record_id}", summary="删除情绪记录（F-1203，前端二次确认）", status_code=204)
def delete_emotion(record_id: str) -> Response:
    with session_scope() as session:
        if not emotion_repo.delete_record(session, record_id):
            raise ApiError("EMOTION_NOT_FOUND", f"记录不存在：{record_id}", status_code=404)
        session.commit()
    return Response(status_code=204)


@router.get("/emotion/summary", summary="情绪仪表盘（F-1202）")
def emotion_summary() -> dict:
    with session_scope() as session:
        data = emotion_repo.summary(session)
    return {"ok": True, **data}


@router.get("/emotion/tags", summary="情绪标签常量表（F-1204 单一来源）")
def emotion_tags() -> dict:
    return emotion_repo.emotion_tags()