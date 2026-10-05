# -*- coding: utf-8 -*-
"""期货搜索（B14 清理）。

2026-09-13 由 `ui/web_api.py::WebAPI.search_futures` 逐字上移（B7/B24 同款手法，
AST 等价 + 去 `self`），桌面与 H5 服务端共用同一实现。

返回 `[{name, code, symbol, exchange, multiplier, contract_month}]`。
"""


def search_futures(query):
    """搜索期货品种，返回 [{name, code, symbol, exchange, multiplier, contract_month}, ...]。

    支持两种输入：
      - 品种代码/名称：'rb' / '螺纹钢' → 主力连续合约（contract_month=None）
      - 具体合约：'jd2609' / 'TA2510' → 具体月份合约（contract_month='2609'）
    """
    from engine.futures_pool import all_contracts, get_contract, parse_contract_code
    q = (query or '').strip()
    if not q:
        # 无查询词时返回全部品种（前端可截断显示）
        return [{'name': c.name, 'code': c.symbol, 'symbol': c.symbol,
                 'exchange': c.exchange, 'multiplier': c.multiplier,
                 'contract_month': None}
                for c in all_contracts()[:30]]

    # 具体合约格式：1~4 位字母 + 4 位数字（如 jd2609 / TA2510）
    import re
    m = re.fullmatch(r'([a-zA-Z]{1,4})(\d{4})', q)
    if m:
        c = get_contract(m.group(1))
        if c:
            month = m.group(2)
            code = f'{c.symbol}{month}'
            return [{'name': c.name, 'code': code, 'symbol': c.symbol,
                     'exchange': c.exchange, 'multiplier': c.multiplier,
                     'contract_month': month}]

    # 中文名 + 月份紧贴格式（'鸡蛋2609' / '沪锌2609' → jd2609 / zn2609）
    from engine.futures_pool import parse_chinese_month_name
    c, month = parse_chinese_month_name(q)
    if c:
        code = f'{c.symbol}{month}'
        return [{'name': c.name, 'code': code, 'symbol': c.symbol,
                 'exchange': c.exchange, 'multiplier': c.multiplier,
                 'contract_month': month}]

    # 中文 + 空格 + 代码（'沪深300 IF' / '螺纹钢 rb'）：拆 token 按代码解析
    _tokens = [t for t in q.split() if t]
    if len(_tokens) > 1:
        for _tok in _tokens:
            _cc, _mm = parse_contract_code(_tok)
            if _cc:
                code = f'{_cc.symbol}{_mm}' if _mm else _cc.symbol
                return [{'name': _cc.name, 'code': code, 'symbol': _cc.symbol,
                         'exchange': _cc.exchange, 'multiplier': _cc.multiplier,
                         'contract_month': _mm}]

    ql = q.lower()
    results = []
    for c in all_contracts():
        if ql in c.symbol.lower() or ql in c.name.lower():
            results.append({'name': c.name, 'code': c.symbol, 'symbol': c.symbol,
                            'exchange': c.exchange, 'multiplier': c.multiplier,
                            'contract_month': None})
        if len(results) >= 20:
            break
    return results
