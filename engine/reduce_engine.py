#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""减仓引擎 - 按股票类型 × 三档（跌破即减模型）

配置来源：quant_config.get_reduce_params()['reduce_tiers']
模型：对每个股票类型配置 3 个档位，每档 = (触发条件, 动作)。
价格跌破对应触发线 / 满足布尔触发条件即按配置比例减仓，第三档通常为清仓。

触发条件分两类：
  - 价格级：MA5/MA10/MA20、ATR下轨、前低、跌幅%、跌破前低% —— 当前价 <= 触发价即触发
  - 布尔级：乖离率超买、RSI超买、MACD死叉、均线死叉 —— 条件为真即触发
空头持仓（ctx.direction=='short'）：价格类反转为"涨破即减仓"（>= 触发价），
死叉/超买类反转为金叉/超卖语义，前低参考改为前高。
"""

from engine import quant_config

# 股票分类 → 配置表行
_TYPE_TO_ROW = {
    'strong': 'strong_standard', 'standard': 'strong_standard', 'cautious': 'strong_standard',
    'test': 'test_pending', 'pending': 'test_pending',
    'weak': 'weak_rebound', 'weak_rebound': 'weak_rebound',
    'panic_rebound': 'panic_rebound', 'panic_avoid': 'panic_rebound',
}
_DEFAULT_ROW = 'strong_standard'

# MA 不可用时退化乘数
_MA_FALLBACK = {'MA5': 0.98, 'MA10': 0.95, 'MA20': 0.92}

_TRIGGER_LABELS = {
    'MA5': 'MA5', 'MA10': 'MA10', 'MA20': 'MA20',
    'ATR_LOWER': 'ATR下轨', 'PREV_LOW': '前低', 'DROP_PCT': '跌幅',
    'PULLBACK_PCT': '跌破前低%', 'DEV_OVER': '乖离率超买',
    'RSI_HIGH': 'RSI超买', 'MACD_DEAD': 'MACD死叉', 'MA_DEAD': '均线死叉',
    'KDJ_DEAD': 'KDJ死叉', 'BB_BREAKDOWN': '布林跌破',
}

# 需要「参数」输入（tier.param）的触发码
_TRIGGERS_WITH_PARAM = {'DROP_PCT', 'PULLBACK_PCT', 'DEV_OVER', 'RSI_HIGH'}


class ReduceEngine:
    """
    减仓引擎 - 按股票类型 × 三档（跌破即减）

    核心逻辑：
    1. 根据股票类型取对应的三档配置
    2. 计算每档触发状态（价格级：当前价 <= 触发价；布尔级：条件为真）
    3. 触发的档位：减仓 X% 或清仓（清仓则归 1.0）
    """

    @staticmethod
    def suggest_reduce(ctx):
        """减仓建议（按股票类型 × 三档表格）"""
        params = quant_config.get_reduce_params()
        if params is None:
            return {
                'can_reduce': False, 'reduce_ratio': 0.0, 'raw_reduce_ratio': 0.0,
                'action': '未启用', 'reason': '减仓引擎未启用，请在模型配置中开启',
                'triggered_tiers': [],
                'note': '以上为系统建议，请结合自身资金与风控调整',
            }
        tiers = params.get('reduce_tiers', {})
        stock_type = getattr(ctx, 'stock_type', '')
        type_to_row = params.get('type_to_row') or _TYPE_TO_ROW  # 配置缺失回落内置映射（勿用空 dict，否则所有类型都命中 default_row）
        default_row = params.get('default_row', 'strong_standard')
        row_key = type_to_row.get(stock_type, default_row)
        row = tiers.get(row_key, tiers.get(default_row, {}))

        latest_price = ctx.latest_price
        entry_price = ctx.entry_price
        tech = ctx.tech

        ma5 = tech.get('sma_5', 0.0)
        ma10 = tech.get('sma_10', 0.0)
        ma20 = tech.get('sma_20', 0.0)
        rsi = tech.get('rsi', 50.0)
        macd_hist = tech.get('macd_hist', 0.0)
        atr = tech.get('atr', latest_price * float(params.get('atr_fallback', 0.02)))
        atr_mult = float(params.get('atr_mult', 0.5))
        low_mult = float(params.get('recent_low_mult', 0.99))
        lookback = max(1, int(params.get('lookback_days', 20)))
        data_list = getattr(ctx, 'data_list', None)

        # 前低/前高（多头减仓参考前低，空头减仓参考前高）
        if data_list:
            window = data_list[-lookback:] if len(data_list) >= lookback else data_list
            lows = [d['low'] for d in window]
            recent_low = min(lows) if lows else latest_price * 0.95
            highs = [d['high'] for d in window]
            recent_high = max(highs) if highs else latest_price * 1.05
        else:
            recent_low = latest_price * 0.95
            recent_high = latest_price * 1.05

        has_position = bool(getattr(ctx, 'has_position', False)) and (entry_price or 0) > 0
        # 持仓方向：空头时价格类触发反转（减仓/止盈=价格反弹向上），死叉类反转为金叉语义
        is_short = getattr(ctx, 'direction', 'long') == 'short'

        cdata = {
            'price': latest_price, 'entry': entry_price,
            'ma5': ma5, 'ma10': ma10, 'ma20': ma20, 'rsi': rsi, 'macd_hist': macd_hist,
            'atr': atr, 'atr_mult': atr_mult, 'recent_low': recent_low, 'recent_high': recent_high,
            'low_mult': low_mult,
        }

        def _tier_triggers(tier):
            """返回该档的触发码列表（兼容旧版单值 trigger 字段）"""
            tlist = tier.get('triggers')
            if isinstance(tlist, list) and tlist:
                return [t for t in tlist if t]
            trig = tier.get('trigger')
            return [trig] if trig else []

        def _param(tier, code, default):
            """读取某触发码专属参数（优先 triggers 配套的 params 字典）"""
            params = tier.get('params') or {}
            if code in params and params[code] not in (None, ''):
                try:
                    return float(params[code])
                except (ValueError, TypeError):
                    return default
            return default

        def _single(trig, tier):
            """单触发码判定（支持配置化触发条件）"""
            price = cdata['price']
            trigger_defs = params.get('trigger_defs', {})
            
            if trig in trigger_defs:
                defn = trigger_defs[trig]
                trig_type = defn.get('type', '')
                source = defn.get('source', '')
                label = defn.get('label', trig)
                
                if trig_type == 'price_le':
                    val = tech.get(source, 0.0)
                    if val > 0:
                        ok = (price >= val) if is_short else (price <= val)
                        return ok, round(val, 2), f"{label}（{val:.2f}）"
                    fb_map = {'MA5': 0.98, 'MA10': 0.95, 'MA20': 0.92}
                    fb = price * (2 - fb_map.get(trig, 0.95)) if is_short else price * fb_map.get(trig, 0.95)
                    ok = (price >= fb) if is_short else (price <= fb)
                    return ok, round(fb, 2), f"{label}（{fb:.2f}）"
                
                elif trig_type == 'price_le_atr':
                    atr_val = tech.get('atr', cdata.get('atr', 0))
                    mult = float(defn.get('mult', cdata.get('atr_mult', 0.5)))
                    entry_price = cdata.get('entry', price)
                    # ⛔ 移动止损基准必须用「近 lookback 日」极值，绝不能用全量 data_list 极值：
                    #   TDX 接入后 store 含全历史，max(全量 close) 会把多年前的历史高位当成
                    #   「持仓最高点」→ 算出远离现价的假止损价（2026-09-19 实锤：sh688277 取到
                    #   2021 年 141.6 → 止损价 141.09，而当日真实价仅 17.62~18.33）。
                    _hi = cdata.get('recent_high') or price
                    _lo = cdata.get('recent_low') or price
                    trailing = max(entry_price, _hi) if not is_short else min(entry_price, _lo)
                    p = (trailing - atr_val * mult) if not is_short else (trailing + atr_val * mult)
                    ok = (price >= p) if is_short else (price <= p)
                    return ok, round(p, 2), f"{label}（{p:.2f}）"
                
                elif trig_type == 'price_le_prev_low':
                    ref = cdata.get('recent_high' if is_short else 'recent_low', 0)
                    mult = float(defn.get('mult', cdata.get('low_mult', 0.99)))
                    p = (ref / mult) if is_short else (ref * mult)
                    ok = (price >= p) if is_short else (price <= p)
                    return ok, round(p, 2), f"{label}（{p:.2f}）"
                
                elif trig_type == 'price_le_pct_from_entry':
                    pct = _param(tier, trig, defn.get('default_pct', 0.03))
                    p = cdata['entry'] * (1 + pct) if is_short else cdata['entry'] * (1 - pct)
                    ok = (price >= p) if is_short else (price <= p)
                    return ok, round(p, 2), f"{label}{pct*100:.0f}%（{p:.2f}）"
                
                elif trig_type == 'price_le_pct_from_prev_low':
                    pct = _param(tier, trig, defn.get('default_pct', 0.03))
                    ref = cdata.get('recent_high' if is_short else 'recent_low', 0)
                    p = ref * (1 + pct) if is_short else ref * (1 - pct)
                    ok = (price >= p) if is_short else (price <= p)
                    return ok, round(p, 2), f"{label}{pct*100:.0f}%（{p:.2f}）"
                
                elif trig_type == 'indicator_ge':
                    if source == 'sma_20' and defn.get('calc') == 'deviation':
                        m = tech.get('sma_20', 0)
                        if not m or m <= 0:
                            return False, 0, ''
                        threshold = _param(tier, trig, defn.get('default_threshold', 0.15))
                        dev = (price - m) / m
                        ok = (dev <= -threshold) if is_short else (dev >= threshold)
                        return ok, 0, f"{label}{dev*100:.1f}%{'≤' if is_short else '≥'}{'-' if is_short else ''}{threshold*100:.0f}%"
                    else:
                        val = tech.get(source, 0.0)
                        threshold = _param(tier, trig, defn.get('default_threshold', 0))
                        ok = (val <= threshold) if is_short else (val >= threshold)
                        return ok, 0, f"{label}{val:.1f}{'≤' if is_short else '≥'}{threshold:.1f}"
                
                elif trig_type == 'indicator_lt':
                    val = tech.get(source, 0.0)
                    threshold = float(defn.get('threshold', 0))
                    ok = (val > threshold) if is_short else (val < threshold)
                    return ok, 0, f"{label}（{val:.3f}）"
                
                elif trig_type == 'cross_lt':
                    val1 = tech.get(source, 0.0)
                    ref = defn.get('ref', '')
                    val2 = tech.get(ref, 0.0)
                    ok = val1 > 0 and val2 > 0 and ((val1 > val2) if is_short else (val1 < val2))
                    return ok, 0, f"{label}（{val1:.2f}{'>' if is_short else '<'}{val2:.2f}）"

                elif trig_type == 'signal_eq':
                    sub_key = defn.get('sub_key', '')
                    target_val = defn.get('value')
                    if sub_key:
                        src_obj = tech.get(source, {})
                        actual = src_obj.get(sub_key) if isinstance(src_obj, dict) else None
                    else:
                        actual = tech.get(source)
                    ok = actual == target_val
                    return ok, 0, label

                elif trig_type == 'signal_contains':
                    actual = tech.get(source, '')
                    target_val = defn.get('value', '')
                    ok = isinstance(actual, str) and target_val in actual
                    return ok, 0, label
            
            if trig in ('MA5', 'MA10', 'MA20'):
                ma = cdata[trig.lower()]
                if ma and ma > 0:
                    ok = (price >= ma) if is_short else (price <= ma)
                    return ok, round(ma, 2), f"{_TRIGGER_LABELS.get(trig, trig)}（{ma:.2f}）"
                fb = price * (2 - _MA_FALLBACK[trig]) if is_short else price * _MA_FALLBACK[trig]
                ok = (price >= fb) if is_short else (price <= fb)
                return ok, round(fb, 2), f"{_TRIGGER_LABELS.get(trig, trig)}（{fb:.2f}）"
            if trig == 'ATR_LOWER':
                entry_price = cdata['entry']
                # ⛔ 同 price_le_atr：移动止损基准取「近 lookback 日」极值（recent_high/recent_low），
                #   不用全量 data_list 极值——TDX 全历史会把多年前高位当持仓最高点（见上方注释）。
                _hi = cdata.get('recent_high') or price
                _lo = cdata.get('recent_low') or price
                trailing = max(entry_price, _hi) if not is_short else min(entry_price, _lo)
                p = (trailing - cdata['atr'] * cdata['atr_mult']) if not is_short else (trailing + cdata['atr'] * cdata['atr_mult'])
                ok = (price >= p) if is_short else (price <= p)
                return ok, round(p, 2), f"{'ATR上轨' if is_short else 'ATR下轨'}（{p:.2f}）"
            if trig == 'PREV_LOW':
                ref = cdata.get('recent_high' if is_short else 'recent_low', 0)
                p = (ref / cdata['low_mult']) if is_short else (ref * cdata['low_mult'])
                ok = (price >= p) if is_short else (price <= p)
                return ok, round(p, 2), f"{'前高' if is_short else '前低'}（{p:.2f}）"
            if trig == 'DROP_PCT':
                param = _param(tier, 'DROP_PCT', 0.03)
                p = cdata['entry'] * (1 + param) if is_short else cdata['entry'] * (1 - param)
                ok = (price >= p) if is_short else (price <= p)
                return ok, round(p, 2), f"{'涨幅' if is_short else '跌幅'}{param*100:.0f}%（{p:.2f}）"
            if trig == 'PULLBACK_PCT':
                param = _param(tier, 'PULLBACK_PCT', 0.03)
                ref = cdata.get('recent_high' if is_short else 'recent_low', 0)
                p = ref * (1 + param) if is_short else ref * (1 - param)
                ok = (price >= p) if is_short else (price <= p)
                return ok, round(p, 2), f"{'涨破前高' if is_short else '跌破前低'}{param*100:.0f}%（{p:.2f}）"
            if trig == 'DEV_OVER':
                m = cdata['ma20']
                if not m or m <= 0:
                    return False, 0, ''
                param = _param(tier, 'DEV_OVER', 0.15)
                dev = (price - m) / m
                ok = (dev <= -param) if is_short else (dev >= param)
                return ok, 0, f"{'乖离率超卖' if is_short else '乖离率超买'}{dev*100:.1f}%{'≤' if is_short else '≥'}{'-' if is_short else ''}{param*100:.0f}%"
            if trig == 'RSI_HIGH':
                # 空单默认 30（超卖离场），多单默认 70（超买减仓）；
                # 二者镜像对称，与 add_engine RSI_LOW 保持一致。
                param = _param(tier, 'RSI_HIGH', 30 if is_short else 70)
                if is_short:
                    # 空单下 RSI_HIGH 存方向化阈值（默认 30=超卖），直接比较
                    return rsi <= param, 0, f"RSI{rsi:.0f}≤{param:.0f}（超卖）"
                return rsi >= param, 0, f"RSI{rsi:.0f}≥{param:.0f}（超买）"
            if trig == 'MACD_DEAD':
                ok = (macd_hist > 0) if is_short else (macd_hist < 0)
                return ok, 0, f"MACD{'金叉' if is_short else '死叉'}（柱{macd_hist:.3f}）"
            if trig == 'MA_DEAD':
                if is_short:
                    ok = cdata['ma5'] > 0 and cdata['ma10'] > 0 and cdata['ma5'] > cdata['ma10']
                    return ok, 0, "均线金叉（MA5>MA10）"
                ok = cdata['ma5'] > 0 and cdata['ma10'] > 0 and cdata['ma5'] < cdata['ma10']
                return ok, 0, "均线死叉（MA5<MA10）"
            if trig == 'KDJ_DEAD':
                target = '金叉' if is_short else '死叉'
                ok = tech.get('kdj_signal') == target
                return ok, 0, f"KDJ{target}（K={tech.get('kdj_k', 0):.1f}）" if ok else ''
            if trig == 'BB_BREAKDOWN':
                breakout = tech.get('breakout_signal', '')
                if is_short:
                    ok = isinstance(breakout, str) and ('触及上轨' in breakout or '向上突破' in breakout)
                    return ok, 0, f"布林突破（{breakout}）" if ok else ''
                ok = isinstance(breakout, str) and ('触及下轨' in breakout or '向下突破' in breakout)
                return ok, 0, f"布林跌破（{breakout}）" if ok else ''
            return False, 0, ''

        def tier_trigger(tier):
            """多触发信号（可多选）：任一触发即视为该档触发（OR 语义）"""
            codes = _tier_triggers(tier)
            fired_prices = []
            labels = []
            for trig in codes:
                f, p, lbl = _single(trig, tier)
                if f:
                    fired_prices.append(p)
                    if lbl:
                        labels.append(lbl)
            price = fired_prices[0] if fired_prices else 0
            return (len(labels) > 0), price, ' + '.join(labels)

        triggered_tiers = []
        tier_out = {'tier1': 0.0, 'tier1_label': '', 'tier2': 0.0, 'tier2_label': '', 'tier3': 0.0, 'tier3_label': ''}
        reduce_total = 0.0
        for i, tk in enumerate(('t1', 't2', 't3'), start=1):
            tier = row.get(tk)
            if not isinstance(tier, dict) or not _tier_triggers(tier):
                continue
            fired, tprice, label = tier_trigger(tier)
            tier_out[f'tier{i}'] = tprice
            tier_out[f'tier{i}_label'] = label
            if not has_position:
                continue
            if fired:
                action = tier.get('action', 'reduce')
                ratio = float(tier.get('ratio', 0.2))
                if action == 'clear':
                    triggered_tiers.append({'price': tprice, 'label': label, 'action': '清仓', 'ratio': 1.0})
                    reduce_total = 1.0
                    break
                act_str = f"减仓{int(ratio*100)}%"
                triggered_tiers.append({'price': tprice, 'label': label, 'action': act_str, 'ratio': ratio})
                reduce_total = min(1.0, reduce_total + ratio)

        reduce_total = round(reduce_total, 2)
        if reduce_total >= 1.0:
            action = '🔴 触发清仓档，立即清仓'
            urgency = '高'
        elif reduce_total > 0:
            action = f'🔴 触发减仓{reduce_total*100:.0f}%'
            urgency = '高' if reduce_total >= 0.3 else '中'
        else:
            action = '✅ 正常持有'
            urgency = '无'

        reasons = [t['label'] for t in triggered_tiers]
        return {
            'risk_score': 0,
            'reduce_ratio': reduce_total,
            'action': action,
            'urgency': urgency,
            'risk_reasons': [],
            'triggered_tiers': triggered_tiers,
            'tier1': tier_out['tier1'], 'tier1_label': tier_out['tier1_label'],
            'tier2': tier_out['tier2'], 'tier2_label': tier_out['tier2_label'],
            'tier3': tier_out['tier3'], 'tier3_label': tier_out['tier3_label'],
            'reason_summary': ' + '.join(reasons[:3]) if reasons else '未触发止损',
        }
