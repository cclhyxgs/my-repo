#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""K 线缓存批量刷新（**app 内置调度与 CLI 共用**）。

为什么放进 engine 而不是留在 audit/
-----------------------------------
内置调度（`engine/kline_scheduler.py`）与 CLI（`audit/_prefetch_kline_cache.py`）
需要同一套「找缺口 → 逐只取 → 配额耗尽优雅停止」逻辑；audit 不在打包白名单里，
放进 engine 才能被 App 复用，避免两处实现漂移。

三种模式
--------
- `fill`    : 只处理**尚未缓存**的票（首次全市场预热）。逐只整段抓取，消耗配额大。
- `refresh` : 处理全部票。已缓存的走「当日 bar 快速通道」（零远程 K 线请求），
              未缓存的才整段抓取。
- `daily`   : **已缓存的优先**（先走快速通道把当日数据补齐），剩余额度再补未缓存的。
              内置调度的默认模式 —— 保证「打开软件就是当天数据」，同时逐步消化缺口。

跑不完是**预期行为**而非故障：K 线主源为通达信（TCP 7709，无 HTTP 配额），瓶颈是
TCP 逐票吞吐；一旦回退到腾讯通道，单入口 ~350 次/窗口、窗口 ≥23 分钟，全市场 5200 只
更不可能一轮拉完。故脚本/调度都是「优雅停止 + 下次续跑」。
"""
import json
import logging
import os
import time

logger = logging.getLogger(__name__)


def all_codes():
    """全市场代码：直接读本地股票列表缓存（纯本地，不联网）。

    刻意不用 `StockListLoader.load()` —— 它返回条数(int) 而非映射，且缓存失效时
    会去联网拉 52 页新浪，在刷新热路径里不可接受。
    """
    from engine.config import STOCK_LIST_CACHE_FILE
    from engine.data_layer import MIN_STOCK_COUNT
    try:
        if not os.path.isfile(STOCK_LIST_CACHE_FILE):
            return []
        with open(STOCK_LIST_CACHE_FILE, 'r', encoding='utf-8') as f:
            d = json.load(f)
        codes = d.get('code_to_name') if isinstance(d, dict) else None
        if not codes or len(codes) < MIN_STOCK_COUNT:
            logger.warning('K线刷新：股票列表缓存缺失/不足(%s)',
                           len(codes) if codes else 0)
            return []
        return sorted(codes.keys())
    except Exception as e:
        logger.warning('K线刷新：读股票列表缓存失败: %s', e)
        return []


def cached_codes():
    """已缓存前复权日线的代码集合。"""
    from engine.state import get_qfq_daily_disk_path
    try:
        d = os.path.dirname(get_qfq_daily_disk_path('probe'))
        if not os.path.isdir(d):
            return set()
        return {f[len('qfq_daily_'):-4] for f in os.listdir(d)
                if f.startswith('qfq_daily_') and f.endswith('.pkl')}
    except Exception as e:
        logger.warning('K线刷新：读缓存目录失败: %s', e)
        return set()


def refresh(mode='daily', limit=0, progress=None, k_type=240, days=300):
    """执行一轮刷新。返回统计 dict。

    Args:
        mode: 'fill' | 'refresh' | 'daily'（见模块 docstring）
        limit: 本轮最多处理多少只（0 = 不限，直到配额耗尽）
        progress: 可选回调 fn(i, total, code, ok, err)，用于 CLI 打印进度
        k_type: 240=日K / 1200=周K
        days: 请求根数（也决定缓存 key）
    """
    from engine.data_layer import KLineFetcher

    t0 = time.time()
    market = all_codes()
    if not market:
        return {'ok': 0, 'fail': 0, 'quota_stop': False, 'elapsed': 0.0,
                'cached_total': 0, 'market_total': 0, 'remaining': 0,
                'error': '股票列表不可用'}

    done = cached_codes()
    if mode == 'fill':
        todo = [c for c in market if c not in done]
    elif mode == 'refresh':
        todo = list(market)
    else:      # daily：已缓存的优先（走快速通道），余下配额再补未缓存的
        todo = [c for c in market if c in done] + [c for c in market if c not in done]
        # 已缓存的票刷新代价极低（零 fqkline 请求），但若缺失量很大，
        # 全量遍历会把「补齐」无限期饿死 —— 故给未缓存的票也留出份额。
        if limit <= 0 and len(done) > 0 and len(market) > len(done):
            limit = max(len(done) + 200, 1000)
    if limit > 0:
        todo = todo[:limit]

    ok = fail = 0
    quota_stop = False
    for i, code in enumerate(todo, 1):
        try:
            df, err = KLineFetcher.fetch(code, k_type, days)
        except Exception as e:
            df, err = None, str(e)
        good = df is not None and len(df) > 0
        ok += 1 if good else 0
        fail += 0 if good else 1
        if progress:
            try:
                progress(i, len(todo), code, good, err)
            except Exception:
                pass
        if not good and err and ('配额' in err or '冷却' in err):
            quota_stop = True
            logger.warning('K线刷新：配额耗尽，本轮提前结束（预期行为），已处理 %d 只', i)
            break

    done_after = cached_codes()
    stats = {
        'ok': ok, 'fail': fail, 'quota_stop': quota_stop,
        'elapsed': time.time() - t0,
        'cached_total': len(done_after), 'market_total': len(market),
        'remaining': max(0, len(market) - len(done_after)),
        'planned': len(todo), 'mode': mode,
    }
    # WARNING 而非 INFO：错误日志只收 WARNING+，而这是判断「刷新到底有没跑」的
    # 唯一凭据 —— 记在 INFO 等于不可见（2026-09-18 实测踩到）。
    logger.warning('K线刷新完成(mode=%s): 成功 %d / 失败 %d / 用时 %.0fs；'
                   '累计缓存 %d/%d，仍缺 %d%s',
                   mode, stats['ok'], stats['fail'], stats['elapsed'],
                   stats['cached_total'], stats['market_total'], stats['remaining'],
                   '（配额耗尽提前结束）' if quota_stop else '')
    return stats
