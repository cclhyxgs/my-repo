# -*- coding: utf-8 -*-
# 因子技术序列预计算与 tech/market 上下文构造（纯指标层，无业务依赖）。
#
# 从 factor_ic_backtest 下沉而来：因子 IC 回测的并行 worker（parallel_utils）
# 与本模块都需要用到 _precompute_tech_series / _build_tech，二者互相引用会成环。
# 故将这一组纯指标计算（仅依赖 numpy/pandas/engine.indicators）独立成底层模块，
# factor_ic_backtest 与 parallel_utils 均直接 import 本模块，打破循环依赖。

import logging
import numpy as np
import pandas as pd
from engine.indicators import (DivergenceDetector, CandlestickPatterns, VolatilityCone,
                               ADXCalculator, KDJCalculator, MATechnical)

logger = logging.getLogger(__name__)
from engine.indicators_advanced import (calc_chip_concentration, calc_fibonacci_retracement,
                                         calc_pivot_points)


def _get_config_period(factor_name, default):
    """读取指定因子的配置周期（period / ma_period / lookback）。读取失败回落默认。"""
    try:
        from engine import quant_config
        _fcfg = quant_config.get_factor_configs() or {}
        _cfg = _fcfg.get(factor_name, {})
        _pm = _cfg.get('params') or {}
        for _k in ('period', 'ma_period', 'lookback'):
            _vp = _pm.get(_k)
            if isinstance(_vp, (int, float)) and int(_vp) >= 2:
                return int(_vp)
    except Exception:
        pass
    return default


def _get_config_param(factor_name, key, default):
    """读取指定因子配置里某个具体参数键（如 MACD 的 fast/slow/signal）。读取失败回落默认。"""
    try:
        from engine import quant_config
        _pm = (quant_config.get_factor_configs() or {}).get(factor_name, {}).get('params') or {}
        _vp = _pm.get(key)
        if isinstance(_vp, (int, float)) and int(_vp) >= 2:
            return int(_vp)
    except Exception:
        pass
    return default

# ==================== 性能优化：整段序列预计算（消除 O(n^2)）====================
_BB_PERIOD = 20  # 与 BollingerAnalyzer.calc 默认 window 保持一致

def _rsi_series(closes, period=14):
    """精确复刻 RSICalculator.calc_series 的 Wilder 递推，返回长度 len(closes) 的整段 RSI 序列。

    注意：seed 窗口 avg_loss==0 时**不能**整段置 100 提前返回——否则后续可能出现的下跌
    失掉递推起点，整段 RSI 恒 100，导致 RSI 顶背离检测失效、rsi_extreme 永久触发。
    正确做法与原 calc_series 一致：仅在该点置 100，并继续逐点递推。
    """
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    if n < period + 1:
        return np.full(n, 50.0)
    deltas = np.diff(closes)
    m = len(deltas)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    rsi = np.full(n, 50.0)
    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))
    if avg_loss > 0:
        rs = avg_gain / avg_loss
        rsi[period] = np.clip(100 - 100 / (1 + rs), 0, 100)
    else:
        rsi[period] = 100.0
    for i in range(period, m):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss > 0:
            rs = avg_gain / avg_loss
            rsi[i + 1] = np.clip(100 - 100 / (1 + rs), 0, 100)
        else:
            rsi[i + 1] = 100.0
    return rsi


def _macd_hist_series(closes, fast=12, slow=26, signal=9):
    """精确等价 MACDCalculator.calc（ewm），返回整段 MACD hist 序列。"""
    s = pd.Series(np.asarray(closes, dtype=float))
    if len(s) < slow + signal:
        return np.zeros(len(s))
    ema_fast = s.ewm(span=fast, adjust=False).mean()
    ema_slow = s.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    macd_signal = macd_line.ewm(span=signal, adjust=False).mean()
    return (macd_line - macd_signal).values


def _macd_accel_from_hist(hist):
    """精确复刻 calc_macd_acceleration_from_hist，返回 (accel序列, status序列)。"""
    n = len(hist)
    accel = np.zeros(n)
    status = np.array([''] * n, dtype=object)
    if n < 3:
        return accel, status
    vel = np.zeros(n)
    vel[2:] = hist[2:] - hist[1:-1]
    accel[2:] = vel[2:] - vel[1:-1]
    for i in range(2, n):
        if hist[i] > 0 and vel[i] > 0:
            status[i] = '多头加速'
        elif hist[i] > 0 and vel[i] < 0:
            status[i] = '多头减速'
        elif hist[i] < 0 and vel[i] < 0:
            status[i] = '空头加速'
        elif hist[i] < 0 and vel[i] > 0:
            status[i] = '空头减速'
        else:
            status[i] = '动能衰竭'
    return accel, status


def _obv_series(closes, volumes):
    closes = np.asarray(closes, float)
    volumes = np.asarray(volumes, float)
    n = len(closes)
    if n == 0:
        return np.array([])
    sign = np.sign(np.diff(closes, prepend=closes[0]))
    return np.cumsum(sign * volumes)


def _bb_series(closes, period=_BB_PERIOD, std_dev=2.0):
    """整段布林带序列（O(n)）。返回 rolling 带宽序列 bandwidth（索引 j 对应窗口
    closes[j-period+1 : j+1]），供样本点按需精确复刻原 BollingerAnalyzer.calc 的分位逻辑。"""
    s = pd.Series(np.asarray(closes, dtype=float))
    n = len(s)
    if n < period:
        last = float(s.iloc[-1]) if n else 0.0
        return {'upper': np.full(n, last), 'middle': np.full(n, last),
                'lower': np.full(n, last), 'bandwidth': np.zeros(n),
                'pct_b': np.full(n, 0.5)}
    ma = s.rolling(period).mean()
    std = s.rolling(period).std(ddof=0)  # 与原 np.std（ddof=0）一致
    upper = ma + std_dev * std
    lower = ma - std_dev * std
    # 守卫：ma 为 0/NaN 时 bandwidth 归 0（fillna 不处理 inf，需显式 np.where 分母）
    _ma_safe = np.where((ma.values > 0) & np.isfinite(ma.values), ma.values, 1.0)
    bandwidth_raw = (upper - lower).values / _ma_safe * 100
    bw = np.where(np.isfinite(bandwidth_raw), bandwidth_raw, 0.0)
    denom = (upper - lower)
    pct_b = np.where(denom > 0, (s - lower) / denom, 0.5)
    return {'upper': upper.values, 'middle': ma.values, 'lower': lower.values,
            'bandwidth': bw, 'pct_b': pct_b}


