#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""回测运行器 — 把现成的回测脚本封装为可调用工具

提供三个高层函数，供 GUI「回测验证」页在后台线程中调用：
  - run_strategy(...)   策略回测（加/减仓 + 净值复利），复用 backtest_strategy.StrategyBacktester
  - run_score_ic(...)   评分 IC 回测（分档/年度 IC/相关性），复用 backtest.Backtester
  - run_factor_ic(...)  因子 IC 回测（每个因子相对未来收益的 Rank-IC），复用 engine.factor_ic_backtest

所有函数：
  - 直接使用 quant_config._cache 中当前配置（由调用方注入，不读写磁盘 quant_model.json）
  - 通过 progress_cb(line) 实时回传日志，便于 UI 展示
  - 用 prefix 隔离输出文件，避免覆盖规范产物（strategy_equity.csv 等）
"""
import os
import json
import contextlib
from datetime import datetime, timedelta

from . import backtest as _btmod
from . import backtest_strategy as _stratmod
from engine import factor_ic_backtest
from engine.config import get_app_dir
import pandas as pd
from engine.data_layer import SCAN_KLINE_DAYS

ROOT = os.path.join(get_app_dir(), 'reports')
# 回测产物统一输出到 reports/ 子目录（不再散落根目录，保持项目/数据目录清爽）；
# 旧版产物在 get_app_dir() 根目录，读取方做兼容回退（见 web_api._bt_locate_file）。
try:
    os.makedirs(ROOT, exist_ok=True)
except Exception:
    pass


class _LineWriter:
    """把 stdout 按行转发给 progress_cb（线程安全由调用方保证）"""

    def __init__(self, cb):
        self.cb = cb
        self._buf = ''

    def write(self, s):
        if not self.cb:
            return
        self._buf += s
        while '\n' in self._buf:
            line, self._buf = self._buf.split('\n', 1)
            if line.strip():
                self.cb(line)

    def flush(self):
        if self._buf and self.cb:
            self.cb(self._buf)
            self._buf = ''


def _redirect(progress_cb):
    if progress_cb:
        return contextlib.redirect_stdout(_LineWriter(progress_cb))
    return contextlib.nullcontext()


def _ns(**kw):
    return type('Args', (), kw)()


def _data_span():
    """预载后从全局缓存推导数据可用区间 (min_date, max_date)，无数据返 None。

    三套回测（strategy/scoreic/factoric）预载后数据均落在 backtest._stock_data_cache，
    数据来自本地统一 store（qfq_daily_*.pkl）。该 store 在 tdx 接入后可能保留全历史
    （而非旧版的约 300 交易日），因此实际可用区间可能长达数十年。UI 不传 years 时，
    回测区间由此范围决定；传 years 时则按 end - years*365 截断。
    """
    try:
        from .backtest import _stock_data_cache
    except Exception:
        return None
    lo = hi = None
    for df in _stock_data_cache.values():
        if df is None or not hasattr(df, 'columns') or 'trade_time' not in df.columns or len(df) == 0:
            continue
        try:
            a = pd.Timestamp(df['trade_time'].min())
            b = pd.Timestamp(df['trade_time'].max())
        except Exception:
            continue
        lo = a if lo is None else (a if a < lo else lo)
        hi = b if hi is None else (b if b > hi else hi)
    if lo is None or hi is None:
        return None
    return lo, hi


def run_strategy(years=None, full_market=False, scan_interval=5,
                 prefix='bt_strategy', progress_cb=None, cancel_event=None, pool=None,
                 offline=False, equity_cb=None):
    """策略回测（净值复利 + 加/减仓）。返回报告 dict。

    时间止损窗口（time_stop_days）由 StrategyBacktester 内部从 config 的
    risk_params.time_stop_days 读取（本函数已移除旧 time_stop_days 参数），
    不再接受运行面板覆盖——避免运行面板压过用户在风险面板里配置的 time_stop_days。

    years: 回测年数。None（UI 不传，默认）= 数据驱动区间——回测区间取「扫描缓存实际
        可用范围」（约300交易日），用满数据；传数字则按 end - years*365 推算（开发者
        CLI 手动覆盖用）。

    offline: True 时仅使用本地缓存，未命中缓存的股票直接跳过，不联网抓取。

    equity_cb: 可选回调 equity_cb(point:dict)，回测过程中按节流回传净值点
        （{'d','nav','dd','dr','pos'}），供 UI 边跑边画曲线。**仅供过程展示**，
        终态曲线仍以 equity.csv 为准。None = 不实时回传（默认，行为与旧版一致）。
    """
    with _redirect(progress_cb):
        today = datetime.now()
        end = today - timedelta(days=30)
        if years is None:
            # 占位区间即可：预载不依赖 start/end，之后再按数据范围覆盖
            start = end - timedelta(days=400)
        else:
            start = end - timedelta(days=int(years * 365))

        bt = _stratmod.StrategyBacktester(
            start_date=start.strftime('%Y-%m-%d'),
            end_date=end.strftime('%Y-%m-%d'),
            max_stocks=None,
            full_market=full_market,
            return_years=(years if years is not None else 1),
            pool=pool,
            offline_mode=offline,
        )
        # 时间止损窗口由 config 的 risk_params.time_stop_days 决定（见
        # StrategyBacktester.__init__），此处不再覆盖——保持运行面板与策略时间止损解耦。
        bt.SCAN_INTERVAL_DAYS = scan_interval
        bt.cancel_event = cancel_event
        bt.equity_cb = equity_cb

        bt.equity_csv_path = os.path.join(ROOT, f'{prefix}_equity.csv')
        bt.trades_json_path = os.path.join(ROOT, f'{prefix}_trades.json')
        bt.report_json_path = os.path.join(ROOT, f'{prefix}_report.json')

        bt.preload_all_data()
        # 数据驱动区间：UI 未指定 years 时，回测区间跟随扫描缓存实际可用范围（用满300天）
        # 注意：回测类构造函数会把 start/end 解析成 datetime，此处也必须赋 datetime 对象，
        # 否则下游 (self.end_date - self.start_date).days / .strftime() 会因 str 类型崩溃。
        if years is None:
            span = _data_span()
            if span:
                lo, hi = span
                bt.start_date = lo.to_pydatetime()
                bt.end_date = hi.to_pydatetime()
                print(f"[区间] 数据驱动(扫描缓存可用范围): {bt.start_date.strftime('%Y-%m-%d')} ~ {bt.end_date.strftime('%Y-%m-%d')}")
        report = bt.run()
        if report:
            with open(bt.report_json_path, 'w', encoding='utf-8') as f:
                json.dump(report, f, ensure_ascii=False, indent=2, default=str)
            print(f"结果已保存: {bt.report_json_path}")
        return report


def run_score_ic(years=None, full_market=False, hold_days=10, sample_interval=5,
                 prefix='bt_scoreic', progress_cb=None, cancel_event=None, pool=None,
                 offline=False):
    """评分 IC 回测（分档累计复利 / 年度 IC / 相关性）。返回报告 dict。

    years: 回测年数。None（UI 不传，默认）= 数据驱动区间（用满扫描缓存约300交易日）；
        传数字则按 end - years*365 推算（开发者 CLI 手动覆盖用）。

    offline: True 时仅使用本地缓存，未命中缓存的股票直接跳过，不联网抓取。
    """
    with _redirect(progress_cb):
        # 评分IC回测依赖完整方案：空/不完整方案下评分内核会对 None 参数运算抛异常，
        # 被 backtest._bt_compute_score_classify 的 except 静默吞掉，静默产出 0 样本、
        # 浪费数分钟。子进程层提前拦截并返回明确错误。
        # 配置自检（局部契约，无中央门禁）：检查 CLI 注入的 _cache，
        # 缺失时由 require_config 抛 ConfigIncompleteError（与引擎入口同机制）。
        try:
            from engine import quant_config as _qc
            _qc.require_config(_qc._cache, [
                ('因子配置(factor_configs)', 'factor_configs'),
                ('评分缩放(score_scale)', 'score_scale'),
                ('状态分界(thresholds)', 'thresholds'),
                ('冲突惩罚(conflict_penalty)', 'conflict_penalty'),
            ])
        except _qc.ConfigIncompleteError as e:
            print('[拦截] 当前方案不完整（' + '、'.join(e.missing) + '），'
                  '评分IC回测无法计算综合评分。请先在【模型配置】中完成方案配置后再运行。')
            return {'error': 'config_incomplete',
                    'message': '方案不完整，评分IC回测需要完整方案（' + '、'.join(e.missing) + '）'}
        except Exception as e:
            print(f'[拦截] 配置自检异常: {e}')
            return {'error': 'config_check_failed', 'message': f'配置自检异常: {e}'}
        today = datetime.now()
        end = today - timedelta(days=hold_days + 15)
        if years is None:
            start = end - timedelta(days=400)  # 占位，预载后由数据范围覆盖
        else:
            start = end - timedelta(days=int(years * 365))

        bt = _btmod.Backtester(
            start_date=start.strftime('%Y-%m-%d'),
            end_date=end.strftime('%Y-%m-%d'),
            hold_days=hold_days,
            sample_interval=sample_interval,
            max_stocks=None,
            full_market=full_market,
            return_years=(years if years is not None else 1),
            pool=pool,
            offline_mode=offline,
        )
        bt.cancel_event = cancel_event
        bt.decile_csv_path = os.path.join(ROOT, f'{prefix}_decile_equity_h{hold_days}.csv')
        bt.report_json_path = os.path.join(ROOT, f'{prefix}_report.json')

        bt.preload_all_data()
        # 数据驱动区间：UI 未指定 years 时，回测区间跟随扫描缓存实际可用范围（用满300天）
        # 注意：回测类构造函数会把 start/end 解析成 datetime，此处也必须赋 datetime 对象，
        # 否则下游 (self.end_date - self.start_date).days / .strftime() 会因 str 类型崩溃。
        if years is None:
            span = _data_span()
            if span:
                lo, hi = span
                bt.start_date = lo.to_pydatetime()
                bt.end_date = hi.to_pydatetime()
                print(f"[区间] 数据驱动(扫描缓存可用范围): {bt.start_date.strftime('%Y-%m-%d')} ~ {bt.end_date.strftime('%Y-%m-%d')}")
        report = bt.run()
        if report:
            # 仅当显式指定 years 时，用占位 start/end 覆盖报告区间；
            # 数据驱动模式(bt 已被 _data_span 设为真实扫描区间)沿用 bt 自身的 start/end，避免写错。
            if years is not None:
                report['start_date'] = start.strftime('%Y-%m-%d')
                report['end_date'] = end.strftime('%Y-%m-%d')
            with open(bt.report_json_path, 'w', encoding='utf-8') as f:
                json.dump(report, f, ensure_ascii=False, indent=2, default=str)
            print(f"结果已保存: {bt.report_json_path}")
        return report


def run_factor_ic(years=None, full_market=False, hold_days=10, sample_interval=5,
                  prefix='bt_factoric', progress_cb=None, cancel_event=None,
                  n_workers=None, n_fetch_threads=4, pool=None,
                  offline=False):
    """因子 IC 回测（每个因子的 Rank-IC）。返回报告 dict。

    years: 回测年数。None（UI 不传，默认）= 取满扫描缓存（约300交易日），等价于
        years≈SCAN_KLINE_DAYS/365；传数字则按 years*365 行回看（开发者 CLI 手动覆盖用）。

    n_workers 默认 None → 由 engine.factor_ic_backtest 自动按 CPU 核数并行（上限 4）。
    n_fetch_threads 默认 4 → 缓存预加载时并发拉取未命中股票，加速冷启动。
    offline: True 时仅使用本地缓存，未命中缓存的股票直接跳过，不联网抓取。
    """
    if years is None:
        # UI 未设置回测年数：因子IC回测按「扫描缓存全部交易日」回看（约300交易日）。
        # 用 365.0（非闰年平均 365.25）保证 int(years*365)==SCAN_KLINE_DAYS 精确取满，不漏 1 天。
        years = SCAN_KLINE_DAYS / 365.0
    with _redirect(progress_cb):
        return factor_ic_backtest.run_factor_ic(
            years=years, full_market=full_market, hold_days=hold_days,
            sample_interval=sample_interval, prefix=prefix, progress_cb=progress_cb,
            cancel_event=cancel_event, n_workers=n_workers, n_fetch_threads=n_fetch_threads,
            pool=pool, offline=offline)


def run_futures(pool='all', direction='long', period='日线', days=300, capital=None,
                prefix='bt_futures', progress_cb=None, cancel_event=None, offline=False):
    """期货策略回测（全生命周期 PnL）。返回报告 dict。

    数据：futures_data.batch_fetch 拉主力连续日线；引擎：backtest_futures.FuturesBacktester。
    方向化参数取自对应期货 scheme（FuturesBacktester 内已用 load_market_scheme 解析）。
    """
    with _redirect(progress_cb):
        from .backtest_futures import FuturesBacktester, batch_fetch, INITIAL_CAPITAL
        from engine.futures_pool import resolve_pool as futures_resolve_pool
        from engine import quant_config as _qc
        # 1) 解析品种池（'all'/板块/逗号代码/'watchlist'）
        if pool == 'watchlist':
            from engine.futures_pool import get_watchlist_futures_pool
            contracts = get_watchlist_futures_pool()
        else:
            contracts = futures_resolve_pool(pool)
        if not contracts:
            print("[期货回测] 错误：品种池为空。")
            return {'error': 'empty_pool', 'message': '期货品种池为空'}
        # 2) 拉取日线
        if offline:
            print("[期货回测] 离线模式：未缓存数据将被跳过")
        data = batch_fetch(contracts, days=days)
        if not data:
            print("[期货回测] 错误：K线数据获取失败（检查网络/数据源）。")
            return {'error': 'no_data', 'message': '期货K线数据获取失败'}
        # 3) 加载期货方案（方向化参数），回测
        try:
            _qc.load_market_scheme('futures', direction, period, force_reload=True)
        except Exception as e:
            print(f"[期货回测] ⚠ 加载期货方案失败：{e}")
        bt = FuturesBacktester(capital=capital if capital else INITIAL_CAPITAL)
        bt.cancel_event = cancel_event
        bt.run(data, cancel_event=cancel_event)
        # 4) 写出结果（对齐 backtest_futures.main()）
        report = bt.report()
        if 'error' in report:
            print(f"[期货回测] {report['error']}")
            return report
        eq_df = pd.DataFrame(bt.equity_curve)
        eq_path = os.path.join(ROOT, f'{prefix}_equity.csv')
        eq_df.to_csv(eq_path, index=False)
        trades_df = pd.DataFrame(bt.trade_log)
        trades_path = os.path.join(ROOT, f'{prefix}_trades.json')
        trades_df.to_json(trades_path, orient='records', force_ascii=False)
        report['equity_csv'] = eq_path
        report['trades_json'] = trades_path
        report_path = os.path.join(ROOT, f'{prefix}_report.json')
        with open(report_path, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)
        print(f"结果已保存: {report_path}")
        return report


def run_futures_factor_ic(years=None, pool='all', hold_days=10, sample_interval=5,
                          direction='long', period='日线',
                          prefix='bt_futures_factoric', progress_cb=None,
                          cancel_event=None, offline=False, multi_horizon=False):
    """期货因子 IC 回测（每个因子的 Rank-IC）。

    复用 engine.factor_ic_backtest.run_factor_ic 的 market='futures' 模式开关，
    与 A股 IC 共用同一套截面IC/板块中性化逻辑（非平行 fork）。
    """
    with _redirect(progress_cb):
        if years is None:
            years = SCAN_KLINE_DAYS / 365.25
        return factor_ic_backtest.run_factor_ic(
            years=years, full_market=False, hold_days=hold_days,
            sample_interval=sample_interval, prefix=prefix, progress_cb=progress_cb,
            cancel_event=cancel_event, pool=pool, offline=offline,
            multi_horizon=multi_horizon, market='futures',
            direction=direction, period=period)
