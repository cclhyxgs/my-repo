#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""高级技术指标 - 筹码密集区 / 斐波那契回撤 / 枢轴点"""

import numpy as np


def calc_chip_concentration(data_list, periods=120, bins=50):
    """
    计算筹码密集区（基于价格分布）
    
    Args:
        data_list: K线数据列表，含 high, low, close, volume
        periods: 统计周期（默认120日）
        bins: 价格区间分档数量
    
    Returns:
        dict: 密集区信息
    """
    if not data_list or len(data_list) < 10:
        return {}
    
    if len(data_list) < periods:
        periods = len(data_list)
    
    recent = data_list[-periods:]
    current = data_list[-1]['close']
    
    all_highs = [d['high'] for d in recent]
    all_lows = [d['low'] for d in recent]
    
    price_min = min(all_lows)
    price_max = max(all_highs)
    
    # 修复：如果价格区间太小，扩展5%
    if price_max - price_min < price_min * 0.01:
        price_min *= 0.95
        price_max *= 1.05
    
    # 如果扩展后仍然太小，直接返回空
    if price_max - price_min < 0.001:
        return {}
    
    bin_width = (price_max - price_min) / bins
    bins_edges = [price_min + i * bin_width for i in range(bins + 1)]
    
    volume_by_price = np.zeros(bins)
    
    for bar in recent:
        high_p = bar['high']
        low_p = bar['low']
        volume = bar['volume']
        
        start_bin = max(0, min(bins - 1, int((low_p - price_min) / bin_width)))
        end_bin = max(0, min(bins - 1, int((high_p - price_min) / bin_width)))
        
        if end_bin >= start_bin:
            count = end_bin - start_bin + 1
            vol_per_bin = volume / count
            for b in range(start_bin, end_bin + 1):
                volume_by_price[b] += vol_per_bin
    
    # 找出成交量最大的价格区间
    peak_idx = np.argmax(volume_by_price)
    peak_price = (bins_edges[peak_idx] + bins_edges[peak_idx + 1]) / 2
    
    # 找出密集区（成交量超过峰值50%的区域）
    threshold = volume_by_price[peak_idx] * 0.5
    dense_indices = [i for i, v in enumerate(volume_by_price) if v >= threshold]
    
    if dense_indices:
        dense_low = bins_edges[dense_indices[0]]
        dense_high = bins_edges[dense_indices[-1] + 1]
    else:
        dense_low = peak_price * 0.97
        dense_high = peak_price * 1.03
    
    total_vol = np.sum(volume_by_price)
    dense_vol = sum(volume_by_price[i] for i in dense_indices) if dense_indices else 0
    
    # 判断当前位置
    if current > dense_high:
        position = '上方'
        signal = '突破密集区，抛压小'
    elif current >= dense_low:
        position = '内部'
        signal = '密集区内震荡'
    else:
        position = '下方'
        signal = '跌破密集区，压力大'
    
    return {
        'dense_zone_low': round(dense_low, 3),
        'dense_zone_high': round(dense_high, 3),
        'peak_price': round(peak_price, 3),
        'concentration_ratio': round(dense_vol / total_vol if total_vol > 0 else 0, 3),
        'position': position,
        'signal': signal,
        'is_support': current > dense_high,
        'is_resistance': current < dense_low,
    }


