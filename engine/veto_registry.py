# -*- coding: utf-8 -*-
"""否决项注册表（可插拔 + 阈值可调 + 6 大场景全覆盖）。

目标：把「否决项」从 factor_tech / quant_config 硬编码收敛为注册表，
使「新增/扩展一个否决项」= 注册表加 1 条 VetoDef（阈值参数化 + 判定函数）。
用户不懂代码，由工具维护者（用户提需求→维护者补判定函数→打包发版）快速扩展。

架构基准 3.2（否决项红灯层）落地：
- 覆盖 6 大场景（极值/趋势破位/量能陷阱/波动异动/形态见顶/筹码失守）。
- 每个否决项阈值可调：def.calc_fn(ctx, params)，params 由方案配置注入，缺失回退 defaults。
- 各档位可自由增删启用项（veto_enabled），真正新增判定由维护者走本注册表。
- 空头方向按语义镜像（做空良机被破坏）。

设计约定：
- 每个否决项 = VetoDef(key, label, direction, scene, defaults, calc_fn, desc, params_schema)。
- calc_fn(ctx, params) -> bool，ctx 固定为：ctx = {'tech': tech, 'pre': pre, 'idx': idx, 'closes': closes}。
  - tech : 单点技术字段（含 rsi_14 / sma_250 / volume_ratio 等实时键）。
  - pre  : 整段序列预计算（含 kdj_j / macd / macd_signal / bb_lower / bb_upper / bb_bandwidth）。
  - idx  : 当前样本点在 pre 序列中的下标（= len(closes)-1 或回测传入的采样点）。
  - closes: 收盘价序列（仅用于当前价/涨跌幅）。
  - params: 方案级阈值参数 dict（{param: value}），缺失时用 defaults。
- direction：'long' 多头过热/破位否决；'short' 空头镜像（做空良机被破坏）。
- scene：6 大场景分组中文 key（extreme/trend/volume/volatility/pattern/chip），供前端分组展示。

新增一个否决项示例：
    def _my_veto(ctx, params):
        return float(ctx['tech'].get('rsi_14', 0.0)) >= params.get('rsi_threshold', 90)
    VETO_REGISTRY['my_veto'] = VetoDef('my_veto', '我的极端', 'long', 'extreme',
                                        {'rsi_threshold': 90}, _my_veto,
                                        'RSI≥阈值', {'rsi_threshold': {'label': 'RSI阈值', 'default': 90}})
仅在放置 VETO_KEYS/VETO_FLAG_LABELS 处按需补入默认开关/标签即可（见 quant_config）。
"""

from dataclasses import dataclass, field

LONG = 'long'
SHORT = 'short'

# 6 大场景中文分组
SCENES = {
    'extreme': '极值/过热',
    'trend': '趋势破位',
    'volume': '量能陷阱',
    'volatility': '波动异动',
    'pattern': '形态见顶',
    'chip': '筹码失守',
    'fundamental': '财务爆雷',
}
SCENE_ORDER = ('extreme', 'trend', 'volume', 'volatility', 'pattern', 'chip', 'fundamental')


@dataclass
class VetoDef:
    """否决项定义。"""
    key: str              # 唯一键（tech dict 中的布尔键）
    label: str            # 中文标签
    direction: str        # LONG / SHORT
    scene: str            # 6 大场景分组 key
    defaults: dict        # 默认阈值参数 {param: default}
    calc_fn: callable     # (ctx, params) -> bool
    desc: str = ''
    params_schema: dict = field(default_factory=dict)  # 阈值字段定义 {name: {label, unit, default, step}}


# ============================================================
# 多头过热/破位否决（LONG）—— 6 大场景全覆盖
# ============================================================

# ---- 极值/过热 ----

def _rsi_extreme(ctx, params):
    tech = ctx['tech']
    return float(tech.get('rsi_14', 0.0)) >= params.get('rsi_threshold', 85)


def _kdj_extreme(ctx, params):
    pre, idx = ctx['pre'], ctx['idx']
    if 'kdj_j' in pre and idx < len(pre['kdj_j']):
        j_now = float(pre['kdj_j'][idx])
        if idx >= 1:
            j_prev = float(pre['kdj_j'][idx - 1])
            return (j_now >= params.get('j_threshold', 100)) and (j_now < j_prev)
    return False


