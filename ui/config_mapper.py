# -*- coding: utf-8 -*-
"""quantData（前端视图模型）<-> quantModel（后端 config dict）互转。

从 web_api.py 顶部收敛出的模块级纯辅助函数与常量：
- 默认 quantData 结构 `_default_quant_data`
- 后端 config ↔ 前端 quantData 映射 `_config_to_quant_data` / `_quant_data_to_config`
- 保存前校验 `_validate_quant_before_save`、数值归一化 `_norm_num`
- % 值互转 `_to_pct_val(_from_pct_val)`、因子行构造 `_factor_default_params(_to_factor)`
- 方向/指数信号映射常量 `_SIGNAL_*` / `_CIRCUIT_*`

设计约束：
- 本模块只依赖 engine 纯逻辑（quant_config / factor_registry），不 import ui，可单测。
- 无任何 `self`/类状态依赖，web_api.py 仅 import 转发，保证调用点行为零回归。
"""

import copy

from engine import quant_config
from engine.factor_registry import REGISTRY


def _default_veto_options(direction='long'):
    """从 veto_registry 注册表派生否决项选项（含 6 大场景分组 + 默认阈值 + 阈值 schema）。
    与引擎唯一权威对齐：新增否决项只需在注册表加一条，此处自动随之扩展。"""
    from engine import veto_registry
    out = []
    for v in veto_registry.get_veto_defs(direction):
        out.append({
            'key': v.key,
            'label': v.label,
            'scene': getattr(v, 'scene', 'extreme'),
            'desc': v.desc,
            'params': dict(v.defaults),
            'params_schema': dict(v.params_schema or {}),
        })
    return out


