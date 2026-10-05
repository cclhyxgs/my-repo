# -*- coding: utf-8 -*-
"""诊断启动（F-401 / 能力 2+3）。

契约（app-architecture.md:345）：`POST /api/diagnosis {text, market, direction, scheme_name}`
→ `{task_id}`。

职责边界（B40）：本层只做「启动」—— 校验 → 解析自选 → 方向过滤 → 提交任务（复用
F-105 任务队列 `TaskExecutor.submit`）。**逐只分析 + summary 五组计数留 F-402**。

R-09（同账号并发不同 market 不串市场）：任务 payload 显式携带 `market`，worker 后续
逐只调 `analyze_stock` 时由 payload 决定 market，**不读任何进程级全局态**。
"""

from server.adapters import engine_bridge
from server.core.errors import ApiError
from server.worker.executor import get_executor

VALID_MARKETS = ("stock", "futures")


def start_diagnosis(
    text: str,
    market: str = "stock",
    direction: str = None,
    scheme_name: str = None,
    k_type: str = "日K",
) -> str:
    """启动一次批量诊断，返回 task_id。"""
    text = (text or "").strip()
    if not text:
        raise ApiError("INVALID_TEXT", "缺少自选列表 text", status_code=422)

    market = (market or "stock").strip().lower()
    if market not in VALID_MARKETS:
        raise ApiError("INVALID_MARKET", f"未知 market：{market}（可选 {'/'.join(VALID_MARKETS)}）", status_code=422)

    # 授权强制已在 F-1404 迁移到端点依赖 `require_license`（账号维度，401/403）。
    # 股票 market：先确保池加载（parse_watchlist 的股票分支依赖 state.name_to_code；
    # 缓存优先，条数达标且 saved_at ≤1 天即不发网络请求）
    if market == "stock":
        engine_bridge.load_stock_list()

    # 解析自选（复用 engine.watchlist_parser，market 显式传入）
    items = engine_bridge.parse_watchlist(text, market)
    if not items:
        raise ApiError("NO_VALID_STOCK", "未识别到有效股票/合约", status_code=422)

    # 方向过滤（监控多空双槽位用；None = 不过滤）
    if direction in ("long", "short"):
        items = [it for it in items if it.get("direction", "long") == direction]
        if not items:
            raise ApiError("NO_DIRECTION_STOCK", f"自选中无 {direction} 方向标的", status_code=422)

    # 提交任务（payload 显式携带 market，worker 不读全局态）
    task_id = get_executor().submit(
        "diagnosis",
        {
            "items": items,
            "market": market,
            "direction": direction,
            "scheme_name": scheme_name,
            "k_type": k_type,
        },
    )
    return task_id
