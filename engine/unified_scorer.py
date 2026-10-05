#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""统一因子预期分融合器 - 融合三维一体架构"""

from engine.market_gate import MarketGate


class UnifiedScorer:
    """
    统一因子预期分融合器

    计算公式：
    最终得分 = 个股原始分 × 个股权重 + 市场评分 × 市场权重
    实际仓位 = min(目标仓位, 环境仓位上限)
    """

    @staticmethod
    def calculate_final_score(stock_score, up_ratio, market='stock'):
        """
        计算最终综合得分

        market: 'stock' 走 A 股环境门控；期货(多/空)走中性（不调整仓位）。
        """
        # 1. 获取环境配置
        env_config = MarketGate.get_environment(up_ratio, market)

        # 2. 获取市场评分
        market_score = MarketGate.get_market_score(up_ratio, market)

        # 3. U型权重加权（factor 为仓位乘子，需 clamp 到 [0,1] 作权重）
        stock_weight = min(env_config.get('factor', 1.0), 1.0)
        market_weight = max(0.0, 1.0 - stock_weight)

        weighted_stock = stock_score * stock_weight
        weighted_market = market_score * market_weight

        # 4. 计算最终得分（clamp 到合理范围）
        final_score = max(-100.0, min(100.0, weighted_stock + weighted_market))

        return {
            'final_score': round(final_score, 1),
            'raw_stock_score': stock_score,
            'market_score': market_score,
            'env_config': env_config,
            'stock_weight': stock_weight,
            'market_weight': market_weight,
            'weighted_stock': round(weighted_stock, 1),
            'weighted_market': round(weighted_market, 1),
            'limit': env_config.get('limit', 1.0),
            'confidence_factor': env_config.get('factor', 1.0),
            'confidence_level': UnifiedScorer._get_confidence_level(env_config),
        }

    @staticmethod
    def _get_confidence_level(env_config):
        """获取置信度等级描述（简化版）"""
        factor = env_config.get('factor', 1.0)
        if factor >= 1.05:
            return '🟢 高置信度（环境加持）'
        elif factor >= 1.00:
            return '🟢 标准置信度'
        elif factor >= 0.95:
            return '🟡 中等置信度（环境压制）'
        elif factor >= 0.85:
            return '🟠 低置信度（环境显著压制）'
        else:
            return '🔴 极低置信度（环境严酷）'