def _bias_extreme(ctx, params):
    """乖离率过大（20 日）：价格偏离 MA 超过阈值%，过热易回归。"""
    tech, closes = ctx['tech'], ctx['closes']
    close_now = float(closes[-1]) if len(closes) > 0 else 0.0
    ma = float(tech.get('sma_20', 0.0))
    if close_now <= 0 or ma <= 0:
        return False
    bias = (close_now - ma) / ma * 100.0
    return bias >= params.get('bias_pct', 8.0)


# ---- 趋势破位 ----

def _macd_high_dead_cross(ctx, params):
    pre, idx = ctx['pre'], ctx['idx']
    if idx >= 1 and idx < len(pre['macd']) and idx < len(pre['macd_signal']):
        dif_now = float(pre['macd'][idx])
        dea_now = float(pre['macd_signal'][idx])
        dif_prev = float(pre['macd'][idx - 1])
        dea_prev = float(pre['macd_signal'][idx - 1])
        cross_down = (dif_prev > dea_prev) and (dif_now < dea_now)
        return cross_down and (dif_now > 0)
    return False


def _ma250_break(ctx, params):
    tech, closes = ctx['tech'], ctx['closes']
    close_now = float(closes[-1]) if len(closes) > 0 else 0.0
    sma_250 = float(tech.get('sma_250', 0.0))
    return (sma_250 > 0) and (close_now < sma_250 * (1 - params.get('break_pct', 0.03)))


def _ma20_turn_down(ctx, params):
    """MA20 拐头向下：今日 MA20 低于昨日 MA20。"""
    pre, idx = ctx['pre'], ctx['idx']
    if ('sma_20' in pre) and idx >= 1 and idx < len(pre['sma_20']):
        ma_now = float(pre['sma_20'][idx])
        ma_prev = float(pre['sma_20'][idx - 1])
        return (ma_now < ma_prev) and (ma_now - ma_prev) < -params.get('slope_threshold', 0.0)
    return False


def _new_high_20d_retreat(ctx, params):
    """20 日新高回落：先见 20 日新高，近 2 日回撤>retreat_pct%。"""
    closes = ctx['closes']
    n = len(closes)
    if n < params.get('lookback', 3) + 3:
        return False
    lookback = int(params.get('new_high_lookback', 20))
    if n < lookback + 1:
        return False
    seg = closes[-lookback - 1:]
    peak = max(seg[:-2]) if len(seg) > 2 else 0.0
    retreat_pct = params.get('retreat_pct', 3.0)
    # 曾创新高：最近第3根之前创下 high_lookback 新高
    near_high = seg[-3] >= max(seg[:-3]) if len(seg) > 3 else False
    close_now = float(closes[-1])
    return near_high and (peak - close_now) / peak * 100.0 >= retreat_pct


def _ma_dead_cross(ctx, params):
    """均线死叉（MA5 下穿 MA20）。"""
    pre, idx = ctx['pre'], ctx['idx']
    if ('sma_5' in pre and 'sma_20' in pre) and idx >= 1 \
            and idx < len(pre['sma_5']) and idx < len(pre['sma_20']):
        ma5_now, ma5_prev = float(pre['sma_5'][idx]), float(pre['sma_5'][idx - 1])
        ma20_now, ma20_prev = float(pre['sma_20'][idx]), float(pre['sma_20'][idx - 1])
        return (ma5_prev > ma20_prev) and (ma5_now < ma20_now)
    return False


# ---- 量能陷阱 ----

def _volume_stagnant(ctx, params):
    tech, closes = ctx['tech'], ctx['closes']
    n = len(closes)
    close_now = float(closes[-1]) if n > 0 else 0.0
    prev_close = float(closes[-2]) if n >= 2 else close_now
    volume_ratio = float(tech.get('volume_ratio', 1.0))
    change_pct = ((close_now - prev_close) / prev_close * 100.0) if prev_close > 0 else 0.0
    return (volume_ratio >= params.get('volume_ratio', 3)) and (change_pct <= params.get('max_change_pct', 1))


def _high_volume_top(ctx, params):
    """高位天量：放量 volume_ratio 倍且价贴近 20 日高点（near_high_pct%）。"""
    tech, closes = ctx['tech'], ctx['closes']
    close_now = float(closes[-1]) if len(closes) > 0 else 0.0
    if close_now <= 0 or len(closes) < 20:
        return False
    peak = max(closes[-20:])
    volume_ratio = float(tech.get('volume_ratio', 1.0))
    near_high_pct = params.get('near_high_pct', 1.0)
    return (volume_ratio >= params.get('volume_ratio', 3)) and (peak - close_now) / peak * 100.0 <= near_high_pct


