# -*- coding: utf-8 -*-
"""A 股交易时段判定（B16 = 服务端自建）。

定位与边界（重要）
------------------
本模块只回答一个**客观事实**：「此刻 A 股是否在开市时段」。用途是让行情类接口在
非交易时段把涨跌幅置 `null`（前端渲染 `--`，F-201 验收②）。

它**刻意不复用** `ui/monitor.py` 的 `DEFAULT_SESSIONS`：那是「监控扫描节拍」语义，
可被 `config/monitor.json: sessions` 覆盖 —— 若混用，用户改扫描节拍会意外改掉行情显示的
有效性判定。两者关注点不同，故各自定义（桌面版退役后 `ui/monitor.py` 一并消失）。

为什么必须显式判定（实测依据）
------------------------------
非交易时段源站并不返回空：腾讯 qt 接口用「当前价 vs 昨收」，盘后当前价=上一交易日收盘 →
**返回上一交易日涨跌幅**（2026-09-12 周六 20:20 实测 4 个指数全部有值）。故「非交易时段
显示 `--`」无法靠数据驱动实现。

已知局限
--------
无节假日日历：**工作日但休市**（法定节假日）会被判为交易时段。与 engine 侧
`pd.bdate_range` 推下一交易日的既有局限同源；补节假日日历属独立变更。
"""

from datetime import date, datetime, time
from typing import Optional, Tuple

from server.core import time as srv_time

TZ = srv_time.TZ

# 时段区间（含端点，与 ui/monitor.py 的 [start, end] 语义一致）
MORNING: Tuple[time, time] = (time(9, 30), time(11, 30))
AFTERNOON: Tuple[time, time] = (time(13, 0), time(15, 0))
SESSIONS: Tuple[Tuple[time, time], ...] = (MORNING, AFTERNOON)


def is_trading_day(day: date) -> bool:
    """是否交易日（仅按工作日判定；**不含节假日日历**，见模块 docstring 局限）。"""
    return day.weekday() < 5


def is_a_share_session(now: Optional[datetime] = None) -> bool:
    """此刻是否处于 A 股连续竞价时段（Asia/Shanghai）。

    `now` 可注入以便测试；缺省取 `server.core.time.now()`（带 +08:00）。
    传入 naive datetime 时按 Asia/Shanghai 解释。
    """
    moment = now or srv_time.now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=TZ)
    if not is_trading_day(moment.date()):
        return False
    current = moment.time()
    return any(start <= current <= end for start, end in SESSIONS)
