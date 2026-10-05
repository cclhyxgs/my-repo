#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""因子 IC 回测的多进程 / 多线程并行工具（仅用 Python 标准库 + numpy/pandas）。

设计要点（Windows spawn 安全，CPython + PyInstaller 打包）：
- 所有 worker 函数均为**模块级**，确保 spawn 启动时能被 pickle（闭包不行）。
- 241MB 离线缓存（backtest._stock_data_cache）**绝不**作为任务参数传输；
  每个 worker 进程在 init_worker 里各自 _load_disk_cache() 一次（仅首跑一次）。
- 主进程只负责「汇总行」+ Rank-IC 计算 + 报告生成 + 文件写出，
  因此多核版与单核串行版产出的 21 因子 IC 报告**逐位一致**。
- 重依赖（backtest / engine.factor_ic_backtest / engine.factor_registry）一律
  **延迟到函数内** import，避免模块顶层交叉导入与进程启动时的额外开销。
"""
import sys

import numpy as np
import pandas as pd


def init_worker(pool_codes=None):
    """每个 worker 进程启动时调用一次：载入本进程自己的离线缓存全局。

    这样 241MB 缓存只在每个 worker 进程内从磁盘加载一次，不会随每个任务参数
    被重复 pickle / 传输（避免 N×241MB 内存与 I/O 放大）。

    pool_codes: 若提供（股票代码列表），worker 只验证+载入池内股票，冷启动
    _validate_qfq 循环从全市场降到池内数量（148池→省1.9s/worker）。
    pickle.load 仍需读全文件（pkl 格式限制，3.25s 硬下限）。
    """
    from . import backtest
    if not backtest._stock_data_cache:
        backtest._load_disk_cache(pool=pool_codes)


def _process_one_stock(stock_code, hold_days, sample_interval, years,
                       all_factor_names, factor_configs, active_factors, board='其他'):
    """处理单只股票，返回 (rows, n_sampled)。

    rows: 每个成功采样点一行，(valid_fvals_dict, fut_ret, year, sample_date, board)；
          valid_fvals_dict 仅含有限值的因子 {factor: float_value}。
          sample_date 用于P0重构后的截面IC计算。
          board 用于P5板块中性化（板块内Z-score标准化）。
    n_sampled: 通过 future_idx 校验的采样点数量（与串行版 total_dates 语义一致，
               即使个别采样点因子计算异常也照常计数，保证报告 sample_dates 逐位一致）。

    active_factors 仅为保持调用签名一致而保留（行生成阶段并不需要）。
    逻辑与 run_factor_ic 原串行热循环**逐字等效**。
    """
    from . import backtest
    from engine.factor_tech import _precompute_tech_series, _precompute_per_sample_state, _build_tech
    from engine import factor_registry

    df = backtest._stock_data_cache.get(stock_code)
    if df is None or len(df) < 60 + hold_days:
        return [], 0

    closes_all = df['close'].tolist()
    n_all = len(closes_all)
    highs_all = df['high'].tolist()
    lows_all = df['low'].tolist()
    volumes_all = df['volume'].tolist()
    opens_all = df['open'].tolist()
    dates_all = df['trade_time'].tolist()
    data_list_all = [{'close': c, 'high': h, 'low': l, 'volume': v, 'date': d, 'open': o}
                     for c, h, l, v, d, o in zip(closes_all, highs_all, lows_all,
                                                 volumes_all, dates_all, opens_all)]
    try:
        pre = _precompute_tech_series(closes_all, highs_all, lows_all, volumes_all,
                                      opens_all, data_list_all)
        state = _precompute_per_sample_state(closes_all, highs_all, lows_all, volumes_all,
                                            opens_all, data_list_all, pre)
    except Exception:
        return [], 0

    start_idx = max(60, n_all - int(years * 365) - hold_days)
    rows = []
    n_sampled = 0
    for idx in range(start_idx, n_all - hold_days, max(1, sample_interval)):
        future_idx = idx + hold_days
        if future_idx >= n_all:
            continue
        n_sampled += 1
        try:
            tech, market = _build_tech(pre, state, idx, closes_all, highs_all, lows_all,
                                       volumes_all, opens_all, data_list_all)
            ctx = {'closes': closes_all[:idx + 1], 'highs': highs_all[:idx + 1],
                   'lows': lows_all[:idx + 1], 'volumes': volumes_all[:idx + 1],
                   'opens': opens_all[:idx + 1], 'data_list': data_list_all[:idx + 1],
                   'latest_price': closes_all[idx], 'tech': tech, 'market': market}
            fvals = factor_registry.calc_all_active_factors(all_factor_names, ctx, factor_configs)
            fut_ret = (closes_all[future_idx] - closes_all[idx]) / closes_all[idx]
            year = pd.Timestamp(dates_all[idx]).year
            # P0重构：记录采样日期（归一化为Timestamp），用于后续截面IC计算
            sample_date = pd.Timestamp(dates_all[idx]).normalize()
            valid = {}
            for f in all_factor_names:
                v = fvals.get(f)
                if v is None or not np.isfinite(v):
                    continue
                valid[f] = float(v)
            rows.append((valid, float(fut_ret), int(year), sample_date, board))
        except Exception:
            continue
    return rows, n_sampled


def parallel_scan_stocks(stock_pool_with_board, hold_days, sample_interval, years,
                         all_factor_names, factor_configs, active_factors,
                         n_workers, cancel_event, progress_cb):
    """按股票分片，用 ProcessPoolExecutor 并行产生 (因子值, 未来收益, 年份) 行。

    stock_pool_with_board: [(stock_code, raw_code, board), ...] 三元组列表
    返回 (samples, total_dates)，供主进程原样接续 Rank-IC 计算与报告生成。
    IC 计算、报告、文件写出全部留在主进程，与串行版逐字一致。
    """
    import multiprocessing as mp
    from concurrent.futures import ProcessPoolExecutor
    from itertools import repeat
    from engine.backtest_cancel import BacktestCancelled

    # 拆分：股票代码列表 + 对应板块列表
    stock_codes = [sc for sc, _, _ in stock_pool_with_board]
    boards = [bd for _, _, bd in stock_pool_with_board]

    samples = {f: {'vals': [], 'rets': [], 'years': [], 'dates': [], 'boards': []} for f in all_factor_names}
    total_dates = 0
    n_total = len(stock_codes)
    done = 0

    def log(msg):
        if progress_cb:
            progress_cb(msg)
        else:
            print(msg)

    ctx = mp.get_context('spawn')
    try:
        with ProcessPoolExecutor(max_workers=n_workers, mp_context=ctx,
                                 initializer=init_worker,
                                 initargs=(list(stock_codes),)) as executor:
            iterables = (stock_codes,
                         repeat(hold_days), repeat(sample_interval), repeat(years),
                         repeat(all_factor_names), repeat(factor_configs),
                         repeat(active_factors), boards)
            for rows, n_sampled in executor.map(_process_one_stock, *iterables):
                done += 1
                for valid, fut_ret, year, sample_date, stock_board in rows:
                    for f, v in valid.items():
                        samples[f]['vals'].append(v)
                        samples[f]['rets'].append(fut_ret)
                        samples[f]['years'].append(year)
                        samples[f]['dates'].append(sample_date)
                        samples[f]['boards'].append(stock_board)  # P5：记录板块
                total_dates += n_sampled
                if progress_cb and (done % 200 == 0 or done == n_total):
                    log(f"[因子IC][多核] 进度: {done}/{n_total} 只 | 采样点 {total_dates}")
                if cancel_event is not None and cancel_event.is_set():
                    log("⏹ 已收到停止信号，因子IC回测中止。")
                    if sys.version_info >= (3, 9):
                        executor.shutdown(wait=False, cancel_futures=True)
                    else:
                        executor.shutdown(wait=False)
                    raise BacktestCancelled()
    except BacktestCancelled:
        raise
    except Exception as e:
        log(f"[因子IC] ⚠ 多核汇总异常: {e}")
        raise
    return samples, total_dates


def parallel_fetch(stock_codes, n_threads=8):
    """用 ThreadPoolExecutor 并行抓取未命中缓存的股票（网络 I/O 密集，真并行）。

    直接复用 backtest._fetch_stock_data（其内部 requests.get 等待时释放 GIL）。
    返回成功抓取的数量。
    """
    import concurrent.futures
    from .backtest import _fetch_stock_data

    success = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=n_threads) as ex:
        futures = [ex.submit(_fetch_stock_data, sc) for sc in stock_codes]
        for fut in concurrent.futures.as_completed(futures):
            try:
                df = fut.result()
                if df is not None:
                    success += 1
            except Exception:
                pass
    return success