def _volume_price_divergence(ctx, params):
    """量价背离：价升但量缩（或价跌但量增的反向异动）。"""
    tech = ctx['tech']
    return bool(tech.get('volume_price_divergence', False))


# ---- 波动异动 ----

def _atr_surge(ctx, params):
    """ATR 异常放大：当日 ATR 相对近期均 ATR 放大 atr_mult 倍。"""
    pre, idx = ctx['pre'], ctx['idx']
    if ('atr' in pre) and idx >= 1 and idx < len(pre['atr']):
        atr_now = float(pre['atr'][idx])
        window = int(params.get('window', 10))
        start = max(0, idx - window)
        if idx - start >= 2:
            atr_avg = sum(float(x) for x in pre['atr'][start:idx]) / (idx - start)
            if atr_avg > 0:
                return atr_now >= atr_avg * params.get('atr_mult', 2.0)
    return False


def _gap_high_low(ctx, params):
    """跳空高开低走：开盘大幅高开（>gap_pct%）但收盘转跌。"""
    ctx_tech = ctx['tech']
    if 'open' in ctx_tech and 'close' in ctx_tech:
        open_ = float(ctx_tech['open'])
        close_ = float(ctx_tech['close'])
        prev_close = float(ctx_tech.get('prev_close', 0.0))
        if prev_close > 0:
            gap_pct = (open_ - prev_close) / prev_close * 100.0
            return gap_pct >= params.get('gap_pct', 1.0) and close_ < open_
    return False


def _gap_down(ctx, params):
    """向下跳空缺口：开盘较昨收低开超 gap_pct%。"""
    ctx_tech = ctx['tech']
    if 'open' in ctx_tech and 'prev_close' in ctx_tech:
        open_ = float(ctx_tech['open'])
        prev_close = float(ctx_tech['prev_close'])
        if prev_close > 0:
            gap_pct = (prev_close - open_) / prev_close * 100.0
            return gap_pct >= params.get('gap_pct', 2.0)
    return False


# ---- 形态见顶 ----

def _long_upper_shadow(ctx, params):
    """长上影/吊颈：上影线长于实体 ratio 倍以上，见顶信号。"""
    ctx_tech = ctx['tech']
    if 'high' in ctx_tech and 'low' in ctx_tech and 'open' in ctx_tech and 'close' in ctx_tech:
        high, low = float(ctx_tech['high']), float(ctx_tech['low'])
        open_, close_ = float(ctx_tech['open']), float(ctx_tech['close'])
        body = abs(close_ - open_)
        upper = high - max(open_, close_)
        lower = min(open_, close_) - low
        rng = (high - low) if (high - low) > 0 else 1
        ratio = upper / rng
        return ratio >= params.get('shadow_ratio', 0.6) and body <= upper
    return False


def _bear_engulf(ctx, params):
    """看跌吞没/乌云盖顶：今日阴线实体吞没昨日前阳线。"""
    pre, idx = ctx['pre'], ctx['idx']
    if idx >= 1 and 'body' in pre and idx < len(pre['body']):
        doji_now = float(pre['body'][idx])
        doji_prev = float(pre['body'][idx - 1])
        # 简化：当前收阴 且 实体覆盖前阳线（由 pre['bear_engulf'] 预计算键驱动）
        if 'bear_engulf' in pre:
            return bool(pre['bear_engulf'][idx])
    return False


# ---- 筹码失守 ----

def _chip_support_break(ctx, params):
    """跌破筹码密集区下沿：价格跌破近期筹码密集区下沿 break_pct%。"""
    tech, closes = ctx['tech'], ctx['closes']
    close_now = float(closes[-1]) if len(closes) > 0 else 0.0
    support = float(tech.get('chip_support', 0.0))
    if support <= 0:
        return False
    return close_now < support * (1 - params.get('break_pct', 0.01))


# ---- 财务爆雷 ----

def _finance_risk(ctx, params):
    """财务爆雷否决：净资产为负 或 最新期净利润为负 → 否决买入。

    依赖 ctx['finance']（由调用方经 fetch_finance 注入，见 factor_tech /
    analyze_service）。缺财务数据时安全降级为 False（不否决），避免误伤。
    字段口径（pytdx get_finance_info）：
      jingzichan = 每股净资产（负即为资不抵债/退市风险）
      jinglirun   = 每股净利润（负数即当期亏损）
    """
    fin = ctx.get('finance')
    if not fin or not isinstance(fin, dict):
        return False
    try:
        jz = float(fin.get('jingzichan', 0.0))
        jl = float(fin.get('jinglirun', 0.0))
    except (TypeError, ValueError):
        return False
    return jz < 0 or jl < 0


