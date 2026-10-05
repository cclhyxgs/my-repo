#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""因子预期分计算器 v5.0 - 因子注册表驱动 + IC加权 + 连续值

核心设计：
1. 因子不再固定，通过 factor_registry 动态读取用户启用的因子
2. z-score 标准化 + IC 加权（数据驱动）
3. 冲突惩罚机制（量价冲突/超跌反弹信号）
4. 市场环境调整已移至 UnifiedScorer（final_score 阶段统一处理，避免重复计入）

v2 优化（99股验证）：IC=0.038, Spread=2.05%, OOS IC=0.080
"""

import logging
import math
from collections import Counter
from engine.indicators import DrawdownCalculator
from engine import quant_config
from engine import factor_registry

logger = logging.getLogger(__name__)

# 无方向强度/波动/风险因子：空单视角下**不取反**（趋势强度/波动水平对多空
# 都是机会或风险，方向中性）。方向化时（direction='short'）仅对白名单之外的
# 方向性因子取反。见 calc_stock_score / compute_futures_score（backtest_futures）。
NON_DIRECTIONAL_FACTORS = frozenset({
    'adx_trend_strength',   # ADX 趋势强度：趋势越强（无论涨跌）信号越可靠
    'bb_bandwidth',         # 布林带宽：波动率水平，对多空同为机会
    'atr_norm',             # ATR 归一化波动率：同为机会
    'volatility_cone',      # 波动率锥分位：高波动=高风险的警示，对多空同为风险
})


class ScoreCalculatorV2:
    """
    因子预期分计算器 v5.0（因子注册表驱动）

    从 21 个候选因子中动态读取用户启用的因子，z-score标准化 + IC加权。
    用户可通过 config/quant_model.json 自定义因子组合、权重、方向、参数、统计参数。
    文件不存在时使用 v2 优化默认配置（5因子IC加权模型）。

    方向化（2026-08-13）：direction='short' 时按空单视角计算——
    方向性因子贡献取反（看空排列=空单加分），无方向强度因子不取反，
    冲突惩罚整体取反（看跌信号=空单加分）。多单（'long'，默认）原逻辑零影响。
    """

    @classmethod
    def _get_factor_weights(cls):
        return quant_config.get_factor_weights()

    @classmethod
    def _get_factor_direction(cls):
        return quant_config.get_factor_direction()

    @classmethod
    def _get_factor_stats(cls, period=None):
        return quant_config.get_factor_stats(period)

    @staticmethod
    def extract_factors(tech, market, closes, highs, lows, volumes, opens, data_list, latest_price, period='日K'):
        """
        提取所有启用因子的连续值（动态读取因子注册表）

        period: 分析周期（'日K'/ '15分钟' 等）。非日线时按周期缩放因子窗口参数，
        使『20日相对强度』等在任何周期都表示约等长自然时间。

        Returns:
            dict: factor_name -> float (连续值)
        """
        active_factors = quant_config.get_active_factors()
        base_configs = quant_config.get_factor_configs() or {}
        factor_configs = quant_config.get_period_scaled_factor_configs(base_configs, period)
        ctx = {
            'closes': closes, 'highs': highs, 'lows': lows, 'volumes': volumes,
            'opens': opens, 'data_list': data_list, 'latest_price': latest_price,
            'tech': tech, 'market': market,
        }
        return factor_registry.calc_all_active_factors(active_factors, ctx, factor_configs)

    @staticmethod
    def standardize_factor(name, value, period='日K'):
        """用历史统计参数做z-score标准化

        当 std 缺失或为极小值（如某些因子标定退化 std≈1e-18）时返回 0，
        避免除零或产生极端 z 值（修复：macd_hist_norm 等退化因子不再失真）。
        新增 NaN/inf 守卫：脏数据（data_layer 把非法价格 coerce 成 NaN 注入 closes）
        直接归 0，避免 (NaN-mean)/std = NaN 在下游 clamp 被 Python min/max 当成
        +z_max 极端正向（min(3.0, nan)=3.0），静默抬高因子预期分——与既往
        period=14.0 同类：算错但无任何报错，结论天差地别。

        period: 周期分桶校准键；分钟周期回落日线校准（报告标注指示性）。
        """
        # NaN/inf 守卫（结构性缺口修复）：返回 None/NaN/inf 的因子值归 0，
        # 不污染加权分，也不触发下游 clamp 的极端正向误判。
        if value is None or not isinstance(value, (int, float)):
            return 0.0
        if math.isnan(value) or math.isinf(value):
            return 0.0
        stats = ScoreCalculatorV2._get_factor_stats(period).get(name, {'mean': 0, 'std': 1})
        if not isinstance(stats, dict) or stats.get('std', 0) <= 1e-9:
            return 0.0
        return (value - stats['mean']) / stats['std']

    @staticmethod
    def calc_stock_score(tech, market, latest_price, adx_result=None,
                         closes=None, highs=None, lows=None, volumes=None, opens=None, data_list=None,
                         direction='long', period='日K'):
        """
        计算因子预期分（Multi-Factor Expected Score，IC 0.039 校准）

        Args:
            tech: 技术指标字典（与v4兼容）
            market: 市场环境字典
            latest_price: 最新价格
            adx_result: ADX结果
            closes, highs, lows, volumes, opens, data_list: 原始K线数据（用于提取连续因子值）
            direction: 计算视角，'long'=多头（默认，A股恒为此值），'short'=空头（期货）。
                空单视角：方向性因子贡献取反、无方向强度因子（NON_DIRECTIONAL_FACTORS）不取反、
                冲突惩罚取反。
            period: 分析周期（'日K'/'15分钟' 等）。非日线时因子窗口按周期缩放、
                校准分桶回落日线（报告标注指示性）。

        Returns:
            score: -100 ~ 100（按 direction 视角）
            detail: 因子预期明细列表
            weight_log: 权重日志
            conflicts: 冲突列表
            adx_result: ADX结果
            tech_strength: 技术共振强度（绝对值能量占比，不受方向影响）
            significant_factors: 显著因子列表（贡献符号随视角方向化）
        """
        # —— 配置自检（局部契约，无中央门禁）——
        # 仅声明本函数所需核心配置；缺失时抛 ConfigIncompleteError，
        # 由各功能运行入口捕获并提示用户「XX 需要配置」。改本函数需求只动此处。
        quant_config.require_config(quant_config._safe_cfg(), [
            ('因子配置(factor_configs)', 'factor_configs'),
            ('评分缩放(score_scale)', 'score_scale'),
            ('状态分界(thresholds)', 'thresholds'),
        ])

        # 如果没有传入原始数据，无法计算因子值
        if closes is None:
            logger.warning("calc_stock_score 未传入K线数据(closes=None)，返回0分")
            return 0, ['无K线数据，因子预期不可用'], ['fallback: no kline data'], [], adx_result or {'state': '数据不足', 'adx': 25}, 0.0, []

        # 1. 提取连续因子值（按周期缩放因子窗口）
        factors = ScoreCalculatorV2.extract_factors(
            tech, market, closes, highs, lows, volumes, opens, data_list, latest_price, period
        )

        # 空单视角：方向性因子贡献取反；无方向强度因子（波动/趋势强度）保持
        is_short = (direction == 'short')

        # 2. 标准化 + IC加权
        # 正确的因子方向语义（2026-08-27）：
        #   f_direction = +1 (看涨): 因子值越高越好，z_score = (value - mean) / std
        #   f_direction = -1 (看跌): 因子值越低越好，z_score = (mean - value) / std
        #   例：RSI 设为 -1(看跌) 时，RSI=10 超卖 → (50-10)/15 = +2.67 正分 ✅
        #        RSI=80 超买 → (50-80)/15 = -2.00 负分 ✅
        #   用户在 UI 看到的是因子对当前交易的净贡献：
        #   - 多头视角：正分=加分项（因子信号支持做多），负分=减分项（因子信号反对做多）
        #   - 空头视角：反之
        score = 0.0
        detail = []
        abs_contribs = []      # 各方案因子贡献的绝对值（用于技术共振强度）
        factor_contribs = []   # (因子名, 贡献值) 列表

        factor_weights = ScoreCalculatorV2._get_factor_weights()
        factor_direction = ScoreCalculatorV2._get_factor_direction()
        score_scale = quant_config.get_score_scale()

        for factor_name, raw_value in factors.items():
            # 脏因子值（None/NaN/inf）归 0：隔离 standardize_factor 之外的潜在膨胀入口，
            # 同时避免下方 f-string 格式化崩溃。
            if raw_value is None or not isinstance(raw_value, (int, float)) \
                    or math.isnan(raw_value) or math.isinf(raw_value):
                raw_value = 0.0
            weight = factor_weights.get(factor_name) or 0.0
            f_direction = factor_direction.get(factor_name) or 1

            # z-score标准化（按周期分桶校准）
            z_score = ScoreCalculatorV2.standardize_factor(factor_name, raw_value, period)

            # ── 方向调整：正确实现 f_direction 语义 ──
            # f_direction = +1: z_score = (value - mean) / std → 高值正分（好信号）
            # f_direction = -1: z_score = (mean - value) / std → 低值正分（好信号）
            # 注意：standardize_factor 返回 (value - mean) / std，所以 f_direction=-1 时需要取反
            # ⚠️ 无方向因子（NON_DIRECTIONAL_FACTORS）跳过此调整，保持原始 z-score
            is_directional = factor_name not in NON_DIRECTIONAL_FACTORS
            if is_directional and f_direction == -1:
                z_score = -z_score  # 反转方向：低值→正分

            # 截断（避免极端值）
            z_lo = min(score_scale['z_truncate_min'], score_scale['z_truncate_max'])
            z_hi = max(score_scale['z_truncate_min'], score_scale['z_truncate_max'])
            if score_scale['z_truncate_min'] > score_scale['z_truncate_max']:
                logger.warning("z_truncate_min > z_truncate_max，已按区间自动钳制（配置疑似填反）")
            z_score = max(z_lo, min(z_hi, z_score))

            # 空单视角：无方向强度因子不取反；方向性因子取反（看多空排列对空单的影响）
            if is_short and factor_name not in NON_DIRECTIONAL_FACTORS:
                z_score = -z_score

            # 加权贡献
            contribution = z_score * weight * score_scale['weight_multiplier']
            score += contribution

            _sign = '+' if contribution >= 0 else ''
            if is_directional:
                _dir_label = '看跌' if f_direction == -1 else '看涨'
            else:
                _dir_label = '无方向/强度'
            detail.append(f'{factor_name}: {raw_value:.4f} -> z={z_score:.2f} x w={weight:.3f} = {contribution:{_sign}.1f} [方向={_dir_label}]')
            abs_contribs.append(abs(contribution))
            factor_contribs.append((factor_name, contribution))

        # 3. 技术共振强度（纯技术面强度；与因子预期分同源，均出自方案因子）
        #    因子预期分本身是"按回测有效性加权的技术面评分"——与历史回测收益相关、
        #    但不预测未来收益；二者仅聚合维度不同：因子预期分=加权净值，本强度=显著能量占比。
        #    显著因子 = |单因子贡献| ≥ 用户配置阈值；
        #    强度 = 显著因子的能量 / 全部因子的能量（加分减分都计入，不看方向）。
        _trc = quant_config.get_tech_resonance_config()
        tech_strength = 0.0
        significant_factors = []
        if _trc is not None:
            _threshold = _trc.get('threshold')
            if _threshold is not None and abs_contribs:
                total_abs = sum(abs_contribs)
                if total_abs > 0:
                    sig_abs = sum(c for c in abs_contribs if c >= _threshold)
                    tech_strength = sig_abs / total_abs
                    significant_factors = [(n, c) for (n, c) in factor_contribs if abs(c) >= _threshold]

        # 4. 冲突惩罚（信号分歧 / 量价背离 → 真实影响因子预期分）
        #    penalty 为带符号的净调整：负值=惩罚(拉低)，正值=反转加成(拉高)。
        #    由 conflict_penalty 配置驱动，直接计入 stock_score，级联影响：
        #    final_score(经 UnifiedScorer 乘环境) → 星级 → 入场档位 → 目标仓位。
        #    冲突文字标签仍经 conflicts 返回，供 ctx.score_conflicts 在报告展示。
        penalty, conflicts = ScoreCalculatorV2._calc_conflict_penalty_v2(
            tech, market, latest_price, closes, direction=direction,
            highs=highs, lows=lows
        )
        if penalty:
            score += penalty
            detail.append(f'冲突调整: {penalty:+.1f} ({", ".join(conflicts)})')
        else:
            detail.append('冲突调整: 0 (无显著冲突信号)')

        # 4. 市场环境不再在此计入（修复：此前与 UnifiedScorer 重复计入 up_ratio）
        #    因子预期分(stock_score)保持为纯个股因子信号；
        #    市场环境调整统一由 UnifiedScorer.calculate_final_score 在 final_score 阶段处理。

        # 5. 截断
        # 边界自检：score_min/max 由用户配置，可能被手滑填反；
        # 统一按区间 [lo, hi] 钳制，杜绝「填反→所有股票得分恒=100(最强买入)」灾难。
        s_lo = min(int(score_scale['score_min']), int(score_scale['score_max']))
        s_hi = max(int(score_scale['score_min']), int(score_scale['score_max']))
        if int(score_scale['score_min']) > int(score_scale['score_max']):
            logger.warning("score_min > score_max，已按区间自动钳制（配置疑似填反）")
        score = max(s_lo, min(s_hi, int(round(score))))

        weight_log = [f'v5.0 IC加权: {len(factors)}个有效因子, 权重和={sum(factor_weights.values()):.3f}']

        return score, detail, weight_log, conflicts, adx_result or {'state': '数据不足', 'adx': 25}, tech_strength, significant_factors

    @staticmethod
    def _calc_conflict_penalty_v2(tech, market, latest_price, closes, direction='long',
                                  highs=None, lows=None):
        """
        冲突惩罚 v2 - 精简版

        只保留IC分析验证有效的冲突模式：
        - MACD底背离 + 超跌 -> 看涨（原系统验证有效）
        - 放量下跌 -> 看跌（原系统验证有效）
        - 缩量上涨 -> 看跌（原系统验证有效）
        - 密集成交区(均衡带)接近 -> 追高/杀跌弱浮动惩罚

        direction='short' 时整体取反：penalty 反向（看跌信号=空单加分），
        文案改述为空单视角（对空单顺风/减分）。
        """
        penalty = 0
        conflicts = []

        n = len(closes)
        if n < 20:
            return 0, []

        cp = quant_config.get_conflict_penalty()
        # 冲突惩罚为可选子系统：未配置（None/空）即不施加，不计入因子预期分
        if not cp:
            return 0, []
        # 清洗：仅保留有值的键，None 视为 0（不施加该项）
        cp = {k: float(v) for k, v in cp.items() if v is not None}

        is_short = (direction == 'short')

        # 量价冲突（原系统验证有效）
        volume_price = market.get('volume_price', '')
        if '放量下跌' in volume_price:
            penalty += cp.get('volume_down_penalty', 0)
            conflicts.append('放量下跌(抛压确认，空单顺风)' if is_short else '放量下跌(抛压确认)')
        if '缩量上涨' in volume_price:
            penalty += cp.get('volume_up_shrink_penalty', 0)
            conflicts.append('缩量上涨(上涨乏力，空单顺风)' if is_short else '缩量上涨(缺乏持续性)')

        # 超跌反弹信号（IC分析中current_drawdown因子有效）
        dd_data = DrawdownCalculator.calc(closes)
        current_dd = dd_data.get('current_dd', 0)
        macd_div = tech.get('macd_divergence', {})
        if current_dd > cp.get('deep_drawdown_threshold', 0) and macd_div.get('signal') == 'bullish':
            penalty += cp.get('deep_drawdown_div_bonus', 0)
            conflicts.append('深度回撤+MACD底背离(反弹风险，空单减分)' if is_short else '深度回撤+MACD底背离(反转信号)')

        if current_dd > cp.get('extreme_drawdown_threshold', 0):
            penalty += cp.get('extreme_drawdown_bonus', 0)
            conflicts.append(f'极端回撤{current_dd:.0f}%(反弹风险，空单减分)' if is_short
                             else f'极端回撤{current_dd:.0f}%(反弹概率高)')

        # 空单视角：冲突惩罚整体取反（看跌信号=空单加分，看涨信号=空单减分）
        if is_short:
            penalty = -penalty

        # 密集成交区(均衡带)：方向专属弱浮动扣分，须在「空单整体取反」之后处理，
        # 否则 direction 专属的扣分会被二次翻转(负负得正)变成立项加分。
        # 定位：多空均衡带，价格在密带上方=追高风险(多头扣分)，
        #       下方=杀跌风险(空单扣分)，密带内部=多空均衡不干预。
        # 口径：「Top20 边界连续带」+「0.5%相对距离阈值」。
        cpz_pct = float(cp.get('cluster_proximity_pct') or 0)
        cpz_pen = float(cp.get('cluster_proximity_penalty') or 0)
        if cpz_pct > 0 and cpz_pen != 0 and highs and lows and len(highs) >= 20:
            _lvl = []
            for _h, _lo in zip(highs[-60:], lows[-60:]):
                _lvl.append(round(_h, 2))
                _lvl.append(round(_lo, 2))
            _cnt = Counter(_lvl).most_common(20)
            if _cnt:
                _band_lo = min(x for x, _ in _cnt)
                _band_hi = max(x for x, _ in _cnt)
                _near = cpz_pct / 100.0 * latest_price  # 0.5%相对距离
                if latest_price > _band_hi + _near:
                    if not is_short:   # 密带上方 → 追高风险(多头扣)
                        penalty += cpz_pen
                        conflicts.append(f'接近密集成交区上方(追高风险，距{_band_hi:.2f})')
                elif latest_price < _band_lo - _near:
                    if is_short:       # 密带下方 → 杀跌风险(空单扣)
                        penalty += cpz_pen
                        conflicts.append(f'接近密集成交区下方(杀跌风险，距{_band_lo:.2f})')
                # 密带内部(均衡) → 不干预

        return max(int(cp.get('penalty_min', penalty)), min(int(cp.get('penalty_max', penalty)), penalty)), conflicts
