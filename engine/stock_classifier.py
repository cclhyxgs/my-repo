#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""股票分类器 - 差异化叙事

type 字段保留细粒度值（供 add/reduce engine 使用，不动），
status / status_label 是面向用户的诚实展示。
"""

import logging
import math

from engine import quant_config
from engine.unified_entry_logic import UnifiedEntryLogic

logger = logging.getLogger(__name__)


def get_limit_up_down_pct(stock_code, raw_name=''):
    """根据股票代码和名称判断涨跌停比例。

    A股规则：
    - ST / *ST 股票：5%
    - 创业板（300xxx）/ 科创板（688xxx）：20%
    - 北交所（8xxxxx / 4xxxxx）：30%
    - 普通 A 股：10%

    Args:
        stock_code: 股票代码（如 sh600000 / sz000001 / bj830799）
        raw_name: 股票名称（用于判断 ST）

    Returns:
        float: 涨跌停比例（如 0.1 表示 10%）
    """
    code = stock_code.lower()
    name = (raw_name or '').upper()

    if 'ST' in name:
        return 0.05

    raw = code.replace('sh', '').replace('sz', '').replace('bj', '')

    if raw.startswith(('30',)):
        return 0.20
    if raw.startswith(('68', '688', '787')):
        return 0.20
    if raw.startswith(('8', '4')):
        return 0.30

    return 0.10


class StockClassifier:
    """
    股票分类器

    核心原则：
    1. 基于因子预期体系分类
    2. type 字段保留给引擎（MA/ATR 止损策略选择）
    3. status 字段是面向用户的诚实简化（仅 4 档，每档有实证支撑）
    4. 分类阈值跟随预设方案自动适配（避免因子预期通胀/通缩）
    """

    @classmethod
    def classify(cls, ctx, direction='long'):
        """基于上下文分类（各分类级别独立技术信号 + 独立否决项）

        direction='short'（期货空单）时，方向性技术信号语义反转，与
        UnifiedEntryLogic._evaluate_tech_signal 的反转映射保持一致；
        博反弹/恐慌反转作为左侧抄底做多逻辑，在空单模式下整体禁用。
        """
        final_score = ctx.final_score
        stock_score = ctx.stock_score
        market = ctx.market or {}
        tech = ctx.tech or {}
        latest_price = ctx.latest_price
        adx_state = ctx.adx_state or {}
        is_short = (direction == 'short')
        # up_ratio NaN/None/inf 守卫：NaN 在 Python 中为 truthy，'or 0.0' 兜底会漏过；
        # 统一归中性值 0.5，避免 rebound 逻辑把 NaN 误判为「非恐慌」而与市场门控
        # （判最熊）自相矛盾；有效 0.0（真正 0% 上涨）仍保留为 0.0。
        if isinstance(ctx.up_ratio, (int, float)) and math.isfinite(ctx.up_ratio):
            up_ratio = float(ctx.up_ratio)
        else:
            up_ratio = 0.5

        thresholds = quant_config.get_thresholds()
        t_strong = thresholds['strong']
        t_standard = thresholds['standard']
        t_test = thresholds['test']
        t_pending = thresholds['pending']

        # 分级判据：初级（因子休眠）按「技术条件」从高到低定档，跳过因子分阈值；
        # 高级按「因子分阈值 + 技术条件」定档。与 unified_entry_logic 的 basic 分支保持一致，
        # 确保 stock_type 在初级下也按技术分级，从而让加/减/清按技术档位路由。
        is_basic = quant_config.get_usage_mode() == 'basic'
        _score_pass = (lambda t: True) if is_basic else (lambda t: final_score >= t)

        # 阈值次序自检：用户经 JSON/迁移注入非递减阈值时，分类会错位判级；
        # 引擎不信任配置有序性，此处给出诊断（不改变有效配置下的匹配语义）。
        if not (t_strong >= t_standard >= t_test >= t_pending):
            logger.warning(
                "状态分界阈值非递减(strong>=standard>=test>=pending 不满足)："
                "%s，分类可能不符预期", (t_strong, t_standard, t_test, t_pending)
            )

        entry_conditions = quant_config.get_entry_conditions()
        if entry_conditions is None:
            entry_conditions = {}

        # === 恐慌市不再改写股票类型（仅作风控：仓位由环境门控系数下调）===
        # 股票类型/状态完全按用户配置的 强/标/试/望/博反弹 判定；
        # 仅保留「恐慌反转」——用户显式配置的左侧抄底档（需 恐慌+极端超跌+反转信号），
        # 属于规则不是系统恐惧。其他恐慌市个股一律按配置归类，不再强制「回避」。
        # 空单模式：博反弹/恐慌反转是抄底做多逻辑，整体禁用。
        if is_short:
            rb = {'is_panic': False, 'can_rebound': False,
                  'reason': '空单模式：不适用博反弹/恐慌反转'}
        else:
            rb = UnifiedEntryLogic._check_rebound(tech, stock_score, up_ratio)
        if rb['is_panic'] and rb['can_rebound']:
            return {
                'type': 'panic_rebound',
                'type_label': '🔥 恐慌反转',
                'narrative': '极端超跌，环境恐慌，极小仓位快进快出',
                'strategy': '快进快出，严格止损',
                'status': '博反弹',
                'status_label': '🔥 恐慌反转·极小仓',
            }
        # is_panic 但 !can_rebound：不强制回避，继续走下方配置型判定（类型/状态归配置）。

        # === 否决项判定统一委托 UnifiedEntryLogic.check_level_veto（唯一实现） ===

        # === 按级别从高到低匹配（触发否决则降级）===
        # strong
        strong_cfg = entry_conditions.get('strong', {})
        strong_sig = strong_cfg.get('tech_signal', 'bull_align')
        sv_flags, sv_adj, sv_has = UnifiedEntryLogic.check_level_veto('strong', tech, entry_conditions, with_suffix=False)
        if (_score_pass(t_strong)
                and UnifiedEntryLogic._evaluate_tech_conditions(strong_sig, tech, market, latest_price, adx_state, direction=direction)
                and not sv_has):
            return {
                'type': 'strong',
                'type_label': '📈 强势股',
                'narrative': '多头排列确认，多维度信号共振',
                'strategy': '趋势跟踪，移动止盈',
                'status': '关注',
                'status_label': '✅ 关注',
            }

        # standard
        standard_cfg = entry_conditions.get('standard', {})
        standard_sig = standard_cfg.get('tech_signal', 'bull_align')
        st_flags, st_adj, st_has = UnifiedEntryLogic.check_level_veto('standard', tech, entry_conditions, with_suffix=False)
        if (_score_pass(t_standard)
                and UnifiedEntryLogic._evaluate_tech_conditions(standard_sig, tech, market, latest_price, adx_state, direction=direction)
                and not st_has):
            return {
                'type': 'standard',
                'type_label': '📊 标准股',
                'narrative': '多头排列确认，信号质量良好',
                'strategy': '标准止损，分批止盈',
                'status': '关注',
                'status_label': '✅ 关注',
            }

        # test
        test_cfg = entry_conditions.get('test', {})
        test_sig = test_cfg.get('tech_signal', 'above_ma20')
        te_flags, te_adj, te_has = UnifiedEntryLogic.check_level_veto('test', tech, entry_conditions, with_suffix=False)
        if (_score_pass(t_test)
                and UnifiedEntryLogic._evaluate_tech_conditions(test_sig, tech, market, latest_price, adx_state, direction=direction)
                and not te_has):
            return {
                'type': 'test',
                'type_label': '🔍 试探股',
                'narrative': '站上MA20，信号初现需验证',
                'strategy': '小仓位，严格止损',
                'status': '关注',
                'status_label': '✅ 关注',
            }

        # pending
        pending_cfg = entry_conditions.get('pending', {})
        pending_sig = pending_cfg.get('tech_signal', 'none')
        pe_flags, pe_adj, pe_has = UnifiedEntryLogic.check_level_veto('pending', tech, entry_conditions, with_suffix=False)
        if (_score_pass(t_pending)
                and UnifiedEntryLogic._evaluate_tech_conditions(pending_sig, tech, market, latest_price, adx_state, direction=direction)
                and not pe_has):
            return {
                'type': 'pending',
                'type_label': '⏳ 待确认股',
                'narrative': '信号不足，等待进一步确认',
                'strategy': '观望，等待信号',
                'status': '观望',
                'status_label': '⏳ 观望',
            }

        # 与 unified_entry_logic.check_entry 对齐：已达观望档但未通过 pending 判定
        # （如触发 pending 否决、或技术信号未现），仍归为「观望」，不下沉到左侧抄底分支，
        # 避免报告中出现「入场动作=观望仓」却「分类状态=博反弹」的矛盾。
        if _score_pass(t_pending):
            return {
                'type': 'pending',
                'type_label': '⏳ 待确认股',
                'narrative': '信号不足或触发否决，等待进一步确认',
                'strategy': '观望，等待信号',
                'status': '观望',
                'status_label': '⏳ 观望',
            }

        # === 博反弹 / 弱势（左侧抄底）===
        # 恐慌市且非极端超跌的个股已落入上方配置型判定；此处 rb['can_rebound'] 仅处理
        # 非恐慌常态抄底（或恐慌反转被否决后回落到常规流程）。is_panic 可能为真但无影响。
        if rb['can_rebound']:
            return {
                'type': 'weak_rebound',
                'type_label': '🔄 博反弹',
                'narrative': f'超跌{stock_score:.0f}分，反弹机会（因子预期低 ≠ 无反弹潜力）',
                'strategy': '快进快出，硬止损',
                'status': '博反弹',
                'status_label': '🔄 博反弹',
            }
        return {
            'type': 'weak',
            'type_label': '📉 弱势股',
            'narrative': f'因子预期{final_score:.0f}分，空头主导',
            'strategy': '空仓观望',
            'status': '观望',
            'status_label': '⏳ 观望',
        }