# ============================================================
# 空头镜像否决（SHORT）—— 语义相反
# ============================================================

def _rsi_extreme_low(ctx, params):
    tech = ctx['tech']
    return float(tech.get('rsi_14', 0.0)) <= params.get('rsi_threshold', 15)


def _kdj_extreme_low(ctx, params):
    pre, idx = ctx['pre'], ctx['idx']
    if 'kdj_j' in pre and idx < len(pre['kdj_j']):
        j_now = float(pre['kdj_j'][idx])
        if idx >= 1:
            j_prev = float(pre['kdj_j'][idx - 1])
            return (j_now <= params.get('j_threshold', 0)) and (j_now > j_prev)
    return False


def _bias_extreme_low(ctx, params):
    tech, closes = ctx['tech'], ctx['closes']
    close_now = float(closes[-1]) if len(closes) > 0 else 0.0
    ma = float(tech.get('sma_20', 0.0))
    if close_now <= 0 or ma <= 0:
        return False
    bias = (close_now - ma) / ma * 100.0
    return bias <= -params.get('bias_pct', 8.0)


def _macd_low_gold_cross(ctx, params):
    pre, idx = ctx['pre'], ctx['idx']
    if idx >= 1 and idx < len(pre['macd']) and idx < len(pre['macd_signal']):
        dif_now = float(pre['macd'][idx])
        dea_now = float(pre['macd_signal'][idx])
        dif_prev = float(pre['macd'][idx - 1])
        dea_prev = float(pre['macd_signal'][idx - 1])
        cross_up = (dif_prev < dea_prev) and (dif_now > dea_now)
        return cross_up and (dif_now < 0)
    return False


def _ma250_break_up(ctx, params):
    tech, closes = ctx['tech'], ctx['closes']
    close_now = float(closes[-1]) if len(closes) > 0 else 0.0
    sma_250 = float(tech.get('sma_250', 0.0))
    return (sma_250 > 0) and (close_now > sma_250 * (1 + params.get('break_pct', 0.03)))


def _volume_stagnant_down(ctx, params):
    tech, closes = ctx['tech'], ctx['closes']
    n = len(closes)
    close_now = float(closes[-1]) if n > 0 else 0.0
    prev_close = float(closes[-2]) if n >= 2 else close_now
    volume_ratio = float(tech.get('volume_ratio', 1.0))
    change_pct = ((close_now - prev_close) / prev_close * 100.0) if prev_close > 0 else 0.0
    return (volume_ratio >= params.get('volume_ratio', 3)) and (change_pct >= -1) and (change_pct < 0)


def _bb_upper_break_widen(ctx, params):
    pre, idx, closes = ctx['pre'], ctx['idx'], ctx['closes']
    close_now = float(closes[-1]) if len(closes) > 0 else 0.0
    if idx >= 1 and 'bb_upper' in pre and 'bb_bandwidth' in pre \
            and idx < len(pre['bb_upper']) and idx < len(pre['bb_bandwidth']):
        upper = float(pre['bb_upper'][idx])
        bw_now = float(pre['bb_bandwidth'][idx])
        bw_prev = float(pre['bb_bandwidth'][idx - 1])
        return (close_now > upper) and (bw_now > bw_prev)
    return False


def _bb_lower_break_widen(ctx, params):
    pre, idx, closes = ctx['pre'], ctx['idx'], ctx['closes']
    close_now = float(closes[-1]) if len(closes) > 0 else 0.0
    if idx >= 1 and 'bb_lower' in pre and 'bb_bandwidth' in pre \
            and idx < len(pre['bb_lower']) and idx < len(pre['bb_bandwidth']):
        lower = float(pre['bb_lower'][idx])
        bw_now = float(pre['bb_bandwidth'][idx])
        bw_prev = float(pre['bb_bandwidth'][idx - 1])
        return (close_now < lower) and (bw_now > bw_prev)
    return False


