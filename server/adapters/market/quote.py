# -*- coding: utf-8 -*-
"""能力 1：实时行情（F-202）。

契约（app-architecture.md:323）：`GET /api/quote/{code}` → `{price, change_pct, volume, amount, time}`

口径（用户 2026-09-12 拍板）：
  B19 ① 代码格式不可解析 → **404**；源不可用 → **200 且字段为 `null`**；**不引入 `success` 字段**
  B20 ① `volume` **统一为「手」**（新浪实时源实测单位是「股」，需 /100；腾讯源本就是「手」），
        并以 `volume_unit: "手"` 显式声明
  附加字段（`name/source/open/prev_close/high/low/change`）沿用 B9 先例：F-202 的 H5 方案列要求
  「前端渲染结构复用」，而前端 `loadQuote`（`index.html:4754-4772`）消费这些字段。

字段位序（归一后，engine 已做双源兼容）：
  0=name 1=open 2=prev_close 3=price 4=high 5=low 6=买一 7=卖一 8=volume 9=amount 30=date 31=time

短 TTL 缓存（H5 方案列要求）：进程内按 code 缓存，默认 3 秒，防前端抖动重复打源站（R-08）。
不接 Redis（B5）。engine 零改动；engine 访问经 `engine_bridge`（T8）。
"""

import os
import re
import time

from server.adapters import engine_bridge
from server.core.errors import ApiError

# 归一后的字段位序
I_NAME, I_OPEN, I_PREV_CLOSE, I_PRICE = 0, 1, 2, 3
I_HIGH, I_LOW, I_VOLUME, I_AMOUNT = 4, 5, 8, 9
I_DATE, I_TIME = 30, 31

VOLUME_UNIT = "手"
CACHE_TTL_SECONDS = float(os.environ.get("MBULL_QUOTE_TTL_SECONDS", "3"))

_CODE_RE = re.compile(r"^(?:sh|sz|bj)\d{6}$", re.IGNORECASE)
_cache: dict = {}


def clear_cache() -> None:
    """清空行情缓存（配置热重载 / 测试用）。"""
    _cache.clear()


def _to_float(fields, idx):
    try:
        raw = fields[idx]
    except (IndexError, TypeError):
        return None
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_text(fields, idx):
    try:
        raw = fields[idx]
    except (IndexError, TypeError):
        return ""
    return str(raw).strip() if raw is not None else ""


def _parse(fields, source, code) -> dict:
    """把归一字段数组解析成响应体；任一项缺失即置 `null`（B19 不阻塞）。"""
    if not fields:
        return {
            "code": code, "name": None, "source": source,
            "price": None, "change": None, "change_pct": None,
            "open": None, "prev_close": None, "high": None, "low": None,
            "volume": None, "volume_unit": VOLUME_UNIT,
            "amount": None, "time": None,
        }

    price = _to_float(fields, I_PRICE)
    prev_close = _to_float(fields, I_PREV_CLOSE)
    volume = _to_float(fields, I_VOLUME)
    # B20：统一量纲为「手」——新浪实时源为「股」，腾讯源已是「手」
    if volume is not None and source == "新浪":
        volume = round(volume / 100, 2)

    change = round(price - prev_close, 2) if price is not None and prev_close is not None else None
    change_pct = (
        round((price - prev_close) / prev_close * 100, 2)
        if price is not None and prev_close not in (None, 0)
        else None
    )

    date_text, clock_text = _to_text(fields, I_DATE), _to_text(fields, I_TIME)
    time_text = f"{date_text} {clock_text}".strip() or None

    return {
        "code": code,
        "name": _to_text(fields, I_NAME) or None,
        "source": source,
        "price": price,
        "change": change,
        "change_pct": change_pct,
        "open": _to_float(fields, I_OPEN),
        "prev_close": prev_close,
        "high": _to_float(fields, I_HIGH),
        "low": _to_float(fields, I_LOW),
        "volume": volume,
        "volume_unit": VOLUME_UNIT,
        "amount": _to_float(fields, I_AMOUNT),
        "time": time_text,
    }


def fetch_quote(code: str) -> dict:
    code = (code or "").strip().lower()
    if not _CODE_RE.match(code):
        # B19：格式不可解析 → 404（格式合法但源无数据走 200+null）
        raise ApiError("NOT_FOUND", f"无法识别的标的代码：{code or '(空)'}", status_code=404)

    hit = _cache.get(code)
    now = time.monotonic()
    if hit is not None and now - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]

    fields, source = engine_bridge.realtime_quote(code)
    payload = _parse(fields, source, code)
    _cache[code] = (now, payload)
    return payload