def _default_quant_data():
    """返回与 index.html 中 quantData 初始值一致的结构。"""
    categories = [
        {'key': 'momentum', 'label': '动量类'},
        {'key': 'trend', 'label': '均线/趋势类'},
        {'key': 'volume', 'label': '量价类'},
        {'key': 'volatility', 'label': '波动类'},
        {'key': 'pattern', 'label': '形态/结构类'},
        {'key': 'custom', 'label': '自定义指标'},
    ]

    factor_rows = [
        # 动量类
        ('relative_strength_20d', '20日相对强度', 'momentum', 0.167, 1, 0.02, 0.15, {'period': 20}, 'N日涨幅，衡量近期动量强度'),
        ('rsi_value', 'RSI', 'momentum', 0.10, 1, 50, 15, {'period': 14}, '相对强弱指标（v2校准：高RSI预测正收益，方向+1）'),
        ('macd_hist_norm', 'MACD柱状图', 'momentum', 0.123, -1, 0.0, 0.02, {'fast': 12, 'slow': 26, 'signal': 9}, 'MACD柱状图归一化值（v2校准：IC为负，方向-1）'),
        ('kdj_signal', 'KDJ信号', 'momentum', 0.10, 1, 0.0, 10.0, {'period': 9}, 'KDJ的K-D差值，正值表示金叉区域'),
        # 均线/趋势类
        ('ma_slope', 'MA20斜率', 'trend', 0.155, 1, 0.0, 0.03, {'period': 20, 'lookback': 5}, '均线斜率，衡量趋势方向'),
        ('di_spread', 'DI方向运动', 'trend', 0.146, 1, 0.0, 15.0, {'period': 14}, '+DI与-DI差值，衡量多空力量对比'),
        ('ma_arrangement', '均线排列', 'trend', 0.10, 1, 0.0, 2.0, {'period': 20}, '均线多空排列强度，+3完美多头到-3完美空头'),
        ('bias_value', '乖离率', 'trend', 0.10, -1, 0.0, 5.0, {'period': 20}, '价格偏离均线的百分比，过大有回归需求'),
        ('adx_trend_strength', 'ADX趋势强度', 'trend', 0.10, 1, 25.0, 10.0, {'period': 14}, 'ADX值衡量趋势强度，>25为趋势市'),
        # 量价类
        ('volume_ratio', '量比', 'volume', 0.10, -1, 1.0, 0.5, {'period': 5}, '当日成交量与近期均量之比'),
        ('obv_trend', 'OBV趋势', 'volume', 0.10, 1, 0.0, 20.0, {'ma_period': 10}, 'OBV变化率，衡量资金流入流出'),
        ('volume_price_signal', '量价关系', 'volume', 0.10, 1, 0.0, 2.0, {}, '量价配合度，+2配合上涨到-2配合下跌'),
        # 波动类
        ('bb_bandwidth', '布林带带宽', 'volatility', 0.149, 1, 15.0, 10.0, {'period': 20}, '布林带宽度，衡量波动率水平'),
        ('atr_norm', 'ATR波动率', 'volatility', 0.10, 1, 0.02, 0.01, {'period': 14}, 'ATR占价格比例，归一化波动率'),
        ('current_drawdown', '当前回撤', 'volatility', 0.119, -1, 8.0, 12.0, {'lookback': 250}, '当前价格相对高点的回撤幅度%'),
        ('volatility_cone', '波动率锥位置', 'volatility', 0.10, -1, 50.0, 25.0, {'period': 20}, '20日波动率在历史分位数，高位注意风险'),
        # 形态/结构类
        ('pattern_reverse', 'K线形态(反向)', 'pattern', 0.091, -1, 0.0, 1.5, {}, 'K线形态反向分，看涨形态反而易跌'),
        ('doji', '十字星(变盘)', 'pattern', 0.08, -1, 0.1, 0.3, {}, '十字星变盘信号，方向不明，压低置信度(弱浮动)'),
        ('fib_position', '斐波那契位置', 'pattern', 0.10, 1, 0.5, 0.25, {'lookback': 120}, '当前价在斐波那契回撤区的位置'),
        ('pivot_distance', '轴心点距离', 'pattern', 0.10, 1, 0.0, 5.0, {}, '当前价相对枢轴点的偏离百分比'),
        ('chip_concentration', '筹码集中度', 'pattern', 0.10, 1, 0.3, 0.2, {'bins': 50}, '筹码密集区成交量占比'),
    ]

    factors = []
    for name, label, cat, weight, dir_, mean, std, params, desc in factor_rows:
        fdef = REGISTRY.get(name)
        ic = getattr(fdef, 'ic', None) if fdef else None
        ic_ir = getattr(fdef, 'ic_ir', None) if fdef else None
        factors.append({
            'name': name, 'label': label, 'cat': cat, 'enabled': False,
            'weight': weight, 'dir': dir_, 'ic': ic, 'ic_ir': ic_ir,
            'mean': mean, 'std': std, 'params': dict(params), 'desc': desc,
        })

    return {
        'categories': categories,
        'factors': factors,
        'scale': [
            {'key': 'weight_multiplier', 'label': '权重乘数', 'val': 1.0, 'desc': '放大因子预期分的灵敏度'},
            {'key': 'z_truncate_min', 'label': 'z-score下限', 'val': -3.0, 'desc': '截断过小的 z 值'},
            {'key': 'z_truncate_max', 'label': 'z-score上限', 'val': 3.0, 'desc': '截断过大的 z 值'},
            {'key': 'score_min', 'label': '因子预期下限', 'val': 0.0, 'desc': '因子预期分下限'},
            {'key': 'score_max', 'label': '因子预期上限', 'val': 100.0, 'desc': '因子预期分上限'},
        ],
        'penalty': [
            {'key': 'volume_down_penalty', 'label': '放量下跌惩罚', 'val': '', 'desc': '负值，越小扣分越多'},
            {'key': 'volume_up_shrink_penalty', 'label': '缩量上涨惩罚', 'val': '', 'desc': '负值，越小扣分越多'},
            {'key': 'deep_drawdown_div_bonus', 'label': '深度回撤+底背离加分', 'val': '', 'desc': '正值，越大加分越多'},
            {'key': 'deep_drawdown_threshold', 'label': '深度回撤阈值(%)', 'val': '', 'desc': '回撤超过此值触发加分'},
            {'key': 'extreme_drawdown_bonus', 'label': '极端回撤加分', 'val': '', 'desc': '正值，越大加分越多'},
            {'key': 'extreme_drawdown_threshold', 'label': '极端回撤阈值(%)', 'val': '', 'desc': '回撤超过此值触发加分'},
            {'key': 'cluster_proximity_penalty', 'label': '密集成交区接近惩罚', 'val': '', 'desc': '追高/杀跌贴近密带扣分(负值)'},
            {'key': 'cluster_proximity_pct', 'label': '密带接近阈值(%)', 'val': '', 'desc': '距密带边界小于该百分比即视为"靠近"（如0.5）'},
            {'key': 'penalty_min', 'label': '惩罚下限', 'val': '', 'desc': '总惩罚不低于此值'},
            {'key': 'penalty_max', 'label': '惩罚上限', 'val': '', 'desc': '总惩罚不高于此值'},
        ],
        'thresholds': [
            {'key': 'strong', 'label': '强势线', 'dir': '≥', 'signal': '无要求', 'val': 22,
             'veto_on': True, 'veto_enabled': {k: True for k in quant_config.VETO_KEYS}},
            {'key': 'standard', 'label': '标准线', 'dir': '≥', 'signal': '多头排列', 'val': 20,
             'veto_on': True, 'veto_enabled': {k: True for k in quant_config.VETO_KEYS}},
            {'key': 'test', 'label': '试探线', 'dir': '≥', 'signal': '站上MA20', 'val': 10,
             'veto_on': True, 'veto_enabled': {k: True for k in quant_config.VETO_KEYS}},
            {'key': 'pending', 'label': '观望线', 'dir': '≥', 'signal': '无要求', 'val': -5,
             'veto_on': True, 'veto_enabled': {k: True for k in quant_config.VETO_KEYS}},
            {'key': 'rebound', 'label': '博反弹', 'dir': '<', 'signal': '—', 'val': -2,
             'veto_on': False, 'veto_enabled': {}},
            {'key': 'panic_rebound', 'label': '恐慌线', 'dir': '<', 'signal': '—', 'val': -8,
             'veto_on': False, 'veto_enabled': {}},
            # 空单专属「顶部回落」（涨势猛/超买 + 顶部反转 → 做空），多单模式 UI 隐藏；否决沿用 SHORT_VETO_KEYS
            {'key': 'top_reversal', 'label': '顶部回落', 'dir': '<', 'signal': '—', 'val': -22,
             'veto_on': True, 'veto_enabled': {k: False for k in quant_config.SHORT_VETO_KEYS}},
        ],
        'signalOptions': [
            '无要求', '多头排列', '空头排列', '站上MA5', '跌破MA5', '站上MA20',
            '跌破MA20', '站上MA60', '跌破MA60', 'KDJ金叉', 'KDJ死叉', 'MACD金叉',
            'MACD死叉', '均线金叉', '均线死叉', '布林带上轨突破', '布林带下轨突破',
            'ADX趋势确认', '放量上涨', '放量下跌'
        ],
        'thresholdResonance': {'significantThreshold': 5.0, 'starCutoffs': [0.2, 0.4, 0.6, 0.8]},
        'scanFilter': [],  # 全市场扫描技术指标筛选条件（AND 组合，随方案保存；空=不筛选）
        'vetoOptions': _default_veto_options('long'),
        'entryPos': [
            {'key': 'strong', 'label': '强势', 'val': 0.20}, {'key': 'standard', 'label': '标准', 'val': 0.15},
            {'key': 'test', 'label': '试探', 'val': 0.08}, {'key': 'observe', 'label': '观望', 'val': 0.03},
            {'key': 'none', 'label': '空仓', 'val': 0.00}, {'key': 'rebound', 'label': '博反弹', 'val': 0.05},
            {'key': 'panic_rebound', 'label': '恐慌反转', 'val': 0.02},
            {'key': 'top_reversal', 'label': '顶部回落', 'val': 0.05},
        ],
        'reversal_score_threshold': 4.0,
        'panic_reversal_threshold': 5.0,
        'marketGate': [
            {'key': 'panic', 'label': '恐慌', 'factor': '', 'limit': ''},
            {'key': 'weak', 'label': '弱势', 'factor': '', 'limit': ''},
            {'key': 'neutral', 'label': '中性', 'factor': '', 'limit': ''},
            {'key': 'strong', 'label': '强势', 'factor': '', 'limit': ''},
            {'key': 'overheat', 'label': '过热', 'factor': '', 'limit': ''},
        ],
        'posTypes': [
            {'key': 'strong_standard', 'label': '强势/标准股'},
            {'key': 'test_pending', 'label': '试探/待确认股'},
            {'key': 'weak_rebound', 'label': '弱势/博反弹'},
            {'key': 'panic_rebound', 'label': '恐慌反转'},
        ],
        'volumeBreakoutMultiplier': 2.0,
        'maxAddPctPerStep': 30,
        'marketGateEnabled': False,
        'vetoParams': {},
        'addTriggerOptions': [
            {'code': None, 'label': '无'},
            {'code': 'MA5', 'label': '站上MA5'}, {'code': 'MA10', 'label': '站上MA10'}, {'code': 'MA20', 'label': '站上MA20'},
            {'code': 'PREV_HIGH', 'label': '突破前高'}, {'code': 'VOL_BREAK', 'label': '放量突破'},
            {'code': 'BREAKOUT_PCT', 'label': '突破前高%', 'param': '%', 'default': 3},
            {'code': 'DEV_UP', 'label': '乖离率走强%', 'param': '%', 'default': 8},
            {'code': 'RSI_LOW', 'label': 'RSI超卖', 'param': 'RSI', 'default': 30},
            {'code': 'MACD_GOLD', 'label': 'MACD金叉'}, {'code': 'MA_GOLD', 'label': '均线金叉'},
            {'code': 'KDJ_GOLD', 'label': 'KDJ金叉'}, {'code': 'ADX_TREND', 'label': 'ADX趋势'},
            {'code': 'OBV_GOLD', 'label': 'OBV金叉'}, {'code': 'BB_BREAKOUT', 'label': '布林突破'},
        ],
        'reduceTriggerOptions': [
            {'code': None, 'label': '无'},
            {'code': 'MA5', 'label': '跌破MA5'}, {'code': 'MA10', 'label': '跌破MA10'}, {'code': 'MA20', 'label': '跌破MA20'},
            {'code': 'ATR_LOWER', 'label': '跌破ATR下轨'}, {'code': 'PREV_LOW', 'label': '跌破前低'},
            {'code': 'DROP_PCT', 'label': '跌幅%', 'param': '%', 'default': 3},
            {'code': 'PULLBACK_PCT', 'label': '跌破前低%', 'param': '破%', 'default': 3},
            {'code': 'DEV_OVER', 'label': '乖离率超买%', 'param': '乖%', 'default': 15},
            {'code': 'RSI_HIGH', 'label': 'RSI超买', 'param': 'RSI', 'default': 70},
            {'code': 'MACD_DEAD', 'label': 'MACD死叉'}, {'code': 'MA_DEAD', 'label': '均线死叉'},
            {'code': 'KDJ_DEAD', 'label': 'KDJ死叉'}, {'code': 'BB_BREAKDOWN', 'label': '布林跌破'},
        ],
        'reduceActions': ['减仓', '清仓'],
        'addTiers': {
            'strong_standard': {'t1': {'codes': ['MA5'], 'params': {}, 'ratio': 20},
                                't2': {'codes': ['PREV_HIGH'], 'params': {}, 'ratio': 20},
                                't3': {'codes': [], 'params': {}, 'ratio': 20}},
            'test_pending': {'t1': {'codes': ['MA10'], 'params': {}, 'ratio': 15},
                             't2': {'codes': [], 'params': {}, 'ratio': 15},
                             't3': {'codes': [], 'params': {}, 'ratio': 15}},
            'weak_rebound': {'t1': {'codes': ['MA20'], 'params': {}, 'ratio': 15},
                             't2': {'codes': [], 'params': {}, 'ratio': 15},
                             't3': {'codes': [], 'params': {}, 'ratio': 15}},
            'panic_rebound': {'t1': {'codes': ['VOL_BREAK'], 'params': {}, 'ratio': 20},
                              't2': {'codes': [], 'params': {}, 'ratio': 20},
                              't3': {'codes': [], 'params': {}, 'ratio': 20}},
        },
        'reduceTiers': {
            'strong_standard': {'t1': {'codes': ['MA5'], 'params': {}, 'action': '减仓', 'ratio': 20},
                                't2': {'codes': ['MA10'], 'params': {}, 'action': '减仓', 'ratio': 20},
                                't3': {'codes': ['MA20'], 'params': {}, 'action': '清仓', 'ratio': 100}},
            'test_pending': {'t1': {'codes': ['MA10'], 'params': {}, 'action': '减仓', 'ratio': 30},
                             't2': {'codes': ['MA20'], 'params': {}, 'action': '清仓', 'ratio': 100},
                             't3': {'codes': [], 'params': {}, 'action': '清仓', 'ratio': 100}},
            'weak_rebound': {'t1': {'codes': ['ATR_LOWER'], 'params': {}, 'action': '减仓', 'ratio': 50},
                             't2': {'codes': ['PREV_LOW'], 'params': {}, 'action': '清仓', 'ratio': 100},
                             't3': {'codes': [], 'params': {}, 'action': '清仓', 'ratio': 100}},
            'panic_rebound': {'t1': {'codes': ['DROP_PCT'], 'params': {'DROP_PCT': 0.03}, 'action': '减仓', 'ratio': 50},
                              't2': {'codes': ['DROP_PCT'], 'params': {'DROP_PCT': 0.05}, 'action': '清仓', 'ratio': 100},
                              't3': {'codes': [], 'params': {}, 'action': '清仓', 'ratio': 100}},
        },
        'reduceTop': [
            {'key': 'atr_mult', 'label': 'ATR止损倍数', 'desc': '回撤止损 = 近期高点 - ATR×倍数', 'enabled': True, 'suffix': '×', 'min': 0, 'max': 2, 'step': 0.1},
            {'key': 'recent_low_mult', 'label': '近低倍数', 'desc': '跌破近期低点×倍数触发减仓', 'enabled': True, 'suffix': '×', 'min': 0.5, 'max': 1, 'step': 0.01},
            {'key': 'lookback_days', 'label': '回看天数', 'desc': '计算近期高/低点的窗口', 'enabled': True, 'suffix': '天', 'min': 1, 'max': 120, 'step': 1},
            {'key': 'atr_fallback', 'label': 'ATR回退值', 'desc': '无ATR时按价格×此值估算', 'enabled': False, 'suffix': '', 'min': 0, 'max': 0.2, 'step': 0.01},
        ],
        'risk': [
            {'key': 'total_position_cap_pct', 'label': '总仓位上限(%)', 'val': '', 'pct': True, 'desc': '所有持仓合计不超过净值的此比例（留空=不启用；50=50%）'},
            {'key': 'max_single_position', 'label': '单只最大仓位(%)', 'val': '', 'pct': True, 'desc': '单只股票投入不超过净值的此比例（留空=不启用；30=30%）'},
            {'key': 'time_stop_days', 'label': '时间止损观察天数', 'val': '', 'desc': '持仓达到此天数后，若收益未达目标则离场（留空=不启用）'},
            {'key': 'time_stop_min_profit_pct', 'label': '时间止损目标收益(%)', 'val': '', 'pct': True, 'desc': '观察期内应达到的最小收益率（留空=不启用；5=5%）'},
            {'key': 'single_max_loss_pct', 'label': '单笔最大亏损(%)', 'val': '', 'pct': True, 'desc': '浮亏超此值强制离场（留空=不启用；8=8%）'},
            {'key': 'market_crash_pct', 'label': '大盘熔断暂停(%)', 'val': '', 'pct': True, 'desc': '宽基指数单日跌幅超此值暂停开新仓（留空=不启用）'},
        ],
        'circuitBreakerIndex': '沪深300',
        'backtest': {'universe': '上证50+创业50+科创50', 'forwardDays': 30, 'scanInterval': 5, 'outputPrefix': 'bt',
                     'futPool': 'all', 'futDir': 'long', 'futPeriod': '日K', 'futDays': 300, 'futMultiHorizon': False},
        'ghost': [
            {'key': 'grace_period_days', 'label': '规则一宽限期', 'suffix': '天', 'hint': '持仓满此天数才判定清仓；期内任意波动不误杀', 'val': ''},
            {'key': 'profit_threshold', 'label': '规则一证伪阈值', 'suffix': '%', 'hint': '宽限期后全程最高浮盈 < 此值 → 判清仓参考', 'val': ''},
            {'key': 'add_profit_threshold', 'label': '规则二加仓浮盈阈值', 'suffix': '%', 'hint': '浮盈 > 此值 且趋势确认 → 加仓参考', 'val': ''},
            {'key': 'rsi_confirm', 'label': '加仓 RSI 动量门', 'suffix': '', 'hint': '加仓前要求 RSI(14) > 此值，过滤弱势反弹（45 位于超卖线之上）', 'val': ''},
            {'key': 'ma_confirm', 'label': '加仓确认均线', 'suffix': '', 'hint': '价格站上该均线才确认趋势（sma_20 默认；切 ema_20 更灵敏）', 'val': '',
             'isSelect': True, 'options': ['sma_5', 'sma_10', 'sma_20', 'sma_60', 'sma_120', 'sma_250', 'ema_5', 'ema_10', 'ema_20', 'ema_60', 'ema_120', 'ema_250']},
        ],
        'ghostDefaults': {'grace_period_days': 3, 'profit_threshold': 0.5, 'add_profit_threshold': 3, 'rsi_confirm': 45, 'ma_confirm': 'sma_20'},
    }


