#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""环境门控 - 参考系数（非硬约束）

环境配置统一从 quant_config.MARKET_GATE_CONFIG 读取（单一数据源）。
"""

from engine.quant_config import get_market_gate_config, is_market_gate_enabled, MARKET_GATE_CONFIG


# 市场情绪修正阈值（可调的保守默认值）：
# 命中任一极端情形即对「仓位参考 factor」做小幅下调，帮散户在情绪过热/恐慌时
# 少追高、少逆势。sentiment 缺省或缺数时系数恒为 1.0，与现状逐位一致。
_SENTIMENT_OVERHEAT_LIMIT_UP = 120   # 当日涨停家数 >= 此 → 情绪过热（亢奋防追高）
_SENTIMENT_OVERHEAT_K        = 0.94
_SENTIMENT_PANIC_LIMIT_DOWN  = 60    # 当日跌停家数 >= 此 → 恐慌（弱势防接刀）
_SENTIMENT_PANIC_K           = 0.92


def _sentiment_dampen(sentiment):
    """把 sentiment({'limit_up','limit_down'}) 折算成仓位参考修正系数；缺数视为中性 1.0。"""
    if not isinstance(sentiment, dict):
        return 1.0
    try:
        lu = sentiment.get('limit_up')
        ld = sentiment.get('limit_down')
    except AttributeError:
        return 1.0
    k = 1.0
    if isinstance(lu, (int, float)) and lu >= _SENTIMENT_OVERHEAT_LIMIT_UP:
        k *= _SENTIMENT_OVERHEAT_K
    if isinstance(ld, (int, float)) and ld >= _SENTIMENT_PANIC_LIMIT_DOWN:
        k *= _SENTIMENT_PANIC_K
    return k


class MarketGate:
    """
    环境门控

    核心原则：
    1. 环境只作为参考系数，不是硬约束
    2. 个股好仍可参与，只是仓位参考调整
    3. 总仓位上限作为参考值展示，不强制
    """

    # 代理属性，供外部直接引用 MarketGate.CONFIG
    CONFIG = MARKET_GATE_CONFIG

    @classmethod
    def is_enabled(cls, market='stock'):
        """全局环境门控是否启用（关闭时仓位不随环境系数调整）。

        期货(多/空)不使用 A 股「全市场上涨家数占比」门控，恒返回 False。
        """
        return is_market_gate_enabled(market)

    # 兜底中性环境配置（get_environment 下游结构变动时的最后防线）
    _NEUTRAL_ENV = {'factor': 1.0, 'limit': 1.0, 'label': '中性兜底', 'desc': '配置异常，回落中性'}

    @classmethod
    def get_environment(cls, up_ratio, market='stock', sentiment=None):
        """根据上涨比例获取环境配置（委托给 quant_config 统一入口）。

        健壮性：若底层返回非 dict 或缺关键字段，归一返回含 factor/limit/label/desc 的合法 dict。
        sentiment：可选的市场情绪 {'limit_up', 'limit_down'}（生产路径注入），
            仅在 A 股且门控启用时对 factor 做保守微调；缺省/缺数 → 与现状逐位一致。
        """
        if up_ratio is None:
            up_ratio = 0.0
        env = get_market_gate_config(up_ratio, market)
        if not isinstance(env, dict):
            return dict(cls._NEUTRAL_ENV)
        # 保证四个关键字段都存在且类型合法（避免下游 env['xx'] 时 KeyError）
        out = dict(cls._NEUTRAL_ENV)
        try:
            out['factor'] = float(env.get('factor', 1.0))
        except (TypeError, ValueError):
            pass
        try:
            out['limit'] = float(env.get('limit', 1.0))
        except (TypeError, ValueError):
            pass
        if 'label' in env and isinstance(env['label'], str):
            out['label'] = env['label']
        if 'desc' in env and isinstance(env['desc'], str):
            out['desc'] = env['desc']
        # 市场情绪修正：仅 A 股 + 门控启用时对仓位参考 factor 做保守下调；
        # sentiment 缺省/缺数 → 系数 1.0，行为与改动前逐位一致（旧调用零改动）。
        if sentiment and market == 'stock' and cls.is_enabled(market):
            _k = _sentiment_dampen(sentiment)
            if _k < 1.0:
                out['factor'] = out['factor'] * _k
        return out

    @classmethod
    def apply_gate(cls, target_position, up_ratio, market='stock', sentiment=None):
        """
        应用仓位参考系数

        Args:
            target_position: 目标仓位（0.0 ~ 1.0）
            up_ratio: 上涨比例
            market: 'stock' 走 A 股环境门控；期货(多/空)走中性（不调整仓位）
            sentiment: 可选市场情绪（透传 get_environment 做 factor 微调）

        Returns:
            actual_position: 参考后仓位
            env_config: 环境配置（已保证字段完整，可用 .get() 或直接索引）
        """
        if up_ratio is None:
            up_ratio = 0.0
        env = cls.get_environment(up_ratio, market, sentiment=sentiment)
        adjusted = target_position * env.get('factor', 1.0)
        actual = min(adjusted, env.get('limit', 1.0))
        return actual, env

    @classmethod
    def get_market_score(cls, up_ratio, market='stock'):
        """市场评分（相对排序微调）；门控关闭或不适用(期货)时不参与环境微调。"""
        if not cls.is_enabled(market):
            return 0
        if up_ratio is None:
            up_ratio = 0.0
        if up_ratio >= 0.80:
            return 15
        elif up_ratio >= 0.65:
            return 12
        elif up_ratio >= 0.50:
            return 8
        elif up_ratio >= 0.40:
            return 4
        elif up_ratio >= 0.30:
            return -4
        elif up_ratio >= 0.20:
            return -8
        elif up_ratio >= 0.10:
            return -12
        else:
            return -15
