#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""出网代理层（单一出口）。

为什么需要
----------
腾讯 / 新浪的免费行情接口对「单 IP 高频」敏感：命中 WAF 后当前实现是把**整个源**
冷却 30 秒（`HTTPClient._mark_cooldown`），冷却期内该源的请求全部快速失败 —— 冷扫
5200 只时等于整轮停摆。有了代理池，正确的降级顺序变成：

    直连失败/被 WAF → 换一个出口 IP 重试 → 所有 IP 都失败 → 才冷却整源

设计
----
- 只做「取哪个代理 / 记账 / 拉黑」，不发请求；调用方把返回值塞进 `requests` 的
  `proxies=` 参数即可（HTTP 与 HTTPS 各一个键）。
- 配置：`<CONFIG_DIR>/proxy.json`（缺失即视为未启用，零行为变化）。
- 也支持环境变量 `MBULL_PROXY`（逗号分隔）—— 便于临时启用而不改文件。
- 线程安全：冷扫是多线程分片共享的，所有状态读写都过 ``_lock``。

配置示例（config/proxy.json）
----------------------------
{
  "enabled": true,
  "mode": "on_failure",            // always=全走代理 | on_failure=直连优先，失败才走
  "strategy": "round_robin",       // round_robin | random
  "proxies": [
    "http://127.0.0.1:7890",
    "http://user:pass@1.2.3.4:8080",
    "socks5://1.2.3.4:1080"
  ],
  "max_fails": 3,                  // 单代理连续失败多少次后拉黑
  "blacklist_sec": 300,            // 拉黑时长（秒）
  "sync_env": false                // true：单代理时同时写 HTTP_PROXY/HTTPS_PROXY 环境变量，
                                   // 让不走本模块的边角请求也走代理（会波及子进程，默认关）
}

⚠️ socks5:// 需要 PySocks（`pip install requests[socks]`），否则 requests 会直接报
   `Missing dependencies for SOCKS support`。本模块会在加载时主动提示。
