# -*- coding: utf-8 -*-
"""单只分析端点（F-301 / 能力 2）。

契约（app-architecture.md:337）：
  `POST /api/analyze` `{code, market_type:'stock'|'futures', k_type:'日K', scheme?}`
  → `{reportData, report_text, discipline, entry_tier}`

失败语义：不可解析代码 404；`<30 条无法评分` / `未配置方案` / `分钟级源不支持` / `期货未开放` 422；
行情源不可用 503。
"""

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from server.adapters import analysis
from server.core import auth as s_auth

router = APIRouter(tags=["analysis"])


class AnalyzeRequest(BaseModel):
    code: str = Field(..., description="标的代码，如 sh600519")
    market_type: str = Field("stock", description="市场类型：stock / futures（futures 归属 F-302）")
    k_type: str = Field("日K", description="周期：日K / 周K / 分钟级（受数据源限制）")
    scheme: Optional[str] = Field(None, description="强制使用的方案名；为空则按周期自动匹配")


@router.post(
    "/analyze",
    summary="单只量化分析（F-301）",
    response_description="200：{reportData, report_text, discipline, entry_tier}；"
                         "422：无法评分/未配置方案/周期不支持/期货未开放；404：代码不可解析；503：行情源不可用",
)
def analyze(req: AnalyzeRequest, _auth: dict = Depends(s_auth.require_license)) -> dict:
    return analysis.analyze(
        code=req.code,
        market_type=req.market_type,
        k_type=req.k_type,
        scheme=req.scheme,
    )
