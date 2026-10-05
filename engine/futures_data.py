# -*- coding: utf-8 -*-
"""期货数据层 — 东方财富 K线 API + 新浪实时行情。

与 data_layer.py 的区别：
  - 数据源：东方财富 push2his（data_layer 用腾讯 web.ifzq）；
    期货在腾讯免费 API 无对应接口，东方财富 secid 支持 113/114/115/8/42 期货前缀。
  - 无前复权：期货无除权除息，fqt=0 原始价格直出。
  - 缓存路径：cache/kline/futures_{symbol}.pkl（与股票缓存隔离）。
  - DataFrame 格式与 data_layer 完全一致：
      trade_time(open) / open / close / high / low / volume
    → scoring_core.compute_stock_score / build_tech 可直接消费。

Eastmoney K 线 API（期货）：
  GET https://push2his.eastmoney.com/api/qt/stock/kline/get
    ?secid=113.rb0          # 交易所前缀.主力代码
    &fields1=f1,f2,f3,f4,f5,f6
    &fields2=f51,f52,f53,f54,f55,f56,f57   # 日期,开,收,高,低,量,额
    &klt=101                # 101=日K
    &fqt=0                  # 不复权
    &beg=20230101
    &end=20241231

  返回 JSON: {"data":{"klines":["2023-01-03,4100,4150,4180,4080,1234567,5e9", ...]}}
"""

import os
import re
import time
import pickle
import logging
import random
import threading
import requests
import pandas as pd
from datetime import datetime, timedelta

from engine.config import get_app_dir, CONFIG_DIR
from engine.exceptions import DataSourceError
from engine import net_proxy

logger = logging.getLogger(__name__)

# ── 期货 K线数据源配置（与 A股 数据源解耦，data_sources.json → futures_kline_source）──
# 独立于 A股 K线源（kline_source）。可选值：'sina'（HTTP，存在反爬/被屏蔽风险）|
# 'eastmoney'（HTTPS，更稳定，**默认**）。缺失/损坏/未知值回退 eastmoney。
_FUTURES_KLINE_SOURCE_CACHE: str | None = None


def _get_futures_kline_source() -> str:
    """读取期货 K线数据源配置（data_sources.json → futures_kline_source）。

    Returns:
        'sina' 或 'eastmoney'，默认 'eastmoney'。
    """
    global _FUTURES_KLINE_SOURCE_CACHE
    if _FUTURES_KLINE_SOURCE_CACHE is not None:
        return _FUTURES_KLINE_SOURCE_CACHE
    src = 'eastmoney'  # 默认 HTTPS
    try:
        import json as _json
        path = os.path.join(CONFIG_DIR, 'data_sources.json')
        with open(path, 'r', encoding='utf-8') as f:
            d = _json.load(f)
        v = d.get('futures_kline_source')
        if v in ('sina', 'eastmoney'):
            src = v
    except Exception as e:
        logger.debug(f"读取期货数据源配置失败，用默认(eastmoney): {e}")
    _FUTURES_KLINE_SOURCE_CACHE = src
    return src


def reset_futures_kline_source():
    """清空期货数据源缓存（web_api 保存配置后热重载调用，无需重启）。"""
    global _FUTURES_KLINE_SOURCE_CACHE
    _FUTURES_KLINE_SOURCE_CACHE = None

# ── 期货专用缓存目录 ──
_FUTURES_CACHE_DIR = os.path.join(get_app_dir(), 'cache', 'kline')
os.makedirs(_FUTURES_CACHE_DIR, exist_ok=True)

# ── 内存缓存 ──
_mem_cache: dict[str, tuple[pd.DataFrame, float]] = {}
_mem_lock = threading.RLock()
_MEM_TTL = 300  # 5 分钟

# ── Eastmoney API 常量 ──
_EM_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
_EM_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Referer': 'https://quote.eastmoney.com/',
}

# ── 速率限制（独立于股票数据源，互不阻塞）──
_last_request_time = 0.0
_rate_lock = threading.Lock()
# 基础请求间隔：0.3s → 1.0s，降低同一域名被频控的概率
_MIN_INTERVAL = 0.3

# 失败冷却：某源连续失败达阈值后临时冷却，期间优先切备用源，避免在已风控/反爬的源上硬撞
_FAIL_THRESHOLD = 3    # 连续失败次数
_FAIL_COOLDOWN = 60.0  # 冷却时长（秒）
_source_state = {
    'sina':      {'fails': 0, 'until': 0.0},
    'eastmoney': {'fails': 0, 'until': 0.0},
    'tdx':       {'fails': 0, 'until': 0.0},
}


def _source_blocked(src: str) -> bool:
    """该源是否处于冷却期（连续失败触发）。"""
    return time.time() < _source_state[src]['until']


def _mark_fail(src: str) -> None:
    """记录一次失败；累积达阈值则启动冷却。"""
    st = _source_state[src]
    st['fails'] += 1
    if st['fails'] >= _FAIL_THRESHOLD:
        st['until'] = time.time() + _FAIL_COOLDOWN
        logger.warning(f"期货数据源 {src} 连续失败 {st['fails']} 次，冷却 {_FAIL_COOLDOWN:.0f}s")
    else:
        logger.debug(f"期货数据源 {src} 失败计数 → {st['fails']}")


