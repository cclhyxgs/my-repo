#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""统一入场逻辑"""

import logging
import math

from engine import quant_config
from engine.indicators import MATechnical

logger = logging.getLogger(__name__)


class UnifiedEntryLogic:
    """
    统一入场逻辑

    核心改进：
    1. 入场阈值跟随预设方案自动适配（从 quant_config 读取）
    2. 否决项区分趋势环境
    3. 博反弹条件收紧（<-50 + 加权>=4）
    4. 仓位配置和反转阈值从 quant_config 读取（集中管理）
    """

    # 入场仓位默认值（entry_params.positions 配置不全时的兜底，杜绝 positions['xx'] KeyError）
    _DEFAULT_POSITIONS = {
        'strong': 0.30,
        'standard': 0.20,
        'test': 0.10,
        'observe': 0.05,
        'rebound': 0.05,
        'panic_rebound': 0.02,
        'top_reversal': 0.05,
    }

    @classmethod
    def _get_positions(cls):
        """获取仓位配置（从 quant_config 读取）；配置缺项时自动回填默认值，杜绝 KeyError。"""
        ep = quant_config.get_entry_params() or {}
        raw = ep.get('positions') or {}
        merged = dict(cls._DEFAULT_POSITIONS)
        if isinstance(raw, dict):
            for k, v in raw.items():
                try:
                    merged[k] = float(v)
                except (TypeError, ValueError):
                    pass  # 非法值保留默认
        return merged

    # 技术信号英文 code → 中文标签（触发指标建仓/加仓档位文案复用）。
    # 与 web_api._SIGNAL_BACK_TO_FRONT 语义对齐；引擎只读不写。
    _SIGNAL_LABELS = {
        'none': '', 'bull_align': '多头排列', 'bear_align': '空头排列',
        'above_ma5': '站上MA5', 'below_ma5': '跌破MA5',
        'above_ma20': '站上MA20', 'below_ma20': '跌破MA20',
        'above_ma60': '站上MA60', 'below_ma60': '跌破MA60',
        'kdj_gold': 'KDJ金叉', 'kdj_dead': 'KDJ死叉', 'macd_gold': 'MACD金叉',
        'macd_dead': 'MACD死叉', 'ma_gold': '均线金叉', 'ma_dead': '均线死叉',
        'bb_breakout': '布林带上轨突破', 'bb_breakdown': '布林带下轨突破',
        'adx_trend': 'ADX趋势确认', 'volume_up': '放量上涨', 'volume_down': '放量下跌',
    }

    # 空单模式下的技术信号语义反转映射（方向性信号取反，强度/趋势信号保持）
    _SHORT_SIGNAL_FLIP = {
        'bull_align': 'bear_align',
        'above_ma5': 'below_ma5',
        'above_ma20': 'below_ma20',
        'above_ma60': 'below_ma60',
        'kdj_gold': 'kdj_dead',
        'macd_gold': 'macd_dead',
        'ma_gold': 'ma_dead',
        'bb_breakout': 'bb_breakdown',
        'volume_up': 'volume_down',
    }

    @classmethod
    def check_entry(cls, stock_score, final_score, tech, market, up_ratio, latest_price, adx_state,
                    short_mode=False, direction='long', period='日K'):
        """
        统一入场判断（各分类独立技术信号 + 独立否决项）

        short_mode: 空单模式（期货 direction='short'）。此时 stock_score/final_score
            已是空单视角分（方向性因子取反），本函数禁用「博反弹/恐慌反转」分支：
            空单视角的负分区对应原分>0 的多头强势区，博反弹（抄底做多）语义完全错误。
        direction: 'long' 或 'short'；short 时 `_evaluate_tech_signal` 会自动将多头技术信号
            映射为空头语义（多头排列→空头排列、站上MA→跌破MA、金叉→死叉等）。
        """
        # up_ratio NaN/None/inf 守卫：避免 rebound 逻辑中 'None <= 0.2' 直接 TypeError 崩溃，
        # 或 NaN 漏过兜底被误判为「非恐慌」而与市场门控（判最熊）自相矛盾；
        # 统一归中性值 0.5（与市场门控 get_market_gate_config 的 None 兜底一致）。
        if not isinstance(up_ratio, (int, float)) or math.isnan(up_ratio) or math.isinf(up_ratio):
            up_ratio = 0.5
        entry_conditions = quant_config.get_entry_conditions()
        # 未启用 entry_conditions 时，使用空 dict（引擎各处 .get(key, default) 自动回退内置默认）
        if entry_conditions is None:
            entry_conditions = {}
        thresholds = quant_config.get_thresholds() or {}
        t_strong = thresholds.get('strong', 22)
        t_standard = thresholds.get('standard', 20)
        t_test = thresholds.get('test', 15)
        t_pending = thresholds.get('pending', 10)
        t_rebound = thresholds.get('rebound', -5)
        t_panic = thresholds.get('panic_rebound', -8)

        positions = cls._get_positions()
        entry_params = quant_config.get_entry_params()

        # === 1. 否决项判定统一委托 cls.check_level_veto（唯一实现，见下方类方法） ===

        # === 0. 恐慌反转（用户显式配置的左侧抄底档，需 恐慌+极端超跌+反转信号）===
        # 恐慌市不再硬阻断入场：入场动作完全按用户配置的 强/标/试/望/博反弹 判定，
        # 仓位风险由环境门控系数下调（恐慌 ×0.85）。仅保留用户配置的「恐慌反转」极小仓档。
        # 空单模式（short_mode）：博反弹/恐慌反转是「抄底做多」左侧逻辑，空单视角下
        # 负分区对应原分>0 的多头强势区，语义完全错误 → 整体禁用（is_panic/can_rebound 恒 False）。
        # 其空头镜像为「顶部回落」(_check_top_reversal)：涨势猛/超买(空单视角分很负)+顶部反转信号→做空，仅空单模式启用。
        if short_mode:
            rebound_result = {'is_panic': False, 'can_rebound': False,
                              'reason': '空单模式：负分区为多头强势区，不适用博反弹/恐慌反转'}
        elif quant_config.get_usage_mode() == 'basic':
            # 初级（因子休眠）：博反弹/恐慌反转档改由用户技术条件判定，不走因子超跌逻辑
            rebound_result = {'is_panic': False, 'can_rebound': False,
                              'reason': '初级模式：博反弹/恐慌按用户技术条件判定'}
        else:
            rebound_result = cls._check_rebound(tech, stock_score, up_ratio)
        if rebound_result['is_panic'] and rebound_result['can_rebound']:
            pk_flags, pk_adj, pk_has = cls.check_level_veto('panic_rebound', tech, entry_conditions, with_suffix=True, period=period)
            if not pk_has:
                return cls._build_entry('恐慌反转', positions.get('panic_rebound', 0.02), rebound_result['reason'], pk_flags, pk_adj)
            # 恐慌反转被否决 → 落入常规判定（按配置 强/标/试/望/博反弹）

        # === 2. 依次判断各分类（从高到低）===
        # 空单模式下方向性技术信号语义反转（强度/趋势类信号不反转）
        sig_direction = 'short' if short_mode else direction

        # 分级判据：初级（因子休眠）按「技术条件」从高到低定档；高级按「因子分阈值+技术条件」定档。
        # 与基准 §3.3 一致：两套入口的唯一差异是分级判据，其余（技术门槛判断、否决项、配置）完全共用。
        is_basic = quant_config.get_usage_mode() == 'basic'
        _score_pass = (lambda t: True) if is_basic else (lambda t: final_score >= t)

        # strong
        strong_cfg = entry_conditions.get('strong', {})
        strong_sig = strong_cfg.get('tech_signal', 'bull_align')
        strong_dsp = cls._tech_conditions_display(strong_sig, direction=sig_direction)
        sv_flags, sv_adj, sv_has = cls.check_level_veto('strong', tech, entry_conditions, with_suffix=True, direction=sig_direction, period=period)
        if _score_pass(t_strong) and cls._evaluate_tech_conditions(strong_sig, tech, market, latest_price, adx_state, direction=sig_direction) and not sv_has:
            reason = (f'技术条件{strong_dsp}满足（强势档）' if is_basic and strong_dsp
                      else f'得分{final_score:.0f}（关注区）' + (f'，{strong_dsp}' if strong_dsp else ''))
            return cls._build_entry('关注建仓', positions['strong'], reason, sv_flags, sv_adj)

        # standard
        standard_cfg = entry_conditions.get('standard', {})
        standard_sig = standard_cfg.get('tech_signal', 'bull_align')
        standard_dsp = cls._tech_conditions_display(standard_sig, direction=sig_direction)
        st_flags, st_adj, st_has = cls.check_level_veto('standard', tech, entry_conditions, with_suffix=True, direction=sig_direction, period=period)
        if _score_pass(t_standard) and cls._evaluate_tech_conditions(standard_sig, tech, market, latest_price, adx_state, direction=sig_direction) and not st_has:
            pos = positions['standard']
            reason = (f'技术条件{standard_dsp}满足（标准档）' if is_basic and standard_dsp
                      else f'得分{final_score:.0f}（关注区）' + (f'，{standard_dsp}' if standard_dsp else ''))
            return cls._build_entry('关注建仓', pos, reason, st_flags, st_adj)

        # test
        test_cfg = entry_conditions.get('test', {})
        test_sig = test_cfg.get('tech_signal', 'above_ma20')
        test_dsp = cls._tech_conditions_display(test_sig, direction=sig_direction)
        te_flags, te_adj, te_has = cls.check_level_veto('test', tech, entry_conditions, with_suffix=True, direction=sig_direction, period=period)
        if _score_pass(t_test) and cls._evaluate_tech_conditions(test_sig, tech, market, latest_price, adx_state, direction=sig_direction) and not te_has:
            pos = positions['test']
            reason = (f'技术条件{test_dsp}满足（试探档）' if is_basic and test_dsp
                      else f'得分{final_score:.0f}（关注区）' + (f'，{test_dsp}' if test_dsp else ''))
            return cls._build_entry('关注建仓', pos, reason, te_flags, te_adj)

        # pending
        pending_cfg = entry_conditions.get('pending', {})
        pending_sig = pending_cfg.get('tech_signal', 'none')
        pe_flags, pe_adj, pe_has = cls.check_level_veto('pending', tech, entry_conditions, with_suffix=True, direction=sig_direction, period=period)
        if _score_pass(t_pending) and cls._evaluate_tech_conditions(pending_sig, tech, market, latest_price, adx_state, direction=sig_direction) and not pe_has:
            reason = (f'技术条件{pending_sig}命中观望档' if is_basic
                      else f'得分{final_score:.0f}（观望区），等待确认')
            return cls._build_entry('观望仓', positions['observe'], reason, pe_flags, pe_adj)
        elif (not is_basic) and final_score >= t_pending:
            return cls._build_entry('观望仓', positions['observe'], f'得分{final_score:.0f}（观望区），等待确认', pe_flags, pe_adj)

        # 顶部回落（空单模式专属左侧做空：涨势猛+顶部反转信号，_check_rebound 的镜像）
        top_reversal_result = {'can_top_reversal': False, 'reason': '非空单模式'}
        if short_mode:
            top_reversal_result = cls._check_top_reversal(tech, stock_score, up_ratio)

        # rebound / 恐慌反转：高级走内置超跌+反转信号；初级(因子休眠)走用户技术条件判定
        rebound_cfg = entry_conditions.get('rebound', {})
        if quant_config.get_usage_mode() == 'basic':
            # 初级 恐慌反转档（需用户显式配置技术条件才触发）
            pk_cfg = entry_conditions.get('panic_rebound', {})
            pk_sig = pk_cfg.get('tech_signal', None)
            if cls._tech_spec_configured(pk_sig):
                pk_dsp = cls._tech_conditions_display(pk_sig, direction=sig_direction)
                pk_flags, pk_adj, pk_has = cls.check_level_veto('panic_rebound', tech, entry_conditions, with_suffix=True, direction=sig_direction, period=period)
                if cls._evaluate_tech_conditions(pk_sig, tech, market, latest_price, adx_state, direction=sig_direction) and not pk_has:
                    return cls._build_entry('恐慌反转', positions.get('panic_rebound', 0.02), f'技术条件{pk_dsp}满足', pk_flags, pk_adj)
            # 初级 博反弹档（同需显式配置技术条件才触发）
            rb_sig = rebound_cfg.get('tech_signal', None)
            if cls._tech_spec_configured(rb_sig):
                rb_dsp = cls._tech_conditions_display(rb_sig, direction=sig_direction)
                rb_flags, rb_adj, rb_has = cls.check_level_veto('rebound', tech, entry_conditions, with_suffix=True, direction=sig_direction, period=period)
                if cls._evaluate_tech_conditions(rb_sig, tech, market, latest_price, adx_state, direction=sig_direction) and not rb_has:
                    return cls._build_entry('博反弹', positions.get('rebound', 0.05), f'技术条件{rb_dsp}满足', rb_flags, rb_adj)
        elif rebound_result['can_rebound']:
            rb_flags, rb_adj, rb_has = cls.check_level_veto('rebound', tech, entry_conditions, with_suffix=True, direction=sig_direction, period=period)
            if not rb_has:
                pos = positions.get('rebound', 0.05)
                return cls._build_entry('博反弹', pos, rebound_result['reason'], rb_flags, rb_adj)
            # 博反弹被否决 → 落入下方 空仓观望

        # 顶部回落确认（空单模式）
        if top_reversal_result['can_top_reversal']:
            tr_flags, tr_adj, tr_has = cls.check_level_veto('top_reversal', tech, entry_conditions, with_suffix=True, direction=sig_direction, period=period)
            if not tr_has:
                pos = positions.get('top_reversal', 0.05)
                return cls._build_entry('顶部回落', pos, top_reversal_result['reason'], tr_flags, tr_adj)
            # 顶部回落被否决 → 落入下方 空仓观望

        if is_basic:
            # 初级（因子休眠）：无因子分兜底，所有技术档均未命中（且未触发博反弹/否决）
            # 时即空仓观望——与技术分级语义自洽（无任何技术条件满足 = 不建仓）。
            return cls._build_entry('空仓观望', 0, '未满足任一技术档条件，空仓观望', [], False)
        return cls._build_entry('空仓观望', 0, f'因子预期{final_score:.0f}分，不满足入场条件', [], False)

    @classmethod
    def check_level_veto(cls, level_key, tech, entry_conditions=None, with_suffix=False, direction='long', period='日K'):
        """单级否决项判定（自选诊断 / 决策报告 / check_entry 共用唯一实现）。

        返回 (触发的否决项标签列表, adjusted_veto, 是否真触发) —— 与旧 _check_level_veto 签名一致。
        - tech: 个股技术/原始否决信号 dict（由 factor_tech 注入否决键）。
        - entry_conditions: 用户配置（可省，内部 get_entry_conditions()）。
        - with_suffix: True 时标签追加「（否决）」，兼容 check_entry 旧输出格式。
        - direction: 'long' 用多头否决集(VETO_KEYS)；'short' 用空头专属否决集(SHORT_VETO_KEYS，语义镜像)。
        - period: 分析周期。非日线(分钟)时，「年线」类长窗口否决(ma250_break/ma250_break_up)
          因分钟数据仅约数百根、MA250 退化为约 1 周均线，语义错乱，强制跳过（数据不足）。
        """
        if entry_conditions is None:
            entry_conditions = quant_config.get_entry_conditions()
        level_cfg = entry_conditions.get(level_key, {})
        if not level_cfg.get('veto_on', True):
            return [], False, False
        level_veto = level_cfg.get('veto_enabled', {}) or {}
        veto_keys = quant_config.veto_keys_for_level(level_key, direction)
        # 分钟周期：禁用年线类长窗口否决（数据不足/语义错乱）
        if period not in ('日K', '周K'):
            veto_keys = {k for k in veto_keys if k not in ('ma250_break', 'ma250_break_up')}
        flag_labels = quant_config.SHORT_VETO_FLAG_LABELS if direction == 'short' else quant_config.VETO_FLAG_LABELS
        raw = {k: bool((tech or {}).get(k, False)) for k in veto_keys}
        hit = []
        for vk in veto_keys:
            if level_veto.get(vk, False) and raw.get(vk, False):
                label = flag_labels.get(vk, vk)
                hit.append(f'{label}（否决）' if with_suffix else label)
        return hit, False, bool(hit)

    _SIGNAL_CODE_BY_LABEL = {v: k for k, v in _SIGNAL_LABELS.items() if v}

    @classmethod
    def _normalize_signal_code(cls, name):
        """把中文标签归一化为英文 code；已是 code 或不认识的写法原样返回。

        为什么需要：前端「技术指标筛选」chips 与「技术门槛」编辑器的 signalOptions
        本身就是**中文数组**，保存进方案的是中文标签，而本模块判定链只认英文 code。
        不归一化时 `_evaluate_tech_signal` 会落到末尾 `return False`（条件恒不满足）。
        """
        s = str(name or '').strip()
        if not s or s in cls._SIGNAL_LABELS:
            return s
        return cls._SIGNAL_CODE_BY_LABEL.get(s, s)

    @classmethod
    def _evaluate_tech_signal(cls, signal_name, tech, market, latest_price, adx_state, direction='long'):
        """评估技术信号是否满足。

        direction='short' 时，方向性信号自动映射为空头语义：
          多头排列 -> 空头排列、站上MA -> 跌破MA、金叉 -> 死叉、
          布林上轨突破 -> 布林下轨突破、放量上涨 -> 放量下跌。
        趋势/强度类信号（ADX趋势确认）不反转。
        """
        # 归一化必须在方向翻转**之前**：_SHORT_SIGNAL_FLIP 的键是英文 code
        signal_name = cls._normalize_signal_code(signal_name)
        is_short = (direction == 'short')
        if is_short and signal_name in cls._SHORT_SIGNAL_FLIP:
            signal_name = cls._SHORT_SIGNAL_FLIP[signal_name]

        if not signal_name or signal_name == 'none':
            return True
        if signal_name == 'bull_align':
            # 严格语义：仅"多头排列/完美多头排列"(档位>=2) 才触发，
            # "多头初期"不再混入（2026-08-19 修复，与 judge_arrangement 档位对齐）
            return MATechnical.arrangement_level(market.get('ma_arrangement', '')) >= 2.0
        if signal_name == 'bear_align':
            return MATechnical.arrangement_level(market.get('ma_arrangement', '')) <= -2.0
        if signal_name == 'above_ma5':
            v = tech.get('sma_5', 0)
            return v > 0 and latest_price > v
        if signal_name == 'above_ma20':
            v = tech.get('sma_20', 0)
            return v > 0 and latest_price > v
        if signal_name == 'above_ma60':
            v = tech.get('sma_60', 0)
            return v > 0 and latest_price > v
        if signal_name == 'below_ma5':
            v = tech.get('sma_5', 0)
            return v > 0 and latest_price < v
        if signal_name == 'below_ma20':
            v = tech.get('sma_20', 0)
            return v > 0 and latest_price < v
        if signal_name == 'below_ma60':
            v = tech.get('sma_60', 0)
            return v > 0 and latest_price < v
        if signal_name == 'kdj_gold':
            return tech.get('kdj_signal') == '金叉'
        if signal_name == 'kdj_dead':
            return tech.get('kdj_signal') == '死叉'
        if signal_name == 'macd_gold':
            return tech.get('macd_status') == '金叉'
        if signal_name == 'macd_dead':
            return tech.get('macd_status') == '死叉'
        if signal_name == 'ma_gold':
            ma5 = tech.get('sma_5', latest_price)
            ma10 = tech.get('sma_10', latest_price)
            return ma5 > ma10
        if signal_name == 'ma_dead':
            ma5 = tech.get('sma_5', latest_price)
            ma10 = tech.get('sma_10', latest_price)
            return ma5 < ma10
        if signal_name == 'bb_breakout':
            breakout = tech.get('breakout_signal', '')
            return isinstance(breakout, str) and ('触及上轨' in breakout or '向上突破' in breakout)
        if signal_name == 'bb_breakdown':
            breakout = tech.get('breakout_signal', '')
            return isinstance(breakout, str) and ('触及下轨' in breakout or '向下突破' in breakout)
        # 空单模式下 bb_breakout 会被映射为 bb_breakdown，上面已处理；
        # 若用户直接配置 bb_breakdown（空单场景），同样按跌破下轨判定。
        if signal_name == 'adx_trend':
            return adx_state.get('adx', 0) > 25
        if signal_name == 'volume_up':
            return market.get('volume_price') == '放量上涨'
        if signal_name == 'volume_down':
            return market.get('volume_price') == '放量下跌'
        logger.warning("未识别的建仓 tech_signal 配置: %r，按不满足处理（避免未识别类型默认放行）", signal_name)
        return False

    # 数值指标阈值比较：tech dict 中可做「阈值可调」比较的数值键 → 中文标签。
    # 缺失键求值为 None → 条件不满足（AND 下不误放行）。
    _NUM_IND_LABELS = {
        'sma_5': 'MA5', 'sma_10': 'MA10', 'sma_20': 'MA20', 'sma_60': 'MA60', 'sma_250': 'MA250',
        'rsi_6': 'RSI6', 'rsi_14': 'RSI14', 'rsi_24': 'RSI24',
        'kdj_k': 'KDJ-K', 'kdj_d': 'KDJ-D', 'kdj_j': 'KDJ-J',
        'macd': 'MACD', 'macd_signal': 'DEA', 'macd_hist': 'MACD柱',
        'volume_ratio': '量比', 'volume_z_score': '量能z-score',
        'bb_pct_b': '%B', 'atr': 'ATR', 'atr_ratio': 'ATR占比',
        'adx': 'ADX', 'di_plus': 'DI+', 'di_minus': 'DI-', 'bias20': '乖离率20',
        'wr': 'WR',  # 威廉指标：-100(超卖)~0(超买)，与看板 WR 口径一致
    }

    @classmethod
    def _eval_indicator(cls, key, tech):
        """读取 tech 中的数值指标；缺失/非数值返回 None。"""
        try:
            v = tech.get(key)
            if v is None or isinstance(v, str):
                return None
            return float(v)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _evaluate_single_cond(cls, cond, tech, market, latest_price, adx_state, direction='long'):
        """求值技术门槛 AND 组合中的单个条件。

        cond 支持两种形态（前端编辑器输出）：
          - {'signal': <英文信号code>}：布尔信号，交 _evaluate_tech_signal（自动空头翻转）；
          - {'indicator': <数值键>, 'op': '>='/<=/... , 'value': <阈值>}：数值阈值比较。
        返回 bool；未知形态视为 True（不影响 AND）。
        """
        if isinstance(cond, str):
            return cls._evaluate_tech_signal(cond, tech, market, latest_price, adx_state, direction=direction)
        if not isinstance(cond, dict):
            return True
        if cond.get('signal'):
            return cls._evaluate_tech_signal(str(cond['signal']), tech, market, latest_price, adx_state, direction=direction)
        ind = cond.get('indicator')
        if ind:
            cur = cls._eval_indicator(str(ind), tech)
            if cur is None:
                return False  # 指标缺失视为不满足，避免 AND 下误放行
            op = str(cond.get('op', '>='))
            try:
                val = float(cond.get('value'))
            except (TypeError, ValueError):
                return False
            try:
                return {'>': cur > val, '>=': cur >= val,
                        '<': cur < val, '<=': cur <= val,
                        '==': cur == val}.get(op, False)
            except TypeError:
                return False
        return True

    @classmethod
    def _tech_spec_configured(cls, spec):
        """判断技术条件 spec 是否被用户显式配置。

        与 _evaluate_tech_conditions 的「空=无要求=True」语义互补：缓冲/恐慌等
        未配置默认不触发的档位需先经本函数判定，避免"未配置也按无要求命中"误触发。
        空/None/'none'/'无要求'/空列表均视为未配置 → False。
        """
        if spec is None:
            return False
        if isinstance(spec, str):
            return spec not in ('', 'none', '无要求')
        if isinstance(spec, list):
            return len(spec) > 0
        return False

    @classmethod
    def _evaluate_tech_conditions(cls, spec, tech, market, latest_price, adx_state, direction='long'):
        """评估技术门槛（支持多条件 AND 组合），统一建仓/进级触发入口。

        spec 为：
          - 字符串（旧单选枚举，含 'none'/'无要求'）：交 _evaluate_tech_signal（向后兼容）；
          - list of cond：按 AND 逐条求值，全部命中才返回 True；
          - 空/None：视为「无要求」= True。
        本函数替换各档位原来的 _evaluate_tech_signal 调用点，是「技术门槛可 AND」的唯一实现。
        """
        if spec is None or spec == '' or spec == 'none' or spec == '无要求':
            return True
        if isinstance(spec, str):
            return cls._evaluate_tech_signal(spec, tech, market, latest_price, adx_state, direction=direction)
        if isinstance(spec, list):
            if not spec:
                return True
            for cond in spec:
                if not cls._evaluate_single_cond(cond, tech, market, latest_price, adx_state, direction=direction):
                    return False
            return True
        return True

    @classmethod
    def _tech_conditions_display(cls, spec, direction='long'):
        """把技术门槛 spec 转成可读中文描述（用于 reason/标签）。列表输出「A ∧ B」。"""
        if isinstance(spec, list):
            parts = []
            for c in spec:
                if not isinstance(c, dict):
                    continue
                if c.get('signal'):
                    code = str(c['signal'])
                    if direction == 'short' and code in cls._SHORT_SIGNAL_FLIP:
                        code = cls._SHORT_SIGNAL_FLIP[code]
                    parts.append(cls._SIGNAL_LABELS.get(code, code))
                elif c.get('indicator'):
                    parts.append('{0} {1} {2}'.format(
                        cls._NUM_IND_LABELS.get(str(c.get('indicator')), str(c.get('indicator'))),
                        c.get('op', '>='), c.get('value', '')))
            return ' ∧ '.join(parts)
        code = str(spec)
        if direction == 'short' and code in cls._SHORT_SIGNAL_FLIP:
            code = cls._SHORT_SIGNAL_FLIP[code]
        return cls._SIGNAL_LABELS.get(code, '')

    @classmethod
    def _check_rebound(cls, tech, stock_score, up_ratio):
        """检查博反弹条件（与 StockClassifier 共用，避免双引擎分叉）。

        两道门槛：① 真正超跌(stock_score < t_rebound)； 反转信号加权分 ≥ 阈值
        （新三维打分表，已含核心背离 / 量能 / 形态位置，不再设独立 OR 动能门）。
        注：不再根据大盘涨跌家数占比做环境分级限制，只看信号。
        """
        # up_ratio NaN/None/inf 守卫（防御：本函数亦可能被外部直接调用）：
        if not isinstance(up_ratio, (int, float)) or math.isnan(up_ratio) or math.isinf(up_ratio):
            up_ratio = 0.5

        # 与 check_entry 一致：thresholds 可能未配置(None)，用空 dict 兜底避免 AttributeError。
        thresholds = quant_config.get_thresholds() or {}
        t_rebound = thresholds.get('rebound', -5)

        # 从配置读取建仓参数
        entry_params = quant_config.get_entry_params() or {}
        reversal_threshold = entry_params.get('reversal_score_threshold', 2.0)

        # 条件1：真正超跌
        if stock_score > t_rebound:
            return {'can_rebound': False, 'is_panic': False,
                    'reason': f'因子预期{stock_score:.0f}分，未达超跌阈值({t_rebound})'}

        # 条件2：反转信号加权分（新三维打分表，无独立 OR 动能门）
        reversal_score, signals = cls._calc_reversal_weighted_score(tech, up_ratio)
        if reversal_score < reversal_threshold:
            return {'can_rebound': False, 'is_panic': False,
                    'reason': f'反转信号加权分{reversal_score:.1f}，需>={reversal_threshold}'}

        return {'can_rebound': True, 'is_panic': False,
                'reason': f'超跌{stock_score:.0f}分，信号{reversal_score:.1f}分'}

    @classmethod
    def _calc_reversal_weighted_score(cls, tech, up_ratio):
        """计算反转信号加权分（三维打分表）。

        维度：
          🔥 核心反转：MACD+RSI 双背离共振 +3.0 / 仅MACD +1.5 / 仅RSI +1.0
          🚀 量能确认：恐慌性爆量长阳 +1.5 / 极度缩量止跌(量<0.6×5日均量 且 连续2日不新低) +0.5
          📊 形态与位置：强势反转K线(看涨吞没/早晨之星) +1.0 / 弱势止跌K线(锤子/十字星/长下影) +0.5 / 极端超跌区 +0.5
        """
        score = 0
        signals = []

        # === 核心反转（王者权重）===
        macd_div = tech.get('macd_divergence', {})
        rsi_div = tech.get('rsi_divergence', {})
        macd_bull = macd_div.get('signal') == 'bullish'
        rsi_bull = rsi_div.get('signal') == 'bullish'
        if macd_bull and rsi_bull:
            score += 3.0
            signals.append(('MACD+RSI双底背离共振', 3.0))
        elif macd_bull:
            score += 1.5
            signals.append(('MACD底背离', 1.5))
        elif rsi_bull:
            score += 1.0
            signals.append(('RSI底背离', 1.0))

        # === 量能确认（点火器）===
        if tech.get('reversal_explosive_up'):
            score += 1.5
            signals.append(('恐慌性爆量长阳', 1.5))
        if tech.get('reversal_no_new_low_2d') and tech.get('volume_ratio', 1) < 0.6:
            score += 0.5
            signals.append(('极度缩量止跌', 0.5))

        # === 形态与位置（安全垫）===
        patterns = tech.get('candlestick_patterns', []) or []
        has_engulfing = any('吞没' in p and '看涨' in p for p in patterns)
        if has_engulfing or tech.get('reversal_morning_star'):
            score += 1.0
            signals.append(('强势反转K线', 1.0))
        if any(('锤子' in p) or ('十字星' in p) or ('下影' in p) for p in patterns):
            score += 0.5
            signals.append(('弱势止跌K线', 0.5))
        if tech.get('reversal_extreme_oversold'):
            score += 0.5
            signals.append(('极端超跌区', 0.5))

        return round(score, 1), signals

    @classmethod
    def _calc_top_reversal_weighted_score(cls, tech):
        """顶部反转信号加权分（镜像 _calc_reversal_weighted_score，用于空单摸顶做空）。

        维度：
          🔥 核心反转：MACD+RSI 双顶背离共振 +3.0 / 仅MACD顶背离 +1.5 / 仅RSI顶背离 +1.0
          🚀 量能确认：爆量长阴 +1.5 / 缩量滞涨(量<0.6×5日均量 且 连续2日不创新高) +0.5
          📊 形态与位置：看跌吞没/倒锤子 +1.0 / 十字星 +0.5 / 极端超买区 +0.5
        """
        score = 0
        signals = []

        # === 核心反转（王者权重，与底部背离对称）===
        macd_div = tech.get('macd_divergence', {})
        rsi_div = tech.get('rsi_divergence', {})
        macd_bear = macd_div.get('signal') == 'bearish'
        rsi_bear = rsi_div.get('signal') == 'bearish'
        if macd_bear and rsi_bear:
            score += 3.0
            signals.append(('MACD+RSI双顶背离共振', 3.0))
        elif macd_bear:
            score += 1.5
            signals.append(('MACD顶背离', 1.5))
        elif rsi_bear:
            score += 1.0
            signals.append(('RSI顶背离', 1.0))

        # === 量能确认（点火器）===
        if tech.get('reversal_explosive_down'):
            score += 1.5
            signals.append(('爆量长阴', 1.5))
        if tech.get('reversal_no_new_high_2d') and tech.get('volume_ratio', 1) < 0.6:
            score += 0.5
            signals.append(('缩量滞涨', 0.5))

        # === 形态与位置（安全垫）===
        patterns = tech.get('candlestick_patterns', []) or []
        has_bear_engulf = any('看跌吞没' in p for p in patterns)
        if has_bear_engulf or tech.get('reversal_evening_star'):
            score += 1.0
            signals.append(('顶部反转K线', 1.0))
        if any('十字星' in p for p in patterns):
            score += 0.5
            signals.append(('变盘十字星', 0.5))
        if tech.get('reversal_extreme_overbought'):
            score += 0.5
            signals.append(('极端超买区', 0.5))

        return round(score, 1), signals

    @classmethod
    def _check_top_reversal(cls, tech, stock_score, up_ratio=None):
        """检查顶部回落/摸顶做空条件（_check_rebound 的空头镜像）。

        两道门槛：① 空单视角分很负(stock_score < t_top_reversal) = 涨势猛/超买；
        ② 顶部反转信号加权分 ≥ 阈值（顶背离 / 看跌吞没 / 射击之星等）。
        仅空单模式调用（check_entry 已 gate；期货环境中性，不做恐慌/弱势分级）。
        """
        # up_ratio 在期货环境无意义，仅占位（与 _check_rebound 保持一致签名）
        thresholds = quant_config.get_thresholds() or {}
        t_top = thresholds.get('top_reversal', -22)
        entry_params = quant_config.get_entry_params() or {}
        reversal_threshold = entry_params.get('reversal_score_threshold', 2.0)

        # 条件1：涨势猛/超买（空单视角分很负）
        if stock_score >= t_top:
            return {'can_top_reversal': False,
                    'reason': f'因子预期{stock_score:.0f}分，未达超买阈值({t_top})'}

        # 条件2：顶部反转信号加权分
        top_score, signals = cls._calc_top_reversal_weighted_score(tech)
        if top_score < reversal_threshold:
            return {'can_top_reversal': False,
                    'reason': f'顶部反转信号加权分{top_score:.1f}，需>={reversal_threshold}'}

        return {'can_top_reversal': True,
                'reason': f'涨势猛超买{stock_score:.0f}分，顶部反转信号{top_score:.1f}分'}

    @classmethod
    def _build_entry(cls, action, position, reason, veto_flags, adjusted_veto):
        """构建入场结果"""
        return {
            'action': action,
            'position': round(position, 3),
            'reason': reason,
            'veto_flags': veto_flags,
            'adjusted_veto': adjusted_veto,
            'has_real_veto': any('否决' in f for f in veto_flags)
        }

