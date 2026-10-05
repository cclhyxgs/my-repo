# -*- coding: utf-8 -*-
"""期货行情条（B17 清理）。

2026-09-13 由 `ui/web_api.py::WebAPI._get_futures_indices` 逐字上移（AST 等价 + 去 `self`）。

返回桌面结构 `{'indices': [...], 'up': None, 'down': None, 'source': 'futures'}`；
服务端在 adapter 层映射为 §3.2 契约（`advance/decline/ts`），engine 保持零语义改动。
"""


def get_futures_indices():
    """期货模式行情条：6 大板块涨跌幅。

    接口层面无期货板块指数（新浪无、东财 push2 被 WAF 挡），
    板块涨跌幅 = 板块内主力合约当日涨跌幅算术平均（2026-08-13 实测定稿）。
    期货模式不展示涨/跌家数（用户要求），up/down 返回 None。
    """
    from engine.futures_pool import all_sectors, get_sector_contracts
    from engine.futures_data import fetch_futures_realtime

    indices = []
    for sector in all_sectors():
        chgs = []
        for c in get_sector_contracts(sector):
            try:
                rt = fetch_futures_realtime(c.symbol, c.secid)
                if rt:
                    chg = rt.get('change_pct')
                    if chg is not None:
                        chgs.append(float(chg))
            except Exception:
                continue
        if chgs:
            avg = round(sum(chgs) / len(chgs), 2)
            indices.append({
                'name': sector,
                'code': 'sector:' + sector,
                'change_pct': avg,
                'member_count': len(chgs),
            })
        else:
            indices.append({'name': sector, 'code': 'sector:' + sector,
                            'change_pct': None, 'member_count': 0})
    return {'indices': indices, 'up': None, 'down': None, 'source': 'futures'}
