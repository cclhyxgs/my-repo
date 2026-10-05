# -*- coding: utf-8 -*-
"""能力 1：K 线代理与缓存（F-203）。

契约（app-architecture §3.2）：`GET /api/kline?code&period&days`
  → 200 `{code, period, bars:[{t,o,h,l,c,v}]}` ∥ 404 / 422

本模块职责：
  1. **市场识别**：A股（`sh/sz/bj` + 6 位）与期货（`rb` / `rb0` / `jd2609`）。
     契约里 `/api/kline` 没有 `market_type` 参数，故由代码形态推定。
  2. **周期支持集校验**：A股取 engine 的 `{周期: 源}` 映射（默认 tdx 全周期可用；
     腾讯源分钟级为 None 不可用），期货固定 日K + 5 个分钟周期（无周K）。
     不支持 → 422（B8：不静默降级）。
  3. **条数上限**：日K/周K ≤ `MAX_DAYS_DAILY`(300)；分钟 ≤ `MAX_DAYS_MINUTE`(1023)
     —— 分钟上限与桌面 `engine.analyze_service` 的分钟抓取深度（1023，新浪 datalen 上限）同口径。
  4. **期货日K 夜盘聚合**：调 `engine.futures_night`（F-203 验收②）。

engine 零改动；所有 engine 访问经 `engine_bridge`（T8）。
"""

import re
from pathlib import Path

import pandas as pd

from server.adapters import engine_bridge
from server.core.errors import ApiError

# 期货支持周期：无周K（桌面 `ui_mockup/index.html:4256` 同口径）
FUTURES_PERIODS = ("日K", "60分钟", "30分钟", "15分钟", "5分钟", "1分钟")
MINUTE_PERIODS = ("60分钟", "30分钟", "15分钟", "5分钟", "1分钟")
MINUTE_TO_INT = {"1分钟": 1, "5分钟": 5, "15分钟": 15, "30分钟": 30, "60分钟": 60}

MAX_DAYS_DAILY = 300
MAX_DAYS_MINUTE = 1023

_STOCK_CODE_RE = re.compile(r"^(?:sh|sz|bj)\d{6}$", re.IGNORECASE)


def supported_periods(market: str):
    """当前生效数据源下该市场支持的周期列表（A股由 engine 源映射决定）。"""
    if market == "futures":
        return list(FUTURES_PERIODS)
    sources = engine_bridge.kline_period_sources()
    return [label for label, src in sources.items() if src]


def resolve_market(code: str):
    """由代码形态推定市场；无法识别返回 None。"""
    if _STOCK_CODE_RE.match(code):
        return "stock"
    if engine_bridge.futures_spec(code) is not None:
        return "futures"
    return None


def _normalize_days(period: str, days) -> int:
    limit = MAX_DAYS_MINUTE if period in MINUTE_PERIODS else MAX_DAYS_DAILY
    if days is None:
        return limit
    if not isinstance(days, int) or isinstance(days, bool) or days < 1:
        raise ApiError(
            "DAYS_OUT_OF_RANGE", f"days 必须为 ≥1 的整数，收到 {days!r}", status_code=422
        )
    if days > limit:
        raise ApiError(
            "DAYS_OUT_OF_RANGE",
            f"{period} 最多 {limit} 根，请求 {days} 根（日K/周K 上限 {MAX_DAYS_DAILY}、分钟上限 {MAX_DAYS_MINUTE}）",
            status_code=422,
        )
    return days


def _night_flag(row) -> bool:
    """判定该 bar 是否为夜盘 K 线。

    ⚠️ 必须显式排除 NaN：合并后的 DataFrame 中，原日K行的 `_is_night` 列是 NaN，
    而 `bool(float('nan')) == True`，若直接 `bool(row.get(...))` 会把**所有**K线
    误标为夜盘（engine 侧 `ui/web_api.py:1308-1309` 亦记录过同一陷阱）。
    """
    for key in ("_is_night", "is_night"):
        val = row.get(key, None)
        if val is None:
            continue
        try:
            if pd.isna(val):
                continue
        except (TypeError, ValueError):
            pass
        if bool(val):
            return True
    return False


