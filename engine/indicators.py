#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""指标层 v4.0 - 新增ADX + 均线过渡态"""

import numpy as np
import pandas as pd


# ==================== 均线 ====================
class MATechnical:
    @staticmethod
    def calc_sma(closes, period):
        n = len(closes)
        if n == 0:
            return 0.0
        if n < period:
            return float(closes[-1])
        return float(np.mean(closes[-period:]))

    @staticmethod
    def calc_ema(closes, period):
        n = len(closes)
        if n == 0:
            return 0.0
        if n < period:
            return float(closes[-1])
        return float(pd.Series(closes).ewm(span=period, adjust=False).mean().iloc[-1])

    @staticmethod
    def calc_all_ma(closes, periods=None):
        if periods is None:
            periods = [5, 10, 20, 60, 120, 250]
        n = len(closes)
        result = {}
        if n == 0:
            for p in periods:
                result[f'sma_{p}'] = 0.0
                result[f'ema_{p}'] = 0.0
            return result
        if n < 2:
            for p in periods:
                result[f'sma_{p}'] = float(closes[-1])
                result[f'ema_{p}'] = float(closes[-1])
            return result
        series = pd.Series(closes, dtype=float)
        for p in periods:
            if n >= p:
                result[f'sma_{p}'] = float(series.rolling(window=p).mean().iloc[-1])
                result[f'ema_{p}'] = float(series.ewm(span=p, adjust=False).mean().iloc[-1])
            else:
                result[f'sma_{p}'] = float(closes[-1])
                result[f'ema_{p}'] = float(closes[-1])
        return result

    @staticmethod
    def ma_periods(base):
        """由基础均线周期派生 6 档判断均线（默认 base=20 → 5/10/20/60/120/250，行为零回归）。"""
        base = int(base)
        return [max(2, int(round(base / 4))), max(2, int(round(base / 2))),
                base, base * 3, base * 6, int(round(base * 12.5))]

    @staticmethod
    def judge_arrangement(closes, period=20):
        """7档均线排列判断（含过渡态）。

        period: 基础均线周期，判断所用的 6 档均线由 ma_periods(period) 派生；
                默认 20 对应 5/10/20/60/120/250，与原固定逻辑完全一致。
        """
        n = len(closes)
        current = closes[-1]
        periods = MATechnical.ma_periods(period)
        p1, p2, p3, p4, p5, p6 = periods
        if n < p1:
            return '数据不足'
        ma1 = MATechnical.calc_sma(closes, p1)
        ma2 = MATechnical.calc_sma(closes, p2) if n >= p2 else ma1
        ma3 = MATechnical.calc_sma(closes, p3) if n >= p3 else ma2
        ma4 = MATechnical.calc_sma(closes, p4) if n >= p4 else ma3
        ma5 = MATechnical.calc_sma(closes, p5) if n >= p5 else ma4
        ma6 = MATechnical.calc_sma(closes, p6) if n >= p6 else ma5
        if n >= p6 and current > ma1 > ma2 > ma3 > ma4 > ma5 > ma6:
            return '完美多头排列'
        if current > ma1 > ma2 > ma3 > ma4:
            return '多头排列'
        if ma1 > ma2 > ma3 and ma3 > 0 and abs(current - ma3) / ma3 < 0.03:
            return '多头初期·均线发散'
        _ma_min = min(ma1, ma2, ma3, ma4)
        if ma4 > 0 and _ma_min > 0 and max(ma1, ma2, ma3, ma4) / _ma_min < 1.06:
            return '均线黏合·即将变盘'
        if ma1 < ma2 < ma3 and ma3 > 0 and abs(current - ma3) / ma3 < 0.03:
            return '空头初期·均线发散'
        if current < ma1 < ma2 < ma3 < ma4:
            return '空头排列'
        if n >= p6 and current < ma1 < ma2 < ma3 < ma4 < ma5 < ma6:
            return '完美空头排列'
        return '均线交织'

    @staticmethod
    def arrangement_level(arrangement):
        """均线排列档位数值化：完美多头3/多头2/多头初期1/黏合0/交织0/空头初期-1/空头-2/完美空头-3。

        修复（2026-08-19）：此前多处用 '多头' in ma_arr 子串判断，
        把"初期/黏合"混入"排列"档（如光迅科技"多头初期"被当成"多头排列"触发标准线信号）。
        统一按档位严格判定：>=2 才为多头排列，<=-2 才为空头排列。
        """
        return {
            '完美多头排列': 3.0, '多头排列': 2.0, '多头初期·均线发散': 1.0,
            '均线黏合·即将变盘': 0.0, '均线交织': 0.0,
            '空头初期·均线发散': -1.0, '空头排列': -2.0, '完美空头排列': -3.0,
            '数据不足': 0.0,
        }.get(arrangement, 0.0)


