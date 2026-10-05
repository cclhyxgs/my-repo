# -*- coding: utf-8 -*-
"""V5 评分核心的单一实现（无 UI / 无 backtest 重依赖）。

此前「tech/market 指标构造 + calc_stock_score + UnifiedScorer」逻辑被手抄于三处：
  - backtest_strategy._st_compute_full_analysis
  - backtest._bt_compute_score_classify
  - engine.market_scan_core._quick_score_core
现统一收口到此模块，三方共用，杜绝「改 A 忘 B」的评分不一致。

设计约束：
  - 本模块只依赖 engine 内部纯逻辑（indicators / score_calculator_v2 / unified_scorer），
    不 import backtest / ui，因此可被 GUI 进程与回测子进程安全复用。
  - rsi_hist 与 macd_accel 因历史实现来源不同（数值等价、仅边缘 fallback 文案微差）
    由调用方显式传入，确保各调用方行为零回归。
"""

from engine.indicators import (MATechnical, RSICalculator, MACDCalculator,
                                DivergenceDetector, OBVCalculator, BollingerAnalyzer,
                                VolumeAnalyzer, BiasAnalyzer, CandlestickPatterns,
                                VolatilityCone, ATRCalculator, ADXCalculator,
                                KDJCalculator, WilliamRCalculator)
from engine.indicators_advanced import (calc_chip_concentration,
                                         calc_fibonacci_retracement,
                                         calc_pivot_points)
from engine.score_calculator_v2 import ScoreCalculatorV2
from engine.unified_scorer import UnifiedScorer


