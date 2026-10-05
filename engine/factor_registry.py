#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""因子注册表 - 21 个候选因子的元数据声明（权威口径：len(REGISTRY)==21，见 tests/h5_skeleton/test_skeleton_acceptance.py::EXPECTED_FACTOR_COUNT）

将因子从 score_calculator_v2.py 的硬编码提取为注册表驱动，
用户可从因子库自由组合、调参、配权重，搭建自己的交易模型。

每个因子声明：
- name: 因子标识（英文，配置文件用）
- label: 中文显示名
- category: 分类（momentum/trend/volume/volatility/pattern）
- calc_fn: 计算函数 calc_fn(ctx, params) -> float
- params_schema: 可调参数声明 {name: {default, min, max, step, label}}
- default_weight: 默认权重
- default_direction: 默认方向（+1看涨/-1看跌）
- default_stats: 默认z-score统计参数 {mean, std}
- ic: IC值（有回测数据的填，没有的为None）
- desc: 因子描述
"""

import logging

import numpy as np
from engine.indicators import (MATechnical, RSICalculator, MACDCalculator,
    BollingerAnalyzer, ATRCalculator, ADXCalculator, OBVCalculator,
    CandlestickPatterns, VolatilityCone, DrawdownCalculator, KDJCalculator)
from engine.indicators_advanced import (calc_chip_concentration,
    calc_fibonacci_retracement, calc_pivot_points)

logger = logging.getLogger(__name__)


# ============================================================
# 因子定义类
# ============================================================

class FactorDef:
    """单个因子的元数据定义"""
    __slots__ = ['name', 'label', 'category', 'calc_fn', 'params_schema',
                 'default_weight', 'default_direction', 'default_stats',
                 'ic', 'ic_ir', 'desc', 'explanations']

    def __init__(self, name, label, category, calc_fn, params_schema,
                 default_weight, default_direction, default_stats,
                 ic=None, ic_ir=None, desc='', explanations=None):
        self.name = name
        self.label = label
        self.category = category
        self.calc_fn = calc_fn
        self.params_schema = params_schema
        self.default_weight = default_weight
        self.default_direction = default_direction
        self.default_stats = default_stats
        self.ic = ic
        self.ic_ir = ic_ir
        self.desc = desc
        self.explanations = explanations


# ============================================================
# 因子计算函数
# 统一签名: calc_fn(ctx, params) -> float
# ctx = {'closes','highs','lows','volumes','opens','data_list','latest_price','tech','market'}
# ============================================================

def _calc_relative_strength(ctx, params):
    """N日相对强度（涨幅）"""
    period = params.get('period', 20)
    closes = ctx['closes']
    if len(closes) < period + 1:
        return 0.0
    return (closes[-1] - closes[-period]) / closes[-period]


def _calc_rsi(ctx, params):
    """RSI值"""
    period = params.get('period', 14)
    return float(RSICalculator.calc_wilder(ctx['closes'], period))


def _calc_macd_hist_norm(ctx, params):
    """MACD柱状图归一化（fast/slow/signal 周期取用户配置，默认 12/26/9）"""
    fast = int(params.get('fast', 12))
    slow = int(params.get('slow', 26))
    signal = int(params.get('signal', 9))
    macd_line, macd_signal, macd_hist = MACDCalculator.calc(ctx['closes'], fast, slow, signal)
    price = ctx['latest_price']
    return macd_hist / price if price > 0 else 0.0


def _calc_ma_slope(ctx, params):
    """MA斜率"""
    period = params.get('period', 20)
    lookback = params.get('lookback', 5)
    closes = ctx['closes']
    if len(closes) < period + lookback:
        return 0.0
    ma_now = MATechnical.calc_sma(closes, period)
    ma_prev = MATechnical.calc_sma(closes[:-lookback], period)
    return (ma_now - ma_prev) / ma_prev if ma_prev > 0 else 0.0


def _calc_di_spread(ctx, params):
    """DI方向运动差"""
    period = params.get('period', 14)
    adx_result = ADXCalculator.calc_adx(ctx['highs'], ctx['lows'], ctx['closes'], period)
    return adx_result.get('di_plus', 0) - adx_result.get('di_minus', 0)


def _calc_ma_arrangement(ctx, params):
    """均线排列强度（数值化），period 为基础均线周期（默认20→5/10/20/60/120/250）"""
    period = params.get('period', 20)
    arrangement = MATechnical.judge_arrangement(ctx['closes'], period)
    score_map = {
        '完美多头排列': 3.0, '多头排列': 2.0, '多头初期·均线发散': 1.0,
        '均线黏合·即将变盘': 0.0, '均线交织': 0.0,
        '空头初期·均线发散': -1.0, '空头排列': -2.0, '完美空头排列': -3.0,
    }
    return score_map.get(arrangement, 0.0)


def _calc_bias(ctx, params):
    """乖离率"""
    period = params.get('period', 20)
    closes = ctx['closes']
    if len(closes) < period:
        return 0.0
    ma = float(np.mean(closes[-period:]))
    return (closes[-1] - ma) / ma * 100 if ma > 0 else 0.0


def _calc_volume_ratio(ctx, params):
    """量比"""
    period = params.get('period', 5)
    volumes = ctx['volumes']
    if len(volumes) < period + 1:
        return 1.0
    avg = float(np.mean(volumes[-period - 1:-1]))
    return volumes[-1] / avg if avg > 0 else 1.0


def _calc_obv_trend(ctx, params):
    """OBV趋势（归一化变化率%）"""
    ma_period = params.get('ma_period', 10)
    closes = ctx['closes']
    volumes = ctx['volumes']
    if len(closes) < 20:
        return 0.0
    obv = OBVCalculator.calc(closes, volumes)
    if len(obv) < ma_period + 5:
        return 0.0
    obv_now = obv[-1]
    obv_prev = obv[-(ma_period + 5)]
    return (obv_now - obv_prev) / abs(obv_prev) * 100 if abs(obv_prev) > 1e-9 else 0.0


def _calc_volume_price_signal(ctx, params):
    """量价关系评分"""
    obv_result = OBVCalculator.analyze(ctx['closes'], ctx['volumes'])
    signal = obv_result.get('signal', '')
    if '配合上涨' in signal:
        return 2.0
    if '配合下跌' in signal:
        return -2.0
    if '收集' in signal:
        return 1.0
    if '背离' in signal and '上涨' in signal:
        return -1.0
    return 0.0


def _calc_bb_bandwidth(ctx, params):
    """布林带带宽"""
    period = params.get('period', 20)
    bb = BollingerAnalyzer.calc(ctx['closes'], period=period)
    return float(bb.get('bandwidth', 15.0))


def _calc_atr_norm(ctx, params):
    """ATR/价格（归一化波动率）"""
    period = params.get('period', 14)
    atr = ATRCalculator.calc_wilder(ctx['data_list'], period)
    price = ctx['latest_price']
    return atr / price if atr and price > 0 else 0.0


def _calc_current_drawdown(ctx, params):
    """当前回撤幅度%（lookback 为回看窗口，缺省=整段历史）"""
    lookback = params.get('lookback')
    dd = DrawdownCalculator.calc(ctx['closes'], lookback=lookback)
    return float(dd.get('current_dd', 0.0))


def _calc_volatility_cone(ctx, params):
    """波动率锥位置（period 窗口波动率在历史分位数，默认 20）"""
    period = params.get('period', 20)
    cone = VolatilityCone.calc(ctx['closes'], lookback=period)
    key = f'vol_{int(round(period))}d'
    entry = cone.get(key)
    if not entry:
        entry = next((v for k, v in cone.items() if k.startswith('vol_') and isinstance(v, dict)), {})
    return float(entry.get('percentile', 50.0))


def _calc_pattern_reverse(ctx, params):
    """K线形态反向分"""
    tech = ctx['tech']
    patterns = tech.get('candlestick_patterns', [])
    if not patterns:
        patterns = CandlestickPatterns.detect(ctx['opens'], ctx['highs'], ctx['lows'], ctx['closes'])
    return float(sum(
        1 if '看涨' in p or '三连阳' in p else -1 if '看跌' in p or '三连阴' in p else 0
        for p in patterns
    ))


def _calc_doji(ctx, params):
    """十字星判定（0/1）：当前K线为十字星（变盘信号）返回1，否则0。

    复用 CandlestickPatterns.detect 现成判定，无需重复实现形态逻辑。
    语义：十字星=变盘/方向不明 → 中性偏谨慎，direction 默认 -1（压低置信度），
    属于"弱浮动调整"而非开仓强信号。
    """
    patterns = ctx['tech'].get('candlestick_patterns', [])
    if not patterns:
        patterns = CandlestickPatterns.detect(ctx['opens'], ctx['highs'], ctx['lows'], ctx['closes'])
    return float(1.0 if any('十字星' in p for p in patterns) else 0.0)


def _calc_fib_position(ctx, params):
    """斐波那契偏多程度（0=极弱势, 1=极强势，值越大越看多）。

    上涨波段：回撤越少越强势 → 返回 1-current_level
    下跌波段：反弹越多越强势 → 返回 current_level
    统一语义后 direction=+1 正确（值越大→z越高→正贡献→偏多）。
    """
    lookback = params.get('lookback', 120)
    fib = calc_fibonacci_retracement(ctx['data_list'], lookback)
    if not fib:
        return 0.5
    level = float(fib.get('current_level', 0.5))
    if fib.get('direction') == '下跌波段':
        return level        # 反弹越多越强
    return 1.0 - level      # 回撤越少越强


def _calc_pivot_distance(ctx, params):
    """轴心点距离（当前价相对pivot的偏离%）"""
    pivot_data = calc_pivot_points(ctx['data_list'])
    if not pivot_data:
        return 0.0
    pivot = pivot_data.get('pivot', 0)
    price = ctx['latest_price']
    return (price - pivot) / pivot * 100 if pivot > 0 else 0.0


def _calc_chip_concentration(ctx, params):
    """筹码集中度（集中区成交量占比，0-1）"""
    bins = params.get('bins', 50)
    chip = calc_chip_concentration(ctx['data_list'], bins=bins)
    if not chip:
        return 0.0
    return float(chip.get('concentration_ratio', 0.0))


def _calc_kdj_signal(ctx, params):
    """KDJ信号强度（K-D差值，正值=金叉区域，负值=死叉区域）"""
    period = params.get('period', 9)
    k, d, j, signal = KDJCalculator.calc(ctx['highs'], ctx['lows'], ctx['closes'], period)
    return k - d


def _calc_adx_trend_strength(ctx, params):
    """ADX趋势强度（ADX值，>25为趋势市）"""
    period = params.get('period', 14)
    adx_result = ADXCalculator.calc_adx(ctx['highs'], ctx['lows'], ctx['closes'], period)
    return float(adx_result.get('adx', 25))


# ============================================================
# 因子注册表
# ============================================================

REGISTRY = {
    # ---- 动量类 ----
    'relative_strength_20d': FactorDef(
        name='relative_strength_20d', label='20日相对强度', category='momentum',
        calc_fn=_calc_relative_strength,
        params_schema={'period': {'default': 20, 'min': 5, 'max': 60, 'step': 1, 'label': '计算周期'}},
        default_weight=0.167, default_direction=+1,
        default_stats={'mean': 0.02, 'std': 0.15},
        ic=None, desc='N日涨幅，衡量近期动量强度（v2校准：IC近乎为零，10日动量无预测力）',
        explanations=('近期动量强势，涨幅领先', '近期动量弱势，涨幅落后')
    ),
    'rsi_value': FactorDef(
        name='rsi_value', label='RSI', category='momentum',
        calc_fn=_calc_rsi,
        params_schema={'period': {'default': 14, 'min': 5, 'max': 30, 'step': 1, 'label': 'RSI周期'}},
        default_weight=0.10, default_direction=+1,
        default_stats={'mean': 50, 'std': 15},
        ic=None, desc='相对强弱指标（v2校准：高RSI预测正收益，动量效应>均值回归，方向+1）',
        explanations=('RSI适中，市场情绪不极端', 'RSI偏高，市场可能过热')
    ),
    'macd_hist_norm': FactorDef(
        name='macd_hist_norm', label='MACD柱状图', category='momentum',
        calc_fn=_calc_macd_hist_norm,
        params_schema={
            'fast': {'default': 12, 'min': 5, 'max': 40, 'step': 1, 'label': '快线EMA'},
            'slow': {'default': 26, 'min': 10, 'max': 60, 'step': 1, 'label': '慢线EMA'},
            'signal': {'default': 9, 'min': 3, 'max': 30, 'step': 1, 'label': '信号线EMA'},
        },
        default_weight=0.123, default_direction=-1,
        default_stats={'mean': 0.0, 'std': 0.02},
        ic=-0.012, desc='MACD柱状图归一化值（v2校准：IC为负，高MACD柱预测负收益，方向-1）',
        explanations=('MACD柱对多头有利，动能向上', 'MACD柱对空头有利，动能向下')
    ),
    'kdj_signal': FactorDef(
        name='kdj_signal', label='KDJ信号', category='momentum',
        calc_fn=_calc_kdj_signal,
        params_schema={'period': {'default': 9, 'min': 5, 'max': 30, 'step': 1, 'label': 'KDJ周期'}},
        default_weight=0.10, default_direction=+1,
        default_stats={'mean': 0.0, 'std': 10.0},
        ic=None, desc='KDJ的K-D差值，正值表示金叉区域，负值表示死叉区域',
        explanations=('KDJ处于金叉区域，短线偏多', 'KDJ处于死叉区域，短线偏空')
    ),

    # ---- 均线/趋势类 ----
    'ma_slope': FactorDef(
        name='ma_slope', label='MA20斜率', category='trend',
        calc_fn=_calc_ma_slope,
        params_schema={
            'period': {'default': 20, 'min': 5, 'max': 60, 'step': 1, 'label': '均线周期'},
            'lookback': {'default': 5, 'min': 1, 'max': 20, 'step': 1, 'label': '回看天数'},
        },
        default_weight=0.155, default_direction=+1,
        default_stats={'mean': 0.0, 'std': 0.03},
        ic=None, desc='均线斜率，衡量趋势方向',
        explanations=('均线斜率向上，中期趋势向好', '均线斜率向下，中期趋势走弱')
    ),
    'di_spread': FactorDef(
        name='di_spread', label='DI方向运动', category='trend',
        calc_fn=_calc_di_spread,
        params_schema={'period': {'default': 14, 'min': 5, 'max': 30, 'step': 1, 'label': 'DI周期'}},
        default_weight=0.146, default_direction=+1,
        default_stats={'mean': 0.0, 'std': 15.0},
        ic=None, desc='+DI与-DI差值，衡量多空力量对比',
        explanations=('多方力量占优，上升动能充足', '空方力量占优，下行压力较大')
    ),
    'ma_arrangement': FactorDef(
        name='ma_arrangement', label='均线排列', category='trend',
        calc_fn=_calc_ma_arrangement,
        params_schema={'period': {'default': 20, 'min': 10, 'max': 60, 'step': 1, 'label': '均线周期'}},
        default_weight=0.10, default_direction=+1,
        default_stats={'mean': 0.0, 'std': 2.0},
        ic=None, desc='均线多空排列强度，+3完美多头到-3完美空头',
        explanations=('均线多头排列，趋势格局良好', '均线空头排列，趋势格局承压')
    ),
    'bias_value': FactorDef(
        name='bias_value', label='乖离率', category='trend',
        calc_fn=_calc_bias,
        params_schema={'period': {'default': 20, 'min': 5, 'max': 60, 'step': 1, 'label': '均线周期'}},
        default_weight=0.10, default_direction=-1,
        default_stats={'mean': 0.0, 'std': 5.0},
        ic=None, desc='价格偏离均线的百分比，过大有回归需求',
        explanations=('乖离率合理，价格与均线关系健康', '乖离率过大，价格有回归均线的需求')
    ),
    'adx_trend_strength': FactorDef(
        name='adx_trend_strength', label='ADX趋势强度', category='trend',
        calc_fn=_calc_adx_trend_strength,
        params_schema={'period': {'default': 14, 'min': 5, 'max': 30, 'step': 1, 'label': 'ADX周期'}},
        default_weight=0.10, default_direction=+1,
        default_stats={'mean': 25.0, 'std': 10.0},
        ic=None, desc='ADX值衡量趋势强度，>25为趋势市，趋势越强信号越可靠',
        explanations=('趋势强度较高，方向性较确定', '趋势强度较弱，方向性不确定')
    ),

    # ---- 量价类 ----
    'volume_ratio': FactorDef(
        name='volume_ratio', label='量比', category='volume',
        calc_fn=_calc_volume_ratio,
        params_schema={'period': {'default': 5, 'min': 3, 'max': 20, 'step': 1, 'label': '均量周期'}},
        default_weight=0.10, default_direction=-1,
        default_stats={'mean': 1.0, 'std': 0.5},
        ic=-0.009, desc='当日成交量与近期均量之比（放量可能是抛压或追高，需结合量价关系判断）',
        explanations=('量比温和，成交稳定', '量比异常放大，注意抛压风险')
    ),
    'obv_trend': FactorDef(
        name='obv_trend', label='OBV趋势', category='volume',
        calc_fn=_calc_obv_trend,
        params_schema={'ma_period': {'default': 10, 'min': 5, 'max': 30, 'step': 1, 'label': 'OBV均线周期'}},
        default_weight=0.10, default_direction=+1,
        default_stats={'mean': 0.0, 'std': 20.0},
        ic=None, desc='OBV变化率，衡量资金流入流出（v2校准：最强因子，t=2.92）',
        explanations=('资金持续流入，量能趋势向上', '资金持续流出，量能趋势向下')
    ),
    'volume_price_signal': FactorDef(
        name='volume_price_signal', label='量价关系', category='volume',
        calc_fn=_calc_volume_price_signal,
        params_schema={},
        default_weight=0.10, default_direction=+1,
        default_stats={'mean': 0.0, 'std': 2.0},
        ic=-0.003, desc='量价配合度，+2配合上涨到-2配合下跌',
        explanations=('量价配合良好，上涨有量能支撑', '量价配合较差，上涨持续性存疑')
    ),

    # ---- 波动类 ----
    'bb_bandwidth': FactorDef(
        name='bb_bandwidth', label='布林带带宽', category='volatility',
        calc_fn=_calc_bb_bandwidth,
        params_schema={'period': {'default': 20, 'min': 10, 'max': 40, 'step': 1, 'label': '布林带周期'}},
        default_weight=0.149, default_direction=+1,
        default_stats={'mean': 15.0, 'std': 10.0},
        ic=None, desc='布林带宽度，衡量波动率水平（v2校准：第二强因子）',
        explanations=('布林带开口放大，波动率扩张', '布林带收窄，波动压缩后倾向于方向性突破')
    ),
    'atr_norm': FactorDef(
        name='atr_norm', label='ATR波动率', category='volatility',
        calc_fn=_calc_atr_norm,
        params_schema={'period': {'default': 14, 'min': 5, 'max': 30, 'step': 1, 'label': 'ATR周期'}},
        default_weight=0.10, default_direction=+1,
        default_stats={'mean': 0.02, 'std': 0.01},
        ic=None, desc='ATR占价格比例，归一化波动率',
        explanations=('ATR偏高，波动率放大，风险提升', 'ATR温和，价格波动在可控范围')
    ),
    'current_drawdown': FactorDef(
        name='current_drawdown', label='当前回撤', category='volatility',
        calc_fn=_calc_current_drawdown,
        params_schema={'lookback': {'default': 250, 'min': 20, 'max': 500, 'step': 10, 'label': '回看周期'}},
        default_weight=0.119, default_direction=-1,
        default_stats={'mean': 8.0, 'std': 12.0},
        ic=-0.018, desc='当前价格相对高点的回撤幅度%（v2校准：IC为负，高回撤预测负收益，方向-1）',
        explanations=('回撤较小，趋势保持良好', '回撤较深，短期趋势偏弱，需要时间修复')
    ),
    'volatility_cone': FactorDef(
        name='volatility_cone', label='波动率锥位置', category='volatility',
        calc_fn=_calc_volatility_cone,
        params_schema={'period': {'default': 20, 'min': 5, 'max': 60, 'step': 1, 'label': '分位窗口'}},
        default_weight=0.10, default_direction=-1,
        default_stats={'mean': 50.0, 'std': 25.0},
        ic=-0.035, desc='20日波动率在历史分位数，高位注意风险（v2校准：第三强因子，t=-2.60）',
        explanations=('波动率处于低位，走势稳健', '波动率处于高位，注意回撤风险')
    ),

    # ---- 形态/结构类 ----
    'pattern_reverse': FactorDef(
        name='pattern_reverse', label='K线形态(反向)', category='pattern',
        calc_fn=_calc_pattern_reverse,
        params_schema={},
        default_weight=0.091, default_direction=-1,
        default_stats={'mean': 0.0, 'std': 1.5},
        ic=-0.009, desc='K线形态反向分，看涨形态反而易跌(contrarian)',
        explanations=('反转形态对多头有利', '反转形态对空头有利')
    ),
    'doji': FactorDef(
        name='doji', label='十字星(变盘)', category='pattern',
        calc_fn=_calc_doji,
        params_schema={},
        default_weight=0.08, default_direction=-1,
        default_stats={'mean': 0.1, 'std': 0.3},
        ic=None, desc='十字星变盘信号，方向不明，压低置信度(弱浮动)',
        explanations=('当前为十字星(变盘/方向不明)', '当前非十字星')
    ),
    'fib_position': FactorDef(
        name='fib_position', label='斐波那契位置', category='pattern',
        calc_fn=_calc_fib_position,
        params_schema={'lookback': {'default': 120, 'min': 30, 'max': 250, 'step': 10, 'label': '回看周期'}},
        default_weight=0.10, default_direction=+1,
        default_stats={'mean': 0.5, 'std': 0.25},
        ic=None, desc='斐波那契偏多程度，0=极弱势 1=极强势（方向+1，值越大越看多）',
        explanations=('价格处于斐波那契强势区，结构偏多', '价格处于斐波那契弱势区，结构偏空')
    ),
    'pivot_distance': FactorDef(
        name='pivot_distance', label='轴心点距离', category='pattern',
        calc_fn=_calc_pivot_distance,
        params_schema={},
        default_weight=0.10, default_direction=+1,
        default_stats={'mean': 0.0, 'std': 5.0},
        ic=-0.002, desc='当前价相对枢轴点的偏离百分比',
        explanations=('价格位于枢轴点上方，结构偏多', '价格位于枢轴点下方，结构偏空')
    ),
    'chip_concentration': FactorDef(
        name='chip_concentration', label='筹码集中度', category='pattern',
        calc_fn=_calc_chip_concentration,
        params_schema={'bins': {'default': 50, 'min': 20, 'max': 100, 'step': 10, 'label': '价格分档数'}},
        default_weight=0.10, default_direction=+1,
        default_stats={'mean': 0.3, 'std': 0.2},
        ic=None, desc='筹码密集区成交量占比（v2校准：IC=0.022, t=1.08，正向预测）',
        explanations=('筹码集中度高，上方抛压较小', '筹码分散，抛压较重')
    ),
}

# 原始7个因子名（用于旧配置迁移）
LEGACY_FACTOR_NAMES = [
    'relative_strength_20d', 'ma_slope', 'bb_bandwidth', 'di_spread',
    'macd_hist_norm', 'current_drawdown', 'pattern_reverse',
]

# 分类中文显示名
CATEGORY_LABELS = {
    'momentum': '动量类',
    'trend': '均线/趋势类',
    'volume': '量价类',
    'volatility': '波动类',
    'pattern': '形态/结构类',
}


# ============================================================
# 报告解释文本：档位/值域映射（2026-08-19 修复）
# 此前 _explain_factor 仅按 contrib 正负选二元 explanations，导致
# 多档位/非单调因子系统性错配：'多头初期'被显示成'多头排列'、
# RSI≈45 被显示成'RSI偏高过热'、高 ATR 显示'ATR温和'等。
# 规则优先级：离散档位表 > 值域分段表 > 二元 pair（原逻辑）。
# ============================================================

# 1) 离散档位因子：raw 值 -> 档位文本（精确匹配，miss 取最近档）
_FACTOR_LEVEL_EXPLANATIONS = {
    'ma_arrangement': {
        3.0: '完美多头排列，均线全面向上',
        2.0: '均线多头排列，趋势格局良好',
        1.0: '均线多头初期，短期均线刚上穿，趋势待确认',
        0.0: '均线交织黏合，方向未明',
        -1.0: '均线空头初期，短期均线刚下穿，趋势转弱',
        -2.0: '均线空头排列，趋势格局承压',
        -3.0: '完美空头排列，均线全面向下',
    },
    'volume_price_signal': {
        2.0: '量价配合良好，上涨有量能支撑',
        1.0: '资金低位收集，蓄势待发',
        0.0: '量价关系中性，无异常信号',
        -1.0: '量价背离，上涨持续性存疑',
        -2.0: '放量下跌，抛压明显',
    },
}

# 2) 连续值域分段因子：(lo, hi, 文本)，闭区间
_FACTOR_RANGE_EXPLANATIONS = {
    'rsi_value': [
        (70.0, 100.0, 'RSI偏高，短线过热，注意追高风险'),
        (50.0, 70.0, 'RSI偏强，短线动量占优'),
        (30.0, 50.0, 'RSI偏弱，短线动能不足'),
        (0.0, 30.0, 'RSI偏低，短线超卖'),
    ],
    'bias_value': [
        (5.0, float('inf'), '正乖离过大，短线有回归压力'),
        (float('-inf'), -5.0, '负乖离过大，超跌后存在反弹需求'),
        (-5.0, 5.0, '乖离率合理，价格与均线关系健康'),
    ],
}


def factor_explanation(name, raw_value, contrib, pair):
    """按因子 raw 档位/值域返回报告解释文本；无档位表时回退二元 pair（原逻辑）。

    Args:
        name: 因子名
        raw_value: 因子原始值（score_detail 行解析），None 时回退
        contrib: 加权贡献值（决定加减分分组，不决定文本选择）
        pair: (positive_text, negative_text) 单调因子的二元解释
    """
    if raw_value is None:
        # 无法解析 raw 时回退原逻辑（仅按贡献符号）
        if contrib > 0:
            return pair[0]
        if contrib < 0:
            return pair[1]
        return pair[1] if pair[1] else pair[0]
    v = float(raw_value)
    levels = _FACTOR_LEVEL_EXPLANATIONS.get(name)
    if levels is not None:
        if v in levels:
            return levels[v]
        best = min(levels, key=lambda k: abs(k - v))
        return levels[best]
    ranges = _FACTOR_RANGE_EXPLANATIONS.get(name)
    if ranges is not None:
        for lo, hi, text in ranges:
            if lo <= v <= hi:
                return text
        return pair[0] if v >= 0 else pair[1]
    # 单调因子：原逻辑
    if contrib > 0:
        return pair[0]
    if contrib < 0:
        return pair[1]
    return pair[1] if pair[1] else pair[0]


def parse_raw_value(entry_line):
    """从 score_detail 行解析因子 raw 值。

    'ma_arrangement: 1.0000 -> z=0.77 x w=0.388 = +9.0' → 1.0
    解析失败返回 None（调用方回退原逻辑）。
    """
    try:
        seg = entry_line.split(':', 1)[1].split('->', 1)[0].strip()
        return float(seg)
    except (ValueError, IndexError):
        return None



# ============================================================
# 查询接口
# ============================================================

def get_factor(name):
    """获取单个因子定义，不存在返回 None"""
    return REGISTRY.get(name)


def get_all_factors():
    """获取全部因子定义"""
    return dict(REGISTRY)


def get_factors_by_category():
    """按分类分组返回因子列表"""
    result = {}
    for name, fdef in REGISTRY.items():
        result.setdefault(fdef.category, []).append(fdef)
    return result


def get_default_active_factors():
    """默认启用的因子（原始7个，兼容旧行为）"""
    return list(LEGACY_FACTOR_NAMES)


def get_default_factor_config(name):
    """获取单个因子的默认配置（weight/direction/params/stats）"""
    fdef = REGISTRY.get(name)
    if not fdef:
        return None
    params = {k: v['default'] for k, v in fdef.params_schema.items()}
    return {
        'weight': fdef.default_weight,
        'direction': fdef.default_direction,
        'params': params,
        'stats': dict(fdef.default_stats),
    }


def get_default_config():
    """生成默认配置（原始7因子启用 + 冲突惩罚/因子预期缩放/市场环境）"""
    active = list(LEGACY_FACTOR_NAMES)
    factor_configs = {}
    for name in active:
        factor_configs[name] = get_default_factor_config(name)
    return {
        'preset': 'default',
        'active_factors': active,
        'factor_configs': factor_configs,
    }


def calc_factor_value(name, ctx, params=None):
    """计算单个因子值，因子不存在或异常返回 0.0。

    兼容性修复：UI/JSON 保存的参数常以 float 形式存储（如 period=14.0），
    而底层指标函数多使用 range()/切片等需要 int 的运算，会导致异常并被静默吞掉，
    最终因子 raw=0、z-score 被误判为极弱/极强。此处先把整数值的 float 参数强转 int。
    """
    fdef = REGISTRY.get(name)
    if not fdef:
        return 0.0
    try:
        normalized = {}
        for k, v in (params or {}).items():
            if isinstance(v, float) and v.is_integer():
                v = int(v)
            normalized[k] = v
        return float(fdef.calc_fn(ctx, normalized))
    except Exception as e:
        logger.warning(f"计算因子 {name} 时异常（params={params}）: {type(e).__name__}: {e}")
        return 0.0


def calc_all_active_factors(active_factors, ctx, factor_configs):
    """计算所有启用因子的值

    Args:
        active_factors: list[str] 启用的因子名
        ctx: dict 计算上下文
        factor_configs: dict 每个因子的配置（含 params）

    Returns:
        dict: {factor_name: float}
    """
    result = {}
    for name in active_factors:
        cfg = factor_configs.get(name, {})
        params = cfg.get('params', {})
        result[name] = calc_factor_value(name, ctx, params)
    return result


# ============================================================
# 自定义公式因子（用户自建指标）
# ============================================================

CUSTOM_PREFIX = 'cf_'
_CUSTOM_SIG = {}   # name -> (formula, direction, weight, stats) 签名，用于增量重编译


def custom_factor_name(label):
    """由用户填写的指标名生成内部因子名（加前缀，避免与内置英文因子冲突）。"""
    return CUSTOM_PREFIX + str(label or '').strip()


def is_custom_factor(name):
    return isinstance(name, str) and name.startswith(CUSTOM_PREFIX)


def register_custom_factor(name, label, calc_fn, direction=1, weight=1.0, stats=None, desc=''):
    """把编译好的自定义公式注册为因子（与内置因子同构，打分链路自动生效）。"""
    REGISTRY[name] = FactorDef(
        name=name, label=label, category='custom', calc_fn=calc_fn,
        params_schema={},
        default_weight=float(weight), default_direction=int(direction),
        default_stats=dict(stats or {'mean': 0.0, 'std': 1.0}),
        ic=None, desc=desc or '自定义公式因子',
        explanations=(f'{label} 偏多', f'{label} 偏空'),
    )
    return REGISTRY[name]


def unregister_custom_factor(name):
    REGISTRY.pop(name, None)
    _CUSTOM_SIG.pop(name, None)


def sync_custom_factors(factor_configs):
    """按 factor_configs 中 custom=True 的条目编译并注册自定义因子。

    幂等：公式/方向/权重/统计未变的因子跳过重编译；配置里已删除的自定义因子会被移除。
    编译失败的因子保留原定义（若有）并记日志，不抛异常（不阻断配置加载）。

    Returns:
        dict: {name: error_message} 编译失败的因子（供 UI 提示）
    """
    from engine import custom_factor

    wanted = {}
    for name, fc in (factor_configs or {}).items():
        if not isinstance(fc, dict) or not fc.get('custom'):
            continue
        formula = fc.get('formula')
        if not isinstance(formula, str) or not formula.strip():
            continue
        wanted[name] = fc

    # 移除配置中已不存在的自定义因子
    for name in [n for n in list(REGISTRY.keys())
                 if is_custom_factor(n) and n not in wanted]:
        unregister_custom_factor(name)

    errors = {}
    for name, fc in wanted.items():
        formula = fc.get('formula')
        label = fc.get('label') or name[len(CUSTOM_PREFIX):]
        direction = fc.get('direction', 1)
        weight = fc.get('weight', 1.0)
        stats = fc.get('stats') or {'mean': 0.0, 'std': 1.0}
        sig = (formula, direction, weight, stats.get('mean'), stats.get('std'))
        if _CUSTOM_SIG.get(name) == sig and name in REGISTRY:
            continue
        try:
            calc_fn = custom_factor.compile_to_factor(formula)
        except Exception as e:
            errors[name] = str(e)
            logger.warning("自定义因子 %s 编译失败：%s", name, e)
            continue
        register_custom_factor(name, label, calc_fn, direction=direction,
                               weight=weight, stats=stats)
        _CUSTOM_SIG[name] = sig
    return errors


def get_custom_factor_names():
    """返回当前已注册的自定义因子名列表（稳定排序）。"""
    return sorted(n for n in REGISTRY if is_custom_factor(n))


def is_legacy_config(config):
    """检测是否为旧格式配置（有 factor_weights 但无 active_factors）"""
    return 'factor_weights' in config and 'active_factors' not in config


def migrate_legacy_config(config):
    """将旧格式配置迁移为新格式

    旧格式: {factor_weights: {...}, factor_direction: {...}, factor_stats: {...}, ...}
    新格式: {active_factors: [...], factor_configs: {name: {weight,direction,params,stats}}, ...}
    """
    legacy_weights = config.get('factor_weights', {})
    legacy_direction = config.get('factor_direction', {})
    legacy_stats = config.get('factor_stats', {})

    active = [name for name in LEGACY_FACTOR_NAMES if name in legacy_weights]
    factor_configs = {}
    for name in active:
        fdef = REGISTRY.get(name)
        if not fdef:
            continue
        params = {k: v['default'] for k, v in fdef.params_schema.items()}
        factor_configs[name] = {
            'weight': legacy_weights.get(name, fdef.default_weight),
            'direction': legacy_direction.get(name, fdef.default_direction),
            'params': params,
            'stats': legacy_stats.get(name, dict(fdef.default_stats)),
        }

    migrated = {
        'preset': config.get('preset', 'custom'),
        'active_factors': active,
        'factor_configs': factor_configs,
        'conflict_penalty': config.get('conflict_penalty'),
        'score_scale': config.get('score_scale'),
    }
    # 移除 None 值
    return {k: v for k, v in migrated.items() if v is not None}
