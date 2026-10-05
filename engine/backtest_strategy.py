#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""策略回测脚本 v8 - 扩大样本 + 资金复利 + 净值曲线

取数口径：`load_qfq_daily` 统一前复权 store；online 模式下命中不足由 `_fetch_full`
联网补（日K 第一手通达信自算复权，失败回退腾讯）。**不做未复权降级。**

v8 新增:
  --years N         回测年数（默认1，最大5）
  资金复利净值跟踪   每笔建仓=当前净值×仓位比例，盈利亏损滚入下一笔
  每日净值曲线       现金+持仓市价估值 → 日收益率序列
  复利指标           CAGR / 最大回撤 / Sharpe Ratio / Calmar Ratio
  权益曲线CSV        输出 strategy_equity.csv
  简单平均 vs 复利对比 报告中直接展示两种计算方式的差异

模拟完整交易过程：
1. 入场：按 UnifiedEntryLogic 的建仓信号买入（关注建仓/观望仓/博反弹）
2. 持仓：每日按 ReduceEngine 检查减仓/清仓信号
3. 减仓：触发 tier1/tier2 → 按比例卖出部分持股，现金增加
4. 加仓：持仓中且 AddEngine 建议 can_add=True → 按当前持仓金额加仓
5. 止盈/止损：清仓信号或时间止损 → 全部卖出，现金回收
"""

import sys
import os

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import json
import time
import argparse

from engine.data_layer import SCAN_KLINE_DAYS  # 提前导入：下方 fetch_sina_history 默认参数用到

from engine.stock_classifier import get_limit_up_down_pct

COMMISSION_RATE = 0.00025       # 佣金比例：万2.5
# 印花税：法定千1，但**自 2023-08-28 起减半征收为 0.5‰（万分之五）**，仅卖出方缴纳。
# ⛔ 原值 0.001 已过时（会让回测高估每笔卖出的成本 0.05%×成交额）。2026-09-20 联网核实：
#    财政部 2026 年预算与 2026H1 印花税收入均仍按减半口径，政策未变。
STAMP_DUTY_RATE = 0.0005        # 印花税比例：万5（仅卖出，2023-08-28 起减半后现行税率）
TRANSFER_FEE_RATE = 0.00001     # 过户费比例：万0.1（仅沪市股票）
SLIPPAGE_RATE = 0.001           # 滑点比例基准：千1（大盘蓝筹）
# 动态滑点分级：基于20日日均成交额
SLIPPAGE_TIERS = {
    'large_cap':  (5e8,   0.001),   # ≥5亿 → 0.1%
    'mid_cap':    (1e8,   0.002),   # 1~5亿 → 0.2%
    'small_cap':  (5e7,   0.003),   # 0.5~1亿 → 0.3%
    'micro_cap':  (0,     0.005),   # <0.5亿 → 0.5%
}

# —— 逐日扫描进度汇报间隔（交易日）——
# run() 的交易日循环体此前**无任何输出**：全市场 358 个交易日约 6.5 分钟全程静默，
# UI 的进度日志停在「多核扫描进程池已创建」一动不动 → 被误判为卡死并反复重启
# （2026-09-19 实测：12:15 启动，12:16:49 后无输出，用户 12:17/12:20/12:26 三次重启，
#  而回测其实一直在正常计算，实跑 426s 后正常产出报告）。
# 每 N 个交易日汇报一次（含已用/剩余耗时），保证长耗时过程可见。
# 2026-09-19 二次修正：原值 10 → 1。10 的间隔下首屏长时间停在「1/243 · 0%」，
# 用户再次误判为一动不动；改为逐交易日汇报，配合扫描分片心跳（见 _collect），
# 使进度条每个交易日均前进，且单日内的多核扫描也有可见推进。
PROGRESS_EVERY_DAYS = 1


# —— 优先选股集合（选股"核心指数优先"排序用）——
# 优先集合 = engine.stock_pool.get_stock_pool() 的"上证50+创业50+科创50"核心指数池（共 142 只）。
# 不依赖任何外部列表文件；该池加载失败返回空集合时，排序自动退化为原逻辑（不影响现有回测）。
_PRIORITY_SET = None

def load_priority_members():
    """懒惰加载优先选股成分股集合（上证50+创业50+科创50 核心指数池）。

    返回 set(str)（小写归一化）；失败返回空集合。
    """
    global _PRIORITY_SET
    if _PRIORITY_SET is not None:
        return _PRIORITY_SET
    s = set()
    try:
        from engine.stock_pool import get_stock_pool
        s = {t[0].lower() for t in get_stock_pool()}
    except Exception as e:
        print(f"  [优先选股] 加载核心指数池失败: {e}")
    _PRIORITY_SET = s
    if not s:
        print("  [优先选股] 核心指数池为空，'核心指数优先'排序将退化为普通排序。")
    return _PRIORITY_SET


def is_priority_stock(stock_code):
    """判断股票是否落在优先选股集合（上证50+创业50+科创50 核心指数池）。"""
    if not stock_code:
        return False
    return str(stock_code).strip().lower() in load_priority_members()


def adjust_buy_lot(shares):
    """买入/加仓：必须向下取整到100的整数倍，不足100则买0。
    
    A股规则：买入只能按100股递增，不允许零股买入。
    """
    return (shares // 100) * 100


def adjust_sell_lot(current_shares, target_reduce):
    """卖出/减仓：四舍五入到100股，不能超过持仓。
    
    实盘场景：
    - 持有150股（由送股产生的碎股），信号减50%即75股 → 四舍五入卖100股留50股
    - 持有100股，信号减50%即50股 → 四舍五入卖100股（清仓）
    - 持有150股，信号减100%即150股 → 清仓150股（允许零股卖出）
    
    Args:
        current_shares: 当前持仓股数（可能含零股）
        target_reduce: 目标减仓股数（信号计算值）
    
    Returns:
        实际减仓股数（100的整数倍，不超过current_shares）
    """
    if target_reduce >= current_shares:
        return current_shares
    reduce = int(target_reduce / 100 + 0.5) * 100
    return min(max(reduce, 0), current_shares)


def calc_dynamic_slippage(stock_code, date=None, base_rate=None):
    """基于个股过去20日日均成交额动态调整滑点率（避免未来函数）

    Args:
        stock_code: 股票代码
        date: 决策日期（仅使用该日期之前的数据，None时使用最后20日，用于回测结束后清算等场景）
        base_rate: 基准滑点率（默认None，使用全局SLIPPAGE_RATE作为大盘基准）

    Returns:
        float: 动态滑点率
    """
    if base_rate is None:
        base_rate = SLIPPAGE_RATE
    df = _stock_data_cache.get(stock_code)
    if df is None or len(df) < 20:
        return base_rate  # 无数据或数据不足，使用基准滑点

    # 取过去20日的成交额（成交额 = close × volume）
    if date is None:
        recent = df.tail(20)
    else:
        # 仅使用决策日期之前的20个交易日（避免未来函数）
        idx = _get_index_for_date(df, date, stock_code)
        if idx < 20:
            return base_rate
        recent = df.iloc[idx-20:idx]  # 不含当天

    avg_amount = (recent['close'] * recent['volume']).mean()
    # 按流动性分级
    for tier_name, (threshold, rate) in sorted(SLIPPAGE_TIERS.items(), key=lambda x: -x[1][0]):
        if avg_amount >= threshold:
            return rate
    return SLIPPAGE_TIERS['micro_cap'][1]

def calculate_trading_cost(amount, stock_code, is_buy):
    """计算交易费用（A股规则：佣金+印花税+过户费，不含滑点）
    
    Args:
        amount: 交易金额（元）
        stock_code: 股票代码（sh开头为沪市）
        is_buy: 是否买入（True=买入，False=卖出）
    
    Returns:
        dict: {commission, stamp_duty, transfer_fee, total}
    """
    commission = max(amount * COMMISSION_RATE, 5)
    
    stamp_duty = amount * STAMP_DUTY_RATE if not is_buy else 0
    
    transfer_fee = amount * TRANSFER_FEE_RATE if stock_code.startswith('sh') else 0
    
    total = commission + stamp_duty + transfer_fee
    
    return {'commission': commission, 'stamp_duty': stamp_duty, 'transfer_fee': transfer_fee, 'total': total}


def apply_slippage(price, is_buy, slippage_rate=None):
    """应用滑点影响成交价（A股价格取整到0.01元）
    
    Args:
        price: 原始价格（开盘价）
        is_buy: 是否买入（买入价变高，卖出价变低）
        slippage_rate: 滑点率（默认None，使用全局SLIPPAGE_RATE）
    
    Returns:
        float: 含滑点的成交价（已取整到0.01元）
    """
    if slippage_rate is None:
        slippage_rate = SLIPPAGE_RATE
    if is_buy:
        adjusted = price * (1 + slippage_rate)
    else:
        adjusted = price * (1 - slippage_rate)
    return round(adjusted, 2)  # A股最小价格单位0.01元


def calculate_trade_fee(amount, stock_code, is_buy):
    """兼容旧接口：计算含滑点的总费用（保留向后兼容）
    
    Args:
        amount: 交易金额（元）
        stock_code: 股票代码（sh开头为沪市）
        is_buy: 是否买入（True=买入，False=卖出）
    
    Returns:
        total_fee: 总费用（含滑点，仅供兼容）
    """
    cost = calculate_trading_cost(amount, stock_code, is_buy)
    slippage = amount * SLIPPAGE_RATE
    return cost['total'] + slippage
import warnings
import requests
warnings.filterwarnings('ignore')

from .backtest import (_stock_data_cache, _precomputed_cache, _date_idx_cache,
                       calc_rsi_series_fast,
                       calc_macd_hist_series_fast, calc_macd_acceleration_from_hist,
                       _load_disk_cache, _save_disk_cache, CHECKPOINT_DIR,
                       normalize_daily_dates)
from engine.stock_pool import get_stock_pool, get_full_market_pool, resolve_stock_pool
from engine.config import get_app_dir


def _auto_n_workers():
    """按机型探测(核数+内存)自动算安全并行进程数；失败回退核数-1。"""
    try:
        from engine.machine_probe import auto_n_workers
        return auto_n_workers()
    except Exception:
        return max(1, (os.cpu_count() or 1) - 1)


# ==================== 新浪历史日K（未调用，保留备查） ====================

def fetch_sina_history(stock_code, days=SCAN_KLINE_DAYS):
    """新浪历史【未复权】日K。

    ⛔ **本函数当前无任何调用方**（全仓 grep 确认）。回测取数统一走
    `_fetch_stock_data` → `load_qfq_daily` 前复权 store，未复权降级已移除。
    保留仅为将来「未经复权原始价」的离线诊断复用；默认天数用 SCAN_KLINE_DAYS，
    以支持最长 5 年回测。
    """
    try:
        if stock_code.startswith('sh'):
            symbol = stock_code[2:]
        elif stock_code.startswith('sz'):
            symbol = stock_code[2:]
        else:
            symbol = stock_code
        
        url = "https://quotes.sina.com.cn/ifzq/zt/quote/zt/quote/zt/getKLineData"
        params = {
            'symbol': symbol,
            'type': 'day',
            'num': days,
        }
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Referer': 'https://finance.sina.com.cn/',
            'Connection': 'keep-alive',
        }
        
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        
        if resp.status_code != 200:
            return None, f"新浪状态码: {resp.status_code}"
        
        data = resp.json()
        if not data or 'data' not in data:
            return None, "新浪返回数据格式异常"
        
        klines = data.get('data', [])
        if not klines:
            return None, "新浪无K线数据"
        
        rows = []
        for item in klines:
            try:
                rows.append({
                    'trade_time': item.get('date', ''),
                    'open': float(item.get('open', 0)),
                    'close': float(item.get('close', 0)),
                    'high': float(item.get('high', 0)),
                    'low': float(item.get('low', 0)),
                    'volume': float(item.get('volume', 0)),
                })
            except (ValueError, TypeError):
                continue
        
        if not rows:
            return None, "新浪解析K线数据失败"
        
        df = pd.DataFrame(rows)
        df['trade_time'] = pd.to_datetime(df['trade_time'])
        df = df.sort_values('trade_time').reset_index(drop=True)
        df = df[df['close'] > 0]
        
        if df.empty:
            return None, "新浪数据全部无效"
        
        return df, None
        
    except requests.exceptions.Timeout:
        return None, "新浪请求超时"
    except requests.exceptions.ConnectionError:
        return None, "新浪连接失败"
    except json.JSONDecodeError:
        return None, "新浪返回数据解析失败"
    except Exception as e:
        return None, f"新浪异常: {e}"


from engine.data_layer import KLineFetcher
from engine.indicators import (RSICalculator, MACDCalculator)
from engine.stock_classifier import StockClassifier
from engine.unified_entry_logic import UnifiedEntryLogic
from engine.reduce_engine import ReduceEngine
from engine.add_engine import AddEngine
from engine import quant_config
from engine.signal_rating import SignalRating
from engine.backtest_cancel import BacktestCancelled
from engine import scoring_core


def _validate_qfq(df, gap_threshold=0.25):
    """校验单只股票日线是否为干净的【前复权】序列，拦截未复权(除权缺口)与单点坏点。

    返回 (is_valid: bool, df: DataFrame|None, reason: str)，reason ∈
    {'ok', 'vshape_repaired', 'unadjusted', 'too_short', 'empty'}。

    规则：
      - 滤掉 close<=0 行；序列 <3 行直接 ok(too_short)。
      - 逐日 |涨跌幅| > gap_threshold 的点 i（缺口进入 close[i]）分类：
          * V-shape（单点坏点）：缺口后 1~2 天价位回到缺口前水平
            （close[i+1] ≈ close[i-2]，±5%）→ 临时尖刺，用前后值线性插值修复 close[i]，
            保留该 trade_time 日期。单点尖刺会产生进/出两个 >threshold 跳变，
            故用 close[i-2] 作前置参考以跳过尖刺本身。
          * persistent step（未复权除权）：缺口后价位持续低位
            （close[i+1] 仍在 close[i] 的 ±5% 内）且 5 个交易日内未回到缺口前水平
            → 未复权数据，拒绝(None)。
          * 缺口在最前/最后一行 → 视为坏点，丢弃该行。
      - 无 >threshold 缺口 → ok（原样返回过滤后的 df）。
    仅用 pandas/numpy，无外部依赖；不修改输入 df。
    """
    if df is None or not hasattr(df, 'columns') or 'close' not in df.columns:
        return (False, None, 'empty')
    sub = df[df['close'] > 0].copy()
    normalize_daily_dates(sub)   # ⛔ 统一 trade_time 口径（与 engine/backtest.py 同源实现）
    n = len(sub)
    if n < 3:
        return (True, sub if n > 0 else df, 'too_short')

    closes = sub['close'].astype(float).to_numpy()
    ret = np.abs(np.diff(closes) / closes[:-1])
    bad = np.where(ret > gap_threshold)[0] + 1   # 缺口进入的索引 i
    if bad.size == 0:
        return (True, sub, 'ok')

    cser = closes.copy()   # 工作副本：修复值写回，后续邻居引用已修复值
    drop = []
    for i in bad:
        c_cur = closes[i]
        if i == 0 or i == n - 1:
            drop.append(i)
            continue
        pre_ref = closes[i - 2] if i - 2 >= 0 else closes[i - 1]
        c_next_raw = closes[i + 1] if i + 1 < n else np.nan
        c_prev_v = cser[i - 1]
        c_next_v = cser[i + 1] if i + 1 < n else np.nan
        # V-shape：缺口后价位回到缺口前水平 → 插值修复（用已修复邻居值）
        if pd.notna(c_next_raw) and abs(c_next_raw - pre_ref) / max(abs(pre_ref), 1e-9) <= 0.05:
            cser[i] = (c_prev_v + c_next_v) / 2.0 if pd.notna(c_next_v) else c_prev_v
            continue
        # persistent step：缺口后停留在新价位且 5 日内未回到缺口前 → 未复权
        if pd.notna(c_next_raw) and abs(c_next_raw - c_cur) / max(abs(c_cur), 1e-9) <= 0.05:
            nxt = closes[i + 1:i + 6]
            if not np.any(np.abs(nxt - pre_ref) / max(abs(pre_ref), 1e-9) <= 0.05):
                return (False, None, 'unadjusted')
        # 模糊（既非 V 也非明确 step）→ 保守插值修复
        cser[i] = (c_prev_v + c_next_v) / 2.0 if pd.notna(c_next_v) else c_prev_v
    repaired = sub.copy()
    repaired['close'] = cser
    if drop:
        keep = [k for k in range(n) if k not in drop]
        repaired = repaired.iloc[keep].reset_index(drop=True)
    return (True, repaired, 'vshape_repaired')


def _current_kline_source():
    """返回 (K线数据源名, data_sources.json 路径)。

    配置缺失/损坏/无该键 → 返回 (None, path)，调用方按「通达信(默认，第一手源)」处理
    （与 `KLineFetcher._load_kline_sources()` 的兜底一致）。
    """
    try:
        from engine.config import CONFIG_DIR
    except Exception:
        return None, ''
    path = os.path.join(CONFIG_DIR, 'data_sources.json')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return (json.load(f) or {}).get('kline_source'), path
    except Exception:
        return None, path


def _explain_empty_cache():
    """离线模式「缓存 0 命中」时的可操作诊断。

    ⚠️ **首要成因是数据源，不是目录**：`data_sources.json` 的 `kline_source` 设为
    `"sina"` 时，K 线写入的是**独立**的未复权 store（`raw_daily_/raw_weekly_{code}.pkl`），
    而回测只读前复权 `qfq_daily_*.pkl` —— 两者复权尺度不同，是**刻意物理隔离**的。
    所以数据源为新浪时，前复权 store 必然为空、离线回测必然 0 命中，且**与缓存目录
    位置无关**（改目录/搬缓存/清缓存全是白费），排查必须**先看数据源**。
    下面这条目录成因是**次要**的（这个坑也踩过多次）：**数据目录随运行形态变化**（源码直跑=项目根目录，打包 EXE
    onefile=%LOCALAPPDATA%\\M-Bull），缓存攒在 A 副本、程序在 B 副本跑 → 离线模式
    必然 0 命中。旧输出只有一句「无有效交易日！」，完全看不出是目录对不上，
    所以这里直接把"当前目录在哪、另一个副本在哪、怎么搬"打出来。
    """
    try:
        from engine.config import APP_DIR, CACHE_DIR
    except Exception:
        return
    kdir = os.path.join(CACHE_DIR, 'kline')
    try:
        files = os.listdir(kdir) if os.path.isdir(kdir) else []
    except Exception:
        files = []
    n = len([f for f in files if f.endswith('.pkl')])
    n_raw = len([f for f in files if f.startswith('raw_')])
    n_qfq = len([f for f in files if f.startswith('qfq_daily_')])
    src, src_path = _current_kline_source()
    bar = "!" * 62
    print("  " + bar)
    print("  ⚠️ 缓存 0 命中，离线模式无法继续。按顺序排查：")
    if src == 'sina':
        print("     0) 【首要成因】K线数据源 = 新浪（未复权）→ 回测读不到它的缓存")
        print(f"        配置文件：{src_path}")
        print(f"        本目录现有：raw_*（新浪未复权）{n_raw} 个 / qfq_daily_*（腾讯前复权）{n_qfq} 个")
        print("        新浪日K/周K **确实已落盘**（raw_daily_/raw_weekly_*.pkl），但回测只读")
        print("        前复权 qfq_daily_*.pkl —— 两者复权尺度不同，是刻意物理隔离的，不可混用。")
        print("        → 对策A：把 kline_source 改为「tencent」（前复权缓存按需自动重建；")
        print("           代价：分钟级 K 线不可用，腾讯仅支持日K/周K）。")
        print("        → 对策B：取消「离线模式」跑一次（回测会自抓腾讯前复权并落盘，")
        print("           之后离线即可命中；全市场首次约 34 分钟）。")
        print("        以下目录类成因仅在数据源为腾讯时适用：")
    else:
        print(f"     0) K线数据源：{src or 'tencent(默认)'}（前复权 → 会落盘）"
              f"  配置：{src_path or '(不存在)'}")
        print(f"        本目录现有：qfq_daily_* {n_qfq} 个 / 全部 .pkl {n} 个")
    print(f"     1) 当前应用数据目录：{APP_DIR}")
    print(f"        对应的 K线缓存目录：{kdir}")
    print(f"        该目录现有 .pkl 文件：{n} 个")
    print("     2) 数据目录随运行形态变化，两边缓存不会自动互通：")
    print(r"        · 源码直跑（开发态）  → 项目根目录\cache\kline")
    print(r"        · 打包 EXE（onefile）→ %LOCALAPPDATA%\M-Bull\cache\kline")
    print(r"        → 若缓存攒在另一个副本，把它的 cache\kline 整个复制过来即可。")
    print("     3) 或者取消勾选「离线模式」联网抓取（首次 5200 只约 40~80 分钟）。")
    print("  " + bar)


class Position:
    """单笔持仓（v8: 增加 shares 和均价追踪，支持复利）"""
    def __init__(self, stock_code, entry_date, entry_price, entry_weight, entry_weight_raw, stock_type, entry_action,
                 invested_amount):
        self.stock_code = stock_code
        self.entry_date = entry_date
        self.entry_price = entry_price
        self.entry_weight = entry_weight          # 实际建仓比例（可能被仓位上限压缩）
        self.entry_weight_raw = entry_weight_raw  # 原始建仓比例（未被压缩）
        self.invested_amount = invested_amount     # 实际投入金额（元）
        
        # A股规则：股数必须为100的整数倍
        shares_raw = invested_amount / entry_price
        self.shares = round(shares_raw / 100) * 100  # 取整到100股
        if self.shares < 100:
            # 投入金额不足以买入1手，标记为0（由调用方过滤）
            self.shares = 0
        
        # 实际投入金额（按取整后的股数重新计算）
        actual_invested = self.shares * entry_price
        self.invested_amount = actual_invested
        self.initial_shares = self.shares          # 建仓时的股数
        
        self.avg_cost = entry_price               # v8: 加权均价（用于后续加仓摊薄）
        self.total_invested = actual_invested      # v8: 累计投入（含加仓）
        self.total_recovered = 0                   # v8.1: 累计回收（含减仓+清仓+强制平仓）
        self.total_fees = 0                        # v9: 累计交易费用（佣金+印花税+过户费）
        self.total_slippage_cost = 0               # v9: 累计滑点损失
        
        # 逐笔成交记录
        self.filled_records = []
        
        # 最后交易日期（用于最小交易间隔检查）
        self.last_trade_date = entry_date
        
        # 保持兼容字段
        self.position_size = entry_weight
        self.initial_size = entry_weight
        self.stock_type = stock_type
        self.entry_action = entry_action
        self.exit_date = None
        self.exit_price = None
        self.exit_reason = ''
        self.risk_meta = {}  # v9: 风控触发时的上下文元数据（peak_nav, current_nav, drawdown等）
        self.bars_held = 0
        self.reduce_history = []
        self.add_history = []
        self.max_pnl = 0
        self.min_pnl = 0
        self.batch2_added = False
        self.batch3_added = False

    @property
    def pnl_pct(self):
        """v8.2: 每笔交易收益率（含减仓回收 — 与 nav_pnl_pct 一致）
        旧版只算 shares × exit_price，忽略减仓回收现金，导致亏损夸大 2-3 倍。
        """
        if self.exit_price is not None and self.total_invested > 0:
            return (self.total_recovered - self.total_invested) / self.total_invested * 100
        return 0

    @property
    def nav_pnl_pct(self):
        """v8.1: 考虑加减仓+减仓回收的综合收益率
        = (累计回收 - 累计投入) / 累计投入 × 100
        累计回收 = 减仓回收 + 清仓回收 + 强制平仓回收
        """
        if self.exit_price is not None and self.total_invested > 0:
            return (self.total_recovered - self.total_invested) / self.total_invested * 100
        return 0

    @property
    def is_closed(self):
        return self.exit_date is not None

    def current_value(self, current_price):
        """v8: 当前持仓市值"""
        return self.shares * current_price

    def unrealized_pnl(self, current_price):
        """v8: 未实现盈亏（金额）"""
        return self.shares * (current_price - self.avg_cost)


def _get_index_for_date(df, date, stock_code=None):
    """快速获取日期对应的索引（O(1)查找）
    
    优先使用预构建的日期→索引映射，回退到 searchsorted。
    
    Returns:
        int: 日期对应的索引，如果未找到返回 -1
    """
    ts = pd.Timestamp(date).normalize()
    if stock_code and stock_code in _date_idx_cache:
        return _date_idx_cache[stock_code].get(ts, -1)
    # 回退到 searchsorted
    idx = df['trade_time'].searchsorted(ts, side='right') - 1
    if idx >= 0 and idx < len(df):
        return idx
    return -1

def _get_next_open_price(df, date, stock_code=None):
    """获取指定日期的次日开盘价（T+1 开盘）。

    用途（⚠️ 原注释写「用于卖出操作」不准确，2026-09-19 更正）：
      - **建仓成交价**（始终走这里，见建仓分支）—— T 日收盘决策、T+1 开盘买入；
      - **期末强制平仓**在 next_open 模式下也用它；
      - ⛔ **常规卖出不走本函数**，一律走 `_try_sell_with_limit_check`
        （默认 `sell_price_mode='next_open'` = T+1 条件单，仅当 low<=X 才以 min(X,open)
        成交；`'same_close'` = 信号日触发阈值价；只有该函数遇到跌停无法成交时，
        才降级到本函数取开盘价）。

    Returns:
        (next_open_price, next_date): 次日开盘价和次日日期，如果没有次日数据则返回(None, None)
    """
    ts = pd.Timestamp(date).normalize()
    if stock_code and stock_code in _date_idx_cache:
        date_idx = _date_idx_cache[stock_code].get(ts, -1)
        next_idx = date_idx + 1 if date_idx >= 0 else -1
    else:
        next_idx = df['trade_time'].searchsorted(ts, side='right')
    if next_idx < 0 or next_idx >= len(df):
        return None, None
    return float(df.iloc[next_idx]['open']), df.iloc[next_idx]['trade_time']

def _is_limit_up(df, date, stock_code=None, stock_name=''):
    """检查指定日期是否涨停（根据股票类型动态调整阈值）

    涨停判断：当日开盘价 = 最高价 = 收盘价（封板），且涨幅 >= 阈值 - 0.2%（误差容差）

    Returns:
        bool: 是否涨停
    """
    idx = _get_index_for_date(df, date, stock_code)
    if idx < 1:
        return False
    
    today = df.iloc[idx]
    prev = df.iloc[idx - 1]
    
    if prev['close'] <= 0:
        return False
    
    limit_pct = get_limit_up_down_pct(stock_code or '', stock_name)
    threshold = limit_pct - 0.002
    change_pct = (today['close'] - prev['close']) / prev['close']
    is_limit_up = (change_pct >= threshold) and (abs(today['open'] - today['high']) < 0.01)
    
    return is_limit_up

def _is_limit_down(df, date, stock_code=None, stock_name=''):
    """检查指定日期是否跌停（根据股票类型动态调整阈值）
    
    Returns:
        bool: 是否跌停
    """
    idx = _get_index_for_date(df, date, stock_code)
    if idx < 1:
        return False
    
    today = df.iloc[idx]
    prev = df.iloc[idx - 1]
    
    if prev['close'] <= 0:
        return False
    
    limit_pct = get_limit_up_down_pct(stock_code or '', stock_name)
    threshold = -(limit_pct - 0.002)
    change_pct = (today['close'] - prev['close']) / prev['close']
    is_limit_down = (change_pct <= threshold) and (abs(today['open'] - today['low']) < 0.01)
    
    return is_limit_down


def _try_sell_with_limit_check(df, date, max_retry_days=10, stock_code=None,
                               trigger_price=None):
    """T+1 起逐日尝试卖出（跌停延后 + 可选「条件单」触发校验）。

    两种模式（由 trigger_price 区分）：

      * ``trigger_price=None`` —— **无条件**卖出：找到第一个非跌停日，以该日**开盘价**成交。
        用于 `same_close` 模式的跌停降级（信号已经成立，只是当天卖不出去）。

      * ``trigger_price=X`` —— **条件单**卖出（向下触发，2026-09-20 起为 `next_open` 模式默认）：
        实盘卖出是「挂止损单等价格跌破 X」，不是「次日开盘无条件市价清仓」。
          - 当日 **`low <= X`** → 成交价 = **`min(X, open)`**
            （跳空低开时按开盘价成交 = 穿透，⛔ 不允许假装还能卖在 X）；
          - 当日 **未触及** → **该日不成交**，返回 None（主循环下一日会按新数据重算 X 再判）；
          - 跌停日 → 顺延（跌停无法成交）。
        实测（438 笔真实卖出）：条件单模型成交率约 95%，其余 5% 实盘根本不会触发；
        而「无条件」两种口径都是 100% 成交，会把不该卖的强行卖掉。

    Args:
        df: 股票K线数据
        date: 决策日期（当日收盘后做出卖出决定）
        max_retry_days: 最大延后天数（默认10）
        stock_code: 股票代码（用于快速日期索引查找）
        trigger_price: 条件单阈值 X；None = 无条件以开盘价成交

    Returns:
        (sell_price, sell_date, was_delayed, delay_days, forced_on_limit):
            sell_price: 实际卖出价（不含滑点，滑点由调用方处理）；**None = 未成交**
            sell_date: 实际卖出日期
            was_delayed: 是否因跌停延后
            delay_days: 延后天数
            forced_on_limit: 是否在跌停日强制卖出
    """
    ts = pd.Timestamp(date).normalize()
    if stock_code and stock_code in _date_idx_cache:
        date_idx = _date_idx_cache[stock_code].get(ts, -1)
        if date_idx < 0:
            # 缓存未命中（停牌/数据缺失），回退到 searchsorted
            base_idx = df['trade_time'].searchsorted(ts, side='right')
        else:
            base_idx = date_idx + 1
    else:
        base_idx = df['trade_time'].searchsorted(ts, side='right')

    try:
        _X = float(trigger_price) if trigger_price is not None else None
        if _X is not None and _X <= 0:
            _X = None          # 无效阈值 → 退化为无条件（不因脏数据卡死卖出）
    except (TypeError, ValueError):
        _X = None

    for delay in range(max_retry_days + 1):
        check_idx = base_idx + delay
        if check_idx >= len(df):
            # 数据不足，无法延后
            break
        bar = df.iloc[check_idx]
        sell_date_candidate = bar['trade_time']
        if _is_limit_down(df, sell_date_candidate):
            continue          # 跌停卖不出 → 顺延
        if _X is None:
            # 无条件：以开盘价卖出（跌停降级 / 旧行为）
            return (float(bar['open']), sell_date_candidate, delay > 0, delay, False)
        # 条件单：仅当当日盘中真正跌破 X 才算触发
        if float(bar['low']) <= _X:
            # 跳空低开（open < X）时按 open 成交 —— 穿透，不给虚假的好价
            sell_price = min(_X, float(bar['open']))
            return (sell_price, sell_date_candidate, delay > 0, delay, False)
        # 未触及 → 本日不成交（由主循环次日重算阈值后再判）
        return (None, None, False, 0, False)

    # 连续跌停超过max_retry_days天，强制按跌停日收盘价折价3%卖出
    forced_idx = base_idx + max_retry_days
    if forced_idx < len(df):
        forced_date = df.iloc[forced_idx]['trade_time']
        forced_close = float(df.iloc[forced_idx]['close'])
        forced_price = forced_close * 0.97  # 跌停折价3%
        return (forced_price, forced_date, True, max_retry_days, True)

    # 数据不足，返回None表示无法卖出
    return (None, None, False, 0, False)


def _try_add_conditional(df, date, trigger_price, stock_code=None):
    """加仓**条件单**（向上突破）：T+1 若 `high >= X` 则以 `max(X, open)` 成交。

    实盘加仓挂的是「突破单：涨破 X 就买」，不是「次日开盘无条件市价买」。方向与卖出相反
    （卖出是向下破位，见 `_try_sell_with_limit_check`）。

    规则：
      - 涨停 → 买不到（无卖方挂单），不成交；
      - `high >= X` → 成交价 = **`max(X, open)`**（跳空高开时按开盘价 = 穿透，⛔ 不给虚低价）；
      - `high < X` → 当日未突破，**不成交**（返回 None，由主循环次日重算阈值后再判）；
      - `trigger_price` 为空/无效 → 退化为次日开盘市价（保持旧行为，不因脏数据卡死加仓）。

    Returns:
        (price, date)：成交价与实际成交日期；**未触发返回 (None, None)**。
    """
    ts = pd.Timestamp(date).normalize()
    if stock_code and stock_code in _date_idx_cache:
        _di = _date_idx_cache[stock_code].get(ts, -1)
        base_idx = (_di + 1) if _di >= 0 else int(
            df['trade_time'].searchsorted(ts, side='right'))
    else:
        base_idx = int(df['trade_time'].searchsorted(ts, side='right'))
    if base_idx >= len(df):
        return (None, None)
    bar = df.iloc[base_idx]
    if _is_limit_up(df, bar['trade_time'], stock_code=stock_code):
        return (None, None)          # 涨停买不到

    try:
        _X = float(trigger_price) if trigger_price is not None else None
        if _X is not None and _X <= 0:
            _X = None                # 无效阈值 → 退化为市价
    except (TypeError, ValueError):
        _X = None

    if _X is None:
        return (float(bar['open']), bar['trade_time'])
    if float(bar['high']) >= _X:
        # 跳空高开（open > X）时按 open 成交 —— 穿透
        return (max(_X, float(bar['open'])), bar['trade_time'])
    return (None, None)


_STOCK_NAME_MAP = None


def _load_stock_name_map():
    """{代码(小写，带 sh/sz 前缀): 名称}，供回测产物 CSV/JSON 显示股票名称。

    优先用 `engine.state.code_to_name`（data_layer 启动时已从 cache/stock_list.json 载入），
    为空则直接读 `cache/stock_list.json` 的 `code_to_name`。
    取不到返回 {} —— 调用方回退空串，绝不因名称缺失而中断导出。
    """
    global _STOCK_NAME_MAP
    if _STOCK_NAME_MAP is not None:
        return _STOCK_NAME_MAP
    m = {}
    try:
        from engine.state import state
        if getattr(state, 'code_to_name', None):
            m = dict(state.code_to_name)
    except Exception:
        pass
    if not m:
        try:
            import json as _json
            p = os.path.join(get_app_dir(), 'cache', 'stock_list.json')
            if os.path.exists(p):
                with open(p, 'r', encoding='utf-8') as f:
                    d = _json.load(f)
                v = d.get('code_to_name')
                if isinstance(v, dict):
                    m = dict(v)
        except Exception:
            pass
    _STOCK_NAME_MAP = m
    return m


def _stock_name(code):
    """查股票名称；查不到返回空串（不抛异常、不阻断导出）。"""
    try:
        if not code:
            return ''
        nm = _load_stock_name_map()
        c = str(code).strip()
        return nm.get(c.lower()) or nm.get(c) or ''
    except Exception:
        return ''


def _pct_from_reason(reason):
    """从成交原因文本解析本次比例（引擎自生成格式：'加仓(10%)' / '减仓30%'）。

    解析不到返回 None —— 调用方以空串呈现，不猜测。
    """
    try:
        import re as _re2
        m = _re2.search(r'(\d+(?:\.\d+)?)\s*%', str(reason or ''))
        return float(m.group(1)) if m else None
    except Exception:
        return None


def _fill_stats(p):
    """汇总一笔持仓的成交股数：建仓 / 加仓 / 减仓 / 平仓（最后清仓）股数。"""
    fills = list(getattr(p, 'filled_records', []) or [])
    buy_shares = sum(float(r.get('shares') or 0) for r in fills if r.get('action') == 'buy')
    sell_shares = sum(float(r.get('shares') or 0) for r in fills if r.get('action') != 'buy')
    init_shares = float(getattr(p, 'initial_shares', 0) or 0)
    close_shares = float(getattr(p, 'shares', 0) or 0)   # 平仓时最后清仓的股数
    return {
        'init': init_shares,
        'add': max(0.0, buy_shares - init_shares),
        'reduce': max(0.0, sell_shares - close_shares),
        'close': close_shares,
        'buy_total': buy_shares,
        'sell_total': sell_shares,
    }


def _build_trade_rows(positions, name_fn):
    """【持仓明细】一笔持仓一行 —— 面向人工阅读（标的/日期/价格/数量/仓位/盈亏/退出原因）。

    刻意不含佣金、印花税、过户费、滑点、calc_check 等引擎内部列（那些留在 *_trades.json）。
    """
    rows = []
    for i, p in enumerate(positions or [], 1):
        if float(getattr(p, 'shares', 0) or 0) <= 0 or float(getattr(p, 'total_invested', 0) or 0) <= 0:
            continue
        s = _fill_stats(p)
        try:
            pnl = round(float(p.total_recovered) - float(p.total_invested), 2)
        except Exception:
            pnl = ''
        rows.append({
            '序号': i,
            '股票代码': p.stock_code,
            '股票名称': name_fn(p.stock_code),
            '类型': getattr(p, 'stock_type', ''),
            '建仓信号': getattr(p, 'entry_action', ''),
            '建仓日期': str(p.entry_date)[:10],
            '平仓日期': str(p.exit_date)[:10] if p.exit_date is not None else '',
            '持仓天数': int(getattr(p, 'bars_held', 0) or 0),
            '建仓价': round(float(p.entry_price), 2),
            '平仓价': round(float(p.exit_price), 2) if p.exit_price is not None else '',
            '建仓股数': round(s['init'], 2),
            '加仓股数': round(s['add'], 2),
            '减仓股数': round(s['reduce'], 2),
            '平仓股数': round(s['close'], 2),
            '信号仓位%': round(float(getattr(p, 'entry_weight_raw', 0) or 0) * 100, 2),
            '实际仓位%': round(float(getattr(p, 'entry_weight', 0) or 0) * 100, 2),
            '累计投入': round(float(p.total_invested), 2),
            '累计回收': round(float(p.total_recovered), 2),
            '交易费用': round(float(getattr(p, 'total_fees', 0) or 0), 2),
            '盈亏金额': pnl,
            '收益率%': round(float(getattr(p, 'pnl_pct', 0) or 0), 2),
            '持仓内最高浮盈%': round(float(getattr(p, 'max_pnl', 0) or 0), 2),
            '持仓内最大浮亏%': round(float(getattr(p, 'min_pnl', 0) or 0), 2),
            '加仓次数': len(getattr(p, 'add_history', []) or []),
            '减仓次数': len(getattr(p, 'reduce_history', []) or []),
            '退出原因': p.exit_reason or '',
        })
    return rows


def _build_fill_rows(positions, name_fn):
    """【交易流水】一笔成交一行 —— 买入/卖出动作 + 信号触发价 + 逐笔已实现盈亏。

    已实现盈亏按**移动平均成本**口径逐笔计算：卖出时 (成交价 − 当时持仓成本价) × 卖出股数，
    买入行留空（尚未实现）。最后一笔附该笔持仓的总收益率%与平仓原因。
    """
    rows = []
    seq = 0
    for p in positions or []:
        fills = list(getattr(p, 'filled_records', []) or [])
        if not fills:
            continue
        last_i = len(fills) - 1
        held = 0.0        # 当前持股
        cost = 0.0        # 当前持仓成本合计（按成交金额移动平均）
        first_buy = True
        for i, rec in enumerate(fills):
            seq += 1
            is_buy = (rec.get('action') == 'buy')
            sh = float(rec.get('shares') or 0)
            px = float(rec.get('price') or 0)
            raw = rec.get('price_raw', px)
            amt = rec.get('amount')
            amt = round(px * sh, 2) if amt is None else round(float(amt), 2)
            reason = str(rec.get('reason') or '')
            if is_buy:
                if first_buy:
                    signal = getattr(p, 'entry_action', '') or '建仓'
                    ratio = round(float(getattr(p, 'entry_weight', 0) or 0) * 100, 2)
                    first_buy = False
                else:
                    signal = '加仓'
                    _r = _pct_from_reason(reason)
                    ratio = round(_r, 2) if _r is not None else ''
                realized, realized_pct = '', ''
                cost += amt
                held += sh
            else:
                avg = (cost / held) if held > 0 else 0.0
                realized = round((px - avg) * sh, 2)
                realized_pct = round((px / avg - 1) * 100, 2) if avg > 0 else ''
                ratio = round(sh / held * 100, 2) if held > 0 else ''
                signal = reason or '卖出'
                cost -= avg * sh
                held -= sh
            rows.append({
                '序号': seq,
                '股票代码': p.stock_code,
                '股票名称': name_fn(p.stock_code),
                '方向': '买入' if is_buy else '卖出',
                '日期': str(rec.get('date') or '')[:10],
                '触发信号': signal,
                '信号触发价': round(float(raw or 0), 2),
                '成交价': round(px, 2),
                '成交数量': round(sh, 2),
                '成交金额': amt,
                '本次比例%': ratio,
                '已实现盈亏': realized,
                '已实现盈亏%': realized_pct,
                '平仓盈亏%': round(float(getattr(p, 'pnl_pct', 0) or 0), 2) if i == last_i else '',
                '平仓原因': (p.exit_reason or '') if i == last_i else '',
            })
    return rows


def _build_equity_rows(equity_curve, initial_capital):
    """【净值曲线】一日一行（累计收益率按初始资金折算）。"""
    rows = []
    for e in (equity_curve or []):
        try:
            nav = float(e.get('nav') or 0)
        except Exception:
            nav = 0.0
        rows.append({
            '日期': str(e.get('date') or '')[:10],
            '净值': round(nav, 2),
            '当日收益率%': round(float(e.get('daily_return') or 0) * 100, 4),
            '累计收益率%': round((nav / initial_capital - 1) * 100, 4) if initial_capital else '',
            '回撤%': round(float(e.get('drawdown') or 0) * 100, 4),
            '持仓市值': round(float(e.get('positions_value') or 0), 2),
            '现金': round(float(e.get('cash') or 0), 2),
            '持仓数': int(e.get('open_positions') or 0),
        })
    return rows


def _safe_write_csv(df, path, what, errors=None):
    """原子写 CSV（先写 .tmp 再 os.replace），且**失败绝不静默**。返回 (ok, err)。

    ⛔ 为什么必须显式处理：Windows 上目标 CSV 若正被 Excel / WPS / 记事本 打开，
       `to_csv` 会抛 `PermissionError`（Errno 13）。旧代码只 `print('[warn] ...')` 就
       放过了 —— 旧文件因此残留，UI 又把旧文件当成本次结果画出来。
       2026-09-19 实锤：22:56 那次回测三个 CSV 全部写失败（json 成功），UI 显示的是
       21:11 的旧净值曲线（+46%）配新报告的指标卡（+3.21%），用户看到"收益曲线和
       实际结果对不上"。所以这里把失败项收集到 `errors`（最终进 report.json），
       并打印含操作指引的告警。

    ⚠️ 用 `.tmp` + `os.replace` 而非直接覆盖：避免写到一半崩溃留下半截文件
       （半截 CSV 比旧文件更危险 —— 它"看起来是新的"）。
    """
    tmp = path + '.tmp'
    try:
        df.to_csv(tmp, index=False, encoding='utf-8-sig')
        os.replace(tmp, path)
        return True, ''
    except Exception as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        err = f'{type(e).__name__}: {e}'
        locked = isinstance(e, PermissionError) or getattr(e, 'errno', None) == 13
        hint = (f'（{os.path.basename(path)} 可能正被 Excel/WPS/记事本 打开占用，'
                f'请关闭后重跑回测；该文件当前仍是上次的旧结果）') if locked else ''
        print(f'[warn] {what}导出失败: {err} {hint}')
        if errors is not None:
            errors.append({'file': os.path.basename(path), 'what': what,
                           'error': err, 'locked': bool(locked)})
        return False, err


def _stock_has_bar_on(df, date_ts, stock_code=None):
    """该股在 date_ts（已 normalize 的当日零点）是否有 K 线。

    ⛔ 交易日循环走的是「全市场交易日并集」（`_get_trading_dates` 汇总所有股票的 bar 日期），
    个股在停牌 / 数据缺口日没有 bar。若不判断，索引会落到上一根旧 bar，用过期数据产生
    建仓/平仓信号，并把成交登记到该股本就不交易的日期上 —— 表现为「卖出日期早于买入日期」
    （2026-09-19 实锤：sh688072 在 store 中 2026-06-26 与 07-13 之间有 10 个交易日缺口，
    却出现 07-07 建仓、07-09 止损；止损价 832.00 = 06-26 收盘，而建仓成交价 890.99 =
    07-13 开盘，于是「7/9 卖」早于「7/13 买」）。

    实现上**不依赖 trade_time 的时间部分**（兼容归一前 15:00 / 00:00 两种口径）：
    先取 searchsorted(ts, 'left')（第一根 ≥ 当日零点的 bar），再按日期比较，而不是精确相等。
    （正常情况下数据入口已 `normalize_daily_dates` 归一到零点。）
    """
    if stock_code and stock_code in _date_idx_cache:
        return _date_idx_cache[stock_code].get(date_ts) is not None
    try:
        i = int(df['trade_time'].searchsorted(date_ts, side='left'))
        return i < len(df) and pd.Timestamp(df.iloc[i]['trade_time']).normalize() == date_ts
    except Exception:
        return True  # 判断失败时不做限制（保持旧行为），避免误杀正常分析


def _st_compute_full_analysis(df, date, stock_code, up_ratio):
    """计算完整分析（评分+分类+入场+减仓+加仓建议）

    新增前置守卫：当日无 K 线直接返回 None（见 `_stock_has_bar_on`）。
    worker 侧尤其危险：`_st_init_worker` 不构建 `_date_idx_cache`，必然走 searchsorted 分支。
    """
    ts = pd.Timestamp(date).normalize()
    if not _stock_has_bar_on(df, ts, stock_code):
        return None  # 当日停牌/无数据 → 不分析、不交易、不记日期
    # 单一确定性口径：收盘截止 = **含当日 bar**（trade_time 已在数据入口 `_validate_qfq`
    # 归一到零点，故 searchsorted 的语义在全库一致）= 用户确认的「T 日收盘决策、T+1 开盘成交」。
    # ⛔ 刻意不再走 `_date_idx_cache` 分支：cache 分支含当日 bar，而 15:00 口径下的
    #    searchsorted 不含，cache 命中与否会让结果静默位移一天（历史遗留隐性差异）。
    idx = int(df['trade_time'].searchsorted(ts, side='right'))
    if idx < 60:
        return None
    df_slice = df.iloc[:idx]

    closes = df_slice['close'].tolist()
    volumes = df_slice['volume'].tolist()
    highs = df_slice['high'].tolist()
    lows = df_slice['low'].tolist()
    opens = df_slice['open'].tolist()
    dates_list = df_slice['trade_time'].tolist()
    n = len(closes)

    data_list = [{'close': c, 'high': h, 'low': l, 'volume': v, 'date': d, 'open': o}
                 for c, h, l, v, d, o in zip(closes, highs, lows, volumes, dates_list, opens)]

    try:
        precomputed = _precomputed_cache.get(stock_code, {})
        macd_hists = precomputed.get('macd_hists') or MACDCalculator.calc_hist_series(closes)
        rsi_hist = precomputed.get('rsi_hist') or \
            [RSICalculator.calc_wilder(closes[:i + 1], 14) for i in range(n)]
        accel = calc_macd_acceleration_from_hist(macd_hists)

        stock_score, final_result, tech, market, adx_result, _ = scoring_core.compute_stock_score(
            closes, volumes, highs, lows, opens, data_list, up_ratio, rsi_hist, accel)
        final_score = final_result['final_score']

        entry_result = UnifiedEntryLogic.check_entry(
            stock_score=stock_score,
            final_score=final_score,
            tech=tech,
            market=market,
            up_ratio=up_ratio,
            latest_price=closes[-1],
            adx_state=adx_result
        )

        ctx = type('obj', (object,), {
            'final_score': final_score,
            'stock_score': stock_score,
            'market': market,
            'tech': tech,
            'latest_price': closes[-1],
            'up_ratio': up_ratio,
            'has_real_veto': entry_result.get('has_real_veto', False),
            'adx_state': adx_result
        })()

        classification = StockClassifier.classify(ctx)

        # 计算星级（信号强度）
        signal_rating = SignalRating.calculate(
            stock_score=stock_score,
            tech=tech,
            up_ratio=up_ratio,
            stock_type=classification.get('type', 'unknown'),
            final_score=final_score
        )

        return {
            'stock_code': stock_code,
            'stock_score': stock_score,
            'final_score': final_score,
            'entry_action': entry_result['action'],
            'entry_position': entry_result['position'],
            'stock_type': classification.get('type', 'unknown'),
            'type_label': classification.get('type_label', '未知'),
            'signal_rating': signal_rating,
            'date': date,
            'price': closes[-1],
            'tech': tech,
            'market': market,
            'adx_result': adx_result,
            'ctx': ctx,
            'final_result': final_result,
            'data_list': data_list,
        }
    except Exception as e:
        return None



# ========== 策略回测多核：信号扫描 worker（进程池，模块级可 pickle）==========

def _st_init_worker(pool_codes=None, qcfg=None):
    """每个 worker 进程启动载入自己的离线缓存（与 parallel_utils.init_worker 一致）。

    pool_codes: 若提供，worker 只验证+载入池内股票，冷启动验证循环从全市场降到池内数量。
    qcfg: 主进程注入到 quant_config._cache 的当前激活方案配置。spawn 出的 worker 是独立
          进程，不会继承主进程的 quant_config._cache（默认为 None）。若不传，worker 内
          score_calculator_v2 调 quant_config.require_config(quant_config._safe_cfg(), ...)
          会因 _cache=None 抛 ConfigIncompleteError，被 compute_full_analysis 的 except 静默
          吞掉 → analysis 恒为 None → 0 候选 → 0 交易（串行分支在主进程跑、_cache 已注入故正常）。
          此处把主进程 _cache 透传给 worker，使并行分支与串行分支用完全相同的方案配置。

    关键修复：worker 是独立 spawn 进程，必须自行从磁盘重载缓存。此前仅调用
    backtest._load_disk_cache（只读 cache/kline/qfq_daily_{code}.pkl），在「共享 store 为空、
    真实数据在旧扫描 cache/kline/kline_{hash}.pkl」的机器上会全部落空 → 并行分支候选为 0 →
    0 交易（串行分支因主进程已预载而正常）。此处缺口用 KLineFetcher.load_qfq_daily
    （含 kline_{hash}.pkl 回退）补齐，与主进程 _fetch_stock_data 口径一致。
    """
    # 透传方案配置：并行 worker 是独立进程，必须显式注入 quant_config._cache
    if qcfg is not None:
        from engine import quant_config as _qc
        _qc._cache = qcfg

    if not _stock_data_cache:
        from . import backtest
        from engine.data_layer import KLineFetcher, SCAN_KLINE_DAYS
        # 先按 code 维度统一 store 载入（qfq_daily_{code}.pkl）
        backtest._load_disk_cache(pool=pool_codes)
        # 缺口再用 load_qfq_daily（含旧扫描 kline_{hash}.pkl 回退）补齐，与主进程口径一致
        if pool_codes:
            for sc in pool_codes:
                if sc in _stock_data_cache:
                    continue
                try:
                    df = KLineFetcher.load_qfq_daily(sc, SCAN_KLINE_DAYS, offline=True)
                except Exception:
                    df = None
                if df is None or df.empty:
                    continue
                df = df.copy()
                df['trade_time'] = pd.to_datetime(df['trade_time'])
                df = df.sort_values('trade_time').reset_index(drop=True)
                df = df[df['close'] > 0]
                if df.empty:
                    continue
                ok, vdf, reason = _validate_qfq(df)
                if reason == 'unadjusted':
                    continue
                _stock_data_cache[sc] = vdf if vdf is not None else df


def _st_warmup_noop():
    """空任务：仅用于等待 executor 全部 worker 完成 initializer（离线缓存加载）。

    在进入逐日循环前逐个等待，确保「进程池就绪」提示出现时缓存确已加载完毕，
    避免首个扫描日静默等待 worker 就绪被误判为卡死。
    """
    return True


# ============================================================
# 分层选股（2026-09-27 用户决策：各等级都取样，横向比性价比，而非只买最高分）
#
# 原逻辑：候选按「星级降序 → 因子预期分降序」全局排序后从第一名往下买，
#   资金/总仓位上限用尽即止 → 高分档（强势/标准）吃掉几乎全部资金，
#   低分档（待确认/博反弹/弱势）样本量趋近 0，报告里「按股票类型分类」形同虚设。
# 现逻辑：按「股票类型档」(stock_type，由因子预期分 thresholds 切分) 分桶，
#   档内按因子预期分降序，档间轮转（先各档第 1 名、再各档第 2 名…），
#   每档每次扫描有建仓配额 → 保证各等级都能取到样本。
# 星级门槛（原 min_stars：强市≥4★ / 中性≥3★ / 弱市≥2★）在新口径下取消——
#   它的作用正是「只留高分」，与分层取样目标直接冲突；星级仍记录，仅用于统计。
# ============================================================
TIER_ORDER = ('strong', 'standard', 'test', 'pending', 'weak_rebound',
              'weak', 'panic_avoid', 'panic_rebound', 'unknown')
TIER_LABELS = {
    'strong': '强势股', 'standard': '标准股', 'test': '试探股', 'pending': '待确认',
    'weak_rebound': '博反弹', 'weak': '弱势股', 'panic_avoid': '恐慌回避',
    'panic_rebound': '恐慌反转', 'unknown': '未知',
}
# 每个类型档每次扫描的建仓配额（0 = 不限）。防止某档在资金充足时一次吃满、
# 把后面轮转到的档挤掉；也把单次扫描的新增持仓控制在可预期范围内。
TIER_QUOTA_PER_SCAN = 2
# 同一类型档的**同时持仓**上限（0 = 不分档限制）。取消星级门槛后候选量级上升，
# 而低分档信号仓位小（试探 8% / 观望 3%），若只看总仓位上限，同样资金会摊成
# 几十笔迷你仓 → 回测逐日估值成本暴涨。按档限 2 只可把总持仓压在 ~18 只以内，
# 同时保证每一档都有样本。这是本次分层改造新增的约束（非原有风控）。
TIER_MAX_HOLDINGS = 2
# 可选的最低星级门槛：**0 = 不过滤**（分层取样默认，各等级都要有样本）。
# 改回 2/3/4 即恢复原「只留高星」的行为（原逻辑按市场强弱动态取 4/3/2）。
# 注意：只要 >0，低分档样本就会再次被系统性排除，分档对比表将失去意义。
TIER_MIN_STARS = 0


def _tier_of(stock_type):
    """把 stock_type 归一到 TIER_ORDER 内的档位（缺失/未知 → 'unknown'）。"""
    t = str(stock_type or '').strip()
    return t if t in TIER_LABELS else 'unknown'


def _star_level_from_score(stars):
    """评级分 → 星级数（1~5），与 SignalRating 的展示口径一致，仅用于统计。"""
    try:
        s = float(stars or 0)
    except (TypeError, ValueError):
        return 1
    if s >= 0.75:
        return 5
    if s >= 0.62:
        return 4
    if s >= 0.48:
        return 3
    if s >= 0.35:
        return 2
    return 1


def _order_candidates_by_tier(candidates):
    """分层轮转排序：档内按因子预期分降序，档间按 rank 交叉合并。

    轮转（而非全局排序）是分层取样的关键：资金按「各档第1名 → 各档第2名 …」
    的顺序分配，低分档不会因为全局排序靠后而永远轮不到资金。
    """
    buckets = {}
    for a in candidates or []:
        buckets.setdefault(_tier_of(a.get('stock_type')), []).append(a)
    for _k, v in buckets.items():
        v.sort(key=lambda x: (-float(x.get('final_score') or 0),
                              0 if is_priority_stock(x.get('stock_code', '')) else 1))
    ordered = []
    max_len = max((len(v) for v in buckets.values()), default=0)
    for rank in range(max_len):
        for t in TIER_ORDER:
            v = buckets.get(t) or []
            if rank < len(v):
                ordered.append(v[rank])
    return ordered


def _st_scan_worker(shard, date, up_ratio, held_codes, failed_stocks):
    """进程池 worker：扫描一个股票分片，返回命中的候选（已裁剪字段，减小 IPC）。

    分层选股口径（2026-09-27）：不再按市场强弱设星级门槛，只保留建仓动作过滤
    （关注建仓/博反弹），各类型档都进候选池；实际建仓由主循环按档轮转 + 每档
    配额决定，保证低分档也能取到样本。
    """
    out = []
    for stock_code, raw_code, board in shard:
        if stock_code in held_codes or stock_code in failed_stocks:
            continue
        df = _stock_data_cache.get(stock_code)
        if df is None:
            continue
        a = _st_compute_full_analysis(df, date, stock_code, up_ratio)
        if a is None:
            continue
        if a['entry_action'] not in ('关注建仓', '博反弹'):
            continue

        # 星级默认仅作统计标签（分层选股口径）；TIER_MIN_STARS>0 时恢复过滤
        rating = a.get('signal_rating', {})
        stars = rating.get('score', 0)
        star_level = _star_level_from_score(stars)
        if TIER_MIN_STARS and star_level < TIER_MIN_STARS:
            continue

        out.append({
            'stock_code': a['stock_code'], 'stock_score': a['stock_score'],
            'final_score': a['final_score'], 'entry_action': a['entry_action'],
            'entry_position': a['entry_position'], 'stock_type': a['stock_type'],
            'type_label': a['type_label'], 'date': a['date'], 'price': a['price'],
            'up_ratio': up_ratio,
            'signal_rating_score': stars,
            'star_level': star_level,
        })
    return out


class StrategyBacktester:
    """策略回测器 v8 - 资金复利 + 净值跟踪"""

    MAX_POSITIONS = 10
    INITIAL_CAPITAL = 1_000_000
    SCAN_INTERVAL_DAYS = 5

    def __init__(self, start_date, end_date, max_stocks=None, full_market=False, return_years=1, n_workers=None, pool=None, offline_mode=False):
        # 风控参数（来自 quant_model.json 的 risk_params，可配置；缺配置用安全默认值）
        rp = quant_config.get_risk_params() or {}
        self.max_single_position = rp.get('max_single_position', 0.30)
        self.total_position_cap_pct = rp.get('total_position_cap_pct', 0.80)
        self.time_stop_days = rp.get('time_stop_days', 20)
        self.time_stop_min_profit_pct = rp.get('time_stop_min_profit_pct', 0)
        self.single_max_loss_pct = rp.get('single_max_loss_pct', 0)
        self.market_crash_pct = rp.get('market_crash_pct', 0)
        self.market_crash_index = rp.get('market_crash_index', 'sh000300') or 'sh000300'
        self.start_date = datetime.strptime(start_date, '%Y-%m-%d')
        self.end_date = datetime.strptime(end_date, '%Y-%m-%d')
        self.max_stocks = max_stocks
        self.return_years = return_years
        self.failed_stocks = []
        self.n_workers = n_workers
        
        if pool is not None:
            # 显式股票池参数（'148'|'full'|'watchlist'）优先于 full_market 布尔
            self.stock_pool = resolve_stock_pool(pool)
            _pool_label = {'148': '上证50+创业50+科创50', 'full': '全市场', 'watchlist': '自选股列表'}.get(pool, pool)
            print(f"  股票池[{_pool_label}]: {len(self.stock_pool)}只股票")
        elif full_market:
            self.stock_pool = get_full_market_pool()
            print(f"  全市场模式: {len(self.stock_pool)}只股票")
        else:
            self.stock_pool = get_stock_pool()
        if max_stocks:
            self.stock_pool = self.stock_pool[:max_stocks]
        
        # v8: 资金账户
        self.cash = self.INITIAL_CAPITAL
        self.peak_nav = self.INITIAL_CAPITAL  # 追踪历史最高净值
        self._current_date = None              # v8.1: 当前处理日期，供 nav/估值使用
        
        self.positions = []
        self.closed_positions = []
        self.equity_curve = []   # v8: [{date, nav, cash, positions_value, daily_return}, ...]
        self.trade_log = []
        self.daily_equity = []   # 保持兼容
        self.risk_events = []    # 风控事件日志
        self._market_stats = {}  # 向量化预计算的市场统计（up_ratio, market_return）
        # 输出路径（工具模式可加前缀，避免覆盖规范产物；None=使用原固定文件名）
        self.equity_csv_path = None
        self.trades_json_path = None
        self.report_json_path = None
        self.cancel_event = None  # 由 runner 注入的 threading.Event，用于中止
        self.equity_cb = None     # 由 runner 注入：接收实时净值点（UI 边跑边画曲线）
        self.export_errors = []   # 产物写盘失败清单（如 CSV 被 Excel 占用），随 report 上报
        
        # 最小交易间隔和价格剧变突破阈值
        self.min_trade_interval = 3  # 天
        self.price_breakthrough_threshold = 0.05  # 5% 价格剧变允许突破间隔
        
        self.offline_mode = offline_mode

        # 卖出/加仓执行价模式（2026-09-19 用户决策：默认改为 next_open，与买入对称）：
        #   'next_open'  = **T+1 开盘价（默认）** —— T 日收盘决策、次日开盘成交，买卖口径一致，
        #                  更贴近实盘，且消除「触发阈值价 = 当日最高」的乐观偏差；
        #   'same_close' = 信号日收盘/触发阈值价（旧默认，消除隔夜跳空但偏乐观）。
        # ⛔ 建仓始终用 T+1 开盘价（见建仓分支），不受此开关影响。
        self.sell_price_mode = rp.get('sell_price_mode', 'next_open') or 'next_open'

    def _use_same_day_close(self):
        """是否使用信号触发价（=引擎触发阈值/信号日收盘）作为卖出/加仓执行价。"""
        return self.sell_price_mode == 'same_close'

    @staticmethod
    def _extract_reduce_trigger_price(reduce_result):
        """从减仓引擎结果提取触发阈值作为成交基准价（无价格阈值则返回 None）。

        引擎在 triggered_tiers 中已给出每档触发的价格水平（如支撑/止损线），
        这就是信号「触发的价格」，比收盘价更贴近实际触发点。
        """
        tt = (reduce_result or {}).get('triggered_tiers') or []
        for t in tt:
            p = t.get('price', 0)
            if p and p > 0:
                return float(p)
        return None

    @staticmethod
    def _extract_add_trigger_price(add_result):
        """从加仓引擎结果提取触发阈值作为成交基准价（无价格阈值则返回 None）。

        注意：AddEngine.suggest_add 返回字段名为 triggered_tiers（非 triggered）。
        """
        tt = (add_result or {}).get('triggered_tiers') or []
        for t in tt:
            p = t.get('price', 0)
            if p and p > 0:
                return float(p)
        return None

    @staticmethod
    def _clamp_price_to_bar(df, date, price, stock_code=None):
        """把成交价夹到当日 K 线的 [low, high] 区间内（防御：阈值价异常时兜底）。

        ⛔ 背景（2026-09-19 实锤）：减仓/加仓引擎给出的「触发价」是阈值价，一旦阈值算错
        （如 TDX 全历史把 2021 年高位当持仓最高点 → sh688277 触发价 141.09，而当日真实
        区间仅 17.62~18.33），直接当成交价会产出与个股真实走势严重不符的交易记录。
        同一交易日内成交价不可能超出当日最高/最低价，故据实夹取。
        """
        try:
            if price is None:
                return price
            ts = pd.Timestamp(date).normalize()
            idx = None
            if stock_code and stock_code in _date_idx_cache:
                idx = _date_idx_cache[stock_code].get(ts)
            if idx is None:
                # ⚠️ 不能用 searchsorted(ts,'right')-1：当 trade_time 带 15:00 时间戳时
                #    会取到「前一交易日」的 bar（历史 store 两种口径混用过，故此处按日期核对，
                #    不依赖时间部分——即使未来又混入非零点时间戳也正确）。
                i = int(df['trade_time'].searchsorted(ts, side='left'))
                if i < len(df) and pd.Timestamp(df.iloc[i]['trade_time']).normalize() == ts:
                    idx = i
            if idx is None or idx < 0 or idx >= len(df):
                return price
            bar = df.iloc[idx]
            lo = float(bar['low']); hi = float(bar['high'])
            if lo > 0 and hi >= lo:
                return min(max(float(price), lo), hi)
        except Exception:
            pass
        return price

    def _clamp_add_invested(self, pos, add_ratio, current_nav):
        """加仓金额风控夹紧：仅受「单只上限」约束，豁免「总仓位上限」。

        返回允许投入的加仓金额（>=0）。

        设计（用户决策 2026-08-07）：总仓位上限只限制初始建仓，加仓可动用预留现金，
        否则总仓到上限后加仓信号永远无法成交（"有信号没钱加"）。单只上限仍生效，
        防止单票无限堆高。现金约束在调用点另行校验（actual_add_invested + fee <= cash）。
        """
        if current_nav <= 0:
            return 0.0
        add_invested = pos.total_invested * add_ratio
        projected_single = (pos.total_invested + add_invested) / current_nav
        if projected_single > self.max_single_position:
            add_invested = max(0.0, self.max_single_position * current_nav - pos.total_invested)
        return add_invested

    @property
    def nav(self):
        """v8.1: 当前总净值 = 现金 + 持仓当日市值（依赖 self._current_date）"""
        pos_value = sum(p.current_value(self._get_position_price(p, self._current_date))
                       for p in self.positions)
        return self.cash + pos_value

    def _get_position_price(self, pos, date):
        """获取持仓在指定日期的收盘价（v8.1 修复：必须按日期查找，不能永远取最后一根K线）"""
        if date is None:
            return pos.avg_cost
        df = _stock_data_cache.get(pos.stock_code)
        if df is None:
            return pos.avg_cost
        idx = _get_index_for_date(df, date, pos.stock_code)
        if idx >= 0:
            return float(df.iloc[idx]['close'])
        return pos.avg_cost

    def preload_all_data(self):
        """预加载所有股票数据（前复权 store 优先，缺数按当前源联网补；v8: 请求更多天数以支持多年）。

        性能优化：缺失票用线程池并发拉取（网络IO密集）。由于 to_load 中的股票
        初始均不在缓存且互不重叠，各线程写 _stock_data_cache 不同键、_precompute_series
        在主线程串行调用，无全局缓存并发写竞态。原串行 time.sleep(0.3~0.5) 已移除。
        """
        print(f"\n{'='*70}")
        print(f"预加载 {len(self.stock_pool)} 只股票日线数据（数据源：腾讯前复权优先）...")
        print(f"{'='*70}")

        if not _stock_data_cache:
            _load_disk_cache(pool=[sc for sc, _, _ in self.stock_pool], offline=self.offline_mode)

        to_load = [(sc, rc, bd) for sc, rc, bd in self.stock_pool if sc not in _stock_data_cache]
        if not to_load:
            print(f"  所有 {len(self.stock_pool)} 只股票数据已就绪（全部命中缓存）")
            return len(_stock_data_cache)

        # 离线模式：未命中缓存的股票直接跳过，不联网抓取
        if self.offline_mode:
            skipped = [sc for sc, _, _ in to_load]
            self.failed_stocks.extend(skipped)
            hit = len(self.stock_pool) - len(skipped)
            print(f"  【离线模式】跳过 {len(skipped)} 只未命中缓存的股票，不联网抓取")
            print(f"  离线模式: 缓存命中 {hit}/{len(self.stock_pool)} 只")
            if hit == 0:
                _explain_empty_cache()
            return len(_stock_data_cache)

        print(f"  缓存命中: {len(_stock_data_cache)} | 需加载: {len(to_load)}")

        success = 0
        failed = []
        import os
        import concurrent.futures as _cf
        n_threads = min(4, max(1, (os.cpu_count() or 4)))
        with _cf.ThreadPoolExecutor(max_workers=n_threads) as ex:
            futs = {ex.submit(self._fetch_stock_data, sc): sc for sc, _, _ in to_load}
            done_cnt = 0
            for fut in _cf.as_completed(futs):
                sc = futs[fut]
                try:
                    df = fut.result()
                except Exception:
                    df = None
                if df is not None:
                    self._precompute_series(sc, df)   # 主线程写 _precomputed_cache，串行安全
                    success += 1
                else:
                    failed.append(sc)
                done_cnt += 1
                if done_cnt % 50 == 0 or done_cnt == len(to_load):
                    print(f"  进度: {done_cnt}/{len(to_load)} | 成功: {success} | 失败: {len(failed)}")
                    if done_cnt % 200 == 0:
                        _save_disk_cache()

        _save_disk_cache()
        if failed:
            print(f"  失败股票 ({len(failed)}只): {failed[:10]}{'...' if len(failed)>10 else ''}")
            self.failed_stocks = failed

        # —— 预构建日期→索引映射（性能优化）——
        # 将 searchsorted O(log n) 查找改为字典 O(1) 查找
        self._build_date_idx_cache()

        print(f"  完成: 本次成功 {success}/{len(to_load)} | 缓存总计: {len(_stock_data_cache)}只")
        return len(_stock_data_cache)

    def _build_date_idx_cache(self):
        """预构建所有股票的日期→索引映射，将 searchsorted O(log n) 改为字典 O(1)"""
        t0 = time.time()
        for stock_code in _stock_data_cache:
            df = _stock_data_cache[stock_code]
            dates = df['trade_time'].values
            date_to_idx = {pd.Timestamp(d).normalize(): i for i, d in enumerate(dates)}
            _date_idx_cache[stock_code] = date_to_idx
        elapsed = time.time() - t0
        print(f"  日期索引映射预构建完成: {len(_date_idx_cache)} 只股票，耗时 {elapsed:.2f}s")

    def _fetch_stock_data(self, stock_code):
        """v8: 单只股票日线获取（【前复权】优先，禁止未复权兜底）。

        流程：
          1) 缓存命中 → 先过 _validate_qfq 质量门禁；
             - persistent-step(未复权除权) → 删除该缓存键，落到下方重拉；
             - V-shape(单点坏点) → 用修复版替换缓存条目并返回；
             - ok/too_short → 直接返回。
          2) 腾讯前复权(qfq) 成功 → 过 _validate_qfq；
             - persistent-step → 不缓存、不降级，该股票被排除(返回 None)；
             - V-shape → 缓存修复版并返回；
             - ok → 缓存并返回。
          3) 腾讯 qfq 失败(异常/空/被拒) → 直接返回 None（该股票排除出回测），
             不再降级到新浪未复权原始价（避免除权伪像污染净值）。
        注：桌面实时分析走 engine/data_layer，与此处回测取数无关。
        """
        if stock_code in _stock_data_cache:
            cached = _stock_data_cache[stock_code]
            ok, vdf, reason = _validate_qfq(cached)
            if reason == 'unadjusted':
                _stock_data_cache.pop(stock_code, None)   # 脏数据删键，走下方重拉
            elif reason == 'vshape_repaired':
                _stock_data_cache[stock_code] = vdf
                return vdf
            elif ok:
                return cached
            else:
                _stock_data_cache.pop(stock_code, None)

        # 1) 统一前复权缓存（与全市场扫描共用 cache/kline/qfq_daily_{code}.pkl）。
        #    修复(2026-08-16)：offline 必须跟随 self.offline_mode，之前硬编码 True
        #    导致「有网环境下缓存外股票全跳过」，出现 成功 0/3538 的批量失败。
        #    online 模式下 load_qfq_daily 内部会：命中不足 → _fetch_full 联网
        #    （日K 第一手通达信自算复权，失败回退腾讯 fqkline）→ 落盘 qfq_daily
        #    统一 store；下次再扫即可直接命中（长期加速）。
        try:
            df = KLineFetcher.load_qfq_daily(stock_code, SCAN_KLINE_DAYS, offline=self.offline_mode)
            if df is not None and not df.empty:
                df = df.copy()
                df['trade_time'] = pd.to_datetime(df['trade_time'])
                df = df.sort_values('trade_time').reset_index(drop=True)
                df = df[df['close'] > 0]
                if not df.empty:
                    ok, vdf, reason = _validate_qfq(df)
                    if reason == 'unadjusted':
                        return None          # 未复权脏数据 → 拒绝、不缓存、不降级
                    if ok:
                        _stock_data_cache[stock_code] = vdf if vdf is not None else df
                        return vdf if vdf is not None else df
        except Exception:
            pass

        # 2) 仍缺失（代码已退市/接口真的失败）→ 排除该股票，不再降级新浪未复权
        return None

    def _precompute_series(self, stock_code, df):
        closes = df['close'].tolist()
        _precomputed_cache[stock_code] = {
            'rsi_hist': calc_rsi_series_fast(closes, 14),
            'macd_hists': calc_macd_hist_series_fast(closes),
        }

    def _get_trading_dates(self):
        """获取回测区间内所有交易日"""
        all_dates = set()
        for stock_code, _, _ in self.stock_pool:
            if stock_code in self.failed_stocks:
                continue
            df = _stock_data_cache.get(stock_code)
            if df is None:
                continue
            mask = (df['trade_time'] >= self.start_date) & (df['trade_time'] <= self.end_date)
            for d in df.loc[mask, 'trade_time']:
                all_dates.add(d.normalize())
        return sorted(all_dates)

    def calculate_up_ratio(self, date):
        """计算当日市场上涨比例（已废弃，保留兼容）"""
        # 已由 _precompute_market_stats 向量化预计算
        return self._market_stats.get(pd.Timestamp(date).normalize(), (0.5, 0.0))[0]

    def calculate_market_return(self, date):
        """计算当日市场平均涨跌幅（已废弃，保留兼容）"""
        # 已由 _precompute_market_stats 向量化预计算
        return self._market_stats.get(pd.Timestamp(date).normalize(), (0.5, 0.0))[1]

    def _precompute_market_stats(self, trading_dates, index_returns=None):
        """向量化预计算所有交易日的市场统计（up_ratio, market_return）

        性能优化：将 O(交易日数 × 股票池大小) 的逐日循环改为
        O(股票池大小) 的向量化计算，一次性完成。

        返回: {date_normalized: (up_ratio, market_return)}
        """
        t0 = time.time()

        # 交易日集合（normalized）
        date_set = {pd.Timestamp(d).normalize() for d in trading_dates}

        # 收集所有股票在回测区间的日收益率
        # 使用 dict 保存: {date: [ret1, ret2, ...]} 和 {date: [up1, up2, ...]}
        daily_returns = {}  # date -> list of returns
        daily_ups = {}      # date -> list of 1/0 (涨/跌)

        for stock_code, _, _ in self.stock_pool:
            if stock_code in self.failed_stocks:
                continue
            df = _stock_data_cache.get(stock_code)
            if df is None or len(df) < 2:
                continue

            # 向量化计算日收益率
            closes = df['close'].values
            dates = df['trade_time'].values

            # 计算涨跌幅（与前一日比较）
            # ret[i] = (close[i] - close[i-1]) / close[i-1]
            rets = np.zeros(len(df))
            rets[1:] = (closes[1:] - closes[:-1]) / np.where(closes[:-1] > 0, closes[:-1], 1.0)
            rets[0] = 0.0

            # 判断涨跌（涨=1，跌或平=0）
            ups = (rets > 0).astype(int)

            # 按日期归集
            for i, d in enumerate(dates):
                d_norm = pd.Timestamp(d).normalize()
                if d_norm in date_set and i > 0:  # 跳过第一天（无前日数据）
                    if d_norm not in daily_returns:
                        daily_returns[d_norm] = []
                        daily_ups[d_norm] = []
                    daily_returns[d_norm].append(rets[i])
                    daily_ups[d_norm].append(ups[i])

        # 汇总每个交易日的市场统计
        result = {}
        for d in trading_dates:
            d_norm = pd.Timestamp(d).normalize()
            if d_norm in daily_returns and daily_returns[d_norm]:
                # up_ratio = 涨的股票数 / 总股票数
                ups_list = daily_ups[d_norm]
                up_ratio = sum(ups_list) / len(ups_list) if ups_list else 0.5

                # market_return：若配置了熔断基准指数且能取到，则用指数当日涨跌幅；
                # 否则回退股票池均值（保证熔断判定与用户配置口径一致）
                rets_list = daily_returns[d_norm]
                if index_returns and d_norm in index_returns:
                    market_return = index_returns[d_norm]
                else:
                    market_return = float(np.mean(rets_list)) if rets_list else 0.0

                result[d_norm] = (up_ratio, market_return)
            else:
                result[d_norm] = (0.5, 0.0)  # 默认值

        return result

    def compute_full_analysis(self, df, date, stock_code, up_ratio):
        return _st_compute_full_analysis(df, date, stock_code, up_ratio)

    def _scan_candidates(self, date, up_ratio):
        """串行扫描：返回命中候选（已含 up_ratio），与多核分支产出逐位一致。

        分层选股口径（2026-09-27）：不设星级门槛，只保留建仓动作过滤，
        各类型档都进候选池；建仓顺序与每档配额由主循环 `_order_candidates_by_tier`
        决定（与 worker 分支保持一致）。
        """
        candidates = []
        for stock_code, raw_code, board in self.stock_pool:
            if any(p.stock_code == stock_code for p in self.positions):
                continue
            if stock_code in self.failed_stocks:
                continue
            df = _stock_data_cache.get(stock_code)
            if df is None:
                continue
            analysis = self.compute_full_analysis(df, date, stock_code, up_ratio)
            if analysis is None:
                continue
            analysis['up_ratio'] = up_ratio
            if analysis['entry_action'] not in ['关注建仓', '博反弹']:
                continue

            # 星级默认仅作统计标签（分层选股口径）；TIER_MIN_STARS>0 时恢复过滤
            rating = analysis.get('signal_rating', {})
            stars = rating.get('score', 0)
            star_level = _star_level_from_score(stars)
            if TIER_MIN_STARS and star_level < TIER_MIN_STARS:
                continue

            analysis['signal_rating_score'] = stars
            analysis['star_level'] = star_level
            candidates.append(analysis)
        return candidates

    def _scan_candidates_parallel(self, date, up_ratio, n_workers, executor=None):
        """多核扫描：仅并行计算 analysis（状态外循环仍串行）。

        executor 为 None 时自建临时进程池（兼容旧调用/测试）；传入 run() 预创建的
        常驻进程池时复用之，避免每个扫描日重建进程池并重复加载 241MB 缓存。
        """
        from concurrent.futures import ProcessPoolExecutor, as_completed
        import multiprocessing as _mp
        held = {p.stock_code for p in self.positions}
        failed = set(self.failed_stocks)
        pool = [(sc, rc, bd) for sc, rc, bd in self.stock_pool]
        chunk = max(50, len(pool) // (n_workers * 6))
        shards = [pool[i:i + chunk] for i in range(0, len(pool), chunk)]
        candidates = []

        def _collect(ex):
            futs = [ex.submit(_st_scan_worker, sh, date, up_ratio, held, failed)
                     for sh in shards]
            # 分片级心跳：单日 5220 只票的多核扫描可能耗时数十秒到数分钟，
            # 期间主循环无输出会让 UI 显示「一动不动」（2026-09-19 实锤）。每 ~10% 分片报一次，
            # 既证明扫描在推进，也能在真卡死时定位卡在哪个分片。
            _n = len(futs)
            _step = max(1, _n // 10)
            _done = 0
            for fut in as_completed(futs):
                try:
                    rows = fut.result()
                except Exception:
                    rows = []
                candidates.extend(rows)
                _done += 1
                if _done % _step == 0 or _done == _n:
                    print(f"    [扫描] {str(date)[:10]} 分片 {_done}/{_n} · 累计候选 {len(candidates)}")

        try:
            if executor is None:
                _ctx = _mp.get_context('spawn')
                from engine import quant_config as _qc_scan
                _qcfg_scan = _qc_scan._cache
                with ProcessPoolExecutor(max_workers=n_workers, mp_context=_ctx,
                                        initializer=_st_init_worker,
                                        initargs=([sc for sc, _, _ in self.stock_pool], _qcfg_scan)) as ex:
                    _collect(ex)
            else:
                _collect(executor)
        except Exception as e:
            print(f"    [策略多核] 汇总异常，回退串行: {e}")
            return self._scan_candidates(date, up_ratio)
        return candidates

    def check_reduce(self, analysis, position):
        """检查减仓信号（动态评估股票类型）"""
        from engine.stock_classifier import StockClassifier
        
        ctx = type('obj', (object,), {
            'latest_price': analysis['price'],
            'entry_price': position.entry_price,
            'pnl_pct': (analysis['price'] - position.avg_cost) / position.avg_cost * 100,
            'tech': analysis['tech'],
            'market': analysis['market'],
            'data_list': analysis['data_list'],
            'up_ratio': analysis.get('up_ratio', 0.5),
            'has_position': True,
            'final_score': analysis.get('final_score', 0),
            'stock_score': analysis.get('stock_score', 0),
            'has_real_veto': analysis.get('has_real_veto', False),
            'adx_state': analysis.get('adx_result', {}),
        })()
        
        classification = StockClassifier.classify(ctx)
        ctx.stock_type = classification['type']
        
        return ReduceEngine.suggest_reduce(ctx)

    def check_add(self, analysis, position):
        """检查加仓信号（动态评估股票类型）"""
        from engine.stock_classifier import StockClassifier
        
        ctx = type('obj', (object,), {
            'final_score': analysis['final_score'],
            'tech': analysis['tech'],
            'market': analysis['market'],
            'latest_price': analysis['price'],
            'data_list': analysis.get('data_list'),
            'confidence_factor': analysis['final_result'].get('confidence_factor', 1.0),
            'entry_price': position.entry_price,
            'has_position': True,
            'stock_score': analysis.get('stock_score', 0),
            'up_ratio': analysis.get('up_ratio', 0.5),
            'has_real_veto': analysis.get('has_real_veto', False),
            'adx_state': analysis.get('adx_result', {}),
        })()
        
        classification = StockClassifier.classify(ctx)
        ctx.stock_type = classification['type']
        
        return AddEngine.suggest_add(ctx)

    def _record_equity(self, date, trading_dates_count=0):
        """v8.1: 记录每日净值到权益曲线（复用 self.nav，避免重复计算）"""
        # 确保 _current_date 正确（在 _record_equity 可能被 run 末尾独立调用）
        if self._current_date is None:
            self._current_date = date
        
        current_nav = self.nav
        pos_value = current_nav - self.cash
        prev_nav = self.equity_curve[-1]['nav'] if self.equity_curve else self.INITIAL_CAPITAL
        daily_ret = (current_nav / prev_nav - 1) if prev_nav > 0 else 0
        
        self.peak_nav = max(self.peak_nav, current_nav)
        
        self.equity_curve.append({
            'date': str(date)[:10],
            'nav': round(current_nav, 2),
            'cash': round(self.cash, 2),
            'positions_value': round(pos_value, 2),
            'daily_return': round(daily_ret, 6),
            'drawdown': round(1 - current_nav / self.peak_nav, 6) if self.peak_nav > 0 else 0,
            'open_positions': len(self.positions),
        })

        # —— 实时净值点（供 UI 边跑边画曲线）——
        # ⛔ 仅供过程展示：终态曲线仍以 equity.csv（run 结束时一次性落盘）为准，
        #    前端在 done 时用产物全量替换，故此处节流（每 5 个交易日 1 点 + 首点）。
        self._equity_pts = getattr(self, '_equity_pts', 0) + 1
        _eq_cb = getattr(self, 'equity_cb', None)
        if _eq_cb is not None and (self._equity_pts == 1 or self._equity_pts % 5 == 0):
            try:
                _eq_cb({
                    'd': str(date)[:10],
                    'nav': round(current_nav, 2),
                    'dd': round(1 - current_nav / self.peak_nav, 6) if self.peak_nav > 0 else 0,
                    'dr': round(daily_ret, 6),
                    'pos': len(self.positions),
                })
            except Exception:
                pass  # 实时点失败绝不影响回测主流程

    def _save_checkpoint(self, day_num):
        try:
            os.makedirs(CHECKPOINT_DIR, exist_ok=True)
            checkpoint = {
                'day': day_num,
                'closed_count': len(self.closed_positions),
                'nav': round(self.nav, 2),
                'cash': round(self.cash, 2),
                'trade_log': self.trade_log[-200:],
            }
            path = os.path.join(CHECKPOINT_DIR, 'strategy_checkpoint.json')
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(checkpoint, f, ensure_ascii=False, default=str)
        except Exception as e:
            print(f"  检查点保存失败: {e}")

    def run(self, n_workers=None):
        """主回测流程（v8: 净值驱动仓位）。仅使用现有磁盘缓存（内置离线包），不联网刷新。"""
        trading_dates = self._get_trading_dates()
        if not trading_dates:
            print("无有效交易日！")
            if self.offline_mode:
                _explain_empty_cache()
            return None

        years_span = (self.end_date - self.start_date).days / 365.25

        print(f"\n{'='*70}")
        print(f"策略回测 v8: {self.start_date.strftime('%Y-%m-%d')} ~ {self.end_date.strftime('%Y-%m-%d')}")
        print(f"交易日数: {len(trading_dates)} | 股票池: {len(self.stock_pool)}只 | 跨度: {years_span:.1f}年")
        print(f"最大持仓: {self.MAX_POSITIONS}只 | 时间止损天数: {self.time_stop_days}天 | "
              f"单只最大仓位: {self.max_single_position*100:.0f}%")
        print(f"初始资金: {self.INITIAL_CAPITAL:,.0f} | 模式: 净值复利")
        try:
            from engine.machine_probe import describe_machine
            print(describe_machine())
        except Exception:
            pass
        print(f"{'='*70}")

        # 预热优先选股集合（上证50+创业50+科创50 核心指数池；仅缺失时告警，不打印加载条数避免误读为回测池）
        load_priority_members()

        start_time = time.time()

        # —— 向量化预计算市场统计（性能优化）——
        # 一次性完成所有交易日的 up_ratio 和 market_return 计算
        # 将 O(交易日数 × 股票池大小) 降为 O(股票池大小)
        # 熔断基准指数历史日收益（用于大盘熔断判定）；失败回退股票池均值
        index_returns = {}
        try:
            from engine.data_layer import IndexFetcher
            index_returns = IndexFetcher.get_daily_returns_history(self.market_crash_index, 1500)
        except Exception as _e:
            print(f"  [warn] 熔断基准指数历史获取失败，回退股票池均值：{_e}")
        self._market_stats = self._precompute_market_stats(trading_dates, index_returns)
        # 基准对照复用同一指数序列（默认沪深300），避免二次联网取数
        self._index_returns = index_returns

        # —— 进程池复用（性能优化）——
        # 提到交易日循环外只建一次，worker 常驻，241MB 大盘缓存每 worker 仅加载一次。
        # 原实现每个扫描日都重建进程池并重新加载缓存，扫描日越多浪费越大。
        n_pool = len(self.stock_pool)
        n_workers_eff = (n_workers if n_workers is not None else self.n_workers) or _auto_n_workers()
        use_parallel = (n_workers_eff > 1 and n_pool >= 50)
        executor = None
        if use_parallel:
            import multiprocessing as _mp
            from concurrent.futures import ProcessPoolExecutor
            _ctx = _mp.get_context('spawn')
            # 把主进程已注入的当前方案配置透传给 worker（spawn 不继承内存态）
            from engine import quant_config as _qc_run
            _qcfg_run = _qc_run._cache
            print(f"  正在启动 {n_workers_eff} 个多核扫描 worker 并加载离线缓存"
                  f"（当前 store 约 212MB/5220 只，首次可能需数分钟，请勿中途关闭窗口）...")
            executor = ProcessPoolExecutor(max_workers=n_workers_eff, mp_context=_ctx,
                                           initializer=_st_init_worker,
                                           initargs=([sc for sc, _, _ in self.stock_pool], _qcfg_run))
            # 预热：显式等待全部 worker 完成 initializer（缓存加载）后再进入逐日循环。
            # 否则「进程池已创建」会先打印，首个扫描日却静默等 worker 就绪 → UI 显示一动不动被误判卡死
            # （2026-09-19 实证：首日扫描长时间无输出，进度停在 1/243 · 0%）。
            try:
                _warm = [executor.submit(_st_warmup_noop) for _ in range(n_workers_eff)]
                for _wf in _warm:
                    _wf.result()
            except Exception:
                pass
            print(f"  多核扫描进程池就绪（{n_workers_eff} workers，缓存已加载，常驻复用）")

        print(f"  开始逐日扫描：{len(trading_dates)} 个交易日 × {len(self.stock_pool)} 只候选"
              f"（每 {PROGRESS_EVERY_DAYS} 个交易日汇报一次进度，请勿中途关闭窗口）")

        for day_idx, date in enumerate(trading_dates):
            # 用户点击「停止」→ 在当前交易日结束后干净中止
            if self.cancel_event is not None and self.cancel_event.is_set():
                print("⏹ 已收到停止信号，回测中止。")
                if executor is not None:
                    executor.shutdown(wait=False)
                raise BacktestCancelled()
            self._current_date = date  # v8.1: 设定当前回测日期，供 nav/估值使用
            # —— 逐日进度汇报（成因见 PROGRESS_EVERY_DAYS 处注释）——
            # 本循环体此前完全静默，是「回测看着像卡死」的直接成因。首日/末日各报一次，
            # 中间每 PROGRESS_EVERY_DAYS 个交易日报一次，附带 ETA 供用户判断还要等多久。
            if (day_idx == 0 or (day_idx + 1) % PROGRESS_EVERY_DAYS == 0
                    or day_idx == len(trading_dates) - 1):
                _done = day_idx + 1
                _total = len(trading_dates)
                _elapsed = time.time() - start_time
                _eta = (_elapsed / _done) * (_total - _done) if _done else 0.0
                print(f"  [进度] {_done}/{_total} 交易日 · 截至 {date} · {_done * 100 // _total}%"
                      f" · 已用 {_elapsed:.0f}s · 剩余约 {_eta:.0f}s"
                      f" · 持仓 {len(self.positions)}")
            up_ratio = self.calculate_up_ratio(date)
            market_ret = self.calculate_market_return(date)
            # 大盘熔断：指数单日跌幅超阈值 → 当日暂停开新仓（已有持仓继续管理）
            market_crash_today = (self.market_crash_pct > 0
                                  and market_ret <= -self.market_crash_pct)
            ts = pd.Timestamp(date)

            # 检查现有持仓
            for pos in self.positions[:]:
                if pos.stock_code in self.failed_stocks:
                    continue
                df = _stock_data_cache.get(pos.stock_code)
                if df is None:
                    continue

                analysis = self.compute_full_analysis(df, date, pos.stock_code, up_ratio)
                if analysis is None:
                    continue

                analysis['up_ratio'] = up_ratio
                current_price = analysis['price']
                pos.bars_held += 1

                pnl = (current_price - pos.avg_cost) / pos.avg_cost * 100
                pos.max_pnl = max(pos.max_pnl, pnl)
                pos.min_pnl = min(pos.min_pnl, pnl)

                # --- 减仓检查 ---
                reduce_result = self.check_reduce(analysis, pos)
                reduce_ratio = reduce_result.get('reduce_ratio', 0)

                if reduce_ratio >= 1.0:
                    # 全部清仓
                    # T+1限制：买入当日不能卖出（bars_held >= 2 表示至少持有2个交易日）
                    if pos.bars_held < 2:
                        continue
                    
                    # 卖出执行（两种模式，详见 _try_sell_with_limit_check 文档）：
                    #   same_close → 信号日触发阈值价成交（T 日跌停时降级为顺延开盘价）
                    #   next_open  → T+1 条件单：仅当盘中跌破触发价 X 才成交，成交价 min(X, open)
                    # 触发价优先用减仓引擎给出的阈值，取不到则为 None（退化为无条件卖出）
                    _tp_cond = self._extract_reduce_trigger_price(reduce_result)
                    if self._use_same_day_close() and not _is_limit_down(df, date):
                        # 触发价夹到当日 K 线区间内，避免异常阈值价被当成成交价
                        sell_price_raw = (self._clamp_price_to_bar(df, date, _tp_cond, pos.stock_code)
                                          if _tp_cond is not None else current_price)
                        sell_date = date
                        was_delayed, delay_days, forced_on_limit = False, 0, False
                    else:
                        # T 日跌停 → 无条件顺延（信号已成立、当天卖不出）；
                        # next_open 模式 → 传触发价做条件校验，未跌破则不成交、继续持有
                        _tp_arg = None if _is_limit_down(df, date) else _tp_cond
                        sell_price_raw, sell_date, was_delayed, delay_days, forced_on_limit = _try_sell_with_limit_check(
                            df, date, stock_code=pos.stock_code, trigger_price=_tp_arg)
                        if sell_price_raw is None:
                            continue
                    
                    # 应用动态滑点（卖出价变低，基于个股流动性）
                    dyn_slippage = calc_dynamic_slippage(pos.stock_code, date)
                    raw_sell_price = sell_price_raw
                    sell_price = apply_slippage(raw_sell_price, is_buy=False, slippage_rate=dyn_slippage)
                    
                    pos.exit_date = sell_date
                    pos.exit_price = sell_price
                    exit_detail = f"清仓: {reduce_result.get('action', '')}"
                    if was_delayed:
                        exit_detail += f'[跌停延后{delay_days}天]'
                    if forced_on_limit:
                        exit_detail += '[跌停折价3%强制卖出]'
                    pos.exit_reason = exit_detail
                    # v8: 现金回收
                    recovered = pos.shares * sell_price
                    cost = calculate_trading_cost(recovered, pos.stock_code, is_buy=False)
                    fee = cost['total']
                    slippage_cost = recovered * dyn_slippage
                    recovered_net = recovered - fee
                    self.cash += recovered_net
                    pos.total_recovered += recovered_net  # v8.1: 记录回收
                    pos.total_fees += fee
                    pos.total_slippage_cost += slippage_cost
                    
                    # 记录成交记录
                    pos.filled_records.append({
                        'date': str(sell_date)[:10],
                        'action': 'sell',
                        'reason': f"清仓{'[跌停延后'+str(delay_days)+'天]' if was_delayed else ''}{'[跌停折价3%强制卖出]' if forced_on_limit else ''}",
                        'price': sell_price,
                        'price_raw': raw_sell_price,
                        'shares': pos.shares,
                        'amount': recovered,
                        'commission': cost['commission'],
                        'stamp_duty': cost['stamp_duty'],
                        'transfer_fee': cost['transfer_fee'],
                        'slippage_cost': slippage_cost,
                        'total_fee': fee,
                    })
                    pos.last_trade_date = sell_date
                    
                    pos.reduce_history.append({
                        'date': str(sell_date)[:10], 'action': '清仓',
                        'price': sell_price,
                        'reason': reduce_result.get('reason_summary', '')
                    })
                    self.closed_positions.append(pos)
                    self.positions.remove(pos)
                    self.trade_log.append({
                        'date': str(date)[:10], 'stock': pos.stock_code,
                        'action': '清仓', 'price': sell_price,
                        'shares': pos.shares,
                        'pnl_pct': pos.nav_pnl_pct,
                        'nav': round(self.nav, 0),
                        'reason': pos.exit_reason
                    })
                    continue

                elif reduce_ratio > 0:
                    # v8: 部分减仓 → 卖出部分持股，现金增加
                    # T+1限制：买入当日不能卖出
                    if pos.bars_held < 2:
                        continue
                    
                    # 最小交易间隔检查（价格剧变允许突破）
                    if pos.last_trade_date:
                        days_since_last = (date - pos.last_trade_date).days
                        if days_since_last < self.min_trade_interval:
                            # 检查价格是否剧变突破间隔限制
                            price_ratio = current_price / pos.avg_cost
                            if abs(price_ratio - 1) < self.price_breakthrough_threshold:
                                continue
                    
                    # 卖出执行（两种模式，详见 _try_sell_with_limit_check 文档）：
                    #   same_close → 信号日触发阈值价成交（T 日跌停时降级为顺延开盘价）
                    #   next_open  → T+1 条件单：仅当盘中跌破触发价 X 才成交，成交价 min(X, open)
                    # 触发价优先用减仓引擎给出的阈值，取不到则为 None（退化为无条件卖出）
                    _tp_cond = self._extract_reduce_trigger_price(reduce_result)
                    if self._use_same_day_close() and not _is_limit_down(df, date):
                        # 触发价夹到当日 K 线区间内，避免异常阈值价被当成成交价
                        sell_price_raw = (self._clamp_price_to_bar(df, date, _tp_cond, pos.stock_code)
                                          if _tp_cond is not None else current_price)
                        sell_date = date
                        was_delayed, delay_days, forced_on_limit = False, 0, False
                    else:
                        # T 日跌停 → 无条件顺延（信号已成立、当天卖不出）；
                        # next_open 模式 → 传触发价做条件校验，未跌破则不成交、继续持有
                        _tp_arg = None if _is_limit_down(df, date) else _tp_cond
                        sell_price_raw, sell_date, was_delayed, delay_days, forced_on_limit = _try_sell_with_limit_check(
                            df, date, stock_code=pos.stock_code, trigger_price=_tp_arg)
                        if sell_price_raw is None:
                            continue
                    
                    # 应用动态滑点（卖出价变低，基于个股流动性）
                    dyn_slippage_reduce = calc_dynamic_slippage(pos.stock_code, date)
                    raw_sell_price = sell_price_raw
                    sell_price = apply_slippage(raw_sell_price, is_buy=False, slippage_rate=dyn_slippage_reduce)
                    
                    # A股规则：卖出股数必须为100的整数倍（允许零股清仓）
                    reduce_shares_raw = pos.shares * reduce_ratio
                    reduce_shares = adjust_sell_lot(pos.shares, reduce_shares_raw)
                    
                    if reduce_shares > 0:
                        recovered = reduce_shares * sell_price
                        cost = calculate_trading_cost(recovered, pos.stock_code, is_buy=False)
                        fee = cost['total']
                        slippage_cost = recovered * dyn_slippage_reduce
                        recovered_net = recovered - fee
                        self.cash += recovered_net
                        pos.total_recovered += recovered_net  # v8.1: 记录减仓回收
                        pos.total_fees += fee
                        pos.total_slippage_cost += slippage_cost
                        pos.shares -= reduce_shares
                        
                        # 记录成交记录
                        pos.filled_records.append({
                            'date': str(sell_date)[:10],
                            'action': 'sell',
                            'reason': f'减仓{reduce_ratio*100:.0f}%{"[跌停延后"+str(delay_days)+"天]" if was_delayed else ""}{"[跌停折价3%强制卖出]" if forced_on_limit else ""}',
                            'price': sell_price,
                            'price_raw': raw_sell_price,
                            'shares': reduce_shares,
                            'amount': recovered,
                            'commission': cost['commission'],
                            'stamp_duty': cost['stamp_duty'],
                            'transfer_fee': cost['transfer_fee'],
                            'slippage_cost': slippage_cost,
                            'total_fee': fee,
                        })
                        pos.last_trade_date = sell_date
                        
                        pos.reduce_history.append({
                            'date': str(sell_date)[:10],
                            'action': f'减仓{reduce_ratio*100:.0f}%',
                            'price': sell_price,
                            'reason': reduce_result.get('reason_summary', '')
                        })
                        self.trade_log.append({
                            'date': str(date)[:10], 'stock': pos.stock_code,
                            'action': f'减仓{reduce_ratio*100:.0f}%', 'price': sell_price,
                            'shares': reduce_shares,
                            'pnl_pct': pnl,
                            'nav': round(self.nav, 0),
                            'reason': reduce_result.get('reason_summary', '')
                        })

                # --- 加仓检查 ---
                if pnl > 0 and not pos.batch2_added:
                    add_result = self.check_add(analysis, pos)
                    if add_result.get('can_add') and add_result.get('add_ratio', 0) > 0:
                        # 最小交易间隔检查（价格剧变允许突破）
                        if pos.last_trade_date:
                            days_since_last = (date - pos.last_trade_date).days
                            if days_since_last < self.min_trade_interval:
                                # 检查价格是否剧变突破间隔限制
                                price_ratio = current_price / pos.avg_cost
                                if abs(price_ratio - 1) < self.price_breakthrough_threshold:
                                    continue
                        
                        # 加仓执行价（两种模式）：
                        #   same_close → T 日触发阈值价成交（夹进当日区间）
                        #   next_open  → T+1 条件单（**向上突破**）：仅当盘中涨破 X 才成交，
                        #                成交价 = max(X, open)（跳空高开按开盘价穿透）
                        tp_add = self._extract_add_trigger_price(add_result)
                        if self._use_same_day_close():
                            # 触发价夹到当日 K 线区间内（买入价不低于当日最低价）
                            raw_add_price = (self._clamp_price_to_bar(df, date, tp_add, pos.stock_code)
                                             if tp_add is not None else current_price)
                            next_date = date

                            # T 日涨停无法加仓（无卖方挂单），跳过
                            if _is_limit_up(df, date, stock_code=pos.stock_code):
                                continue
                        else:
                            # next_open：T+1 突破条件单；未涨破 X（或涨停买不到）则本次不加仓
                            _ap, _adate = _try_add_conditional(df, date, tp_add,
                                                               stock_code=pos.stock_code)
                            if _ap is None:
                                continue
                            raw_add_price, next_date = _ap, _adate

                        # 应用动态滑点（买入价变高，基于个股流动性）
                        dyn_slippage_add = calc_dynamic_slippage(pos.stock_code, date)
                        add_price = apply_slippage(raw_add_price, is_buy=True, slippage_rate=dyn_slippage_add)

                        # 加仓 = 当前持仓金额 × add_ratio 比例
                        current_nav = self.nav
                        add_ratio = add_result['add_ratio']
                        add_invested = self._clamp_add_invested(pos, add_ratio, current_nav)
                        
                        # A股规则：加仓股数必须为100的整数倍（不足100则不加仓）
                        add_shares_raw = add_invested / add_price
                        add_shares = adjust_buy_lot(add_shares_raw)
                        
                        if add_shares <= 0:
                            continue  # 风控压缩后无加仓空间，跳过
                        
                        # 重新计算实际加仓金额（基于取整后的股数和含滑点的成交价）
                        actual_add_invested = add_shares * add_price
                        cost = calculate_trading_cost(actual_add_invested, pos.stock_code, is_buy=True)
                        fee = cost['total']
                        slippage_cost = actual_add_invested * dyn_slippage_add
                        
                        if self.cash >= actual_add_invested + fee:
                            self.cash -= (actual_add_invested + fee)
                            pos.total_invested += actual_add_invested
                            pos.total_fees += fee
                            pos.total_slippage_cost += slippage_cost
                            # 更新加权均价（使用含滑点的成交价）
                            old_value = pos.shares * pos.avg_cost
                            pos.shares += add_shares
                            pos.avg_cost = (old_value + actual_add_invested) / pos.shares if pos.shares > 0 else add_price
                            pos.batch2_added = True
                            # v8.1: 同步更新 position_size；initial_size 保持原始建仓比例不变
                            new_total_weight = pos.total_invested / current_nav if current_nav > 0 else pos.position_size
                            pos.position_size = new_total_weight
                            
                            # 记录成交记录
                            pos.filled_records.append({
                                'date': str(next_date)[:10],
                                'action': 'buy',
                                'reason': f'加仓({add_ratio*100:.0f}%)',
                                'price': add_price,
                                'price_raw': raw_add_price,
                                'shares': add_shares,
                                'amount': actual_add_invested,
                                'commission': cost['commission'],
                                'stamp_duty': cost['stamp_duty'],
                                'transfer_fee': cost['transfer_fee'],
                                'slippage_cost': slippage_cost,
                                'total_fee': fee,
                            })
                            pos.last_trade_date = next_date
                            
                            pos.add_history.append({
                                'date': str(date)[:10],
                                'action': f'加仓({add_ratio*100:.0f}%)',
                                'price': add_price,
                                'reason': add_result.get('reason', ''),
                                'amount': round(actual_add_invested, 0),
                            })
                            self.trade_log.append({
                                'date': str(date)[:10], 'stock': pos.stock_code,
                                'action': '加仓', 'price': add_price,
                                'shares': add_shares,
                                'pnl_pct': pnl,
                                'nav': round(self.nav, 0),
                                'reason': add_result.get('reason', '')
                            })
                        else:
                            # v8.1: 现金不足时记录日志，避免静默跳过误导分析
                            self.trade_log.append({
                                'date': str(date)[:10], 'stock': pos.stock_code,
                                'action': '加仓失败(现金不足)', 'price': add_price,
                                'pnl_pct': pnl,
                                'nav': round(self.nav, 0),
                                'reason': f'需{actual_add_invested+fee:,.0f} 现金仅{self.cash:,.0f}'
                            })

                # --- 持仓级风控硬止损（独立于用户减仓档位）---
                risk_exit = None
                if self.single_max_loss_pct > 0 and pnl <= -self.single_max_loss_pct * 100:
                    risk_exit = ('单笔最大亏损止损', f'个股浮亏{pnl:.1f}%≥止损线{-self.single_max_loss_pct*100:.0f}%')

                if risk_exit:
                    # T+1限制：买入当日不能卖出
                    if pos.bars_held < 2:
                        continue

                    # 单笔止损用**静态止损线** X = 建仓均价 × (1 − 单笔最大亏损%)：
                    # 它不依赖当日数据 ⇒ 实盘是「挂单持续有效、盘中跌破即成交」，**不该等 T+1**。
                    # 实测代价：等一天会让实际离场从设定 −6% 恶化到 −10%~−15%（科创板 ±20%
                    # 涨跌幅下跳空尤其致命，2026-09-20 实测 39 笔里最深 −17.6%）。
                    # 成交价 = min(X, T 日开盘)（跳空低开穿透）。
                    # 无未来函数：X 静态、判定只用当日 bar 内部的 low，不依赖收盘后才知道的信息。
                    _sl_price = (pos.avg_cost * (1 - self.single_max_loss_pct)
                                 if getattr(pos, 'avg_cost', 0) and pos.avg_cost > 0 else None)
                    _t_idx = _get_index_for_date(df, date, pos.stock_code)
                    _hit_sl = (_sl_price is not None and _t_idx >= 0
                               and float(df.iloc[_t_idx]['low']) <= _sl_price)
                    if _hit_sl:
                        # 当日盘中已跌破止损线 → 以 min(X, 开盘价) 成交
                        sell_price_raw = min(_sl_price, float(df.iloc[_t_idx]['open']))
                        sell_date = date
                        was_delayed, delay_days, forced_on_limit = False, 0, False
                    elif self._use_same_day_close() and not _is_limit_down(df, date):
                        # 信号触发价（T 日收盘 = analysis['price'] = current_price）卖出
                        sell_price_raw = current_price
                        sell_date = date
                        was_delayed, delay_days, forced_on_limit = False, 0, False
                    else:
                        # T 日跌停 → 无条件顺延；next_open → 条件单校验（盘中跌破 X 才成交）
                        _tp_arg = None if _is_limit_down(df, date) else _sl_price
                        sell_price_raw, sell_date, was_delayed, delay_days, forced_on_limit = _try_sell_with_limit_check(
                            df, date, stock_code=pos.stock_code, trigger_price=_tp_arg)
                        if sell_price_raw is None:
                            continue
                    
                    # 应用动态滑点（卖出价变低，基于个股流动性）
                    dyn_slippage_risk = calc_dynamic_slippage(pos.stock_code, date)
                    raw_sell_price = sell_price_raw
                    sell_price = apply_slippage(raw_sell_price, is_buy=False, slippage_rate=dyn_slippage_risk)
                    
                    action_name, reason = risk_exit
                    pos.exit_date = sell_date
                    pos.exit_price = sell_price
                    exit_detail = reason
                    if was_delayed:
                        exit_detail += f'[跌停延后{delay_days}天]'
                    if forced_on_limit:
                        exit_detail += '[跌停折价3%强制卖出]'
                    pos.exit_reason = exit_detail
                    recovered = pos.shares * sell_price
                    cost = calculate_trading_cost(recovered, pos.stock_code, is_buy=False)
                    fee = cost['total']
                    slippage_cost = recovered * dyn_slippage_risk
                    recovered_net = recovered - fee
                    self.cash += recovered_net
                    pos.total_recovered += recovered_net
                    pos.total_fees += fee
                    pos.total_slippage_cost += slippage_cost
                    
                    # 记录成交记录
                    pos.filled_records.append({
                        'date': str(sell_date)[:10],
                        'action': 'sell',
                        'reason': action_name + (f'[跌停延后{delay_days}天]' if was_delayed else '') + ('[跌停折价3%强制卖出]' if forced_on_limit else ''),
                        'price': sell_price,
                        'price_raw': raw_sell_price,
                        'shares': pos.shares,
                        'amount': recovered,
                        'commission': cost['commission'],
                        'stamp_duty': cost['stamp_duty'],
                        'transfer_fee': cost['transfer_fee'],
                        'slippage_cost': slippage_cost,
                        'total_fee': fee,
                    })
                    pos.last_trade_date = sell_date
                    
                    self.closed_positions.append(pos)
                    self.positions.remove(pos)
                    self.trade_log.append({
                        'date': str(date)[:10], 'stock': pos.stock_code,
                        'action': action_name, 'price': sell_price,
                        'shares': pos.shares,
                        'pnl_pct': pos.nav_pnl_pct,
                        'nav': round(self.nav, 0),
                        'reason': reason
                    })
                    continue

                # --- 时间止损（新语义：持仓够久 且 收益未达目标才离场）---
                # BUGFIX: nav_pnl_pct 依赖 exit_price（此时为 None→返回 0），不能用于判断。
                # 改用浮动综合收益率 = (减仓回收 + 当前市值 - 累计投入) / 累计投入 × 100
                if pos.total_invested > 0:
                    _float_recovered = pos.total_recovered + pos.shares * current_price
                    _float_pnl_pct = (_float_recovered - pos.total_invested) / pos.total_invested * 100
                else:
                    _float_pnl_pct = 0
                if pos.bars_held >= self.time_stop_days and _float_pnl_pct < self.time_stop_min_profit_pct * 100:
                    # T+1限制：买入当日不能卖出
                    if pos.bars_held < 2:
                        continue

                    # 时间止损是**时间条件**（无价格阈值）→ 没有可挂的条件单，
                    # next_open 下按 T+1 开盘市价卖出（不传 trigger_price）
                    if self._use_same_day_close() and not _is_limit_down(df, date):
                        # 信号触发价（T 日收盘 = analysis['price'] = current_price）卖出
                        sell_price_raw = current_price
                        sell_date = date
                        was_delayed, delay_days, forced_on_limit = False, 0, False
                    else:
                        # T 日跌停 → 无条件顺延；next_open → 无条件次日开盘
                        sell_price_raw, sell_date, was_delayed, delay_days, forced_on_limit = _try_sell_with_limit_check(
                            df, date, stock_code=pos.stock_code)
                        if sell_price_raw is None:
                            continue
                    
                    # 应用动态滑点（卖出价变低，基于个股流动性）
                    dyn_slippage_time = calc_dynamic_slippage(pos.stock_code, date)
                    raw_sell_price = sell_price_raw
                    sell_price = apply_slippage(raw_sell_price, is_buy=False, slippage_rate=dyn_slippage_time)
                    
                    pos.exit_date = sell_date
                    pos.exit_price = sell_price
                    exit_detail = f'时间止损(持仓{pos.bars_held}天,收益{_float_pnl_pct:.1f}%<{self.time_stop_min_profit_pct*100:.0f}%)'
                    if was_delayed:
                        exit_detail += f'[跌停延后{delay_days}天]'
                    if forced_on_limit:
                        exit_detail += '[跌停折价3%强制卖出]'
                    pos.exit_reason = exit_detail
                    # v8: 现金回收
                    recovered = pos.shares * sell_price
                    cost = calculate_trading_cost(recovered, pos.stock_code, is_buy=False)
                    fee = cost['total']
                    slippage_cost = recovered * dyn_slippage_time
                    recovered_net = recovered - fee
                    self.cash += recovered_net
                    pos.total_recovered += recovered_net  # v8.1: 记录回收
                    pos.total_fees += fee
                    pos.total_slippage_cost += slippage_cost
                    
                    # 记录成交记录
                    pos.filled_records.append({
                        'date': str(sell_date)[:10],
                        'action': 'sell',
                        'reason': '时间止损' + (f'[跌停延后{delay_days}天]' if was_delayed else '') + ('[跌停折价3%强制卖出]' if forced_on_limit else ''),
                        'price': sell_price,
                        'price_raw': raw_sell_price,
                        'shares': pos.shares,
                        'amount': recovered,
                        'commission': cost['commission'],
                        'stamp_duty': cost['stamp_duty'],
                        'transfer_fee': cost['transfer_fee'],
                        'slippage_cost': slippage_cost,
                        'total_fee': fee,
                    })
                    pos.last_trade_date = sell_date
                    
                    self.closed_positions.append(pos)
                    self.positions.remove(pos)
                    self.trade_log.append({
                        'date': str(date)[:10], 'stock': pos.stock_code,
                        'action': '时间止损', 'price': sell_price,
                        'pnl_pct': pos.nav_pnl_pct,
                        'nav': round(self.nav, 0),
                        'reason': pos.exit_reason
                    })

            # --- 扫描新信号 ---
            should_scan = (day_idx % self.SCAN_INTERVAL_DAYS == 0) and not market_crash_today
            if should_scan:
                if use_parallel:
                    candidates = self._scan_candidates_parallel(date, up_ratio, n_workers_eff, executor)
                else:
                    candidates = self._scan_candidates(date, up_ratio)
                # 分层选股（2026-09-27 用户决策）：按股票类型档分桶 + 档间轮转，
                # 使各档都有机会先于高分档拿到资金（原「星级降序 → 分数降序」全局排序
                # 会让强势/标准档吃光资金，低分档零成交，无法横向比性价比）。
                # 档内仍按因子预期分降序，核心指数池作末位 tiebreaker。
                candidates = _order_candidates_by_tier(candidates)
                tier_taken = {}

                for analysis in candidates:
                    _tier = _tier_of(analysis.get('stock_type'))
                    if TIER_QUOTA_PER_SCAN and tier_taken.get(_tier, 0) >= TIER_QUOTA_PER_SCAN:
                        continue  # 该档本轮配额已满，让位给其他档
                    if TIER_MAX_HOLDINGS and sum(
                            1 for p in self.positions
                            if _tier_of(getattr(p, 'stock_type', '')) == _tier) >= TIER_MAX_HOLDINGS:
                        continue  # 该档持仓已满，等已有持仓平仓后再补
                    entry_weight = analysis['entry_position']
                    # 环境门控（与线上 TradingPipeline 口径一致）：按市场冷暖乘参考系数。
                    # 此前回测缺该步 → 建仓仓位系统性偏大、收益偏乐观。
                    try:
                        from engine.market_gate import MarketGate
                        entry_weight = MarketGate.apply_gate(entry_weight, up_ratio, 'stock')[0]
                    except Exception:
                        pass
                    # 单只最大仓位风控约束
                    entry_weight = min(entry_weight, self.max_single_position)
                    # v8: 投资金额 = 当前净值 × 仓位比例
                    current_nav = self.nav
                    # 总仓位上限约束：开仓后所有持仓合计不超过净值上限
                    if self.total_position_cap_pct < 1.0:
                        held_weight = sum(p.total_invested for p in self.positions) / current_nav if current_nav > 0 else 0
                        room = max(0.0, self.total_position_cap_pct - held_weight)
                        entry_weight = min(entry_weight, room)
                    invested = current_nav * entry_weight

                    # 零仓位过滤：风控压缩后仓位为0，不创建伪交易
                    if entry_weight <= 0:
                        continue

                    # 获取次日开盘价（避免未来函数）
                    stock_code = analysis['stock_code']
                    df = _stock_data_cache.get(stock_code)
                    if df is None:
                        continue
                    raw_entry_price, next_date = _get_next_open_price(df, date, stock_code)
                    if raw_entry_price is None:
                        continue
                    
                    # 检查次日是否涨停（涨停难以买入）
                    if _is_limit_up(df, next_date, stock_code=stock_code):
                        continue
                    
                    # 应用动态滑点（买入价变高，基于个股流动性）
                    dyn_slippage_entry = calc_dynamic_slippage(stock_code, date)
                    entry_price = apply_slippage(raw_entry_price, is_buy=True, slippage_rate=dyn_slippage_entry)
                    
                    # 零仓位过滤：投入金额不足买入1手（100股），跳过
                    actual_invested = current_nav * entry_weight
                    if actual_invested < entry_price * 100:
                        continue
                    
                    # 确保现金足够（预留交易费用）
                    estimated_fee = calculate_trade_fee(actual_invested, stock_code, is_buy=True)
                    if actual_invested + estimated_fee > self.cash:
                        continue
                    
                    # 创建持仓（Position类会自动处理股数取整）
                    pos = Position(
                        stock_code=stock_code,
                        entry_date=date,
                        entry_price=entry_price,
                        entry_weight=entry_weight,
                        entry_weight_raw=analysis['entry_position'],
                        stock_type=analysis['stock_type'],
                        entry_action=analysis['entry_action'],
                        invested_amount=actual_invested
                    )
                    
                    # 零仓位过滤：取整后股数为0则跳过（投入金额不足买入1手）
                    if pos.shares <= 0:
                        continue

                    # 资金门槛检查：实际可买金额 < 信号理论仓位金额 × 0.5，标记为作废
                    signal_theoretical_amount = current_nav * analysis['entry_position']
                    actual_buy_amount = pos.shares * entry_price
                    if actual_buy_amount < signal_theoretical_amount * 0.5:
                        self.trade_log.append({
                            'date': str(date)[:10],
                            'stock': stock_code,
                            'action': '信号作废（资金不足）',
                            'price': entry_price,
                            'shares': pos.shares,
                            'amount': round(actual_buy_amount, 2),
                            'theoretical_amount': round(signal_theoretical_amount, 2),
                            'reason': f"实际可买{pos.shares}股×{entry_price:.2f}元={actual_buy_amount:.0f}元 < 理论{analysis['entry_position']*100:.1f}%×{current_nav:.0f}×0.5={signal_theoretical_amount*0.5:.0f}元"
                        })
                        continue

                    # 重新计算实际投入（基于取整后的股数和含滑点的成交价）
                    actual_invested = pos.shares * entry_price
                    cost = calculate_trading_cost(actual_invested, stock_code, is_buy=True)
                    fee = cost['total']
                    slippage_cost = actual_invested * dyn_slippage_entry
                    
                    pos.total_invested = actual_invested
                    pos.invested_amount = actual_invested
                    pos.total_fees += fee
                    pos.total_slippage_cost += slippage_cost
                    
                    # 记录成交记录
                    pos.filled_records.append({
                        'date': str(next_date)[:10],
                        'action': 'buy',
                        'reason': '建仓',
                        'price': entry_price,
                        'price_raw': raw_entry_price,
                        'shares': pos.shares,
                        'amount': actual_invested,
                        'commission': cost['commission'],
                        'stamp_duty': cost['stamp_duty'],
                        'transfer_fee': cost['transfer_fee'],
                        'slippage_cost': slippage_cost,
                        'total_fee': fee,
                    })
                    pos.last_trade_date = next_date
                    
                    # 扣除现金
                    self.cash -= (actual_invested + fee)

                    self.positions.append(pos)
                    tier_taken[_tier] = tier_taken.get(_tier, 0) + 1
                    self.trade_log.append({
                        'date': str(date)[:10], 'stock': pos.stock_code,
                        'action': f'开仓({pos.entry_action})', 'price': pos.entry_price,
                        'shares': pos.shares,
                        'pnl_pct': 0,
                        'nav': round(self.nav, 0),
                        'reason': f'评分{analysis["final_score"]:.0f}, 投入{actual_invested:,.0f}, 费用{fee:.2f}, 滑点{slippage_cost:.2f}'
                    })

            # v8: 记录每日净值
            self._record_equity(date)

            # 兼容旧 daily_equity（v8.1: 改用 self.nav 避免重复计算）
            self.daily_equity.append({
                'date': str(date)[:10],
                'open_positions': len(self.positions),
                'unrealized_pnl': round(self.nav - self.INITIAL_CAPITAL, 2)
            })

            elapsed = time.time() - start_time
            done = day_idx + 1
            remaining = elapsed / done * (len(trading_dates) - done) if done > 0 else 0
            if done % 10 == 0 or done == len(trading_dates):
                scan_tag = " [扫描日]" if should_scan else ""
                current_nav = self.nav
                total_return = (current_nav / self.INITIAL_CAPITAL - 1) * 100
                dd = (1 - current_nav / self.peak_nav) * 100
                print(f"  日期 {done}/{len(trading_dates)} ({str(date)[:10]}){scan_tag} | "
                      f"持仓: {len(self.positions)} | 已平仓: {len(self.closed_positions)} | "
                      f"净值: {current_nav:,.0f} ({total_return:+.1f}%) | 回撤: {dd:.1f}% | "
                      f"耗时: {elapsed:.0f}s")

            if done % 20 == 0:
                self._save_checkpoint(done)

        # for 正常结束：回收常驻进程池
        if executor is not None:
            executor.shutdown(wait=True)

        # --- 强制平仓剩余持仓（所有持仓按最后收盘价平仓，严格T+1仅用于标注） ---
        force_closed = []
        for pos in self.positions:
            if pos.stock_code in self.failed_stocks:
                continue
            if pos.shares <= 0:
                continue
            df = _stock_data_cache.get(pos.stock_code)
            if df is None:
                continue

            idx = _get_index_for_date(df, trading_dates[-1], pos.stock_code)
            if idx < 0:
                idx = len(df) - 1
                if idx < 0:
                    continue

            # 执行价模式：same_close=当日收盘价, next_open=次日开盘价(原逻辑)
            if self._use_same_day_close():
                exit_price = float(df.iloc[idx]['close'])
                exit_date = df.iloc[idx]['trade_time']
                price_source = '当日收盘价'
            else:
                # 优先用次日开盘价卖出（与回测过程一致，T+1规则）
                # 若没有次日数据（最后一天），回退到当日收盘价
                next_open, next_date = _get_next_open_price(df, df.iloc[idx]['trade_time'], stock_code=pos.stock_code)
                if next_open is not None and next_date is not None:
                    exit_price = next_open
                    exit_date = next_date
                    price_source = '次日开盘价'
                else:
                    exit_price = float(df.iloc[idx]['close'])
                    exit_date = df.iloc[idx]['trade_time']
                    price_source = '当日收盘价(无次日数据)'
            pos.exit_date = exit_date
            pos.exit_price = exit_price

            # 严格T+1：bars_held<2 的也计入收益，但明确标注
            if pos.bars_held < 2:
                pos.exit_reason = f'回测结束按{price_source}平仓(T+1严格模式)'
            else:
                pos.exit_reason = f'回测结束按{price_source}平仓'

            # 计算回收金额和交易费用
            recovered = pos.shares * exit_price
            cost = calculate_trading_cost(recovered, pos.stock_code, is_buy=False)
            fee = cost['total']
            recovered_net = recovered - fee
            self.cash += recovered_net
            pos.total_recovered += recovered_net
            pos.total_fees += fee

            # 记录成交明细
            pos.filled_records.append({
                'date': str(exit_date)[:10],
                'action': 'sell',
                'reason': pos.exit_reason,
                'price': exit_price,
                'price_raw': exit_price,
                'shares': pos.shares,
                'amount': recovered,
                'commission': cost['commission'],
                'stamp_duty': cost['stamp_duty'],
                'transfer_fee': cost['transfer_fee'],
                'slippage_cost': 0,
                'total_fee': fee,
            })

            force_closed.append(pos)
            self.closed_positions.append(pos)

        self.positions = []

        # 最后一日净值记录
        if trading_dates:
            self._record_equity(trading_dates[-1])

        total_time = time.time() - start_time
        print(f"\n  总耗时: {total_time:.1f}s ({total_time/60:.1f}分钟)")
        return self.generate_report(years_span)

    def _compute_compound_metrics(self, years_span):
        """v8: 从权益曲线计算复利指标"""
        if not self.equity_curve or len(self.equity_curve) < 2:
            return {'cagr': 0, 'max_drawdown': 0, 'sharpe': 0, 'calmar': 0,
                    'final_nav': self.INITIAL_CAPITAL}

        final_nav = self.equity_curve[-1]['nav']
        initial_nav = self.INITIAL_CAPITAL
        total_return = (final_nav / initial_nav - 1)

        # CAGR
        years_span = max(years_span, 0.05)
        cagr = (final_nav / initial_nav) ** (1 / years_span) - 1

        # 最大回撤
        max_dd = max(r['drawdown'] for r in self.equity_curve)

        # Sharpe Ratio (年化)
        daily_rets = [r['daily_return'] for r in self.equity_curve]
        if len(daily_rets) >= 5:
            mean_ret = np.mean(daily_rets)
            std_ret = np.std(daily_rets, ddof=1)
            sharpe = (mean_ret / std_ret) * np.sqrt(252) if std_ret > 0 else 0
        else:
            sharpe = 0

        # Calmar Ratio
        calmar = cagr / max_dd if max_dd > 0 else 0

        return {
            'final_nav': round(final_nav, 2),
            'total_return': round(total_return * 100, 2),
            'cagr': round(cagr * 100, 2),
            'max_drawdown': round(max_dd * 100, 2),
            'sharpe': round(sharpe, 2),
            'calmar': round(calmar, 2),
            'years_span': round(years_span, 2),
        }

    def _compute_benchmark(self, years_span=1.0):
        """基准买入持有对照（默认沪深300）—— 复用熔断基准指数的日收益序列。

        与策略同窗口：从首个交易日收盘持有到末日收盘，收益 = ∏(1+r_d) − 1。
        用于把「绝对收益」升级为「超额收益」，否则无法判断策略是否跑赢大盘。
        """
        idx = getattr(self, '_index_returns', None) or {}
        code = getattr(self, 'market_crash_index', '') or 'sh000300'
        base = {'index': code, 'available': False,
                'benchmark_return': None, 'benchmark_cagr': None}
        if not idx:
            return base
        # 窗口用实际交易日（净值曲线首/末日），而非 start_date —— 后者可能是非交易日，
        # 会把「窗口前一日→首个交易日」的涨跌误算进基准。
        _eq = self.equity_curve or []
        _first = (_eq[0].get('date') if _eq else None) or self.start_date
        _last = (_eq[-1].get('date') if _eq else None) or self.end_date
        if not _first or not _last:
            return base
        start = pd.Timestamp(_first).normalize()
        end = pd.Timestamp(_last).normalize()
        cum, n = 1.0, 0
        for d, r in idx.items():
            try:
                dn = pd.Timestamp(d).normalize()
            except Exception:
                continue
            if start < dn <= end:  # 排除首日：其收益相对窗口外的前一日
                cum *= (1.0 + float(r or 0.0))
                n += 1
        if n == 0:
            return base
        years = max(float(years_span or 1.0), 0.05)
        cagr = (cum ** (1.0 / years) - 1.0) * 100 if cum > 0 else 0.0
        return {'index': code, 'available': True, 'days': n,
                'benchmark_return': round((cum - 1.0) * 100, 2),
                'benchmark_cagr': round(cagr, 2)}

    def _compute_tier_stats(self):
        """按股票类型档统计成交绩效 —— 分层选股的核心产出：横向比较各档性价比。

        只统计有效持仓（有股数、有投入、已平仓）。收益率用 nav_pnl_pct（复利口径，
        与报告主口径一致）；净盈亏用 累计回收 − 累计投入（含费用与滑点）。
        """
        groups = {}
        for p in self.closed_positions:
            if not (float(getattr(p, 'shares', 0) or 0) > 0
                    and float(getattr(p, 'total_invested', 0) or 0) > 0
                    and getattr(p, 'exit_price', None) is not None):
                continue
            groups.setdefault(_tier_of(getattr(p, 'stock_type', '')), []).append(p)

        stats = []
        for t in TIER_ORDER:
            ps = groups.get(t) or []
            if not ps:
                continue
            pnls = [float(getattr(p, 'nav_pnl_pct', 0) or 0) for p in ps]
            wins = [x for x in pnls if x > 0]
            losses = [x for x in pnls if x <= 0]
            avg_win = float(np.mean(wins)) if wins else 0.0
            avg_loss = float(np.mean(losses)) if losses else 0.0
            invested = float(np.sum([float(getattr(p, 'total_invested', 0) or 0) for p in ps]))
            net = float(np.sum([float(getattr(p, 'total_recovered', 0) or 0)
                                - float(getattr(p, 'total_invested', 0) or 0) for p in ps]))
            stats.append({
                'tier': t,
                'label': TIER_LABELS.get(t, t),
                'trades': len(ps),
                'win_rate': round(len(wins) / len(ps) * 100, 1),
                'avg_pnl': round(float(np.mean(pnls)), 2),
                'avg_win': round(avg_win, 2),
                'avg_loss': round(avg_loss, 2),
                'profit_loss_ratio': round(abs(avg_win / avg_loss), 2) if avg_loss else None,
                'avg_hold_days': round(float(np.mean([float(getattr(p, 'bars_held', 0) or 0) for p in ps])), 1),
                'avg_weight': round(float(np.mean([float(getattr(p, 'entry_weight', 0) or 0)
                                                   for p in ps])) * 100, 2),
                'total_invested': round(invested, 2),
                'net_pnl': round(net, 2),
                'roi_on_invested': round(net / invested * 100, 2) if invested else None,
                'avg_max_dd': round(float(np.mean([float(getattr(p, 'min_pnl', 0) or 0) for p in ps])), 2),
            })
        return stats

    def generate_report(self, years_span=1.0):
        """生成策略回测报告（v8: 增加复利指标）"""
        if not self.closed_positions:
            print("无平仓记录！本次回测 0 交易，仍写出空报告以覆盖历史结果。")
            # 不 return None：返回空报告对象，使上层 `if report:` 写盘守卫通过，
            # 否则前端会读到上一次的历史旧报告（造成“过程 0 交易 vs 结果有交易”的不匹配）。
            empty_compound = {
                'final_nav': self.INITIAL_CAPITAL,
                'total_return': 0.0,
                'cagr': 0.0,
                'max_drawdown': 0.0,
                'sharpe': 0.0,
                'calmar': 0.0,
            }
            report = {
                'total_trades': 0,
                'win_rate': 0.0,
                'avg_pnl_simple': 0.0,
                'avg_pnl_compound': 0.0,
                'avg_win': 0.0,
                'avg_loss': 0.0,
                'profit_loss_ratio': None,
                'reduce_triggered': 0,
                'reduce_protected': 0,
                'clean_exit': 0,
                'add_triggered': 0,
                'start_date': (self.start_date.strftime('%Y-%m-%d') if self.start_date else ''),
                'end_date': (self.end_date.strftime('%Y-%m-%d') if self.end_date else ''),
                'compound': empty_compound,
                'benchmark': {'index': getattr(self, 'market_crash_index', '') or 'sh000300',
                              'available': False, 'benchmark_return': None,
                              'benchmark_cagr': None},
                'equity_curve_file': self.equity_csv_path or os.path.join(get_app_dir(), 'strategy_equity.csv'),
                'tier_stats': [],   # 分层选股：0 交易时也给出空表，避免前端读到上次旧结果
                'empty': True,
            }
            # 仍写出净值曲线（全为初始资金），保证文件存在且与过程一致
            equity_csv = self.equity_csv_path or os.path.join(get_app_dir(), 'strategy_equity.csv')
            if _safe_write_csv(
                    pd.DataFrame(_build_equity_rows(self.equity_curve, self.INITIAL_CAPITAL)),
                    equity_csv, '净值曲线', self.export_errors)[0]:
                print(f"\n净值曲线已保存: {equity_csv}")
            if self.export_errors:
                report['export_errors'] = list(self.export_errors)
            return report

        compound = self._compute_compound_metrics(years_span)

        # 基准对照（默认沪深300买入持有）→ 超额收益
        benchmark = self._compute_benchmark(years_span)
        if benchmark.get('available'):
            benchmark['excess_return'] = round(compound['total_return'] - benchmark['benchmark_return'], 2)
            benchmark['excess_cagr'] = round(compound['cagr'] - benchmark['benchmark_cagr'], 2)

        print("\n" + "=" * 70)
        print("策略回测报告 v8 - 加仓减仓逻辑验证（净值复利）")
        print("=" * 70)

        # ========== 回测参数摘要 ==========
        print(f"\n--- 回测参数 ---")
        print(f"  初始资金: {self.INITIAL_CAPITAL:,.0f} 元")
        print(f"  股票池: {len(self.stock_pool)} 只")
        print(f"  费率: 佣金万{COMMISSION_RATE*10000:.0f} / 印花税千{STAMP_DUTY_RATE*1000:.0f}(卖) / 过户费万{TRANSFER_FEE_RATE*10000:.0f}(沪)")
        print(f"  滑点: {SLIPPAGE_RATE*100:.1f}%")
        print(f"  卖出/加仓执行价: {'信号触发价(T日收盘)' if self._use_same_day_close() else 'T+1开盘价'}")
        print(f"  风控: 单笔最大亏损{self.single_max_loss_pct*100:.0f}%")
        print(f"  仓位: 单只最大{self.max_single_position*100:.0f}% | 总仓位上限{self.total_position_cap_pct*100:.0f}%")
        print(f"  最小交易间隔: {self.min_trade_interval} 天")
        print(f"  价格剧变突破阈值: {self.price_breakthrough_threshold*100:.0f}%")

        # ========== v8 新增: 复利总览 ==========
        print(f"\n--- 复利净值总览 ---")
        print(f"  初始资金: {self.INITIAL_CAPITAL:,.0f}")
        print(f"  最终净值: {compound['final_nav']:,.0f}")
        print(f"  总收益率: {compound['total_return']:+.2f}%")
        print(f"  CAGR: {compound['cagr']:+.2f}%")
        print(f"  最大回撤: {compound['max_drawdown']:.2f}%")
        print(f"  Sharpe Ratio: {compound['sharpe']:.2f}")
        print(f"  Calmar Ratio: {compound['calmar']:.2f}")

        # ========== 基准对照（把绝对收益升级为超额收益）==========
        if benchmark.get('available'):
            print(f"\n--- 基准对照（{benchmark['index']} 买入持有，{benchmark['days']}个交易日） ---")
            print(f"  基准收益: {benchmark['benchmark_return']:+.2f}%  (年化 {benchmark['benchmark_cagr']:+.2f}%)")
            print(f"  超额收益: {benchmark['excess_return']:+.2f}%  (年化 {benchmark['excess_cagr']:+.2f}%)")
        else:
            print("\n--- 基准对照 ---")
            print("  [warn] 基准指数数据不可用，无法计算超额收益")

        total_trades = len(self.closed_positions)
        valid_positions = [p for p in self.closed_positions if p.shares > 0 and p.total_invested > 0 and p.exit_price is not None]
        wins = [p for p in valid_positions if p.nav_pnl_pct > 0]
        losses = [p for p in valid_positions if p.nav_pnl_pct <= 0]
        win_rate = len(wins) / len(valid_positions) * 100 if valid_positions else 0

        avg_win = np.mean([p.nav_pnl_pct for p in wins]) if wins else 0
        avg_loss = np.mean([p.nav_pnl_pct for p in losses]) if losses else 0
        avg_pnl = np.mean([p.nav_pnl_pct for p in valid_positions]) if valid_positions else 0

        print(f"\n--- 交易统计（基于复利后的每笔收益） ---")
        print(f"  总交易次数: {total_trades} (有效: {len(valid_positions)})")
        print(f"  胜率: {win_rate:.1f}% ({len(wins)}胜 / {len(losses)}负)")
        print(f"  平均收益: {avg_pnl:+.2f}%")
        print(f"  平均盈利: {avg_win:+.2f}%")
        print(f"  平均亏损: {avg_loss:+.2f}%")
        if avg_loss != 0:
            print(f"  盈亏比: {abs(avg_win/avg_loss):.2f}")
        print(f"  最大单笔盈利: {max(p.nav_pnl_pct for p in valid_positions):+.2f}%" if valid_positions else "  最大单笔盈利: N/A")
        print(f"  最大单笔亏损: {min(p.nav_pnl_pct for p in valid_positions):+.2f}%" if valid_positions else "  最大单笔亏损: N/A")

        # ========== 仓位压缩统计（v9新增）==========
        print(f"\n--- 仓位压缩统计 ---")
        compressed_count = 0
        compression_sum = 0.0
        for p in self.closed_positions:
            raw = getattr(p, 'entry_weight_raw', 0)
            actual = getattr(p, 'entry_weight', 0)
            if raw > 0 and actual < raw * 0.9:  # 被压缩超过10%
                compressed_count += 1
                compression_sum += (raw - actual) * 100
        if compressed_count > 0:
            print(f"  因环境门控/风控/资金被压缩的笔数: {compressed_count}/{total_trades} ({compressed_count/total_trades*100:.1f}%)")
            print(f"  平均压缩幅度: {compression_sum/compressed_count:.2f}%")
            print(f"  【提示】这些交易在实盘中可能因资金/仓位上限无法按信号仓位建仓")
        else:
            print(f"  无明显仓位压缩")

        # ========== 简单平均 vs 复利 对比 ==========
        simple_avg = np.mean([p.pnl_pct for p in self.closed_positions])
        # 正确口径：简单平均×笔数（粗略参考，非复利叠加；
        # 交易在时间上重叠，不可直接把单笔均值复利相乘，旧版 (1+avg)^n-1 数学无意义）
        simple_scaled = simple_avg * total_trades

        print(f"\n--- 简单平均 vs 净值复利 对比 ---")
        print(f"  简单算术平均收益: {simple_avg:+.2f}%/笔")
        print(f"  复利模式下每笔收益: {avg_pnl:+.2f}%/笔 (考虑加减仓权重)")
        print(f"  简单平均×笔数（粗略参考）: {simple_scaled:+.1f}%")
        print(f"  净值复利实际总收益: {compound['total_return']:+.2f}%")
        print(f"  说明: 二者口径不同（前者为等权单笔平均的线性外推，后者为实际资金复利），")
        print(f"        不可直接相减得出'加减仓价值'，仅作量级参考。")

        # ========== 风控事件日志 ==========
        if self.risk_events:
            print(f"\n--- 风控事件日志 ---")
            print(f"  {'日期':<12} {'类型':<15} {'回撤':>8} {'阈值':>8} {'持仓数':>6}")
            print("  " + "-" * 50)
            for event in self.risk_events:
                drawdown_pct = event.get('drawdown', 0) * 100
                threshold_pct = event.get('threshold', 0) * 100
                pos_count = len(event.get('positions', []))
                print(f"  {event['date']:<12} {event['type']:<15} {drawdown_pct:>7.1f}% {threshold_pct:>7.1f}% {pos_count:>6}")

        # ========== 减仓逻辑验证 ==========
        print(f"\n--- 减仓逻辑验证 ---")
        reduce_triggered = 0
        reduce_protected = 0
        clean_exit = 0
        for p in self.closed_positions:
            if p.reduce_history:
                reduce_triggered += 1
                has_clear = any('清仓' in r['action'] for r in p.reduce_history)
                if has_clear:
                    clean_exit += 1
                if p.reduce_history:
                    first_reduce = p.reduce_history[0]
                    reduce_price = first_reduce['price']
                    if p.exit_price and p.exit_price < reduce_price and p.nav_pnl_pct < 0:
                        reduce_protected += 1

        print(f"  触发减仓的交易: {reduce_triggered}/{total_trades} "
              f"({reduce_triggered/total_trades*100:.1f}%)" if total_trades > 0 else "")
        if reduce_triggered > 0:
            print(f"  其中清仓退出: {clean_exit}/{total_trades} "
                  f"({clean_exit/total_trades*100:.1f}%)" if total_trades > 0 else "")
            print(f"  减仓保护成功: {reduce_protected}/{reduce_triggered} "
                  f"({reduce_protected/reduce_triggered*100:.1f}%)")

        reduced_pnl = [p.nav_pnl_pct for p in self.closed_positions if p.reduce_history]
        not_reduced_pnl = [p.nav_pnl_pct for p in self.closed_positions if not p.reduce_history]
        if reduced_pnl and not_reduced_pnl:
            print(f"\n  减仓交易平均收益: {np.mean(reduced_pnl):+.2f}%")
            print(f"  不减仓交易平均收益: {np.mean(not_reduced_pnl):+.2f}%")
            print(f"  {'减仓逻辑有效: 减仓交易收益更优' if np.mean(reduced_pnl) > np.mean(not_reduced_pnl) else '减仓逻辑需优化: 减仓反而降低了收益'}")

        # ========== 加仓逻辑验证 ==========
        print(f"\n--- 加仓逻辑验证 ---")
        added_positions = [p for p in self.closed_positions if p.add_history]
        not_added_positions = [p for p in self.closed_positions if not p.add_history]
        if added_positions:
            added_pnl = [p.nav_pnl_pct for p in added_positions]
            not_added_pnl = [p.nav_pnl_pct for p in not_added_positions] if not_added_positions else []
            print(f"  触发加仓的交易: {len(added_positions)}/{total_trades} "
                  f"({len(added_positions)/total_trades*100:.1f}%)")
            print(f"  加仓交易平均收益: {np.mean(added_pnl):+.2f}%")
            if not_added_pnl:
                print(f"  未加仓交易平均收益: {np.mean(not_added_pnl):+.2f}%")
                diff_add = np.mean(added_pnl) - np.mean(not_added_pnl)
                print(f"  收益差: {diff_add:+.2f}%")
                print(f"  {'加仓逻辑有效: 加仓交易收益更高' if diff_add > 0 else '加仓逻辑需优化: 加仓交易收益反而更低'}")

        # ========== 按入场信号分类 ==========
        print(f"\n--- 按入场信号分类 ---")
        print(f"  {'入场信号':<12} {'次数':>6} {'平均收益':>8} {'胜率':>7} {'平均持仓天数':>10}")
        print("  " + "-" * 50)
        entry_groups = {}
        for p in self.closed_positions:
            action = p.entry_action
            if action not in entry_groups:
                entry_groups[action] = []
            entry_groups[action].append(p)
        for action, positions in sorted(entry_groups.items(), key=lambda x: -len(x[1])):
            pnls = [p.nav_pnl_pct for p in positions]
            wr = sum(1 for p in positions if p.nav_pnl_pct > 0) / len(positions) * 100
            avg_days = np.mean([p.bars_held for p in positions])
            print(f"  {action:<10} {len(positions):>6} {np.mean(pnls):>7.2f}% {wr:>6.1f}% {avg_days:>9.1f}天")

        # ========== 按股票类型档分类（分层选股的性价比对比）==========
        # 分层选股（2026-09-27）：各档都建仓取样，本表即「哪一档更划算」的横向对照。
        # 读法：avg_pnl=单笔平均收益率（复利口径）；roi_on_invested=净盈亏/累计投入，
        #       同时反映「赚多少」和「占了多少资金」，是横向比性价比的主指标。
        tier_stats = self._compute_tier_stats()
        print(f"\n--- 按股票类型档分类（分层选股·性价比对比）---")
        if tier_stats:
            print(f"  {'类型档':<8}{'笔数':>6}{'胜率':>8}{'平均收益':>10}{'平均盈利':>10}"
                  f"{'平均亏损':>10}{'盈亏比':>8}{'持仓天':>8}{'净盈亏':>14}{'投入回报':>10}")
            print("  " + "-" * 84)
            for s in tier_stats:
                print(f"  {s['label']:<8}{s['trades']:>6}{s['win_rate']:>7.1f}%"
                      f"{s['avg_pnl']:>9.2f}%{s['avg_win']:>9.2f}%{s['avg_loss']:>9.2f}%"
                      f"{(s['profit_loss_ratio'] if s['profit_loss_ratio'] is not None else 0):>8.2f}"
                      f"{s['avg_hold_days']:>7.1f}天{s['net_pnl']:>13,.0f}"
                      f"{(s['roi_on_invested'] if s['roi_on_invested'] is not None else 0):>9.2f}%")
            _best_roi = max(tier_stats, key=lambda x: x['roi_on_invested'] or -1e9)
            _best_pnl = max(tier_stats, key=lambda x: x['avg_pnl'])
            print(f"  【提示】单笔平均收益最高: {_best_pnl['label']} {_best_pnl['avg_pnl']:+.2f}%"
                  f" | 投入回报最高: {_best_roi['label']} {(_best_roi['roi_on_invested'] or 0):+.2f}%"
                  f"（笔数 <10 的档样本不足，结论仅供参考）")
        else:
            print("  无有效分档样本")

        # ========== 持仓时间统计 ==========
        print(f"\n--- 持仓时间统计 ---")
        hold_days = [p.bars_held for p in self.closed_positions]
        print(f"  平均持仓: {np.mean(hold_days):.1f}天")
        print(f"  中位数: {np.median(hold_days):.0f}天")
        print(f"  最长: {max(hold_days)}天")
        print(f"  最短: {min(hold_days)}天")

        # ========== 退出原因统计 ==========
        print(f"\n--- 退出原因统计 ---")
        print(f"  {'退出原因':<20} {'次数':>6} {'平均收益':>8}")
        print("  " + "-" * 40)
        exit_groups = {}
        for p in self.closed_positions:
            reason = p.exit_reason.split(':')[0].split('(')[0].strip()
            if reason not in exit_groups:
                exit_groups[reason] = []
            exit_groups[reason].append(p)
        for reason, positions in sorted(exit_groups.items(), key=lambda x: -len(x[1])):
            pnls = [p.nav_pnl_pct for p in positions]
            print(f"  {reason:<18} {len(positions):>6} {np.mean(pnls):>7.2f}%")

        # ========== 数据质量（离群/伪像检测）==========
        print(f"\n--- 数据质量（离群交易检测）---")
        suspect_trades = 0
        suspect_pnl_sum = 0.0
        for p in self.closed_positions:
            df = _stock_data_cache.get(p.stock_code)
            if df is None or p.entry_date is None:
                continue
            try:
                ts0 = pd.Timestamp(p.entry_date)
                ts1 = pd.Timestamp(p.exit_date) if p.exit_date is not None else ts0
                mask = (df['trade_time'] >= ts0) & (df['trade_time'] <= ts1)
                seg = df.loc[mask, 'close'].astype(float).values
                seg = seg[seg > 0]   # 防 0 收盘导致除零误报
                if len(seg) >= 2:
                    daily = np.abs(np.diff(seg) / seg[:-1])
                    if daily.max() > 0.25:   # A股涨跌停±10%/±20%，>25%单日缺口几乎必为数据伪像
                        suspect_trades += 1
                        suspect_pnl_sum += p.nav_pnl_pct
            except Exception:
                pass
        if suspect_trades > 0:
            print(f"  ⚠ 检测到 {suspect_trades} 笔持仓区间内含 >25% 单日缺口")
            print(f"    这些交易合计贡献收益率（粗略）: {suspect_pnl_sum:+.1f}%")
            print(f"    【说明】抓取层已拦截未复权数据，此类缺口几乎必为真实极端事件")
            print(f"    （连板/复牌/重大利好利空），非数据伪像；请人工复核是否纳入样本")
        else:
            print(f"  未检测到 >25% 单日缺口的离群交易（数据质量良好）")

        # ========== 结论 ==========
        print(f"\n{'='*70}")
        print("综合结论")
        print("=" * 70)

        print(f"\n  复利指标:")
        print(f"    总收益率: {compound['total_return']:+.1f}%")
        print(f"    CAGR: {compound['cagr']:+.1f}%")
        print(f"    最大回撤: {compound['max_drawdown']:.1f}%")
        print(f"    Sharpe: {compound['sharpe']:.2f}")
        print(f"    胜率: {win_rate:.1f}%")

        if compound['cagr'] > 10 and compound['sharpe'] > 0.5:
            print(f"\n  [策略有效] 正CAGR + 合理Sharpe，复利模式下实现了正期望收益")
        elif compound['cagr'] > 0:
            print(f"\n  [策略微利] CAGR为正但Sharpe偏低，依赖少数大盈利")
        else:
            print(f"\n  [策略亏损] 复利CAGR为负，需要调整参数或逻辑")

        if compound['max_drawdown'] < 20:
            print(f"  [回撤可控] 最大回撤{compound['max_drawdown']:.1f}%在可接受范围")
        else:
            print(f"  [回撤较大] 最大回撤{compound['max_drawdown']:.1f}%需要关注风险控制")

        print("\n" + "=" * 70)

        # ========== 保存净值曲线CSV（一日一行，人工可读）==========
        equity_csv = self.equity_csv_path or os.path.join(get_app_dir(),
                                  'strategy_equity.csv')
        if _safe_write_csv(
                pd.DataFrame(_build_equity_rows(self.equity_curve, self.INITIAL_CAPITAL)),
                equity_csv, '净值曲线', self.export_errors)[0]:
            print(f"\n净值曲线已保存: {equity_csv}")

        # 产物路径（由 {prefix}_trades.json 派生）
        trades_base = (self.trades_json_path or os.path.join(
            get_app_dir(), 'backtest_trades.json'))
        trades_csv_path = trades_base.replace('.json', '.csv')
        detail_csv_path = trades_csv_path.replace('.csv', '_detail.csv')

        # ========== 逐笔平仓明细导出（供专家交付三件套使用，不改策略逻辑）==========
        try:
            trades_raw = []
            for p in self.closed_positions:
                if p.shares <= 0 or p.total_invested <= 0:
                    continue
                # 哨兵：建仓日期晚于平仓日期 = 日期解析错误（曾由停牌/数据缺口导致，
                # 见 _st_compute_full_analysis 注释）。显式告警，不再静默产出矛盾记录。
                try:
                    if (p.entry_date is not None and p.exit_date is not None
                            and pd.Timestamp(p.exit_date) < pd.Timestamp(p.entry_date)):
                        print(f"[warn] 交易日期异常：{p.stock_code} 建仓 {str(p.entry_date)[:10]} "
                              f"晚于平仓 {str(p.exit_date)[:10]}（疑似停牌/数据缺口）")
                except Exception:
                    pass
                _s = _fill_stats(p)
                trades_raw.append({
                    'stock_code': p.stock_code,
                    'stock_name': _stock_name(p.stock_code),
                    'stock_type': getattr(p, 'stock_type', ''),
                    'entry_action': getattr(p, 'entry_action', ''),
                    'entry_date': str(p.entry_date)[:10],
                    'exit_date': str(p.exit_date)[:10] if p.exit_date is not None else '',
                    'entry_price': round(float(p.entry_price), 4),
                    'exit_price': round(float(p.exit_price), 4) if p.exit_price is not None else 0.0,
                    'shares': round(float(p.shares), 2),
                    'initial_shares': round(float(getattr(p, 'initial_shares', 0)), 2),
                    'add_shares': round(_s['add'], 2),
                    'reduce_shares': round(_s['reduce'], 2),
                    'close_shares': round(_s['close'], 2),
                    'total_invested': round(float(p.total_invested), 2),
                    'total_recovered': round(float(p.total_recovered), 2),
                    'total_fees': round(float(getattr(p, 'total_fees', 0)), 2),
                    'total_slippage_cost': round(float(getattr(p, 'total_slippage_cost', 0) or 0), 2),
                    'pnl_pct': round(float(p.pnl_pct), 4),
                    'nav_pnl_pct': round(float(p.nav_pnl_pct), 4),
                    'max_pnl': round(float(getattr(p, 'max_pnl', 0) or 0), 4),
                    'min_pnl': round(float(getattr(p, 'min_pnl', 0) or 0), 4),
                    'bars_held': int(getattr(p, 'bars_held', 0)),
                    'exit_reason': p.exit_reason or '',
                    'reduce_count': len(getattr(p, 'reduce_history', []) or []),
                    'add_count': len(getattr(p, 'add_history', []) or []),
                    'entry_weight': round(float(getattr(p, 'entry_weight', 0)), 4),
                    'entry_weight_raw': round(float(getattr(p, 'entry_weight_raw', 0)), 4),
                    # 以下为审计明细：人类 CSV 已剔除，故完整保留在 JSON 里，不丢信息
                    'risk_meta': getattr(p, 'risk_meta', {}) or {},
                    'fills': list(getattr(p, 'filled_records', []) or []),
                    'add_history': list(getattr(p, 'add_history', []) or []),
                    'reduce_history': list(getattr(p, 'reduce_history', []) or []),
                })
            with open(trades_base, 'w', encoding='utf-8') as tf:
                json.dump(trades_raw, tf, ensure_ascii=False, indent=1)
            print(f"逐笔明细已保存: {trades_base}（{len(trades_raw)} 笔）")
        except Exception as _e:
            print(f"[warn] 逐笔明细导出失败: {_e}")

        # ========== 持仓明细 CSV（一笔持仓一行，人工核对用）==========
        trade_rows = _build_trade_rows(self.closed_positions, _stock_name)
        if _safe_write_csv(pd.DataFrame(trade_rows), trades_csv_path,
                           '持仓明细CSV', self.export_errors)[0]:
            print(f"持仓明细CSV已保存: {trades_csv_path}（{len(trade_rows)} 笔）")

        # ========== 交易流水 CSV（一笔成交一行，含逐笔已实现盈亏）==========
        fill_rows = _build_fill_rows(self.closed_positions, _stock_name)
        if _safe_write_csv(pd.DataFrame(fill_rows), detail_csv_path,
                           '交易流水CSV', self.export_errors)[0]:
            print(f"交易流水CSV已保存: {detail_csv_path}（{len(fill_rows)} 笔成交）")

        # ========== 分档绩效 CSV（分层选股：各类型档性价比对照）==========
        tier_rows = [{
            '类型档': s['label'], '档位键': s['tier'], '笔数': s['trades'],
            '胜率%': s['win_rate'], '平均收益%': s['avg_pnl'],
            '平均盈利%': s['avg_win'], '平均亏损%': s['avg_loss'],
            '盈亏比': s['profit_loss_ratio'], '平均持仓天数': s['avg_hold_days'],
            '平均仓位%': s['avg_weight'], '累计投入': s['total_invested'],
            '净盈亏': s['net_pnl'], '投入回报%': s['roi_on_invested'],
            '平均持仓内最大浮亏%': s['avg_max_dd'],
        } for s in tier_stats]
        tier_csv_path = os.path.splitext(trades_csv_path)[0] + '_tier_stats.csv'
        if _safe_write_csv(pd.DataFrame(tier_rows), tier_csv_path,
                           '分档绩效CSV', self.export_errors)[0]:
            print(f"分档绩效CSV已保存: {tier_csv_path}（{len(tier_rows)} 档）")

        report = {
            'total_trades': total_trades,
            'win_rate': round(win_rate, 1),
            'avg_pnl_simple': round(simple_avg, 2),  # 简单算术平均
            'avg_pnl_compound': round(avg_pnl, 2),   # 复利加权
            'avg_win': round(avg_win, 2),
            'avg_loss': round(avg_loss, 2),
            'profit_loss_ratio': round(abs(avg_win / avg_loss), 2) if avg_loss != 0 else None,
            'reduce_triggered': reduce_triggered,
            'reduce_protected': reduce_protected,
            'clean_exit': clean_exit,
            'add_triggered': len(added_positions),
            'sell_price_mode': self.sell_price_mode,
            'start_date': self.start_date.strftime('%Y-%m-%d'),
            'end_date': self.end_date.strftime('%Y-%m-%d'),
            # v8 复利指标
            'compound': compound,
            # 基准对照（沪深300买入持有）与超额收益
            'benchmark': benchmark,
            'equity_curve_file': equity_csv,
            # 分层选股：按股票类型档的绩效对照（前端/CSV 同源）
            'tier_stats': tier_stats,
            'tier_stats_file': tier_csv_path,
        }
        # 产物写盘失败清单（如 CSV 被 Excel 占用）→ 前端据此明确告警，
        # 避免把"上次的旧文件"当成本次结果展示（见 _safe_write_csv 注释）。
        if self.export_errors:
            report['export_errors'] = list(self.export_errors)
        return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='策略回测 v8 - 验证加仓减仓逻辑（净值复利）')
    parser.add_argument('--full-market', action='store_true',
                        help='全市场回测（5000+只A股）')
    parser.add_argument('--max-stocks', type=int, default=None,
                        help='限制最大股票数量')
    parser.add_argument('--max-positions', type=int, default=None,
                        help='同时持仓上限（默认10）')
    parser.add_argument('--time-stop-days', type=int, default=None,
                        help='时间止损观察天数（默认用 quant_model.json 的 risk_params.time_stop_days）')
    parser.add_argument('--scan-interval', type=int, default=None,
                        help='新信号扫描间隔交易日（默认5）')
    parser.add_argument('--years', type=float, default=1,
                        help='回测年数（默认1年，最大5年，支持1.5=一年半）')
    parser.add_argument('--n-workers', type=int, default=None,
                        help='策略回测信号扫描并行进程数（默认 None=自动 min(cpu,4)；1=单核）')
    parser.add_argument('--start', type=str, default=None,
                        help='回测开始日期 YYYY-MM-DD（覆盖--years）')
    parser.add_argument('--end', type=str, default=None,
                        help='回测结束日期 YYYY-MM-DD（默认今天前30天）')
    parser.add_argument('--no-cache', action='store_true',
                        help='禁用磁盘缓存，重新下载所有数据')
    parser.add_argument('--config', type=str, default=None,
                        help='指定回测用的配置 JSON 路径（不修改磁盘 quant_model.json）')
    parser.add_argument('--sell-price-mode', choices=['same_close', 'next_open'], default='next_open',
                        help='卖出/加仓执行价: next_open=T+1开盘价(默认,T日收盘决策次日开盘成交,与买入对称), '
                             'same_close=信号日收盘/触发阈值价(旧默认,消除隔夜跳空但偏乐观)')
    args = parser.parse_args()

    args.years = max(0.5, min(5.0, args.years))

    # 外部配置注入（不触碰磁盘活跃配置）
    if args.config:
        import json as _json
        with open(args.config, 'r', encoding='utf-8') as _f:
            _raw = _json.load(_f)
        quant_config._cache = quant_config._validate_config(_raw)
        print(f"[config] 已加载外部配置: {args.config}")

    if args.no_cache:
        # 清理统一前复权缓存（按代码维度 cache/kline/qfq_daily_{code}.pkl）
        _kline_dir = os.path.join(get_app_dir(), 'cache', 'kline')
        _removed = 0
        if os.path.isdir(_kline_dir):
            for _f in os.listdir(_kline_dir):
                if _f.startswith('qfq_daily_') and _f.endswith('.pkl'):
                    try:
                        os.remove(os.path.join(_kline_dir, _f))
                        _removed += 1
                    except Exception:
                        pass
        print(f"已清理统一前复权缓存 {_removed} 只（cache/kline/qfq_daily_*.pkl）")

    today = datetime.now()

    # 日期范围
    end = datetime.strptime(args.end, '%Y-%m-%d') if args.end else today - timedelta(days=30)
    start = datetime.strptime(args.start, '%Y-%m-%d') if args.start else end - timedelta(days=int(args.years * 365))

    print("=" * 70)
    mode = "全市场" if args.full_market else "上证50+创业50+科创50"
    print(f"策略回测 v8 - {mode} - 净值复利模式")
    print(f"回测区间: {start.strftime('%Y-%m-%d')} ~ {end.strftime('%Y-%m-%d')} "
          f"(约{(end-start).days/365:.1f}年)")
    print("数据源: 腾讯前复权(qfq)优先，禁用未复权兜底（抓取层已拦截）")
    print("=" * 70)

    bt = StrategyBacktester(
        start_date=start.strftime('%Y-%m-%d'),
        end_date=end.strftime('%Y-%m-%d'),
        max_stocks=args.max_stocks,
        full_market=args.full_market,
        return_years=args.years,
        n_workers=args.n_workers
    )

    if args.max_positions:
        bt.MAX_POSITIONS = args.max_positions
    if args.time_stop_days:
        bt.time_stop_days = args.time_stop_days
    if args.scan_interval:
        bt.SCAN_INTERVAL_DAYS = args.scan_interval
    bt.sell_price_mode = args.sell_price_mode

    print(f"  参数: 最大持仓={bt.MAX_POSITIONS} | 时间止损天数={bt.time_stop_days}天 | "
          f"扫描间隔={bt.SCAN_INTERVAL_DAYS}个交易日")
    print(f"  初始资金: {bt.INITIAL_CAPITAL:,.0f} | 净值复利模式: ON")

    # 启动前加载内置离线缓存（仅读磁盘，不联网刷新）
    _load_disk_cache()

    bt.preload_all_data()
    report = bt.run(n_workers=args.n_workers)

    if report:
        suffix = '_fullmarket' if args.full_market else ''
        if args.years != 1:
            suffix += f'_{args.years:.0f}y'
        output_file = os.path.join(get_app_dir(),
                                   f'backtest_strategy_report_v8{suffix}.json')
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)
        print(f"\n结果已保存: {output_file}")
    print("=" * 70)