def _volume_z_series(volumes, period=20):
    s = pd.Series(np.asarray(volumes, dtype=float))
    n = len(s)
    if n < period:
        return np.zeros(n), np.ones(n)
    mean = s.rolling(period).mean()
    std = s.rolling(period).std()
    # 守卫：std 为 0/NaN 时 z 归 0；std>0 时才做除法，避免 inf
    std_val = std.values
    mean_val = mean.values
    _safe = np.where((std_val > 0) & np.isfinite(std_val), std_val, 1.0)
    z_raw = (s.values - mean_val) / _safe
    z = np.where((std_val > 0) & np.isfinite(std_val), z_raw, 0.0)
    return z, np.where(np.isfinite(mean_val), mean_val, 0.0)


def _bias_series(closes, periods=(5, 10, 20, 60, 120, 250)):
    s = pd.Series(np.asarray(closes, dtype=float))
    n = len(s)
    s_val = s.values
    out = {}
    for p in periods:
        if n >= p:
            m = s.rolling(p).mean().values
            # 守卫：m 为 0/NaN/inf 时 bias 归 0；fillna(0) 不处理 inf
            _safe = np.where((m > 0) & np.isfinite(m), m, 1.0)
            raw = (s_val - m) / _safe * 100
            out[f'bias_{p}'] = np.where((m > 0) & np.isfinite(m), raw, 0.0)
        else:
            out[f'bias_{p}'] = np.zeros(n)
    return out


def _bb_status(pctile, pct_b):
    if pctile < 15:
        sq = '极度收缩（即将变盘）'
    elif pctile < 30:
        sq = '收缩（关注突破）'
    elif pctile < 70:
        sq = '正常'
    elif pctile < 85:
        sq = '扩张（趋势持续）'
    else:
        sq = '极度扩张（注意风险）'
    if pctile < 15 and pct_b > 0.6:
        breakout = '⚠️ 收缩+偏上，可能向上突破'
    elif pctile < 15 and pct_b < 0.4:
        breakout = '⚠️ 收缩+偏下，可能向下突破'
    elif pctile < 15:
        breakout = '⏳ 极度收缩，方向待定'
    elif pct_b > 0.95:
        breakout = '触及上轨，短期超买'
    elif pct_b < 0.05:
        breakout = '触及下轨，短期超卖'
    else:
        breakout = '正常区间'
    return sq, breakout


def _vol_status(z):
    if z > 3:
        return '极端放量', True
    elif z > 2:
        return '显著放量', True
    elif z > 1:
        return '温和放量', False
    elif z < -2:
        return '极端缩量', True
    elif z < -1:
        return '显著缩量', False
    else:
        return '正常', False


def _bias_analyze(biases, last_close):
    analysis = []
    b5 = biases.get('bias_5', 0)
    b20 = biases.get('bias_20', 0)
    b60 = biases.get('bias_60', 0)
    if abs(b5) > 8:
        analysis.append(f'短期乖离过大({b5:+.1f}%)')
    elif abs(b5) > 5:
        analysis.append(f'短期偏离({b5:+.1f}%)')
    if abs(b20) > 15:
        analysis.append(f'中期乖离过大({b20:+.1f}%)')
    if abs(b60) > 25:
        analysis.append(f'长期乖离过大({b60:+.1f}%)')
    av = [v for v in biases.values() if v != 0]
    if av:
        pc = sum(1 for b in av if b > 0)
        if pc == len(av):
            direction = '价格高于所有均线，强势'
        elif pc == 0:
            direction = '价格低于所有均线，弱势'
        else:
            direction = '价格在均线之间震荡'
    else:
        direction = '数据不足'
    return {'biases': biases, 'alerts': analysis, 'direction': direction,
            'max_bias': max(av) if av else 0, 'min_bias': min(av) if av else 0}


def _kdj_j_series(highs_all, lows_all, closes_all, period=9):
    """整段 KDJ-J 序列（递推，O(n)，每只股票只算一次），供否决项按 idx 切片取值。

    递推公式与 KDJCalculator.calc 完全一致（k/d 初值 50，RSV 取 trailing period 窗口），
    因此最后一位 J 与 KDJCalculator.calc 返回一致，前一日 J 即索引 idx-1。
    """
    n = len(closes_all)
    if n < period + 1:
        return np.full(n, 50.0, dtype=float)
    k = np.full(n, 50.0, dtype=float)
    d = np.full(n, 50.0, dtype=float)
    highs = np.asarray(highs_all, dtype=float)
    lows = np.asarray(lows_all, dtype=float)
    closes = np.asarray(closes_all, dtype=float)
    for i in range(period, n):
        ll = float(np.min(lows[i - period:i]))
        hh = float(np.max(highs[i - period:i]))
        rsv = 50.0 if hh == ll else (closes[i] - ll) / (hh - ll) * 100.0
        k[i] = (2.0 * k[i - 1] + rsv) / 3.0
        d[i] = (2.0 * d[i - 1] + k[i]) / 3.0
    return 3.0 * k - 2.0 * d