"""

import json
import logging
import os
import random
import threading
import time

logger = logging.getLogger(__name__)

_lock = threading.RLock()

_config_cache = None
_config_mtime = 0.0
_config_path_cached = None

# 单代理连续失败计数 / 拉黑到期时间：{proxy_url: [fails, blacklisted_until]}
_fails = {}
# 轮换游标
_rr_index = 0
# 统计：{source: {'ok': n, 'fail': n}}
_stats = {}
# 最近一次选中的代理（仅供 status() 展示）
_last_pick = None

_DEFAULT_CONF = {
    'enabled': False,
    'mode': 'on_failure',
    'strategy': 'round_robin',
    'proxies': [],
    'max_fails': 3,
    'blacklist_sec': 300,
    'sync_env': False,
}

_SOCKS_WARNED = False


def _config_path():
    """proxy.json 路径（延迟 import engine.config，避免循环依赖与导入期副作用）。"""
    global _config_path_cached
    if _config_path_cached:
        return _config_path_cached
    try:
        from engine.config import CONFIG_DIR
        _config_path_cached = os.path.join(CONFIG_DIR, 'proxy.json')
    except Exception:  # pragma: no cover - 极端环境兜底
        _config_path_cached = os.path.join(os.getcwd(), 'config', 'proxy.json')
    return _config_path_cached


def _read_raw():
    path = _config_path()
    try:
        if not os.path.isfile(path):
            return {}
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning('proxy.json 解析失败（按未启用处理）: %s', e)
        return {}


def _env_proxies():
    raw = os.environ.get('MBULL_PROXY') or ''
    return [p.strip() for p in raw.split(',') if p.strip()]


def load_config(force_reload=False):
    """读取配置（带 mtime 热重载：改完 json 无需重启）。"""
    global _config_cache, _config_mtime
    with _lock:
        path = _config_path()
        try:
            mtime = os.path.getmtime(path) if os.path.isfile(path) else 0.0
        except OSError:
            mtime = 0.0
        if (not force_reload) and _config_cache is not None and mtime == _config_mtime:
            return _config_cache

        conf = dict(_DEFAULT_CONF)
        conf.update(_read_raw())
        # 环境变量优先级最高：便于"临时加个代理试一下"
        env_px = _env_proxies()
        if env_px:
            conf['proxies'] = list(env_px)
            conf['enabled'] = True
        conf['proxies'] = [str(p).strip() for p in (conf.get('proxies') or []) if str(p).strip()]
        conf['mode'] = str(conf.get('mode') or 'on_failure').lower()
        if conf['mode'] not in ('always', 'on_failure'):
            conf['mode'] = 'on_failure'
        conf['strategy'] = str(conf.get('strategy') or 'round_robin').lower()
        if conf['strategy'] not in ('round_robin', 'random'):
            conf['strategy'] = 'round_robin'
        # max_fails 必须 ≥1：0/负数/非数值都属"写错了"，一律回退默认，不要静默变成
        # 「首次失败即拉黑」这种激进策略。blacklist_sec 的 0 有明确语义（不拉黑），故只钳负值。
        try:
            _mf = int(conf.get('max_fails', 3))
        except (TypeError, ValueError):
            _mf = 3
        conf['max_fails'] = _mf if _mf >= 1 else 3
        try:
            _bl = int(conf.get('blacklist_sec', 300))
        except (TypeError, ValueError):
            _bl = 300
        conf['blacklist_sec'] = max(0, _bl)

        _warn_socks_once(conf['proxies'])
        _config_cache = conf
        _config_mtime = mtime
        return conf


def _warn_socks_once(proxies):
    """socks5 缺 PySocks 时提前告警：否则只在请求时抛一句难懂的依赖错误。"""
    global _SOCKS_WARNED
    if _SOCKS_WARNED:
        return
    if not any(str(p).lower().startswith('socks') for p in proxies or []):
        return
    try:
        import socks  # noqa: F401  (PySocks)
    except Exception:
        _SOCKS_WARNED = True
        logger.warning('代理列表含 socks5:// 但缺少 PySocks，SOCKS 代理将不可用：'
                       '请 pip install "requests[socks]"')


def enabled():
    conf = load_config()
    return bool(conf.get('enabled')) and bool(conf.get('proxies'))


def _alive_proxies(now=None):
    now = now or time.time()
    conf = load_config()
    out = []
    for p in conf.get('proxies') or []:
        bl_until = (_fails.get(p) or [0, 0])[1]
        if bl_until and bl_until > now:
            continue
        out.append(p)
    return out


def has_alive():
    """是否还有可用（未被拉黑）的代理。决定 WAF 后「换 IP 继续」还是「冷却整源」。"""
    if not enabled():
        return False
    with _lock:
        return bool(_alive_proxies())


def acquire(source=None):
    """取一个可用代理 URL；无可返回 None（调用方直连）。

    mode=on_failure：**首次调用返回 None**（先试直连），失败后由 `report()` 记账，
    下一次 acquire 才会给出代理 —— 避免为了一次偶发抖动就白白绕代理。
    """
    global _rr_index
    conf = load_config()
    if not conf.get('enabled'):
        return None
    with _lock:
        pool = _alive_proxies()
        if not pool:
            return None
        if conf['mode'] != 'always':
            # on_failure：该源尚未出现过失败 → 仍走直连
            st = _stats.get(source or '') or {}
            if st.get('fail', 0) <= 0:
                return None
        if conf['strategy'] == 'random':
            return random.choice(pool)
        _rr_index = (_rr_index + 1) % len(pool)
        return pool[_rr_index]


def to_requests(proxy_url):
    """代理 URL → requests 的 proxies dict；None → None（表示直连）。"""
    if not proxy_url:
        return None
    return {'http': proxy_url, 'https': proxy_url}


def proxies_for(source=None):
    """一步到位：直接拿 requests 的 proxies 参数（供不改造成 HTTPClient 的边角请求用）。"""
    return to_requests(acquire(source))


def report(source, proxy_url, ok):
    """记账。连续失败达阈值 → 拉黑该代理 blacklist_sec 秒（期间不再被选中）。"""
    with _lock:
        st = _stats.setdefault(source or '', {'ok': 0, 'fail': 0})
        st['ok' if ok else 'fail'] += 1
        if ok:
            if proxy_url:
                _fails[proxy_url] = [0, 0]     # 成功即清零
            return
        if not proxy_url:
            return                              # 直连失败：不涉及代理记账
        rec = _fails.setdefault(proxy_url, [0, 0])
        rec[0] += 1
        conf = load_config()
        if rec[0] >= conf['max_fails']:
            rec[1] = time.time() + conf['blacklist_sec']
            rec[0] = 0
            logger.warning('代理连续失败 %d 次，拉黑 %d 秒: %s',
                           conf['max_fails'], conf['blacklist_sec'], _mask(proxy_url))


def mark_success(source, proxy_url=None):
    report(source, proxy_url, True)


def reset():
    """清空记账与拉黑（配置热重载 / 用户点「重置」时调用）。"""
    global _rr_index, _config_cache, _config_mtime
    with _lock:
        _fails.clear()
        _stats.clear()
        _rr_index = 0
        _config_cache = None
        _config_mtime = 0.0
        _sync_env(None)


def _mask(url):
    """打日志时隐去代理里的账号密码。"""
    try:
        if '@' in url:
            scheme, rest = url.split('://', 1)
            return f'{scheme}://***@{rest.split("@", 1)[1]}'
    except Exception:
        pass
    return url


def sync_env_if_needed():
    """sync_env=true 且只有 1 个代理时，写 HTTP_PROXY/HTTPS_PROXY。

    这样不走本模块的边角请求（以及子进程）也会走代理。多代理需要轮换，写环境变量
    没有意义（环境变量只能表达一个代理），故不做。
    """
    conf = load_config()
    if conf.get('enabled') and conf.get('sync_env') and len(conf.get('proxies') or []) == 1:
        _sync_env(conf['proxies'][0])
    else:
        _sync_env(None)


def _sync_env(proxy_url):
    for key in ('HTTP_PROXY', 'HTTPS_PROXY'):
        if proxy_url:
            os.environ[key] = proxy_url
        else:
            os.environ.pop(key, None)


def status():
    """诊断快照（供设置页 / 健康检查展示）。"""
    conf = load_config()
    now = time.time()
    with _lock:
        pool = []
        for p in conf.get('proxies') or []:
            rec = _fails.get(p) or [0, 0]
            pool.append({
                'proxy': _mask(p),
                'fails': rec[0],
                'blacklisted_sec': max(0, int(rec[1] - now)) if rec[1] > now else 0,
            })
        return {
            'enabled': bool(conf.get('enabled')),
            'mode': conf['mode'],
            'strategy': conf['strategy'],
            'configured': len(conf.get('proxies') or []),
            'alive': len(_alive_proxies(now)),
            'current': _mask(_last_pick) if _last_pick else None,
            'config_path': _config_path(),
            'pool': pool,
            'stats': {k: dict(v) for k, v in _stats.items()},
        }


def acquire_with_meta(source=None):
    """acquire 的带元数据版本：返回 (proxies_dict, proxy_url)，便于调用方回传 report。"""
    global _last_pick
    url = acquire(source)
    if url:
        _last_pick = url
    return to_requests(url), url
