#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""信号有效性评级模块"""

from engine import quant_config
from engine.quant_config import get_market_gate_config, get_tech_resonance_config


_LEVELS = {5: "极强", 4: "强", 3: "中", 2: "弱", 1: "极弱"}
_ACTIONS = {
    5: "技术面高度共振，信号确定性最强",
    4: "技术面共振较强，信号较确定",
    3: "技术面中度共振",
    2: "技术面弱共振，信号确定性低",
    1: "技术面无显著共振，信号模糊",
}
# 注：_ACTIONS 仅描述技术面强度（确定性），不给出任何仓位/买卖建议；
#     是否操作由用户独立判断（合规：规则 authorship 归用户）。


class SignalRating:
    """
    信号有效性评级（星级）

    核心定位：不是"胜率预测"，而是"当前环境下，这个信号值不值得冒险"
    权重分配：个股趋势50% + 信号质量30% + 环境20%

    分类阈值跟随预设方案自动适配（从 quant_config 读取），
    避免不同预设的因子预期分分布差异导致评级失真。
    环境配置统一从 quant_config.MARKET_GATE_CONFIG 读取。
    """

    # 代理属性（向后兼容外部直接引用 SignalRating.ENV_CONFIG）
    ENV_CONFIG = quant_config.MARKET_GATE_CONFIG

    @classmethod
    def get_environment(cls, up_ratio, market='stock'):
        """根据上涨比例获取环境配置（委托给 quant_config 统一入口）"""
        return get_market_gate_config(up_ratio, market)

    @classmethod
    def calculate(cls, stock_score, tech, up_ratio, stock_type, final_score=None,
                  tech_strength=None, significant_factors=None, market='stock'):
        """
        计算信号有效性评级

        Args:
            stock_score: 因子预期原始分（-100 ~ 100）
            tech: 技术指标字典
            up_ratio: 上涨比例
            stock_type: 股票类型（用于风险提示）
            final_score: 因子预期终分（经市场门控/缩放后的分数，优先使用）

        Returns:
            dict: { ... }
        """
        # 优先使用 final_score（更反映实际信号质量），否则回退到 stock_score
        score = final_score if final_score is not None else stock_score

        # 从配置读取分类阈值（跟随预设方案）
        thresholds = quant_config.get_thresholds()
        t_strong = thresholds['strong']
        t_standard = thresholds['standard']
        t_test = thresholds['test']
        t_pending = thresholds['pending']
        t_rebound = thresholds.get('rebound', -5)
        t_panic = thresholds.get('panic_rebound', -8)

        # ============================================================
        # 1. 基础强度（优先使用 final_score） - 权重50%
        #
        # 数据驱动标定（2026-07-08）
        # 数据源：全市场回测，267,111个样本，5,176只股票，2025-06 ~ 2026-06
        # 指标：10日持仓前向收益均值，IC=0.039
        #
        # 实证收益排序：
        #   试探股(-4~1):  +1.81%, wr=52.9%   ← 最佳收益（噪声平台内）
        #   标准股(1~10):  +1.78%, wr=56.7%   ← 最高胜率
        #   强势股(≥10):   +1.62%, wr=48.5%   ← 样本最多但胜率平庸
        #   博反弹(<-5):   +1.31%, wr=55.0%   ← 意外有效
        #   待确认(-10~-4): +0.98%, wr=50.6%
        #   警惕股(<-10):   +0.90%, wr=48.9%
        #   恐慌回避:       +0.74%, wr=41.1%
        #   恐慌反转(<-8):  -1.98%, wr=29.4%  ← 主动有害，强制回避
        #
        # 核心结论：
        #   正向三区（试探/标准/强势）收益高度集中（1.62~1.81%），
        #   IC仅0.039意味着因子预期无法在正向区内做细粒度区分，
        #   因此 top3 的 base_strength 应当压缩而非拉开。
        #   真正的区分力来自：
        #     (a) 能否避开恐慌区（base_strength 悬崖）
        #     (b) 质量加成（技术信号共振）
        #     (c) 环境修正（市场冷暖）
        # ============================================================

        # stock_type 优先处理特殊信号（分类器已判断）
        _st = stock_type if stock_type else ''

        if 'panic_rebound' in _st:
            base_strength = 0.05   # 实证 -1.98%，主动有害 → 强制回避
        elif 'panic_avoid' in _st:
            base_strength = 0.20   # 恐慌环境上限：实证 +0.74%，环境压制主导
        elif 'weak_rebound' in _st:
            base_strength = 0.45   # 实证 +1.31%，博反弹意外有效
        elif score >= t_strong:
            base_strength = 0.58   # 强势：实证 +1.62%
        elif score >= t_standard:
            base_strength = 0.60   # 标准：实证 +1.78%，最高胜率
        elif score >= t_test:
            base_strength = 0.62   # 试探：实证 +1.81%，最佳收益
            # ↑ 试探区反超强势区，原因是IC=0.039无法区分正向区内细分
            #   这不是"试探比强势好"的推荐，而是它们同属一个噪声平台
        elif score >= t_pending:
            base_strength = 0.30   # 待确认：实证 +0.98%
        elif score >= t_rebound:
            base_strength = 0.25   # 警惕/弱势：实证 +0.90%
        elif score >= t_panic:
            base_strength = 0.12   # 深度负向：实证 +0.74%
        else:
            base_strength = 0.10   # 极端负向（罕见，通常已被恐慌类型覆盖）

        # ============================================================
        # 2. 技术共振（纯技术面强度）已移至 ScoreCalculatorV2.calc_stock_score
        #    以 tech_strength(方案因子显著能量占比, 含加减分) 给出，并经
        #    tech_resonance 配置映射星级。此处不再内置 quality_boost（引擎
        #    硬编码技术信号 MACD/RSI/OBV/K线），改由用户方案因子驱动，契合纯去默认。
        # ============================================================

        # ============================================================
        # 3. 环境修正（轻市场） - 仅用于 final_rating(因子预期分经环境缩放的展示值) 展示，
        #    不再参与星级（星级由 tech_strength 绝对映射，与因子预期分/环境解耦）。
        # ============================================================
        env_config = cls.get_environment(up_ratio, market)

        # 综合评级（因子预期分映射值，仅展示/兼容下游，不驱动星级）
        raw_rating = base_strength
        final_rating = raw_rating * env_config.get('factor', 1.0)
        final_rating = max(0.05, min(0.95, final_rating))

        # ============================================================
        # 4. 星级映射（技术共振强度 → 绝对标尺）
        #
        # 星级 = 技术面强度（tech_strength∈[0,1]），由方案 tech_strength_bands
        # 切点绝对映射，跨日/跨股票可比，不与因子预期分排序挂钩——二者同源（均出自
        # 方案因子），仅星级衡量"技术面强不强/确不确定"，因子预期分衡量"综合打分高低"；
        # 两者都是技术面评分（按回测有效性加权），都不预测未来收益。
        # tech_strength 由 ScoreCalculatorV2 计算并透传；缺失时降级为 1★。
        # ============================================================
        _trc = get_tech_resonance_config()
        _bands = _trc.get('bands') if _trc else None
        _threshold = _trc.get('threshold') if _trc else None
        if tech_strength is None or _bands is None:
            tier = 1
        else:
            tier = 1
            for b in _bands:
                if tech_strength >= b:
                    tier += 1

        stars = "⭐" * tier
        level = _LEVELS.get(tier, "")
        action = _ACTIONS.get(tier, "")

        # ============================================================
        # 5. 风险提示（仅展示，不干预因子预期评级）
        # 阈值跟随 quant_config 预设方案
        # ============================================================
        risk_note = ""
        if stock_type in ('weak', 'weak_rebound') and stock_score < t_rebound:
            risk_note = f"⚠️ 个股中度破位（因子预期{stock_score:.0f}分），下跌中继概率较高，谨慎参与"
        elif stock_type in ('weak', 'weak_rebound') and stock_score < 0:
            risk_note = f"⚠️ 个股偏弱（因子预期{stock_score:.0f}分），仓位建议减半"

        return {
            'stars': stars,
            'level': level,
            'score': round(final_rating, 2),
            'action': action,
            'env_label': env_config.get('label', ''),
            'env_desc': env_config.get('desc', ''),
            'env_factor': env_config.get('factor', 1.0),
            'env_limit': env_config.get('limit', 1.0),
            'base_strength': round(base_strength, 2),
            'raw_rating': round(raw_rating, 2),
            'tech_strength': round(tech_strength, 2) if tech_strength is not None else 0.0,
            'tech_resonance_threshold': _threshold,
            'tech_strength_bands': _bands,
            'significant_factors': significant_factors or [],
            'risk_note': risk_note,
        }

    @classmethod
    def assign_tech_stars(cls, items, bands=None):
        """在已评分的批量结果上，基于技术共振强度(tech_strength∈[0,1])做绝对星级映射。

        星级是跨日/跨股票可比的绝对标尺，由方案 tech_strength_bands 切点决定，
        不再做"评估日内相对分位"（相对分位会随当日样本分布漂移，且把技术面
        强度错绑到因子预期分排序上）。星级只回答"技术面强不强"，与因子预期分
        同源（都是技术面评分，后者按回测有效性加权）。

        Args:
            items: list[dict]，每个 dict 必须含 'tech_strength'（由 SignalRating.calculate
                   透出）。本方法会就地写入 'tier' / 'stars' / 'level'。
            bands: 方案 tech_strength_bands（4 个升序切点，0~1）；None 用默认 [0.2,0.4,0.6,0.8]。

        Returns:
            items（已就地更新）。缺 tech_strength 的项保留各自回退值。
        """
        if not items:
            return items

        _bands = bands if (isinstance(bands, (list, tuple)) and len(bands) == 4) \
            else [0.2, 0.4, 0.6, 0.8]

        for it in items:
            ts = it.get('tech_strength')
            if ts is None:
                continue  # 保留各自回退值
            tier = 1
            for b in _bands:
                if ts >= b:
                    tier += 1
            it['tier'] = tier
            it['stars'] = "⭐" * tier
            it['level'] = _LEVELS.get(tier, "")
        return items