#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""内置定时刷新调度：M-Bull 运行期间自动补齐当日 K 线。

为什么需要
----------
外部定时任务（WorkBuddy automation / Windows 计划任务）都要求**那个调度器本身在运行**。
用户要的是「只要 M-Bull 开着，数据就是最新的」—— 那就必须由 App 自己调度。

触发条件（三个同时满足）
------------------------
① 周一~周五（不处理节假日；非交易日跑一轮也无害：缓存已最新 ⇒ 零请求）
② 当天 **≥15:30**（收盘后，当日 bar 已终值；盘中写缓存会污染前复权序列）
③ **当天还没刷过**（状态文件记录，避免重复跑）

另：启动时若发现「已过收盘时间但当天没刷」，会立即补一次 —— 打开软件就把数据补上。

设计取舍
--------
- **不做首次全市场预热**：一次性、耗时数小时、大量消耗配额，交给 CLI/外部定时任务；
  本模块只负责「日常保鲜」（已缓存的走批量快速通道，全市场约 50 秒）。
  但 `daily` 模式会给未缓存的票保留配额份额，使其顺带逐步消化。
- **绝不打扰用户**：任何异常只写日志，不弹窗、不阻塞（daemon 线程）。
- 可用环境变量关闭：`MBULL_DISABLE_KLINE_SCHEDULER=1`（便于排查）。
"""
import json
import logging
import os
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)

_STATE_FILENAME = 'kline_refresh_state.json'
_CHECK_INTERVAL_SEC = 600          # 每 10 分钟检查一次
_FIRST_CHECK_DELAY_SEC = 90        # 启动后延迟首检，别抢启动资源
_TRIGGER_TIME = (15, 30)           # 当日触发时刻（收盘后）

_started = False
_start_lock = threading.Lock()
_running = False                   # 是否有刷新正在执行（防重入）
_run_lock = threading.Lock()


def _state_path():
    from engine.config import CONFIG_DIR
    return os.path.join(CONFIG_DIR, _STATE_FILENAME)


def _load_state():
    try:
        p = _state_path()
        if os.path.isfile(p):
            with open(p, 'r', encoding='utf-8') as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception as e:
        logger.debug('K线调度：读状态失败（忽略）: %s', e)
    return {}


def _save_state(state):
    try:
        p = _state_path()
        parent = os.path.dirname(p)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = p + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, p)          # 原子替换
    except Exception as e:
        logger.debug('K线调度：写状态失败（忽略）: %s', e)


def is_due(now=None):
    """现在是否该跑一轮（纯判断，不执行）。"""
    now = now or datetime.now()
    if now.weekday() >= 5:                                  # ① 周末不跑
        return False, 'weekend'
    if now.time() < datetime(2000, 1, 1, *_TRIGGER_TIME).time():   # ② 未到收盘后
        return False, 'before_trigger'
    today = now.strftime('%Y-%m-%d')
    if _load_state().get('last_refresh_date') == today:     # ③ 今天已刷过
        return False, 'already_done'
    return True, 'due'


def run_once(mode='daily', limit=0):
    """立即执行一轮刷新（不判断时机）。返回 stats 或 None（已有刷新在跑时）。"""
    global _running
    with _run_lock:
        if _running:
            logger.info('K线调度：已有刷新在执行，跳过本次')
            return None
        _running = True
    try:
        from engine.kline_refresh import refresh
        stats = refresh(mode=mode, limit=limit)
        st = _load_state()
        st['last_refresh_date'] = datetime.now().strftime('%Y-%m-%d')
        st['last_run_at'] = datetime.now().isoformat(timespec='seconds')
        st['last_stats'] = {k: v for k, v in stats.items() if k != 'mode'}
        _save_state(st)
        return stats
    except Exception as e:
        logger.warning('K线调度：刷新异常（忽略，不影响主流程）: %s', e)
        return None
    finally:
        with _run_lock:
            _running = False


def check_and_run(force=False):
    """按条件检查并执行一次。force=True 时跳过时机判断。"""
    if not force:
        due, why = is_due()
        if not due:
            return None, why
    return run_once(), 'ran'


def _loop():
    """守护线程主循环。

    ⚠️ 日志级别刻意用 WARNING：本项目的错误日志只收 WARNING 及以上，而调度是
    daemon 线程 —— 任何静默失败（时机判断异常 / 刷新异常）如果记在 INFO/DEBUG，
    表现就是「功能写了但没生效」且无从排查（2026-09-18 实测踩到：首检异常被
    logger.debug 吞掉，之后 sleep 600s，完全静默）。
    """
    # 首检前先把「本次是否会跑」的原因记下来，便于事后定位
    try:
        due, why = is_due()
        logger.warning('K线调度：线程已起，将于 %d 秒后首检（当前时机: %s，原因: %s）',
                       _FIRST_CHECK_DELAY_SEC, '该跑' if due else '不跑', why)
    except Exception as e:
        logger.warning('K线调度：首检前时机判断异常: %s', e, exc_info=True)
    time.sleep(_FIRST_CHECK_DELAY_SEC)
    while True:
        try:
            stats, why = check_and_run()
            # 刷新成功时由 kline_refresh.refresh() 自己打完成汇总，这里不重复；
            # 只报「本该跑却没跑」的情况。
            if not stats and why != 'already_done':
                logger.warning('K线调度：本轮未执行（%s）', why)
        except Exception as e:                      # 绝不把异常抛到线程外
            logger.warning('K线调度：循环异常（忽略）: %s', e, exc_info=True)
        time.sleep(_CHECK_INTERVAL_SEC)


def maybe_start():
    """启动调度线程（幂等）。应在 App 启动时调用。"""
    global _started
    if os.environ.get('MBULL_DISABLE_KLINE_SCHEDULER') == '1':
        logger.info('K线调度：已被 MBULL_DISABLE_KLINE_SCHEDULER 关闭')
        return False
    with _start_lock:
        if _started:
            return False
        _started = True
    try:
        threading.Thread(target=_loop, name='kline-scheduler', daemon=True).start()
        logger.warning('K线调度：已启动（每 %d 分钟检查，%02d:%02d 后触发当日刷新）',
                       _CHECK_INTERVAL_SEC // 60, _TRIGGER_TIME[0], _TRIGGER_TIME[1])
        return True
    except Exception as e:
        logger.warning('K线调度：启动失败: %s', e, exc_info=True)
        return False


def status():
    """诊断快照。"""
    due, why = is_due()
    st = _load_state()
    return {
        'started': _started, 'running': _running,
        'due': due, 'due_reason': why,
        'trigger_time': '%02d:%02d' % _TRIGGER_TIME,
        'check_interval_sec': _CHECK_INTERVAL_SEC,
        'last_refresh_date': st.get('last_refresh_date'),
        'last_run_at': st.get('last_run_at'),
        'last_stats': st.get('last_stats'),
        'state_path': _state_path(),
    }