# ═══════════════════════════════════════════════════════════
# 配置映射：backend config <-> frontend quantData
# ═══════════════════════════════════════════════════════════
_SIGNAL_BACK_TO_FRONT = {
    'none': '无要求',
    'bull_align': '多头排列', 'bear_align': '空头排列',
    'above_ma5': '站上MA5', 'below_ma5': '跌破MA5',
    'above_ma20': '站上MA20', 'below_ma20': '跌破MA20',
    'above_ma60': '站上MA60', 'below_ma60': '跌破MA60',
    'kdj_gold': 'KDJ金叉', 'kdj_dead': 'KDJ死叉',
    'macd_gold': 'MACD金叉', 'macd_dead': 'MACD死叉',
    'ma_gold': '均线金叉', 'ma_dead': '均线死叉',
    'bb_breakout': '布林带上轨突破', 'bb_breakdown': '布林带下轨突破',
    'adx_trend': 'ADX趋势确认', 'volume_up': '放量上涨', 'volume_down': '放量下跌',
}
_SIGNAL_FRONT_TO_BACK = {v: k for k, v in _SIGNAL_BACK_TO_FRONT.items()}


def _signal_cond_to_config(cond):
    """把前端 AND 组合里的单个元素转成引擎可读结构。
    {signal: 中文label} → {signal: 英文code}；数值阈值条件({indicator/op/value})原样保留。"""
    if not isinstance(cond, dict):
        return cond
    out = dict(cond)
    if 'signal' in cond:
        _s = str(cond['signal'])
        out['signal'] = _SIGNAL_FRONT_TO_BACK.get(_s, _s or 'none')
    return out