def calc_fibonacci_retracement(data_list, lookback=120):
    """
    计算斐波那契回撤位
    
    Args:
        data_list: K线数据列表
        lookback: 回看周期
    
    Returns:
        dict: 斐波那契回撤信息，数据无效时返回 None
    """
    if not data_list or len(data_list) < 30:
        return None
    
    recent = data_list[-lookback:]
    highs = [d['high'] for d in recent]
    lows = [d['low'] for d in recent]
    closes = [d['close'] for d in recent]
    current = closes[-1]
    
    # 自动检测最近波段
    # 找最近60日内的最高点和最低点
    lookback_band = min(60, len(highs))
    recent_highs = highs[-lookback_band:]
    recent_lows = lows[-lookback_band:]
    
    high_idx = np.argmax(recent_highs)
    low_idx = np.argmin(recent_lows)
    
    # 计算全局偏移
    offset = len(highs) - lookback_band
    
    if high_idx > low_idx:
        # 上涨波段：低点 → 高点
        band_low = recent_lows[low_idx]
        band_high = recent_highs[high_idx]
        direction = '上涨波段'
    else:
        # 下跌波段：高点 → 低点
        band_high = recent_highs[high_idx]
        band_low = recent_lows[low_idx]
        direction = '下跌波段'
    
    diff = band_high - band_low
    # 修复：如果波段幅度太小或无效，返回 None
    if diff <= 0.001:
        return None
    
    fib_levels = [0.0, 0.236, 0.382, 0.500, 0.618, 0.786, 1.0]
    retracements = {}
    
    if direction == '上涨波段':
        # 上涨波段回撤：从高点向下计算
        for level in fib_levels:
            retracements[level] = band_high - diff * level
    else:
        # 下跌波段反弹：从低点向上计算
        for level in fib_levels:
            retracements[level] = band_low + diff * level
    
    # 判断当前位置在哪个回撤位之间
    fib_keys = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]
    closest_level = min(fib_keys, key=lambda x: abs(current - retracements[x]))
    
    # 生成信号
    if direction == '上涨波段':
        if closest_level <= 0.236:
            signal = '强势回撤，上涨趋势强劲'
        elif closest_level <= 0.382:
            signal = '正常回撤，关注支撑'
        elif closest_level <= 0.500:
            signal = '回撤至半分位，多空分水岭'
        elif closest_level <= 0.618:
            signal = '回撤至黄金分割位，强支撑区'
        elif closest_level <= 0.786:
            signal = '深度回撤，趋势存疑'
        else:
            signal = '接近起点，趋势可能反转'
    else:
        if closest_level <= 0.236:
            signal = '弱势反弹，下跌趋势强劲'
        elif closest_level <= 0.382:
            signal = '正常反弹，关注压力'
        elif closest_level <= 0.500:
            signal = '反弹至半分位，多空分水岭'
        elif closest_level <= 0.618:
            signal = '反弹至黄金分割位，强压力区'
        elif closest_level <= 0.786:
            signal = '强势反弹，趋势可能反转'
        else:
            signal = '接近起点，趋势可能反转'
    
    return {
        'high': round(band_high, 3),
        'low': round(band_low, 3),
        'direction': direction,
        'retracements': {k: round(v, 3) for k, v in retracements.items()},
        'current_level': closest_level,
        'current_level_desc': f'{closest_level*100:.1f}%回撤位',
        'current_price': round(current, 3),
        'signal': signal,
    }


def calc_pivot_points(data_list, method='standard'):
    """
    计算枢轴点
    
    Args:
        data_list: K线数据列表（至少2条）
        method: 'standard' 标准, 'fibonacci' 斐波那契, 'woodie' 伍迪
    
    Returns:
        dict: 枢轴点信息
    """
    if not data_list or len(data_list) < 2:
        return None
    
    yesterday = data_list[-2]
    today = data_list[-1]
    
    prev_high = yesterday['high']
    prev_low = yesterday['low']
    prev_close = yesterday['close']
    current = today['close']
    
    if method == 'standard':
        pivot = (prev_high + prev_low + prev_close) / 3
        r1 = 2 * pivot - prev_low
        r2 = pivot + (prev_high - prev_low)
        r3 = prev_high + 2 * (pivot - prev_low)
        s1 = 2 * pivot - prev_high
        s2 = pivot - (prev_high - prev_low)
        s3 = prev_low - 2 * (prev_high - pivot)
        method_name = '标准'
    
    elif method == 'fibonacci':
        pivot = (prev_high + prev_low + prev_close) / 3
        range_ = prev_high - prev_low
        r1 = pivot + 0.382 * range_
        r2 = pivot + 0.618 * range_
        r3 = pivot + 1.000 * range_
        s1 = pivot - 0.382 * range_
        s2 = pivot - 0.618 * range_
        s3 = pivot - 1.000 * range_
        method_name = '斐波那契'
    
    elif method == 'woodie':
        pivot = (prev_high + prev_low + 2 * prev_close) / 4
        r1 = 2 * pivot - prev_low
        r2 = pivot + (prev_high - prev_low)
        r3 = prev_high + 2 * (pivot - prev_low)
        s1 = 2 * pivot - prev_high
        s2 = pivot - (prev_high - prev_low)
        s3 = prev_low - 2 * (prev_high - pivot)
        method_name = '伍迪'
    
    else:
        # 默认标准
        pivot = (prev_high + prev_low + prev_close) / 3
        r1 = 2 * pivot - prev_low
        r2 = pivot + (prev_high - prev_low)
        r3 = prev_high + 2 * (pivot - prev_low)
        s1 = 2 * pivot - prev_high
        s2 = pivot - (prev_high - prev_low)
        s3 = prev_low - 2 * (prev_high - pivot)
        method_name = '标准'
    
    # 先计算 current_position
    current_position = '上方' if current > pivot else '下方'
    
    return {
        'pivot': round(pivot, 3),
        'r1': round(r1, 3),
        'r2': round(r2, 3),
        'r3': round(r3, 3),
        's1': round(s1, 3),
        's2': round(s2, 3),
        's3': round(s3, 3),
        'current': round(current, 3),
        'current_position': current_position,
        'signal': f'在枢轴点{current_position}，{"偏多" if current > pivot else "偏空"}',
        'method': method_name,
        'is_bullish': current > pivot,
    }