def _mark_ok(src: str) -> None:
    """一次成功，重置该源失败计数。"""
    st = _source_state[src]
    if st['fails']:
        st['fails'] = 0
        logger.debug(f"期货数据源 {src} 恢复正常，重置失败计数")


def _is_continuous_contract(main_code: str) -> bool:
    """判断是否为主力连续合约（如 rb0/TA0/cu0 — 无具体月份后缀）。

    东财 push2his 不支持主力连续合约，传入 rb0 会直接返回 data:None。
    只有具体月份合约（rb2510/TA2510）两边都支持。
    """
    if not main_code or not isinstance(main_code, str):
        return False
    # 连续合约 = 品种代码 + '0'（纯字母+0，无数字后缀）
    # 具体合约 = 品种代码 + 4位数字（如 rb2510）
    m = re.match(r'^([a-zA-Z]+)(\d+)$', main_code)
    if not m:
        return False
    suffix = m.group(2)
    # 月份为 '0' 时为连续合约（如 rb0）
    return suffix == '0'


def _ordered_sources(main_code: str | None = None, days: int = 300) -> tuple:
    """返回有序源列表（tdx 优先，其后按用户首选源）。

    - **tdx 恒排最前**（TCP 7721，无 WAF 配额；主力连续合约 L8 只有 tdx/新浪支持——
      东财 push2his 对 rb0 直接返回 data:None）。days 超出 tdx 主连深度上限（700 根）
      时由调用方跳过 tdx，避免白白撞一次深度不足。
    - 主力连续合约（rb0/TA0 等）：东财不支持，序列为 [tdx, sina, eastmoney]。
    - 具体月份合约：[tdx, 首选源, 备用源]，冷却中的源后移。
    """
    if main_code and _is_continuous_contract(main_code):
        return ('tdx', 'sina', 'eastmoney')

    pref = _get_futures_kline_source()
    other = 'sina' if pref == 'eastmoney' else 'eastmoney'
    seq = ['tdx']
    for s in (pref, other):
        if s not in seq:
            seq.append(s)
    kept = [s for s in seq if not _source_blocked(s)]
    return tuple(kept) if kept else ('sina', 'eastmoney')


def _rate_limit():
    """按源速率限制 + 随机抖动（打散请求节奏，降低规律性触发风控）。"""
    global _last_request_time
    with _rate_lock:
        now = time.time()
        wait = (_last_request_time + _MIN_INTERVAL) + random.uniform(0.0, 0.4) - now
        if wait < 0.2:
            wait = 0.2
        if wait > 0:
            time.sleep(wait)
        _last_request_time = time.time()


def _get_cache_path(code: str) -> str:
    """期货 K 线 pickle 缓存路径（按合约代码隔离，主力连续 rb0 与具体合约 rb2510 分开）。"""
    return os.path.join(_FUTURES_CACHE_DIR, f'futures_{code}.pkl')


def _load_disk_cache(code: str) -> pd.DataFrame | None:
    """从磁盘加载缓存。"""
    path = _get_cache_path(code)
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'rb') as f:
            df = pickle.load(f)
        if df is not None and len(df) > 0:
            logger.debug(f"期货 {code} 磁盘缓存命中: {len(df)} 根K线")
            return df
    except Exception as e:
        logger.warning(f"期货 {code} 磁盘缓存加载失败: {e}")
    return None


def _save_disk_cache(code: str, df: pd.DataFrame):
    """保存到磁盘缓存。"""
    path = _get_cache_path(code)
    try:
        with open(path, 'wb') as f:
            pickle.dump(df, f)
        logger.debug(f"期货 {code} 磁盘缓存已保存: {len(df)} 根K线")
    except Exception as e:
        logger.warning(f"期货 {code} 磁盘缓存保存失败: {e}")


def _load_mem_cache(code: str) -> pd.DataFrame | None:
    """从内存缓存加载。"""
    with _mem_lock:
        entry = _mem_cache.get(code)
        if entry is None:
            return None
        df, ts = entry
        if time.time() - ts < _MEM_TTL:
            return df
        del _mem_cache[code]
    return None


def _save_mem_cache(code: str, df: pd.DataFrame):
    """保存到内存缓存。"""
    with _mem_lock:
        _mem_cache[code] = (df, time.time())


# 分钟K内存缓存 TTL（秒）— 分钟数据不落盘，短 TTL 即可
_MINUTE_MEM_TTL = 60