def _ma20_turn_up(ctx, params):
    pre, idx = ctx['pre'], ctx['idx']
    if ('sma_20' in pre) and idx >= 1 and idx < len(pre['sma_20']):
        ma_now = float(pre['sma_20'][idx])
        ma_prev = float(pre['sma_20'][idx - 1])
        return (ma_now > ma_prev) and (ma_now - ma_prev) > params.get('slope_threshold', 0.0)
    return False


def _new_low_20d_retreat(ctx, params):
    closes = ctx['closes']
    n = len(closes)
    if n < 20 + 3:
        return False
    seg = closes[-21:]
    trough = min(seg[:-2])
    retreat_pct = params.get('retreat_pct', 3.0)
    near_low = seg[-3] <= min(seg[:-3]) if len(seg) > 3 else False
    close_now = float(closes[-1])
    return near_low and (close_now - trough) / abs(trough) * 100.0 >= retreat_pct if trough != 0 else False


def _ma_gold_cross_up(ctx, params):
    pre, idx = ctx['pre'], ctx['idx']
    if ('sma_5' in pre and 'sma_20' in pre) and idx >= 1 \
            and idx < len(pre['sma_5']) and idx < len(pre['sma_20']):
        ma5_now, ma5_prev = float(pre['sma_5'][idx]), float(pre['sma_5'][idx - 1])
        ma20_now, ma20_prev = float(pre['sma_20'][idx]), float(pre['sma_20'][idx - 1])
        return (ma5_prev < ma20_prev) and (ma5_now > ma20_now)
    return False


def _volume_stagnant_down_hi(ctx, params):
    tech, closes = ctx['tech'], ctx['closes']
    close_now = float(closes[-1]) if len(closes) > 0 else 0.0
    prev_close = float(closes[-2]) if len(closes) >= 2 else close_now
    volume_ratio = float(tech.get('volume_ratio', 1.0))
    change_pct = ((close_now - prev_close) / prev_close * 100.0) if prev_close > 0 else 0.0
    return (volume_ratio >= params.get('volume_ratio', 3)) and (change_pct >= params.get('min_change_pct', -1)) and (change_pct >= 0) and (change_pct <= params.get('max_change_pct', 1))


def _volume_price_up_shrink(ctx, params):
    return bool(ctx['tech'].get('volume_price_divergence', False))


def _atr_shrink(ctx, params):
    pre, idx = ctx['pre'], ctx['idx']
    if ('atr' in pre) and idx >= 1 and idx < len(pre['atr']):
        atr_now = float(pre['atr'][idx])
        window = int(params.get('window', 10))
        start = max(0, idx - window)
        if idx - start >= 2:
            atr_avg = sum(float(x) for x in pre['atr'][start:idx]) / (idx - start)
            if atr_avg > 0:
                return atr_now <= atr_avg / params.get('atr_div', 2.0)
    return False


def _gap_low_high(ctx, params):
    ctx_tech = ctx['tech']
    if 'open' in ctx_tech and 'close' in ctx_tech:
        open_ = float(ctx_tech['open'])
        close_ = float(ctx_tech['close'])
        prev_close = float(ctx_tech.get('prev_close', 0.0))
        if prev_close > 0:
            gap_pct = (prev_close - open_) / prev_close * 100.0
            return gap_pct >= params.get('gap_pct', 1.0) and close_ > open_
    return False


def _gap_up(ctx, params):
    ctx_tech = ctx['tech']
    if 'open' in ctx_tech and 'prev_close' in ctx_tech:
        open_ = float(ctx_tech['open'])
        prev_close = float(ctx_tech['prev_close'])
        if prev_close > 0:
            gap_pct = (open_ - prev_close) / prev_close * 100.0
            return gap_pct >= params.get('gap_pct', 2.0)
    return False


def _long_lower_shadow(ctx, params):
    ctx_tech = ctx['tech']
    if 'high' in ctx_tech and 'low' in ctx_tech and 'open' in ctx_tech and 'close' in ctx_tech:
        high, low = float(ctx_tech['high']), float(ctx_tech['low'])
        open_, close_ = float(ctx_tech['open']), float(ctx_tech['close'])
        body = abs(close_ - open_)
        lower = min(open_, close_) - low
        rng = (high - low) if (high - low) > 0 else 1
        ratio = lower / rng
        return ratio >= params.get('shadow_ratio', 0.6) and body <= lower
    return False


def _bull_engulf(ctx, params):
    pre, idx = ctx['pre'], ctx['idx']
    if idx >= 1 and 'bull_engulf' in pre:
        return bool(pre['bull_engulf'][idx])
    return False


