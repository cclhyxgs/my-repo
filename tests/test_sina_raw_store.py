#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""新浪【未复权】K 线落盘（独立 raw store）回归测试。

背景（2026-09-17，用户要求「新浪数据源全市场扫描也要落盘」）：
新浪源原先**一律不落盘**（只有内存 TTL 300s）—— 因为未复权数据若写进
`qfq_daily_*`（前复权）store 并打上 `_qfq_flag=1`，会**冒充前复权**喂给回测，
造成收益/因子错位。代价是 `cache/kline` 恒空、扫描每次冷抓（5200 只约 40 分钟）。

修复方式：给未复权数据一个**物理隔离**的 store
（`raw_daily_{code}.pkl` / `raw_weekly_{code}.pkl`，带 `_qfq_flag=0` 显式标注），
并把它接成「内存 → 磁盘 → 网络」三级缓存。

本测试锁定的不变量：
1. 路径按周期分流，且与 qfq store / hash store **互不重叠**；
2. 落盘带 `_qfq_flag=0`，读回时该列被剥掉（下游不该看到内部标记）；
3. **绝不动 qfq store**（防冒充前复权的核心）；
4. 三级缓存：磁盘足够时不联网；**长度不足必须补抓**（raw store 按代码维度建文件、
   不含 days，这是最容易漏的一环）；
