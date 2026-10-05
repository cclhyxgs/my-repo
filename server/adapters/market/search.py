# -*- coding: utf-8 -*-
"""能力 1：股票搜索（F-204）。

契约（app-architecture.md:326）：`GET /api/search?q=` → **裸数组** `[{code, name}]` ≤20 条

口径（用户 2026-09-12 拍板）：
  B12 服务端搜索（**不**下发全量码表 —— 平台差异列已标注全量下发 = 股票池变公开数据）
  B13 **支持**裸 6 位数字代码匹配（桌面仅支持 sh/sz 前缀完整代码；移动端敲数字更自然）
  B14 期货搜索**不纳入**本条（期货搜索逻辑在 UI 层，与 F-302 一并决策）

匹配顺序（与桌面 `ui/web_api.py:1075 search_stocks` 同构，另加 B13 分支）：
  ① 代码精确（`sh600519`）→ ② 裸数字前缀（`600519` / `600`）→ ③ 名称子串包含
上限 20 条；空白查询返回 `[]`；无匹配返回 `[]`（非 404）。

engine 零改动；engine 访问一律经 `engine_bridge`（T8）。
"""

import re

from server.adapters import engine_bridge

LIMIT = 20

_FULL_CODE_RE = re.compile(r"^(?:sh|sz|bj)\d{6}$", re.IGNORECASE)
_BARE_DIGITS_RE = re.compile(r"^\d{3,6}$")

_pool_ready = False


def _ensure_pool() -> None:
    """池懒加载（缓存优先，见 engine `StockListLoader.load`）。仅在成功时置位。"""
    global _pool_ready
    if _pool_ready:
        return
    if engine_bridge.load_stock_list():
        _pool_ready = True


def _raw(code: str) -> str:
    """去掉市场前缀，仅留数字部分（`sh600519` → `600519`）。"""
    return re.sub(r"^[a-zA-Z]+", "", code)


def search_stocks(q: str, limit: int = LIMIT) -> list:
    q = (q or "").strip()
    if not q:
        return []

    _ensure_pool()
    q_compact = q.replace(" ", "")
    items = []
    seen = set()

    def _add(code, name):
        if code and name and code not in seen and len(items) < limit:
            items.append({"code": code, "name": name})
            seen.add(code)

    # ① 完整代码精确匹配（sh600519 / sz000001 / bj430047）
    if _FULL_CODE_RE.match(q):
        _add(q.lower(), engine_bridge.stock_name_of(q))

    # ② 裸 6 位数字代码前缀匹配（B13）：600519 → sh600519；600 → 600xxx 一簇
    elif _BARE_DIGITS_RE.match(q):
        for code, name in engine_bridge.stock_code_to_name().items():
            if _raw(code).startswith(q):
                _add(code, name)
            if len(items) >= limit:
                break

    # ③ 名称子串包含（与桌面同口径；空格不参与比较）
    if len(items) < limit:
        for name, code in engine_bridge.stock_name_to_code().items():
            if q_compact in name.replace(" ", ""):
                _add(code, name)
            if len(items) >= limit:
                break

    return items


def search_futures(q: str) -> list:
    """期货品种搜索（B14 清理）：委托 engine.futures_search。

    返回 `[{name, code, symbol, exchange, multiplier, contract_month}]`（与股票
    `/api/search` 的 `[{code, name}]` 结构不同，故走独立端点，避免多态响应）。
    """
    return engine_bridge.futures_search(q or "")
