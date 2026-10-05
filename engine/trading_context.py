#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""统一上下文 - 所有模块共享的数据容器"""

from datetime import datetime


class TradingContext:
    """
    交易上下文 - 所有模块共享的统一数据容器

    用途：
    1. 因子预期模块写入 stock_score, final_score
    2. 入场模块读取 final_score, 写入 entry_action
    3. 分类模块读取 ctx, 写入 stock_type
    4. 加减仓模块读取 ctx, 写入 add/reduce 建议
    5. 幽灵模块读取 ctx, 验证持仓
    6. 报告模块读取 ctx, 生成报告
    """

    def __init__(self, stock_code, up_ratio, tech, market, data_list):
        self.stock_code = stock_code
        self.up_ratio = up_ratio
        # 涨跌家数是否真实获取（False 表示 up_ratio 用中性默认 0.5，报告需标注）
        self.breadth_fetched = False
        self.tech = tech
        self.market = market
        self.data_list = data_list
        self.latest_price = data_list[-1].get('close', 0) if data_list else 0
        self.query_time = datetime.now()

        # 因子预期结果
        self.stock_score = 0
        self.market_score = 0
        self.final_score = 0
        self.score_detail = []
        self.score_conflicts = []

        # ADX 状态（由 trading_pipeline 计算后写入，供股票分类/加减仓引擎使用）
        self.adx_state = {}

        # 环境配置
        self.env_config = None
        self.limit = 1.0
        self.confidence_factor = 1.0
        self.confidence_level = ''

        # 入场结果
        self.entry_action = ''
        self.entry_position = 0
        self.entry_reason = ''
        self.veto_flags = []
        self.has_real_veto = False

        # 股票分类（内部引擎用，保留兼容）
        self.stock_type = ''
        self.type_label = ''
        self.type_narrative = ''
        self.type_strategy = ''

        # v8.2: 用户展示用——基于回测实证的4档状态
        self.status = ''
        self.status_label = ''
        # 信号评级（trading_pipeline 计算后写入；to_dict 会读取，必须先初始化避免 AttributeError）
        self.signal_rating = {}
        # 信号仓位（trading_pipeline:102 写入 ctx.signal_position；此处声明避免 AttributeError，to_dict 序列化）
        self.signal_position = None

        # 持仓信息（用户输入，仅用于展示）
        self.has_position = False
        self.entry_price = 0
        self.bars_held = 0
        self.pnl_pct = 0
        # 持仓方向（'long'=多头/默认，'short'=空头，期货专用；A 股恒为 long）
        self.direction = 'long'
        # 市场类型（'stock'=A股 / 'futures'=期货），供报告/门禁标签标识
        self.market_type = 'stock'

        # 加减仓结果
        self.add_suggestion = {}
        self.reduce_suggestion = {}

        # 幽灵验证
        self.ghost_result = None

        # 关键价位
        self.stop_loss = 0
        self.support = 0
        self.resistance = 0

        # 多空信号
        self.chip_signal = {}
        self.fib_signal = {}
        self.pivot_signal = {}
        self.ma_signals = {}

        # 条件预案
        self.plan_scenarios = []

        # 建仓决策单一真相源（由 TradingPipeline.execute 经 compute_decision 计算并缓存；
        # 非流水线路径可能为空，report_builder.build 会惰性兜底）
        self.decision = None

    def to_dict(self):
        """导出为字典，供报告使用"""
        return {
            'stock_code': self.stock_code,
            'latest_price': self.latest_price,
            'query_time': self.query_time,
            'breadth_fetched': self.breadth_fetched,
            'stock_score': self.stock_score,
            'market_score': self.market_score,
            'final_score': self.final_score,
            'env_config': self.env_config,
            'limit': self.limit,
            'confidence_factor': self.confidence_factor,
            'confidence_level': self.confidence_level,
            'entry_action': self.entry_action,
            'entry_position': self.entry_position,
            'entry_reason': self.entry_reason,
            'veto_flags': self.veto_flags,
            'has_real_veto': self.has_real_veto,
            'stock_type': self.stock_type,
            'type_label': self.type_label,
            'type_narrative': self.type_narrative,
            'type_strategy': self.type_strategy,
            'has_position': self.has_position,
            'entry_price': self.entry_price,
            'bars_held': self.bars_held,
            'pnl_pct': self.pnl_pct,
            'direction': self.direction,
            'market_type': self.market_type,
            'add_suggestion': self.add_suggestion,
            'reduce_suggestion': self.reduce_suggestion,
            'ghost_result': self.ghost_result,
            'stop_loss': self.stop_loss,
            'support': self.support,
            'resistance': self.resistance,
            'chip_signal': self.chip_signal,
            'fib_signal': self.fib_signal,
            'pivot_signal': self.pivot_signal,
            'ma_signals': self.ma_signals,
            'plan_scenarios': self.plan_scenarios,
            # v8.2+: 状态字段
            'status': self.status,
            'signal_position': self.signal_position,
            'status_label': self.status_label,
            'signal_rating': self.signal_rating,
            # ADX 状态（供报告/调试使用）
            'adx_state': self.adx_state,
            # 因子预期明细
            'score_detail': self.score_detail,
            'score_conflicts': self.score_conflicts,
            # 建仓决策单一真相源（序列化供报告/调试使用；无则为 None）
            'decision': (None if self.decision is None else {
                'verdict': self.decision.verdict,
                'position': self.decision.position,
                'badge_text': self.decision.badge_text,
                'entry_levels': self.decision.entry_levels,
            }),
        }