def fetch_futures_minute(symbol: str, secid: str, period: int = 15,
                         days: int = 120) -> pd.DataFrame | None:
    """获取期货分钟K线（新浪 getFewMinLine JSONP）。

    新浪期货分钟K API：
      http://stock2.finance.sina.com.cn/futures/api/jsonp.php/var _{code}=
        /InnerFuturesNewService.getFewMinLine?symbol={code}&type={period}

    type: 1=1分钟 5=5分钟 15=15分钟 30=30分钟 60=60分钟
    返回字段与日K一致：d=日期时间 o=开 h=高 l=低 c=收 v=成交量 p=成交额

    Args:
        symbol: 品种代码（如 'rb'），仅用于日志
        secid: Eastmoney secid（如 '113.rb0' 或 '113.rb2510'）
        period: 分钟数（1/5/15/30/60），非法值回落 15
        days: K线根数上限（新浪最多返回 1023 根）

    Returns:
        pd.DataFrame: trade_time/open/close/high/low/volume 或 None
    """
    import re
    import json as _json

    if period not in (1, 5, 15, 30, 60):
        period = 15
    main_code = secid.split('.')[-1] if '.' in secid else secid
    cache_key = f'{main_code}_{period}min'

    # 内存缓存（短 TTL，分钟数据不落盘）
    with _mem_lock:
        entry = _mem_cache.get(cache_key)
        if entry is not None:
            df, ts = entry
            if time.time() - ts < _MINUTE_MEM_TTL:
                return df
            del _mem_cache[cache_key]

    df = None
    # 按有序源请求（首选优先，冷却中的源后移；连续合约强制新浪优先）
    for src in _ordered_sources(main_code):
        if src == 'eastmoney':
            df = _fetch_eastmoney_kline(symbol, secid, days, klt=period)
        else:  # 'sina'
            df = _fetch_sina_minute(symbol, main_code, period, days)
        if df is not None:
            _mark_ok(src)
            break
        _mark_fail(src)
        logger.info(f"期货 {main_code} {src}分钟K失败，尝试下一数据源")
    if df is None:
        return None

    # 写内存缓存
    with _mem_lock:
        _mem_cache[cache_key] = (df, time.time())

    logger.info(f"期货 {symbol}({main_code}) {period}分钟K: 获取 {len(df)} 根 "
                f"({df['trade_time'].iloc[0]} ~ {df['trade_time'].iloc[-1]})")
    return df


def _fetch_sina_minute(symbol: str, main_code: str, period: int = 15,
                       days: int = 120) -> pd.DataFrame | None:
    """从新浪获取期货分钟K线（getFewMinLine JSONP，主源）。

    新浪期货分钟K API：
      http://stock2.finance.sina.com.cn/futures/api/jsonp.php/var _{code}=
        /InnerFuturesNewService.getFewMinLine?symbol={code}&type={period}

    type: 1=1分钟 5=5分钟 15=15分钟 30=30分钟 60=60分钟
    返回字段与日K一致：d=日期时间 o=开 h=高 l=低 c=收 v=成交量 p=成交额
    """
    import re
    import json as _json

    url = (f'http://stock2.finance.sina.com.cn/futures/api/jsonp.php'
           f'/var%20_{main_code}=/InnerFuturesNewService.getFewMinLine'
           f'?symbol={main_code}&type={period}')
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://finance.sina.com.cn/',
    }

    try:
        _rate_limit()
        resp = requests.get(url, headers=headers, timeout=15,
                            proxies=net_proxy.proxies_for('sina_futures_kline'))
        if resp.status_code != 200:
            logger.warning(f"新浪期货{period}分钟K HTTP {resp.status_code}: {symbol}")
            return None

        text = resp.text
        m = re.search(r'=\(\[(.+)\]\)', text, re.DOTALL)
        if not m:
            logger.warning(f"新浪期货{period}分钟K解析失败(无JSON数组): {symbol}")
            return None

        klines = _json.loads('[' + m.group(1) + ']')
        if not klines:
            logger.warning(f"新浪期货{period}分钟K返回空数组: {symbol}")
            return None

        rows = []
        for k in klines:
            try:
                rows.append({
                    'trade_time': pd.Timestamp(k['d']),
                    'open':   float(k['o']),
                    'high':   float(k['h']),
                    'low':    float(k['l']),
                    'close':  float(k['c']),
                    'volume': float(k['v']),
                })
            except (KeyError, ValueError, TypeError):
                continue

        if not rows:
            logger.warning(f"新浪期货{period}分钟K解析后为空: {symbol}")
            return None

        df = pd.DataFrame(rows)
        df = df.sort_values('trade_time').reset_index(drop=True)
        if len(df) > days:
            df = df.iloc[-days:].reset_index(drop=True)
        return df

    except requests.exceptions.Timeout:
        logger.warning(f"新浪期货{period}分钟K请求超时: {symbol}")
        return None
    except requests.exceptions.ConnectionError:
        logger.warning(f"新浪期货{period}分钟K连接失败: {symbol}")
        return None
    except Exception as e:
        logger.warning(f"新浪期货{period}分钟K获取异常: {symbol}: {e}")
        return None