# ==================== RSI ====================
class RSICalculator:
    @staticmethod
    def calc_wilder(closes, period=14):
        n = len(closes)
        if n < period + 1:
            return 50
        deltas = np.diff(np.array(closes, dtype=float))
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_gain = np.mean(gains[:period])
        avg_loss = np.mean(losses[:period])
        if avg_loss == 0:
            return 100.0
        for i in range(period, len(deltas)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss > 0 else 100
        return max(0.0, min(100.0, 100.0 - (100.0 / (1.0 + rs))))

    @staticmethod
    def calc_series(closes, period=14):
        n = len(closes)
        if n < period + 1:
            return [50.0] * n
        rsi_series = [50.0] * n
        deltas = np.diff(np.array(closes, dtype=float))
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)
        avg_gain = float(np.mean(gains[:period]))
        avg_loss = float(np.mean(losses[:period]))
        if avg_loss == 0:
            rsi_series[period] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi_series[period] = max(0.0, min(100.0, 100.0 - 100.0 / (1.0 + rs)))
        for i in range(period, len(deltas)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
            if avg_loss == 0:
                rsi_series[i + 1] = 100.0
            else:
                rs = avg_gain / avg_loss
                rsi_series[i + 1] = max(0.0, min(100.0, 100.0 - 100.0 / (1.0 + rs)))
        return rsi_series

    @staticmethod
    def calc_all(closes, periods=None):
        if periods is None:
            periods = [6, 14, 24]
        result = {}
        for p in periods:
            result[f'rsi_{p}'] = RSICalculator.calc_wilder(closes, p)
        return result


# ==================== MACD ====================
class MACDCalculator:
    @staticmethod
    def calc(closes, fast=12, slow=26, signal=9):
        if len(closes) < slow + signal:
            return 0, 0, 0
        series = pd.Series(closes)
        ema_fast = series.ewm(span=fast, adjust=False).mean()
        ema_slow = series.ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        macd_signal = macd_line.ewm(span=signal, adjust=False).mean()
        macd_hist = macd_line - macd_signal
        return macd_line.iloc[-1], macd_signal.iloc[-1], macd_hist.iloc[-1]

    @staticmethod
    def calc_all(closes):
        macd_mid, sig_mid, hist_mid = MACDCalculator.calc(closes, 12, 26, 9)
        macd_short, sig_short, hist_short = MACDCalculator.calc(closes, 6, 13, 5)
        macd_long, sig_long, hist_long = MACDCalculator.calc(closes, 24, 52, 18)
        return {
            'macd': macd_mid,
            'macd_signal': sig_mid,
            'macd_hist': hist_mid,
            'macd_status': '金叉' if macd_mid > sig_mid else '死叉',
            'macd_short': macd_short,
            'macd_short_signal': sig_short,
            'macd_long': macd_long,
            'macd_long_signal': sig_long
        }

    @staticmethod
    def calc_hist_series(closes):
        n = len(closes)
        warmup = 26 + 9 - 1  # slow + signal - 1 = 34，与 calc() 行为一致
        if n < warmup:
            return [0.0] * n
        series = pd.Series(closes, dtype=float)
        ema_fast = series.ewm(span=12, adjust=False).mean()
        ema_slow = series.ewm(span=26, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        macd_signal = macd_line.ewm(span=9, adjust=False).mean()
        macd_hist = macd_line - macd_signal
        hists = macd_hist.tolist()
        return [0.0 if i < warmup else hists[i] for i in range(n)]

    @staticmethod
    def calc_acceleration(closes):
        hists = MACDCalculator.calc_hist_series(closes)
        if len(hists) < 3:
            return 0, '数据不足'
        current = hists[-1]
        prev1 = hists[-2]
        prev2 = hists[-3]
        velocity = current - prev1
        accel = velocity - (prev1 - prev2)
        if current > 0 and velocity > 0:
            status = '多头加速'
        elif current > 0 and velocity < 0:
            status = '多头减速'
        elif current < 0 and velocity < 0:
            status = '空头加速'
        elif current < 0 and velocity > 0:
            status = '空头减速'
        else:
            status = '动能衰竭'
        return accel, status


# ==================== 背离检测 ====================
class DivergenceDetector:
    @staticmethod
    def _find_peaks_troughs(data, min_distance=5):
        peaks, troughs = [], []
        n = len(data)
        if n < 2 * min_distance + 1:
            return peaks, troughs
        arr = np.array(data, dtype=float)
        for i in range(min_distance, n - min_distance):
            window = arr[i - min_distance:i + min_distance + 1]
            center_val = arr[i]
            if center_val == np.max(window):
                peaks.append(i)
            if center_val == np.min(window):
                troughs.append(i)
        return peaks, troughs

    @staticmethod
    def detect_macd(closes, macd_hists):
        n = min(len(closes), len(macd_hists))
        if n < 30:
            return {'type': '无背离', 'strength': 0, 'signal': 'neutral', 'desc': ''}
        cr = closes[-60:] if n >= 60 else closes
        mr = macd_hists[-60:] if n >= 60 else macd_hists
        pp, pt = DivergenceDetector._find_peaks_troughs(cr)
        mp, mt = DivergenceDetector._find_peaks_troughs(mr)
        if len(pp) >= 2 and len(mp) >= 2:
            p1, p2 = pp[-2], pp[-1]
            mn = [(i, mr[i]) for i in mp if abs(i-p1) <= 3 or abs(i-p2) <= 3]
            if len(mn) >= 2:
                mn.sort(key=lambda x: x[0])
                m1, m2 = mn[-2][1], mn[-1][1]
                if cr[p2] > cr[p1] and m2 < m1:
                    price_term = (cr[p2] - cr[p1]) / cr[p1] * 100 if cr[p1] else 0.0
                    macd_term = (m1 - m2) / abs(m1) * 50 if m1 else 0.0
                    strength = min(90, int(price_term + macd_term))
                    return {'type': '顶背离', 'strength': strength, 'signal': 'bearish', 'desc': '价格新高但MACD动能减弱'}
        if len(pt) >= 2 and len(mt) >= 2:
            p1, p2 = pt[-2], pt[-1]
            mn = [(i, mr[i]) for i in mt if abs(i-p1) <= 3 or abs(i-p2) <= 3]
            if len(mn) >= 2:
                mn.sort(key=lambda x: x[0])
                m1, m2 = mn[-2][1], mn[-1][1]
                if cr[p2] < cr[p1] and m2 > m1:
                    price_term = (cr[p1] - cr[p2]) / cr[p1] * 100 if cr[p1] else 0.0
                    macd_term = (m2 - m1) / abs(m1) * 50 if m1 else 0.0
                    strength = min(90, int(price_term + macd_term))
                    return {'type': '底背离', 'strength': strength, 'signal': 'bullish', 'desc': '价格新低但MACD动能增强'}
        return {'type': '无背离', 'strength': 0, 'signal': 'neutral', 'desc': ''}

    @staticmethod
    def detect_rsi(closes, rsi_values):
        n = min(len(closes), len(rsi_values))
        if n < 20:
            return {'type': '无背离', 'strength': 0, 'signal': 'neutral', 'desc': ''}
        cr = closes[-30:] if n >= 30 else closes
        rr = rsi_values[-30:] if n >= 30 else rsi_values
        pp, pt = DivergenceDetector._find_peaks_troughs(cr, min_distance=3)
        rp, rt = DivergenceDetector._find_peaks_troughs(rr, min_distance=3)
        if len(pp) >= 2 and len(rp) >= 2:
            p1, p2 = pp[-2], pp[-1]
            rn = [i for i in rp if abs(i-p1) <= 2 or abs(i-p2) <= 2]
            if len(rn) >= 2:
                r1, r2 = rn[-2], rn[-1]
                if cr[p2] > cr[p1] and rr[r2] < rr[r1]:
                    return {'type': '顶背离', 'strength': 70, 'signal': 'bearish', 'desc': 'RSI顶背离'}
        if len(pt) >= 2 and len(rt) >= 2:
            p1, p2 = pt[-2], pt[-1]
            rn = [i for i in rt if abs(i-p1) <= 2 or abs(i-p2) <= 2]
            if len(rn) >= 2:
                r1, r2 = rn[-2], rn[-1]
                if cr[p2] < cr[p1] and rr[r2] > rr[r1]:
                    return {'type': '底背离', 'strength': 70, 'signal': 'bullish', 'desc': 'RSI底背离'}
        return {'type': '无背离', 'strength': 0, 'signal': 'neutral', 'desc': ''}


# ==================== OBV ====================
class OBVCalculator:
    @staticmethod
    def calc(closes, volumes):
        obv = [0.0]
        for i in range(1, len(closes)):
            if closes[i] > closes[i-1]:
                obv.append(obv[-1] + volumes[i])
            elif closes[i] < closes[i-1]:
                obv.append(obv[-1] - volumes[i])
            else:
                obv.append(obv[-1])
        return obv

    @staticmethod
    def calc_ma(obv, period=10):
        if len(obv) < period:
            return [np.mean(obv)] * len(obv)
        return pd.Series(obv).rolling(window=period).mean().tolist()

    @staticmethod
    def analyze(closes, volumes):
        if len(closes) < 20:
            return {'signal': '数据不足', 'divergence': False, 'obv_breakout': False, 'obv_trend': '数据不足', 'current_obv': 0, 'obv_ma': 0}
        obv = OBVCalculator.calc(closes, volumes)
        obv_ma = OBVCalculator.calc_ma(obv, 10)
        sc = (obv[-1]-obv[-5])/abs(obv[-5])*100 if obv[-5] != 0 else 0
        sp = (closes[-1]-closes[-5])/closes[-5]*100 if closes[-5] != 0 else 0
        obo = obv[-1] > obv_ma[-1] if obv_ma else False
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
        return {
            'signal': signal,
            'divergence': divergence,
            'obv_breakout': obo,
            'obv_trend': '上升' if obv[-1] > obv[-10] else '下降' if obv[-1] < obv[-10] else '持平',
            'current_obv': obv[-1],
            'obv_ma': obv_ma[-1] if obv_ma else 0
        }


# ==================== 布林带 ====================
class BollingerAnalyzer:
    @staticmethod
    def calc(closes, period=20, std_dev=2.0):
        if len(closes) < period:
            return {
                'upper': closes[-1],
                'middle': closes[-1],
                'lower': closes[-1],
                'bandwidth': 0,
                'bandwidth_percentile': 50,
                'squeeze_status': '数据不足',
                'pct_b': 0.5,
                'breakout_signal': '数据不足'
            }
        ma = np.mean(closes[-period:])
        std = np.std(closes[-period:])
        upper, lower, middle = ma + std_dev * std, ma - std_dev * std, ma
        bandwidth = (upper - lower) / middle * 100 if middle > 0 else 0
        # O(n) 向量化：滚动窗口均值/标准差 -> 历史带宽序列
        # （等价原按 i 循环的 O(n^2)；ddof=0 匹配原 np.std 总体标准差。
        #  注意：pd rolling(period) 在 index>=period-1 即有效，已完整覆盖
        #  原 range(period, n+1) 的每一段窗口，无需额外补窗。）
        s = pd.Series(closes)
        roll_mean = s.rolling(period).mean()
        roll_std = s.rolling(period).std(ddof=0)
        bw = (2 * std_dev * roll_std / roll_mean * 100)
        bw = bw.dropna()
        pct = float((bw < bandwidth).mean() * 100) if len(bw) > 0 else 50
        if pct < 15:
            sq = '极度收缩（即将变盘）'
        elif pct < 30:
            sq = '收缩（关注突破）'
        elif pct < 70:
            sq = '正常'
        elif pct < 85:
            sq = '扩张（趋势持续）'
        else:
            sq = '极度扩张（注意风险）'
        pct_b = (closes[-1] - lower) / (upper - lower) if upper > lower else 0.5
        if pct < 15 and pct_b > 0.6:
            breakout = '⚠️ 收缩+偏上，可能向上突破'
        elif pct < 15 and pct_b < 0.4:
            breakout = '⚠️ 收缩+偏下，可能向下突破'
        elif pct < 15:
            breakout = '⏳ 极度收缩，方向待定'
        elif pct_b > 0.95:
            breakout = '触及上轨，短期超买'
        elif pct_b < 0.05:
            breakout = '触及下轨，短期超卖'
        else:
            breakout = '正常区间'
        return {
            'upper': upper,
            'middle': middle,
            'lower': lower,
            'bandwidth': bandwidth,
            'bandwidth_percentile': pct,
            'squeeze_status': sq,
            'pct_b': pct_b,
            'price_position': '上轨附近' if pct_b > 0.8 else '下轨附近' if pct_b < 0.2 else '中轨附近',
            'breakout_signal': breakout
        }


# ==================== 乖离率 ====================
class BiasAnalyzer:
    @staticmethod
    def calc(closes, ma_periods=None):
        if ma_periods is None:
            ma_periods = [5, 10, 20, 60, 120, 250]
        biases = {}
        cp = closes[-1]
        for p in ma_periods:
            if len(closes) >= p:
                biases[f'bias_{p}'] = (cp - np.mean(closes[-p:])) / np.mean(closes[-p:]) * 100
            else:
                biases[f'bias_{p}'] = 0
        return biases

    @staticmethod
    def analyze(closes):
        biases = BiasAnalyzer.calc(closes)
        analysis = []
        b5 = biases.get('bias_5', 0)
        b20 = biases.get('bias_20', 0)
        b60 = biases.get('bias_60', 0)
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
        return {'biases': biases, 'alerts': analysis, 'direction': direction, 'max_bias': max(av) if av else 0, 'min_bias': min(av) if av else 0}


# ==================== 成交量 ====================
class VolumeAnalyzer:
    @staticmethod
    def detect_anomaly_zscore(volumes, period=20):
        if len(volumes) < period:
            return {'type': '数据不足', 'z_score': 0, 'alert': False, 'volume_ratio': 1, 'mean_volume': 0, 'reliability': '数据不足'}
        recent = volumes[-period:]
        mean = np.mean(recent)
        std = np.std(recent)
        if std == 0:
            return {'type': '正常', 'z_score': 0, 'alert': False, 'volume_ratio': 1, 'mean_volume': mean, 'reliability': '正常'}
        z = (volumes[-1] - mean) / std
        if z > 3:
            vt = '极端放量'
            alert = True
        elif z > 2:
            vt = '显著放量'
            alert = True
        elif z > 1:
            vt = '温和放量'
            alert = False
        elif z < -2:
            vt = '极端缩量'
            alert = True
        elif z < -1:
            vt = '显著缩量'
            alert = False
        else:
            vt = '正常'
            alert = False
        reliability = '数据不足'
        if len(volumes) >= period * 3:
            # 原版 O(n) 逐棒检测（非 O(n^2)；此前误判为 O(n^2) 的向量化
            # 引入了 off-by-one，已回退。reliability 仅展示字段，逐位一致优先。
            signals = []
            hist = volumes[:-1]
            for i in range(period, len(hist)):
                r = hist[i-period:i]
                m = np.mean(r)
                s = np.std(r)
                if s > 0 and (hist[i] - m) / s > 2:
                    signals.append(1 if i + 1 < len(volumes) and volumes[i+1] > volumes[i] else 0)
            if signals:
                wr = sum(signals) / len(signals) * 100
                if wr > 60:
                    reliability = f'放量后上涨概率{wr:.0f}%（可靠）'
                elif wr > 40:
                    reliability = f'放量后上涨概率{wr:.0f}%（中性）'
                else:
                    reliability = f'放量后上涨概率{wr:.0f}%（反向）'
        return {'type': vt, 'z_score': z, 'alert': alert, 'volume_ratio': volumes[-1] / mean if mean > 0 else 1, 'mean_volume': mean, 'reliability': reliability}


# ==================== ATR ====================
class ATRCalculator:
    @staticmethod
    def calc_wilder(data_list, period=14):
        if len(data_list) < period + 1:
            return None
        tr_list = []
        for i in range(1, min(period + 1, len(data_list))):
            h = data_list[i]['high']
            l = data_list[i]['low']
            pc = data_list[i-1]['close']
            tr_list.append(max(h - l, abs(h - pc), abs(l - pc)))
        if not tr_list:
            return None
        atr = np.mean(tr_list)
        for i in range(period + 1, len(data_list)):
            h = data_list[i]['high']
            l = data_list[i]['low']
            pc = data_list[i-1]['close']
            atr = (atr * (period - 1) + max(h - l, abs(h - pc), abs(l - pc))) / period
        return atr


# ==================== ADX（新增） ====================
class ADXCalculator:
    @staticmethod
    def calc_adx(highs, lows, closes, period=14):
        n = len(closes)
        if n < period + 1:
            return {'adx': 25, 'state': '数据不足', 'note': '数据不足，使用默认权重', 'di_plus': 0, 'di_minus': 0, 'spread': 0}

        # 构造 TR / +DM / -DM 序列（首根置 0）
        tr_arr = np.zeros(n)
        p_arr = np.zeros(n)
        m_arr = np.zeros(n)
        for i in range(1, n):
            hl = highs[i] - lows[i]
            hc = abs(highs[i] - closes[i-1])
            lc = abs(lows[i] - closes[i-1])
            tr_arr[i] = max(hl, hc, lc)
            up = highs[i] - highs[i-1]
            down = lows[i-1] - lows[i]
            if up > down and up > 0:
                p_arr[i] = up
            if down > up and down > 0:
                m_arr[i] = down

        if n < period + 1:
            return {'adx': 25, 'state': '数据不足', 'note': '数据不足，使用默认权重', 'di_plus': 0, 'di_minus': 0, 'spread': 0}

        # Wilder 平滑：种子 = 前 period 个值简单均值，之后递推
        tr_s = np.full(n, np.nan)
        p_s = np.full(n, np.nan)
        m_s = np.full(n, np.nan)
        tr_s[period] = tr_arr[1:period + 1].mean()
        p_s[period] = p_arr[1:period + 1].mean()
        m_s[period] = m_arr[1:period + 1].mean()
        for i in range(period + 1, n):
            tr_s[i] = (tr_s[i - 1] * (period - 1) + tr_arr[i]) / period
            p_s[i] = (p_s[i - 1] * (period - 1) + p_arr[i]) / period
            m_s[i] = (m_s[i - 1] * (period - 1) + m_arr[i]) / period

        atr = float(tr_s[-1])
        if not np.isfinite(atr) or atr == 0:
            return {'adx': 25, 'state': '数据不足', 'note': 'ATR为零，使用默认权重', 'di_plus': 0, 'di_minus': 0, 'spread': 0}

        # +DI / -DI / DX 序列
        di_p = p_s / tr_s * 100
        di_m = m_s / tr_s * 100
        di_plus = float(di_p[-1])
        di_minus = float(di_m[-1])
        dx_denom = di_p + di_m
        dx = np.zeros(n)
        valid = dx_denom > 0
        dx[valid] = np.abs(di_p[valid] - di_m[valid]) / dx_denom[valid] * 100

        # ADX = DX 的 Wilder 平滑（种子 = 首个有效 DX）
        adx = np.full(n, np.nan)
        adx[period] = dx[period]
        for i in range(period + 1, n):
            adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period
        adx = float(adx[-1]) if np.isfinite(adx[-1]) else 25.0

        if n >= 60:
            ma5 = MATechnical.calc_sma(closes, 5)
            ma20 = MATechnical.calc_sma(closes, 20)
            ma60 = MATechnical.calc_sma(closes, 60)
            spread = max(ma5, ma20, ma60) / min(ma5, ma20, ma60) - 1 if min(ma5, ma20, ma60) > 0 else 0
        else:
            spread = 0

        if adx > 30:
            state = '强趋势市'
            note = f'ADX={adx:.0f}，趋势强劲'
        elif adx > 25:
            state = '趋势市'
            note = f'ADX={adx:.0f}，趋势明确'
        elif adx > 20:
            state = '弱趋势'
            note = f'ADX={adx:.0f}，趋势初现'
        else:
            state = '震荡市'
            note = f'ADX={adx:.0f}，方向不明'

        atr_ratio = atr / closes[-1] if closes[-1] > 0 else 0.02
        if atr_ratio > 0.04:
            state += '（高波动）'
            note += '，波动率高'
        elif atr_ratio < 0.015:
            state += '（低波动）'
            note += '，波动率低'

        return {
            'adx': round(adx, 1),
            'state': state,
            'note': note,
            'di_plus': round(di_plus, 1),
            'di_minus': round(di_minus, 1),
            'spread': round(spread, 4),
            'atr_ratio': round(atr_ratio, 4)
        }


# ==================== K线形态 ====================
class CandlestickPatterns:
    @staticmethod
    def detect(opens, highs, lows, closes):
        if len(closes) < 3:
            return []
        patterns = []
        o, h, l, c = opens[-3:], highs[-3:], lows[-3:], closes[-3:]
        body = abs(c[-1] - o[-1])
        shadow = h[-1] - max(c[-1], o[-1])
        lower_shadow = min(c[-1], o[-1]) - l[-1]
        if body > 0 and lower_shadow > body * 2 and shadow < body * 0.3:
            patterns.append('🔨锤子线（看涨）')
        if body > 0 and shadow > body * 2 and lower_shadow < body * 0.3:
            patterns.append('🔻倒锤子（看跌）')
        if body < h[-1] - l[-1] and body < (h[-1] - l[-1]) * 0.1:
            patterns.append('➕十字星（变盘信号）')
        if c[-1] > o[-1] and o[-2] > c[-2] and c[-1] > o[-2] and o[-1] < c[-2]:
            patterns.append('📈看涨吞没')
        if c[-1] < o[-1] and c[-2] > o[-2] and c[-1] < o[-2] and o[-1] > c[-2]:
            patterns.append('📉看跌吞没')
        if all(c[i] > o[i] for i in range(3)) and c[-1] > c[-2] > c[-3]:
            patterns.append('🔥三连阳（强势）')
        if all(c[i] < o[i] for i in range(3)) and c[-1] < c[-2] < c[-3]:
            patterns.append('❄️三连阴（弱势）')
        return patterns


# ==================== 波动率锥 ====================
class VolatilityCone:
    @staticmethod
    def calc(closes, periods=[5, 10, 20, 60], lookback=None):
        if lookback is not None:
            _lb = int(round(lookback))
            if _lb >= 2 and _lb not in periods:
                periods = list(periods) + [_lb]
        if len(closes) < max(periods) + 1:
            return {}
        # 守卫：避免 np.log(价格≤0) 产生 -inf/NaN（停牌/未上市数据价格可能为 0 或负值异常）
        _arr = np.asarray(closes, dtype=float)
        if (_arr <= 0).any():
            # 将非正价格替换为历史首个正价格或 1e-8，保证 log 定义域合法
            first_pos = float(_arr[_arr > 0][0]) if (_arr > 0).any() else 1e-8
            _arr = np.where(_arr > 0, _arr, max(first_pos, 1e-8))
        cone = {}
        for p in periods:
            if len(_arr) >= p + 1:
                slice_ = _arr[-p-1:] if len(_arr) >= p + 2 else _arr
                rets = np.diff(np.log(slice_))
                if len(rets) >= p:
                    vols = [float(np.std(rets[i:i+p])) * np.sqrt(252) for i in range(len(rets) - p + 1)]
                else:
                    vols = [float(np.std(rets)) * np.sqrt(252)]
                cone[f'vol_{p}d'] = {
                    'current': vols[-1],
                    'min': float(np.min(vols)),
                    'max': float(np.max(vols)),
                    'median': float(np.median(vols)),
                    'percentile': sum(1 for v in vols if v < vols[-1]) / len(vols) * 100
                }
        return cone


# ==================== KDJ ====================
class KDJCalculator:
    @staticmethod
    def calc(highs, lows, closes, period=9):
        n = len(closes)
        if n < period + 1:
            return 50.0, 50.0, 50.0, '数据不足'
        lowest_low = min(lows[-period:])
        highest_high = max(highs[-period:])
        if highest_high == lowest_low:
            return 50.0, 50.0, 50.0, '数据不足'
        rsv = (closes[-1] - lowest_low) / (highest_high - lowest_low) * 100
        k_values = [50.0] * n
        d_values = [50.0] * n
        for i in range(period, n):
            ll = min(lows[i-period:i])
            hh = max(highs[i-period:i])
            if hh == ll:
                rsv_i = 50.0
            else:
                rsv_i = (closes[i] - ll) / (hh - ll) * 100
            k_values[i] = (2 * k_values[i-1] + rsv_i) / 3
            d_values[i] = (2 * d_values[i-1] + k_values[i]) / 3
        k = k_values[-1]
        d = d_values[-1]
        j = 3 * k - 2 * d
        signal = '金叉' if k > d else '死叉'
        return round(k, 2), round(d, 2), round(j, 2), signal

    @staticmethod
    def calc_all(highs, lows, closes, period=9):
        k, d, j, signal = KDJCalculator.calc(highs, lows, closes, period)
        k_14, d_14, j_14, signal_14 = KDJCalculator.calc(highs, lows, closes, 14)
        return {
            'kdj_k': k,
            'kdj_d': d,
            'kdj_j': j,
            'kdj_signal': signal,
            'kdj_k_14': k_14,
            'kdj_d_14': d_14,
            'kdj_j_14': j_14,
            'kdj_signal_14': signal_14
        }


# ==================== 威廉指标 WR ====================
class WilliamRCalculator:
    """威廉指标（Williams %R）· 超买超卖动量。

    口径：WR = (highest_high − close) / (highest_high − lowest_low) × −100
    取值 0 ~ −100；−80 及以下超卖（有反弹动能），−20 及以上超买（有回落风险）。
    与 KDJ 的 RSV 同源（WR = −(100 − RSV)），手感互补：WR 对拐点更灵敏。
    """

    @staticmethod
    def calc(highs, lows, closes, period=14):
        n = len(closes)
        if n < period + 1:
            return {'wr': -50.0, 'state': '数据不足'}
        highest_high = max(highs[-period:])
        lowest_low = min(lows[-period:])
        if highest_high == lowest_low:
            return {'wr': -50.0, 'state': '数据不足'}
        wr = (highest_high - closes[-1]) / (highest_high - lowest_low) * -100
        if wr <= -80:
            state = '超卖'
        elif wr >= -20:
            state = '超买'
        else:
            state = '中性'
        return {'wr': round(float(wr), 2), 'state': state}


# ==================== 回撤 ====================
class DrawdownCalculator:
    @staticmethod
    def calc(closes, lookback=None):
        arr = closes
        if lookback is not None:
            _lb = int(round(lookback))
            if _lb >= 5 and len(arr) > _lb:
                arr = arr[-_lb:]
        if len(arr) < 5:
            return {'current_dd': 0, 'max_dd': 0, 'dd_duration': 0, 'dd_severity': '数据不足', 'recovery_estimate': '数据不足'}
        highs = np.maximum.accumulate(arr)
        drawdowns = (highs - np.array(arr)) / highs * 100
        cd = drawdowns[-1]
        md = np.max(drawdowns)
        mi = np.argmax(drawdowns)
        dd = len(drawdowns) - mi
        if cd > 15:
            severity = '严重'
        elif cd > 8:
            severity = '中等'
        else:
            severity = '轻微'
        if dd > 60:
            recovery = '回撤已持续60+天，历史恢复概率低'
        elif dd > 30:
            recovery = '回撤已持续30+天，关注反转信号'
        elif dd > 10:
            recovery = '回撤持续中，等待企稳'
        else:
            recovery = '近期回撤，观察是否止跌'
        return {'current_dd': cd, 'max_dd': md, 'dd_duration': dd, 'dd_severity': severity, 'recovery_estimate': recovery}