def _signal_cond_to_front(cond):
    """把引擎的 AND 组合单元素转回前端可读结构（英文 code → 中文 label）。"""
    if not isinstance(cond, dict):
        return cond
    out = dict(cond)
    if 'signal' in cond:
        _s = str(cond['signal'])
        out['signal'] = _SIGNAL_BACK_TO_FRONT.get(_s, _s)
    return out

_CIRCUIT_INDEX_NAME = {
    'sh000300': '沪深300', 'sh000001': '上证指数', 'sz399001': '深证成指',
    'sh000985': '中证全指', 'sz399006': '创业板指', 'sh000016': '上证50',
}
_CIRCUIT_NAME_TO_CODE = {v: k for k, v in _CIRCUIT_INDEX_NAME.items()}


def _to_pct_val(v):
    """把前端 % 数值转成后端 0-1 小数；空字符串/None 返回 0。"""
    if v is None or v == '':
        return 0
    try:
        return float(v) / 100.0
    except (ValueError, TypeError):
        return 0


def _from_pct_val(v):
    """把后端 0-1 小数转成前端 % 数值；None/0 返回 ''。"""
    if v is None:
        return ''
    try:
        fv = float(v)
        if fv == 0:
            return ''
        return round(fv * 100, 1)
    except (ValueError, TypeError):
        return ''


def _factor_default_params(name):
    """按因子注册表 params_schema 的默认值回填缺失键，供前端渲染参数输入框。
    无 schema（无参数因子）返回空 dict；已配置过的键保留配置值不覆盖。"""
    fdef = REGISTRY.get(name)
    schema = (fdef.params_schema if fdef else None) or {}
    return {k: (v.get('default') if isinstance(v, dict) else None) for k, v in schema.items()}


def _to_factor(fcfg):
    """把后端 factor_configs 单项转成前端 factor 行。"""
    name = fcfg.get('name')
    fdef = REGISTRY.get(name)
    label = fdef.label if fdef else name
    cat = fdef.category if fdef else 'momentum'
    stats = fcfg.get('stats') or {}
    return {
        'name': name, 'label': label, 'cat': cat,
        'enabled': False, 'weight': fcfg.get('weight', 0),
        'dir': fcfg.get('direction', 1),
        'ic': fcfg.get('ic'), 'ic_ir': fcfg.get('ic_ir'),
        'mean': stats.get('mean', 0), 'std': stats.get('std', 1),
        'params': {**_factor_default_params(name), **dict(fcfg.get('params', {}) or {})},
        'desc': fdef.desc if fdef else '',
    }