def _fetch_sina_kline(symbol: str, main_code: str, days: int = 300) -> pd.DataFrame | None:
    """从新浪获取期货日K线（JSONP 接口）。

    新浪期货日K API：
      http://stock2.finance.sina.com.cn/futures/api/jsonp.php/var _{code}=
        /InnerFuturesNewService.getDailyKLine?symbol={code}

    返回 JSONP：
      var _rb0=([{"d":"2009-03-27","o":"3550","h":"3663","l":"3513",
                  "c":"3561","v":"354590","p":"45548","s":"0.000"}, ...])

    字段：d=日期 o=开 h=高 l=低 c=收 v=成交量 p=成交额 s=结算价

    Args:
        symbol: 品种代码（如 'rb'），仅用于日志
        main_code: 主力合约代码（如 'rb0', 'TA0', 'IF0'）
        days: K线根数上限（取最近 N 根）

    Returns:
        pd.DataFrame: trade_time/open/close/high/low/volume 或 None
    """
    import re
    import json as _json

    url = (f"http://stock2.finance.sina.com.cn/futures/api/jsonp.php"
           f"/var%20_{main_code}=/InnerFuturesNewService.getDailyKLine"
           f"?symbol={main_code}")
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://finance.sina.com.cn/',
    }

    try:
        _rate_limit()
        resp = requests.get(url, headers=headers, timeout=20,
                            proxies=net_proxy.proxies_for('sina_futures_kline'))

        if resp.status_code != 200:
            logger.warning(f"新浪期货K线 HTTP {resp.status_code}: {symbol}")
            return None

        text = resp.text
        # 提取 JSON 数组：var _rb0=([...])
        m = re.search(r'=\(\[(.+)\]\)', text, re.DOTALL)
        if not m:
            logger.warning(f"新浪期货K线解析失败(无JSON数组): {symbol}")
            return None

        klines = _json.loads('[' + m.group(1) + ']')
        if not klines:
            logger.warning(f"新浪期货K线返回空数组: {symbol}")
            return None

        rows = []
        for k in klines:
            try:
                rows.append({
                    'trade_time': pd.Timestamp(k['d']),
                    'open':   float(k['o']),
                    'high':   float(k['h']),
                    'low':    float(k['l']),
                    'close':  float(k['c']),
                    'volume': float(k['v']),
                })
            except (KeyError, ValueError, TypeError):
                continue

        if not rows:
            logger.warning(f"新浪期货K线解析后为空: {symbol}")
            return None

        df = pd.DataFrame(rows)
        df = df.sort_values('trade_time').reset_index(drop=True)
        # 只取最近 days 根
        if len(df) > days:
            df = df.iloc[-days:].reset_index(drop=True)

        logger.info(f"期货K线 {symbol}({main_code}): 获取 {len(df)} 根日K "
                    f"({df['trade_time'].iloc[0].date()} ~ {df['trade_time'].iloc[-1].date()})")
        return df

    except requests.exceptions.Timeout:
        logger.warning(f"新浪期货K线请求超时: {symbol}")
        return None
    except requests.exceptions.ConnectionError:
        logger.warning(f"新浪期货K线连接失败: {symbol}")
        return None
    except Exception as e:
        logger.warning(f"新浪期货K线获取异常: {symbol}: {e}")
        return None


def _fetch_tdx_kline(symbol: str, main_code: str, days: int = 300):
    """通达信期货日K（ExHq TCP 7721，无 WAF 配额；主连 L8 是唯一不依赖新浪的主连源）。

    失败 / 深度不足返回 None 交由后续源（东财/新浪）兜底。
    volume 已按 trade×100 对齐东财口径（sina 的 v 是手，量纲与其相差 100 倍
    属既有差异——东财/tdx 两源一致）。
    """
    try:
        from engine.data_sources.tdx import fetch_futures_daily as _tdx_fetch
    except Exception as e:
        logger.warning(f"通达信期货模块不可用: {e}")
        return None
    df, err = _tdx_fetch(main_code, days)
    if df is None:
        logger.info(f"通达信期货K线未命中 {symbol}({main_code}): {err}")
        return None
    return df


# ── 东财 push2his K线兜底 ──
# 新浪期货 K 线仅 HTTP/80 且有反爬（响应体带 `location.href='//sina.com'` 跳转脚本），
# 在部分运行环境（防火墙/代理/被限频）下会整体失败。东财 push2his 走 HTTPS，
# 原生支持期货 secid 前缀（113=SHFE 114=DCE 115=CZCE 8=CFFEX 42=INE 142=GFEX），
# 作为主源失败时的兜底数据源，避免单源失败直接报「K线数据获取失败」。
_EM_KLINE_HIS_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"


def _parse_em_klines(klines: list[str], days: int) -> pd.DataFrame | None:
    """解析东财 klines 字符串列表。

    东财字段顺序（fields2=f51,f52,f53,f54,f55,f56,f57）：
        date, open, close, high, low, volume, amount
    注意 open/close 顺序与 Sina(o,h,l,c) 不同，必须按位映射。
    """
    rows = []
    for kl in klines:
        parts = kl.split(',')
        if len(parts) < 6:
            continue
        try:
            rows.append({
                'trade_time': pd.Timestamp(parts[0]),
                'open':   float(parts[1]),
                'close':  float(parts[2]),
                'high':   float(parts[3]),
                'low':    float(parts[4]),
                'volume': float(parts[5]),
            })
        except (ValueError, TypeError):
            continue
    if not rows:
        return None
    df = pd.DataFrame(rows).sort_values('trade_time').reset_index(drop=True)
    if len(df) > days:
        df = df.iloc[-days:].reset_index(drop=True)
    return df


