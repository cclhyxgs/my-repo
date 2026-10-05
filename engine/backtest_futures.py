#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""期货策略回测引擎 — 多空双向 / 保证金杠杆 / T+0 / 期货手续费。

与 backtest_strategy.py 的核心差异：
  ┌──────────────┬─────────────────────────┬───────────────────────────┐
  │              │  backtest_strategy (A股)  │  backtest_futures (期货)    │
  ├──────────────┼─────────────────────────┼───────────────────────────┤
  │ 方向         │  仅做多                  │  多空双向                   │
  │ 结算         │  T+1（次日开盘成交）      │  T+0（当日可开平仓）        │
  │ 杠杆         │  1:1（全额资金）          │  保证金杠杆（5%~14%）       │
  │ 手续费       │  佣金+印花税+过户费       │  手续费（按成交额万分比）   │
  │ 取整         │  100股向下取整            │  按手数取整（1手起）        │
  │ 涨跌停       │  涨停不买/跌停不卖        │  无（或可选期货涨跌停）     │
  │ 评分来源     │  quant_config 体系        │  factor_registry 独立评分   │
  └──────────────┴─────────────────────────┴───────────────────────────┘

评分集成：
  使用 factor_registry 的 REGISTRY 直接计算因子值 + 默认 stats 做 z-score 标准化 +
  默认 weight 加权求和。多空双视角（严格版方向化，与分析侧 calc_stock_score 同口径）：
  多单视角分 score_long = 方向性贡献和 + 强度贡献和（score > 0 → 偏多）；
  空单视角分 score_short = -(方向性贡献和) + 强度贡献和（score > 0 → 偏空）。
  无方向强度因子（ADX/布林带宽/ATR/波动率锥，见 NON_DIRECTIONAL_FACTORS）不取反。
  开多/开空分别用对应视角分，保证「回测规则 == 分析信号」。
  不依赖 quant_config（合规：不内置方案，但提供默认因子权重供回测验证）。

用法：
  python backtest_futures.py --pool black --days 300 --capital 1000000
  python backtest_futures.py --symbols rb,cu,i --days 500
