# -*- coding: utf-8 -*-
"""能力 1：顶部行情跑马灯（F-201）。

契约（app-architecture.md:322）：`GET /api/market/indices` → `{indices[], advance, decline, ts}`

口径（用户 2026-09-12 拍板）：
  B15 `--` 在接口中以 **`null`** 表示（`--` 是前端渲染；`index.html:7749` `pct === null ? '--' : ...`）
  B16 交易时段判定服务端自建（`server/core/market_session.py`）
  B17 **仅 A 股 4 指数**，期货行情条不纳入本条（随 F-302）

行为：
  - 交易时段内：真实涨跌幅与涨跌家数，失败项置 `null` 且**不阻塞**（业务规则明写）。
  - 非交易时段：直接全 `null`，且**不发源站请求**（值本就该是 `--`，顺带省请求 / R-08）。
  - 字段名用冻结契约的 `advance`/`decline`（桌面旧实现是 `up`/`down`，H5 前端接入时按新契约写）。

engine 零改动；engine 访问经 `engine_bridge`（T8）。
"""

from server.adapters import engine_bridge
from server.core import market_session
from server.core import time as srv_time
from server.core.logging import get_logger

logger = get_logger(__name__)

# 桌面常量（ui/web_api.py:2303）：上证指数 / 深证成指 / 创业板指 / 沪深300
INDEX_CODES = ("sh000001", "sz399001", "sz399006", "sh000300")


def fetch_indices() -> dict:
    trading = market_session.is_a_share_session()

    indices = []
    for code in INDEX_CODES:
        name = code
        change_pct = None
        try:
            name = engine_bridge.index_name_of(code)
        except Exception:
            pass

        if trading:
            try:
                real_name, ret = engine_bridge.index_daily_return(code)
                if real_name:
                    name = real_name
                if ret is not None:
                    change_pct = round(float(ret) * 100, 2)
            except Exception as exc:
                # 业务规则：源不可用显示 `--` 不阻塞 —— 不吞掉信息，落日志后置 null
                logger.warning("指数 %s 取数失败，置 null：%s", code, exc)

        indices.append({"code": code, "name": name, "change_pct": change_pct})

    advance = decline = None
    if trading:
        try:
            up, down, _source = engine_bridge.market_breadth()
            advance = int(up) if up is not None else None
            decline = int(down) if down is not None else None
        except Exception as exc:
            logger.warning("涨跌家数取数失败，置 null：%s", exc)

    return {
        "indices": indices,
        "advance": advance,
        "decline": decline,
        "ts": srv_time.now_iso(),
    }


def fetch_futures_indices() -> dict:
    """期货行情条（B17 清理）：6 大板块涨跌幅，`advance`/`decline` 恒 `null`。

    板块涨跌幅 = 板块内主力合约当日涨跌幅算术平均（engine `get_futures_indices`，
    2026-08-13 实测定稿）；期货不展示涨跌家数（用户要求）。**不做 A 股时段判定**
    （期货含夜盘，时段与 A 股不同，桌面也未做 —— 与桌面行为对齐）。
    """
    indices = []
    try:
        raw = engine_bridge.futures_indices()
        indices = raw.get("indices") or []
    except Exception as exc:
        # 业务规则：源不可用显示 `--` 不阻塞 —— 最外层异常降级为空，仍 200
        logger.warning("期货行情条取数失败，置空：%s", exc)
    return {
        "indices": indices,  # 元素含 name/code/change_pct/member_count（多 member_count，B9 附加字段）
        "advance": None,
        "decline": None,
        "ts": srv_time.now_iso(),
    }