def _to_eastmoney_czce_secid(secid: str) -> str:
    """东方财富对郑商所(CZCE, secid 前缀 115)合约采用「年份末位 1 位 + 月份 2 位」编码，
    例如 TA2611→TA611、MA2701→MA701、SR2609→SR609；而 SHFE(113)/DCE(114)/INE(142)/
    CFFEX(8) 等用标准 4 位年份(如 113.cu2610)。本函数仅对 115 前缀做转换，其余原样返回。

    背景：此前 CZCE 合约(PTA/甲醇/白糖等)在东财主源全部返回 NO DATA，根因就是传了
    4 位年份 secid(115.TA2611)，东财只认 1 位年份(115.TA611)。新浪对 CZCE 同样用 4 位
    年份，故新浪兜底路径不受影响(走 main_code 原值)。
    """
    if not isinstance(secid, str) or '.' not in secid:
        return secid
    prefix, code = secid.split('.', 1)
    if prefix != '115':
        return secid
    m = re.match(r'^([A-Za-z]+)(\d{2})(\d{2})$', code)
    if not m:
        return secid  # 连续合约(如 TA0)或非标准格式，原样返回交由新浪兜底
    product, yy, mm = m.group(1), m.group(2), m.group(3)
    em_code = f"{product}{yy[-1]}{mm}"  # 年份取末位(2026→'6')
    return f"{prefix}.{em_code}"


def _fetch_eastmoney_kline(symbol: str, secid: str, days: int = 300,
                           klt: int = 101) -> pd.DataFrame | None:
    """东财 push2his K线兜底（日K klt=101；分钟 klt=1/5/15/30/60）。

    Args:
        symbol: 品种代码（如 'rb'），仅用于日志
        secid: 东财 secid（如 '113.rb2610'，CZCE 内部会自动转 1 位年份）
        days: K线根数上限
        klt: K线周期（101=日K, 1/5/15/30/60=分钟）

    Returns:
        pd.DataFrame: trade_time/open/close/high/low/volume 或 None
    """
    # 郑商所(CZCE, 115)东财编码为「年份末位1位+月份2位」，此处统一转换
    secid = _to_eastmoney_czce_secid(secid)
    url = (f"{_EM_KLINE_HIS_URL}?secid={secid}"
           "&fields1=f1,f2,f3,f4,f5,f6"
           "&fields2=f51,f52,f53,f54,f55,f56,f57"
           f"&klt={klt}&fqt=0&beg=19900101&end=20991231")
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://quote.eastmoney.com/',
    }
    try:
        _rate_limit()
        resp = requests.get(url, headers=headers, timeout=20,
                            proxies=net_proxy.proxies_for('eastmoney_futures_kline'))
        if resp.status_code != 200:
            logger.warning(f"东财期货K线 HTTP {resp.status_code}: {symbol}({secid})")
            return None
        try:
            j = resp.json()
        except ValueError:
            logger.warning(f"东财期货K线 JSON 解析失败: {symbol}({secid})")
            return None
        data = j.get('data')
        if not data:
            logger.warning(f"东财期货K线无 data(可能未上市/已退市): {symbol}({secid})")
            return None
        klines = data.get('klines')
        if not klines:
            logger.warning(f"东财期货K线 klines 为空: {symbol}({secid})")
            return None
        df = _parse_em_klines(klines, days)
        if df is None or df.empty:
            return None
        logger.info(f"期货K线(东财兜底) {symbol}({secid}) klt={klt}: 获取 {len(df)} 根 "
                    f"({df['trade_time'].iloc[0].date()} ~ {df['trade_time'].iloc[-1].date()})")
        return df
    except requests.exceptions.Timeout:
        logger.warning(f"东财期货K线请求超时: {symbol}({secid})")
        return None
    except requests.exceptions.ConnectionError:
        logger.warning(f"东财期货K线连接失败: {symbol}({secid})")
        return None
    except Exception as e:
        logger.warning(f"东财期货K线获取异常: {symbol}({secid}): {e}")
        return None


def _clip_kline_days(df: pd.DataFrame | None, days: int) -> pd.DataFrame | None:
    """把 K 线裁到最近 days 根（对外契约：返回根数不得超过请求的 days）。

    必须裁：① 新浪/东财的 days 是「请求根数」，源站实际返回常多几根；
    ② 磁盘/内存缓存存的是**全量**（供后续更大 days 复用），命中缓存时长度与
    本次请求的 days 无关。不裁会让 `GET /api/kline?days=300` 返回 302 根，
    违反 F-203 验收①（`test_ft2f_futures_rb0_daily_night_live` 暴露）。
    注意：调用方写缓存必须用**未裁剪**的 df，切勿本末倒置。
    """
    if df is None or not days or days <= 0 or len(df) <= days:
        return df
    return df.iloc[-days:].reset_index(drop=True)