def _chip_resistance_break(ctx, params):
    tech, closes = ctx['tech'], ctx['closes']
    close_now = float(closes[-1]) if len(closes) > 0 else 0.0
    resistance = float(tech.get('chip_resistance', 0.0))
    if resistance <= 0:
        return False
    return close_now > resistance * (1 + params.get('break_pct', 0.01))


# ============================================================
# 注册表
# ============================================================

VETO_REGISTRY = {
    # —— 极值/过热 ——
    'rsi_extreme': VetoDef('rsi_extreme', 'RSI极端高位', LONG, 'extreme',
                           {'rsi_threshold': 85}, _rsi_extreme,
                           'RSI(14)≥85，过热', {'rsi_threshold': {'label': 'RSI阈值', 'default': 85}}),
    'kdj_extreme': VetoDef('kdj_extreme', 'KDJ极端', LONG, 'extreme',
                           {'j_threshold': 100}, _kdj_extreme,
                           'J≥100 且自昨日回落', {'j_threshold': {'label': 'J阈值', 'default': 100}}),
    'bias_extreme': VetoDef('bias_extreme', '乖离率过大', LONG, 'extreme',
                            {'bias_pct': 8.0}, _bias_extreme,
                            '20日乖离率超过阈值，过热易回归', {'bias_pct': {'label': '乖离%', 'default': 8.0, 'unit': '%'}}),
    # —— 趋势破位 ——
    'ma250_break': VetoDef('ma250_break', '跌破年线', LONG, 'trend',
                           {'break_pct': 0.03}, _ma250_break,
                           '收盘跌破 MA250 达 3%', {'break_pct': {'label': '破位%', 'default': 3.0, 'unit': '%'}}),
    'macd_high_dead_cross': VetoDef('macd_high_dead_cross', 'MACD高位死叉', LONG, 'trend',
                                    {}, _macd_high_dead_cross,
                                    'DIF 零轴上方下穿 DEA', {}),
    'ma20_turn_down': VetoDef('ma20_turn_down', 'MA20拐头向下', LONG, 'trend',
                              {'slope_threshold': 0.0}, _ma20_turn_down,
                              'MA20 当日低于昨日', {'slope_threshold': {'label': '斜率阈值', 'default': 0.0}}),
    'new_high_20d_retreat': VetoDef('new_high_20d_retreat', '20日新高回落', LONG, 'trend',
                                    {'new_high_lookback': 20, 'retreat_pct': 3.0}, _new_high_20d_retreat,
                                    '先创 20 日新高，近 3 日回撤超阈值', {'retreat_pct': {'label': '回撤%', 'default': 3.0, 'unit': '%'}}),
    'ma_dead_cross': VetoDef('ma_dead_cross', '均线死叉', LONG, 'trend',
                             {}, _ma_dead_cross,
                             'MA5 下穿 MA20', {}),
    # —— 量能陷阱 ——
    'volume_stagnant': VetoDef('volume_stagnant', '放量滞涨', LONG, 'volume',
                               {'volume_ratio': 3, 'max_change_pct': 1}, _volume_stagnant,
                               '放量3倍但涨幅≤1%',
                               {'volume_ratio': {'label': '放量倍数', 'default': 3}, 'max_change_pct': {'label': '涨幅上限%', 'default': 1.0, 'unit': '%'}}),
    'high_volume_top': VetoDef('high_volume_top', '高位天量', LONG, 'volume',
                               {'volume_ratio': 3, 'near_high_pct': 1.0}, _high_volume_top,
                               '放量3倍且价贴近20日高点',
                               {'volume_ratio': {'label': '放量倍数', 'default': 3}, 'near_high_pct': {'label': '距高点%', 'default': 1.0, 'unit': '%'}}),
    'volume_price_divergence': VetoDef('volume_price_divergence', '量价背离', LONG, 'volume',
                                       {}, _volume_price_divergence,
                                       '价升量缩/价跌量增异动', {}),
    # —— 波动异动 ——
    'atr_surge': VetoDef('atr_surge', 'ATR异常放大', LONG, 'volatility',
                         {'atr_mult': 2.0}, _atr_surge,
                         '当日 ATR 达 10 日均值的2倍', {'atr_mult': {'label': '放大倍数', 'default': 2.0}}),
    'gap_high_low': VetoDef('gap_high_low', '跳空高开低走', LONG, 'volatility',
                            {'gap_pct': 1.0}, _gap_high_low,
                            '高开≥1%但收盘转跌', {'gap_pct': {'label': '跳空%', 'default': 1.0, 'unit': '%'}}),
    'gap_down': VetoDef('gap_down', '向下跳空缺口', LONG, 'volatility',
                        {'gap_pct': 2.0}, _gap_down,
                        '较昨收低开≥2%', {'gap_pct': {'label': '跳空%', 'default': 2.0, 'unit': '%'}}),
    # —— 形态见顶 ——
    'long_upper_shadow': VetoDef('long_upper_shadow', '长上影/吊颈', LONG, 'pattern',
                                 {'shadow_ratio': 0.6}, _long_upper_shadow,
                                 '上影线占振幅≥60%且长于实体', {'shadow_ratio': {'label': '上影占比', 'default': 0.6}}),
    'bear_engulf': VetoDef('bear_engulf', '看跌吞没/乌云盖顶', LONG, 'pattern',
                           {}, _bear_engulf,
                           '阴线吞没前阳线实体', {}),
    # —— 筹码失守 ——
    'chip_support_break': VetoDef('chip_support_break', '跌破筹码密集区', LONG, 'chip',
                                  {'break_pct': 0.01}, _chip_support_break,
                                  '价格跌破筹码密集区下沿1%', {'break_pct': {'label': '破位%', 'default': 1.0, 'unit': '%'}}),
    # —— 财务爆雷 ——
    'finance_risk': VetoDef('finance_risk', '财务爆雷', LONG, 'fundamental',
                            {}, _finance_risk,
                            '净资产为负或最新期亏损（资不抵债/退市风险，禁止接飞刀）', {}),

    # ==== 空头镜像（SHORT） ====
    'rsi_extreme_low': VetoDef('rsi_extreme_low', 'RSI极端低位', SHORT, 'extreme',
                               {'rsi_threshold': 15}, _rsi_extreme_low,
                               'RSI(14)≤15，超卖易反抽', {'rsi_threshold': {'label': 'RSI阈值', 'default': 15}}),
    'kdj_extreme_low': VetoDef('kdj_extreme_low', 'KDJ极端低位', SHORT, 'extreme',
                               {'j_threshold': 0}, _kdj_extreme_low,
                               'J≤0 且自昨日回升', {'j_threshold': {'label': 'J阈值', 'default': 0}}),
    'bias_extreme_low': VetoDef('bias_extreme_low', '乖离率过低', SHORT, 'extreme',
                                {'bias_pct': 8.0}, _bias_extreme_low,
                                '20日负乖离超过阈值，超卖易反抽', {'bias_pct': {'label': '乖离%', 'default': 8.0, 'unit': '%'}}),
    'ma250_break_up': VetoDef('ma250_break_up', '突破年线', SHORT, 'trend',
                              {'break_pct': 0.03}, _ma250_break_up,
                              '收盘突破 MA250 达 3%', {'break_pct': {'label': '突破%', 'default': 3.0, 'unit': '%'}}),
    'macd_low_gold_cross': VetoDef('macd_low_gold_cross', 'MACD低位金叉', SHORT, 'trend',
                                   {}, _macd_low_gold_cross,
                                   'DIF 零轴下方上穿 DEA', {}),
    'ma20_turn_up': VetoDef('ma20_turn_up', 'MA20拐头向上', SHORT, 'trend',
                            {'slope_threshold': 0.0}, _ma20_turn_up,
                            'MA20 当日高于昨日', {'slope_threshold': {'label': '斜率阈值', 'default': 0.0}}),
    'new_low_20d_retreat': VetoDef('new_low_20d_retreat', '20日新低回升', SHORT, 'trend',
                                   {'retreat_pct': 3.0}, _new_low_20d_retreat,
                                   '先创 20 日新低，近 3 日反弹超阈值', {'retreat_pct': {'label': '反弹%', 'default': 3.0, 'unit': '%'}}),
    'ma_gold_cross_up': VetoDef('ma_gold_cross_up', '均线金叉', SHORT, 'trend',
                                {}, _ma_gold_cross_up,
                                'MA5 上穿 MA20（做空良机破坏）', {}),
    'volume_stagnant_down_hi': VetoDef('volume_stagnant_down_hi', '放量滞跌', SHORT, 'volume',
                                       {'volume_ratio': 3, 'max_change_pct': 1.0}, _volume_stagnant_down_hi,
                                       '放量3倍但跌幅收窄', {'volume_ratio': {'label': '放量倍数', 'default': 3}, 'max_change_pct': {'label': '跌幅上限%', 'default': 1.0, 'unit': '%'}}),
    'high_volume_low': VetoDef('high_volume_low', '低位放量', SHORT, 'volume',
                               {'volume_ratio': 3, 'near_low_pct': 1.0}, _high_volume_top,
                               '放量3倍且价贴近20日低点（空头停歇危险）', {'volume_ratio': {'label': '放量倍数', 'default': 3}}),
    'volume_price_up_shrink': VetoDef('volume_price_up_shrink', '价升量缩', SHORT, 'volume',
                                      {}, _volume_price_up_shrink,
                                      '缩量反弹，空头勿追', {}),
    'atr_shrink': VetoDef('atr_shrink', 'ATR异常收敛', SHORT, 'volatility',
                          {'atr_div': 2.0}, _atr_shrink,
                          'ATR 骤降至 10 日均值1/2（变盘前兆）', {'atr_div': {'label': '收缩倍数', 'default': 2.0}}),
    'gap_low_high': VetoDef('gap_low_high', '跳空低开高走', SHORT, 'volatility',
                            {'gap_pct': 1.0}, _gap_low_high,
                            '低开≥1%但收盘转涨', {'gap_pct': {'label': '跳空%', 'default': 1.0, 'unit': '%'}}),
    'gap_up': VetoDef('gap_up', '向上跳空缺口', SHORT, 'volatility',
                      {'gap_pct': 2.0}, _gap_up,
                      '较昨收高开≥2%（空头强势破坏）', {'gap_pct': {'label': '跳空%', 'default': 2.0, 'unit': '%'}}),
    'long_lower_shadow': VetoDef('long_lower_shadow', '长下影/锤子', SHORT, 'pattern',
                                 {'shadow_ratio': 0.6}, _long_lower_shadow,
                                 '下影线占振幅≥60%（见底反弹）', {'shadow_ratio': {'label': '下影占比', 'default': 0.6}}),
    'bull_engulf': VetoDef('bull_engulf', '看涨吞没', SHORT, 'pattern',
                           {}, _bull_engulf,
                           '阳线吞没前阴线实体', {}),
    'chip_resistance_break': VetoDef('chip_resistance_break', '突破筹码密集区上沿', SHORT, 'chip',
                                     {'break_pct': 0.01}, _chip_resistance_break,
                                     '价格突破筹码密集区上沿1%', {'break_pct': {'label': '突破%', 'default': 1.0, 'unit': '%'}}),
}