def _config_to_quant_data(cfg, direction='long'):
    """把后端 config dict 映射为前端 quantData 结构。

    direction：'short'（空单）时否决项键集取 SHORT_VETO_KEYS（超卖/突破，语义镜像），
        与保存路径 _quant_data_to_config 的 veto_keys_for_level 对称；
        空单视图不残留多单的顶部过热 key（RSI≥85 等），避免误显/误勾。
    """
    data = _default_quant_data()
    if not isinstance(cfg, dict):
        return data

    # 因子
    active = set(cfg.get('active_factors') or [])
    fconfigs = cfg.get('factor_configs') or {}
    for f in data['factors']:
        fcfg = fconfigs.get(f['name'])
        if isinstance(fcfg, dict):
            f['enabled'] = f['name'] in active
            f['weight'] = fcfg.get('weight', f['weight'])
            f['dir'] = fcfg.get('direction', f['dir'])
            f['ic'] = fcfg.get('ic', f['ic'])
            f['ic_ir'] = fcfg.get('ic_ir', f['ic_ir'])
            stats = fcfg.get('stats') or {}
            f['mean'] = stats.get('mean', f['mean'])
            f['std'] = stats.get('std', f['std'])
            f['params'] = {**_factor_default_params(f['name']), **dict(fcfg.get('params', f['params']) or {})}
        else:
            f['enabled'] = f['name'] in active

    # 自定义公式因子：内置行是硬编码清单，这里把方案里 custom=True 的因子追加进前端列表
    # （分类=自定义指标），使它们在因子权重表里可调权、可启停。
    for _name, _fc in fconfigs.items():
        if not isinstance(_fc, dict) or not _fc.get('custom'):
            continue
        _stats = _fc.get('stats') or {}
        data['factors'].append({
            'name': _name,
            'label': _fc.get('label') or _name[3:],
            'cat': 'custom',
            'enabled': _name in active,
            'weight': _fc.get('weight', 1.0),
            'dir': _fc.get('direction', 1),
            'ic': _fc.get('ic'), 'ic_ir': _fc.get('ic_ir'),
            'mean': _stats.get('mean', 0), 'std': _stats.get('std', 1),
            'params': {},
            'desc': '自定义公式因子',
            'custom': True,
            'formula': _fc.get('formula') or '',
        })

    # 评分缩放
    ss = cfg.get('score_scale') or {}
    scale_map = {
        'weight_multiplier': ss.get('weight_multiplier', 1.0),
        'z_truncate_min': ss.get('z_truncate_min', -3.0),
        'z_truncate_max': ss.get('z_truncate_max', 3.0),
        'score_min': ss.get('score_min', 0.0),
        'score_max': ss.get('score_max', 100.0),
    }
    for s in data['scale']:
        s['val'] = scale_map.get(s['key'], s['val'])

    # 冲突惩罚
    cp = cfg.get('conflict_penalty') or {}
    for p in data['penalty']:
        v = cp.get(p['key'])
        p['val'] = '' if v is None else v

    # 阈值 + 技术信号 + 否决项
    thresholds = cfg.get('thresholds') or {}
    entry_conditions = cfg.get('entry_conditions') or {}
    # 否决项集合按方向区分：多单=VETO_KEYS（顶部过热），空单=SHORT_VETO_KEYS（超卖/突破，语义镜像）
    _cfg_short = (direction == 'short')
    _veto_keys = quant_config.SHORT_VETO_KEYS if _cfg_short else quant_config.VETO_KEYS
    for t in data['thresholds']:
        ec = entry_conditions.get(t['key'], {}) if isinstance(entry_conditions, dict) else {}
        t['val'] = thresholds.get(t['key'], t['val'])
        sig = ec.get('tech_signal', 'none')
        # tech_signal 支持多条件 AND 组合(list)；回源时把元素内英文 code 换回前端可读
        if isinstance(sig, list):
            t['signal'] = [_signal_cond_to_front(c) for c in sig] if sig else '无要求'
        else:
            t['signal'] = _SIGNAL_BACK_TO_FRONT.get(sig, sig) if sig else '无要求'
        t['veto_on'] = bool(ec.get('veto_on', t['veto_on']))
        veto_enabled = ec.get('veto_enabled') or {}
        # 重建为方向对应键集，避免残留另一方向的 key（多单顶部信号在空单视图下误显/误勾）
        t['veto_enabled'] = {vk: bool(veto_enabled.get(vk, False)) for vk in _veto_keys}

    # 否决项阈值参数（方案级全局一套；前端编辑面板读入，缺失为空{}）
    data['vetoParams'] = cfg.get('veto_params') or {}

    # 技术共振
    tr = cfg.get('tech_resonance') or {}
    data['thresholdResonance'] = {
        'significantThreshold': tr.get('threshold', 5.0),
        'starCutoffs': list(tr.get('bands', [0.2, 0.4, 0.6, 0.8]))
    }

    # 建仓仓位
    ep = cfg.get('entry_params') or {}
    positions = ep.get('positions') or {}
    for p in data['entryPos']:
        p['val'] = positions.get(p['key'], p['val']) or 0
    data['reversal_score_threshold'] = ep.get('reversal_score_threshold', 4.0) or 4.0
    data['panic_reversal_threshold'] = ep.get('panic_reversal_threshold', 5.0) or 5.0

    # 初级模式建仓已升级为「状态分界技术分级」：按技术条件从高到低定档，命中哪档用该档比例。
    # 旧「触发指标 + 统一比例」单行(basic_entry)已废弃，不再读写。

    # 市场门控
    mg = cfg.get('market_gate') or {}
    data['marketGateEnabled'] = bool(mg.get('enabled'))
    envs = mg.get('envs') or {}
    for g in data['marketGate']:
        e = envs.get(g['key'], {}) if isinstance(envs, dict) else {}
        g['factor'] = e.get('factor', '') if e.get('factor') is not None else ''
        g['limit'] = e.get('limit', '') if e.get('limit') is not None else ''

    # 加仓参数
    addp = cfg.get('add_params') or {}
    data['volumeBreakoutMultiplier'] = addp.get('vol_mult', 2.0) or 2.0
    data['maxAddPctPerStep'] = _from_pct_val(addp.get('max_add_ratio', 0.3))
    add_tiers = addp.get('add_tiers') or {}
    for row_key, tiers in data['addTiers'].items():
        src = add_tiers.get(row_key) or {}
        for tk in ('t1', 't2', 't3'):
            st = src.get(tk)
            if isinstance(st, dict):
                trig = st.get('triggers') or st.get('trigger')
                tiers[tk] = {
                    'codes': [trig] if isinstance(trig, str) else list(trig or []),
                    'params': dict(st.get('params', {})),
                    'ratio': _from_pct_val(st.get('ratio', 0)),
                }
            else:
                tiers[tk] = {'codes': [], 'params': {}, 'ratio': 0}

    # 减仓参数
    redp = cfg.get('reduce_params') or {}
    for rt in data['reduceTop']:
        rt['val'] = redp.get(rt['key'], '')
        # 如果配置里有该参数，说明已启用；否则保持前端默认
        if rt['key'] in redp:
            rt['enabled'] = True
    reduce_tiers = redp.get('reduce_tiers') or {}
    for row_key, tiers in data['reduceTiers'].items():
        src = reduce_tiers.get(row_key) or {}
        for tk in ('t1', 't2', 't3'):
            st = src.get(tk)
            if isinstance(st, dict):
                trig = st.get('triggers') or st.get('trigger')
                tiers[tk] = {
                    'codes': [trig] if isinstance(trig, str) else list(trig or []),
                    'params': dict(st.get('params', {})),
                    'action': '减仓' if st.get('action') == 'reduce' else '清仓',
                    'ratio': _from_pct_val(st.get('ratio', 0)),
                }
            else:
                tiers[tk] = {'codes': [], 'params': {}, 'action': '清仓', 'ratio': 100}

    # 风控（留空=不启用：空值或0均回显为空，与保存路径对称）
    rp = cfg.get('risk_params') or {}
    for r in data['risk']:
        raw = rp.get(r['key'])  # 缺失键 = 未配置
        if raw is None:
            r['val'] = ''
        elif r.get('pct'):
            r['val'] = _from_pct_val(raw)
        else:
            # 非pct字段：0或负值视为不启用，回显为空
            try:
                r['val'] = '' if float(raw) <= 0 else raw
            except (ValueError, TypeError):
                r['val'] = ''
    idx = rp.get('market_crash_index', 'sh000300')
    data['circuitBreakerIndex'] = _CIRCUIT_INDEX_NAME.get(idx, idx)

    # 回测设置不随方案加载（2026-09-19 用户决策，与保存路径对称）
    # 回测面板始终使用前端默认值；历史方案遗留的 config['backtest'] 不再回填，
    # 避免「切换方案后回测池/年数被上一个方案改动带走」。

    # 幽灵规则
    gr = cfg.get('ghost_rules') or {}
    for g in data['ghost']:
        g['val'] = gr.get(g['key'], '')
        # 加仓 RSI 动量门 hint 随方向适配（引擎 ghost_engine 已按 is_short 翻转判定）
        if g['key'] == 'rsi_confirm':
            g['hint'] = ('加仓前要求 RSI(14) < 此值，过滤弱势反抽（空头加仓在超卖后动量衰竭时更优）'
                         if direction == 'short' else
                         '加仓前要求 RSI(14) > 此值，过滤弱势反弹（45 位于超卖线之上）')
        elif g['key'] == 'ma_confirm':
            # 空单加仓确认均线：价格跌破均线才确认下跌趋势（引擎 ghost_engine 已按 is_short 翻转）
            g['hint'] = ('价格跌破该均线才确认下跌趋势（sma_20 默认；空头加仓要求趋势向下）'
                         if direction == 'short' else
                         '价格站上该均线才确认趋势（sma_20 默认；切 ema_20 更灵敏）')

    # 高级模式全市场扫描·技术指标二级筛选（AND 组合条件；随方案保存）
    data['scanFilter'] = cfg.get('scan_tech_filter') or []

    # 高级模式全市场扫描·财务筛选阈值（随方案保存；缺键给默认开启，保持爆雷护栏现状）
    _sff = cfg.get('scan_finance_filter')
    data['scanFinanceFilter'] = _sff if isinstance(_sff, dict) else {'enabled': True}

    return data


