# -*- coding: utf-8 -*-
"""通达信行情节点池：种子 + 探测持久化 + 漂移自愈。

问题
----
K 线可用节点会随时间漂移。实测（2026-10-05）：``117.34.114.13`` 可取 K 线，
却不在 ``tdx.py`` 硬编码的 7 台里 —— 那份列表是 2026-09-19 的快照。节点一变，
用户侧只能等维护者改代码、重新发版，这正是「降低维护者改动成本」要消除的。

做法
----
1. **种子** ``SEED_SERVERS``：首启 / 从未探测时的兜底，行为与旧版一致。
2. **探测**：并发扫 pytdx 内置池（``pytdx.config.hosts.hq_hosts``，104 台），
   只保留「能取到 K 线」的节点 —— TCP 可连 ≠ 支持 0x10c K 线命令（实测
   104 台里 TCP 可连 33 台、真正能取 K 线仅 8 台）。
3. **持久化**：结果落 ``CONFIG_DIR/tdx_servers.json``，下次启动直接读，零探测。
4. **触发**：① 持久化过期（> ``STALE_SEC``）→ 后台重扫；② tdx 全节点失败时由
   ``note_failure()`` 上报 → 后台重扫（``RETRY_THROTTLE_SEC`` 节流）。两者都在
   daemon 线程里跑，绝不阻塞调用方。

⚠️ **本模块不解决「整个 /24 段被封」**：实测内置池里能取 K 线的 8 台全在
``117.34.114.x``，没有跨网段替代。那种场景要靠腾讯回退通道，是另一条线。
"""
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

logger = logging.getLogger(__name__)

# 2026-09-19 全池扫描（104 台）唯一能出 K 线的集群，全部属于国泰君安。
# ⚠️ 七个节点**在同一 /24 段**：该段被封则整源失效 —— 这是必须保留腾讯回退的原因。
# 本列表仅作**首启兜底**；运行时会由探测结果替换（见模块 docstring）。
SEED_SERVERS = (
    ("117.34.114.15", 7709),
    ("117.34.114.14", 7709),
    ("117.34.114.16", 7709),
    ("117.34.114.17", 7709),
    ("117.34.114.18", 7709),
    ("117.34.114.20", 7709),
    ("117.34.114.27", 7709),
)

_POOL_FILENAME = 'tdx_servers.json'
STALE_SEC = 7 * 86400            # 持久化超过 7 天视为过期 -> 后台重扫
RETRY_THROTTLE_SEC = 600         # 两次重扫的最小间隔（防失败风暴）
_PROBE_TIMEOUT = 3.0             # 单台连接/取数超时
_PROBE_WORKERS = 16              # 并发探测数（对齐 audit/_probe_pytdx_scan104.py）

# 判据：日K（category=9）能取到非空 bar 才算「支持 K 线」。沪、深各试一只。
_KLINE_TARGETS = ((9, 1, "600023", 0, 3), (9, 0, "000001", 0, 3))

_lock = threading.RLock()
_servers = None                  # list[(ip, port)]；None = 尚未加载
_pool_source = None              # 'seed' | 'persisted'
_updated_at = None               # 最近一次**成功**探测的 ISO 时间
_last_attempt_ts = 0.0           # 最近一次探测**尝试**的时间戳（含失败）
_nodes_detail = []               # 最近一次探测明细 [{'ip','port','ms'}]
_probing = False


def _pool_path():
    from engine.config import CONFIG_DIR
    return os.path.join(CONFIG_DIR, _POOL_FILENAME)


def _import_api_cls():
    """延迟 import pytdx（未安装时返回 None，由调用方回退腾讯源）。"""
    try:
        from pytdx.hq import TdxHq_API
        return TdxHq_API
    except Exception as e:
        logger.warning('通达信节点重扫：pytdx 不可用: %s', e)
        return None


def _load_builtin_hosts():
    """pytdx 内置服务器池 [(name, ip, port)]；不可用时返回 []。"""
    try:
        from pytdx.config.hosts import hq_hosts
        return [(h[0], h[1], int(h[2])) for h in hq_hosts
                if h and len(h) >= 3]
    except Exception as e:
        logger.warning('通达信节点重扫：内置服务器池不可用: %s', e)
        return []


def _probe_one(cls, host):
    """单台探测：能取到日K bar 返回 (ip, port, ms)，否则 None。"""
    _name, ip, port = host
    t0 = time.time()
    api = None
    try:
        api = cls(heartbeat=False, auto_retry=False, raise_exception=False)
        if not api.connect(ip, port, time_out=_PROBE_TIMEOUT):
            return None
        for cat, mkt, code, start, cnt in _KLINE_TARGETS:
            try:
                bars = api.get_security_bars(cat, mkt, code, start, cnt)
            except Exception:
                bars = None
            if bars:
                return (ip, int(port), round((time.time() - t0) * 1000, 1))
        return None
    except Exception:
        return None
    finally:
        if api is not None:
            try:
                api.disconnect()
            except Exception:
                pass


