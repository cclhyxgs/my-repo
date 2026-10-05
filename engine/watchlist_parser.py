# -*- coding: utf-8 -*-
"""自选列表解析（F-401 诊断启动，2026-09-13 由 `ui/web_api.py::WebAPI._parse_watchlist` 上移）。

原实现把 `self._market_type` 当作全局态读取；上移后改为**显式 `market` 参数**，
与 F-401 验收「同账号并发不同 market 不串市场」对齐（R-09 全局态逐个拔除）。
"""

from engine.data_layer import DataAPI
from engine.state import state


def parse_watchlist(text, market):
    """解析自选品种文本，返回 [{code,name,entry_price,bars_held,direction}] 列表。

    按市场类型分流：
      - 股票：DataAPI 代码/名称解析（sh600519 / 600519 / 贵州茅台）
      - 期货：futures_pool 解析（rb / jd2609 / TA2510 / 螺纹钢 / 螺纹钢 rb / 鸡蛋2609）
    第 4 个字段为可选持仓方向：空/short → 空头，其余（含缺省）→ 多头。
    """
    is_futures = market == 'futures'
    if is_futures:
        from engine.futures_pool import parse_contract_code, all_contracts
        futures_pool = all_contracts()
    items = []
    for line in (text or '').split('\n'):
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(',') if p.strip()]
        if not parts:
            continue
        query = parts[0]
        if is_futures:
            # 期货：支持 'rb' / 'jd2609' / '螺纹钢' / '螺纹钢 rb'
            code = None
            name = None
            _tokens = [t for t in query.split() if t]
            _candidates = [query]
            if len(_tokens) > 1:
                _candidates.insert(0, _tokens[-1])
            for _q in _candidates:
                contract, month = parse_contract_code(_q)
                if contract:
                    code = f"{contract.symbol}{month}" if month else contract.symbol
                    name = contract.name
                    break
            if not code:
                # 中文名匹配（精确/包含）
                name = query
                for c in futures_pool:
                    if query == c.name or query in c.name:
                        code = c.symbol
                        name = c.name
                        break
            if not code:
                # 中文+月份紧贴格式（'鸡蛋2609' / '沪锌2609' → jd2609 / zn2609）
                from engine.futures_pool import parse_chinese_month_name as _pcn
                _c2, _m2 = _pcn(query)
                if _c2:
                    code = f"{_c2.symbol}{_m2}"
                    name = _c2.name
        else:
            code = DataAPI.get_stock_code(query)
            name = DataAPI.get_stock_name(code) if code else None
            if not name:
                # 尝试把 query 当名称直接匹配
                name = query
                code = state.name_to_code.get(query)
                if not code:
                    # 尝试模糊匹配名称
                    for n, c in (state.name_to_code or {}).items():
                        if query in n:
                            code = c
                            name = n
                            break
        entry_price = 0
        bars_held = 0
        direction = 'long'
        if len(parts) >= 2:
            try:
                entry_price = float(parts[1])
            except (ValueError, TypeError):
                pass
            if len(parts) >= 3:
                try:
                    bars_held = int(parts[2])
                except (ValueError, TypeError):
                    pass
            if len(parts) >= 4 and market == 'futures':
                # 第4字段仅期货有效（空/short/s → 空单）；A 股恒为多头，忽略该字段防波及
                _d = (parts[3] or '').strip().lower()
                if _d in ('空', 'short', 's'):
                    direction = 'short'
        items.append({
            'code': code or query,
            'name': name or query,
            'entry_price': entry_price,
            'bars_held': bars_held,
            'direction': direction,
        })
    return items
