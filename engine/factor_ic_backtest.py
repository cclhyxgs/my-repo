#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""因子 IC 回测 — 每个启用因子相对未来 N 日收益的 Rank-IC

复用 backtest 的离线缓存 / 股票池，以及 factor_registry 的因子计算函数。
对每个 (股票, 采样日) 计算注册表全部 21 个因子的原始值与技术上下文，
再与未来 hold_days 日收益做 Spearman 秩相关（Rank-IC），给出：
  - 每个因子的整体 Rank-IC / IC_IR / 样本量 / 方向 / 是否当前模型启用(in_config)
（注意：扫描全部因子，不限于 quant_model.json 当前启用的因子，
  用于回答"回测区间里哪些因子预测相关性最好"）
  - 年度 IC 稳定性（按自然年拆分）
输出 JSON + CSV + matplotlib 柱状图 PNG，供 GUI 工具直接调用。

注意：本模块仅做因子预测力统计，不涉及交易/仓位，与策略回测相互独立。
"""
import os
import sys
import json
import time
import warnings
import numpy as np
import pandas as pd

warnings.filterwarnings('ignore', category=FutureWarning)

# 复用现有回测的离线数据与股票池（不触发网络，缓存优先）
from .backtest import (_stock_data_cache,
                      _load_disk_cache, _save_disk_cache, _fetch_stock_data)
from engine.stock_pool import get_stock_pool, get_full_market_pool, resolve_stock_pool
from engine import factor_registry, quant_config
from engine.backtest_cancel import BacktestCancelled
from engine.config import get_app_dir
# 期货 IC 模式：与 A股 IC 共用截面IC/板块中性化逻辑，仅数据源与收益窗口单位不同
from engine.futures_data import fetch_futures_daily
from engine.futures_pool import all_contracts, resolve_pool as futures_resolve_pool


# 下沉到底层纯指标模块（打破 factor_ic_backtest <-> parallel_utils 环）
from engine.factor_tech import (
    _precompute_tech_series, _build_tech, _precompute_per_sample_state,
)



def _preload(pool, n_threads=1, offline=False):
    """载入离线缓存；未命中缓存的股票串行/并行抓取并写回磁盘缓存。

    n_threads<=1（默认）走原串行抓取；n_threads>1 时改用 backtest._preload_parallel
    （ThreadPoolExecutor 并行抓取，I/O 密集）。任何异常自动回退串行，保证向后兼容。
    offline=True 时未命中缓存的股票直接跳过，不联网抓取。
    """
    if not _stock_data_cache:
        _load_disk_cache(pool=[sc for sc, _, _ in pool])
    to_load = [(sc, rc, bd) for sc, rc, bd in pool if sc not in _stock_data_cache]
    success = 0

    # 离线模式：未命中缓存的股票直接跳过
    if to_load and offline:
        print(f"[因子IC] 【离线模式】跳过 {len(to_load)} 只未命中缓存的股票，不联网抓取")
        return 0, len(to_load)

    if to_load and n_threads and n_threads > 1:
        try:
            from .backtest import _preload_parallel
            s, _ = _preload_parallel(pool, n_threads)
            return s, len(to_load)
        except Exception as e:
            print(f"[因子IC] 并行预加载异常，回退串行: {e}")
            # 落到下方串行兜底
    for stock_code, raw_code, board in to_load:
        df = _fetch_stock_data(stock_code)
        if df is not None:
            success += 1
    if to_load:
        _save_disk_cache()
    return success, len(to_load)


def _serial_scan(pool, hold_days, sample_interval, years, all_factor_names,
                 factor_configs, active_factors, cancel_event, progress_cb, t0,
                 market='stock', data_cache=None):
    """原 run_factor_ic 串行热循环（逐字等效），返回 (samples, total_dates)。

    IC 计算 / 报告生成 / 文件写出不在本函数内，由 run_factor_ic 在主进程统一处理，
    确保多核版与串行版产出逐位一致。

    P0重构：samples 新增 'dates' 字段，记录每个采样点的截面日期，
    用于后续截面IC计算（逐日Spearman，而非池化Spearman）。
    """
    samples = {f: {'vals': [], 'rets': [], 'years': [], 'dates': [], 'boards': []} for f in all_factor_names}
    total_dates = 0
    n_total = len(pool)
    n_done = 0

    def log(msg):
        if progress_cb:
            progress_cb(msg)
        else:
            print(msg, flush=True)

    for item in pool:
        # 用户点击「停止」→ 干净中止
        if cancel_event is not None and cancel_event.is_set():
            log("⏹ 已收到停止信号，因子IC回测中止。")
            raise BacktestCancelled()
        # 数据源分支：A股走离线缓存，期货走预拉取的 data_cache
        if market == 'futures':
            symbol, _name, _secid = item
            board = '期货'
            df = data_cache.get(symbol) if data_cache else None
            stock_code = symbol  # 兼容下方日志
        else:
            stock_code, raw_code, board = item
            df = _stock_data_cache.get(stock_code)
        n_done += 1
        # 进度提示（每 50 只或末只打印一次，避免闷头跑像死机；带百分比+ETA）
        if n_done % 50 == 0 or n_done == n_total:
            elapsed = time.time() - t0
            rate = n_done / elapsed if elapsed > 0 else 0
            remain = (n_total - n_done) / rate if rate > 0 else 0
            pct = 100.0 * n_done / n_total if n_total else 0
            log(f"[因子IC] 进度: {n_done}/{n_total} 只 ({pct:.1f}%) | 已用 {elapsed/60:.1f}分 | "
                f"预计剩 {remain/60:.1f}分 | 采样点 {total_dates}")
        if df is None or len(df) < 60 + hold_days:
            continue
        closes_all = df['close'].tolist()
        n_all = len(closes_all)
        # 整段序列预计算（每只股票只算一次，O(n)），消除原 per-采样点的 O(n^2) 重复
        highs_all = df['high'].tolist()
        lows_all = df['low'].tolist()
        volumes_all = df['volume'].tolist()
        opens_all = df['open'].tolist()
        dates_all = df['trade_time'].tolist()
        data_list_all = [{'close': c, 'high': h, 'low': l, 'volume': v, 'date': d, 'open': o}
                         for c, h, l, v, d, o in zip(closes_all, highs_all, lows_all, volumes_all, dates_all, opens_all)]
        try:
            pre = _precompute_tech_series(closes_all, highs_all, lows_all, volumes_all, opens_all, data_list_all)
            state = _precompute_per_sample_state(closes_all, highs_all, lows_all, volumes_all, opens_all, data_list_all, pre)
        except Exception as e:
            log(f"[因子IC] ⚠ 预计算失败，跳过 {stock_code}: {e}")
            continue
        # 回测区间：取最近 years*365 天
        start_idx = max(60, n_all - int(years * 365) - hold_days)
        calc_errors = 0
        for idx in range(start_idx, n_all - hold_days, max(1, sample_interval)):
            # 用户点击「停止」→ 当前股票的采样循环内也及时响应
            if cancel_event is not None and cancel_event.is_set():
                log("⏹ 已收到停止信号，因子IC回测中止。")
                raise BacktestCancelled()
            future_idx = idx + hold_days
            if future_idx >= n_all:
                continue
            total_dates += 1
            try:
                # 从预计算序列切片取值（O(1)），消除 O(n^2) 重复计算
                # 注意：_build_tech 返回的第二个元素是「市场上下文 dict」，
                # 绝不能赋值给同名变量 market（会覆盖 mode 参数 'futures'/'stock'，
                # 导致后续合约 if market=='futures' 判断失效）。故改名 mkt_ctx。
                tech, mkt_ctx = _build_tech(pre, state, idx, closes_all, highs_all, lows_all, volumes_all, opens_all, data_list_all)
                ctx = {'closes': closes_all[:idx + 1], 'highs': highs_all[:idx + 1],
                       'lows': lows_all[:idx + 1], 'volumes': volumes_all[:idx + 1],
                       'opens': opens_all[:idx + 1], 'data_list': data_list_all[:idx + 1],
                       'latest_price': closes_all[idx], 'tech': tech, 'market': mkt_ctx}
                fvals = factor_registry.calc_all_active_factors(all_factor_names, ctx, factor_configs)
            except Exception as e:
                calc_errors += 1
                if calc_errors <= 20:
                    print(f"[因子IC警告] 因子计算异常（跳过该采样日）: {e}")
                continue
            fut_ret = (closes_all[future_idx] - closes_all[idx]) / closes_all[idx]
            year = pd.Timestamp(dates_all[idx]).year
            # P0重构：记录采样日期（归一化为Timestamp），用于截面IC计算
            sample_date = pd.Timestamp(dates_all[idx]).normalize()
            for f in all_factor_names:
                v = fvals.get(f)
                if v is None or not np.isfinite(v):
                    continue
                samples[f]['vals'].append(float(v))
                samples[f]['rets'].append(float(fut_ret))
                samples[f]['years'].append(int(year))
                samples[f]['dates'].append(sample_date)
                samples[f]['boards'].append(board)  # P5：记录板块
    if calc_errors:
        print(f"[因子IC汇总] 因子计算异常共 {calc_errors} 次（已跳过对应采样日，对应因子 IC 可能偏低）")
    return samples, total_dates


def _calculate_factor_ics(samples, all_factor_names, active_factors, log,
                          date_filter_start=None, date_filter_end=None):
    """P0重构：基于宽表计算截面IC（逐日Spearman）。

    Args:
        samples: 采样数据
        all_factor_names: 全部因子名
        active_factors: 当前模型启用的因子
        log: 日志回调
        date_filter_start: 起始日期过滤（含），None=不限制
        date_filter_end: 结束日期过滤（含），None=不限制
        用于P2样本外切分（train/valid）。

    Returns factor_rows 列表，每个元素包含：
    - factor / label / category
    - rank_ic: 截面IC均值
    - ic_ir: mean / std
    - ic_positive_rate: IC正率
    - ic_std: 截面IC标准差
    - n_samples / n_cross_sections / n_skipped_dates
    - stats: {mean, std} 原始因子值分布
    - direction / in_config
    - annual_ic: 年度IC列表
    """
    clip_errors = [0]  # 缩尾失败计数（静默失败→可见）；用 list 供嵌套函数累加
    MIN_CROSS_SECTION_SIZE = 30  # 截面最小样本量

    factor_rows = []
    for f in all_factor_names:
        s = samples[f]
        if len(s['vals']) < 50:
            log(f"[因子IC] {f}: 样本不足({len(s['vals'])})，跳过")
            continue

        # 构建宽表（date + val + ret + year + board）
        df = pd.DataFrame({
            'date': s['dates'],
            'val': s['vals'],
            'ret': s['rets'],
            'year': s['years'],
        })
        # P5：添加板块字段（如果有）
        if s.get('boards') and len(s['boards']) == len(s['vals']):
            df['board'] = s['boards']
        else:
            df['board'] = '其他'
        df = df.dropna(subset=['val', 'ret'])
        if len(df) < 50:
            continue

        # P5：板块内Z-score标准化（剥离板块风格影响）
        # 规则：每个 (date, board) 组内，val 做 Z-score 标准化
        # 缺板块或组内std=0则原值保留
        def _winsorize_then_zscore(group):
            """组内缩尾 + Z-score标准化"""
            v = group['val'].copy()
            if len(v) < 3:
                group['val_neutral'] = 0.0
                return group
            # 缩尾：1% / 99% 分位数
            try:
                p1, p99 = v.quantile([0.01, 0.99])
                v = v.clip(lower=p1, upper=p99)
            except Exception:
                clip_errors[0] += 1
            # Z-score
            std = v.std()
            if std > 0:
                group['val_neutral'] = (v - v.mean()) / std
            else:
                group['val_neutral'] = 0.0
            return group

        # 检查板块覆盖率
        if df['board'].nunique() > 1 and (df['board'] != '其他').sum() > len(df) * 0.5:
            # 板块覆盖率足够（>50%），启用板块中性化
            # 在每个 (date, board) 组内对 val 缩尾 + Z-score；用 transform 保持原表结构
            # （date 列不变动），避免 groupby(...).apply() 把分组键提升为 index 导致
            # 后续 df.groupby('date') 报 KeyError。
            def _zscore_group_vals(s):
                if len(s) < 3:
                    return pd.Series(0.0, index=s.index)
                v = s.copy()
                try:
                    p1, p99 = v.quantile([0.01, 0.99])
                    v = v.clip(lower=p1, upper=p99)
                except Exception:
                    pass
                std = v.std()
                if std > 0:
                    return (v - v.mean()) / std
                return pd.Series(0.0, index=s.index)

            df['val_neutral'] = df.groupby(['date', 'board'])['val'].transform(_zscore_group_vals)
            has_neutralization = True
        else:
            # 板块覆盖率不足，退化为全市场Z-score
            v = df['val'].copy()
            try:
                p1, p99 = v.quantile([0.01, 0.99])
                v = v.clip(lower=p1, upper=p99)
            except Exception:
                clip_errors[0] += 1
            std = v.std()
            if std > 0:
                df['val_neutral'] = (v - v.mean()) / std
            else:
                df['val_neutral'] = 0.0
            has_neutralization = False

        # P2样本外切分：按日期过滤
        if date_filter_start is not None:
            df = df[df['date'] >= pd.Timestamp(date_filter_start)]
        if date_filter_end is not None:
            df = df[df['date'] <= pd.Timestamp(date_filter_end)]
        if len(df) < 50:
            log(f"[因子IC] {f}: 切分后样本不足(<50)，跳过")
            continue

        # ===== 截面IC计算（核心改进，P5使用中性化后的val）=====
        daily_ics = []
        daily_dates = []
        long_short_returns = []  # P3：每日多空收益序列
        skipped_dates = 0

        # P3：多空分组比例（默认10%，业界标准）
        LS_TOP_RATIO = 0.10
        LS_BOTTOM_RATIO = 0.10

        # 决定使用哪个val：P5中性化后 或 原始
        val_col = 'val_neutral' if has_neutralization else 'val'

        for date, grp in df.groupby('date'):
            if len(grp) < MIN_CROSS_SECTION_SIZE:
                skipped_dates += 1
                continue
            try:
                # ===== 截面IC（P5：使用val_neutral）=====
                grp_ranks_val = grp[val_col].rank(method='average')
                grp_ranks_ret = grp['ret'].rank(method='average')
                ic = grp_ranks_val.corr(grp_ranks_ret, method='pearson')
                if np.isfinite(ic):
                    daily_ics.append(float(ic))
                    daily_dates.append(date)

                # ===== P3：多空收益（P5：使用val_neutral排序）=====
                n_top = max(int(len(grp) * LS_TOP_RATIO), 1)
                n_bottom = max(int(len(grp) * LS_BOTTOM_RATIO), 1)
                grp_sorted = grp.sort_values(val_col, ascending=False)
                top_ret = grp_sorted.head(n_top)['ret'].mean()
                bottom_ret = grp_sorted.tail(n_bottom)['ret'].mean()
                long_short_returns.append(float(top_ret - bottom_ret))
            except Exception:
                skipped_dates += 1
                continue

        if not daily_ics:
            log(f"[因子IC] {f}: 无有效截面（所有日期样本均不足{MIN_CROSS_SECTION_SIZE}）")
            continue

        n_cross_sections = len(daily_ics)
        mean_ic = float(np.mean(daily_ics))
        std_ic = float(np.std(daily_ics, ddof=1)) if n_cross_sections > 1 else 0.0
        ic_ir = mean_ic / std_ic if std_ic > 0 else (mean_ic if mean_ic != 0 else 0.0)
        ic_positive_rate = sum(1 for ic in daily_ics if ic > 0) / n_cross_sections
        rank_ic = mean_ic

        # ===== P3：多空夏普计算 =====
        long_short_sharpe = 0.0
        long_short_mean = 0.0
        long_short_std = 0.0
        n_ls_days = len(long_short_returns)
        if n_ls_days > 1:
            long_short_mean = float(np.mean(long_short_returns))
            long_short_std = float(np.std(long_short_returns, ddof=1))
            # 年化夏普：日频收益 * sqrt(252)
            if long_short_std > 0:
                long_short_sharpe = (long_short_mean / long_short_std) * np.sqrt(252)
            else:
                long_short_sharpe = long_short_mean * np.sqrt(252) if long_short_mean != 0 else 0.0
        elif n_ls_days == 1:
            long_short_mean = float(long_short_returns[0])
            long_short_sharpe = long_short_mean * np.sqrt(252)

        # 多空正率
        ls_positive_rate = sum(1 for r in long_short_returns if r > 0) / n_ls_days if n_ls_days > 0 else 0.0

        # P3：噪声因子标记
        noise_status = ''
        if long_short_sharpe < 1.0:
            noise_status = '❌噪声因子(多空夏普<1.0)'

        # 经验 z-score 统计
        raw_vals = np.array(s['vals'], dtype=float)
        mean_v = float(np.mean(raw_vals))
        std_v = float(np.std(raw_vals))
        if std_v <= 0:
            std_v = 1e-9

        # 年度 IC
        df_ics = pd.DataFrame({'date': daily_dates, 'ic': daily_ics})
        df_ics['year'] = pd.to_datetime(df_ics['date']).dt.year
        annual = []
        for y in sorted(df_ics['year'].unique()):
            year_ics = df_ics[df_ics['year'] == y]['ic'].tolist()
            if len(year_ics) >= 30:
                annual.append({
                    'year': int(y),
                    'rank_ic': round(float(np.mean(year_ics)), 4),
                    'std_ic': round(float(np.std(year_ics, ddof=1)), 4) if len(year_ics) > 1 else 0.0,
                    'positive_rate': round(sum(1 for ic in year_ics if ic > 0) / len(year_ics), 3),
                    'samples': int(len(year_ics))
                })

        fdef = factor_registry.get_factor(f)
        factor_rows.append({
            'factor': f,
            'label': fdef.label if fdef else f,
            'category': fdef.category if fdef else '',
            'rank_ic': round(rank_ic, 4),
            'ic_ir': round(ic_ir, 3),
            'ic_positive_rate': round(ic_positive_rate, 3),
            'ic_std': round(std_ic, 4),
            'n_samples': int(len(df)),
            'n_cross_sections': int(n_cross_sections),
            'n_skipped_dates': int(skipped_dates),
            'stats': {'mean': round(mean_v, 6), 'std': round(std_v, 6)},
            'direction': fdef.default_direction if fdef else 0,
            'in_config': f in active_factors,
            'annual_ic': annual,
            # P3 多空夏普字段
            'long_short_sharpe': round(long_short_sharpe, 3),
            'long_short_mean_daily': round(long_short_mean, 6),
            'long_short_std_daily': round(long_short_std, 6),
            'ls_positive_rate': round(ls_positive_rate, 3),
            'n_ls_days': n_ls_days,
            'noise_status': noise_status,
            # P5 板块中性化标记
            'neutralization': '✅板块内Z-score' if has_neutralization else '⚠️全市场Z-score(板块覆盖不足)',
        })
        neutral_tag = '[P5✅]' if has_neutralization else '[P5⚠️降级]'
        log(f"[因子IC] {f:20s} {neutral_tag} Rank-IC={rank_ic:+.4f}  IC_IR={ic_ir:+.2f}  "
            f"多空夏普={long_short_sharpe:+.2f}  正率={ic_positive_rate:.1%}  "
            f"N截面={n_cross_sections}  N样本={len(df)}")

    # 排序（按 |Rank-IC| 降序）
    factor_rows.sort(key=lambda r: abs(r['rank_ic']), reverse=True)
    if clip_errors[0]:
        print(f"[因子IC汇总] 共 {clip_errors[0]} 个分组缩尾失败（未截尾，对应因子 IC 可能受极端值影响）")
    return factor_rows


def run_factor_ic(years, full_market, hold_days, sample_interval,
                  prefix='bt_factoric', progress_cb=None, cancel_event=None,
                  n_workers=None, n_fetch_threads=1, pool=None,
                  offline=False, multi_horizon=False, train_ratio=0.0,
                  market='stock', direction='long', period='日线'):
    """运行因子 IC 回测，返回报告 dict 并写出 JSON/CSV/PNG。

    Args:
        years: 回测年数（默认1）
        full_market: 是否全市场（默认 False=148 池）
        hold_days: 预测持有天数（计算未来 N 日收益），默认 10
        sample_interval: 采样间隔交易日，默认 5
        prefix: 输出文件前缀
        progress_cb: 可选回调 progress_cb(line:str)，用于 UI 实时日志
        n_workers: 并行进程数（默认 None=自动按机型探测(核数+内存)决定安全并行数，
            兼顾吃满核与内存安全；显式传 1 则保持原单核串行；传更大的数则覆盖自动值。
            >1 时按股票多进程并行产生采样行）
        n_fetch_threads: 缓存未命中时并行抓取线程数（默认1=串行）
        offline: True 时仅使用本地缓存，未命中缓存的股票直接跳过，不联网抓取。
        multi_horizon: True 时同时测试 5/10/15/20/30 天四个持仓周期的IC衰减（v9 P1新增）。
        train_ratio: 训练集占比（0.0~1.0），剩余为验证集。
            0.0=禁用（默认，保持兼容）；0.6=前60%训练，后40%验证（v9 P2新增）。
    """
    # 运行前刷新（若勾选）：仅刷新当前股票池，放在股票池解析之后、spawn worker 之前，
    # 保证 worker 各自 _load_disk_cache 读到的是新鲜数据。默认不刷新（秒级启动）。

    # 多核自动启用：不显式指定时，按机型探测(核数+内存)自动算安全并行数，
    # 既尽量吃满核、又不会撑爆内存（每个 spawn worker 进程需独立载入离线缓存）。
    # 显式传 n_workers=1 则保持原单核串行；传更大的数则覆盖自动值。
    if n_workers is None:
        from engine.machine_probe import auto_n_workers, describe_machine
        n_workers = auto_n_workers()

    BASE = get_app_dir()

    def log(msg):
        if progress_cb:
            progress_cb(msg)
        else:
            print(msg, flush=True)

    t0 = time.time()
    is_futures = (market == 'futures')
    data_cache = None

    if is_futures:
        # ── 期货 IC 模式：复用同一套截面IC/板块中性化，仅数据源换期货日线 ──
        if pool is None:
            fut_contracts = all_contracts()
            _pool_label = '期货全市场(主力连续)'
        else:
            fut_contracts = futures_resolve_pool(pool)
            _pool_label = f'期货池[{pool}]'
        if not fut_contracts:
            log("[因子IC] 错误：期货池为空。")
            return None
        pool = [(c.symbol, c.name, c.secid) for c in fut_contracts]
        # 预拉取期货日线到 data_cache（绕过 A股离线缓存）
        data_cache = {}
        for c in fut_contracts:
            try:
                days = min(int(years * 365) + hold_days + 60, 1200) if years else 600
                df = fetch_futures_daily(c.symbol, c.secid, days=days)
            except Exception as e:
                log(f"[因子IC] ⚠ 拉取期货 {c.symbol} 日线失败: {e}")
                df = None
            if df is not None:
                data_cache[c.symbol] = df
        if not data_cache:
            log("[因子IC] 错误：期货日线全部拉取失败（检查网络/数据源）。")
            return None
        log(f"[因子IC] 期货池: {len(pool)} 个品种 | 命中日线 {len(data_cache)} 个 | 预测周期: {hold_days}天")
        # 期货方案隔离：加载 futures scheme 的因子配置（多/空方向化）
        try:
            quant_config.load_market_scheme('futures', direction, period, force_reload=True)
        except Exception as e:
            log(f"[因子IC] ⚠ 加载期货方案失败，回落全局配置: {e}")
        active_factors = quant_config.get_active_factors()
        factor_configs = quant_config.get_factor_configs()
        all_factor_names = list(factor_registry.get_all_factors().keys())
        if not all_factor_names:
            log("[因子IC] 错误：因子注册表为空。")
            return None
        log(f"[因子IC] 扫描全部 {len(all_factor_names)} 个因子 | 期货方案启用 {len(active_factors or [])} 个")
    else:
        # ── A股 IC 模式（原逻辑）──
        if pool is not None:
            # 显式股票池参数（'148'|'full'|'watchlist'）优先于 full_market 布尔
            _pool_mode = pool
            pool = resolve_stock_pool(pool)
            _pool_label = {'148': '上证50+创业50+科创50', 'full': '全市场', 'watchlist': '自选股列表'}.get(_pool_mode, _pool_mode)
        else:
            pool = get_full_market_pool() if full_market else get_stock_pool()
            _pool_label = '全市场' if full_market else '上证50+创业50+科创50'
        from engine.machine_probe import describe_machine
        log(describe_machine())
        log(f"[因子IC] 股票池: {len(pool)} 只 [{_pool_label}] | 预测周期: {hold_days}天 | 采样间隔: {sample_interval}交易日")

        ok, need = _preload(pool, n_fetch_threads, offline=offline)
        log(f"[因子IC] 数据缓存命中 {len(_stock_data_cache)} 只（本次新加载 {ok}/{need}）")

        # 全因子 IC 扫描：对注册表全部 21 个因子算 Rank-IC，不限于当前模型配置
        all_factor_names = list(factor_registry.get_all_factors().keys())
        active_factors = quant_config.get_active_factors()  # 仅用于标记 in_config
        factor_configs = quant_config.get_factor_configs()
        if not all_factor_names:
            log("[因子IC] 错误：因子注册表为空。")
            return None
        log(f"[因子IC] 扫描全部 {len(all_factor_names)} 个因子 | 当前模型启用 {len(active_factors)} 个: "
             f"{', '.join(active_factors) if active_factors else '无'}")

    # 收集每个因子的 (值, 未来收益, 年份)
    # n_workers>1 时走多进程并行（按股票分片）；否则（含默认 n_workers=1）走原串行路径，
    # 行为与改动前完全一致。IC 计算 / 报告生成 / 文件写出始终在主进程，确保逐位一致。
    use_parallel = (n_workers > 1) and not is_futures
    if use_parallel:
        try:
            from engine.parallel_utils import parallel_scan_stocks
            samples, total_dates = parallel_scan_stocks(
                pool, hold_days, sample_interval, years,
                all_factor_names, factor_configs, active_factors,
                n_workers, cancel_event, progress_cb)
        except BacktestCancelled:
            raise
        except Exception as e:
            log(f"[因子IC] ⚠ 多核执行失败，回退串行: {e}")
            use_parallel = False
    if not use_parallel:
        samples, total_dates = _serial_scan(
            pool, hold_days, sample_interval, years, all_factor_names,
            factor_configs, active_factors, cancel_event, progress_cb, t0,
            market=market, data_cache=data_cache)

    log(f"[因子IC] 有效采样点: {total_dates} 个交易日期")

    # ===== P1 多周期IC衰减 =====
    # 当 multi_horizon=True 时，对 5/10/15/20/30 天分别计算IC，输出衰减矩阵
    if multi_horizon:
        return _run_multi_horizon(
            pool=pool,
            market=market, data_cache=data_cache,
            years=years,
            sample_interval=sample_interval,
            prefix=prefix,
            progress_cb=progress_cb,
            cancel_event=cancel_event,
            n_workers=n_workers,
            n_fetch_threads=n_fetch_threads,
            offline=offline,
            all_factor_names=all_factor_names,
            active_factors=active_factors,
            factor_configs=factor_configs,
            log=log,
            t0=t0,
        )

    # ===== 单周期计算 =====
    factor_rows = _calculate_factor_ics(samples, all_factor_names, active_factors, log)

    # ===== P2 样本外验证 =====
    # train_ratio>0 时，将数据按日期切分为训练期（前train_ratio）和验证期（后1-train_ratio）
    train_valid_info = None  # 训练/验证切分信息
    if train_ratio > 0.0 and 0.0 < train_ratio < 1.0:
        # 计算训练/验证日期分界点（基于所有样本日期的中位数）
        all_dates = []
        for s in samples.values():
            if s.get('dates'):
                all_dates.extend(s['dates'])
        if all_dates:
            all_dates_sorted = sorted(set(all_dates))
            split_idx = int(len(all_dates_sorted) * train_ratio)
            train_end_date = all_dates_sorted[split_idx] if split_idx < len(all_dates_sorted) else None
            if train_end_date is not None:
                train_valid_info = {
                    'train_ratio': train_ratio,
                    'train_end_date': str(train_end_date)[:10],
                    'train_start': str(all_dates_sorted[0])[:10],
                    'valid_end': str(all_dates_sorted[-1])[:10],
                }
                log(f"[P2样本外验证] 训练期: {train_valid_info['train_start']} ~ {train_valid_info['train_end_date']}")
                log(f"[P2样本外验证] 验证期: {train_valid_info['train_end_date']} ~ {train_valid_info['valid_end']}")

                # 计算训练期IC
                train_rows = _calculate_factor_ics(
                    samples, all_factor_names, active_factors, log,
                    date_filter_start=None, date_filter_end=train_end_date)

                # 计算验证期IC
                valid_rows = _calculate_factor_ics(
                    samples, all_factor_names, active_factors, log,
                    date_filter_start=train_end_date, date_filter_end=None)

                # 验证集样本数检查
                valid_total_samples = sum(r['n_samples'] for r in valid_rows)
                if valid_total_samples < 500:
                    log(f"[P2样本外验证] ⚠️ 验证集样本数仅{valid_total_samples}（<500），"
                        f"统计意义不足，结果仅供参考")

                # 按因子名合并train/valid
                train_map = {r['factor']: r for r in train_rows}
                valid_map = {r['factor']: r for r in valid_rows}

                # 用验证期IC作为"真实"IC（保守原则），训练期仅用于对比
                for f_row in factor_rows:
                    fname = f_row['factor']
                    tr = train_map.get(fname)
                    va = valid_map.get(fname)
                    f_row['train_ic'] = tr['rank_ic'] if tr else None
                    f_row['valid_ic'] = va['rank_ic'] if va else None
                    f_row['train_ir'] = tr['ic_ir'] if tr else None
                    f_row['valid_ir'] = va['ic_ir'] if va else None
                    f_row['train_n_samples'] = tr['n_samples'] if tr else 0
                    f_row['valid_n_samples'] = va['n_samples'] if va else 0

                    # 计算过拟合风险
                    if tr and va and tr['rank_ic'] is not None and va['rank_ic'] is not None:
                        ti = tr['rank_ic']
                        vi = va['rank_ic']
                        # 方向反转
                        if ti * vi < 0:
                            f_row['overfit_status'] = '⚠️方向反转'
                        # 衰减>30%
                        elif abs(ti) > 0 and abs(vi) < abs(ti) * 0.7:
                            f_row['overfit_status'] = '⚠️衰减>30%'
                            f_row['valid_decay_pct'] = round(
                                (1 - abs(vi) / abs(ti)) * 100, 1)
                        # 稳定
                        else:
                            f_row['overfit_status'] = '✅稳定'
                            f_row['valid_decay_pct'] = round(
                                (1 - abs(vi) / abs(ti)) * 100, 1) if abs(ti) > 0 else 0
                    else:
                        f_row['overfit_status'] = 'N/A(样本不足)'
                        f_row['valid_decay_pct'] = None

    report = {
        'mode': 'futures_factor_ic' if is_futures else 'factor_ic',
        'hold_days': hold_days,
        'years': round(years, 2),
        'full_market': full_market,
        'n_scan_factors': len(all_factor_names),
        'n_active_factors': len(active_factors),
        'n_factors': len(factor_rows),
        'sample_dates': total_dates,
        'factors': factor_rows,
        'factor_stats': {r['factor']: {
            'mean': r['stats']['mean'],
            'std': r['stats']['std'],
            'rank_ic': r['rank_ic'],
            'ic_ir': r['ic_ir']
        } for r in factor_rows},
        'elapsed_sec': round(time.time() - t0, 1),
    }
    # P2：将样本外切分信息附加到报告
    if train_valid_info is not None:
        report['train_valid_split'] = train_valid_info

    # 写出 JSON / CSV / PNG
    json_path = os.path.join(BASE, f'{prefix}_report.json')
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    log(f"[因子IC] 报告已保存: {json_path}")

    csv_path = os.path.join(BASE, f'{prefix}.csv')
    # P0+P2+P3+P4+P5：CSV增加IC正率、标准差、方向冲突、截面数、训练/验证、多空夏普、板块中性化等新字段
    pd.DataFrame([{
        'factor': r['factor'],
        'label': r['label'],
        'category': r['category'],
        'rank_ic': r['rank_ic'],
        'ic_ir': r['ic_ir'],
        'ic_positive_rate': r.get('ic_positive_rate', 0),
        'ic_std': r.get('ic_std', 0),
        'n_samples': r['n_samples'],
        'n_cross_sections': r.get('n_cross_sections', 0),
        'direction': r['direction'],
        '方向冲突': '⚠️是' if (r['rank_ic'] * r['direction'] < 0) else '否',
        # P2样本外字段（仅启用train_ratio时填充）
        'train_ic': r.get('train_ic'),
        'valid_ic': r.get('valid_ic'),
        'train_ir': r.get('train_ir'),
        'valid_ir': r.get('valid_ir'),
        'valid_decay_pct': r.get('valid_decay_pct'),
        'overfit_status': r.get('overfit_status', ''),
        # P3多空夏普字段
        'long_short_sharpe': r.get('long_short_sharpe', 0),
        'ls_positive_rate': r.get('ls_positive_rate', 0),
        'noise_status': r.get('noise_status', ''),
        # P5板块中性化字段
        'neutralization': r.get('neutralization', ''),
        'in_config': r['in_config'],
    } for r in factor_rows]).to_csv(csv_path, index=False, encoding='utf-8-sig')
    log(f"[因子IC] CSV 已保存: {csv_path}")

    _save_chart(report, os.path.join(BASE, f'{prefix}.png'))
    log(f"[因子IC] 完成，耗时 {report['elapsed_sec']}s")
    return report


def _run_multi_horizon(pool, years, sample_interval, prefix, progress_cb, cancel_event,
                       n_workers, n_fetch_threads, offline,
                       all_factor_names, active_factors, factor_configs, log, t0,
                       market='stock', data_cache=None):
    """P1多周期IC衰减：对 5/10/15/20/30 天分别计算IC并输出衰减矩阵。

    工作流程：
    1. 对每个周期（5/10/15/20/30天）调用 _serial_scan 收集 samples
    2. 调用 _calculate_factor_ics 计算每个周期下的IC统计
    3. 汇总为衰减矩阵，输出到 bt_factoric_decay.csv
    4. 标记"高度滞后"和"短线有效"因子

    Returns:
        完整的多周期报告 dict
    """
    BASE = get_app_dir()
    HORIZONS = [5, 10, 15, 20, 30]  # 测试的持仓周期

    log(f"[多周期IC] 启动：测试 {HORIZONS} 天持仓周期")

    # 每个周期的factor_rows
    horizon_results = {}  # {hold_days: factor_rows}
    use_parallel = (n_workers > 1) and market != 'futures'

    for hd in HORIZONS:
        log(f"[多周期IC] === 计算 hold_days={hd} ===")
        if cancel_event is not None and cancel_event.is_set():
            log("⏹ 已收到停止信号，多周期IC中止。")
            raise BacktestCancelled()
        # 重新扫描（不同hold_days需要不同future_idx）
        if use_parallel:
            try:
                from engine.parallel_utils import parallel_scan_stocks
                samples, _ = parallel_scan_stocks(
                    pool, hd, sample_interval, years,
                    all_factor_names, factor_configs, active_factors,
                    n_workers, cancel_event, progress_cb)
            except BacktestCancelled:
                raise
            except Exception as e:
                log(f"[多周期IC] ⚠ 多核失败（hd={hd}），回退串行: {e}")
                samples, _ = _serial_scan(
                    pool, hd, sample_interval, years, all_factor_names,
                    factor_configs, active_factors, cancel_event, progress_cb, t0,
                    market=market, data_cache=data_cache)
        else:
            samples, _ = _serial_scan(
                pool, hd, sample_interval, years, all_factor_names,
                factor_configs, active_factors, cancel_event, progress_cb, t0,
                market=market, data_cache=data_cache)

        rows = _calculate_factor_ics(samples, all_factor_names, active_factors, log)
        horizon_results[hd] = rows

    # ===== 构造衰减矩阵 =====
    # 以所有因子并集为行
    all_factors = set()
    for rows in horizon_results.values():
        for r in rows:
            all_factors.add(r['factor'])
    all_factors = sorted(all_factors)

    decay_matrix = []
    for f in all_factors:
        fdef = factor_registry.get_factor(f)
        row = {
            'factor': f,
            'label': fdef.label if fdef else f,
            'category': fdef.category if fdef else '',
            'direction': fdef.default_direction if fdef else 0,
            'in_config': f in active_factors,
        }
        for hd in HORIZONS:
            # 该周期下该因子的IC
            hit = next((r for r in horizon_results[hd] if r['factor'] == f), None)
            row[f'ic_{hd}d'] = hit['rank_ic'] if hit else None
            row[f'ir_{hd}d'] = hit['ic_ir'] if hit else None
            row[f'posrate_{hd}d'] = hit['ic_positive_rate'] if hit else None

        # 衰减特征判断
        ic5 = row.get('ic_5d')
        ic30 = row.get('ic_30d')
        if ic5 is not None and ic30 is not None:
            # 高度滞后：5日IC<0.01 且 30日IC>0.03 → 实战价值低
            if ic5 < 0.01 and ic30 > 0.03:
                row['衰减特征'] = '⚠️高度滞后(建议淘汰)'
            # 短线有效：5日IC最强 → 优先
            elif ic5 > 0.03:
                row['衰减特征'] = '✅短线有效'
            # 持续有效
            elif ic5 > 0.02 and ic30 > 0.02:
                row['衰减特征'] = '✅持续有效'
            # 弱有效
            elif abs(ic5) < 0.01 and abs(ic30) < 0.01:
                row['衰减特征'] = '❌无明显IC'
            else:
                row['衰减特征'] = '➖一般'
        else:
            row['衰减特征'] = 'N/A'

        # 方向冲突
        if ic5 is not None:
            row['方向冲突(5d)'] = '⚠️是' if (ic5 * row['direction'] < 0) else '否'
        else:
            row['方向冲突(5d)'] = 'N/A'
        decay_matrix.append(row)

    # 按5日IC绝对值排序
    decay_matrix.sort(key=lambda r: abs(r.get('ic_5d') or 0), reverse=True)

    # ===== 输出CSV =====
    csv_path = os.path.join(BASE, f'{prefix}_decay.csv')
    pd.DataFrame(decay_matrix).to_csv(csv_path, index=False, encoding='utf-8-sig')
    log(f"[多周期IC] 衰减矩阵CSV已保存: {csv_path}")

    # ===== 输出JSON报告 =====
    report = {
        'mode': 'factor_ic_multi_horizon',
        'horizons': HORIZONS,
        'years': round(years, 2),
        'sample_interval': sample_interval,
        'n_factors': len(decay_matrix),
        'n_active_factors': len(active_factors),
        'elapsed_sec': round(time.time() - t0, 1),
        'decay_matrix': decay_matrix,
        'horizon_details': {
            str(hd): [
                {
                    'factor': r['factor'],
                    'rank_ic': r['rank_ic'],
                    'ic_ir': r['ic_ir'],
                    'ic_positive_rate': r['ic_positive_rate'],
                    'n_cross_sections': r['n_cross_sections'],
                }
                for r in rows
            ]
            for hd, rows in horizon_results.items()
        },
    }
    json_path = os.path.join(BASE, f'{prefix}_decay_report.json')
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    log(f"[多周期IC] 报告已保存: {json_path}")

    # ===== 控制台打印摘要 =====
    log(f"\n[多周期IC] ====== 衰减矩阵摘要（按5日IC绝对值排序）======")
    log(f"{'因子':<25s} {'5d':>8s} {'10d':>8s} {'15d':>8s} {'20d':>8s} {'30d':>8s}  {'特征'}")
    for r in decay_matrix[:10]:  # 仅显示前10
        line = f"{r['label'][:24]:<25s} "
        for hd in HORIZONS:
            v = r.get(f'ic_{hd}d')
            line += f"{v:+.4f}  " if v is not None else "  N/A    "
        line += f" {r['衰减特征']}"
        log(line)

    log(f"[多周期IC] 完成，耗时 {report['elapsed_sec']}s")
    return report


def _save_chart(report, png_path):
    """绘制因子 Rank-IC 柱状图（含零线与方向着色）"""
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        plt.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei', 'DejaVu Sans']
        plt.rcParams['axes.unicode_minus'] = False

        rows = report['factors']
        if not rows:
            return
        labels = [f"{r['label']}{'★' if r.get('in_config') else ''}\n({r['factor']})" for r in rows]
        ics = [r['rank_ic'] for r in rows]
        colors = ['#16a34a' if v > 0 else '#dc2626' for v in ics]

        fig, ax = plt.subplots(figsize=(max(8, len(rows) * 1.1), 5))
        bars = ax.bar(range(len(rows)), ics, color=colors, alpha=0.85)
        ax.axhline(y=0, color='#212529', linewidth=0.8)
        ax.axhline(y=0.03, color='#16a34a', linestyle='--', linewidth=0.8, alpha=0.6, label='弱有效阈值(+0.03)')
        ax.axhline(y=-0.03, color='#dc2626', linestyle='--', linewidth=0.8, alpha=0.6)
        ax.set_xticks(range(len(rows)))
        ax.set_xticklabels(labels, fontsize=8, rotation=30, ha='right')
        ax.set_ylabel('Rank-IC')
        ax.set_xlabel('★ = 当前模型已启用因子', fontsize=8)
        ax.set_title(f"因子 Rank-IC（预测 {report['hold_days']} 日收益，{report['sample_dates']} 采样点）",
                     fontsize=12, fontweight='bold')
        for i, v in enumerate(ics):
            ax.text(i, v + (0.002 if v >= 0 else -0.004), f'{v:+.3f}',
                    ha='center', va='bottom' if v >= 0 else 'top', fontsize=8)
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3, axis='y')
        plt.tight_layout()
        plt.savefig(png_path, dpi=150, bbox_inches='tight')
        plt.close()
    except Exception as e:
        print(f"[因子IC] 图表生成跳过: {e}")


if __name__ == '__main__':
    # 强制 stdout/stderr 行缓冲：管道/重定向下进度行不再被块缓冲憋住（看着像卡死的根因）
    import sys as _sys
    for _s in (_sys.stdout, _sys.stderr):
        if hasattr(_s, 'reconfigure'):
            try:
                _s.reconfigure(line_buffering=True, encoding='utf-8')
            except Exception:
                pass
    # 命令行快速入口（可选）
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--sample-interval', type=int, default=5, help='采样间隔(根K)')
    ap.add_argument('--prefix', type=str, default='bt_factoric')
    ap.add_argument('--years', type=float, default=1.0)
    ap.add_argument('--full-market', action='store_true')
    ap.add_argument('--hold-days', type=int, default=10)
    a = ap.parse_args()
    run_factor_ic(years=a.years, full_market=a.full_market, hold_days=a.hold_days,
                  sample_interval=a.sample_interval, prefix=a.prefix)
