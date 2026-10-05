# -*- coding: utf-8 -*-
"""进程内滑动窗口限流（F-305「独立限流桶」）。

前提与边界
----------
- 当前 **api 单副本**（R-09 未解决前禁止扩副本），故进程内计数即可；
  扩到多副本后计数会各算各的，届时必须换 Redis（与 P1/P2 的缓存层一起做，非本次范围）。
- 无账号体系 → 限流键只能用**客户端 IP**（授权/账号落地后可改为按用户）。
- 桶之间**互不共享配额**：`quote_only` 与将来的 `analyze` 是两套计数，
  "仅行情"被限流不会牵连分析请求，反之亦然（正是 F-305 平台差异列要的"避免被挤占"）。

算法：滑动窗口（deque 存命中时刻），比固定窗口更平滑，不会在窗口边界出现 2 倍突刺。
"""

import os
import threading
import time
from collections import deque

_LOCK = threading.Lock()
_HITS: dict = {}

# 默认配额（可用 env 覆盖）
QUOTE_ONLY_LIMIT = int(os.environ.get("MBULL_RL_QUOTE_ONLY_LIMIT", "30"))
QUOTE_ONLY_WINDOW = float(os.environ.get("MBULL_RL_QUOTE_ONLY_WINDOW", "60"))


def allow(bucket: str, key: str, limit: int, window: float):
    """判定是否放行。

    Returns:
        (allowed: bool, retry_after: float)
        - allowed=False 时 `retry_after` 为还需要等待的秒数（>0）
    """
    now = time.monotonic()
    slot = (bucket, key)
    with _LOCK:
        hits = _HITS.setdefault(slot, deque())
        cutoff = now - window
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if len(hits) >= limit:
            return False, max(hits[0] + window - now, 0.001)
        hits.append(now)
        return True, 0.0


def clear(bucket: str = None) -> None:
    """清空计数（配置热重载 / 测试用）。`bucket=None` 清空全部。"""
    with _LOCK:
        if bucket is None:
            _HITS.clear()
        else:
            for slot in [s for s in _HITS if s[0] == bucket]:
                _HITS.pop(slot, None)


def snapshot() -> dict:
    """当前各桶命中数（诊断/测试用）。"""
    with _LOCK:
        return {f"{b}:{k}": len(v) for (b, k), v in _HITS.items()}