def _precompute_tech_series(closes_all, highs_all, lows_all, volumes_all, opens_all, data_list_all):
    """整段序列预计算（每只股票只算一次，O(n)），供采样点切片取值，消除 O(n^2)。"""
    n = len(closes_all)
    pre = {}
    s = pd.Series(np.asarray(closes_all, dtype=float))
    # MA 序列（SMA + EMA）
    for p in [5, 10, 20, 60, 120, 250]:
        pre[f'sma_{p}'] = s.rolling(p).mean().values if n >= p else np.full(n, np.nan)
        pre[f'ema_{p}'] = s.ewm(span=p, adjust=False).mean().values if n >= p else np.full(n, np.nan)
    # 均线排列：按用户配置的基础均线周期派生 6 档判断均线（默认 20 → 5/10/20/60/120/250）
    _ma_base = _get_config_param('ma_arrangement', 'period', 20)
    pre['ma_cfg_period'] = _ma_base
    pre['ma_cfg'] = MATechnical.ma_periods(_ma_base)
    for _i, _pp in enumerate(pre['ma_cfg']):
        pre[f'sma_cfg{_i}'] = s.rolling(_pp).mean().values if n >= _pp else np.full(n, np.nan)
    # RSI 序列（多周期）
    pre['rsi_6'] = _rsi_series(closes_all, 6)
    pre['rsi_14'] = _rsi_series(closes_all, 14)
    pre['rsi_24'] = _rsi_series(closes_all, 24)
    # MACD 序列（三套）：前缀长度 < slow+signal 时原 calc 返回 0，需按阈值置 0
    macd_cfgs = {'mid': (12, 26, 9), 'short': (6, 13, 5), 'long': (24, 52, 18)}
    for tag, (f, sl, sg) in macd_cfgs.items():
        h = _macd_hist_series(closes_all, f, sl, sg)
        h = np.array(h, dtype=float)
        h[:sl + sg - 1] = 0.0
        pre[f'macd_hist_{tag}'] = h
    pre['macd_hist'] = np.array(pre['macd_hist_mid'], dtype=float)  # 已含 <34 置 0
    ema_fast = s.ewm(span=12, adjust=False).mean()
    ema_slow = s.ewm(span=26, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    macd_signal = macd_line.ewm(span=9, adjust=False).mean()
    pre['macd'] = np.array(macd_line.values, dtype=float); pre['macd'][:34] = 0.0
    pre['macd_signal'] = np.array(macd_signal.values, dtype=float); pre['macd_signal'][:34] = 0.0
    pre['macd_status'] = np.where(pre['macd'] > pre['macd_signal'], '金叉', '死叉')
    ef2 = s.ewm(span=6, adjust=False).mean()
    es2 = s.ewm(span=13, adjust=False).mean()
    ml2 = ef2 - es2
    ms2 = ml2.ewm(span=5, adjust=False).mean()
    pre['macd_short'] = np.array(ml2.values, dtype=float); pre['macd_short'][:17] = 0.0
    pre['macd_short_signal'] = np.array(ms2.values, dtype=float); pre['macd_short_signal'][:17] = 0.0
    ef3 = s.ewm(span=24, adjust=False).mean()
    es3 = s.ewm(span=52, adjust=False).mean()
    ml3 = ef3 - es3
    ms3 = ml3.ewm(span=18, adjust=False).mean()
    pre['macd_long'] = np.array(ml3.values, dtype=float); pre['macd_long'][:69] = 0.0
    pre['macd_long_signal'] = np.array(ms3.values, dtype=float); pre['macd_long_signal'][:69] = 0.0
    # MACD 快/慢/信号按用户配置计算的报告序列（默认 12/26/9 时与 macd_mid 一致不重复）
    _mf = _get_config_param('macd_hist_norm', 'fast', 12)
    _ms = _get_config_param('macd_hist_norm', 'slow', 26)
    _mg = _get_config_param('macd_hist_norm', 'signal', 9)
    pre['macd_cfg_periods'] = (_mf, _ms, _mg)
    if not (_mf == 12 and _ms == 26 and _mg == 9):
        _ef = s.ewm(span=_mf, adjust=False).mean()
        _es = s.ewm(span=_ms, adjust=False).mean()
        _ml4 = _ef - _es
        _ms4 = _ml4.ewm(span=_mg, adjust=False).mean()
        _warm = _ms + _mg - 1
        _line = np.array(_ml4.values, dtype=float); _line[:_warm] = 0.0
        _sig = np.array(_ms4.values, dtype=float); _sig[:_warm] = 0.0
        pre['macd_cfg'] = _line
        pre['macd_cfg_signal'] = _sig
        pre['macd_cfg_hist'] = _line - _sig
        pre['macd_cfg_hist'][:_warm] = 0.0
    # MACD 加速度 + 状态（基于整段 hist）
    accel, accel_status = _macd_accel_from_hist(pre['macd_hist'])
    pre['macd_accel'] = accel
    pre['macd_accel_status'] = accel_status
    # OBV 序列
    pre['obv'] = _obv_series(closes_all, volumes_all)
    # Bollinger 序列（bandwidth 为整段 rolling 带宽，供样本点按需算分位）
    bb = _bb_series(closes_all)
    pre['bb_upper'] = bb['upper']
    pre['bb_middle'] = bb['middle']
    pre['bb_lower'] = bb['lower']
    pre['bb_bandwidth'] = bb['bandwidth']
    pre['bb_pct_b'] = bb['pct_b']
    # KDJ-J 序列（供否决项切片，O(n) 一次）
    pre['kdj_j'] = _kdj_j_series(highs_all, lows_all, closes_all, period=9)
    # 成交量 z 序列
    pre['vol_z'], pre['vol_mean'] = _volume_z_series(volumes_all)
    # 乖离率序列
    pre.update(_bias_series(closes_all))
    # ATR 序列（Wilder 递推，精确复刻 ATRCalculator.calc_wilder）
    pre['atr'] = _atr_series(data_list_all, 14)
    # ---- 配置周期序列：报告/分析按用户配置周期如实展示，不再硬编码 ----
    # 与默认序列并存：默认序列(rsi_6/14/24、bb_*、kdj_j、atr、vol_z)供否决项/背离等
    # 固定口径使用；下述 _cfg 序列仅用于报告展示，读取失败回落默认周期。
    _rsi_cfg = _get_config_period('rsi_value', 14)
    pre['rsi_cfg'] = _rsi_series(closes_all, _rsi_cfg)
    pre['rsi_cfg_period'] = _rsi_cfg
    _bb_cfg = _get_config_period('bb_bandwidth', _BB_PERIOD)
    _bb_c = _bb_series(closes_all, period=_bb_cfg)
    pre['bb_cfg_upper'] = _bb_c['upper']
    pre['bb_cfg_middle'] = _bb_c['middle']
    pre['bb_cfg_lower'] = _bb_c['lower']
    pre['bb_cfg_bandwidth'] = _bb_c['bandwidth']
    pre['bb_cfg_pct_b'] = _bb_c['pct_b']
    pre['bb_cfg_period'] = _bb_cfg
    _kdj_cfg = _get_config_period('kdj_signal', 9)
    pre['kdj_j_cfg'] = _kdj_j_series(highs_all, lows_all, closes_all, period=_kdj_cfg)
    pre['kdj_cfg_period'] = _kdj_cfg
    _atr_cfg = _get_config_period('atr_norm', 14)
    pre['atr_cfg'] = _atr_series(data_list_all, _atr_cfg)
    pre['atr_cfg_period'] = _atr_cfg
    _vol_cfg = _get_config_period('volume_ratio', 20)
    pre['vol_cfg_period'] = _vol_cfg
    return pre


def _atr_series(data_list, period=14):
    n = len(data_list)
    out = np.zeros(n)
    if n < period + 1:
        return out
    highs = np.array([d['high'] for d in data_list], dtype=float)
    lows = np.array([d['low'] for d in data_list], dtype=float)
    closes = np.array([d['close'] for d in data_list], dtype=float)
    prev_closes = np.roll(closes, 1)
    prev_closes[0] = closes[0]
    tr1 = highs - lows
    tr2 = np.abs(highs - prev_closes)
    tr3 = np.abs(lows - prev_closes)
    tr = np.maximum(np.maximum(tr1, tr2), tr3)
    tr[0] = 0.0
    avg = np.zeros(n)
    avg[period] = tr[1:period + 1].mean()
    for i in range(period + 1, n):
        avg[i] = (avg[i - 1] * (period - 1) + tr[i]) / period
    return avg


def _rebuild_obv_analyze(state, idx, closes_all, volumes_all, pre):
    """O(1) 复刻 OBVCalculator.analyze(closes[:idx+1], volumes[:idx+1]) 输出。
    原版依赖：obv[-1]/obv[-5]/obv[-10]、obv_ma[-1]、closes[-1]/closes[-5]，
    全部可从 pre['obv'] / state['obv_ma10'] / closes_all 直接索引得到。"""
    n = idx + 1
    if n < 20 or pre.get('obv') is None:
        return {'signal': '数据不足', 'divergence': False, 'obv_breakout': False,
                'obv_trend': '数据不足', 'current_obv': 0, 'obv_ma': 0}
    obv_i = float(pre['obv'][idx])
    obv_5 = float(pre['obv'][idx - 4])
    # 原版 obv[-10] on 长度 idx+1 的列表 = obv[idx+1-10] = idx-9；原代码 idx-10 偏 1 bar → 修正
    obv_10 = float(pre['obv'][idx - 9])
    obv_ma_i = float(state['obv_ma10'][idx])
    if np.isnan(obv_ma_i):
        obv_ma_i = 0.0
    close_i = float(closes_all[idx])
    close_5 = float(closes_all[idx - 4])
    sp = (close_i - close_5) / close_5 * 100 if close_5 != 0 else 0
    sc = (obv_i - obv_5) / abs(obv_5) * 100 if obv_5 != 0 else 0
    obo = obv_i > obv_ma_i
    if sp > 1 and sc > 1:
        signal = '量价配合上涨'
        divergence = False
    elif sp > 1 and sc < -1:
        signal = '量价背离（上涨缩量）'
        divergence = True
    elif sp < -1 and sc > 1:
        signal = '资金收集（下跌放量承接）'
        divergence = True
    elif sp < -1 and sc < -1:
        signal = '量价配合下跌'
        divergence = False
    else:
        signal = '量价平稳'
        divergence = False
    if obv_i > obv_10:
        obv_trend = '上升'
    elif obv_i < obv_10:
        obv_trend = '下降'
    else:
        obv_trend = '持平'
    return {'signal': signal, 'divergence': divergence, 'obv_breakout': obo,
            'obv_trend': obv_trend, 'current_obv': obv_i, 'obv_ma': obv_ma_i}


def _rebuild_volume_analyze(state, idx, volumes_all):
    """O(1) 复刻 VolumeAnalyzer.detect_anomaly_zscore(volumes[:idx+1]) 输出。
    原版 z/mean 基于 volumes[-20:]（ddof=0），由 state['vol_z']/state['vol_mean'] 预计算；
    reliability 累计由 state['vol_total_cumsum']/state['vol_up_cumsum'] 提供。"""
    n = idx + 1
    if n < 20:
        return {'type': '数据不足', 'z_score': 0, 'alert': False,
                'volume_ratio': 1, 'mean_volume': 0, 'reliability': '数据不足'}
    vol_i = float(volumes_all[idx])
    mean_i = float(state['vol_mean'][idx])
    std_i = float(state['vol_std'][idx])
    z_i = float(state['vol_z'][idx])
    if np.isnan(mean_i):
        mean_i = 0.0
    if np.isnan(std_i) or std_i <= 0:
        return {'type': '正常', 'z_score': 0, 'alert': False,
                'volume_ratio': 1, 'mean_volume': mean_i, 'reliability': '正常'}
    if np.isnan(z_i):
        z_i = 0.0
    if z_i > 3:
        vt, alert = '极端放量', True
    elif z_i > 2:
        vt, alert = '显著放量', True
    elif z_i > 1:
        vt, alert = '温和放量', False
    elif z_i < -2:
        vt, alert = '极端缩量', True
    elif z_i < -1:
        vt, alert = '显著缩量', False
    else:
        vt, alert = '正常', False
    # reliability：原版循环 for i in range(period, len(hist))，hist = volumes[:-1]，
    # 切片后等效 i in [period, idx-1]（idx=len(volumes)-1）。
    # 信号仅在 放量 (z>2) 时 append；append 值为 1 if volumes[i+1] > volumes[i] else 0。
    # 当 idx-1 >= period 时（n >= period+1=21），累加才有意义。Slice 版 wr = sum/len*100。
    reliability = '数据不足'
    if n >= 60:
        # idx-1 是 slice 循环最后 i 值；cumsum 在 i 处的累加值即为截至 i 的累计
        if idx - 1 >= 20:
            total = int(state['vol_total_cumsum'][idx - 1])
            up = int(state['vol_up_cumsum'][idx - 1])
        else:
            total, up = 0, 0
        if total > 0:
            wr = up / total * 100
            if wr > 60:
                reliability = f'放量后上涨概率{wr:.0f}%（可靠）'
            elif wr > 40:
                reliability = f'放量后上涨概率{wr:.0f}%（中性）'
            else:
                reliability = f'放量后上涨概率{wr:.0f}%（反向）'
        else:
            reliability = '数据不足'
    volume_ratio = vol_i / mean_i if mean_i > 0 else 1
    return {'type': vt, 'z_score': z_i, 'alert': alert,
            'volume_ratio': volume_ratio, 'mean_volume': mean_i, 'reliability': reliability}


def _rebuild_bias_analyze(state, idx, closes_all, pre):
    """O(1) 复刻 BiasAnalyzer.analyze(closes[:idx+1]) 输出。复用 pre 已有的 bias_* 整段序列。"""
    n = idx + 1
    if n == 0:
        return {'biases': {}, 'alerts': [], 'direction': '数据不足', 'max_bias': 0, 'min_bias': 0}
    biases = {}
    for p in [5, 10, 20, 60, 120, 250]:
        v = pre.get(f'bias_{p}')
        if v is not None and idx < len(v) and not np.isnan(v[idx]):
            biases[f'bias_{p}'] = float(v[idx])
        else:
            biases[f'bias_{p}'] = 0.0
    b5 = biases.get('bias_5', 0)
    b20 = biases.get('bias_20', 0)
    b60 = biases.get('bias_60', 0)
    analysis = []
    if abs(b5) > 8:
        analysis.append(f'短期乖离过大({b5:+.1f}%)，有回调/反弹需求')
    elif abs(b5) > 5:
        analysis.append(f'短期偏离({b5:+.1f}%)，注意波动')
    if abs(b20) > 15:
        analysis.append(f'中期乖离过大({b20:+.1f}%)')
    if abs(b60) > 25:
        analysis.append(f'长期乖离过大({b60:+.1f}%)，极端行情')
    av = [v for v in biases.values() if v != 0]
    if av:
        pc = sum(1 for b in av if b > 0)
        if pc == len(av):
            direction = '价格高于所有均线，强势'
        elif pc == 0:
            direction = '价格低于所有均线，弱势'
        else:
            direction = '价格在均线之间震荡'
    else:
        direction = '数据不足'
    return {'biases': biases, 'alerts': analysis, 'direction': direction,
            'max_bias': max(av) if av else 0, 'min_bias': min(av) if av else 0}


def _rebuild_ma_arrangement(pre, idx, closes_all):
    """O(1) 复刻 MATechnical.judge_arrangement(closes[:idx+1], period) 输出。

    判断均线组取自预计算 pre['ma_cfg']（由用户配置的基础均线周期派生，默认
    20 → 5/10/20/60/120/250）；sma_cfg_i 为对应窗口的整段 SMA，NaN 处按同样规则回退
    到更短周期 SMA / 当前价，与 MATechnical.judge_arrangement 的 fallback 语义一致。
    """
    n = idx + 1
    cfgs = pre.get('ma_cfg')
    if not cfgs:
        cfgs = [5, 10, 20, 60, 120, 250]
    p1, p2, p3, p4, p5, p6 = cfgs
    if not cfgs or n < (p1 or 5):
        return '数据不足'
    current = float(closes_all[idx])
    prev = current
    sma = {}
    for _i, _pp in enumerate(cfgs):
        _arr = pre.get(f'sma_cfg{_i}')
        if _arr is not None and not np.isnan(_arr[idx]):
            prev = float(_arr[idx])
        sma[_pp] = prev
    m1 = sma[p1]; m2 = sma[p2]; m3 = sma[p3]; m4 = sma[p4]; m5 = sma[p5]; m6 = sma[p6]
    if n >= p6 and current > m1 > m2 > m3 > m4 > m5 > m6:
        return '完美多头排列'
    if current > m1 > m2 > m3 > m4:
        return '多头排列'
    if m1 > m2 > m3 and m3 > 0 and abs(current - m3) / m3 < 0.03:
        return '多头初期·均线发散'
    _mi = min(m1, m2, m3, m4)
    if m4 > 0 and _mi > 0 and max(m1, m2, m3, m4) / _mi < 1.06:
        return '均线黏合·即将变盘'
    if m1 < m2 < m3 and m3 > 0 and abs(current - m3) / m3 < 0.03:
        return '空头初期·均线发散'
    if current < m1 < m2 < m3 < m4:
        return '空头排列'
    if n >= p6 and current < m1 < m2 < m3 < m4 < m5 < m6:
        return '完美空头排列'
    return '均线交织'


def _precompute_per_sample_state(closes_all, highs_all, lows_all, volumes_all, opens_all, data_list_all, pre):
    """每只股票预计算每采样点 state，消除 _build_tech 中 OBV/Volume/Bias/MA 排列 4 个 O(n) 调用。
    复杂度：每股票 O(n) 一次（原是 O(n) × 50 sample），总体 ~50x 加速。
    """
    n = len(closes_all)
    # OBV MA(10) 整段序列（与原 OBVCalculator.calc_ma 等价）
    obv_arr = pre.get('obv')
    if obv_arr is not None and len(obv_arr) >= 10:
        obv_ma10 = pd.Series(obv_arr).rolling(10).mean().values
        obv_ma10 = np.where(np.isnan(obv_ma10), 0.0, obv_ma10)
    else:
        obv_ma10 = np.zeros(n)
    # 成交量 rolling 20 z/mean/std（ddof=0 匹配原 np.std）；窗口跟随 volume_ratio 配置周期
    _vol_p = int(pre.get('vol_cfg_period', 20))
    vs = pd.Series(np.asarray(volumes_all, dtype=float))
    if n >= _vol_p:
        vol_mean = vs.rolling(_vol_p).mean().values
        vol_std = vs.rolling(_vol_p).std(ddof=0).values
        vol_z = np.where(vol_std > 0, (vs.values - vol_mean) / np.where(vol_std > 0, vol_std, 1.0), 0.0)
        # NaN（前 _vol_p-1）置 0
        vol_mean = np.where(np.isnan(vol_mean), 0.0, vol_mean)
        vol_std = np.where(np.isnan(vol_std), 0.0, vol_std)
        vol_z = np.where(np.isnan(vol_z), 0.0, vol_z)
    else:
        vol_mean = np.zeros(n)
        vol_std = np.zeros(n)
        vol_z = np.zeros(n)
    # reliability 累计（首/次累计）：i in [_vol_p, n-2]，对 volumes[i] 做 _vol_p 窗口放量判断
    # 仅在 放量 (z>2) 时 total+=1，append 值 1 if volumes[i+1] > volumes[i] else 0
    vol_total = np.zeros(n)
    vol_up = np.zeros(n)
    for i in range(_vol_p, n - 1):
        r = vs.values[i - _vol_p:i]
        if len(r) < _vol_p:
            continue
        m = r.mean()
        s = r.std(ddof=0)
        if s <= 0:
            continue
        z = (vs.values[i] - m) / s
        if z > 2:
            vol_total[i] = 1
            if i + 1 < n and vs.values[i + 1] > vs.values[i]:
                vol_up[i] = 1
    return {
        'obv_ma10': obv_ma10,
        'vol_z': vol_z,
        'vol_mean': vol_mean,
        'vol_std': vol_std,
        'vol_total_cumsum': np.cumsum(vol_total),
        'vol_up_cumsum': np.cumsum(vol_up),
    }


def _build_tech(pre, state, idx, closes_all, highs_all, lows_all, volumes_all, opens_all, data_list_all):
    """从预计算序列 pre/state 切片取值（O(1)），消除 O(n^2) 重复；
    其余依赖截至 idx 子序列的指标保留原 calc_* 调用（单次 O(n) 小头，结果逐位一致）。"""
    n = idx + 1
    closes = np.asarray(closes_all[:n], dtype=float)
    highs = np.asarray(highs_all[:n], dtype=float)
    lows = np.asarray(lows_all[:n], dtype=float)
    volumes = np.asarray(volumes_all[:n], dtype=float)
    opens = np.asarray(opens_all[:n], dtype=float)
    data_list = data_list_all[:n]
    tech = {}
    # MA（从 pre 切片取值，O(1)）
    for p in [5, 10, 20, 60, 120, 250]:
        sk, ek = f'sma_{p}', f'ema_{p}'
        if sk in pre and idx < len(pre[sk]):
            v = float(pre[sk][idx])
            tech[sk] = v if not np.isnan(v) else float(closes[-1])
        else:
            tech[sk] = float(closes[-1])
        if ek in pre and idx < len(pre[ek]):
            v = float(pre[ek][idx])
            tech[ek] = v if not np.isnan(v) else float(closes[-1])
        else:
            tech[ek] = float(closes[-1])
    # RSI（从预计算切片，消除 O(n^2) 前缀循环）；报告用配置周期，rsi_6/14/24 供否决项等固定口径
    tech['rsi_6'] = float(pre['rsi_6'][idx])
    tech['rsi_14'] = float(pre['rsi_14'][idx])
    tech['rsi_24'] = float(pre['rsi_24'][idx])
    tech['rsi'] = float(pre['rsi_cfg'][idx])
    # MACD（从预计算切片，消除 O(n^2) hist_series + accel）
    tech['macd'] = float(pre['macd'][idx])
    tech['macd_signal'] = float(pre['macd_signal'][idx])
    tech['macd_hist'] = float(pre['macd_hist'][idx])
    # MACD 报告值：用户配置周期 ≠ 默认(12/26/9)时用配置序列覆盖（与 macd_hist_norm 打分一致）
    _mcfg = pre.get('macd_cfg')
    if _mcfg is not None:
        tech['macd'] = float(_mcfg[idx])
        tech['macd_signal'] = float(pre['macd_cfg_signal'][idx])
        tech['macd_hist'] = float(pre['macd_cfg_hist'][idx])
    tech['macd_status'] = '金叉' if tech['macd'] > tech['macd_signal'] else '死叉'
    tech['macd_short'] = float(pre['macd_short'][idx])
    tech['macd_short_signal'] = float(pre['macd_short_signal'][idx])
    tech['macd_long'] = float(pre['macd_long'][idx])
    tech['macd_long_signal'] = float(pre['macd_long_signal'][idx])
    tech['macd_accel'] = float(pre['macd_accel'][idx])
    tech['macd_accel_status'] = str(pre['macd_accel_status'][idx])
    # 背离：依赖子序列模式（detect_macd/detect_rsi 内部用 closed window），
    # 保留原调用；传 pre 切片避免重复计算 hist/rsi。
    macd_hists = pre['macd_hist'][:n]
    rsi_hist = pre['rsi_cfg'][:n]
    tech['macd_divergence'] = DivergenceDetector.detect_macd(closes, macd_hists)
    tech['rsi_divergence'] = DivergenceDetector.detect_rsi(closes, rsi_hist)
    # OBV：O(1) 重建（消除原 OBVCalculator.analyze O(n) cumsum + rolling）
    tech['obv_data'] = _rebuild_obv_analyze(state, idx, closes_all, volumes_all, pre)
    # 布林带（squeeze/breakout；pctile 按样本点精确复刻原 BollingerAnalyzer.calc 的分位逻辑）
    bw = pre['bb_cfg_bandwidth']
    _bb_cfg_p = int(pre.get('bb_cfg_period', _BB_PERIOD))
    p_from = _bb_cfg_p - 1
    if idx >= p_from:
        prefix_bw = bw[p_from:idx + 1]
        bb_pctile = float((prefix_bw < bw[idx]).mean() * 100.0)
    else:
        bb_pctile = 50.0
    sq, bo = _bb_status(bb_pctile, pre['bb_cfg_pct_b'][idx])
    tech['bb_squeeze'] = sq
    tech['bb_pct_b'] = float(pre['bb_cfg_pct_b'][idx])
    tech['breakout_signal'] = bo
    # 成交量：O(1) 重建（消除原 VolumeAnalyzer.detect_anomaly_zscore O(n) reliability 循环）
    vol_data = _rebuild_volume_analyze(state, idx, volumes_all)
    tech['volume_ratio'] = vol_data.get('volume_ratio', 1)
    tech['volume_z_score'] = vol_data.get('z_score', 0)
    tech['volume_status'] = vol_data.get('type', '正常')
    tech['volume_alert'] = vol_data.get('alert', False)
    tech['volume_reliability'] = vol_data.get('reliability', '')
    # 乖离率：O(1) 重建（消除原 BiasAnalyzer.analyze O(n) 多窗口均值）
    tech['bias_analysis'] = _rebuild_bias_analyze(state, idx, closes_all, pre)
    # 形态/波动率锥/筹码/斐波/枢轴：保留原 calc_* 调用（每次仅 O(n) 小头，逻辑复杂、
    # 数据形态依赖完整 data_list 字典，逐位一致优先；不在本轮 O(n^2) 消除范围内）。
    tech['candlestick_patterns'] = CandlestickPatterns.detect(opens, highs, lows, closes)
    tech['volatility_cone'] = VolatilityCone.calc(closes, lookback=_get_config_period('volatility_cone', 20))
    tech['chip_concentration'] = calc_chip_concentration(
        data_list, periods=_get_config_period('fib_position', 120),
        bins=_get_config_period('chip_concentration', 50)) or {}
    tech['fibonacci'] = calc_fibonacci_retracement(
        data_list, lookback=_get_config_period('fib_position', 120)) or {}
    tech['pivot_points'] = calc_pivot_points(data_list) or {}
    atr = float(pre['atr_cfg'][idx]) if pre['atr_cfg'][idx] > 0 else 0.0
    tech['atr'] = atr
    tech['atr_ratio'] = atr / closes[-1] if closes[-1] > 0 else 0.02

    # KDJ / ADX：补齐参考版 scoring_core.build_tech 中存在的键（缺键会导致下游因子 KeyError）。
    # KDJ-J 用预计算切片(O(1))，KDJ-K/D/信号与 ADX 直接调 calc_*（周期取配置值，报告如实展示）。
    tech.update(KDJCalculator.calc_all(highs, lows, closes, period=int(pre.get('kdj_cfg_period', 9))))
    adx_result = ADXCalculator.calc_adx(highs, lows, closes, _get_config_period('adx_trend_strength', 14))
    tech['adx'] = adx_result.get('adx', 25)
    tech['adx_state'] = adx_result.get('state', '')
    tech['di_plus'] = adx_result.get('di_plus', 0)
    tech['di_minus'] = adx_result.get('di_minus', 0)

    # 极端否决项原始信号（O(1) 切片 pre/tech，供入场逻辑/分类器使用）
    tech.update(_build_extreme_vetos(pre, idx, closes, tech))

    # 博反弹/恐慌反转新增信号（独立 tech 键，不污染共享键）
    tech.update(compute_reversal_extra_signals(closes, highs, lows, volumes, opens))

    market = {
        # MA 排列：O(1) 重建（消除原 MATechnical.judge_arrangement 多次 calc_sma 调用）
        'ma_arrangement': _rebuild_ma_arrangement(pre, idx, closes_all),
        'volume_price': '正常',
        'mtf_score': 0,
        'up_ratio': 0.5,
    }
    return tech, market


def _build_extreme_vetos(pre, idx, closes, tech, finance=None):
    """计算 6 个极端否决项的原始触发状态（基于 pre 切片，O(1)），注入 tech。

    对应 quant_config.VETO_KEYS（右侧追涨档 强/标/探/望 的过热/破位否决项）：
        rsi_extreme / kdj_extreme / macd_high_dead_cross /
        ma250_break / bb_lower_break_widen / volume_stagnant
    博反弹/恐慌（左侧抄底档）按设计不使用否决项，故不在此列。

    2026-08 重构为可插拔：判定逻辑全部收敛到 veto_registry.VETO_REGISTRY，
    本函数仅负责以统一 ctx 遍历注册表并返回布尔 dict（行为与原硬编码完全一致）。
    否决项阈值从方案配置读取（quant_config.get_veto_params 注入），实现「阈值可调」。
    """
    from engine import veto_registry
    from engine import quant_config
    return veto_registry.evaluate_vetos(tech, pre, idx, closes,
                                        params=quant_config.get_veto_params(),
                                        finance=finance)


def compute_extreme_vetos(closes, highs, lows, volumes, opens, data_list, tech,
                          stock_code=None, finance=None):
    """为实时单股分析（`engine/analyze_service.py`：A股 analyze 传 stock_code、
    期货 analyze 不传）计算 12 个极端否决项原始触发信号（多头 6 + 空头镜像 6），
    逻辑与回测路径 _build_tech 完全一致。

    tech 需已含 rsi_14 / sma_250 / volume_ratio（上述两条实时路径均已构建这些字段）；
    函数内部构造 pre 序列切片取值，保证与回测路径同一套判定口径，避免双份漂移。

    stock_code / finance：可选。实时分析与诊断传入股票代码，用于拉取财务并驱动
        finance_risk 否决项。缺省（旧调用/批量）不拉财务，finance_risk 安全降级 False。

    鲁棒性：
      - 序列过短（<2 根）直接返回全 False（无从判定，等价于无否决）；
      - 过热否决项依赖完整 OHLC 的 pre 序列，若 data_list 缺失 OHLC 无法构造 pre，
        安全降级为 False（不confirm极端即不否决）。

    Returns:
        dict: 12+1 个否决项布尔值（VETO_KEYS 多头 + SHORT_VETO_KEYS 空头镜像
        （+ finance_risk 财务爆雷，LONG，缺财务时 False）；博反弹/恐慌档不使用否决项，
        不在此列）。
    """
    from engine import quant_config
    n = len(closes)
    all_keys = set(quant_config.VETO_KEYS) | set(quant_config.SHORT_VETO_KEYS)
    if n < 2:
        return {k: False for k in all_keys}
    vetos = {k: False for k in all_keys}
    # 实时分析注入财务（仅 finance 为 None 且给了股票的路径考虑拉取；否则直接用传入值）
    if finance is None and stock_code:
        from engine.data_sources import tdx
        _fin, _ = tdx.fetch_finance(stock_code)
        finance = _fin
    # 过热/破位否决项依赖 pre（需要完整 OHLC）；缺失时安全降级为 False
    try:
        pre = _precompute_tech_series(closes, highs, lows, volumes, opens, data_list)
        idx = n - 1
        vetos.update(_build_extreme_vetos(pre, idx, closes, tech, finance=finance))
    except Exception as e:
        # 缺少完整 OHLC / 序列异常时无法判定过热否决项，安全降级为 False，
        # 但必须记录告警——否则实时诊断里否决项会静默失效（fail-open），
        # 与回测路径（走 _build_extreme_vetos，无 try 包裹）口径不一致。
        logger.warning("compute_extreme_vetos 降级为全 False（数据异常）：%s", e)
    return vetos


def compute_reversal_extra_signals(closes, highs, lows, volumes, opens):
    """博反弹/恐慌反转打分所需的 3(+1) 个新增信号，作为独立 tech 键注入。

    完全独立于既有共享键（candlestick_patterns / volume_z_score / volume_ratio 等），
    避免污染主因子评分（score_calculator_v2 / signal_rating / factor_registry）。

    底部反转信号（多单博反弹用）：
      - reversal_explosive_up    : 恐慌性爆量长阳（量>5日均量1.5倍 且 收涨实体>2%）
      - reversal_no_new_low_2d   : 连续2日不再创新低（最近2日收盘价均不创新低）
      - reversal_morning_star    : 早晨之星形态（独立键，不写入 candlestick_patterns）
      - reversal_extreme_oversold: 极端超跌（收盘价位于布林下轨外侧 或 20日乖离率≤-8%）
    顶部反转信号（空单摸顶做空用，底部信号的镜像）：
      - reversal_explosive_down    : 爆量长阴（量>5日均量1.5倍 且 收跌实体>2%）
      - reversal_no_new_high_2d   : 连续2日不再创新高（最近2日收盘价均不创新高）
      - reversal_evening_star     : 黄昏之星/倒锤子形态（独立键，不写入 candlestick_patterns）
      - reversal_extreme_overbought: 极端超买（收盘价位于布林上轨外侧 或 20日乖离率≥8%）
    """
    result = {
        'reversal_explosive_up': False,
        'reversal_no_new_low_2d': False,
        'reversal_morning_star': False,
        'reversal_extreme_oversold': False,
        'reversal_explosive_down': False,
        'reversal_no_new_high_2d': False,
        'reversal_evening_star': False,
        'reversal_extreme_overbought': False,
    }
    n = len(closes)
    if n < 3:
        return result
    try:
        closes = [float(x) for x in closes]
        volumes = [float(x) for x in volumes]
        opens = [float(x) for x in opens]
        highs = [float(x) for x in highs]
        lows = [float(x) for x in lows]
    except (TypeError, ValueError):
        return result

    c1, c2, c3 = closes[-1], closes[-2], closes[-3]

    # 1. 连续2日不再创新低 / 不再创新高
    result['reversal_no_new_low_2d'] = (c1 >= c2) and (c2 >= c3)
    result['reversal_no_new_high_2d'] = (c1 <= c2) and (c2 <= c3)

    # 2. 爆量长阳 / 爆量长阴：量 > 5日均量1.5倍 且 实体 > 2%
    win = volumes[-5:] if n >= 5 else volumes
    avg5 = sum(win) / len(win) if len(win) > 0 else 0.0
    vol_ratio = (volumes[-1] / avg5) if avg5 > 0 else 1.0
    up_entity_pct = ((c1 - opens[-1]) / c2 * 100.0) if c2 > 0 else 0.0
    result['reversal_explosive_up'] = (vol_ratio > 1.5) and (up_entity_pct > 2.0)
    down_entity_pct = ((opens[-1] - c1) / c2 * 100.0) if c2 > 0 else 0.0
    result['reversal_explosive_down'] = (vol_ratio > 1.5) and (down_entity_pct > 2.0)

    # 3. 早晨之星 / 黄昏之星（三日 K 线）
    result['reversal_morning_star'] = _detect_morning_star(opens, highs, lows, closes)
    result['reversal_evening_star'] = _detect_evening_star(opens, highs, lows, closes)

    # 4. 极端超跌 / 极端超买：布林轨道外 或 20日乖离率越界
    if n >= 20:
        m20 = sum(closes[-20:]) / 20.0
        if m20 > 0:
            var20 = sum((x - m20) ** 2 for x in closes[-20:]) / 20.0
            std20 = var20 ** 0.5
            bb_lower = m20 - 2.0 * std20
            bb_upper = m20 + 2.0 * std20
            bias20 = (c1 - m20) / m20 * 100.0
            result['reversal_extreme_oversold'] = (c1 < bb_lower) or (bias20 <= -8.0)
            result['reversal_extreme_overbought'] = (c1 > bb_upper) or (bias20 >= 8.0)

    return result


def _detect_evening_star(opens, highs, lows, closes):
    """三日黄昏之星形态识别（独立实现，不污染共享 candlestick_patterns）。

    判定：① 第一日为实体较长的阳线；
          ② 第二日实体极小（≤第一日振幅30%）且最高价高于第一日最高（向上跳空）；
          ③ 第三日为阴线且收盘低于第一日实体中点。
    """
    try:
        o1, o2, o3 = opens[-3], opens[-2], opens[-1]
        h1, h2, h3 = highs[-3], highs[-2], highs[-1]
        l1, l2, l3 = lows[-3], lows[-2], lows[-1]
        c1, c2, c3 = closes[-3], closes[-2], closes[-1]
        # 第一日阳线
        if not (c1 > o1):
            return False
        body1 = abs(c1 - o1)
        rng1 = h1 - l1
        if rng1 <= 0:
            return False
        # 第二日极小实体 + 向上跳空
        body2 = abs(c2 - o2)
        if body2 > rng1 * 0.30:
            return False
        if h2 <= h1:
            return False
        # 第三日阴线，收盘低于第一日实体中点
        if not (c3 < o3):
            return False
        mid1 = (o1 + c1) / 2.0
        return c3 < mid1
    except (IndexError, TypeError, ValueError):
        return False


def _detect_morning_star(opens, highs, lows, closes):
    """三日早晨之星形态识别（独立实现，不污染共享 candlestick_patterns）。

    判定：① 第一日为实体较长的阴线；
          ② 第二日实体极小（≤第一日振幅30%）且最低价低于第一日最低（向下跳空）；
          ③ 第三日为阳线且收盘高于第一日实体中点。
    """
    n = len(closes)
    if n < 3:
        return False
    o1, h1, l1, c1 = opens[-3], highs[-3], lows[-3], closes[-3]
    o2, h2, l2, c2 = opens[-2], highs[-2], lows[-2], closes[-2]
    o3, h3, l3, c3 = opens[-1], highs[-1], lows[-1], closes[-1]

    # 第一日：阴线（收盘<开盘），实体不可忽略
    body1 = abs(c1 - o1)
    if c1 >= o1 or body1 <= 0:
        return False
    rng1 = (h1 - l1) if (h1 - l1) > 0 else 1e-9
    # 第二日：实体极小（≤第一日振幅30%），且向下跳空（最低低于第一日最低）
    body2 = abs(c2 - o2)
    if body2 > 0.3 * rng1:
        return False
    if not (l2 < l1):
        return False
    # 第三日：阳线，收盘高于第一日实体中点
    mid1 = (o1 + c1) / 2.0
    if not (c3 > o3 and c3 > mid1):
        return False
    return True