5. 并集累积：递增天数请求后 store 变长，而不是互相覆盖；
6. 分钟级不落盘。
"""
import os
import pickle
from datetime import datetime, timedelta

import pandas as pd
import pytest


def _mkdf(n, start='2025-01-01'):
    """造 n 根连续工作日 K 线（列与真实落盘一致）。"""
    return pd.DataFrame({
        'trade_time': pd.date_range(start, periods=int(n), freq='B'),
        'open': [1.0] * int(n),
        'high': [1.1] * int(n),
        'low': [0.9] * int(n),
        'close': [1.0] * int(n),
        'volume': [100] * int(n),
    })


@pytest.fixture
def iso(tmp_path, monkeypatch):
    """隔离应用数据目录。

    ⚠️ 必须用 monkeypatch（用完自动恢复），**不可** `os.environ.setdefault`：
    那样会把 QUANT_SYSTEM_DIR 泄漏到整个 pytest 进程，令 h5_skeleton 的
    conftest 隔离失效（曾造成 16 errors / 5 failed 的假回归）。
    """
    monkeypatch.setenv('QUANT_SYSTEM_DIR', str(tmp_path))
    return tmp_path


def _kline_dir():
    from engine.state import get_raw_daily_disk_path
    return os.path.dirname(get_raw_daily_disk_path('probe', 240))


# ── 1. 路径隔离 ────────────────────────────────────────────────
def test_paths_are_distinct(iso):
    from engine.state import get_raw_daily_disk_path, get_qfq_daily_disk_path
    p_daily = get_raw_daily_disk_path('sh600000', 240)
    p_weekly = get_raw_daily_disk_path('sh600000', 1200)
    p_qfq = get_qfq_daily_disk_path('sh600000')

    assert os.path.basename(p_daily) == 'raw_daily_sh600000.pkl'
    assert os.path.basename(p_weekly) == 'raw_weekly_sh600000.pkl'
    assert os.path.basename(p_qfq) == 'qfq_daily_sh600000.pkl'
    assert len({p_daily, p_weekly, p_qfq}) == 3
    # 同一个 cache/kline 目录下（便于统一搬迁/统计），但文件名不同
    assert len({os.path.dirname(p) for p in (p_daily, p_weekly, p_qfq)}) == 1


# ── 2/3. 标记、读回剥列、与 qfq store 隔离 ─────────────────────
def test_save_marks_unadjusted_and_never_touches_qfq_store(iso):
    from engine.data_layer import KLineFetcher
    from engine.state import get_qfq_daily_disk_path, get_raw_daily_disk_path

    KLineFetcher._save_raw_daily_disk('sh600000', 240, _mkdf(10))

    raw_path = get_raw_daily_disk_path('sh600000', 240)
    assert os.path.isfile(raw_path)
    with open(raw_path, 'rb') as f:
        obj = pickle.load(f)
    assert set(obj['_qfq_flag']) == {0}, '未复权 store 必须带 _qfq_flag=0 标注'

    # 核心不变量：raw 落盘绝不可创建/触碰前复权 store
    assert not os.path.exists(get_qfq_daily_disk_path('sh600000')), \
        '写 raw store 不得创建 qfq_daily_*（会让未复权数据冒充前复权）'
    assert KLineFetcher._load_qfq_daily_disk('sh600000') is None


def test_load_strips_internal_flag(iso):
    from engine.data_layer import KLineFetcher

    KLineFetcher._save_raw_daily_disk('sh600000', 240, _mkdf(10))
    df = KLineFetcher._load_raw_daily_disk('sh600000', 240)

    assert df is not None and len(df) == 10
    assert '_qfq_flag' not in df.columns, '内部标记不应泄漏给下游'
    assert list(df.columns) == ['trade_time', 'open', 'high', 'low', 'close', 'volume']


def test_load_missing_returns_none(iso):
    from engine.data_layer import KLineFetcher
    assert KLineFetcher._load_raw_daily_disk('sz999999', 240) is None


# ── 5. 并集累积 ────────────────────────────────────────────────
def test_save_unions_by_trade_time(iso):
    """未复权历史是确定性的 → 按 trade_time 并集合并（新值覆盖旧值）。"""
    from engine.data_layer import KLineFetcher
    from engine.state import get_raw_daily_disk_path

    out1 = KLineFetcher._save_raw_daily_disk('sh600000', 240, _mkdf(10, '2026-01-01'))
    assert out1 is not None and len(out1) == 10

    # 第二批与第一批重叠 5 天（1/8~1/14），另带 10 天新数据
    out2 = KLineFetcher._save_raw_daily_disk('sh600000', 240, _mkdf(15, '2026-01-08'))
    with open(get_raw_daily_disk_path('sh600000', 240), 'rb') as f:
        obj = pickle.load(f)
    assert len(obj['trade_time']) == 20, f"并集应为 10+15-5=20，实际 {len(obj['trade_time'])}"

    # 返回值即合并结果，且已剥掉内部标记（供调用方直接使用）
    assert len(out2) == 20
    assert '_qfq_flag' not in out2.columns


# ── 4/6. 三级缓存 / 分钟不落盘 ─────────────────────────────────
def test_three_level_cache_and_refetch_when_too_short(iso, monkeypatch):
    """内存命中 → 不联网；磁盘够长 → 不联网；**磁盘偏短 → 必须补抓**。"""
    from engine import data_layer as dl
    from engine.state import state

    calls = []

    def fake_fetch(code, k_type, days):
        calls.append(int(days))
        # 必须造「截止今天」的数据：造旧日期会被新鲜度判定视为陈旧而触发补抓
        # （那是正确行为，但会让本用例想验证的「长度语义」失真）。
        return _mkdf_ending(datetime.now().date(), int(days)), None

    monkeypatch.setattr(dl.KLineFetcher, '_fetch_sina_minute', staticmethod(fake_fetch))

    d1 = dl.KLineFetcher._fetch_sina_raw_cached('sh600000', 240, 30)
    assert len(d1) == 30 and calls == [30]

    # 内存命中
    dl.KLineFetcher._fetch_sina_raw_cached('sh600000', 240, 30)
    assert calls == [30], '内存命中不应再取数'

    # 内存清了 → 走磁盘；store 30 行已满足 30 天
    state.cache.clear()
    d3 = dl.KLineFetcher._fetch_sina_raw_cached('sh600000', 240, 30)
    assert len(d3) == 30
    assert calls == [30], '磁盘长度足够时不应联网'

    # 关键：raw store 按代码维度建文件、不含 days，所以「命中」≠「够长」
    state.cache.clear()
    d4 = dl.KLineFetcher._fetch_sina_raw_cached('sh600000', 240, 60)
    assert calls == [30, 60], f'磁盘仅 30 行、请求 60 天时必须补抓，实际 {calls}'
    assert len(d4) == 60


def test_minute_kline_not_persisted(iso, monkeypatch):
    """分钟级数量级大且扫描用不到 → 只走内存，不落盘。"""
    from engine import data_layer as dl

    monkeypatch.setattr(dl.KLineFetcher, '_fetch_sina_minute',
                        staticmethod(lambda c, k, d: (_mkdf(10), None)))
    dl.KLineFetcher._fetch_sina_raw_cached('sh600000', 5, 10)

    kdir = _kline_dir()
    files = os.listdir(kdir) if os.path.isdir(kdir) else []
    assert not [f for f in files if f.startswith('raw_')], '分钟级不应落盘'


def test_all_end_to_end_writes_raw_store(iso, monkeypatch):
    """走完整 fetch 链路（mock 掉网络）→ 文件确实出现在 kline 目录。"""
    from engine import data_layer as dl

    monkeypatch.setattr(dl.KLineFetcher, '_fetch_sina_minute',
                        staticmethod(lambda c, k, d: (_mkdf(50), None)))
    # 让 fetch 判定为新浪源
    monkeypatch.setattr(dl.KLineFetcher, '_kline_source_for',
                        staticmethod(lambda k_type: 'sina_raw'))

    df, err = dl.KLineFetcher.fetch('sh600000', 240, 50)
    assert df is not None and len(df) == 50 and err is None

    kdir = _kline_dir()
    files = os.listdir(kdir)
    assert 'raw_daily_sh600000.pkl' in files
    assert not [f for f in files if f.startswith('qfq_daily_')], \
        '新浪源绝不可写 qfq store'


# ── 7. 新鲜度判定 + 增量补抓（供全市场分析复用的正确性前提）─────────
def _mkdf_ending(end, n=300):
    """造截止到 end（含）的 n 根工作日 K 线。"""
    return pd.DataFrame({
        'trade_time': pd.date_range(end=end, periods=int(n), freq='B'),
        'open': [1.0] * int(n), 'high': [1.1] * int(n), 'low': [0.9] * int(n),
        'close': [1.0] * int(n), 'volume': [100] * int(n),
    })


def test_freshness_check_boundaries(iso):
    """新鲜度：覆盖今天/未来 → 新鲜；停在 30 天前 → 陈旧（中间必有工作日）。"""
    from engine.data_layer import KLineFetcher as K
    today = datetime.now().date()
    assert K._raw_store_is_fresh(_mkdf_ending(today, 300)) is True
    assert K._raw_store_is_fresh(_mkdf_ending(today + timedelta(days=1), 300)) is True, \
        '时钟偏差的未来日期不应触发重抓'
    assert K._raw_store_is_fresh(_mkdf_ending(today - timedelta(days=30), 300)) is False
    assert K._raw_store_is_fresh(None) is False
    assert K._raw_store_is_fresh(pd.DataFrame()) is False


def test_stale_store_triggers_incremental_refetch(iso, monkeypatch):
    """陈旧 store **不可**直接复用（否则永远少一根当天 K 线），且应做增量补抓。

    这是「落盘能否供全市场分析长期使用」的关键：若只判长度不判新鲜度，
    隔天扫描会静默吃旧数据；若补抓不做增量，隔天扫描又退化成整段重抓（41 分钟）。
    """
    from engine import data_layer as dl

    today = datetime.now().date()
    dl.KLineFetcher._save_raw_daily_disk(
        'sh600000', 240, _mkdf_ending(today - timedelta(days=30), 300))

    calls = []

    def fake_fetch(code, k_type, days):
        calls.append(int(days))
        return _mkdf_ending(today, int(days)), None

    monkeypatch.setattr(dl.KLineFetcher, '_fetch_sina_minute', staticmethod(fake_fetch))

    df = dl.KLineFetcher._fetch_sina_raw_cached('sh600000', 240, 300)

    assert calls, '陈旧 store 必须触发补抓'
    assert calls[0] < 300, f'应增量补抓（远小于 300 根），实际抓了 {calls[0]} 根'
    # 并集后应已覆盖到最近。⚠️ 不能用 `today - 1天`：今天是周末/节假日时最近交易日会更早
    # （2026-09-20 周日 实测：最近交易日是 09-18 周五，原断言直接失败）→ 回退到最近工作日。
    _last_bd = today
    while _last_bd.weekday() >= 5:      # 5=Sat 6=Sun
        _last_bd -= timedelta(days=1)
    assert pd.to_datetime(df['trade_time']).max().date() >= _last_bd


def test_fresh_store_served_from_disk_without_network(iso, monkeypatch):
    """新鲜且够长 → 纯磁盘命中，一次请求都不发。"""
    from engine import data_layer as dl
    from engine.state import state

    today = datetime.now().date()
    dl.KLineFetcher._save_raw_daily_disk('sh600000', 240, _mkdf_ending(today, 300))

    calls = []
    monkeypatch.setattr(dl.KLineFetcher, '_fetch_sina_minute',
                        staticmethod(lambda c, k, d: calls.append(d) or (_mkdf_ending(today, 300), None)))

    state.cache.clear()
    df = dl.KLineFetcher._fetch_sina_raw_cached('sh600000', 240, 300)

    assert calls == [], f'新鲜缓存不应联网，实际请求 {calls}'
    assert len(df) == 300


def test_network_failure_falls_back_to_stale_disk(iso, monkeypatch):
    """补抓失败时降级用陈旧磁盘数据 —— 有（略滞后）好过没有。"""
    from engine import data_layer as dl
    from engine.state import state

    today = datetime.now().date()
    dl.KLineFetcher._save_raw_daily_disk(
        'sh600000', 240, _mkdf_ending(today - timedelta(days=30), 300))
    monkeypatch.setattr(dl.KLineFetcher, '_fetch_sina_minute',
                        staticmethod(lambda c, k, d: (None, 'simulated failure')))

    state.cache.clear()
    df = dl.KLineFetcher._fetch_sina_raw_cached('sh600000', 240, 300)

    assert df is not None and len(df) == 300, '联网失败应降级返回磁盘旧数据'