def _validate_quant_before_save(data, direction='long', skip_factor_check=False):
    """保存前校验 quantData（保存前最后一道闸，前端已校验的项在此复验）。

    校验项：
      1. 至少启用 1 个因子（决定 factor_configs 是否为空）——初级用法（因子休眠）跳过
      2. 阈值（strong/standard/test/pending/rebound/panic_rebound）均可解析为数字
      3. 因子预期分界线递减：strong > standard > test > pending
      4. 原始因子分界线：rebound > panic_rebound（越负越极端）

    初级用法（因子休眠）额外跳过 3 / 4 的阈值关系校验——初级纯按技术条件定档，
    阈值不参与判定，且前端已隐藏阈值输入，用户无从修改，关系校验会造成误拒。
    数值解析（校验项 2）仍保留，用于防写入脏数据。

    Returns:
        None  —— 校验通过；
        str   —— 校验失败的具体错误信息（供前端展示）。
    """
    # 1. 至少启用 1 个因子；初级用法无因子门槛（因子休眠，建仓/持有期只靠触发指标）
    if not skip_factor_check:
        active = [f['name'] for f in data.get('factors', []) if f.get('enabled')]
        if not active:
            return "请至少启用一个因子"

    # 2. 阈值可解析为数字（前端 val 可能是 number 或字符串，空/非法均视为缺失）
    tmap = {t.get('key'): t.get('val') for t in data.get('thresholds', [])}

    def _to_num(v):
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return float(v)
        s = str(v).strip()
        if not s:
            return None
        try:
            return float(s)
        except (ValueError, TypeError):
            return None

    # 空单模式：左侧抄底「博反弹/恐慌线」语义错误（空单视角负分区=多头强势区），
    # 改以空单专属「顶部回落」单档（涨势猛/超买 → 做空）；多单模式保持原 rebound/panic_rebound。
    is_short = (direction == 'short')
    if is_short:
        required = ('strong', 'standard', 'test', 'pending', 'top_reversal')
    else:
        required = ('strong', 'standard', 'test', 'pending', 'rebound', 'panic_rebound')
    nums = {}
    for k in required:
        if k not in tmap:
            return f"阈值输入无效: 缺少阈值字段 {k}"
        n = _to_num(tmap[k])
        if n is None:
            return f"阈值输入无效: {k} 含空值或非数字"
        nums[k] = n

    # 初级用法（因子休眠，纯技术定档）跳过阈值关系校验；高级保留
    if skip_factor_check:
        return None

    # 3 & 4. 阈值关系校验
    errors = []
    if not (nums['strong'] > nums['standard'] > nums['test'] > nums['pending']):
        errors.append(
            f"因子预期分界线必须递减: 强势({nums['strong']}) > 标准({nums['standard']}) "
            f"> 试探({nums['test']}) > 观望({nums['pending']})"
        )
    # 多单：原始因子分界线 博反弹 > 恐慌（越负越极端）；空单顶部回落为单档，无二级比较
    if not is_short:
        if not (nums['rebound'] > nums['panic_rebound']):
            errors.append(
                f"原始因子分界线: 博反弹({nums['rebound']}) 必须 > 恐慌({nums['panic_rebound']})（越负越极端）"
            )
    if errors:
        return "请修正以下问题:\n" + "\n".join(errors)

    return None


