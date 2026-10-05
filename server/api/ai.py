# -*- coding: utf-8 -*-
"""AI 解读 API（叠加在既有引擎结果之上的解读层）。

端点：
  GET  /api/ai/config             → {configured, model, enabled}（不透出 key）
  PUT  /api/ai/config             → 保存 {api_key, model}
  POST /api/ai/analyze            → 个股分析白话解读 + 触发条件建议
  POST /api/ai/diagnosis          → 单只诊断白话解读 + 触发条件建议
  POST /api/ai/scan               → 扫描概览 + 筛选条件建议
  POST /api/ai/ledger             → 情绪/纪律账本复盘 + 改善建议
  POST /api/ai/scheme-conditions  → 自然语言 → 方案配置条件

实现内核收敛在 server.ai.service（纯函数），本层只做"引擎产物 → 服务函数"的薄封装，
与桌面 WebAPI 桥接共用同一份逻辑，避免两套前端各写一份调用代码。
"""

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from server.adapters import analysis as analysis_adapter
from server.ai import config as ai_config
from server.ai import service as ai_service
from server.core.errors import ApiError
from server.db import ledger_repo
from server.db.session import session_scope

router = APIRouter(tags=["ai"])


# ---------------------------------------------------------------- 配置
@router.get("/ai/config", summary="AI 配置快照（不透出 key）")
def get_config() -> dict:
    return {"ok": True, **ai_config.public_ai_config()}


class AiConfigBody(BaseModel):
    api_key: str = ""
    model: Optional[str] = None


@router.put("/ai/config", summary="保存 AI 配置")
def put_config(body: AiConfigBody) -> dict:
    if body.model and body.model not in ai_config.VALID_MODELS:
        raise ApiError("INVALID_MODEL", f"未知模型：{body.model}", status_code=422)
    data = ai_config.save_ai_config(api_key=body.api_key, model=body.model)
    return {"ok": True, "configured": data["enabled"], "model": data["model"]}


# ---------------------------------------------------------------- 个股分析
class AnalyzeBody(BaseModel):
    code: str
    market_type: str = "stock"
    k_type: str = "日K"
    scheme: Optional[str] = None


@router.post("/ai/analyze", summary="个股分析 AI 解读")
def ai_analyze(body: AnalyzeBody) -> dict:
    # 容错：纯 6 位数字自动补 sh/sz/bj 前缀
    code = (body.code or "").strip().lower()
    if code and code.isdigit() and len(code) == 6:
        code = ("sh" if code[0] in "56" else "sz") + code
    # 复用现有分析适配层获取真实结果，不改其逻辑
    result = analysis_adapter.analyze(
        code, market_type=body.market_type, k_type=body.k_type, scheme=body.scheme
    )
    return ai_service.ai_analyze_result(
        result.get("reportData") or {},
        result.get("report_text") or "",
        result.get("scheme_summary"),
    )


# ---------------------------------------------------------------- 自选诊断单只
class DiagnosisBody(BaseModel):
    item: dict = Field(default_factory=dict)


@router.post("/ai/diagnosis", summary="单只诊断 AI 解读")
def ai_diagnosis(body: DiagnosisBody) -> dict:
    return ai_service.ai_diagnosis_result(body.item)


# ---------------------------------------------------------------- 全市场/自选扫描
class ScanBody(BaseModel):
    items: list = Field(default_factory=list)
    filters: Optional[dict] = None


@router.post("/ai/diagnosis-batch", summary="自选诊断批量 AI 解读（持仓体检视角）")
def ai_diagnosis_batch(body: ScanBody) -> dict:
    return ai_service.ai_diagnosis_batch_result(body.items)


@router.post("/ai/scan", summary="扫描概览 AI 解读")
def ai_scan(body: ScanBody) -> dict:
    return ai_service.ai_scan_result(body.items, body.filters)


# ---------------------------------------------------------------- 对话式筛选（AI 出 DSL，后端执行）
class ScanDslBody(BaseModel):
    text: str
    items: Optional[list] = None


@router.post("/ai/scan-dsl", summary="自然语言 → 筛选 DSL（不返回股票列表）")
def ai_scan_dsl(body: ScanDslBody) -> dict:
    return ai_service.ai_scan_dsl(body.text, body.items)


class ScanFilterBody(BaseModel):
    items: list = Field(default_factory=list)
    dsl: dict = Field(default_factory=dict)


@router.post("/ai/scan-filter", summary="在结果集上执行筛选 DSL")
def ai_scan_filter(body: ScanFilterBody) -> dict:
    return ai_service.ai_scan_filter(body.items, body.dsl)


# ---------------------------------------------------------------- 情绪账本
class LedgerBody(BaseModel):
    market: str = "all"


@router.post("/ai/ledger", summary="纪律/情绪账本 AI 复盘")
def ai_ledger(body: LedgerBody) -> dict:
    with session_scope() as session:
        # list_signals 已返回 _decorate 装饰行（含 conclusion/emotion_diff）
        signals = ledger_repo.list_signals(session, market=body.market, limit=50)
        summary = ledger_repo.ledger_summary(session, market=body.market)
        cooldown = ledger_repo.cooldown_status(session)
    return ai_service.ai_ledger_result(signals, summary, cooldown)


# ---------------------------------------------------------------- 自然语言 → 方案配置
class SchemeConditionsBody(BaseModel):
    text: str


@router.post("/ai/scheme-conditions", summary="自然语言 → 方案配置条件")
def ai_scheme_conditions(body: SchemeConditionsBody) -> dict:
    return ai_service.ai_scheme_conditions_text(body.text)