def _rsi_series_fast(closes, period=14):
    """增量 Wilder RSI 序列（O(n)）。与 market_scan_core 原同名函数逐字一致。"""
    n = len(closes)
    if n < period + 1:
        return [50.0] * n
    series = [50.0] * n
    init_deltas = [closes[i] - closes[i - 1] for i in range(1, period + 1)]
    avg_gain = sum(max(d, 0) for d in init_deltas) / period
    avg_loss = sum(max(-d, 0) for d in init_deltas) / period
    if avg_loss == 0:
        series[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        series[period] = max(0.0, min(100.0, 100 - 100 / (1 + rs)))
    for i in range(period + 1, n):
        d = closes[i] - closes[i - 1]
        gain = max(d, 0)
        loss = max(-d, 0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            series[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            series[i] = max(0.0, min(100.0, 100 - 100 / (1 + rs)))
    return series


def _get_factor_period(factor_name, default):
    """读取指定因子的配置周期（period / ma_period / lookback）。读取失败回落默认。"""
    try:
        from engine import quant_config
        _fcfg = quant_config.get_factor_configs() or {}
        _cfg = _fcfg.get(factor_name, {})
        _pm = _cfg.get('params') or {}
        # 允许键名 fallback：period / ma_period / lookback
        for _k in ('period', 'ma_period', 'lookback'):
            _vp = _pm.get(_k)
            if isinstance(_vp, (int, float)) and int(_vp) >= 2:
                return int(_vp)
    except Exception:
        pass
    return default


def _get_factor_param(factor_name, key, default):
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


def _get_volume_ratio_period(default=20):
    """量比周期：解析 volume_ratio 因子配置（默认 20）。"""
    return _get_factor_period('volume_ratio', default)


def build_market_dict(closes, volumes, up_ratio):
    """构建 market 上下文字典（三处调用方完全一致）。

    量价关系判断的量比周期：跟随用户配置的量比因子（volume_ratio）周期，
    未配置时默认 20；均线排列基础周期取 ma_arrangement 因子周期。
    """
    vol_period = _get_volume_ratio_period(20)
    _ma_base = _get_factor_period('ma_arrangement', 20)

    market = {
        'ma_arrangement': MATechnical.judge_arrangement(closes, _ma_base),
        'volume_price': '正常',
        'mtf_score': 0,
        'up_ratio': up_ratio
    }
    if len(volumes) >= vol_period:
        vs = volumes[-1] / (sum(volumes[-vol_period:]) / vol_period) if sum(volumes[-vol_period:]) > 0 else 1
        if vs > 1.2 and closes[-1] > closes[-2]:
            market['volume_price'] = '放量上涨'
        elif vs > 1.2 and closes[-1] < closes[-2]:
            market['volume_price'] = '放量下跌'
        elif vs < 0.7 and closes[-1] > closes[-2]:
            market['volume_price'] = '缩量上涨'
        elif vs < 0.7 and closes[-1] < closes[-2]:
            market['volume_price'] = '缩量下跌'
    return market


def build_tech(closes, volumes, highs, lows, opens, data_list, rsi_hist, macd_accel, rsi_period=14):
    """构造共享的 tech 指标字典（三处调用方核心一致的部分）。

    macd_hists 在三处均由 MACDCalculator.calc_hist_series(closes) 得到（完全一致），
    故内部计算；rsi_hist 与 macd_accel 由调用方显式传入。

    rsi_period：用户配置的 RSI 周期（默认 14），用于显示值 tech['rsi'] 和背离检测。
    """
    macd_hists = MACDCalculator.calc_hist_series(closes)
    tech = {}
    # 技术参数统一从因子配置派生（rsi_period 由调用方经 build_tech 形参传入）
    _vol_period = _get_factor_period('volume_ratio', 20)
    _adx_period = _get_factor_period('adx_trend_strength', 14)
    _atr_period = _get_factor_period('atr_norm', 14)
    _bb_period = _get_factor_param('bb_bandwidth', 'period', 20)
    _bb_std = 2.0
    _kdj_period = _get_factor_period('kdj_signal', 9)
    # 威廉 WR 周期：跟随 RSI/KDJ 同源配置风格，默认 14
    _wr_period = _get_factor_period('rsi', 14)
    tech.update(MATechnical.calc_all_ma(closes))
    # 按用户配置的 RSI 周期计算；未配置时回落 [6,14,24] 全桶
    rsi_buckets = RSICalculator.calc_all(closes, periods=[6, 14, 24, int(rsi_period)])
    tech.update(rsi_buckets)
    rsi_key = f'rsi_{int(rsi_period)}'
    tech['rsi'] = rsi_buckets.get(rsi_key, rsi_buckets.get('rsi_14', 50))
    tech['rsi_period'] = int(rsi_period)
    tech.update(MACDCalculator.calc_all(closes))
    # MACD 快/慢/信号周期按用户配置覆盖报告展示值（与 macd_hist_norm 因子打分一致）；
    # 默认 12/26/9 时不改动 calc_all 输出，行为零回归。
    _m_fast = _get_factor_param('macd_hist_norm', 'fast', 12)
    _m_slow = _get_factor_param('macd_hist_norm', 'slow', 26)
    _m_sig = _get_factor_param('macd_hist_norm', 'signal', 9)
    if not (_m_fast == 12 and _m_slow == 26 and _m_sig == 9):
        _ml, _msig, _mhist = MACDCalculator.calc(closes, _m_fast, _m_slow, _m_sig)
        tech['macd'] = _ml
        tech['macd_signal'] = _msig
        tech['macd_hist'] = _mhist

    tech['macd_accel'], tech['macd_accel_status'] = macd_accel
    tech['macd_divergence'] = DivergenceDetector.detect_macd(closes, macd_hists)
    # RSI 背离检测：与 factor_tech._build_tech 口径一致，用用户配置周期序列；
    # 仅当配置周期 != 14 时重建（默认 14 沿用调用方传入的 rsi_hist，行为零回归）。
    if int(rsi_period) != 14:
        rsi_hist = _rsi_series_fast(closes, int(rsi_period))
    tech['rsi_divergence'] = DivergenceDetector.detect_rsi(closes, rsi_hist)

    tech['obv_data'] = OBVCalculator.analyze(closes, volumes)
    bb_data = BollingerAnalyzer.calc(closes, period=_bb_period, std_dev=_bb_std)
    tech['bb_squeeze'] = bb_data.get('squeeze_status', '')
    tech['bb_pct_b'] = bb_data.get('pct_b', 0.5)
    tech['bb_upper'] = bb_data.get('upper', None)
    tech['bb_mid'] = bb_data.get('middle', None)
    tech['bb_lower'] = bb_data.get('lower', None)
    tech['bb_bandwidth'] = bb_data.get('bandwidth', 0)
    tech['breakout_signal'] = bb_data.get('breakout_signal', '')
    vol_data = VolumeAnalyzer.detect_anomaly_zscore(volumes, _vol_period)
    tech['volume_ratio'] = vol_data.get('volume_ratio', 1)
    tech['volume_z_score'] = vol_data.get('z_score', 0)
    tech['volume_status'] = vol_data.get('type', '正常')
    tech['volume_alert'] = vol_data.get('alert', False)
    tech['volume_reliability'] = vol_data.get('reliability', '')
    tech['bias_analysis'] = BiasAnalyzer.analyze(closes)
    tech['candlestick_patterns'] = CandlestickPatterns.detect(opens, highs, lows, closes)
    tech['volatility_cone'] = VolatilityCone.calc(closes, lookback=_get_factor_period('volatility_cone', 20))
    tech['chip_concentration'] = calc_chip_concentration(
        data_list, periods=_get_factor_period('fib_position', 120),
        bins=_get_factor_period('chip_concentration', 50)) or {}
    tech['fibonacci'] = calc_fibonacci_retracement(
        data_list, lookback=_get_factor_period('fib_position', 120)) or {}
    tech['pivot_points'] = calc_pivot_points(data_list) or {}
    atr = ATRCalculator.calc_wilder(data_list, _atr_period) or 0
    tech['atr'] = atr
    tech['atr_ratio'] = atr / closes[-1] if closes[-1] > 0 else 0.02
    tech.update(KDJCalculator.calc_all(highs, lows, closes, period=_kdj_period))
    wr_data = WilliamRCalculator.calc(highs, lows, closes, period=_wr_period)
    tech['wr'] = wr_data.get('wr', -50.0)
    tech['wr_state'] = wr_data.get('state', '数据不足')
    # 将 ADX 值存入 tech，供加仓/减仓引擎触发条件使用
    adx_result = ADXCalculator.calc_adx(highs, lows, closes, _adx_period)
    tech['adx'] = adx_result.get('adx', 25)
    tech['adx_state'] = adx_result.get('state', '')
    tech['di_plus'] = adx_result.get('di_plus', 0)
    tech['di_minus'] = adx_result.get('di_minus', 0)
    # 博反弹/恐慌反转新增信号（独立 tech 键，不污染主评分共享键）
    from engine.factor_tech import compute_reversal_extra_signals
    tech.update(compute_reversal_extra_signals(closes, highs, lows, volumes, opens))
    return tech


def compute_stock_score(closes, volumes, highs, lows, opens, data_list, up_ratio,
                        rsi_hist, macd_accel, direction='long', period='日K', market_type='stock', rsi_period=None):
    """三处调用方共用的「评分 + final」内核。

    返回 (stock_score, final_result, tech, market, adx_result, tech_strength)，其中 final_result 是
    UnifiedScorer.calculate_final_score 的完整返回字典（含 confidence_factor 等），
    供调用方进一步做入场/分类（回测）或信号评级（全市场扫描）。

    direction: 评分视角，'long'=多头（默认），'short'=空头（期货扫描方向化预留）。
    period: 分析周期（'日K'/'15分钟' 等），透传到因子窗口缩放与校准分桶。
    rsi_period: 用户配置的 RSI 周期；默认 None 时从因子配置读取（rsi_value），
               确保回测/扫描路径与报告口径一致地「配置周期即生效」。
    """
    if rsi_period is None:
        rsi_period = _get_factor_period('rsi_value', 14)
    rsi_period = int(rsi_period)
    tech = build_tech(closes, volumes, highs, lows, opens, data_list, rsi_hist, macd_accel, rsi_period=rsi_period)
    market = build_market_dict(closes, volumes, up_ratio)
    adx_result = ADXCalculator.calc_adx(highs, lows, closes)
    stock_score, _, _, _, _, tech_strength, _ = ScoreCalculatorV2.calc_stock_score(
        tech, market, closes[-1], adx_result,
        closes=closes, highs=highs, lows=lows, volumes=volumes, opens=opens,
        data_list=data_list, direction=direction, period=period
    )
    final_result = UnifiedScorer.calculate_final_score(stock_score, up_ratio, market_type)
    return stock_score, final_result, tech, market, adx_result, tech_strength