"""

import sys
import os

import json
import math
import time
import argparse
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from collections import defaultdict

from engine.futures_pool import get_contract, all_contracts, resolve_pool, FuturesContract, make_specific_contracts
from engine.futures_data import fetch_futures_daily, batch_fetch
from engine.scoring_core import build_tech, build_market_dict
from engine.indicators import RSICalculator, MACDCalculator, ADXCalculator
from engine.backtest_cancel import BacktestCancelled
from engine.factor_registry import REGISTRY, calc_factor_value
from engine.score_calculator_v2 import NON_DIRECTIONAL_FACTORS

import logging
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

# quant_config 为可选依赖：若可用则回测按方向读取对应期货 scheme 的风控/阈值参数；
# 不可用（纯 CLI 独立运行）时回落模块常量，保证脚本始终可跑。
try:
    from engine import quant_config as _QC
    _QC_AVAILABLE = True
except Exception:  # pragma: no cover - 仅在依赖缺失时触发
    _QC = None
    _QC_AVAILABLE = False


# ============================================================
# 默认因子权重（基于 A 股小盘动量 IC 实证，期货需重新标定）
# ============================================================
DEFAULT_ACTIVE_FACTORS = [
    'ma_arrangement',
    'obv_trend',
    'relative_strength_20d',
    'macd_hist_norm',
    'volume_price_signal',
    'ma_slope',
    'kdj_signal',
    'adx_trend_strength',
    'volume_ratio',
]

# 交易费用
COMMISSION_RATE = 0.0001      # 手续费：成交额的万分之一（期货无印花税/过户费）
SLIPPAGE_TICKS = 1            # 滑点：1 个最小变动价位
CLOSE_TODAY_DISCOUNT = 0.5    # 平今手续费减半（部分交易所对当日开平仓有优惠）

# 回测参数
INITIAL_CAPITAL = 1_000_000   # 初始资金
MAX_MARGIN_PCT = 0.80         # 最大保证金占用率（占净值）
MAX_SINGLE_MARGIN_PCT = 0.20  # 单品种最大保证金占用率
POSITION_WEIGHT = 0.10        # 单次开仓目标保证金比例（占净值）
STOP_LOSS_PCT = 0.05          # 止损线：亏损达保证金 5% 平仓
TAKE_PROFIT_PCT = 0.15        # 止盈线：盈利达保证金 15% 平仓
TIME_STOP_DAYS = 10           # 时间止损：持仓超过 N 天平仓
ENTRY_THRESHOLD = 5.0         # 入场阈值：对应视角分（多单/空单）> 5 才开仓
EXIT_THRESHOLD = 2.0          # 离场阈值：对应视角分 < -2 平仓（信号反转）
MAX_POSITIONS = 10            # 最大同时持仓品种数


# ============================================================
# FuturesPosition — 多空双向持仓
# ============================================================
class FuturesPosition:
    """期货持仓，支持多头(long)和空头(short)。

    保证金 = 合约价值 × 保证金率 = price × multiplier × hands × margin_rate
    盈亏（多头）= (current - entry) × multiplier × hands
    盈亏（空头）= (entry - current) × multiplier × hands
    """

    def __init__(self, contract: FuturesContract, direction: str, entry_date,
                 entry_price: float, hands: int):
        self.contract = contract
        self.direction = direction        # 'long' or 'short'
        self.entry_date = entry_date
        self.entry_price = entry_price
        self.hands = hands
        self.multiplier = contract.multiplier
        self.margin_rate = contract.margin_rate

        # 保证金 = 合约价值 × 保证金率
        self.margin = entry_price * self.multiplier * hands * self.margin_rate
        # 手续费（开仓）
        self.open_commission = self._calc_commission(entry_price, hands)

        # 平仓信息
        self.exit_date = None
        self.exit_price = None
        self.exit_reason = ''
        self.close_commission = 0.0

        # 持仓追踪
        self.bars_held = 0
        self.max_pnl = 0.0
        self.min_pnl = 0.0
        self.peak_pnl = 0.0

    def _calc_commission(self, price, hands):
        """手续费 = 成交额 × 手续费率"""
        turnover = price * self.multiplier * hands
        return turnover * COMMISSION_RATE

    @property
    def is_closed(self):
        return self.exit_date is not None

    @property
    def total_commission(self):
        return self.open_commission + self.close_commission

    def unrealized_pnl(self, current_price):
        """未实现盈亏（金额）"""
        if self.direction == 'long':
            return (current_price - self.entry_price) * self.multiplier * self.hands
        else:
            return (self.entry_price - current_price) * self.multiplier * self.hands

    def pnl_pct_of_margin(self, current_price):
        """盈亏占保证金百分比（杠杆放大后的收益率）"""
        if self.margin <= 0:
            return 0.0
        return self.unrealized_pnl(current_price) / self.margin * 100

    def contract_value(self, price):
        """合约价值"""
        return price * self.multiplier * self.hands

    def close(self, exit_date, exit_price, reason=''):
        """平仓"""
        self.exit_date = exit_date
        self.exit_price = exit_price
        self.exit_reason = reason
        self.close_commission = self._calc_commission(exit_price, self.hands)

    @property
    def realized_pnl(self):
        """已实现盈亏（扣除手续费）"""
        if self.exit_price is None:
            return 0.0
        if self.direction == 'long':
            gross = (self.exit_price - self.entry_price) * self.multiplier * self.hands
        else:
            gross = (self.entry_price - self.exit_price) * self.multiplier * self.hands
        return gross - self.total_commission

    def to_dict(self):
        return {
            'symbol': self.contract.symbol,
            'name': self.contract.name,
            'direction': self.direction,
            'entry_date': str(self.entry_date.date()) if hasattr(self.entry_date, 'date') else str(self.entry_date),
            'entry_price': self.entry_price,
            'hands': self.hands,
            'margin': round(self.margin, 2),
            'exit_date': str(self.exit_date.date()) if self.exit_date and hasattr(self.exit_date, 'date') else None,
            'exit_price': self.exit_price,
            'exit_reason': self.exit_reason,
            'realized_pnl': round(self.realized_pnl, 2),
            'pnl_pct': round(self.pnl_pct_of_margin(self.exit_price or self.entry_price), 2),
            'bars_held': self.bars_held,
            'commission': round(self.total_commission, 2),
        }


# ============================================================
# 独立因子评分（不依赖 quant_config）
# ============================================================
def compute_futures_score(df: pd.DataFrame, active_factors=None, direction='long') -> tuple[float, dict]:
    """计算期货因子评分（独立于 quant_config 体系，与分析侧同口径方向化）。

    使用 factor_registry 的默认 stats/weight 做 z-score 标准化 + 加权求和。
    方向化（严格版）：direction='long' 返回多单视角分；direction='short' 返回
    空单视角分——方向性因子贡献取反（看空排列 → 空单加分），无方向强度因子
    （NON_DIRECTIONAL_FACTORS）不取反。多单视角分 > 0 → 偏多；空单视角分 > 0 → 偏空。

    Args:
        df: K线 DataFrame（trade_time/open/close/high/low/volume）
        active_factors: 启用的因子列表，None 则用默认
        direction: 'long' 多单视角（默认）/'short' 空单视角

    Returns:
        (score, factor_details)
    """
    if active_factors is None:
        active_factors = DEFAULT_ACTIVE_FACTORS

    if len(df) < 30:
        return 0.0, {'warning': 'K线不足30根，无法计算因子'}

    # 提取 OHLCV 序列
    closes = df['close'].values.astype(float)
    opens = df['open'].values.astype(float)
    highs = df['high'].values.astype(float)
    lows = df['low'].values.astype(float)
    volumes = df['volume'].values.astype(float)
    latest_price = closes[-1]
    data_list = df[['open', 'high', 'low', 'close', 'volume']].to_dict('records')

    # 构造 tech 指标字典（复用 scoring_core.build_tech）
    rsi_hist = RSICalculator.calc_series(closes) if hasattr(RSICalculator, 'calc_series') else [50.0] * len(closes)
    macd_accel = MACDCalculator.calc_accel(closes) if hasattr(MACDCalculator, 'calc_accel') else (0.0, '')

    try:
        tech = build_tech(closes, volumes, highs, lows, opens, data_list, rsi_hist, macd_accel)
    except Exception as e:
        logger.debug(f"build_tech 失败: {e}")
        return 0.0, {'warning': f'build_tech 失败: {e}'}

    # 市场上下文（up_ratio 设中性 0.5，期货无市场宽度概念）
    market = build_market_dict(closes, volumes, 0.5)

    # 构造因子计算上下文
    ctx = {
        'closes': closes, 'highs': highs, 'lows': lows,
        'volumes': volumes, 'opens': opens,
        'data_list': data_list, 'latest_price': latest_price,
        'tech': tech, 'market': market,
    }

    # 计算每个因子的值 + z-score 标准化 + 加权
    score = 0.0
    details = {}
    total_weight = 0.0

    for name in active_factors:
        fdef = REGISTRY.get(name)
        if fdef is None:
            continue

        # 计算原始因子值
        params = {k: v['default'] for k, v in fdef.params_schema.items()}
        raw_value = calc_factor_value(name, ctx, params)

        # z-score 标准化
        mean = fdef.default_stats.get('mean', 0)
        std = fdef.default_stats.get('std', 1)
        if std <= 0:
            std = 1
        z = (raw_value - mean) / std

        # 方向调整 + 截断
        z = z * fdef.default_direction
        z = max(-3.0, min(3.0, z))  # ±3σ 截断
        # 空单视角：方向性因子贡献取反（看空排列 → 空单加分）；强度因子不取反
        if direction == 'short' and name not in NON_DIRECTIONAL_FACTORS:
            z = -z

        # 加权贡献
        weight = fdef.default_weight
        contribution = z * weight
        score += contribution
        total_weight += weight

        details[name] = {
            'raw': round(raw_value, 4),
            'z': round(z, 3),
            'weight': round(weight, 3),
            'contribution': round(contribution, 2),
        }

    # 归一化到 -100~100
    if total_weight > 0:
        score = score / total_weight * 100

    return score, details


# ============================================================
# FuturesBacktester — 期货回测引擎
# ============================================================
class FuturesBacktester:
    """期货策略回测。

    策略逻辑：
      1. 每个交易日对每个品种计算多/空双视角因子评分（严格版方向化）
      2. 多单视角分 > ENTRY_THRESHOLD 且无多头 → 开多
         空单视角分 > ENTRY_THRESHOLD 且无空头 → 开空
      3. 持仓管理：
         - 止损：|盈亏|/保证金 < -STOP_LOSS_PCT → 平仓
         - 止盈：|盈亏|/保证金 > TAKE_PROFIT_PCT → 平仓
         - 时间止损：bars_held > TIME_STOP_DAYS → 平仓
         - 信号反转：多头持仓但多单视角分 < -EXIT_THRESHOLD → 平仓
                     空头持仓但空单视角分 < -EXIT_THRESHOLD → 平仓
      4. T+0：当日开仓当日可平仓
    """

    def __init__(self, capital=INITIAL_CAPITAL, active_factors=None,
                 max_margin_pct=MAX_MARGIN_PCT, max_single_margin_pct=MAX_SINGLE_MARGIN_PCT,
                 position_weight=POSITION_WEIGHT, stop_loss_pct=STOP_LOSS_PCT,
                 take_profit_pct=TAKE_PROFIT_PCT, time_stop_days=TIME_STOP_DAYS,
                 entry_threshold=ENTRY_THRESHOLD, exit_threshold=EXIT_THRESHOLD,
                 max_positions=MAX_POSITIONS):
        self.initial_capital = capital
        self.cash = capital
        self.positions: list[FuturesPosition] = []
        self.closed_positions: list[FuturesPosition] = []
        self.equity_curve = []
        self.trade_log = []
        self.active_factors = active_factors or DEFAULT_ACTIVE_FACTORS

        # 风控参数
        self.max_margin_pct = max_margin_pct
        self.max_single_margin_pct = max_single_margin_pct
        self.position_weight = position_weight
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.time_stop_days = time_stop_days
        self.entry_threshold = entry_threshold
        self.exit_threshold = exit_threshold
        self.max_positions = max_positions

        # 回测参数默认值（模块常量快照）；run() 会按方向用 scheme 配置覆盖。
        self._param_defaults = {
            'stop_loss_pct': self.stop_loss_pct,
            'take_profit_pct': self.take_profit_pct,
            'time_stop_days': self.time_stop_days,
            'entry_threshold': self.entry_threshold,
            'exit_threshold': self.exit_threshold,
            'max_margin_pct': self.max_margin_pct,
            'max_single_margin_pct': self.max_single_margin_pct,
            'max_positions': self.max_positions,
            'position_weight': self.position_weight,
        }
        # 按方向参数表（long/short 各一份）；run() 启动时被 _resolve_dir_params 覆盖。
        self._dir_params = {'long': dict(self._param_defaults),
                            'short': dict(self._param_defaults)}

        # 统计
        self.daily_scores = {}  # date -> {symbol: score}

    # 回测参数 key 与 scheme 配置段的约定映射（用户未配则回落模块常量）
    _RISK_KEYS = ('stop_loss_pct', 'take_profit_pct', 'time_stop_days',
                  'max_margin_pct', 'max_single_margin_pct',
                  'max_positions', 'position_weight')
    _ENTRY_KEYS = ('entry_threshold', 'exit_threshold')

    def _resolve_dir_params(self):
        """按方向解析回测参数，返回 {direction: {param: value}}。

        优先取对应期货 scheme 的 risk_params / entry_conditions / thresholds；
        缺失或 quant_config 不可用时回落实例默认（模块常量），保持纯 CLI 可独立运行。
        """
        params = {'long': dict(self._param_defaults),
                  'short': dict(self._param_defaults)}
        if not _QC_AVAILABLE:
            return params
        for direction in ('long', 'short'):
            try:
                _QC.load_market_scheme('futures', direction)
                rp = _QC.get_risk_params() or {}
                ec = _QC.get_entry_conditions() or {}
                th = _QC.get_thresholds() or {}
                for k in self._RISK_KEYS:
                    v = rp.get(k) if isinstance(rp, dict) else None
                    if isinstance(v, (int, float)):
                        params[direction][k] = v
                for k in self._ENTRY_KEYS:
                    v = ec.get(k) if isinstance(ec, dict) else None
                    if not isinstance(v, (int, float)) and isinstance(th, dict):
                        v = th.get(k)
                    if isinstance(v, (int, float)):
                        params[direction][k] = v
            except Exception as e:  # noqa: BLE001
                logger.debug('回测读取期货%s方案参数失败，回落默认: %s', direction, e)
        return params

    @property
    def total_margin(self):
        return sum(p.margin for p in self.positions if not p.is_closed)

    @property
    def floating_pnl(self):
        return sum(p.unrealized_pnl(p._current_price) for p in self.positions if not p.is_closed and hasattr(p, '_current_price'))

    def nav(self, date, price_map):
        """当前净值 = 现金 + 持仓浮盈亏"""
        pos_pnl = 0.0
        for p in self.positions:
            if p.is_closed:
                continue
            price = price_map.get(p.contract.symbol, p.entry_price)
            pos_pnl += p.unrealized_pnl(price)
        return self.cash + pos_pnl

    def _calc_hands(self, price, multiplier, margin_rate, nav, direction='long'):
        """计算可开仓手数。

        目标保证金 = NAV × position_weight
        每手保证金 = price × multiplier × margin_rate
        手数 = floor(目标保证金 / 每手保证金)
        position_weight / max_single_margin_pct 按方向取对应 scheme 参数。
        """
        margin_per_hand = price * multiplier * margin_rate
        if margin_per_hand <= 0:
            return 0
        dp = self._dir_params.get(direction, self._param_defaults)
        target_margin = nav * dp['position_weight']
        # 受单品种上限约束
        max_margin = nav * dp['max_single_margin_pct']
        target_margin = min(target_margin, max_margin)
        hands = int(target_margin // margin_per_hand)
        return max(hands, 0)

    def _can_open(self, symbol, nav, direction='long'):
        """检查是否可以开新仓（max_positions / max_margin_pct 按方向取 scheme 参数）"""
        dp = self._dir_params.get(direction, self._param_defaults)
        if len([p for p in self.positions if not p.is_closed]) >= dp['max_positions']:
            return False
        # 已有同品种同方向持仓则不加仓（简化版）
        existing = [p for p in self.positions if not p.is_closed and p.contract.symbol == symbol]
        if existing:
            return False
        # 保证金占用检查
        if self.total_margin / max(nav, 1) > dp['max_margin_pct']:
            return False
        return True

    def _open_position(self, contract, direction, date, price, nav):
        """开仓"""
        hands = self._calc_hands(price, contract.multiplier, contract.margin_rate, nav, direction)
        if hands < 1:
            return None

        pos = FuturesPosition(contract, direction, date, price, hands)
        # 扣除保证金 + 开仓手续费
        self.cash -= pos.margin + pos.open_commission

        self.positions.append(pos)
        self.trade_log.append({
            'date': str(date.date()) if hasattr(date, 'date') else str(date),
            'symbol': contract.symbol,
            'action': f'OPEN_{direction.upper()}',
            'price': price,
            'hands': hands,
            'margin': round(pos.margin, 2),
            'commission': round(pos.open_commission, 2),
        })
        return pos

    def _close_position(self, pos, date, price, reason=''):
        """平仓"""
        pos.close(date, price, reason)
        # 回收保证金 + 盈亏 - 平仓手续费
        pnl = pos.unrealized_pnl(price)
        self.cash += pos.margin + pnl - pos.close_commission
        self.closed_positions.append(pos)
        self.positions.remove(pos)

        self.trade_log.append({
            'date': str(date.date()) if hasattr(date, 'date') else str(date),
            'symbol': pos.contract.symbol,
            'action': f'CLOSE_{pos.direction.upper()}',
            'price': price,
            'hands': pos.hands,
            'pnl': round(pnl, 2),
            'commission': round(pos.close_commission, 2),
            'reason': reason,
        })

    def run(self, data: dict, start_date=None, end_date=None, show_progress=True,
            cancel_event=None):
        """运行回测。

        Args:
            data: dict[symbol -> pd.DataFrame] K线数据
            start_date: 起始日期 (str 或 datetime)
            end_date: 结束日期
            show_progress: 打印进度
        """
        if not data:
            print("  [回测] 无数据，终止")
            return

        # 找到所有交易日期的并集
        all_dates = set()
        for symbol, df in data.items():
            for d in df['trade_time']:
                all_dates.add(pd.Timestamp(d).normalize())
        all_dates = sorted(all_dates)

        if start_date:
            start_ts = pd.Timestamp(start_date).normalize()
            all_dates = [d for d in all_dates if d >= start_ts]
        if end_date:
            end_ts = pd.Timestamp(end_date).normalize()
            all_dates = [d for d in all_dates if d <= end_ts]

        if not all_dates:
            print("  [回测] 日期范围内无交易日")
            return

        print(f"  [回测] {all_dates[0].date()} ~ {all_dates[-1].date()}，"
              f"{len(all_dates)} 个交易日，{len(data)} 个品种")

        # 按方向解析回测参数（取对应期货 scheme 配置，缺失回落模块常量）
        self._dir_params = self._resolve_dir_params()

        # 预构建日期→行索引映射
        date_idx = {}
        for symbol, df in data.items():
            date_idx[symbol] = {}
            for i, d in enumerate(df['trade_time']):
                date_idx[symbol][pd.Timestamp(d).normalize()] = i

        # 逐日回测
        for day_i, current_date in enumerate(all_dates):
            # 取消信号：子进程回测中及时响应停止
            if cancel_event is not None and cancel_event.is_set():
                print("  [回测] 收到停止信号，中止。")
                raise BacktestCancelled()
            if show_progress and (day_i % 20 == 0 or day_i == len(all_dates) - 1):
                nav_val = self.nav(current_date, self._get_price_map(data, date_idx, current_date))
                print(f"  [{day_i+1}/{len(all_dates)}] {current_date.date()} "
                      f"NAV={nav_val:,.0f} 持仓={len([p for p in self.positions if not p.is_closed])}")

            # 1. 获取当日价格
            price_map = self._get_price_map(data, date_idx, current_date)

            # 2. 更新持仓的当前价格（用于浮盈亏计算）
            for pos in self.positions:
                if not pos.is_closed:
                    pos._current_price = price_map.get(pos.contract.symbol, pos.entry_price)
                    pos.bars_held += 1
                    pnl = pos.unrealized_pnl(pos._current_price)
                    pos.max_pnl = max(pos.max_pnl, pnl)
                    pos.min_pnl = min(pos.min_pnl, pnl)

            # 3. 检查平仓信号（止损/止盈/时间止损/信号反转）
            for pos in list(self.positions):
                if pos.is_closed:
                    continue

                symbol = pos.contract.symbol
                if symbol not in price_map:
                    continue

                current_price = price_map[symbol]
                pnl_pct = pos.pnl_pct_of_margin(current_price)

                # 止损
                if pnl_pct < -self._dir_params[pos.direction]['stop_loss_pct'] * 100:
                    self._close_position(pos, current_date, current_price, f'止损({pnl_pct:.1f}%)')
                    continue

                # 止盈
                if pnl_pct > self._dir_params[pos.direction]['take_profit_pct'] * 100:
                    self._close_position(pos, current_date, current_price, f'止盈({pnl_pct:.1f}%)')
                    continue

                # 时间止损
                if pos.bars_held > self._dir_params[pos.direction]['time_stop_days']:
                    self._close_position(pos, current_date, current_price, f'时间止损({pos.bars_held}天)')
                    continue

                # 信号反转平仓（用对应视角分：多仓看多单视角、空仓看空单视角）
                df = data.get(symbol)
                if df is not None:
                    idx = date_idx[symbol].get(current_date)
                    if idx is not None and idx >= 30:
                        df_slice = df.iloc[:idx+1]
                        score, _ = compute_futures_score(df_slice, self.active_factors, pos.direction)
                        if score < -self._dir_params[pos.direction]['exit_threshold']:
                            self._close_position(pos, current_date, current_price, f'信号反转(score={score:.1f})')

            # 4. 检查开仓信号
            nav_val = self.nav(current_date, price_map)
            if nav_val <= 0:
                print(f"  [回测] 净值为负，停止开仓: {current_date.date()}")
                continue

            daily_scores = {}
            for symbol, df in data.items():
                idx = date_idx[symbol].get(current_date)
                if idx is None or idx < 30:
                    continue

                df_slice = df.iloc[:idx+1]
                # 多/空双视角分（严格版方向化：方向性因子取反、强度因子不取反）
                score_long, _ = compute_futures_score(df_slice, self.active_factors, 'long')
                score_short, _ = compute_futures_score(df_slice, self.active_factors, 'short')
                daily_scores[symbol] = score_long  # 多单视角分（兼容历史 daily_scores 口径）

                # 开多：多单视角分高
                if score_long > self._dir_params['long']['entry_threshold'] and self._can_open(symbol, nav_val, 'long'):
                    contract = get_contract(symbol)
                    if contract:
                        self._open_position(contract, 'long', current_date,
                                           price_map[symbol], nav_val)

                # 开空：空单视角分高（方向化后看空排列 = 正分）
                elif score_short > self._dir_params['short']['entry_threshold'] and self._can_open(symbol, nav_val, 'short'):
                    contract = get_contract(symbol)
                    if contract:
                        self._open_position(contract, 'short', current_date,
                                           price_map[symbol], nav_val)

            self.daily_scores[current_date] = daily_scores

            # 5. 记录净值曲线
            pos_value = sum(p.unrealized_pnl(price_map.get(p.contract.symbol, p.entry_price))
                          for p in self.positions if not p.is_closed)
            nav_end = self.cash + sum(p.margin for p in self.positions if not p.is_closed) + pos_value
            # 正确计算：nav = cash + 持仓保证金 + 浮盈亏
            # 但 cash 已经扣除了保证金，所以 nav = cash + 保证金 + 浮盈亏
            # 实际上 cash 中已经包含了未占用的资金，保证金被"冻结"但实际仍然属于账户
            # 期货采用逐日盯市：nav = cash + sum(保证金 + 浮盈亏)
            nav_end = self.cash + sum(
                p.margin + p.unrealized_pnl(price_map.get(p.contract.symbol, p.entry_price))
                for p in self.positions if not p.is_closed
            )
            self.equity_curve.append({
                'date': current_date,
                'nav': nav_end,
                'cash': self.cash,
                'margin_used': self.total_margin,
                'positions': len([p for p in self.positions if not p.is_closed]),
                'floating_pnl': pos_value,
            })

        # 回测结束：平掉所有剩余持仓
        last_date = all_dates[-1]
        last_prices = self._get_price_map(data, date_idx, last_date)
        for pos in list(self.positions):
            if not pos.is_closed:
                symbol = pos.contract.symbol
                price = last_prices.get(symbol, pos.entry_price)
                self._close_position(pos, last_date, price, '回测结束')

    @staticmethod
    def _get_price_map(data, date_idx, current_date):
        """获取当日所有品种的收盘价。"""
        price_map = {}
        for symbol, df in data.items():
            idx = date_idx[symbol].get(current_date)
            if idx is not None and idx < len(df):
                price_map[symbol] = float(df.iloc[idx]['close'])
        return price_map

    # ── 结果分析 ──

    def report(self) -> dict:
        """生成回测报告。"""
        closed = self.closed_positions
        if not closed:
            return {'error': '无平仓记录'}

        equity = pd.DataFrame(self.equity_curve)
        if len(equity) > 1:
            equity['daily_return'] = equity['nav'].pct_change()
            total_days = len(equity)
            years = total_days / 252
            cagr = (equity['nav'].iloc[-1] / equity['nav'].iloc[0]) ** (1 / max(years, 0.01)) - 1
            max_dd = ((equity['nav'] / equity['nav'].cummax()) - 1).min()
            sharpe = equity['daily_return'].mean() / max(equity['daily_return'].std(), 1e-9) * (252 ** 0.5)
            calmar = cagr / abs(max_dd) if max_dd < 0 else 0
        else:
            cagr = max_dd = sharpe = calmar = 0

        longs = [p for p in closed if p.direction == 'long']
        shorts = [p for p in closed if p.direction == 'short']
        wins = [p for p in closed if p.realized_pnl > 0]
        losses = [p for p in closed if p.realized_pnl <= 0]

        total_pnl = sum(p.realized_pnl for p in closed)
        total_comm = sum(p.total_commission for p in closed)
        avg_pnl_pct = np.mean([p.pnl_pct_of_margin(p.exit_price) for p in closed if p.exit_price])

        # 按品种统计
        by_symbol = defaultdict(list)
        for p in closed:
            by_symbol[p.contract.symbol].append(p.realized_pnl)

        symbol_stats = []
        for sym, pnls in sorted(by_symbol.items(), key=lambda x: -sum(x[1])):
            contract = get_contract(sym)
            symbol_stats.append({
                'symbol': sym,
                'name': contract.name if contract else sym,
                'trades': len(pnls),
                'total_pnl': round(sum(pnls), 2),
                'avg_pnl': round(np.mean(pnls), 2),
                'win_rate': round(len([p for p in pnls if p > 0]) / len(pnls) * 100, 1),
            })

        return {
            'period': f"{equity['date'].iloc[0].date()} ~ {equity['date'].iloc[-1].date()}" if len(equity) > 0 else '',
            'trading_days': len(equity),
            'initial_capital': self.initial_capital,
            'final_nav': round(equity['nav'].iloc[-1], 2) if len(equity) > 0 else 0,
            'total_return_pct': round((equity['nav'].iloc[-1] / self.initial_capital - 1) * 100, 2) if len(equity) > 0 else 0,
            'cagr_pct': round(cagr * 100, 2),
            'max_drawdown_pct': round(max_dd * 100, 2),
            'sharpe_ratio': round(sharpe, 2),
            'calmar_ratio': round(calmar, 2),
            'total_trades': len(closed),
            'long_trades': len(longs),
            'short_trades': len(shorts),
            'win_rate': round(len(wins) / len(closed) * 100, 1),
            'avg_pnl_pct': round(avg_pnl_pct, 2),
            'total_pnl': round(total_pnl, 2),
            'total_commission': round(total_comm, 2),
            'avg_hold_days': round(np.mean([p.bars_held for p in closed]), 1),
            'symbol_stats': symbol_stats[:20],
        }

    def print_report(self):
        """打印回测报告。"""
        r = self.report()
        if 'error' in r:
            print(f"\n  {r['error']}")
            return

        print("\n" + "=" * 60)
        print("  期货策略回测报告")
        print("=" * 60)
        print(f"  回测区间:      {r['period']}")
        print(f"  交易日数:      {r['trading_days']}")
        print(f"  初始资金:      {r['initial_capital']:,.0f}")
        print(f"  期末净值:      {r['final_nav']:,.0f}")
        print(f"  总收益率:      {r['total_return_pct']:.2f}%")
        print(f"  年化(CAGR):    {r['cagr_pct']:.2f}%")
        print(f"  最大回撤:      {r['max_drawdown_pct']:.2f}%")
        print(f"  Sharpe:        {r['sharpe_ratio']:.2f}")
        print(f"  Calmar:        {r['calmar_ratio']:.2f}")
        print("-" * 60)
        print(f"  总交易次数:    {r['total_trades']} (多{r['long_trades']}/空{r['short_trades']})")
        print(f"  胜率:          {r['win_rate']}%")
        print(f"  平均盈亏(保):  {r['avg_pnl_pct']}%")
        print(f"  总盈亏:        {r['total_pnl']:,.2f}")
        print(f"  总手续费:      {r['total_commission']:,.2f}")
        print(f"  平均持仓天数:  {r['avg_hold_days']}")
        print("-" * 60)
        print("  品种明细 (按总盈亏排序):")
        print(f"  {'品种':<8} {'名称':<10} {'次数':>4} {'总盈亏':>12} {'平均':>10} {'胜率':>6}")
        for s in r.get('symbol_stats', []):
            print(f"  {s['symbol']:<8} {s['name']:<10} {s['trades']:>4} "
                  f"{s['total_pnl']:>12,.0f} {s['avg_pnl']:>10,.0f} {s['win_rate']:>6}%")
        print("=" * 60)


# ============================================================
# CLI 入口
# ============================================================
def main():
    parser = argparse.ArgumentParser(description='期货策略回测')
    parser.add_argument('--pool', default='black',
                        help='品种池: all/black/metal/precious/energy/agri/chem/financial 或逗号分隔代码')
    parser.add_argument('--symbols', default=None,
                        help='指定品种代码(逗号分隔), 如 rb,cu,i  (覆盖 --pool)')
    parser.add_argument('--days', type=int, default=300, help='K线根数(默认300)')
    parser.add_argument('--contract-month', default=None,
                        help='具体交割月份(4位年月, 如 2510)。不指定则用主力连续(rb0)。'
                             '所有交易所统一4位码(如TA2510)。')
    parser.add_argument('--capital', type=float, default=INITIAL_CAPITAL, help='初始资金')
    parser.add_argument('--start', default=None, help='起始日期 YYYY-MM-DD')
    parser.add_argument('--end', default=None, help='结束日期 YYYY-MM-DD')
    parser.add_argument('--save', default=None, help='保存结果到JSON文件')
    args = parser.parse_args()

    # 1. 解析品种池
    if args.symbols:
        contracts = resolve_pool(args.symbols)
    else:
        contracts = resolve_pool(args.pool)

    if not contracts:
        print("  无有效品种，退出")
        return

    # 附加具体合约月份（如果指定）
    if args.contract_month:
        contracts = make_specific_contracts(contracts, args.contract_month)

    contract_tag = f"合约{args.contract_month}" if args.contract_month else "主力连续"
    print(f"\n  期货品种池 [{args.pool if not args.symbols else args.symbols}] "
          f"[{contract_tag}]: {len(contracts)} 个品种")
    for c in contracts:
        print(f"    {c.symbol:<6} {c.name:<10} {c.exchange:<6} "
              f"代码={c.main_code:<8} 乘数={c.multiplier:<6} "
              f"保证金={c.margin_rate:.0%} tick={c.tick_size}")

    # 2. 批量获取K线
    print(f"\n  获取K线数据 (days={args.days})...")
    data = batch_fetch(contracts, days=args.days)

    if not data:
        print("  数据获取失败，退出")
        return

    # 3. 运行回测
    print(f"\n  开始回测...")
    bt = FuturesBacktester(capital=args.capital)
    bt.run(data, start_date=args.start, end_date=args.end)

    # 4. 打印报告
    bt.print_report()

    # 5. 保存净值曲线
    if bt.equity_curve:
        eq_df = pd.DataFrame(bt.equity_curve)
        eq_path = os.path.join(os.path.dirname(__file__), 'bt_futures_equity.csv')
        eq_df.to_csv(eq_path, index=False)
        print(f"\n  净值曲线已保存: {eq_path}")

        trades_df = pd.DataFrame(bt.trade_log)
        trades_path = os.path.join(os.path.dirname(__file__), 'bt_futures_trades.csv')
        trades_df.to_csv(trades_path, index=False)
        print(f"  交易记录已保存: {trades_path}")

    # 6. 保存JSON报告
    if args.save:
        report = bt.report()
        report['trades'] = [p.to_dict() for p in bt.closed_positions]
        with open(args.save, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"  完整报告已保存: {args.save}")


if __name__ == '__main__':
    main()
