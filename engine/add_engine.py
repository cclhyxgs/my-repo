#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""加仓引擎 - 按股票类型 × 三档（触发即加模型）

配置来源：quant_config.get_add_params()['add_tiers']
模型：对每个股票类型配置 3 个档位，每档 = (触发条件, 加仓比例)。
满足触发条件即按配置比例加仓（占持仓金额，受 max_add_ratio / max_single_position 约束）。
"""

from engine import quant_config
from engine.indicators import MATechnical

# 股票分类 → 配置表行
_TYPE_TO_ROW = {
    'strong': 'strong_standard', 'standard': 'strong_standard', 'cautious': 'strong_standard',
    'test': 'test_pending', 'pending': 'test_pending',
    'weak': 'weak_rebound', 'weak_rebound': 'weak_rebound',
    'panic_rebound': 'panic_rebound', 'panic_avoid': 'panic_rebound',
}
_DEFAULT_ROW = 'strong_standard'

# 触发条件中文标签（运行时优先使用 trigger_defs['label']，此为 fallback）
_TRIGGER_LABELS = {
    'MA5': 'MA5', 'MA10': 'MA10', 'MA20': 'MA20',
    'PREV_HIGH': '前高', 'VOL_BREAK': '放量',
    'BREAKOUT_PCT': '突破前高%', 'DEV_UP': '乖离率走强',
    'RSI_LOW': 'RSI超卖', 'MACD_GOLD': 'MACD金叉', 'MA_GOLD': '均线金叉',
    'KDJ_GOLD': 'KDJ金叉', 'ADX_TREND': 'ADX趋势', 'OBV_GOLD': 'OBV金叉', 'BB_BREAKOUT': '布林突破',
}

# 需要「参数」输入（tier.param）的触发码；None 表示无参数
_TRIGGERS_WITH_PARAM = {'BREAKOUT_PCT', 'DEV_UP', 'RSI_LOW'}

# 计算前高 / 均量的回看窗口（用于 PREV_HIGH / VOL_BREAK）
_ADD_WINDOW = 20


class AddEngine:
    """
    加仓引擎 - 按股票类型 × 三档（触发即加）
    """

    @staticmethod
    def suggest_add(ctx):
        """加仓建议（按股票类型 × 三档表格）"""
        params = quant_config.get_add_params()
        if params is None:
            return {
                'can_add': False, 'add_ratio': 0.0, 'raw_add_ratio': 0.0,
                'max_add_ratio': 0.30, 'triggered_tiers': [],
                'action': '未启用', 'reason': '加仓引擎未启用，请在模型配置中开启',
                'note': '以上为系统建议，请结合自身资金与风控调整',
            }
        tiers = params.get('add_tiers', {})
        stock_type = getattr(ctx, 'stock_type', '')
        type_to_row = params.get('type_to_row') or _TYPE_TO_ROW  # 配置缺失回落内置映射（勿用空 dict，否则所有类型都命中 default_row）
        default_row = params.get('default_row', 'strong_standard')
        row_key = type_to_row.get(stock_type, default_row)
        row = tiers.get(row_key, tiers.get(default_row, {}))

        max_add = float(params.get('max_add_ratio', 0.30))
        vol_mult = float(params.get('vol_mult', 2.0))

        latest_price = ctx.latest_price
        if latest_price <= 0:
            return {
                'can_add': False,
                'add_ratio': 0.0,
                'raw_add_ratio': 0.0,
                'max_add_ratio': max_add,
                'triggered_tiers': [],
                'action': '❓ 价格无效',
                'reason': '当前价格无效，无法计算加仓建议',
                'note': '以上为系统建议，请结合自身资金与风控调整',
            }
        tech = ctx.tech
        ma5 = tech.get('sma_5', 0.0)
        ma10 = tech.get('sma_10', 0.0)
        ma20 = tech.get('sma_20', 0.0)
        rsi = tech.get('rsi', 50.0)
        macd_hist = tech.get('macd_hist', 0.0)
        data_list = getattr(ctx, 'data_list', None)

        # 前高 与 均量（用于 PREV_HIGH / VOL_BREAK）；空头加仓参考前低
        if data_list and len(data_list) >= 2:
            window = data_list[-_ADD_WINDOW:] if len(data_list) >= _ADD_WINDOW else data_list
            highs = [d['high'] for d in window]
            recent_high = max(highs) if highs else latest_price * 1.05
            lows = [d['low'] for d in window]
            recent_low = min(lows) if lows else latest_price * 0.95
            vols = [d.get('volume', 0) for d in window]
            avg_volume = sum(vols) / len(vols) if vols else 0.0
            cur_volume = data_list[-1].get('volume', 0)
        else:
            recent_high = latest_price * 1.05
            recent_low = latest_price * 0.95
            avg_volume = 0.0
            cur_volume = 0.0

        has_position = bool(getattr(ctx, 'has_position', False)) and (getattr(ctx, 'entry_price', 0) or 0) > 0
        # 持仓方向：空头时价格类触发反转（加空=向下破位），金叉类触发反转为死叉语义
        is_short = getattr(ctx, 'direction', 'long') == 'short'

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
            """单触发码判定（支持配置化触发条件），返回 (ok, label, price)

            price = 触发阈值（如支撑/阻力/均线），无价格阈值的指标类触发返回 0.0
            """
            trigger_defs = params.get('trigger_defs', {})
            if trig in trigger_defs:
                defn = trigger_defs[trig]
                trig_type = defn.get('type', '')
                source = defn.get('source', '')
                label = defn.get('label', trig)
                
                if trig_type == 'price_ge':
                    if source.startswith('sma_'):
                        val = tech.get(source, 0.0)
                    elif source == 'recent_high':
                        val = recent_high if not is_short else recent_low
                    else:
                        val = tech.get(source, 0.0)
                    if val <= 0:
                        return False, '', 0.0
                    ok = (latest_price <= val) if is_short else (latest_price >= val)
                    return ok, f"{label}（{val:.2f}）", val
                
                elif trig_type == 'price_ge_pct':
                    if source == 'recent_high':
                        ref_val = recent_high if not is_short else recent_low
                    else:
                        ref_val = tech.get(source, 0.0)
                    if ref_val <= 0:
                        return False, '', 0.0
                    pct = _param(tier, trig, defn.get('default_pct', 0.03))
                    ref = ref_val * (1 - pct) if is_short else ref_val * (1 + pct)
                    ok = (latest_price <= ref) if is_short else (latest_price >= ref)
                    return ok, f"{label}{pct*100:.0f}%（{ref:.2f}）", ref
                
                elif trig_type == 'vol_mult':
                    if avg_volume <= 0:
                        return False, '', 0.0
                    mult = float(defn.get('mult', vol_mult))
                    ok = cur_volume >= avg_volume * mult
                    return ok, f"{label}（量比{mult:.1f}）", 0.0
                
                elif trig_type == 'indicator_ge':
                    if source == 'sma_20' and defn.get('calc') == 'deviation':
                        ma_val = tech.get(source, 0.0)
                        if ma_val <= 0:
                            return False, '', 0.0
                        threshold = _param(tier, trig, defn.get('default_threshold', 0.08))
                        dev = (latest_price - ma_val) / ma_val
                        ok = (dev <= -threshold) if is_short else (dev >= threshold)
                        return ok, f"{label}{dev*100:.1f}%{'≤' if is_short else '≥'}{'−' if is_short else ''}{threshold*100:.0f}%", 0.0
                    else:
                        val = tech.get(source, 0.0)
                        threshold = _param(tier, trig, defn.get('default_threshold', 0))
                        ok = (val <= threshold) if is_short else (val >= threshold)
                        return ok, f"{label}{val:.1f}{'≤' if is_short else '≥'}{threshold:.1f}", 0.0
                
                elif trig_type == 'indicator_le':
                    val = tech.get(source, 0.0)
                    threshold = _param(tier, trig, defn.get('default_threshold', 0))
                    ok = (val >= threshold) if is_short else (val <= threshold)
                    return ok, f"{label}{val:.0f}{'≥' if is_short else '≤'}{threshold:.0f}", 0.0
                
                elif trig_type == 'indicator_gt':
                    val = tech.get(source, 0.0)
                    threshold = float(defn.get('threshold', 0))
                    ok = (val < threshold) if is_short else (val > threshold)
                    return ok, f"{label}（{val:.3f}）", 0.0
                
                elif trig_type == 'cross_gt':
                    val1 = tech.get(source, 0.0)
                    ref = defn.get('ref', '')
                    val2 = tech.get(ref, 0.0)
                    if val1 <= 0 or val2 <= 0:
                        return False, '', 0.0
                    ok = (val1 < val2) if is_short else (val1 > val2)
                    return ok, f"{label}（{val1:.2f}{'<' if is_short else '>'}{val2:.2f}）", 0.0

                elif trig_type == 'signal_eq':
                    sub_key = defn.get('sub_key', '')
                    target_val = defn.get('value')
                    if sub_key:
                        src_obj = tech.get(source, {})
                        actual = src_obj.get(sub_key) if isinstance(src_obj, dict) else None
                    else:
                        actual = tech.get(source)
                    ok = actual == target_val
                    return ok, label, 0.0

                elif trig_type == 'signal_contains':
                    actual = tech.get(source, '')
                    target_val = defn.get('value', '')
                    ok = isinstance(actual, str) and target_val in actual
                    return ok, label, 0.0
            
            if trig in ('MA5', 'MA10', 'MA20'):
                ma = {'MA5': ma5, 'MA10': ma10, 'MA20': ma20}[trig]
                if not ma or ma <= 0:
                    return False, '', 0.0
                ok = (latest_price <= ma) if is_short else (latest_price >= ma)
                return ok, f"{_TRIGGER_LABELS.get(trig, trig)}（{ma:.2f}）", ma
            if trig == 'PREV_HIGH':
                if is_short:
                    if recent_low <= 0:
                        return False, '', 0.0
                    ok = latest_price <= recent_low
                    return ok, f"跌破前低（{recent_low:.2f}）", recent_low
                if recent_high <= 0:
                    return False, '', 0.0
                ok = latest_price >= recent_high
                return ok, f"突破前高（{recent_high:.2f}）", recent_high
            if trig == 'VOL_BREAK':
                if avg_volume <= 0:
                    return False, '', 0.0
                ok = cur_volume >= avg_volume * vol_mult
                return ok, f"放量突破（量比{vol_mult:.1f}）", 0.0
            if trig == 'BREAKOUT_PCT':
                param = _param(tier, 'BREAKOUT_PCT', 0.03)
                if is_short:
                    ref = recent_low * (1 - param) if recent_low > 0 else latest_price * 0.95
                    ok = latest_price <= ref
                    return ok, f"跌破前低-{param*100:.0f}%（{ref:.2f}）", ref
                ref = recent_high * (1 + param) if recent_high > 0 else latest_price * 1.05
                ok = latest_price >= ref
                return ok, f"突破前高+{param*100:.0f}%（{ref:.2f}）", ref
            if trig == 'DEV_UP':
                if not ma20 or ma20 <= 0:
                    return False, '', 0.0
                param = _param(tier, 'DEV_UP', 0.08)
                dev = (latest_price - ma20) / ma20
                if is_short:
                    ok = dev <= -param
                    return ok, f"负乖离{dev*100:.1f}%≤{-param*100:.0f}%", 0.0
                ok = dev >= param
                return ok, f"乖离率{dev*100:.1f}%≥{param*100:.0f}%", 0.0
            if trig == 'RSI_LOW':
                # 空单默认 70（超买做空），多单默认 30（超卖加仓）；
                # 二者镜像对称，与 reduce_engine RSI_HIGH 保持一致。
                param = _param(tier, 'RSI_LOW', 70 if is_short else 30)
                ok = (rsi >= param) if is_short else (rsi <= param)
                return ok, f"RSI{rsi:.0f}{'≥' if is_short else '≤'}{param:.0f}（{'超买' if is_short else '超卖'}）", 0.0
            if trig == 'MACD_GOLD':
                ok = (macd_hist < 0) if is_short else (macd_hist > 0)
                return ok, f"MACD{'死叉' if is_short else '金叉'}（柱{macd_hist:.3f}）", 0.0
            if trig == 'MA_GOLD':
                if is_short:
                    ok = (ma5 > 0 and ma10 > 0 and ma5 < ma10)
                    return ok, "均线死叉（MA5<MA10）", 0.0
                ok = (ma5 > 0 and ma10 > 0 and ma5 > ma10)
                return ok, "均线金叉（MA5>MA10）", 0.0
            if trig == 'KDJ_GOLD':
                if is_short:
                    ok = tech.get('kdj_signal') == '死叉'
                    return ok, f"KDJ死叉（K={tech.get('kdj_k', 0):.1f}）", 0.0
                ok = tech.get('kdj_signal') == '金叉'
                return ok, f"KDJ金叉（K={tech.get('kdj_k', 0):.1f}）", 0.0
            if trig == 'ADX_TREND':
                adx_val = tech.get('adx', 0)
                ok = adx_val > 25
                return ok, f"ADX趋势（{adx_val:.0f}）", 0.0
            if trig == 'OBV_GOLD':
                obv_data = tech.get('obv_data', {})
                ok = obv_data.get('obv_breakout', False) if isinstance(obv_data, dict) else False
                return ok, "OBV金叉", 0.0
            if trig == 'BB_BREAKOUT':
                breakout = tech.get('breakout_signal', '')
                if is_short:
                    ok = isinstance(breakout, str) and ('触及下轨' in breakout or '向下突破' in breakout)
                else:
                    ok = isinstance(breakout, str) and ('触及上轨' in breakout or '向上突破' in breakout)
                return ok, f"布林突破（{breakout}）" if ok else '', 0.0
            return False, '', 0.0

        def tier_fired(tier):
            """多触发信号（可多选）：任一触发即视为该档触发（OR 语义），返回 (ok, label, price)

            price = 首个触发的含价格阈值档位的触发价（无则 0.0）
            """
            codes = _tier_triggers(tier)
            labels = []
            prices = []
            for trig in codes:
                ok, label, price = _single(trig, tier)
                if ok and label:
                    labels.append(label)
                    if price and price > 0:
                        prices.append(price)
            return (len(labels) > 0), ' + '.join(labels), (prices[0] if prices else 0.0)

        raw_add = 0.0
        triggered = []
        fired_labels = []
        tier_details = []  # 逐档判定详情，供持仓报告复用（消除报告自算不一致/未识别类型默认满足）
        for tk in ('t1', 't2', 't3'):
            tier = row.get(tk)
            if not isinstance(tier, dict) or not _tier_triggers(tier):
                continue
            ok, label, price = tier_fired(tier)
            ratio = float(tier.get('ratio', 0.2))
            tier_details.append({'slot': tk, 'fired': ok, 'label': label, 'ratio': ratio, 'price': price})
            if ok:
                raw_add += ratio
                triggered.append({'label': label, 'ratio': round(ratio, 3), 'price': price})
                fired_labels.append(label)

        # 受单次加仓上限约束
        add_ratio = min(raw_add, max_add)
        add_ratio = round(add_ratio, 3)

        # 空头排列禁止加仓（风控）；空头持仓反向：多头排列禁止加空
        # 严格档位判定（2026-08-19）：仅"空头排列/完美空头排列"(<=-2) 触发风控，
        # "空头初期"(-1) 仅为转弱预警、不再禁止加仓（文案与档位一致）
        market = getattr(ctx, 'market', {}) or {}
        ma_arrangement = market.get('ma_arrangement', '') if isinstance(market, dict) else ''
        ma_level = MATechnical.arrangement_level(ma_arrangement)
        if is_short:
            bearish = ma_level >= 2.0
            _forbid_label = '多头排列，禁止加空'
            _forbid_reason = '当前为多头排列，触发风控，禁止加空'
        else:
            bearish = ma_level <= -2.0
            _forbid_label = '空头排列，禁止加仓'
            _forbid_reason = '当前为空头排列，触发风控，禁止加仓'

        can_add = bool(has_position) and (raw_add > 0) and (not bearish)
        if bearish:
            action = f'⛔ {_forbid_label}'
            reason = _forbid_reason
        elif not has_position:
            action = 'ℹ️ 当前无持仓，加仓档位仅供参考'
            reason = '未持仓，以下档位为后续加仓预案：' + ('；'.join(fired_labels) if fired_labels else '暂无触发')
        elif add_ratio > 0:
            action = f'🟢 触发加仓 {add_ratio*100:.0f}%（上限 {max_add*100:.0f}%）'
            reason = ' + '.join(fired_labels)
            if raw_add > max_add:
                reason += f'（叠加超出单次上限，已封顶{max_add*100:.0f}%）'
        else:
            action = '✅ 暂不加仓'
            reason = '未触发任何加仓档位'

        return {
            'can_add': can_add,
            'add_ratio': add_ratio,
            'raw_add_ratio': round(raw_add, 3),
            'max_add_ratio': max_add,
            'triggered_tiers': triggered,
            'tier_details': tier_details,
            'action': action,
            'reason': reason,
            'note': '以上为系统建议，请结合自身资金与风控调整',
        }