def _bar(ts, row, is_minute: bool) -> dict:
    bar = {
        "t": pd.Timestamp(ts).strftime("%Y-%m-%d %H:%M" if is_minute else "%Y-%m-%d"),
        "o": float(row["open"]),
        "h": float(row["high"]),
        "l": float(row["low"]),
        "c": float(row["close"]),
        "v": float(row["volume"]) if "volume" in row and pd.notna(row["volume"]) else 0.0,
    }
    if _night_flag(row):
        bar["is_night"] = True
    return bar


def _to_bars(df: pd.DataFrame, is_minute: bool) -> list:
    if df is None or len(df) == 0:
        return []
    for col in ("open", "high", "low", "close"):
        if col not in df.columns:
            raise ApiError("NOT_FOUND", f"数据缺少必要列 {col}")
    return [_bar(r["trade_time"], r, is_minute) for _, r in df.iterrows()]


def _disk_store_mtime(code: str):
    """前复权日线 store 的 mtime（用于 `cached` 启发式判定）。"""
    try:
        path = Path(engine_bridge.qfq_daily_disk_path(code))
    except Exception:
        return None
    return path.stat().st_mtime if path.exists() else None


def fetch_kline(code: str, period: str, days) -> dict:
    code = (code or "").strip()
    if not code:
        raise ApiError("INVALID_CODE", "缺少代码参数 code", status_code=422)
    period = (period or "日K").strip()

    market = resolve_market(code)
    if market is None:
        raise ApiError("NOT_FOUND", f"未知标的代码：{code}", status_code=404)

    allowed = supported_periods(market)
    if period not in allowed:
        raise ApiError(
            "UNSUPPORTED_PERIOD",
            f"{period} 在当前数据源下不可用；{market} 支持：{'、'.join(allowed)}",
            status_code=422,
        )

    days = _normalize_days(period, days)
    is_minute = period in MINUTE_PERIODS

    if market == "futures":
        return _fetch_futures(code, period, days, is_minute)
    return _fetch_stock(code, period, days, is_minute)


def _fetch_stock(code: str, period: str, days: int, is_minute: bool) -> dict:
    before = _disk_store_mtime(code)
    df, err = engine_bridge.data_api().get_kline(code, period, days)
    after = _disk_store_mtime(code)

    if df is None or len(df) == 0:
        raise ApiError("NOT_FOUND", f"无 K 线数据：{code}（{err or '源站未返回数据'}）", status_code=404)

    source = engine_bridge.kline_period_sources().get(period) or "unknown"
    return {
        "code": code,
        "period": period,
        "days": days,
        "market": "stock",
        "is_futures": False,
        "source": source,
        # 启发式：磁盘 store 未被本次请求改写 → 视为命中缓存（仅日K/周K 落盘）
        "cached": bool(before is not None and before == after and not is_minute),
        "count": int(len(df)),
        "bars": _to_bars(df, is_minute),
    }


def _fetch_futures(code: str, period: str, days: int, is_minute: bool) -> dict:
    spec = engine_bridge.futures_spec(code)
    if spec is None:
        raise ApiError("NOT_FOUND", f"未知期货品种或合约：{code}", status_code=404)

    if is_minute:
        df = engine_bridge.fetch_futures_minute(spec.symbol, spec.secid, MINUTE_TO_INT[period], days)
    else:
        df = engine_bridge.fetch_futures_daily(spec.symbol, spec.secid, days)
        if df is not None and len(df) > 0:
            # F-203 验收②：日K 夜盘归入下一交易日（逻辑在 engine.futures_night）
            df = engine_bridge.merge_night_session(df, spec.symbol, spec.secid)
            # 夜盘 bar 是 merge 时**额外追加**的一根（下一交易日夜盘），绕过了
            # engine 侧 `_clip_kline_days` 的截尾 → days=300 会变成 301 根，
            # 违反 F-203 验收①「返回根数 ≤ days」。故合并后必须再收口一次。
            if len(df) > days:
                df = df.iloc[-days:].reset_index(drop=True)

    if df is None or len(df) == 0:
        raise ApiError("NOT_FOUND", f"无 K 线数据：{code}", status_code=404)

    return {
        "code": code,
        "period": period,
        "days": days,
        "market": "futures",
        "is_futures": True,
        "source": engine_bridge.futures_kline_source(),
        "cached": False,  # 期货走独立磁盘缓存（futures_*），不适用 qfq 日线 store 判定
        "count": int(len(df)),
        "bars": _to_bars(df, is_minute),
    }
