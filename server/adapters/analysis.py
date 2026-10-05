# -*- coding: utf-8 -*-
"""能力 2：A 股分析流程编排（F-301）。

契约（app-architecture.md:337）：
  `POST /api/analyze` `{code, market_type, k_type, scheme}`
  → `{reportData, report_text, discipline, entry_tier}`

实现要点
--------
分析核心**不在本层**：`engine.analyze_service.analyze_stock()`（2026-09-12 由
`ui/web_api.py::WebAPI.analyze` 主体上移，B27 方案 1），桌面与 H5 共用同一实现。
本层只做：参数校验 → 调用 → engine 错误映射为统一错误体 → 补齐契约顶层字段。

错误映射（B26 + 本次新增）：
  无法评分（K线 <30 条）      → 422 `INSUFFICIENT_DATA`
  分钟级在当前数据源不可用     → 422 `UNSUPPORTED_PERIOD`（与 F-203 同码）
  行情源不可用                → 503 `SOURCE_UNAVAILABLE`
  其余（配置不完整/未配置方案）→ 422 `ANALYZE_REJECTED`
  期货                        → 422 `NOT_IMPLEMENTED`（F-302 才开放）
"""

import re

from server.adapters import engine_bridge
from server.adapters.market.kline import FUTURES_PERIODS
from server.core import time as srv_time
from server.core.errors import ApiError

_STOCK_CODE_RE = re.compile(r"^(?:sh|sz|bj)\d{6}$", re.IGNORECASE)
VALID_MARKET_TYPES = ("stock", "futures")


def analyze(code: str, market_type: str = "stock", k_type: str = "日K", scheme=None) -> dict:
    """执行一次 A 股分析，返回 §3.2 契约结构。"""
    code = (code or "").strip()
    if not code:
        raise ApiError("INVALID_CODE", "缺少代码参数 code", status_code=422)

    market_type = (market_type or "stock").strip().lower()
    if market_type not in VALID_MARKET_TYPES:
        raise ApiError(
            "INVALID_MARKET_TYPE",
            f"未知 market_type：{market_type}（可选 {'/'.join(VALID_MARKET_TYPES)}）",
            status_code=422,
        )
    # 授权强制已在 F-1404 迁移到端点依赖 `require_license`（账号维度，401/403）。
    if market_type == "futures":
        return _analyze_futures(code, k_type=k_type, scheme=scheme)

    if not _STOCK_CODE_RE.match(code):
        raise ApiError("NOT_FOUND", f"无法识别的标的代码：{code}", status_code=404)

    k_type = (k_type or "日K").strip()
    result = engine_bridge.analyze_stock(code, k_type=k_type, scheme_name=(scheme or None))
    _raise_if_engine_error(result)

    report_data = result.get("reportData") or {}
    # §3.2 要求顶层 `entry_tier`：实现里入场档位的真实字段是 `reportData.status`
    # （`entry_tier` 该名只存在于扫描设计文档，当前实现无此键 —— 见 F-301 汇报的 B28 说明）
    return {
        "reportData": report_data,
        "report_text": result.get("report_text", "") or "",
        "entry_tier": report_data.get("status"),
        "entry_action": report_data.get("entry_action"),
        "env": report_data.get("env"),
        # 纪律落册属 F-1103（P2），本条先返回空占位，避免前端缺键
        "discipline": {},
        "used_scheme_name": result.get("used_scheme_name"),
        "used_scheme_period": result.get("used_scheme_period"),
        "scheme_summary": result.get("scheme_summary"),
    }


def _analyze_futures(code: str, k_type: str, scheme) -> dict:
    """期货分析分支（F-302）。

    周期集沿用 F-203 已定（日K + 5 个分钟，**无周K**）；过期合约由 engine 判定后
    映射为 422 `CONTRACT_EXPIRED`；`now` 显式注入 **Asia/Shanghai**（§2.13-2 ——
    F-302 平台差异列明确要求"过期判定依赖服务端时区"）。
    """
    if k_type not in FUTURES_PERIODS:
        raise ApiError(
            "UNSUPPORTED_PERIOD",
            f"期货不支持 {k_type}；支持：{'、'.join(FUTURES_PERIODS)}",
            status_code=422,
        )
    if engine_bridge.futures_spec(code) is None:
        raise ApiError("NOT_FOUND", f"未知期货品种或合约：{code}", status_code=404)

    result = engine_bridge.analyze_futures(
        code, k_type=k_type, scheme_name=(scheme or None), now=srv_time.now()
    )
    _raise_if_engine_error(result)

    report_data = result.get("reportData") or {}
    return {
        "reportData": report_data,
        "report_text": result.get("report_text", "") or "",
        "entry_tier": report_data.get("status"),
        "entry_action": report_data.get("entry_action"),
        "env": report_data.get("env"),
        "discipline": {},  # 纪律落册属 F-1103（P2）
        "used_scheme_name": result.get("used_scheme_name"),
        "used_scheme_period": result.get("used_scheme_period"),
        "used_scheme_market": "futures",
        "used_scheme_direction": result.get("used_scheme_direction"),
        "futures_contract": report_data.get("futures_contract"),
        "scheme_summary": result.get("scheme_summary"),
    }


def _raise_if_engine_error(result) -> None:
    """把 engine 侧 `{'error': ...}` 映射为统一错误体（不吞：原文透出）。"""
    if not isinstance(result, dict) or not result.get("error"):
        return
    message = str(result["error"])

    if "已到期/已交割" in message:
        raise ApiError("CONTRACT_EXPIRED", message, status_code=422)
    if "无法评分" in message:
        raise ApiError("INSUFFICIENT_DATA", message, status_code=422)
    if "无法做" in message and "级分析" in message:
        raise ApiError("UNSUPPORTED_PERIOD", message, status_code=422)
    if "无法获取行情数据" in message:
        raise ApiError("SOURCE_UNAVAILABLE", message, status_code=503)
    raise ApiError("ANALYZE_REJECTED", message, status_code=422)