def _probe_hosts(hosts):
    """并发探测，返回可用 [(ip, port, ms)]，按延迟升序。"""
    cls = _import_api_cls()
    if cls is None:
        return []
    found = []
    with ThreadPoolExecutor(max_workers=_PROBE_WORKERS) as ex:
        futs = [ex.submit(_probe_one, cls, h) for h in hosts]
        for fu in as_completed(futs):
            try:
                r = fu.result()
            except Exception:
                r = None
            if r:
                found.append(r)
    found.sort(key=lambda x: x[2])
    return found


def _load_persisted():
    """读持久化节点池，返回 (nodes, updated_at)；缺失/损坏返回 ([], None)。"""
    try:
        with open(_pool_path(), 'r', encoding='utf-8') as f:
            d = json.load(f)
    except Exception:
        return [], None
    nodes = []
    for n in (d.get('nodes') or []):
        ip = str(n.get('ip') or '').strip()
        if not ip:
            continue
        try:
            port = int(n.get('port') or 7709)
        except (TypeError, ValueError):
            port = 7709
        nodes.append({'ip': ip, 'port': port, 'ms': n.get('ms')})
    return nodes, d.get('updated_at')


def _save_persisted(nodes, updated_at):
    """原子落盘；任何异常只记日志，不影响取数。"""
    path = _pool_path()
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        payload = {'version': 1, 'updated_at': updated_at, 'nodes': nodes}
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning('通达信节点池落盘失败（忽略）: %s', e)


def _is_stale():
    if not _updated_at:
        return True
    try:
        ts = datetime.fromisoformat(_updated_at)
    except Exception:
        return True
    return (datetime.now() - ts).total_seconds() > STALE_SEC


def _ensure_loaded():
    """惰性加载节点池（持久化优先，否则种子）。**不触发重扫**。返回 (servers, stale)。"""
    global _servers, _pool_source, _updated_at
    with _lock:
        if _servers is None:
            nodes, updated = _load_persisted()
            if nodes:
                _servers = [(n['ip'], n['port']) for n in nodes]
                _pool_source = 'persisted'
                _updated_at = updated
                _nodes_detail.extend(nodes)
            else:
                _servers = list(SEED_SERVERS)
                _pool_source = 'seed'
        return list(_servers), _is_stale()


def get_servers():
    """当前节点池 [(ip, port)]。**永不返回空** —— 无持久化时回落 SEED_SERVERS。

    首次调用加载持久化；若已过期则顺手触发一次后台重扫（不阻塞本次调用）。
    """
    out, stale = _ensure_loaded()
    if stale:
        maybe_refresh_async('stale')
    return out


def refresh(reason='manual'):
    """同步重扫一轮。返回统计 dict；已有探测在跑时返回 None。

    探测为空（网络抖动/全池不可达）时**保留现有池**，不因一次失败把可用节点清空。
    """
    global _servers, _pool_source, _updated_at, _nodes_detail, _last_attempt_ts, _probing
    with _lock:
        if _probing:
            return None
        _probing = True
        _last_attempt_ts = time.time()
    try:
        hosts = _load_builtin_hosts()
        if not hosts:
            return None
        found = _probe_hosts(hosts)
        with _lock:
            _nodes_detail = [{'ip': ip, 'port': p, 'ms': ms} for ip, p, ms in found]
            if found:
                _servers = [(ip, p) for ip, p, ms in found]
                _pool_source = 'persisted'
                _updated_at = datetime.now().isoformat(timespec='seconds')
                _save_persisted(_nodes_detail, _updated_at)
            logger.warning('通达信节点重扫(%s)：可用 %d/%d，当前池 %d 台',
                           reason, len(found), len(hosts), len(_servers or ()))
            return {'found': len(found), 'total': len(hosts),
                    'pool': len(_servers or ())}
    finally:
        with _lock:
            _probing = False


def maybe_refresh_async(reason='auto'):
    """按节流条件触发后台重扫。返回是否真的起了线程。"""
    global _last_attempt_ts
    now = time.time()
    with _lock:
        if _probing:
            return False
        if now - _last_attempt_ts < RETRY_THROTTLE_SEC:
            return False
        _last_attempt_ts = now
    threading.Thread(target=refresh, args=(reason,),
                     daemon=True, name='tdx-node-probe').start()
    return True


def note_failure():
    """tdx 全节点连接失败时由调用方上报：触发后台重扫（节流）。"""
    return maybe_refresh_async('all_failed')


def status():
    """诊断快照（供 stats() / 数据源设置页展示）。

    会惰性加载节点池（持久化优先），确保首次调用即报出真实池大小，
    但**不触发重扫**（避免只读诊断引发网络探测）。
    """
    servers, _ = _ensure_loaded()
    with _lock:
        return {
            'pool_size': len(servers),
            'pool_source': _pool_source or 'seed',
            'updated_at': _updated_at,
            'probing': _probing,
            'nodes': list(_nodes_detail),
        }


def _reset_state():
    """清空内存态（测试用；正式流程不需要）。"""
    global _servers, _pool_source, _updated_at, _last_attempt_ts, _nodes_detail, _probing
    with _lock:
        _servers = None
        _pool_source = None
        _updated_at = None
        _last_attempt_ts = 0.0
        _nodes_detail = []
        _probing = False