def get_veto_keys(direction=LONG):
    """返回指定方向的否决项键集合。"""
    return {k for k, v in VETO_REGISTRY.items() if v.direction == direction}


def get_veto_labels(direction=LONG):
    """返回指定方向的 {key: label} 映射。"""
    return {k: v.label for k, v in VETO_REGISTRY.items() if v.direction == direction}


def get_veto_defs(direction=LONG):
    """返回指定方向的 VetoDef 列表（按 6 大场景分组，供前端渲染）。"""
    return [v for k, v in VETO_REGISTRY.items() if v.direction == direction]


def get_veto_params_def(direction=LONG):
    """返回 {key: params_schema}，供前端生产阈值输入框。"""
    return {k: v.params_schema for k, v in VETO_REGISTRY.items() if v.direction == direction}


def evaluate_vetos(tech, pre, idx, closes, params=None, direction=None, finance=None):
    """遍历注册表计算否决项布尔值。

    params：方案级阈值 {key: {param: value}}，缺失项用各 VetoDef 的 defaults。
    direction：None=计算全部方向；否则只算该方向（默认 None 兼容旧 4 参调用）。
    finance：可选，实时分析与诊断注入的财务 dict（供 finance_risk 否决项）；
        缺省（回测/批量路径）不注入，finance_risk 安全降级 False。
    异常项安全降级为 False（不等价否定整个逻辑）。
    """
    params = params or {}
    ctx = {'tech': tech, 'pre': pre, 'idx': idx, 'closes': closes}
    if finance is not None:
        ctx['finance'] = finance
    out = {}
    for key, vdef in VETO_REGISTRY.items():
        if direction is not None and vdef.direction != direction:
            continue
        try:
            p = dict(vdef.defaults)
            p.update(params.get(key) or {})
            out[key] = bool(vdef.calc_fn(ctx, p))
        except Exception:
            out[key] = False
    return out