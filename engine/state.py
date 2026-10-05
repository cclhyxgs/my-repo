#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""全局状态管理"""

from collections import OrderedDict
import hashlib
import os
import time
import threading
from engine.config import get_app_dir

K_TYPE_MAP = {
    "日K": 240, "周K": 1200,
    "60分钟": 60, "30分钟": 30, "15分钟": 15, "5分钟": 5, "1分钟": 1,
}
# 分钟级 k_type 集合（新浪未复权源，getKLineData scale=1/5/15/30/60 均支持）
MINUTE_K_TYPES = (1, 5, 15, 30, 60)

def _get_cache_config():
    from engine.config import C
    cfg = C.get('cache', {})
    return cfg.get('max_size', 100), cfg.get('ttl_seconds', 300)


def get_kline_disk_path(key):
    """返回 (code, k_type, days) 对应的 K 线磁盘缓存 pickle 路径。

    路径基于 get_app_dir()（frozen 时 = exe 目录，持久可写），
    绝不使用 _MEIPASS 或 __file__，保证重启/重复扫描可复用历史。
    """
    return os.path.join(get_app_dir(), 'cache', 'kline', f'{key}.pkl')


def get_qfq_daily_disk_path(code):
    """返回按代码维度的统一日线前复权缓存 pickle 路径。

    与 get_kline_disk_path(按 code+ktype+days 分文件) 不同，本路径一票一个文件、
    存全历史，供回测与全市场扫描共用同一 store，避免维护独立的
    backtest_data_cache_qfq.pkl 大文件。
    """
    return os.path.join(get_app_dir(), 'cache', 'kline', f'qfq_daily_{code}.pkl')


def get_raw_daily_disk_path(code, k_type=240):
    """返回按代码维度的统一日线/周线【未复权】缓存路径（新浪源）。

    ⚠️ 与 get_qfq_daily_disk_path 是**两个物理隔离的 store，绝不可混用**：
    前者是前复权、后者是未复权，复权尺度不同，混并会让收益/因子错位。
    回测的 load_qfq_daily 只读 qfq_daily_*，不会读到这里。
    """
    tag = 'weekly' if int(k_type) == 1200 else 'daily'
    return os.path.join(get_app_dir(), 'cache', 'kline', f'raw_{tag}_{code}.pkl')


class AppState:
    def __init__(self):
        self.stock_code = None
        self.stock_name = None
        self.kline_df = None
        self.display_mode = None
        self.name_to_code = {}
        self.code_to_name = {}
        self.raw_code_to_name = {}
        self.is_loading = False
        self.cache = OrderedDict()
        self._cache_lock = threading.RLock()


state = AppState()


def get_cache_key(stock_code, k_type, days):
    return hashlib.md5(f"{stock_code}_{k_type}_{days}".encode()).hexdigest()


def get_cached(key):
    max_size, ttl = _get_cache_config()
    with state._cache_lock:
        if key in state.cache:
            data, timestamp = state.cache[key]
            if time.time() - timestamp < ttl:
                state.cache.move_to_end(key)
                return data
            else:
                del state.cache[key]
    return None


def set_cache(key, data):
    max_size, ttl = _get_cache_config()
    with state._cache_lock:
        if max_size > 0 and len(state.cache) >= max_size and state.cache:
            oldest = next(iter(state.cache))
            del state.cache[oldest]
        state.cache[key] = (data, time.time())
        state.cache.move_to_end(key)