def _norm_num(val):
    """UI 数值收集归一化。
    整数值 float -> int，真小数保持 float，空/非法 -> None。
    从源头杜绝 '20' 被 float() 成 20.0 写入 json 的脏数据自我复制。
    """
    if val is None:
        return None
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return None
        try:
            val = float(s)
        except ValueError:
            return None
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        if float(val) == int(val):
            return int(val)      # 整数 → int，避免 20.0 脏数据
        return float(val)        # 真小数 → float
    return None


def _quant_data_to_config(data, direction='long'):
    """把前端 quantData 映射回后端 config dict。"""
    config = {}

    # 当前主配置：用于保留前端不编辑的只读元数据（ic_weighted_value / entry_condition 等），
    # 避免保存时被默认值覆盖导致用户已有配置丢失
    # （只读元数据原样带过，不做归一化）
    _cur_cfg = quant_config._safe_cfg() or {}
    old_factor_configs = _cur_cfg.get('factor_configs', {}) or {}

    # 激活因子
    config['active_factors'] = [f['name'] for f in data.get('factors', []) if f.get('enabled')]

    # 因子配置
    fconfigs = {}
    for f in data.get('factors', []):
        # ic / ic_ir / ic_weighted_value 由「③ 因子IC回测」写入、前端不编辑，
        # 须从旧配置原样保留，否则每次保存都会把回测标定结果抹掉
        old_fc = old_factor_configs.get(f['name'], {}) or {}
        # params 用 _norm_num 归一化（避免 20 → 20.0 脏数据）
        raw_params = dict(f.get('params', {}))
        norm_params = {}
        for pk, pv in raw_params.items():
            nv = _norm_num(pv)
            if nv is not None:
                norm_params[pk] = nv
        fconfigs[f['name']] = {
            'weight': float(f.get('weight', 0)),
            'direction': int(f.get('dir', 1)),
            'params': norm_params,
            'stats': {
                'mean': _norm_num(f.get('mean', 0)) if _norm_num(f.get('mean', 0)) is not None else 0,
                'std': _norm_num(f.get('std', 1)) if _norm_num(f.get('std', 1)) is not None else 1,
            },
            'ic': old_fc.get('ic'),
            'ic_ir': old_fc.get('ic_ir'),
            'ic_weighted_value': old_fc.get('ic_weighted_value'),
        }
        # 自定义公式因子：公式/中文名/标记不来自本页表单，必须从旧配置原样带过，
        # 否则在「因子配置」页保存一次就会把公式抹掉（因子退化成空计算）。
        if old_fc.get('custom'):
            fconfigs[f['name']]['custom'] = True
            fconfigs[f['name']]['label'] = old_fc.get('label') or f.get('label')
            fconfigs[f['name']]['formula'] = old_fc.get('formula')
    # 兜底：本页不编辑自定义因子，若前端数据是旧快照（不含自定义行）也不能丢。
    for _name, _old_fc in old_factor_configs.items():
        if not isinstance(_old_fc, dict) or not _old_fc.get('custom'):
            continue
        if _name not in fconfigs and _old_fc.get('formula'):
            fconfigs[_name] = copy.deepcopy(_old_fc)
    config['factor_configs'] = fconfigs

    # 评分缩放（_norm_num 归一化）
    scale = {s['key']: s['val'] for s in data.get('scale', [])}
    config['score_scale'] = {
        'weight_multiplier': _norm_num(scale.get('weight_multiplier', 1.0)) or 1.0,
        'z_truncate_min': _norm_num(scale.get('z_truncate_min', -3.0)) if _norm_num(scale.get('z_truncate_min', -3.0)) is not None else -3.0,
        'z_truncate_max': _norm_num(scale.get('z_truncate_max', 3.0)) if _norm_num(scale.get('z_truncate_max', 3.0)) is not None else 3.0,
        'score_min': _norm_num(scale.get('score_min', 0.0)) or 0.0,
        'score_max': _norm_num(scale.get('score_max', 100.0)) or 100.0,
    }

    # 冲突惩罚（_norm_num 归一化）
    config['conflict_penalty'] = {}
    for p in data.get('penalty', []):
        v = p.get('val')
        if v != '' and v is not None:
            nv = _norm_num(v)
            if nv is not None:
                config['conflict_penalty'][p['key']] = nv

    # 阈值（int(round(float())) 强制转 int）
    config['thresholds'] = {t['key']: int(round(float(t.get('val', 0)))) for t in data.get('thresholds', [])}

    # 入场条件（技术信号 + 否决项）
    entry_conditions = {}
    for t in data.get('thresholds', []):
        _RAW = t.get('signal')
        # 技术门槛支持多条件 AND 组合(list)；旧单选字符串保持兼容
        if isinstance(_RAW, list):
            sig = [_signal_cond_to_config(c) for c in _RAW] if _RAW else 'none'
        else:
            sig = _SIGNAL_FRONT_TO_BACK.get(_RAW, 'none')
        # 否决键集按方向取：多单 VETO_KEYS / 空单 SHORT_VETO_KEYS（语义镜像），与 check_level_veto 一致
        _vkeys = quant_config.veto_keys_for_level(t['key'], direction)
        entry_conditions[t['key']] = {
            'tech_signal': sig,
            'veto_on': bool(t.get('veto_on', False)),
            'veto_enabled': {k: bool(t.get('veto_enabled', {}).get(k, False)) for k in _vkeys},
        }
    config['entry_conditions'] = entry_conditions

    # 高级模式全市场扫描·技术指标二级筛选（AND 组合条件；空 = 不筛选，仍随方案保存）
    config['scan_tech_filter'] = data.get('scanFilter') or []

    # 高级模式全市场扫描·财务筛选阈值（随方案保存；前端缺省 enabled=True 保持爆雷护栏）
    _sff = data.get('scanFinanceFilter')
    config['scan_finance_filter'] = _sff if isinstance(_sff, dict) else {'enabled': True}

    # 否决项阈值参数（方案级全局一套；随方案保存，缺失项引擎用注册表 defaults 兜底）
    _VP = data.get('vetoParams')
    if isinstance(_VP, dict) and _VP:
        config['veto_params'] = {k: dict(v) for k, v in _VP.items() if isinstance(v, dict)}

    # 技术共振（_norm_num 归一化）
    tr = data.get('thresholdResonance', {})
    config['tech_resonance'] = {
        'threshold': _norm_num(tr.get('significantThreshold', 5.0)) if _norm_num(tr.get('significantThreshold', 5.0)) is not None else 5.0,
        'bands': list(tr.get('starCutoffs', [0.2, 0.4, 0.6, 0.8])),
    }

    # 建仓参数（_norm_num 归一化）
    positions = {}
    for p in data.get('entryPos', []):
        nv = _norm_num(p.get('val', 0))
        positions[p['key']] = nv if nv is not None else 0
    config['entry_params'] = {
        'positions': positions,
        'reversal_score_threshold': _norm_num(data.get('reversal_score_threshold', 4.0)) if _norm_num(data.get('reversal_score_threshold', 4.0)) is not None else 4.0,
        'panic_reversal_threshold': _norm_num(data.get('panic_reversal_threshold', 5.0)) if _norm_num(data.get('panic_reversal_threshold', 5.0)) is not None else 5.0,
    }

    # 市场门控（_norm_num 归一化）
    market_gate = {'enabled': bool(data.get('marketGateEnabled', False))}
    envs = {}
    for g in data.get('marketGate', []):
        factor = g.get('factor')
        limit = g.get('limit')
        envs[g['key']] = {
            'factor': _norm_num(factor) if factor != '' and factor is not None else None,
            'limit': _norm_num(limit) if limit != '' and limit is not None else None,
        }
    market_gate['envs'] = envs
    config['market_gate'] = market_gate
    config.setdefault('enabled', {})['market_gate'] = market_gate['enabled']

    # 加仓参数（_norm_num 归一化）
    add_tiers = {}
    for row_key, tiers in data.get('addTiers', {}).items():
        row = {}
        for tk in ('t1', 't2', 't3'):
            t = tiers.get(tk)
            codes = [c for c in (t.get('codes') or []) if c]
            if codes:
                # params 用 _norm_num 归一化
                raw_p = dict(t.get('params', {}))
                norm_p = {}
                for pk, pv in raw_p.items():
                    nv = _norm_num(pv)
                    if nv is not None:
                        norm_p[pk] = nv
                row[tk] = {
                    'triggers': codes,
                    'action': 'add',
                    'ratio': _to_pct_val(t.get('ratio', 0)),
                    'params': norm_p,
                }
            else:
                row[tk] = None
        add_tiers[row_key] = row
    config['add_params'] = {
        'vol_mult': _norm_num(data.get('volumeBreakoutMultiplier', 2.0)) if _norm_num(data.get('volumeBreakoutMultiplier', 2.0)) is not None else 2.0,
        'max_add_ratio': _to_pct_val(data.get('maxAddPctPerStep', 30)),
        'add_tiers': add_tiers,
    }
    config.setdefault('enabled', {})['add_engine'] = True

    # 减仓参数（_norm_num 归一化）
    reduce_tiers = {}
    for row_key, tiers in data.get('reduceTiers', {}).items():
        row = {}
        for tk in ('t1', 't2', 't3'):
            t = tiers.get(tk)
            codes = [c for c in (t.get('codes') or []) if c]
            if codes:
                # params 用 _norm_num 归一化
                raw_p = dict(t.get('params', {}))
                norm_p = {}
                for pk, pv in raw_p.items():
                    nv = _norm_num(pv)
                    if nv is not None:
                        norm_p[pk] = nv
                row[tk] = {
                    'triggers': codes,
                    'action': 'clear' if t.get('action') == '清仓' else 'reduce',
                    'ratio': _to_pct_val(t.get('ratio', 0)),
                    'params': norm_p,
                }
            else:
                row[tk] = None
        reduce_tiers[row_key] = row
    reduce_params = {
        'reduce_tiers': reduce_tiers,
    }
    for rt in data.get('reduceTop', []):
        # enabled=False 的参数不写入配置（视为未启用）
        if not rt.get('enabled', True):
            continue
        v = rt.get('val')
        if v != '' and v is not None:
            nv = _norm_num(v)
            if nv is not None:
                reduce_params[rt['key']] = nv
    config['reduce_params'] = reduce_params
    config.setdefault('enabled', {})['reduce_engine'] = True

    # 风控（_norm_num 归一化；pct 字段需转 0-1 小数）
    # 留空 = 不启用：空值不写入 risk_params，前端回显时显示为空
    risk = {}
    for r in data.get('risk', []):
        _v = r.get('val')
        if _v == '' or _v is None:
            continue  # 留空=不启用，不写入配置
        if r.get('pct'):
            # 前端以百分比(0-100)录入，转成后端存储用的 0-1 小数
            pv = _to_pct_val(_v)
            if pv != 0:
                risk[r['key']] = pv
        else:
            nv = _norm_num(_v)
            if nv is not None:
                risk[r['key']] = nv
    if risk:
        risk['market_crash_index'] = _CIRCUIT_NAME_TO_CODE.get(data.get('circuitBreakerIndex', '沪深300'), 'sh000300')
    # 保留「前端表单里没有」的纯后端开关（目前仅 sell_price_mode）：
    # 这里 risk_params 是按 UI 控件**重建**的，若不显式沿用旧值，用户每次保存方案都会
    # 把这些开关抹掉 —— 表现为「改了配置但一保存就失效」。
    # ⛔ 以后新增同类后端开关（无 UI 控件、仅配置驱动），请一并登记到这里。
    _prev_risk = _cur_cfg.get('risk_params')
    if isinstance(_prev_risk, dict):
        for _k in ('sell_price_mode',):
            if _k in _prev_risk and _k not in risk:
                risk[_k] = _prev_risk[_k]
    config['risk_params'] = risk

    # 回测设置不写入方案（2026-09-19 用户决策）
    # 回测池/年数/前瞻/扫描间隔/输出前缀/期货方向周期等均属「运行面板」的临时选择，
    # 不是策略本体：切换方案不应带走或覆盖回测设置，保存方案也不应把它们持久化。
    # 回测参数由前端 startBacktest 直接经 web_api.start_backtest 的 _run_params 传给
    # 子进程，不依赖方案配置；历史方案里遗留的 config['backtest'] 在反映射侧一并忽略。

    # 幽灵规则（_norm_num 归一化 + int/grace_period_days）
    ghost = {}
    for g in data.get('ghost', []):
        v = g.get('val')
        if v != '' and v is not None:
            nv = _norm_num(v)
            if nv is None:
                continue
            if g['key'] in ('grace_period_days',):
                ghost[g['key']] = int(nv)
            elif g['key'] == 'ma_confirm':
                ghost[g['key']] = str(v)
            else:
                ghost[g['key']] = nv
    config['ghost_rules'] = ghost
    config.setdefault('enabled', {})['ghost_rules'] = bool(ghost)

    # 入场条件（动态监控）：前端不编辑此段，从当前主配置原样 deepcopy 带过，
    # 避免保存时被硬编码默认覆盖导致用户已有配置丢失
    import copy as _copy
    _ec = _cur_cfg.get('entry_condition')
    if isinstance(_ec, dict) and _ec:
        config['entry_condition'] = _copy.deepcopy(_ec)

    return config