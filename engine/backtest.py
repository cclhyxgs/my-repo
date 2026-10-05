#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""回测验证脚本 v8 - 扩大样本 + 分档复利 + 多周期IC

取数口径：统一走前复权共享 store `cache/kline/qfq_daily_{code}.pkl`（`load_qfq_daily`），
缺数时由 `_fetch_stock_data` 按当前 `kline_source` 联网补（默认通达信，失败回退腾讯）；
**不做未复权降级**。

v8 新增:
  --years N         回测年数（默认1，最大5），替代固定360天窗口
  --multi-horizon   同时测试 5/10/15/20/30 天持仓周期的IC衰减
  分档累计收益       每次采样日按评分分10档，追踪各档等权组合的累计复利收益
  年度IC稳定性       按自然年拆分样本，输出各年IC和样本量
  分档权益曲线CSV    输出 decile_equity.csv
"""
import sys
import os
import shutil

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import json
import time
import pickle
import argparse
import warnings
import requests
import random
import threading
warnings.filterwarnings('ignore')

from engine.data_layer import KLineFetcher, SCAN_KLINE_DAYS
from engine.indicators import (RSICalculator, MACDCalculator)
from engine.stock_classifier import StockClassifier
from engine.unified_entry_logic import UnifiedEntryLogic
from engine.backtest_cancel import BacktestCancelled
from engine.config import get_app_dir
from engine.stock_pool import (get_stock_pool, get_full_market_pool,
                               resolve_stock_pool)
from engine import scoring_core


# ==================== 新浪历史日K（未调用，保留备查） ====================

def fetch_sina_history(stock_code, days=SCAN_KLINE_DAYS):
    """新浪历史日K线数据获取（【未复权】原始日线，非前复权！）。

    ⛔ **本函数当前无任何调用方**（全仓 grep 确认），回测取数不再经过它 ——
    取数统一走 `_fetch_stock_data` → `load_qfq_daily` 前复权 store，未复权降级已被
    移除（未复权序列遇送转/拆细会人为跳变，混入前复权缓存会污染收益与因子评分）。

    保留原因：仅为将来需要「未经复权原始价」的离线诊断时可直接复用。
    使用 money.finance.sina.com.cn 接口，datalen 支持至 3000+（约 12 年）。"""
    try:
        url = "http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
        params = {
            'symbol': stock_code,
            'scale': 240,        # 240分钟 = 日K
            'ma': 'no',
            'datalen': days,
        }
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://finance.sina.com.cn/',
        }

        resp = requests.get(url, params=params, headers=headers, timeout=15)
        if resp.status_code != 200:
            return None, f"新浪状态码: {resp.status_code}"

        data = resp.json()
        if not data:
            return None, "新浪无K线数据"

        rows = []
        for item in data:
            try:
                rows.append({
                    'trade_time': item.get('day', ''),
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


# ==================== 缓存管理 ====================

_stock_data_cache = {}   # stock_code -> DataFrame
_precomputed_cache = {}  # stock_code -> dict(rsi_hist, macd_hists)
_date_idx_cache = {}     # stock_code -> {date_normalized: index} 日期→索引映射（O(1)查找）


def _get_index_for_date(df, date, stock_code=None):
    """快速获取日期对应的索引（O(1)查找）
    
    优先使用预构建的日期→索引映射，回退到 searchsorted。
    
    Returns:
        int: 日期对应的索引，如果未找到返回 -1
    """
    ts = pd.Timestamp(date).normalize()
    if stock_code and stock_code in _date_idx_cache:
        return _date_idx_cache[stock_code].get(ts, -1)
    idx = df['trade_time'].searchsorted(ts, side='right') - 1
    if idx >= 0 and idx < len(df):
        return idx
    return -1


def _build_date_idx_cache():
    """预构建所有股票的日期→索引映射，将 searchsorted O(log n) 改为字典 O(1)"""
    t0 = time.time()
    for stock_code in _stock_data_cache:
        df = _stock_data_cache[stock_code]
        dates = df['trade_time'].values
        date_to_idx = {pd.Timestamp(d).normalize(): i for i, d in enumerate(dates)}
        _date_idx_cache[stock_code] = date_to_idx
    elapsed = time.time() - t0
    print(f"  日期索引映射预构建完成: {len(_date_idx_cache)} 只股票，耗时 {elapsed:.2f}s")

# 文件名带 qfq 标签，明确这是【前复权】数据源缓存。
# 旧的 backtest_data_cache_sina.pkl（未复权）与之不再兼容，不会被加载（可手动删除）。
CHECKPOINT_DIR = os.path.join(get_app_dir(), 'cache', 'backtest_checkpoints')


def _auto_n_workers():
    """按机型探测(核数+内存)自动算安全并行进程数；失败回退核数-1。"""
    try:
        from engine.machine_probe import auto_n_workers
        return auto_n_workers()
    except Exception:
        return max(1, (os.cpu_count() or 1) - 1)


# 数量门槛：仅当缓存只数达到该值才允许整文件落盘。
# 事故根因：用小/空缓存 pickle.dump 整文件覆盖大盘缓存（原 241MB / 5200 只）。
# 设定 5000 作为安全下限，绝不允许用小缓存冲掉大盘缓存。
_MIN_STOCKS_TO_SAVE = 5000

# 缓存读写锁：增量刷新(后台线程)与回测落盘可能并发，串行化避免半写/竞态。
_disk_cache_lock = threading.RLock()


def _save_disk_cache():
    """统一缓存已改为按代码维度的 cache/kline/qfq_daily_{code}.pkl——由 KLineFetcher
    在抓取/增量时自动按票落盘，不再集中维护单一 backtest_data_cache_qfq.pkl 大文件。
    此处保留空实现以兼容既有调用点（preload_all_data / 并行回测落盘）。
    """
    return False


def _ensure_cache_seed():
    """首次启动（打包后）将内置缓存种子复制到持久化目录的 cache/。

    仅复制 stock_list.json（全市场代码清单，避免首启还要联网拉清单）。
    回测数据缓存已不再依赖独立的 backtest_data_cache_qfq.pkl 大文件——
    改为按代码维度的 cache/kline/qfq_daily_{code}.pkl，由全市场扫描/回测自动维护，
    无需打包/手动更新种子。
    """
    from engine.config import _get_bundled_dir
    bundled_dir = _get_bundled_dir()
    if not bundled_dir:
        return
    seed_dir = os.path.join(bundled_dir, 'cache')
    targets = [
        ('stock_list.json', os.path.join(get_app_dir(), 'cache', 'stock_list.json')),
    ]
    os.makedirs(os.path.join(get_app_dir(), 'cache'), exist_ok=True)
    for name, dst in targets:
        if os.path.exists(dst):
            continue
        seed = os.path.join(seed_dir, name)
        if not os.path.isfile(seed):
            continue
        try:
            shutil.copy2(seed, dst)
            print(f"  [缓存种子] 已从内置资源复制 {name} -> {dst}")
        except Exception as e:
            print(f"  [缓存种子] 复制 {name} 失败（用户侧将自行联网刷新）: {e}")


_ensure_cache_seed()  # 模块加载时调一次（仅 frozen + 内置种子存在时才真正执行）


def _load_disk_cache(pool=None, offline=False):
    """从统一前复权缓存 cache/kline/qfq_daily_{code}.pkl 预载股票池数据到内存。

    取代旧 backtest_data_cache_qfq.pkl 大文件：与全市场扫描共用同一 store。
    仅载入磁盘上已存在的票（不联网）；缺失票交由 preload_all_data 循环在线补/跳过。
    pool 为 None 时直接返回（兼容 _load_disk_cache() 旧调用语义）。
    """
    if pool is None:
        return True
    pool_codes = [sc for sc in pool if sc not in _stock_data_cache]
    loaded = 0
    for sc in pool_codes:
        df = KLineFetcher._load_qfq_daily_disk(sc)
        if df is not None and len(df) > 0:
            ok, vdf, reason = _validate_qfq(df)
            if ok or reason == 'vshape_repaired':
                _stock_data_cache[sc] = vdf if vdf is not None else df
                loaded += 1
    if loaded:
        print(f"  [缓存] 从统一 store 载入 {loaded} 只（池 {len(pool)} 只）")
    return True


def _full_fetch_qfq(stock_code, days=SCAN_KLINE_DAYS):
    """整段全量前复权抓取（按当前数据源分发：通达信优先，失败回退腾讯），
    返回规整后的 df 或 None。

    不再降级新浪未复权原始价（避免除权伪像污染共享缓存）；若返回未复权状
    数据则拒绝(None)，由调用方保留旧缓存。

    ⚠️ 原先硬编码 `_fetch_tencent_fq`：在 kline_source=tdx 下，回测补抓会绕过
    数据源配置又打回腾讯（撞配额），且只能拿 days 根 —— 而通达信能给全历史
    (~3102 根)。改走 `_fetch_full` 后按配置分发，下面的 `_validate_qfq` 校验
    对两个源一视同仁（tdx 自算前复权同样要过这一关）。
    """
    df, err = KLineFetcher._fetch_full(stock_code, 240, days)
    if df is not None and not df.empty:
        df = df.copy()
        df['trade_time'] = pd.to_datetime(df['trade_time'])
        df = df[df['close'] > 0].sort_values('trade_time').reset_index(drop=True)
        if not df.empty:
            ok, vdf, reason = _validate_qfq(df)
            if reason == 'unadjusted':
                return None
            if ok:
                return vdf
    return None


def _read_stock_list_codes():
    """从 stock_list.json 提取全部股票代码（用于发现新股）。"""
    try:
        import json
        p = os.path.join(get_app_dir(), 'cache', 'stock_list.json')
        if not os.path.exists(p):
            return []
        with open(p, 'r', encoding='utf-8') as f:
            d = json.load(f)
        codes = set()
        for key in ('name_to_code', 'code_to_name', 'raw_code_to_name'):
            v = d.get(key)
            if isinstance(v, dict):
                # 注意：只有 name_to_code 的值是代码(sh/sz 前缀)；
                # code_to_name / raw_code_to_name 的值是中文名，绝不能当代码收集。
                # 统一按前缀过滤，从源头杜绝把名称当代码去抓取。
                for c in v.values():
                    if isinstance(c, str) and (c.startswith('sh') or c.startswith('sz')):
                        codes.add(c)
            elif isinstance(v, list):
                for it in v:
                    if isinstance(it, str) and (it.startswith('sh') or it.startswith('sz')):
                        codes.add(it)
        return [c for c in codes if isinstance(c, str)]
    except Exception:
        return []


def _emit(msg, progress_cb=None):
    """统一进度输出：传入 progress_cb 则走回调(供 GUI 接状态栏/面板)，否则回退 print
    （CLI 直连控制台；回测运行时被 backtest_runner 的 stdout 重定向捕获进任务日志面板）。"""
    if progress_cb:
        try:
            progress_cb(msg)
            return
        except Exception:
            pass
    print(msg, flush=True)


# ==================== 快速指标序列计算 ====================

def calc_rsi_series_fast(closes, period=14):
    n = len(closes)
    if n < period + 1:
        return [50.0] * n
    rsi_series = [50.0] * n
    deltas = np.diff(np.array(closes, dtype=float))
    gains = [max(d, 0) for d in deltas[:period]]
    losses = [max(-d, 0) for d in deltas[:period]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    # rsi_series[period] = 初始 RSI（无平滑），与 calc_wilder(closes[:period+1]) 一致
    rsi_series[period] = (100.0 if avg_loss == 0
                          else max(0.0, min(100.0, 100 - (100 / (1 + (avg_gain / avg_loss))))))
    for i in range(period, len(deltas)):
        d = deltas[i]
        if d > 0:
            avg_gain = (avg_gain * (period - 1) + d) / period
            avg_loss = (avg_loss * (period - 1)) / period
        else:
            avg_gain = (avg_gain * (period - 1)) / period
            avg_loss = (avg_loss * (period - 1) + abs(d)) / period
        if avg_loss == 0:
            rsi_series[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi_series[i + 1] = max(0, min(100, 100 - (100 / (1 + rs))))
    return rsi_series


def calc_macd_hist_series_fast(closes):
    series = pd.Series(closes, dtype=float)
    ema_fast = series.ewm(span=12, adjust=False).mean()
    ema_slow = series.ewm(span=26, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    macd_signal = macd_line.ewm(span=9, adjust=False).mean()
    macd_hist = macd_line - macd_signal
    return macd_hist.tolist()


def calc_macd_acceleration_from_hist(hists):
    if len(hists) < 3:
        return 0, '数据不足'
    current = hists[-1]
    prev1 = hists[-2]
    prev2 = hists[-3]
    velocity = current - prev1
    accel = velocity - (prev1 - prev2)
    if current > 0 and velocity > 0:
        status = '多头加速'
    elif current > 0 and velocity < 0:
        status = '多头减速'
    elif current < 0 and velocity < 0:
        status = '空头加速'
    elif current < 0 and velocity > 0:
        status = '空头减速'
    else:
        status = '趋势不明'
    return accel, status


def normalize_daily_dates(df):
    """把日 K 的 trade_time 归一到「日期零点」（原地修改传入的 DataFrame）。

    ⛔ 必须归一：store 写盘来源不同，同一批 pkl 里既有 `15:00:00` 也有 `00:00:00` 口径
    （实测 `qfq_daily_sh688277.pkl` = 15:00、`qfq_daily_sh688072.pkl` = 00:00）。
    不归一会让所有 `df['trade_time'].searchsorted(date_ts, ...)` 的解析**随口径漂移一天**：
      - 15:00 口径：searchsorted(当日零点, 'right') 不含当日 bar → 用「前一日收盘」决策/估值
      - 00:00 口径：含当日 bar → 用「当日收盘」
    统一为 00:00 后，全库口径一致 = **【T 日收盘决策、T+1 开盘成交】**（用户 2026-09-19 确认），
    且 `_date_idx_cache` 分支与 searchsorted 分支结果相同（消除 cache 命中与否的隐性差异），
    同时修掉净值曲线 / 期末估值取到「前一日收盘」的一天漂移。

    只在回测数据入口（`_validate_qfq`）调用，不动实时分析链路。
    """
    try:
        if df is not None and 'trade_time' in getattr(df, 'columns', ()):
            df['trade_time'] = pd.to_datetime(df['trade_time']).dt.normalize()
    except Exception:
        pass
    return df


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
    normalize_daily_dates(sub)   # ⛔ 统一 trade_time 口径（见 normalize_daily_dates）
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


def _fetch_stock_data(stock_code, days=SCAN_KLINE_DAYS, offline=True):
    """单只股票日线获取 —— 复用统一前复权缓存 cache/kline/qfq_daily_{code}.pkl。

    与全市场扫描共用同一 store（扫描写入、回测读取），不再依赖独立的
    backtest_data_cache_qfq.pkl 大文件。    命中且有效直接返回；缺失（未扫描/扫描未覆盖）直接返回 None，绝不在线补抓
    （offline 恒为 True，仅复用扫描写入的 qfq_daily_{code}.pkl）。过 _validate_qfq 门禁：
    未复权/无效 → 排除（不降级、不污染）。
    """
    if stock_code in _stock_data_cache:
        cached = _stock_data_cache[stock_code]
        ok, vdf, reason = _validate_qfq(cached)
        if reason == 'vshape_repaired':
            _stock_data_cache[stock_code] = vdf
            return vdf
        if reason == 'unadjusted':
            print(f"  [回测] {stock_code} 缓存为未复权脏数据，已排除")
            return None
        if ok:
            return cached
        print(f"  [回测] {stock_code} 缓存无效（{reason}），已排除")
        return None
    df = KLineFetcher.load_qfq_daily(stock_code, days, offline=True)
    if df is None:
        return None
    ok, vdf, reason = _validate_qfq(df)
    if reason == 'unadjusted':
        print(f"  [回测] {stock_code} 未复权，已排除")
        return None
    if ok or reason == 'vshape_repaired':
        return vdf if vdf is not None else df
    print(f"  [回测] {stock_code} 数据无效（{reason}），已排除")
    return None


def _preload_parallel(pool, n_threads=8):
    """严格离线预加载：仅从内置磁盘缓存载入，未命中的股票直接排除，绝不联网。

    回测设计：完全零联网，只使用程序打包内置的全市场前复权缓存包。
    原并行联网抓取(parallel_fetch)路径已移除——与"回测仅读内置缓存"一致。
    """
    if not _stock_data_cache:
        _load_disk_cache(pool=[sc for sc, _, _ in pool])
    to_load = [sc for sc, rc, bd in pool if sc not in _stock_data_cache]
    loaded = len(_stock_data_cache)
    if to_load:
        print(f"  [离线回测] {len(to_load)} 只不在内置缓存包中，已排除（不联网）")
    return loaded, len(to_load)


# ==================== Backtester 类 ====================

class Backtester:
    def __init__(self, start_date, end_date, hold_days, sample_interval, max_stocks=None,
                 full_market=False, return_years=1, n_workers=None, pool=None, offline_mode=False):
        self.start_date = datetime.strptime(start_date, '%Y-%m-%d')
        self.end_date = datetime.strptime(end_date, '%Y-%m-%d')
        self.hold_days = hold_days
        self.sample_interval = sample_interval
        self.n_workers = n_workers
        self.max_stocks = max_stocks
        self.return_years = return_years  # v8: 用于年化IC等计算
        self.offline_mode = offline_mode  # 离线模式：未命中缓存时不联网抓取
        self.results = []
        self.failed_stocks = []
        # 输出路径（工具模式可加前缀，避免覆盖规范产物；None=使用原固定文件名）
        self.decile_csv_path = None
        self.report_json_path = None
        self.cancel_event = None  # 由 runner 注入的 threading.Event，用于中止

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

    def _fetch_stock_data(self, stock_code, days=SCAN_KLINE_DAYS):
        """单只股票日线获取 —— 复用统一前复权缓存（与全市场扫描共用 store）。

        委托模块级 _fetch_stock_data（按代码维度 cache/kline/qfq_daily_{code}.pkl），
        缺失时在线补抓（offline_mode 下跳过）。
        """
        return globals()['_fetch_stock_data'](stock_code, days, offline=True)

    def _precompute_series(self, stock_code, df):
        closes = df['close'].tolist()
        _precomputed_cache[stock_code] = {
            'rsi_hist': calc_rsi_series_fast(closes, 14),
            'macd_hists': calc_macd_hist_series_fast(closes),
        }

    def preload_all_data(self):
        """预加载所有股票数据"""
        if not _stock_data_cache:
            _load_disk_cache(pool=[sc for sc, _, _ in self.stock_pool])

        to_load = [(sc, rc, bd) for sc, rc, bd in self.stock_pool if sc not in _stock_data_cache]
        if not to_load:
            print(f"  所有 {len(self.stock_pool)} 只股票数据已就绪（全部命中缓存）")
            return len(_stock_data_cache)

        print(f"\n{'='*70}")
        if self.offline_mode:
            print(f"预加载 {len(to_load)} 只股票日线数据（共{len(self.stock_pool)}只，缓存命中{len(_stock_data_cache)}只）...")
            print(f"【离线模式】未命中缓存的股票直接跳过，不联网抓取")
        else:
            print(f"预加载 {len(to_load)} 只股票日线数据（共{len(self.stock_pool)}只，缓存命中{len(_stock_data_cache)}只）...")
            print(f"数据源: 腾讯前复权")
        print(f"{'='*70}")

        success = 0
        failed = []
        skipped_offline = 0
        for idx, (stock_code, raw_code, board) in enumerate(to_load):
            if self.offline_mode:
                # 离线模式：直接标记为失败，不联网、不重试、不 sleep
                failed.append(stock_code)
                skipped_offline += 1
                continue
            df = self._fetch_stock_data(stock_code)
            if df is not None:
                self._precompute_series(stock_code, df)
                success += 1
            else:
                failed.append(stock_code)
            if (idx + 1) % 50 == 0:
                print(f"  进度: {idx+1}/{len(to_load)} | 成功: {success} | 失败: {len(failed)} | 缓存总计: {len(_stock_data_cache)}")
                _save_disk_cache()
            # 降低单次请求间隔（原 0.3~0.5s，全市场 5000 只累计可达 30+ 分钟）
            # 数据经 KLineFetcher 写入统一 store cache/kline/qfq_daily_{code}.pkl（前复权），
            # 后续重跑命中该 store 不再联网；_save_disk_cache() 现已是空实现，仅保调用点兼容。
            time.sleep(0.1 + random.random() * 0.05)

        if not self.offline_mode:
            _save_disk_cache()

        # —— 预构建日期→索引映射（性能优化）——
        _build_date_idx_cache()

        print(f"  完成: 本次成功 {success}/{len(to_load)} | 缓存总计: {len(_stock_data_cache)}只")
        if self.offline_mode and skipped_offline:
            print(f"  离线跳过: {skipped_offline} 只（未命中缓存，未联网）")
        if failed:
            print(f"  失败股票 ({len(failed)}只): {failed[:10]}{'...' if len(failed)>10 else ''}")
            self.failed_stocks = failed
        return len(_stock_data_cache)

    def calculate_up_ratio(self, date):
        """从股票池计算当日市场上涨比例"""
        ts = pd.Timestamp(date)
        up_count = 0
        total_count = 0
        for stock_code, _, _ in self.stock_pool:
            df = _stock_data_cache.get(stock_code)
            if df is None:
                continue
            idx = _get_index_for_date(df, ts, stock_code)
            if idx < 1:
                continue
            if df.iloc[idx]['close'] > df.iloc[idx - 1]['close']:
                up_count += 1
            total_count += 1
        return up_count / total_count if total_count > 0 else 0.5

    def calculate_score_and_classify(self, df, date, stock_code, up_ratio):
        return _bt_compute_score_classify(df, date, stock_code, up_ratio)

    def get_future_return(self, df, date, days):
        return _get_future_return(df, date, days)

    def _save_checkpoint(self, tag=''):
        try:
            os.makedirs(CHECKPOINT_DIR, exist_ok=True)
            fname = f'checkpoint_h{self.hold_days}{tag}.json'
            path = os.path.join(CHECKPOINT_DIR, fname)
            serializable = []
            for r in self.results:
                sr = {k: v for k, v in r.items()
                      if k in ('stock_code', 'stock_score', 'final_score', 'entry_action',
                               'stock_type', 'type_label', 'date', 'price',
                               'future_return', 'board')}
                serializable.append(sr)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(serializable, f, ensure_ascii=False, default=str)
        except Exception as e:
            print(f"  检查点保存失败: {e}")

    def run(self, n_workers=None):
        """主回测流程（v8: 增加分档追踪）。仅使用现有磁盘缓存（内置离线包），不联网刷新。"""
        dates = []
        current = self.start_date
        while current <= self.end_date:
            if current.weekday() < 5:
                dates.append(current)
            current += timedelta(days=self.sample_interval)

        # 过滤掉非交易日（法定节假日等），用已有股票缓存中的实际交易日历
        if _stock_data_cache:
            first_df = next((v for v in _stock_data_cache.values() if v is not None and not v.empty), None)
            if first_df is not None:
                trade_dates = set(pd.Timestamp(d).normalize() for d in first_df['trade_time'])
                dates = [d for d in dates if pd.Timestamp(d).normalize() in trade_dates]

        years_span = (self.end_date - self.start_date).days / 365.25

        print(f"\n{'='*70}")
        print(f"回测: 持有{self.hold_days}天 | 采样{len(dates)}个日期 | {len(self.stock_pool)}只股票")
        print(f"回测区间: {self.start_date.strftime('%Y-%m-%d')} ~ {self.end_date.strftime('%Y-%m-%d')} ({years_span:.1f}年)")
        try:
            from engine.machine_probe import describe_machine
            print(describe_machine())
        except Exception:
            pass
        print(f"{'='*70}")
        if self.failed_stocks:
            print(f"⚠ 注意: {len(self.failed_stocks)}只股票数据获取失败，将跳过")

        start_time = time.time()

        n_pool = len(self.stock_pool)
        n_workers = (n_workers if n_workers is not None else self.n_workers) or _auto_n_workers()
        use_parallel = n_workers > 1 and n_pool >= 50

        # 常驻进程池：整个采样日期循环只建一次，每个 worker 仅加载一次 241MB 缓存
        # （修复旧实现：进程池原本在「每个采样日」循环内反复创建 + worker 重加载缓存，
        #  采样日越多浪费越夸张）。
        executor = None
        if use_parallel:
            import multiprocessing as _mp
            from concurrent.futures import ProcessPoolExecutor
            _ctx = _mp.get_context('spawn')
            try:
                from engine import quant_config as _qc_bt
                _qcfg_bt = _qc_bt._cache
                executor = ProcessPoolExecutor(max_workers=n_workers, mp_context=_ctx,
                                                initializer=_bt_init_worker,
                                                initargs=([sc for sc, _, _ in self.stock_pool], _qcfg_bt))
                print(f"  [评分IC] 常驻进程池已建（{n_workers} worker，缓存每 worker 仅加载一次）")
            except Exception as e:
                print(f"  [评分IC] 进程池创建失败，回退串行: {e}")
                executor = None
                use_parallel = False

        try:
            for idx, date in enumerate(dates):
                # 用户点击「停止」-> 干净中止
                if self.cancel_event is not None and self.cancel_event.is_set():
                    print("⏹ 已收到停止信号，回测中止。")
                    raise BacktestCancelled()

                up_ratio = self.calculate_up_ratio(date)
                if use_parallel:
                    date_results = self._scan_date_parallel(date, up_ratio, n_workers, executor)
                else:
                    date_results = self._scan_date_serial(date, up_ratio)

                elapsed = time.time() - start_time
                done = idx + 1
                remaining = elapsed / done * (len(dates) - done) if done > 0 else 0
                print(f"  日期 {done}/{len(dates)} ({date.strftime('%Y-%m-%d')}) | "
                      f"当日: {date_results} | 总样本: {len(self.results)} | "
                      f"耗时: {elapsed:.0f}s | 剩余: {remaining:.0f}s")

                if done % 5 == 0:
                    self._save_checkpoint()
        finally:
            # 回测结束（正常或取消）统一回收常驻进程池
            if executor is not None:
                executor.shutdown(wait=True)

        self._save_checkpoint('_final')

        total_time = time.time() - start_time
        print(f"\n  总样本: {len(self.results)} | 总耗时: {total_time:.1f}s "
              f"({total_time/60:.1f}分钟)")
        return self.generate_report(years_span)

    def _scan_date_serial(self, date, up_ratio):
        """单日期串行评分（原逻辑）：返回当日新增样本数。"""
        n_pool = len(self.stock_pool)
        date_results = 0
        for j, (stock_code, raw_code, board) in enumerate(self.stock_pool):
            if stock_code in self.failed_stocks:
                continue
            df = _stock_data_cache.get(stock_code)
            if df is None:
                continue
            result = _bt_compute_score_classify(df, date, stock_code, up_ratio)
            if result is None:
                continue
            future_return = _get_future_return(df, date, self.hold_days)
            if future_return is None:
                continue
            result['future_return'] = future_return
            result['board'] = board
            self.results.append(result)
            date_results += 1
            if (j + 1) % 2000 == 0 or (j + 1) == n_pool:
                print(f"    个股 {j+1}/{n_pool} | 当日已得 {date_results} 样本")
        return date_results

    def _scan_date_parallel(self, date, up_ratio, n_workers, executor):
        """单日期多核评分（复用常驻进程池）：返回当日新增样本数。

        executor 为 None 时回退串行；否则复用 run() 创建的常驻池。
        """
        from concurrent.futures import as_completed
        if executor is None:
            return self._scan_date_serial(date, up_ratio)
        pool = [(sc, rc, bd) for sc, rc, bd in self.stock_pool if sc not in self.failed_stocks]
        chunk = max(50, len(pool) // (n_workers * 6))
        shards = [pool[i:i + chunk] for i in range(0, len(pool), chunk)]
        done = 0
        date_results = 0
        try:
            futures = [executor.submit(_bt_score_shard, sh, date, up_ratio, self.hold_days)
                       for sh in shards]
            for fut in as_completed(futures):
                if self.cancel_event is not None and self.cancel_event.is_set():
                    for f in futures:
                        f.cancel()
                    break
                try:
                    rows = fut.result()
                except Exception:
                    rows = []
                for r in rows:
                    self.results.append(r)
                    date_results += 1
                done += 1
                if done % 10 == 0 or done == len(shards):
                    print(f"    个股分片 {done}/{len(shards)} | 当日已得 {date_results} 样本")
        except BacktestCancelled:
            raise
        except Exception as e:
            print(f"    [多核] 汇总异常，本日期回退串行: {e}")
            date_results += self._scan_date_serial(date, up_ratio)
        return date_results

    def _compute_decile_returns(self, df):
        """v8: 按每个采样日内的评分排名分10档，计算各档等权收益"""
        decile_records = []
        for date, group in df.groupby('date'):
            if len(group) < 20:
                continue
            # 按 final_score 排名
            ranked = group.sort_values('final_score', ascending=False)
            n = len(ranked)
            # 分10档，每档等权
            for d in range(10):
                lo = int(n * d / 10)
                hi = int(n * (d + 1) / 10)
                if hi <= lo:
                    continue
                bucket = ranked.iloc[lo:hi]
                decile_records.append({
                    'date': date,
                    'decile': d + 1,  # 1=最低分, 10=最高分
                    'avg_return': bucket['future_return'].mean(),
                    'count': len(bucket),
                    'avg_score': bucket['final_score'].mean(),
                })
        return pd.DataFrame(decile_records)

    def generate_report(self, years_span=1.0):
        """生成回测报告（v8: 增加分档复利、年度IC、多周期IC）"""
        if not self.results:
            print("无有效回测数据！")
            return {'error': '无数据'}

        df = pd.DataFrame(self.results)

        print("\n" + "=" * 70)
        print(f"回测验证报告 v8 - 持有{self.hold_days}天（数据源：腾讯前复权）")
        print("=" * 70)
        print(f"\n总样本: {len(df)}")
        print(f"回测跨度: {years_span:.1f}年")
        print(f"评分范围: {df['final_score'].min():.0f} ~ {df['final_score'].max():.0f}")
        print(f"未来{self.hold_days}日收益均值: {df['future_return'].mean()*100:.2f}%")
        print(f"胜率: {(df['future_return'] > 0).mean()*100:.1f}%")

        # ========== v8 新增: 分档累计复利 ==========
        print("\n" + "-" * 70)
        print("分档累计复利收益（按评分10分档，等权组合逐期复利）")
        print("-" * 70)

        decile_df = self._compute_decile_returns(df)
        decile_stats = {}
        if not decile_df.empty:
            # 每档按日期排序
            for d in range(1, 11):
                bucket = decile_df[decile_df['decile'] == d].sort_values('date')
                if len(bucket) == 0:
                    continue
                # 复利累计收益 = ∏(1 + r_i) - 1
                compound_return = np.prod(1 + bucket['avg_return'].values) - 1
                avg_period = bucket['avg_return'].mean()
                n_periods = len(bucket)
                # 年化: (1 + compound)^(1/years) - 1
                annualized = (1 + compound_return) ** (1 / max(years_span, 0.25)) - 1
                decile_stats[d] = {
                    'decile': d,
                    'avg_return': avg_period * 100,
                    'compound_return': compound_return * 100,
                    'annualized': annualized * 100,
                    'n_periods': n_periods,
                    'win_rate': (bucket['avg_return'] > 0).mean() * 100,
                }
                print(f"  第{d:2d}档 (n={n_periods:>4}期) | 单期均值: {avg_period*100:>6.2f}% | "
                      f"累计复利: {compound_return*100:>7.2f}% | 年化: {annualized*100:>6.2f}% | "
                      f"胜率: {decile_stats[d]['win_rate']:.1f}%")

            # Top-Bottom 差值
            if 10 in decile_stats and 1 in decile_stats:
                spread = decile_stats[10]['compound_return'] - decile_stats[1]['compound_return']
                spread_ann = decile_stats[10]['annualized'] - decile_stats[1]['annualized']
                print(f"\n  分档极差: 累计 {spread:.2f}% | 年化 {spread_ann:.2f}%")
                print(f"  {'[有效] 高评分显著跑赢低评分' if spread > 5 else '[弱效] 评分区分度有限' if spread > 0 else '[无效] 高评分跑输低评分'}")

            # 保存分档权益曲线
            decile_curve = []
            for d in range(1, 11):
                bucket = decile_df[decile_df['decile'] == d].sort_values('date')
                equity = 1.0
                for _, row in bucket.iterrows():
                    equity *= (1 + row['avg_return'])
                    decile_curve.append({
                        'date': str(row['date'])[:10],
                        'decile': d,
                        'equity': round(equity, 6),
                    })
            decile_csv = self.decile_csv_path or os.path.join(get_app_dir(),
                                      f'decile_equity_h{self.hold_days}.csv')
            pd.DataFrame(decile_curve).to_csv(decile_csv, index=False)
            print(f"\n  分档权益曲线已保存: {decile_csv}")

        # ========== v8 新增: 年度IC稳定性 ==========
        print("\n" + "-" * 70)
        print("年度IC稳定性（Rank IC，按自然年拆分）")
        print("-" * 70)
        print(f"  {'年份':<8} {'样本数':>8} {'Rank IC':>8} {'Pearson r':>10}")
        print("  " + "-" * 40)

        df['year'] = pd.to_datetime(df['date']).dt.year
        annual_ic = []
        for year, group in df.groupby('year'):
            if len(group) < 100:
                continue
            # Rank IC = Spearman correlation (use rank-based)
            rank_ic = group['final_score'].rank().corr(group['future_return'].rank())
            pearson = group['final_score'].corr(group['future_return'])
            annual_ic.append({'year': int(year), 'rank_ic': rank_ic, 'pearson': pearson, 'samples': len(group)})
            print(f"  {year:<8} {len(group):>8} {rank_ic:>8.4f} {pearson:>10.4f}")

        if annual_ic:
            avg_rank_ic = np.mean([a['rank_ic'] for a in annual_ic])
            std_rank_ic = np.std([a['rank_ic'] for a in annual_ic])
            print(f"\n  平均 Rank IC: {avg_rank_ic:.4f}  |  标准差: {std_rank_ic:.4f}")
            print(f"  IC_IR (IC/Std): {avg_rank_ic/std_rank_ic:.2f}" if std_rank_ic > 0 else "  IC_IR: N/A")
            # 判断稳定性
            ics = [a['rank_ic'] for a in annual_ic]
            n_neg = sum(1 for ic in ics if ic < 0)
            if n_neg == 0 and std_rank_ic < 0.02:
                print(f"  [稳定正IC] 各年IC全正，波动小，评分系统跨年度一致有效")
            elif n_neg == 0:
                print(f"  [正IC但有波动] 各年IC全正但波动较大")
            elif n_neg <= len(annual_ic) // 3:
                print(f"  [IC不稳定] {n_neg}/{len(annual_ic)}年出现负IC，评分系统受市场环境影响")
            else:
                print(f"  [IC严重退化] 多数年份IC为负，评分系统可能存在过拟合")

        # ============================================================
        # 以下为原有统计（保持不变）
        # ============================================================

        # 1. 按工具股票分类统计
        print("\n" + "-" * 70)
        print("按工具股票分类统计（StockClassifier）")
        print("-" * 70)
        print(f"{'分类':<14} {'样本数':>6} {'平均收益':>8} {'胜率':>7} {'最大收益':>8} {'最小收益':>8} {'标准差':>7}")
        print("  " + "-" * 62)

        type_labels = {
            'strong': '强势股', 'standard': '标准股', 'test': '试探股',
            'pending': '待确认股', 'cautious': '警惕股', 'weak': '弱势股',
            'weak_rebound': '博反弹', 'panic_rebound': '恐慌反转',
            'panic_avoid': '恐慌回避', 'unknown': '未知'
        }

        type_stats = []
        for type_key, label in type_labels.items():
            group = df[df['stock_type'] == type_key]
            if len(group) > 0:
                avg_ret = group['future_return'].mean() * 100
                win_rate = (group['future_return'] > 0).mean() * 100
                rets = group['future_return'] * 100
                stats = {
                    'type': type_key, 'label': label, 'count': len(group),
                    'avg_return': avg_ret, 'win_rate': win_rate,
                    'max_return': rets.max(), 'min_return': rets.min(),
                    'std': rets.std()
                }
                type_stats.append(stats)
                print(f"  {label:<12} {len(group):>6} {avg_ret:>7.2f}% {win_rate:>6.1f}% "
                      f"{rets.max():>7.2f}% {rets.min():>7.2f}% {rets.std():>6.2f}%")

        # 2. 按入场信号分类统计
        print("\n" + "-" * 70)
        print("按入场信号分类统计")
        print("-" * 70)
        print(f"{'信号':<14} {'样本数':>6} {'平均收益':>8} {'胜率':>7}")
        print("  " + "-" * 40)

        action_labels = {
            '关注建仓': '关注建仓',
            '观望仓': '观望仓',
            '博反弹': '博反弹', '空仓观望': '空仓观望'
        }

        action_stats = []
        for action, label in action_labels.items():
            group = df[df['entry_action'] == action]
            if len(group) > 0:
                avg_ret = group['future_return'].mean() * 100
                win_rate = (group['future_return'] > 0).mean() * 100
                action_stats.append({
                    'action': action, 'label': label, 'count': len(group),
                    'avg_return': avg_ret, 'win_rate': win_rate
                })
                print(f"  {label:<12} {len(group):>6} {avg_ret:>7.2f}% {win_rate:>6.1f}%")

        # 3. 强势 vs 弱势 交叉分析
        print("\n" + "-" * 70)
        print("强势 vs 弱势 交叉分析")
        print("-" * 70)

        strong_types = ['strong', 'standard']
        weak_types = ['weak', 'weak_rebound', 'cautious']
        strong_group = df[df['stock_type'].isin(strong_types)]
        weak_group = df[df['stock_type'].isin(weak_types)]

        strong_avg = weak_avg = 0
        if len(strong_group) > 0:
            strong_avg = strong_group['future_return'].mean() * 100
            strong_win = (strong_group['future_return'] > 0).mean() * 100
            print(f"  强势/标准股: {len(strong_group):>5}样本  收益{strong_avg:>6.2f}%  胜率{strong_win:>6.1f}%")
        if len(weak_group) > 0:
            weak_avg = weak_group['future_return'].mean() * 100
            weak_win = (weak_group['future_return'] > 0).mean() * 100
            print(f"  弱势/警惕股: {len(weak_group):>5}样本  收益{weak_avg:>6.2f}%  胜率{weak_win:>6.1f}%")
        if len(strong_group) > 0 and len(weak_group) > 0:
            diff = strong_avg - weak_avg
            print(f"  收益差: {diff:>6.2f}%  {'(强势优于弱势)' if diff > 0 else '(弱势反超)'}")

        # 4. 评分分档统计
        print("\n" + "-" * 70)
        print("评分分档统计")
        print("-" * 70)
        print(f"{'评分区间':<12} {'样本数':>6} {'平均收益':>8} {'胜率':>7}")
        print("  " + "-" * 40)

        score_bins = [(45, 100, '45+强势'), (25, 45, '25-45标准'),
                      (12, 25, '12-25试探'), (5, 12, '5-12观察'),
                      (-5, 5, '-5~5中性'), (-20, -5, '<-5弱势')]
        for lo, hi, label in score_bins:
            group = df[(df['final_score'] >= lo) & (df['final_score'] < hi)]
            if len(group) > 0:
                avg_ret = group['future_return'].mean() * 100
                win_rate = (group['future_return'] > 0).mean() * 100
                print(f"  {label:<10} {len(group):>6} {avg_ret:>7.2f}% {win_rate:>6.1f}%")

        # 5. 评分分档统计（同时写入报告 dict，供全市场分析[历史参考]读取）
        print("\n" + "-" * 70)
        print("评分分档统计")
        print("-" * 70)
        print(f"{'评分区间':<12} {'样本数':>6} {'平均收益':>8} {'胜率':>7}")
        print("  " + "-" * 40)

        score_bins = [(10, 100, '强势(≥10)'), (1, 10, '标准(1~10)'),
                          (-4, 1, '试探(-4~1)'), (-10, -4, '待确认(-10~-4)'),
                          (-100, -5, '博反弹/弱势(<-5)')]
        score_bins_stats = []
        for lo, hi, label in score_bins:
            if hi == 100:
                group = df[df['final_score'] >= lo]
            else:
                group = df[(df['final_score'] >= lo) & (df['final_score'] < hi)]
            if len(group) > 0:
                avg_ret = group['future_return'].mean() * 100
                win_rate = (group['future_return'] > 0).mean() * 100
                print(f"  {label:<10} {len(group):>6} {avg_ret:>7.2f}% {win_rate:>6.1f}%")
                score_bins_stats.append({
                    'label': label, 'lo': lo, 'hi': hi,
                    'count': int(len(group)),
                    'avg_return': round(float(avg_ret), 2),
                    'win_rate': round(float(win_rate), 1),
                })

        # 5b. 按板块统计
        print("\n" + "-" * 70)
        print("按股票板块统计")
        print("-" * 70)
        print(f"{'板块':<10} {'样本数':>6} {'平均收益':>8} {'胜率':>7}")
        print("  " + "-" * 40)

        for board in sorted(df['board'].unique()):
            group = df[df['board'] == board]
            if len(group) > 0:
                avg_ret = group['future_return'].mean() * 100
                win_rate = (group['future_return'] > 0).mean() * 100
                print(f"  {board:<8} {len(group):>6} {avg_ret:>7.2f}% {win_rate:>6.1f}%")

        # 6. 评分与收益相关性
        corr = df['final_score'].corr(df['future_return'])
        print("\n" + "-" * 70)
        print("评分与收益相关性")
        print("-" * 70)
        print(f"  Pearson相关系数: {corr:.4f}")
        if corr > 0.1:
            print("  [正有效] 评分越高，未来收益越高，评分系统有预测力")
        elif corr > 0.05:
            print("  [弱正相关] 评分有一定预测力但不强")
        elif corr > 0:
            print("  [极弱正相关] 评分预测力很弱")
        else:
            print("  [负相关/无关] 评分系统无预测力")

        # 7. 结论
        print("\n" + "=" * 70)
        print("综合结论")
        print("=" * 70)

        if len(strong_group) > 0 and len(weak_group) > 0:
            diff = strong_avg - weak_avg
            if diff > 2 and corr > 0.05:
                print(f"  评分系统有效: 强势股收益({strong_avg:.2f}%) 显著高于弱势股({weak_avg:.2f}%)")
            elif diff > 0:
                print(f"  评分系统有一定效果: 强势股收益略高于弱势股({diff:.2f}%)")
            else:
                print(f"  评分系统效果不明显")

        # v8: 分档结轮
        if 10 in decile_stats and 1 in decile_stats:
            top_ann = decile_stats[10]['annualized']
            bot_ann = decile_stats[1]['annualized']
            if top_ann - bot_ann > 5:
                print(f"  分档有显著区分度: Top年化{top_ann:.1f}% vs Bottom年化{bot_ann:.1f}%")
            else:
                print(f"  分档区分度有限: Top-Bottom年化差{top_ann-bot_ann:.1f}%")

        if annual_ic:
            print(f"  年度IC稳定性: 均值{avg_rank_ic:.4f} / 标准差{std_rank_ic:.4f}")

        print("\n" + "=" * 70)

        report = {
            'hold_days': self.hold_days,
            'total_samples': len(df),
            'years_span': round(years_span, 2),
            'correlation': round(corr, 4),
            'overall_win_rate': round((df['future_return'] > 0).mean() * 100, 1),
            'avg_return': round(df['future_return'].mean() * 100, 2),
            'strong_group_return': round(strong_avg, 2),
            'weak_group_return': round(weak_avg, 2),
            'return_diff': round(strong_avg - weak_avg, 2),
            'type_stats': type_stats,
            'action_stats': action_stats,
            'score_bins': score_bins_stats,
            'sample_dates': len(df['date'].unique()),
            'stock_count': len(df['stock_code'].unique()),
            # v8 新增
            'decile_stats': {str(k): v for k, v in decile_stats.items()},
            'annual_ic': annual_ic,
            'avg_rank_ic': round(avg_rank_ic, 4) if annual_ic else None,
            'ic_std': round(std_rank_ic, 4) if annual_ic else None,
        }
        return report


# ========== 多核评分回测：模块级评分内核（与 backtest_strategy.compute_full_analysis 同款 V5 逻辑）==========

# 评分/分类异常诊断计数器（仅前几次打印，避免刷屏）
_bt_score_err_count = [0]


def _bt_compute_score_classify(df, date, stock_code, up_ratio):
    """模块级 V5 评分+分类（与 compute_full_analysis 同款引擎调用，输出回测所需字段）。

    为支持 spawn 多进程，必须是模块级函数且不引用 self。
    逐项复刻 backtest_strategy.compute_full_analysis 的评分核心：
    tech/market 构建 → calc_stock_score → UnifiedScorer → UnifiedEntryLogic → StockClassifier。
    """
    ts = pd.Timestamp(date).normalize()
    if stock_code and stock_code in _date_idx_cache:
        idx = _date_idx_cache[stock_code].get(ts, -1) + 1
    else:
        idx = df['trade_time'].searchsorted(ts, side='right')
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
        macd_hists = MACDCalculator.calc_hist_series(closes)
        rsi_hist = RSICalculator.calc_series(closes, 14)
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

        return {
            'stock_code': stock_code,
            'stock_score': stock_score,
            'final_score': final_score,
            'entry_action': entry_result['action'],
            'entry_position': entry_result['position'],
            'stock_type': classification.get('type', 'unknown'),
            'type_label': classification.get('type_label', '未知'),
            'date': date,
            'price': closes[-1],
        }
    except Exception as _e:
        # 评分/分类失败：不再静默吞掉。前 3 次打印诊断（避免 142×53 刷屏），
        # 便于定位"全 0 样本"的根因（最常见：方案不完整，None 参数参与运算抛异常）。
        if _bt_score_err_count[0] < 3:
            _bt_score_err_count[0] += 1
            print(f"  [评分异常] {stock_code}@{date}: {type(_e).__name__}: {_e}")
        return None


def _get_future_return(df, date, days, stock_code=None):
    """模块级未来收益（与 _bt_compute_score_classify 同款 idx 约定：当前行 = searchsorted 右边界-1）。"""
    try:
        ts = pd.Timestamp(date).normalize()
        if stock_code and stock_code in _date_idx_cache:
            cur_row = _date_idx_cache[stock_code].get(ts, -1)
        else:
            cur_row = df['trade_time'].searchsorted(ts, side='right') - 1
        if cur_row < 0:
            return None
        future_idx = cur_row + days
        if future_idx >= len(df):
            return None
        cur = float(df['close'].iloc[cur_row])
        fut = float(df['close'].iloc[future_idx])
        if cur <= 0:
            return None
        return (fut - cur) / cur
    except Exception:
        return None


# ========== 多核评分回测（进程池 worker，模块级可 pickle）==========

def _bt_score_shard(shard, date, up_ratio, hold_days):
    """进程池 worker：处理一个股票分片，返回该日期的有效 result 列表。"""
    out = []
    for stock_code, raw_code, board in shard:
        df = _stock_data_cache.get(stock_code)
        if df is None:
            continue
        res = _bt_compute_score_classify(df, date, stock_code, up_ratio)
        if res is None:
            continue
        fr = _get_future_return(df, date, hold_days)
        if fr is None:
            continue
        res['future_return'] = fr
        res['board'] = board
        out.append(res)
    return out


def _bt_init_worker(pool_codes=None, qcfg=None):
    """每个 worker 进程启动载入自己的离线缓存（与 parallel_utils.init_worker 一致）。

    pool_codes: 若提供，worker 只验证+载入池内股票，冷启动验证循环从全市场降到池内数量。
    qcfg: 主进程注入到 quant_config._cache 的当前激活方案配置。spawn 出的 worker 是独立
          进程，不会继承主进程的 quant_config._cache（默认为 None）。若不传，worker 内
          score_calculator_v2 调 quant_config.require_config(quant_config._safe_cfg(), ...)
          会因 _cache=None 抛 ConfigIncompleteError，被 backtest._bt_compute_score_classify
          的 except 静默吞掉 → 0 样本。此处透传主进程 _cache，使并行与串行用相同方案配置。

    关键修复：worker 是独立 spawn 进程，必须自行从磁盘重载缓存。此前仅调用
    _load_disk_cache（只读 cache/kline/qfq_daily_{code}.pkl），在「共享 store 为空、真实数据
    在旧扫描 cache/kline/kline_{hash}.pkl」的机器上会全部落空 → 并行分支候选为 0 →
    0 样本（串行分支因主进程已预载而正常）。此处缺口用 KLineFetcher.load_qfq_daily
    （含 kline_{hash}.pkl 回退）补齐，与主进程 _fetch_stock_data 口径一致。
    """
    # 透传方案配置：并行 worker 是独立进程，必须显式注入 quant_config._cache
    if qcfg is not None:
        from engine import quant_config as _qc
        _qc._cache = qcfg

    if not _stock_data_cache:
        _load_disk_cache(pool=pool_codes)
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

if __name__ == "__main__":
    # 强制 stdout/stderr 行缓冲：管道/重定向下进度行不再被块缓冲憋住（看着像卡死的根因）
    import sys as _sys
    for _s in (_sys.stdout, _sys.stderr):
        if hasattr(_s, 'reconfigure'):
            try:
                _s.reconfigure(line_buffering=True)
            except Exception:
                pass
    parser = argparse.ArgumentParser(description='回测验证脚本 v8 - V5评分系统可靠性验证（新浪数据源）')
    parser.add_argument('--full-market', action='store_true',
                        help='全市场回测（5000+只A股）')
    parser.add_argument('--max-stocks', type=int, default=None,
                        help='限制最大股票数量（测试用）')
    parser.add_argument('--hold-days', type=int, nargs='+', default=[10],
                        help='持仓天数列表（默认仅10天）。配合--multi-horizon可测试多个周期')
    parser.add_argument('--multi-horizon', action='store_true',
                        help='同时测试 5/10/15/20/30 天四个周期，观察IC随持仓时间衰减')
    parser.add_argument('--sample-interval', type=int, default=5,
                        help='采样间隔天数（默认5，多年回测建议10以减少计算量）')
    parser.add_argument('--n-workers', type=int, default=None,
                        help='评分回测并行进程数（默认 None=自动 min(cpu,4)；1=单核）')
    parser.add_argument('--years', type=float, default=1,
                        help='回测年数（默认1年，最大5年，支持小数如1.5=一年半）')
    parser.add_argument('--start', type=str, default=None,
                        help='回测开始日期 YYYY-MM-DD（会覆盖--years设置）')
    parser.add_argument('--end', type=str, default=None,
                        help='回测结束日期 YYYY-MM-DD（默认今天前30天）')
    parser.add_argument('--no-cache', action='store_true',
                        help='禁用磁盘缓存，重新下载所有数据')
    args = parser.parse_args()

    # 限制 years 范围
    args.years = max(0.5, min(5.0, args.years))

    if args.no_cache:
        # 统一前复权缓存已改为按代码维度的 cache/kline/qfq_daily_{code}.pkl，
        # 不再有单一的 backtest_data_cache_qfq.pkl；此处清理该目录下的前复权缓存。
        import glob as _glob
        kline_dir = os.path.join(get_app_dir(), 'cache', 'kline')
        removed = 0
        for fp in _glob.glob(os.path.join(kline_dir, 'qfq_daily_*.pkl')):
            try:
                os.remove(fp)
                removed += 1
            except Exception:
                pass
        if removed:
            print(f"已删除 {removed} 个前复权缓存文件（cache/kline/qfq_daily_*.pkl）")

    # 多周期模式
    if args.multi_horizon and args.hold_days == [10]:
        args.hold_days = [5, 10, 15, 20, 30]
        print("多周期模式: 将测试 5/10/15/20/30 天四个持仓周期")

    today = datetime.now()
    all_results = []

    for hold_days in args.hold_days:
        # 计算日期范围：优先使用 --start/--end，否则用 --years 推算
        if args.end:
            end = datetime.strptime(args.end, '%Y-%m-%d')
        else:
            # 结束日 = 今天 - (hold_days + 15) 留足未来数据
            end = today - timedelta(days=hold_days + 15)
        if args.start:
            start = datetime.strptime(args.start, '%Y-%m-%d')
        else:
            # 开始日 = 结束日 - years*365天
            start = end - timedelta(days=int(args.years * 365))

        print("=" * 70)
        mode = "全市场" if args.full_market else "上证50+创业50+科创50"
        print(f"回测验证 v8 - {mode}回测（V5评分引擎）")
        print(f"回测区间: {start.strftime('%Y-%m-%d')} ~ {end.strftime('%Y-%m-%d')} "
              f"(约{(end-start).days/365:.1f}年)")
        print(f"数据源: 新浪财经（优先）→ 腾讯（备用）")
        print("=" * 70)

        bt = Backtester(
            start_date=start.strftime('%Y-%m-%d'),
            end_date=end.strftime('%Y-%m-%d'),
            hold_days=hold_days,
            sample_interval=args.sample_interval,
            max_stocks=args.max_stocks,
            full_market=args.full_market,
            return_years=args.years,
            n_workers=args.n_workers,
        )

        bt.preload_all_data()
        report = bt.run()
        report['start_date'] = start.strftime('%Y-%m-%d')
        report['end_date'] = end.strftime('%Y-%m-%d')
        all_results.append(report)

    # 多周期对比汇总
    if len(all_results) > 1:
        print("\n" + "=" * 70)
        print("多周期对比汇总（IC衰减分析）")
        print("=" * 70)
        print(f"{'持仓':>6} | {'样本数':>6} | {'Pearson r':>10} | {'Rank IC':>10} | {'强势收益':>8} | {'弱势收益':>8} | {'收益差':>7} | {'胜率':>6}")
        print("-" * 84)
        for r in all_results:
            if 'error' in r:
                print(f"  {r['hold_days']:>4}天 | {'失败':>6}")
                continue
            rank_ic_val = r.get('avg_rank_ic', r.get('correlation', 0))
            print(f"  {r['hold_days']:>4}天 | {r['total_samples']:>6} | {r['correlation']:>10.4f} | "
                  f"{rank_ic_val:>10.4f} | {r['strong_group_return']:>7.2f}% | "
                  f"{r['weak_group_return']:>7.2f}% | {r['return_diff']:>6.2f}% | "
                  f"{r['overall_win_rate']:>5.1f}%")

        # IC衰减判断
        if len(all_results) >= 2:
            ics = [r.get('correlation', 0) for r in all_results if 'error' not in r]
            if ics:
                short_ic = ics[0]
                long_ic = ics[-1]
                if long_ic > short_ic * 0.8:
                    print(f"\n  [IC不衰减] 短周期IC={short_ic:.4f}, 长周期IC={long_ic:.4f}, 信号有持续性")
                elif long_ic > 0:
                    print(f"\n  [IC温和衰减] 短周期IC={short_ic:.4f} → 长周期IC={long_ic:.4f}")
                else:
                    print(f"\n  [IC反转] 长周期IC转负={long_ic:.4f}, 短期信号在中长期失效")

    suffix = '_fullmarket' if args.full_market else ''
    if args.years != 1:
        suffix += f'_{args.years:.0f}y'
    output_file = os.path.join(get_app_dir(),
                               f'backtest_report_v8_sina{suffix}.json')
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n结果已保存: {output_file}")
    print("=" * 70)