def fetch_futures_daily(symbol: str, secid: str, days: int = 300,
                        use_cache: bool = True) -> pd.DataFrame | None:
    """获取期货日K线（带三级缓存：内存 → 磁盘 → 网络）。

    缓存 key = main_code（从 secid 提取），主力连续(rb0)与具体合约(rb2510)互不覆盖。

    Args:
        symbol: 品种代码（如 'rb', 'cu', 'TA'）
        secid: Eastmoney secid（如 '113.rb0' 或 '113.rb2510'）
        days: K线根数上限
        use_cache: 是否使用缓存（回测可关闭以强制刷新）

    Returns:
        pd.DataFrame: trade_time/open/close/high/low/volume 或 None
    """
    # 从 secid 提取合约代码（如 '113.rb0' → 'rb0', '115.TA510' → 'TA510'）
    main_code = secid.split('.')[-1] if '.' in secid else secid
    # 1. 内存缓存
    if use_cache:
        df = _load_mem_cache(main_code)
        if df is not None:
            return _clip_kline_days(df, days)

    # 2. 磁盘缓存
    if use_cache:
        df = _load_disk_cache(main_code)
        if df is not None:
            # 检查是否需要更新（最后一条 K 线距今 >1 天则刷新）
            last_bar_date = df['trade_time'].iloc[-1]
            if (datetime.now() - last_bar_date.to_pydatetime()).days <= 1:
                _save_mem_cache(main_code, df)
                return _clip_kline_days(df, days)
            # 缓存过期 → 在线刷新
            logger.info(f"期货 {main_code} 缓存过期(最后K线 {last_bar_date.date()})，在线刷新")

    # 3. 在线获取（tdx 恒优先：TCP 7721 无 WAF 配额，且主连 L8 只有 tdx/新浪支持）
    #    - tdx：days 超出主连深度上限（700 根）时直接跳过，交给东财/新浪拿全历史
    #    - 主力连续（rb0/TA0 等）：东财不支持 → [tdx, sina, eastmoney]
    #    - 具体月份（rb2610/TA2510 等）：[tdx, 首选源, 备用源]
    df = None
    for src in _ordered_sources(main_code, days):
        if src == 'tdx':
            if days > 700:      # 与 tdx._TDX_FUT_MAX_BARS 对齐
                logger.debug(f"期货 {main_code} 请求 {days} 根超出 tdx 主连深度，跳过")
                continue
            df = _fetch_tdx_kline(symbol, main_code, days)
        elif src == 'eastmoney':
            df = _fetch_eastmoney_kline(symbol, secid, days, klt=101)
        else:  # 'sina'
            df = _fetch_sina_kline(symbol, main_code, days)
        if df is not None:
            _mark_ok(src)
            break
        _mark_fail(src)
        logger.info(f"期货 {main_code} {src}失败，尝试下一数据源")
    if df is None:
        # 全部网络源失败 → 回退磁盘缓存（即便过期，总比没有强）
        if use_cache:
            df = _load_disk_cache(main_code)
            if df is not None:
                logger.info(f"期货 {main_code} 全部数据源失败，使用过期磁盘缓存({len(df)}根)")
                _save_mem_cache(main_code, df)
                return _clip_kline_days(df, days)
        logger.warning(f"期货 {main_code} 全部数据源失败（主源+兜底+磁盘缓存均无数据）")
        return None

    # 4. 写缓存（缓存保留全量，供后续更大 days 复用）
    _save_disk_cache(main_code, df)
    _save_mem_cache(main_code, df)
    return _clip_kline_days(df, days)


# ── 东财实时行情 API ──
_EM_REALTIME_URL = "https://push2.eastmoney.com/api/qt/stock/get"
# 期货关键字段：f43=最新价 f44=最高 f45=最低 f46=开盘 f47=成交量 f48=成交额
# f58=名称 f60=昨收 f169=涨跌 f170=涨跌幅 f31=持仓量(期货专用)
_EM_REALTIME_FIELDS = "f43,f44,f45,f46,f47,f48,f58,f60,f169,f170,f31"


def _fetch_eastmoney_realtime(symbol: str, secid: str) -> dict | None:
    """东财 push2 实时行情（含持仓量）。

    Args:
      symbol: 品种代码（如 'rb'）
      secid: 东财 secid（如 '113.rb0'）

    Returns:
      dict: {name, last, open, high, low, volume, amount, prev_settle,
             change, change_pct, open_interest, source} 或 None
    """
    em_secid = _to_eastmoney_czce_secid(secid)
    url = (f"{_EM_REALTIME_URL}?secid={em_secid}&fields={_EM_REALTIME_FIELDS}")
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://quote.eastmoney.com/',
    }
    try:
        _rate_limit()
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            logger.warning(f"东财实时行情 HTTP {resp.status_code}: {symbol}({em_secid})")
            return None
        j = resp.json()
        d = j.get('data')
        if not d:
            logger.warning(f"东财实时行情无 data: {symbol}({em_secid})")
            return None
        # 东财期货价格字段需除以 100（f43/f44/f45/f46/f60）
        def _p(v):
            try:
                return float(v) / 100.0 if v and v != '-' else 0.0
            except (ValueError, TypeError):
                return 0.0
        last_price = _p(d.get('f43', 0))
        prev_settle = _p(d.get('f60', 0))
        open_interest = 0
        try:
            oi_val = d.get('f31')
            if oi_val and oi_val != '-':
                open_interest = int(oi_val)
        except (ValueError, TypeError):
            pass
        return {
            'symbol': symbol,
            'name': d.get('f58') or symbol,
            'last': last_price,
            'open': _p(d.get('f46', 0)),
            'high': _p(d.get('f44', 0)),
            'low': _p(d.get('f45', 0)),
            'volume': float(d.get('f47', 0) or 0),
            'amount': float(d.get('f48', 0) or 0),
            'prev_settle': prev_settle,
            'change': round(last_price - prev_settle, 2) if prev_settle else None,
            'change_pct': round((last_price - prev_settle) / prev_settle * 100, 2) if prev_settle else None,
            'open_interest': open_interest,
            'source': 'eastmoney_realtime',
        }
    except Exception as e:
        logger.debug(f"东财实时行情 {symbol}({em_secid}) 失败: {e}")
        return None


