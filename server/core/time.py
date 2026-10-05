# -*- coding: utf-8 -*-
"""时区唯一入口（§2.13-2 / R-19）。

规则：服务端任何地方取当前时间都必须走本模块，禁止裸 datetime.now()
（裸调用取本机/容器本地时区，且返回 naive datetime，正是 R-19「下游时区未统一」
导致夜盘 K 线归错交易日、期货合约过期判定错的成因）。
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from server.settings import TIMEZONE

TZ = ZoneInfo(TIMEZONE)


def now() -> datetime:
    """当前时间，带 Asia/Shanghai 时区（aware）。"""
    return datetime.now(TZ)


def today() -> date:
    """当前交易日归属日（按 Asia/Shanghai）。"""
    return now().date()


def to_iso(dt: datetime) -> str:
    """转 ISO8601 字符串；naive datetime 按 Asia/Shanghai 解释后补全时区。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.isoformat()


def now_iso() -> str:
    return now().isoformat()
