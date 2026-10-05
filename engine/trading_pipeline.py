#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""统一调度流水线 - 编排所有模块"""

from engine.trading_context import TradingContext
from engine.score_calculator_v2 import ScoreCalculatorV2
from engine.unified_scorer import UnifiedScorer
from engine.unified_entry_logic import UnifiedEntryLogic
from engine.market_gate import MarketGate
from engine.stock_classifier import StockClassifier
from engine.add_engine import AddEngine
from engine.reduce_engine import ReduceEngine
from engine.ghost_engine import GhostEngine
from engine.signal_rating import SignalRating
from engine.indicators import ADXCalculator, ATRCalculator
from engine.indicators_advanced import calc_chip_concentration, calc_fibonacci_retracement, calc_pivot_points
from engine.quant_config import get_risk_params
from engine import quant_config
from engine.report_builder import compute_decision


class TradingPipeline:
    """
    交易流水线 - 统一执行所有模块

    执行顺序：
    1. 计算ADX
    2. 计算个股因子预期分
    3. 获取环境配置
    4. 计算最终得分
    5. 入场判断
    6. 应用仓位门控
    7. 股票分类
    8. 信号有效性评级
    9. 计算关键价位
    10. 计算多空信号
    11. 加减仓建议（如有持仓）
    12. 幽灵验证（如有持仓）
    13. 生成条件预案
    """

    @classmethod
    def execute(cls, stock_code, data_list, up_ratio, tech, market,
                has_position=False, entry_price=None, bars_held=0,
                account_equity=None, current_weight=None, breadth_fetched=False,
                direction='long', market_type='stock', period='日K', sentiment=None):
        """执行完整分析流水线

        Args:
            account_equity: 当前账户净值（用于动态模型的「账户回撤」维度；
                            桌面单股调用默认 None -> 回撤维度中性）。
            current_weight: 当前实际持仓权重（占净值）；有持仓时传入以保证连续性。
            direction: 持仓方向，'long'=多头（默认，A 股恒为此值），'short'=空头（期货）。
            market_type: 市场类型，'stock'=A股 / 'futures'=期货。供报告/门禁标签标识，
                不影响因子翻转逻辑（方向化已由 direction + scheme 配置共同决定）。
            period: 分析周期（'日K'/'15分钟' 等），透传到因子窗口缩放与校准分桶。
        """
        # 授权硬门禁（引擎层）：即便 UI 的 `if not allowed` 被 patch，引擎也拒绝计算。
        # enforce 内部做 seal 自校验（PyInstaller 冻结态），patch 其字节码同样触发锁死。
        try:
            from license.license_guard import enforce as _enforce_license, LicenseDenied
            _enforce_license('query')
        except LicenseDenied:
            raise
        except Exception:
            # 非授权类异常（如模块缺失）不在此阻断，交由上层 UI 兜底
            pass

        # —— 配置自检（局部契约，无中央门禁）——
        # 建仓参数(entry_params) 由本流水线在 check_entry 阶段直接索引，缺失即崩；
        # 声明在此，由各功能运行入口捕获并提示用户补全。评分相关 4 项已由 calc_stock_score 声明。
        quant_config.require_config(quant_config._safe_cfg(), [
            ('建仓参数(entry_params)', 'entry_params'),
        ])

        # 1. 创建上下文
        if not data_list:
            raise ValueError('data_list 不能为空')
        ctx = TradingContext(stock_code, up_ratio, tech, market, data_list)
        ctx.breadth_fetched = breadth_fetched
        ctx.has_position = has_position
        ctx.entry_price = entry_price if entry_price is not None else 0
        ctx.bars_held = bars_held or 0
        ctx.market_type = market_type
        # 分析周期：驱动因子窗口缩放/校准分桶（日线原样；分钟按自然时间等价）
        ctx.period = period or '日K'
        # 持仓方向：仅期货支持 'short'，其余一律归一为 'long'（A 股路径零影响）
        ctx.direction = direction if direction == 'short' else 'long'

        # 2. 计算ADX
        highs = [d.get('high', 0) for d in data_list]
        lows = [d.get('low', 0) for d in data_list]
        closes = [d.get('close', 0) for d in data_list]
        volumes = [d.get('volume', 0) for d in data_list]
        opens = [d.get('open', d.get('close', 0)) for d in data_list]
        adx_result = ADXCalculator.calc_adx(highs, lows, closes)
        ctx.adx_state = adx_result

        # 3. 计算因子预期分（V5: IC加权 + 连续因子值）
        #    direction 方向化：空单视角下方向性因子贡献取反、强度因子不取反、冲突惩罚取反，
        #    多单（'long'）原逻辑零影响。
        stock_score, score_detail, weight_log, conflicts, adx_result, tech_strength, significant_factors = ScoreCalculatorV2.calc_stock_score(
            tech, market, ctx.latest_price, adx_result,
            closes=closes, highs=highs, lows=lows, volumes=volumes, opens=opens, data_list=data_list,
            direction=ctx.direction, period=ctx.period,
        )
        ctx.stock_score = stock_score
        ctx.score_detail = score_detail
        ctx.score_conflicts = conflicts
        # 空单视角对照：保留多单视角分（报告双视角展示用），多单时二者相等
        ctx.stock_score_long = stock_score
        if ctx.direction == 'short':
            score_long, _, _, _, _, _, _ = ScoreCalculatorV2.calc_stock_score(
                tech, market, ctx.latest_price, adx_result,
                closes=closes, highs=highs, lows=lows, volumes=volumes, opens=opens, data_list=data_list,
                direction='long', period=ctx.period,
            )
            ctx.stock_score_long = score_long
            ctx.final_score_long = UnifiedScorer.calculate_final_score(score_long, up_ratio, ctx.market_type).get('final_score', score_long)

        # 4. 获取环境配置（期货走中性门控：不套 A 股全市场上涨家数系数）
        env_config = MarketGate.get_environment(up_ratio, ctx.market_type, sentiment=sentiment)
        ctx.env_config = env_config
        ctx.limit = env_config.get('limit', 1.0)
        ctx.confidence_factor = env_config.get('factor', 1.0)

        # 5. 计算最终得分（期货走中性门控）
        unified_result = UnifiedScorer.calculate_final_score(stock_score, up_ratio, ctx.market_type)
        ctx.final_score = unified_result['final_score']
        ctx.market_score = unified_result['market_score']
        ctx.confidence_level = unified_result['confidence_level']

        # 6. 入场判断（空单模式 short_mode=True：禁用博反弹/恐慌反转分支）
        entry_result = UnifiedEntryLogic.check_entry(
            stock_score=stock_score,
            final_score=ctx.final_score,
            tech=tech,
            market=market,
            up_ratio=up_ratio,
            latest_price=ctx.latest_price,
            adx_state=adx_result,
            short_mode=(ctx.direction == 'short'),
            direction=ctx.direction,
            period=ctx.period,
        )
        ctx.entry_action = entry_result['action']
        ctx.entry_position = entry_result['position']
        ctx.signal_position = entry_result['position']
        ctx.entry_reason = entry_result['reason']
        ctx.veto_flags = entry_result['veto_flags']
        ctx.has_real_veto = entry_result['has_real_veto']

        # 6.5 动态仓位管理已移除（按需求不再使用）；如需连续状态机可单独恢复。
        ctx.dynamic_position = None

        # 7. 应用仓位门控（参考系数；期货中性，系数=1.0）
        actual_position, _ = MarketGate.apply_gate(ctx.entry_position, up_ratio, ctx.market_type, sentiment=sentiment)
        ctx.entry_position = actual_position

        # 8. 股票分类（基于因子预期体系；空单时技术信号语义反转）
        classification = StockClassifier.classify(ctx, direction=ctx.direction)
        ctx.stock_type = classification['type']
        ctx.type_label = classification['type_label']
        ctx.type_narrative = classification['narrative']
        ctx.type_strategy = classification['strategy']
        # v8.2: 用户展示字段
        ctx.status = classification.get('status', '')
        ctx.status_label = classification.get('status_label', '')

        # 9. 信号有效性评级
        signal_rating = SignalRating.calculate(
            stock_score=ctx.stock_score,
            tech=tech,
            up_ratio=up_ratio,
            stock_type=ctx.stock_type,
            final_score=ctx.final_score,
            tech_strength=tech_strength,
            significant_factors=significant_factors
        )
        ctx.signal_rating = signal_rating

        # 10. 计算关键价位
        atr = ATRCalculator.calc_wilder(data_list, 14) or ctx.latest_price * 0.02

        # 止损：读用户配置（风控优先）—— 硬止损 = 建仓价 × (1 - single_max_loss_pct)
        # 空头方向反转：价格涨破 建仓价 × (1 + loss_pct) 触发（对照 backtest_futures.py 语义）
        # 无持仓（决策报告）或配置缺失时，退回 MA20/ATR 作内部参考（空仓报告不展示硬止损）
        ma20 = tech.get('sma_20', ctx.latest_price)
        sl_cfg = get_risk_params().get('single_max_loss_pct', 0) or 0
        is_short = (ctx.direction == 'short')
        if ctx.entry_price and ctx.entry_price > 0 and sl_cfg > 0:
            if is_short:
                ctx.stop_loss = round(ctx.entry_price * (1 + sl_cfg), 2)
            else:
                ctx.stop_loss = round(ctx.entry_price * (1 - sl_cfg), 2)
        elif ma20 > 0:
            ctx.stop_loss = round(ma20, 2)
        else:
            ctx.stop_loss = round(ctx.latest_price - 2 * atr, 2)

        # 支撑：MA60 或 近期低点
        ma60 = tech.get('sma_60', ctx.latest_price)
        _lookback = min(20, len(data_list))
        recent_low = min([d.get('low', 0) for d in data_list[-_lookback:]]) if _lookback > 0 else ctx.latest_price * 0.95
        ctx.support = min(ma60, recent_low) if ma60 > 0 else recent_low
        ctx.support = round(ctx.support, 2)

        # 压力：MA20 或 近期高点
        ma20_for_resistance = tech.get('sma_20', ctx.latest_price)
        recent_high = max([d.get('high', 0) for d in data_list[-_lookback:]]) if _lookback > 0 else ctx.latest_price * 1.05
        ctx.resistance = max(ma20_for_resistance, recent_high) if ma20_for_resistance > 0 else recent_high
        ctx.resistance = round(ctx.resistance, 2)

        # 11. 计算多空信号
        ctx.chip_signal = calc_chip_concentration(data_list) or {}
        ctx.fib_signal = calc_fibonacci_retracement(data_list) or {}
        ctx.pivot_signal = calc_pivot_points(data_list) or {}

        # MA信号
        ma_signals = []
        for ma_name in ['sma_5', 'sma_10', 'sma_20', 'sma_60', 'sma_120']:
            ma_val = tech.get(ma_name, 0)
            if ma_val > 0:
                ma_signals.append({
                    'name': ma_name.replace('sma_', 'MA'),
                    'value': round(ma_val, 2),
                    'position': '上方' if ctx.latest_price > ma_val else '下方'
                })
        ctx.ma_signals = ma_signals

        # 12. 如有持仓，计算加减仓和幽灵验证（配置即启用：对应参数存在即运行）
        if has_position and (entry_price or 0) > 0:
            # 浮盈：多头 (现价-成本)/成本；空头反向 (成本-现价)/成本（下跌盈利）
            if ctx.direction == 'short':
                ctx.pnl_pct = (entry_price - ctx.latest_price) / entry_price * 100
            else:
                ctx.pnl_pct = (ctx.latest_price - entry_price) / entry_price * 100

            # 加仓（配置即启用：add_params 存在即运行）
            if quant_config.get_add_params():
                ctx.add_suggestion = AddEngine.suggest_add(ctx)
            else:
                ctx.add_suggestion = {'can_add': False, 'add_ratio': 0.0,
                                      'action': '未配置', 'reason': '未配置加仓参数（配置即启用）'}

            # 减仓（配置即启用：reduce_params 存在即运行）
            if quant_config.get_reduce_params():
                ctx.reduce_suggestion = ReduceEngine.suggest_reduce(ctx)
            else:
                ctx.reduce_suggestion = {'can_reduce': False, 'reduce_ratio': 0.0,
                                         'action': '未配置', 'reason': '未配置减仓参数（配置即启用）'}

            # 幽灵验证（配置即启用：ghost_rules 存在即运行）
            if quant_config.get_ghost_params():
                ctx.ghost_result = GhostEngine.validate(ctx)
            else:
                ctx.ghost_result = None

        # 13. 生成条件预案
        ctx.plan_scenarios = cls._generate_plan_scenarios(ctx)

        # 14. 统一计算决策裁决（单一真相源，报告各板块与 web_api 只渲染不重算）
        ctx.decision = compute_decision(ctx)

        return ctx

    @classmethod
    def _generate_plan_scenarios(cls, ctx):
        """
        生成条件预案

        逻辑：
        1. 获取当前价格和关键均线（MA5/MA10/MA20）
        2. 判断当前价格在均线之上还是之下
        3. 向上预案：站上当前在下方均线 → 执行加仓
        4. 向下预案：跌破当前在上方均线 → 执行减仓
        5. 突破前高或跌破前低
        6. 横盘3天重新查询
        """
        current_price = ctx.latest_price
        tech = ctx.tech

        # 获取关键均线
        ma5 = tech.get('sma_5', current_price)
        ma10 = tech.get('sma_10', current_price)
        ma20 = tech.get('sma_20', current_price)

        # 获取近期高低点
        data_list = ctx.data_list
        _lookback = min(20, len(data_list)) if data_list else 0
        if _lookback > 0:
            recent_high = max([d.get('high', 0) for d in data_list[-_lookback:]])
            recent_low = min([d.get('low', 0) for d in data_list[-_lookback:]])
        else:
            recent_high = current_price * 1.05
            recent_low = current_price * 0.95

        scenarios = []

        # ============================================================
        # 向上路径：站上当前在下方的均线
        # ============================================================
        above_ma5 = current_price > ma5
        above_ma10 = current_price > ma10
        above_ma20 = current_price > ma20

        if not above_ma5:
            scenarios.append({
                'icon': '🟢',
                'trigger': f'站上 MA5（{ma5:.2f}）',
                'action': '触及加仓参考条件'
            })
        if not above_ma10:
            scenarios.append({
                'icon': '🟢',
                'trigger': f'站上 MA10（{ma10:.2f}）',
                'action': '触及加仓参考条件'
            })
        if not above_ma20:
            scenarios.append({
                'icon': '🟢',
                'trigger': f'站上 MA20（{ma20:.2f}）',
                'action': '触及加仓参考条件'
            })

        # 突破前高
        if recent_high > current_price:
            scenarios.append({
                'icon': '🎯',
                'trigger': f'突破前高（{recent_high:.2f}）',
                'action': '触及加仓参考条件'
            })

        # ============================================================
        # 向下路径：跌破当前在上方的均线
        # ============================================================
        if above_ma5:
            scenarios.append({
                'icon': '🔴',
                'trigger': f'跌破 MA5（{ma5:.2f}）',
                'action': '触及减仓参考条件'
            })
        if above_ma10:
            scenarios.append({
                'icon': '🔴',
                'trigger': f'跌破 MA10（{ma10:.2f}）',
                'action': '触及减仓参考条件'
            })
        if above_ma20:
            scenarios.append({
                'icon': '🛑',
                'trigger': f'跌破 MA20（{ma20:.2f}）',
                'action': '触及清仓参考条件'
            })

        # 跌破前低
        if recent_low < current_price and not above_ma20:
            scenarios.append({
                'icon': '🛑',
                'trigger': f'跌破前低（{recent_low:.2f}）',
                'action': '触及清仓参考条件'
            })

        # 横盘
        scenarios.append({
            'icon': '⏰',
            'trigger': '3个交易日内未触及任何价位',
            'action': '重新查询评估'
        })

        # 去重，避免重复
        seen = set()
        unique_scenarios = []
        for s in scenarios:
            key = s['trigger']
            if key not in seen:
                seen.add(key)
                unique_scenarios.append(s)

        return unique_scenarios