def _fetch_sina_hq_realtime(symbol: str) -> dict | None:
    """新浪实时行情（hq.sinajs.cn，GBK）——含持仓量。

    是持仓量的可靠备用来源（东财 push2 被断连/封禁时仍可用）。
    仅商品期货支持（代码格式 ``{SYMBOL}0``，如 RB0/CU0/AU0）；股指/国债（IF/IH/IC/IM/T/TF/TS/TL）
    新浪 hq 不提供，返回空串 → None（由调用方继续走后续兜底，不影响 K 线主链）。

    新浪期货 hq 字段布局（实测 AU0，28 字段）：
      0=名称 1=时间 2=开盘 3=最高 4=最低 5=昨结? 6=买一 7=卖一 8=最新
      9=涨跌 10=昨结算 11=涨? 12=跌? 13=成交量 14=持仓量 15=交易所 16=中文名 17=日期 ...

    Returns:
      dict: {name, last, open, high, low, prev_settle, change, change_pct,
             volume, open_interest, time_str, source} 或 None
    """
    if not symbol:
        return None
    code = str(symbol).upper() + '0'  # hq 连续合约代码（RB0/CU0/AU0/TA0）
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://finance.sina.com.cn/',
    }
    try:
        _rate_limit()
        resp = requests.get(f'http://hq.sinajs.cn/list={code}', headers=headers, timeout=10)
        if resp.status_code != 200:
            logger.debug(f"新浪hq实时 {code} HTTP {resp.status_code}")
            return None
        resp.encoding = 'gbk'
        text = resp.text.strip()
        if '=\"' not in text:
            return None
        fields = text.split('=\"', 1)[1].rsplit('\"', 1)[0].split(',')
        # 股指/国债等新浪不提供 → 空串或字段不足 → 交给后续兜底
        if not fields or len(fields) < 15 or not fields[14]:
            return None

        def _f(i):
            try:
                v = fields[i] if i < len(fields) else ''
                return float(v) if v and v != '-' else 0.0
            except (ValueError, TypeError):
                return 0.0

        last_price = _f(8)
        prev_settle = _f(10)
        open_interest = int(_f(14)) if fields[14] else 0
        volume = _f(13)
        return {
            'symbol': symbol,
            'name': fields[0] or symbol,
            'last': last_price,
            'open': _f(2),
            'high': _f(3),
            'low': _f(4),
            'volume': volume,
            'amount': 0.0,
            'prev_settle': prev_settle,
            'change': round(last_price - prev_settle, 2) if prev_settle else None,
            'change_pct': round((last_price - prev_settle) / prev_settle * 100, 2) if prev_settle else None,
            'open_interest': open_interest,
            'time_str': fields[1],
            'source': 'sina_hq',
        }
    except Exception as e:
        logger.debug(f"新浪hq实时 {code} 失败: {e}")
        return None


