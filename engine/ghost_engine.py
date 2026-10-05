#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""幽灵规则引擎 - 联动减仓档位"""

import logging

logger = logging.getLogger(__name__)


class GhostEngine:
    """
    幽灵规则引擎

    核心原则：
    1. 规则一：只持有正确的仓位（持有天数内未产生浮盈 → 离场）
    2. 规则二：正确加码（浮盈>3% + 趋势确认 + 未触发减仓档位 → 可加仓）
    3. 减仓档位优先级高于加仓信号

    配置来源：engine.quant_config.get_ghost_params()（config/quant_model.json 各 scheme 的
    config.ghost_rules，随方案可配）
    """

    # 从量化模型读取幽灵规则参数（默认值在 quant_config.DEFAULT_GHOST_RULES 中定义）
    @classmethod
    def _get_cfg(cls):
        from engine.quant_config import get_ghost_params
        return get_ghost_params() or {}  # 未启用时返回空 dict，所有 .get() 使用默认值

    @classmethod
    def _grace_period_days(cls):
        return cls._get_cfg().get('grace_period_days', 3)

    @classmethod
    def _profit_threshold(cls):
        return cls._get_cfg().get('profit_threshold', 0.5)

    @classmethod
    def _add_profit_threshold(cls):
        return cls._get_cfg().get('add_profit_threshold', 3)

    @classmethod
    def _rsi_confirm(cls):
        return cls._get_cfg().get('rsi_confirm', 45)

    @classmethod
    def _ma_confirm(cls):
        return cls._get_cfg().get('ma_confirm', 'sma_20')

    @classmethod
    def validate(cls, ctx):
        """
        幽灵验证
        """
        # 1. 检查是否有持仓信息
        if not ctx.has_position or ctx.entry_price <= 0:
            return None

        entry_price = ctx.entry_price
        current_price = ctx.latest_price
        bars_held = ctx.bars_held
        pnl_pct = ctx.pnl_pct
        is_short = getattr(ctx, 'direction', 'long') == 'short'

        # 2. 检查证明期内是否有浮盈
        # 多头：证明期最高价 → 最高浮盈；空头：证明期最低价 → 最高浮盈（下跌盈利）
        if is_short:
            _extreme_price = current_price
            if ctx.data_list and bars_held > 0:
                _look = min(bars_held, len(ctx.data_list))
                recent_data = ctx.data_list[-_look:]
                _extreme_price = min([d.get('low', 0) for d in recent_data])
            max_pnl_pct = (entry_price - _extreme_price) / entry_price * 100 if entry_price > 0 else 0
        else:
            max_price_since_entry = current_price
            if ctx.data_list and bars_held > 0:
                _look = min(bars_held, len(ctx.data_list))
                recent_data = ctx.data_list[-_look:]
                max_price_since_entry = max([d.get('high', 0) for d in recent_data])
            max_pnl_pct = (max_price_since_entry - entry_price) / entry_price * 100 if entry_price > 0 else 0

        # 3. 规则一：只持有正确的仓位
        grace_period = cls._grace_period_days()
        profit_th = cls._profit_threshold()

        if bars_held >= grace_period and max_pnl_pct < profit_th:
            rule1 = {
                'result': False,
                'reason': f'持有{bars_held}天，最高浮盈{max_pnl_pct:+.1f}%，未达到{profit_th}%阈值',
                'action': '已触及清仓参考条件'
            }
            rule2_result = False
            rule2 = {
                'result': False,
                'reason': '规则一未通过，不检查规则二',
                'action': '未满足加仓参考条件'
            }
            summary = '❌ 持仓未达预期，已触及您设定的风控参考条件'

            return {
                'rule1': rule1,
                'rule2': rule2,
                'summary': summary,
                'entry_price': entry_price,
                'current_price': current_price,
                'bars_held': bars_held,
                'pnl_pct': pnl_pct,
                'max_pnl_pct': max_pnl_pct
            }

        # 4. 规则一通过
        rule1 = {
            'result': True,
            'reason': f'持有{bars_held}天，最高浮盈{max_pnl_pct:+.1f}%，仍在证明期内或已产生浮盈',
            'action': '持仓状态正常'
        }

        # 5. 规则二：正确加码
        # 条件1：浮盈 > add_profit_threshold
        # 条件2：趋势确认（价格 > ma_confirm 且 MA5 > MA10，ma_confirm 可配：sma_20/ema_20…）
        # 条件3：动量确认（RSI(14) > rsi_confirm，过滤弱势反弹中的加仓参考）
        # 条件4：未触发减仓档位（优先级：风控 > 进攻）
        rule2_result = False
        trigger_reason = ""
        rule2_action = ""

        # 检查是否触发了减仓档位
        reduce_suggestion = getattr(ctx, 'reduce_suggestion', None)
        triggered_tiers = reduce_suggestion.get('triggered_tiers', []) if reduce_suggestion else []

        add_profit_th = cls._add_profit_threshold()
        if pnl_pct > add_profit_th:
            ma5 = ctx.tech.get('sma_5', current_price)
            ma10 = ctx.tech.get('sma_10', current_price)
            ma_confirm = cls._ma_confirm()
            if not isinstance(ma_confirm, str):
                ma_confirm = 'sma_20'
            ma_ref = ctx.tech.get(ma_confirm, current_price)
            rsi = ctx.tech.get('rsi', 50)
            rsi_confirm = cls._rsi_confirm()

            trend_ok = (current_price < ma_ref and ma5 < ma10) if is_short else (current_price > ma_ref and ma5 > ma10)
            rsi_ok = (rsi < rsi_confirm) if is_short else (rsi > rsi_confirm)

            if trend_ok and rsi_ok:
                if triggered_tiers:
                    # 已触发减仓档位 → 暂缓加仓
                    rule2_result = False
                    trigger_reason = f'浮盈{pnl_pct:+.1f}%且趋势/动量确认，但已触及减仓参考条件（{triggered_tiers[0]["label"]}），暂未满足加仓参考条件，等待企稳'
                    rule2_action = '暂未满足加仓参考条件'
                else:
                    # 所有条件满足 → 可加仓
                    rule2_result = True
                    trigger_reason = f'浮盈{pnl_pct:+.1f}% > {add_profit_th}% 且趋势/动量确认（{ma_confirm}/RSI），未触及减仓参考条件'
                    rule2_action = '已触及加仓参考条件'
            else:
                # 构建未满足原因（定位具体卡在哪道确认）
                reasons = []
                if not trend_ok:
                    if is_short:
                        if current_price >= ma_ref:
                            reasons.append(f'价格≥{ma_confirm}')
                        if ma5 >= ma10:
                            reasons.append('MA5≥MA10')
                    else:
                        if current_price <= ma_ref:
                            reasons.append(f'价格≤{ma_confirm}')
                        if ma5 <= ma10:
                            reasons.append('MA5≤MA10')
                if not rsi_ok:
                    if is_short:
                        reasons.append(f'RSI{rsi:.0f}≥{rsi_confirm}')
                    else:
                        reasons.append(f'RSI{rsi:.0f}≤{rsi_confirm}')
                rule2_result = False
                trigger_reason = f'浮盈{pnl_pct:+.1f}% > {add_profit_th}% 但{"、".join(reasons)}，等待确认'
                rule2_action = '等待趋势/动量确认'
        else:
            rule2_result = False
            trigger_reason = f'浮盈{pnl_pct:+.1f}%，未达您设定的加仓参考条件阈值'
            rule2_action = '持仓状态正常，继续观察'

        rule2 = {
            'result': rule2_result,
            'reason': trigger_reason,
            'action': rule2_action
        }

        # 6. 生成总结（到达此处 = 规则一已通过，因为未通过时已在步骤3中提前返回）
        if rule2_result:
            summary = '✅ 持仓符合预期，已触及加仓参考条件'
        else:
            summary = '✅ 持仓符合预期，尚未触及加仓参考条件'

        return {
            'rule1': rule1,
            'rule2': rule2,
            'summary': summary,
            'entry_price': entry_price,
            'current_price': current_price,
            'bars_held': bars_held,
            'pnl_pct': pnl_pct,
            'max_pnl_pct': max_pnl_pct
        }