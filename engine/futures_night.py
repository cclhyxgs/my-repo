#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""期货夜盘 K 线聚合（自 UI 层上移，2026-09-12，B7 方案 1）。

来源与用途
----------
本模块的 `merge_night_session()` 由 `ui/web_api.py` 的
`WebAPI._merge_futures_night()`（原 :1336-1392）**逐字上移**而来，
上移原因：H5 服务端按 §2.8「engine 可直接 import、不做服务化包装」设计，
但夜盘聚合此前只存在于 UI 层，导致 F-203「期货日K 夜盘合并到下一交易日」
这条验收标准在服务端无法达成（`grep -rn 夜盘 engine/` 曾为空）。

上移后 UI 层 `_merge_futures_night()` 退化为对本函数的薄委托，
三处既有调用方（`get_kline` / `_analyze_futures` / 全市场扫描）行为不变。

⚠️ 行为保持声明（勿在未复核消费方的情况下改动）
------------------------------------------------
- 异常一律静默吞掉（`except Exception: pass` → 返回原 df）。这是**上移前的既有
  语义**，本次刻意原样保留；若要改为显式报错，属独立变更，须连同三处消费方
  一起评估。
- 下一交易日用 `pd.bdate_range` 推定，**不识别法定节假日**（既有局限）。
- 夜盘归属规则：21:00 后归当日；凌晨 00:00~03:00 归前一交易日。
- 仅日K/周K 需要本聚合；分钟级图表本身已含夜盘分钟 bar，勿重复调用。
"""


def merge_night_session(df, symbol, secid):
    """把"最近一个交易日"的夜盘(21点后/跨零点的凌晨段)单独聚合成一根"下一交易日夜盘"K线。

    夜盘归属【下一个交易日】：先定位"已发生的夜盘所在交易日"（避开新浪垃圾/未来时钟根如
    周日00:00），再向后跳到下一交易日；若下一交易日日K尚未生成（如周末看周五夜盘→待周一），
    则把夜盘单独作为一根"下一交易日夜盘"K线追加在最后（_is_night=True）。

    三个消费方共用此逻辑，确保显示与评分口径一致：
      - get_kline       前端显示这根"xx 夜"K线，周六也能看到最新周五夜盘；
      - _analyze_futures 把该夜盘bar喂给评分，让"周五夜盘大涨"这类最新走势正确进入因子，
        不再因下一交易日未开盘而漏掉。

    下一交易日日K一旦生成（如周一开盘），返回前判断其已存在于 _day_dates → 不再重复追加，
    夜盘由真实日K自然合并，与A股"同一活跃bar不重复"一致。
    """
    if df is None or df.empty:
        return df
    try:
        from engine.futures_data import fetch_futures_minute
        import pandas as _pd2
        _min = fetch_futures_minute(symbol, secid, period=5, days=120)
        if _min is None or _min.empty or 'trade_time' not in _min.columns:
            return df
        _hr = _pd2.to_datetime(_min['trade_time']).dt.hour
        _night = _min[(_hr >= 21) | (_hr < 3)].copy()
        if _night.empty:
            return df
        _nt = _pd2.to_datetime(_night['trade_time'])
        # 归属交易日：21点后归当天；凌晨段(<3点)归前一交易日(True→1天偏移)
        _delta = (_nt.dt.hour < 3).astype(int)
        _belong = _nt.dt.normalize() - _pd2.to_timedelta(_delta, unit='D')
        _day_dates = set(_pd2.to_datetime(df['trade_time']).dt.date)
        _in_days = _belong[_belong.dt.date.isin(_day_dates)]
        if _in_days.empty:
            return df
        _last = _in_days.max()
        _rows = _night[_belong == _last].sort_values('trade_time')
        if _rows.empty:
            return df
        # 下一交易日（跳过周末；未覆盖法定节假日时周一开盘后由真实日K自然合并）
        _bd = _in_days.max().date() + _pd2.Timedelta(days=1)
        _next = _pd2.bdate_range(_bd, periods=1)[0].date()
        if _next in _day_dates:
            return df  # 下一交易日日K已存在 → 夜盘已并入其中，无需再单独追加
        df = _pd2.concat([df, _pd2.DataFrame([{
            'trade_time': _pd2.Timestamp(_next),
            'open': float(_rows['open'].iloc[0]),
            'high': float(_rows['high'].max()),
            'low': float(_rows['low'].min()),
            'close': float(_rows['close'].iloc[-1]),
            'volume': float(_rows['volume'].sum()),
            '_is_night': True,
        }])], ignore_index=True)
    except Exception:
        pass
    return df