def fetch_futures_realtime(symbol: str, secid: str) -> dict | None:
    """获取期货实时行情（含持仓量）。

    策略（按优先级）：
      1. 东财 push2 实时行情 → 含持仓量/昨收/最新价
      2. 新浪 1 分钟 K 线 JSONP → 取最后一根的最新价（无持仓量）
      3. 日 K 线最新收盘价（兜底）

    Args:
      symbol: 品种代码（如 'rb'），仅用于日志和返回
      secid: Eastmoney secid（如 '113.rb0' 或 '113.rb2510'）

    Returns:
      dict: {name, open, prev_settle, last, high, low, volume, open_interest, ...} 或 None
    """
    # 从 secid 提取合约代码
    main_code = secid.split('.')[-1] if '.' in secid else secid

    # 方法1: 东财实时行情（含持仓量）
    try:
        result = _fetch_eastmoney_realtime(symbol, secid)
        if result and result.get('last', 0) > 0:
            _mark_ok('eastmoney')
            result['time_str'] = ''
            return result
        elif result:
            _mark_fail('eastmoney')
    except Exception as e:
        _mark_fail('eastmoney')
        logger.debug(f"东财实时行情 {symbol} 失败: {e}")

    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Referer': 'https://finance.sina.com.cn/',
    }

    # 方法2: 新浪 hq 实时行情（含持仓量；商品期货可用，股指/国债无）
    try:
        hq = _fetch_sina_hq_realtime(symbol)
        if hq and hq.get('last', 0) > 0:
            _mark_ok('sina')
            return hq
        elif hq:
            _mark_fail('sina')
    except Exception as e:
        _mark_fail('sina')
        logger.debug(f"新浪hq实时 {symbol} 失败: {e}")

    # 方法3: 新浪1分钟K线 → 最新价（无持仓量）
    try:
        _rate_limit()
        url = (f'http://stock2.finance.sina.com.cn/futures/api/jsonp.php'
               f'/var%20_{main_code}=/InnerFuturesNewService.getFewMinLine'
               f'?symbol={main_code}&type=1')
        resp = requests.get(url, headers=headers, timeout=10)
        text = resp.text
        # 提取 JSON 数组
        import re
        m = re.search(r'=\(\[(.+)\]\)', text, re.DOTALL)
        if m:
            import json as _json
            klines = _json.loads('[' + m.group(1) + ']')
            if klines:
                _mark_ok('sina')
                last_bar = klines[-1]
                # 用缓存的日K取昨收
                df_daily = _load_disk_cache(main_code)
                prev_settle = float(df_daily['close'].iloc[-2]) if df_daily is not None and len(df_daily) >= 2 else 0
                last_price = float(last_bar.get('c', 0))
                return {
                    'symbol': symbol,
                    'name': symbol,
                    'last': last_price,
                    'open': float(last_bar.get('o', 0)),
                    'high': float(last_bar.get('h', 0)),
                    'low': float(last_bar.get('l', 0)),
                    'volume': float(last_bar.get('v', 0)),
                    'amount': float(last_bar.get('p', 0)),
                    'prev_settle': prev_settle,
                    'change': round(last_price - prev_settle, 2) if prev_settle else None,
                    'change_pct': round((last_price - prev_settle) / prev_settle * 100, 2) if prev_settle else None,
                    'open_interest': 0,
                    'time_str': last_bar.get('d', ''),
                    'source': 'sina_1min',
                }
    except Exception as e:
        _mark_fail('sina')
        logger.debug(f"新浪1分钟K线 {main_code} 失败: {e}")

    # 方法3: 日K线最新收盘价（兜底）
    try:
        df = _load_disk_cache(main_code)
        if df is None:
            df = _fetch_sina_kline(symbol, main_code, 300)
            if df is not None:
                _mark_ok('sina')
            else:
                _mark_fail('sina')
        if df is None:
            # 新浪日K也失败 → 走东财兜底（与 fetch_futures_daily 同一策略，CZCE 编码自动转换）
            em_secid = _to_eastmoney_czce_secid(secid)
            df = _fetch_eastmoney_kline(symbol, em_secid, 300, klt=101)
            if df is not None:
                _mark_ok('eastmoney')
            else:
                _mark_fail('eastmoney')
        if df is not None and not df.empty:
            last_row = df.iloc[-1]
            prev_close = float(df['close'].iloc[-2]) if len(df) >= 2 else 0
            last_price = float(last_row['close'])
            from engine.futures_pool import get_contract
            contract = get_contract(symbol)
            return {
                'symbol': symbol,
                'name': contract.name if contract else symbol,
                'last': last_price,
                'open': float(last_row['open']),
                'high': float(last_row['high']),
                'low': float(last_row['low']),
                'volume': float(last_row['volume']),
                'amount': 0,
                'prev_settle': prev_close,
                'change': round(last_price - prev_close, 2) if prev_close else None,
                'change_pct': round((last_price - prev_close) / prev_close * 100, 2) if prev_close else None,
                'open_interest': 0,
                'time_str': str(last_row['trade_time']),
                'source': 'daily_kline',
            }
    except Exception as e:
        logger.debug(f"日K线兜底 {symbol} 失败: {e}")

    return None


def batch_fetch(contracts, days=300, show_progress=True):
    """批量获取多个期货品种的日K线。

    Args:
        contracts: list[FuturesContract]
        days: K线根数
        show_progress: 是否打印进度

    Returns:
        dict[symbol -> pd.DataFrame]: 品种代码到K线DataFrame的映射
    """
    result = {}
    total = len(contracts)
    failed = []

    for i, c in enumerate(contracts):
        if show_progress:
            print(f"  [{i+1}/{total}] {c.symbol} ({c.name}) ...", end=' ', flush=True)

        df = fetch_futures_daily(c.symbol, c.secid, days=days)
        if df is not None and len(df) > 0:
            result[c.symbol] = df
            if show_progress:
                print(f"OK ({len(df)}根)")
        else:
            failed.append(c.symbol)
            if show_progress:
                print("FAIL")

    if show_progress:
        print(f"  批量获取完成: {len(result)}/{total} 成功", end='')
        if failed:
            print(f", 失败: {failed}")
        else:
            print()

    return result
