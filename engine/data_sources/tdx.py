#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""通达信（pytdx）K 线数据源 —— 第一手前复权日 K。

为什么存在
----------
腾讯 fqkline 入口受「IP + 入口」物理配额约束（单入口约 350 次 / 窗口 1800s），
5200 只全市场刷新必然撞墙，只能靠压缩请求数（快速通道）绕。通达信 pytdx 走
TCP 7709 原生行情协议，实测 5220 只连续请求**零限流**（27.5s / 7 连接并行），
且自算前复权与腾讯 qfq **逐日偏差 0.000%** —— 故作为第一手日 K 源接入，
腾讯退为回退源。

⛔ 反直觉的一条（排查必读）
--------------------------
pytdx 内置 104 台服务器里**只有 7 台**能返回 K 线。其余 97 台 TCP 可连、
``get_security_count`` / ``get_xdxr_info`` / ``get_finance_info`` **全部正常**
返回真实数据，唯独 ``get_security_bars``（命令号 ``0x10c``）恒为空。
这是**命令级不被响应**（老协议命令在这些节点已下线），
**不是**网络问题、不是封禁、不是限流 ——
排查方向是「换服务器」，不要降频、换 UA 或上代理。
判据脚本 `audit/_probe_pytdx_scan104.py`。

前复权口径
----------
pytdx 只给未复权原始价，前复权由本模块自算：取 ``category == 1`` 的除权除息
事件，按

    除权参考价 = (前收 - 派息/10 + 配股价 × 配股/10) / (1 + 送转/10 + 配股/10)

得参考价，比值即该事件因子，累积乘到**该日之前**的所有价格上。
⚠️ xdxr 字段是**每 10 股**口径（``peigujia`` 除外，它是每股配股价）。
⚠️ 只取 ``category == 1``；5/9/11/12/13/14 是股本变更类，套进价格公式会算错。

单位口径
--------
``vol`` 已是**手**（与腾讯 fqkline 的 volume 一致）——
依据：浙能电力 2026-09-18 快照 vol=331100 / amount=167770288，
按「手」算均价 5.07 元合理，按「股」算 506 元不合理。
"""
import bisect
import threading
import itertools
import logging
import time
from datetime import datetime

import pandas as pd

from engine.data_sources import tdx_nodes
from engine.data_sources.base import DataSource

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 服务器池
# ---------------------------------------------------------------------------
# 节点池已收敛到 engine/data_sources/tdx_nodes.py：种子兜底 + 运行时探测持久化，
# 节点漂移时 App 自愈，无需改代码重新发版。此处 _SERVERS 仅保留为**种子别名**，
# 供 audit/_verify_tdx_source.py 等旧引用使用；实际轮询走 tdx_nodes.get_servers()。
_SERVERS = tdx_nodes.SEED_SERVERS

_CONNECT_TIMEOUT = 8.0     # 秒；实测同城 29~37ms，8s 足够容忍抖动
_BARS_PER_REQ = 800        # pytdx 单次请求上限，超出靠 start 分页
_MAX_PAGES = 12            # 全历史最多 9600 根（A 股最早 1990-12 上市 ≈ 8600 个交易日）
                           # ⚠️ 服务器**保留全部历史**，没有固定根数上限：实测 sh600519
                           # 拿到 6008 根（2001-08-27 上市至今）。先前「服务器只留 ~3100 根」
                           # 的说法有误 —— 浙能电力只有 3102 根是因为它 2013-12 才借壳上市。
_REQUEST_RETRY = 2         # 单次抓取的换服务器重试次数

# k_type -> pytdx category：日K/周K（前复权链路用）与分钟K（未复权链路用）
_CATEGORY = {240: 9, 1200: 5}
_CATEGORY_MINUTE = {1: 7, 5: 0, 15: 1, 30: 2, 60: 3}

# 进程内 xdxr 缓存：{code_key: (抓取日期, [事件])}。
# 除权信息一年才变几次，逐只每日重拉是纯浪费（5200 只 = 5200 次多余请求）。
# 按「自然日」失效：跨日自动重拉，日内复用。
_xdxr_cache = {}
_xdxr_cache_lock = threading.RLock()

# 进程内财务指标缓存：{code_key: (抓取日期, (fin, None))}。
# 财务数据一天才变一次，逐次重拉是纯浪费（实时分析/自选诊断会逐只拉）。
# 同样按「自然日」失效：跨日自动重拉，日内复用。复用 _xdxr 的失效口径。
_finance_cache = {}
_finance_cache_lock = threading.RLock()

# 每线程一个连接（pytdx 单 socket 非线程安全；并发靠多线程 × 多服务器）
_tls = threading.local()
_server_cursor = itertools.count()


def _next_server():
    """轮询取下一台服务器（多线程下分摊到池内各台，避免同台挤连接）。"""
    servers = tdx_nodes.get_servers()
    return servers[next(_server_cursor) % len(servers)]


def _split_code(stock_code):
    """'sh600023' -> (1, '600023')；'sz000001' -> (0, '000001')。

    无前缀时按首位数字猜市场（6/9/5 为沪，其余为深/北）。
    """
    s = str(stock_code or "").strip().lower()
    if s.startswith("sh"):
        return 1, s[2:]
    if s.startswith("sz"):
        return 0, s[2:]
    if s.startswith("bj"):
        return 0, s[2:]
    if s[:1] in ("6", "9", "5"):
        return 1, s
    return 0, s


def _import_api_cls():
    """延迟 import pytdx：未安装时返回 None，由调用方回退腾讯源。

    ⚠️ 打包时必须在 build_webview.spec 的 hiddenimports 登记 pytdx，
    否则 frozen 环境下 ImportError 会被 try/except 吞掉 → 静默回退腾讯，
    表现为「装了 pytdx 却没生效」。
    """
    try:
        from pytdx.hq import TdxHq_API
        return TdxHq_API
    except Exception as e:
        logger.warning("pytdx 不可用（将回退腾讯源）: %s", e)
        return None


# ---------------------------------------------------------------------------
# 连接管理
# ---------------------------------------------------------------------------
def _ensure_api():
    """取本线程的连接；无则新建（失败换下一台）。返回 api 或 None。"""
    api = getattr(_tls, "api", None)
    if api is not None:
        return api
    cls = _import_api_cls()
    if cls is None:
        return None
    servers = tdx_nodes.get_servers()
    for _ in range(len(servers)):
        ip, port = _next_server()
        try:
            a = cls(heartbeat=False, auto_retry=False, raise_exception=False)
            if a.connect(ip, port, time_out=_CONNECT_TIMEOUT):
                _tls.api = a
                _tls.srv = (ip, port)
                return a
        except Exception as e:
            logger.debug("pytdx 连接 %s:%s 失败: %s", ip, port, e)
            continue
    # 全池连接失败：上报节点池触发后台重扫（节流，不阻塞本次调用）
    tdx_nodes.note_failure()
    return None


def _drop_api():
    """断开并丢弃本线程连接，下次调用自动换服务器重连。"""
    api = getattr(_tls, "api", None)
    if api is not None:
        try:
            api.disconnect()
        except Exception:
            pass
    _tls.api = None


# ── ExHq（期货）独立连接管理 ──────────────────────────────────────
# ⛔ 期货走 TdxExHq_API + 7721，与股票的 TdxHq_API + 7709 是**两套协议、两批服务器**。
# 连接管理必须独立 —— 复用股票的 _ensure_api 会在 TdxHq_API 上调 get_instrument_bars
# ⇒ AttributeError ⇒ _call 吞掉重试后返回 None（实测踩到：
# 「期货是单独增加通达信接口吧」这一问暴露，此前东财兜底把失败掩盖了）。
_EX_SERVERS = (
    ("58.63.254.216", 7721),
    ("202.96.138.90", 7721),
)
_ex_tls = threading.local()
_ex_cursor = itertools.count()


def _import_ex_api_cls():
    """延迟 import ExHq API。pytdx 包内就含 pytdx.exhq 单模块，无需额外打包登记
    （spec 已 collect_submodules('pytdx') 全量收集）。"""
    try:
        from pytdx.exhq import TdxExHq_API
        return TdxExHq_API
    except Exception as e:
        logger.warning("pytdx.exhq 不可用（期货源将回退东财/新浪）: %s", e)
        return None


def _ensure_ex_api():
    """取本线程的 ExHq 连接；无则新建（失败换下一台）。"""
    api = getattr(_ex_tls, "api", None)
    if api is not None:
        return api
    cls = _import_ex_api_cls()
    if cls is None:
        return None
    for _ in range(len(_EX_SERVERS)):
        ip, port = _EX_SERVERS[next(_ex_cursor) % len(_EX_SERVERS)]
        try:
            a = cls(heartbeat=False, auto_retry=False, raise_exception=False)
            if a.connect(ip, port, time_out=_CONNECT_TIMEOUT):
                _ex_tls.api = a
                _ex_tls.srv = (ip, port)
                return a
        except Exception as e:
            logger.debug("ExHq 连接 %s:%s 失败: %s", ip, port, e)
            continue
    return None


def _drop_ex_api():
    api = getattr(_ex_tls, "api", None)
    if api is not None:
        try:
            api.disconnect()
        except Exception:
            pass
    _ex_tls.api = None


def _ex_call(fn):
    """ExHq 版 _call：执行 fn(api)，异常换服务器重试；全失败返回 None。"""
    last_exc = None
    for _ in range(_REQUEST_RETRY + 1):
        api = _ensure_ex_api()
        if api is None:
            return None
        try:
            return fn(api)
        except Exception as e:
            last_exc = e
            _drop_ex_api()
    if last_exc is not None:
        logger.debug("ExHq 请求最终失败: %s", last_exc)
    return None


def _call(fn):
    """执行 fn(api)，异常时换服务器重试。返回 fn 的结果，全失败返回 None。

    注意：fn 返回 None 表示「源侧确实没数据」，不触发重试；
    只有**抛异常**（断连 / 协议错）才重试。
    """
    last_exc = None
    for _ in range(_REQUEST_RETRY + 1):
        api = _ensure_api()
        if api is None:
            return None
        try:
            return fn(api)
        except Exception as e:
            last_exc = e
            _drop_api()
    if last_exc is not None:
        logger.debug("pytdx 请求最终失败: %s", last_exc)
    return None


# ---------------------------------------------------------------------------
# 取数
# ---------------------------------------------------------------------------
def _fetch_raw_bars(market, code, category, total=None, full=False):
    """拉原始（未复权）K 线，升序返回。

    start 语义 = 从**最新**往前数的偏移，所以分页要前插。
    full=True 时一直翻到服务器尽头（实测日 K 约 3100 根，即 ~12.7 年）。
    """
    per = _BARS_PER_REQ
    pages = _MAX_PAGES if full else max(1, (int(total or 1) + per - 1) // per)

    def _work(api):
        out = []
        start = 0
        for _ in range(pages):
            cnt = per if full else min(per, max(1, int(total or 1)) - len(out))
            if cnt <= 0:
                break
            chunk = api.get_security_bars(category, market, code, start, cnt)
            if not chunk:
                # 个股接口无数据：可能是指数 → 试指数接口（仅首页尝试一次）
                if start == 0:
                    chunk = api.get_index_bars(category, market, code, 0, cnt)
                if not chunk:
                    break
            out = chunk + out          # 前插：start=0 是最新
            if len(chunk) < cnt:
                break                  # 已到尽头
            start += cnt
        return out

    return _call(_work)


def _get_xdxr(market, code):
    """取除权除息事件（进程内按日缓存）。返回 list，失败返回 []。"""
    key = f"{market}:{code}"
    today = datetime.now().date()
    with _xdxr_cache_lock:
        hit = _xdxr_cache.get(key)
        if hit and hit[0] == today:
            return hit[1]

    data = _call(lambda api: api.get_xdxr_info(market, code)) or []
    with _xdxr_cache_lock:
        _xdxr_cache[key] = (today, data)
    return data


def compute_qfq_factors(n_bars, dates, xdxr, closes):
    """计算每个 bar 的前复权累积因子（以最新价为锚点）。

    Args:
        n_bars: bar 数量
        dates: 长度 n_bars 的日期字符串列表（**必须升序**）
        xdxr: pytdx 的 xdxr 记录列表
        closes: 长度 n_bars 的原始收盘价列表

    Returns:
        list[float]，长度 n_bars；qfq_close[i] = closes[i] * factors[i]

    ⛔ 关键：除权日**可能不在 K 线里**。长期停牌（股权分置改革、重大资产重组）
    会让除权日落在停牌区间内 —— 例如 sh600519 于 2006-05-19 除权（10送10），
    但当天停牌，K 线从 2006-04-25 直接跳到 2006-05-25（复牌首日）。

    若按日期**精确匹配**，这次除权会被静默跳过 → 复牌首日出现 -56% 的假跳变
    → 被 backtest 的 `_validate_qfq` 判成「未复权」而整只拒绝（实测踩到）。
    正确做法：用 bisect 找「第一个 >= 除权日的 bar」作为锚点 ——
    该 bar 就是复权后的第一根，其前一根（停牌前最后交易日）即公式里的「前收」。
    """
    date_list = list(dates)                 # 已升序
    events = []
    for r in xdxr:
        if r.get("category") != 1:          # 只认除权除息，股本变更类不参与价格调整
            continue
        try:
            d = "%04d-%02d-%02d" % (int(r["year"]), int(r["month"]), int(r["day"]))
        except Exception:
            continue
        idx = bisect.bisect_left(date_list, d)
        if idx >= n_bars:
            # 除权日在数据末端之后（尚未复牌 / 数据未更新）—— 无从调整
            continue
        events.append((d, idx, r))
    # 多个除权日可能锚到同一根（同一停牌期内的多次分派）→ 按 (锚点, 日期) 升序累积
    events.sort(key=lambda x: (x[1], x[0]))

    factors = [1.0] * n_bars
    for _d, idx, r in events:
        if idx == 0:
            continue
        prev_close = float(closes[idx - 1])
        if prev_close <= 0:
            continue
        fh = (r.get("fenhong") or 0.0) / 10.0          # 每 10 股派息 -> 每股
        sz = (r.get("songzhuangu") or 0.0) / 10.0      # 每 10 股送转 -> 每股
        pg = (r.get("peigu") or 0.0) / 10.0            # 每 10 股配股 -> 每股
        pgj = float(r.get("peigujia") or 0.0)          # 配股价（每股，不除 10）
        ref = (prev_close - fh + pgj * pg) / (1.0 + sz + pg)
        if ref <= 0:
            continue
        k = ref / prev_close
        for i in range(idx):                           # 该锚点**之前**的价格缩放
            factors[i] *= k
    return factors


def fetch_daily_qfq(stock_code, k_type=240, days=None, full=False):
    """抓取前复权 K 线，返回 ``(df, err)``，与 ``KLineFetcher`` 契约一致。

    df 列：trade_time / open / close / high / low / volume（升序）。
    trade_time 为 Timestamp；volume 单位为**手**。

    Args:
        days: 期望根数（None 表示不限，配合 full=True 拉全历史）
        full: True 时翻页到服务器尽头（首次建库用，实测 ~3100 根）
    """
    pre = _preflight()
    if pre:
        return None, pre
    if k_type not in _CATEGORY:
        return None, f"通达信源不支持该周期(k_type={k_type})"
    category = _CATEGORY[k_type]

    market, code = _split_code(stock_code)
    if not code or not code.isdigit():
        return None, f"通达信源代码非法: {stock_code}"

    bars = _fetch_raw_bars(market, code, category, total=days, full=full)
    if not bars:
        return None, f"通达信K线为空({stock_code})"

    dates = [(b.get("datetime") or "")[:10] for b in bars]
    closes = [float(b.get("close") or 0.0) for b in bars]

    # 前复权只在日K有意义；周K/其他周期同样按除权事件缩放（口径与日K一致）
    xdxr = _get_xdxr(market, code)
    factors = compute_qfq_factors(len(bars), dates, xdxr, closes) if xdxr else [1.0] * len(bars)

    rows = []
    for b, f in zip(bars, factors):
        try:
            rows.append({
                "trade_time": pd.Timestamp(b.get("datetime")),
                "open": float(b.get("open")) * f,
                "close": float(b.get("close")) * f,
                "high": float(b.get("high")) * f,
                "low": float(b.get("low")) * f,
                "volume": float(b.get("vol") or 0.0),
            })
        except Exception:
            continue
    if not rows:
        return None, f"通达信K线解析为空({stock_code})"

    df = pd.DataFrame(rows)
    df = df[df["close"] > 0]
    df = df.sort_values("trade_time").drop_duplicates(
        subset=["trade_time"], keep="last").reset_index(drop=True)
    if df.empty:
        return None, f"通达信K线清洗后为空({stock_code})"
    return df, None


def fetch_minute_raw(stock_code, k_type, days=300):
    """抓取**未复权**分钟K，返回 ``(df, err)``，契约与新浪分钟链路一致。

    df 列：trade_time / open / close / high / low / volume（升序）。

    ⚠️ 单位口径：volume = **股** —— 与现有新浪分钟链路（`_fetch_sina_minute`）
    完全一致，**无需换算**。注意这与本模块日K的 `vol`（=手）**不同**：
    通达信两个接口的单位本来就不一样，实测 sh600023 收盘竞价分钟 bar
    V=1162900（股，占当日 3311 万股成交约 3.5%，合理）。

    历史深度实测（sh600023）：1分K 22320 根（2026-05-12 起）、
    5分K 23568 根（2024-09-10 起 ≈2 年）、15/30/60 分同样回溯到 2024-09-10
    —— 比新浪 `datalen` 上限 1023 根（5分K ≈21 个交易日）**深约 20 倍**。
    """
    pre = _preflight()
    if pre:
        return None, pre
    category = _CATEGORY_MINUTE.get(int(k_type) if k_type else 0)
    if category is None:
        return None, f"通达信源不支持该分钟周期(k_type={k_type})"

    market, code = _split_code(stock_code)
    if not code or not code.isdigit():
        return None, f"通达信源代码非法: {stock_code}"

    total = max(1, int(days or 300))
    pages = max(1, (total + _BARS_PER_REQ - 1) // _BARS_PER_REQ)

    def _work(a):
        out = []
        start = 0
        while len(out) < total:
            cnt = min(_BARS_PER_REQ, total - len(out))
            chunk = a.get_security_bars(category, market, code, start, cnt)
            if not chunk:
                break
            out = chunk + out          # start=0 是最新，前插保持升序
            if len(chunk) < cnt:
                break
            start += cnt
        return out

    bars = _call(_work)
    if not bars:
        return None, f"通达信分钟K为空({stock_code})"

    rows = []
    for b in bars:
        try:
            c = float(b.get("close") or 0)
            if c <= 0:
                continue
            rows.append({
                "trade_time": pd.Timestamp(b.get("datetime")),
                "open": float(b.get("open")),
                "high": float(b.get("high")),
                "low": float(b.get("low")),
                "close": c,
                "volume": float(b.get("vol") or 0),
            })
        except Exception:
            continue
    if not rows:
        return None, f"通达信分钟K解析为空({stock_code})"

    df = pd.DataFrame(rows)
    df = df.sort_values("trade_time").drop_duplicates(
        subset=["trade_time"], keep="last").reset_index(drop=True)
    return df, None


def fetch_index_daily(index_code, days=1500):
    """指数日线（`get_index_bars`，**不复权** —— 指数无复权概念）。

    返回 ``(df, err)``，列契约与 `IndexFetcher._fetch_index_daily`（腾讯）完全一致：
    trade_time / open / close / high / low / volume，按时间升序。
    实测覆盖：上证指数 / 深证成指 / 创业板指 / 沪深300（market 0/1 由前缀推导）。
    """
    pre = _preflight()
    if pre:
        return None, pre
    market, code = _split_code(index_code)
    if not code or not code.isdigit():
        return None, f"指数代码非法: {index_code}"

    total = max(1, int(days or 1500))
    pages = max(1, (total + _BARS_PER_REQ - 1) // _BARS_PER_REQ)

    def _work(a):
        out, start = [], 0
        while len(out) < total:
            cnt = min(_BARS_PER_REQ, total - len(out))
            chunk = a.get_index_bars(9, market, code, start, cnt)
            if not chunk:
                break
            out = chunk + out          # start=0 是最新，前插保持升序
            if len(chunk) < cnt:
                break
            start += cnt
        return out

    bars = _call(_work)
    if not bars:
        return None, f"通达信指数日线为空({index_code})"

    rows = []
    for b in bars:
        try:
            c = float(b.get("close") or 0)
            if c <= 0:
                continue
            rows.append({
                "trade_time": pd.Timestamp(b.get("datetime")),
                "open": float(b.get("open")),
                "close": c,
                "high": float(b.get("high")),
                "low": float(b.get("low")),
                "volume": float(b.get("vol") or 0),
            })
        except Exception:
            continue
    if not rows:
        return None, f"通达信指数日线解析为空({index_code})"

    df = pd.DataFrame(rows)
    df = df.sort_values("trade_time").drop_duplicates(
        subset=["trade_time"], keep="last").reset_index(drop=True)
    return df, None


def fetch_weekly_qfq(stock_code, days=200):
    """周K 前复权（自算）。返回 ``(df, err)``，列契约与腾讯周K（`fetch_weekly`）一致。

    复权用与日K相同的 `compute_qfq_factors`（bisect 锚定：找第一个 >= 除权日的
    周K bar，其前一根即「前收」）。

    ⚠️ **已知精度边界（用户拍板接受）**：除权日落在周中时，该周K是
    「除权前 + 除权后」的混合价，单一因子无法精确复权 —— 实测除权周与腾讯
    服务端有 0.1%~0.94% 偏差，无除权的周与腾讯**逐周完全一致**
    （`audit/_probe_tdx_weekly.py`，三标的 × 最近 20 周）。
    """
    pre = _preflight()
    if pre:
        return None, pre
    market, code = _split_code(stock_code)
    if not code or not code.isdigit():
        return None, f"通达信源代码非法: {stock_code}"

    bars = _fetch_raw_bars(market, code, 5, total=days, full=False)   # category 5 = 周K
    if not bars:
        return None, f"通达信周K为空({stock_code})"

    dates = [(b.get("datetime") or "")[:10] for b in bars]
    closes = [float(b.get("close") or 0) for b in bars]
    xdxr = _get_xdxr(market, code)
    factors = compute_qfq_factors(len(bars), dates, xdxr, closes) if xdxr else [1.0] * len(bars)

    rows = []
    for b, f in zip(bars, factors):
        try:
            c = float(b.get("close") or 0)
            if c <= 0:
                continue
            rows.append({
                "trade_time": pd.Timestamp(b.get("datetime")),
                "open": float(b.get("open")) * f,
                "close": c * f,
                "high": float(b.get("high")) * f,
                "low": float(b.get("low")) * f,
                "volume": float(b.get("vol") or 0),
            })
        except Exception:
            continue
    if not rows:
        return None, f"通达信周K解析为空({stock_code})"

    df = pd.DataFrame(rows)
    df = df.sort_values("trade_time").drop_duplicates(
        subset=["trade_time"], keep="last").reset_index(drop=True)
    return df, None


def fetch_realtime_snapshot(stock_code):
    """单只实时快照（`get_security_quotes`），返回 ``(fields, err)``。

    fields 为**归一新浪位序**（与 `RealtimeQuoteFetcher.fetch_tencent` 的产出一致，
    消费方契约见 `server/adapters/engine_bridge.py::realtime_quote`）：
      0=name 1=open 2=prev_close 3=price 4=high 5=low
      6=买一价 7=卖一价 8=volume(股) 9=amount(元) 10..29='' 30=date 31=time
    ⚠️ 单位：tdx 快照 `vol` = **手**（与日K一致），归一布局的 volume = **股**
    ⇒ 输出前 ×100（依据：浙能电力快照 vol=331100 手 × 5.08 × 100 ≈ amount 1.68 亿）。
    """
    market, code = _split_code(stock_code)
    if not code or not code.isdigit():
        return None, f"通达信源代码非法: {stock_code}"

    def _work(a):
        return a.get_security_quotes([(market, code)])

    q = _call(_work)
    if not q:
        return None, f"通达信实时快照为空({stock_code})"
    d = q[0] if isinstance(q, list) else q
    price = float(d.get("price") or 0)
    if price <= 0:
        return None, f"通达信实时快照无有效价格({stock_code})"

    name = ""
    try:
        from engine import state as _state
        name = (_state.state.code_to_name or {}).get(
            str(stock_code).strip().lower(), "")
    except Exception:
        name = ""

    ser = str(d.get("ser_time") or "")
    now = datetime.now()
    date_s, time_s = now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S")
    if len(ser) >= 6 and ser[:6].isdigit():
        time_s = f"{ser[0:2]}:{ser[2:4]}:{ser[4:6]}"

    fields = [
        name,
        f"{float(d.get('open') or 0):.2f}",
        f"{float(d.get('last_close') or 0):.2f}",
        f"{price:.2f}",
        f"{float(d.get('high') or 0):.2f}",
        f"{float(d.get('low') or 0):.2f}",
        f"{float(d.get('bid1') or 0):.3f}",
        f"{float(d.get('ask1') or 0):.3f}",
        str(int(float(d.get("vol") or 0) * 100)),      # 手 -> 股
        f"{float(d.get('amount') or 0):.0f}",
    ] + [""] * 20 + [date_s, time_s]
    return fields, None


# ── 财务数据 ─────────────────────────────────────────────────────
def fetch_finance(stock_code):
    """拉最新一期财务指标（`get_finance_info`），复用 `_call` 连接/重试模式。

    返回 ``(fin, err)``：
        fin = {meigujingzichan: 每股净资产, jingzichan: 每股净资产(别名),
               jinglirun: 净利润(判符号), updated_date},
        因 pytdx 该接口字段来自统计口径（见 get_finance_info 注释），
        逾期/解释性字段缺失时安全返回 None 由调用方降级。
    失败返回 (None, 错误串)；不可用（未装 pytdx/节点不可达）由 _preflight 拦截。

    ⛔ 只服务实时分析与诊断（单股逐查，成本可控），不进全市场扫描——
    全市场逐只拉会有请求量/限流成本，违背扫描性能约束。
    """
    market, code = _split_code(stock_code)
    if not code or not code.isdigit():
        return None, f"通达信财务代码非法: {stock_code}"

    # 进程内按日缓存：财务一天才变一次，日内复用，跨日重拉（与 _xdxr_cache 同口径）。
    key = f"{market}:{code}"
    today = datetime.now().date()
    with _finance_cache_lock:
        hit = _finance_cache.get(key)
        if hit and hit[0] == today:
            return hit[1]

    pre = _preflight()
    if pre:
        return None, pre

    def _work(api):
        return api.get_finance_info(market, code)

    d = _call(_work)
    if not d:
        return None, f"通达信财务为空({stock_code})"

    # 口径说明：pytdx get_finance_info 的 jingzichan/jinglirun 是大额总量口径且带
    # *10000 换算，不可直接当局值用；**每股净资产 meigujingzichan 才是标准可靠值**
    # （茅台 200.99 元/股，正数）。否决判定用每股口径；净利润取 jinglirun 判正负
    # （仅看符号，单位无关）。
    def _f(key):
        try:
            v = float(d.get(key) or 0.0)
            return v if v == v else 0.0      # NaN 归零
        except (TypeError, ValueError):
            return 0.0

    up_date = d.get("updated_date") or 0
    try:
        updated_date = "%08d" % int(up_date)
    except (TypeError, ValueError):
        updated_date = ""

    fin = {
        "meigujingzichan": _f("meigujingzichan"),
        "jingzichan": _f("meigujingzichan"),   # 别名：每股净资产（否决判资不抵债用）
        "jinglirun": _f("jinglirun"),
        "shuihoulirun": _f("shuihoulirun"),
        # 全市场扫描财务筛选用（与 jingzichan/jinglirun 同口径 ×10000 总量换算，
        # 只用于比值/乘积计算——市值=price×总股本、EPS=净利/总股本、
        # 资产负债率=(流动+长期负债)/总资产——同口径相除对绝对单位不敏感）：
        "zongguben": _f("zongguben"),         # 总股本（×10000 口径）
        "liutongguben": _f("liutongguben"),   # 流通股本（×10000 口径）
        "zongzichan": _f("zongzichan"),       # 总资产（×10000 口径）
        "liudongfuzhai": _f("liudongfuzhai"), # 流动负债（×10000 口径）
        "changqifuzhai": _f("changqifuzhai"), # 长期负债（×10000 口径）
        "updated_date": updated_date,
    }
    with _finance_cache_lock:
        _finance_cache[key] = (today, (fin, None))
    return fin, None


# ── 期货（ExHq）──────────────────────────────────────────────────
_TDX_FUT_CFFEX = frozenset({"IF", "IH", "IC", "IM", "T", "TF", "TS", "TL"})
_TDX_FUT_DCE = frozenset({"m", "i", "j", "jm", "jd", "c", "cs", "a", "b", "p",
                          "v", "pp", "eg", "l", "pg", "rr", "lh", "eb",
                          "fb", "bb", "lg"})
_TDX_FUT_MAX_BARS = 700      # 主连历史深度上限（实测各品种统一 2023-11-06 起）


def futures_market_and_code(main_code):
    """新浪式期货代码 → tdx ``(market, code)``。无法识别返回 ``(None, None)``。

    支持主连（'rb0'/'TA0'/'IF0'，→ 品种+L8）与具体月份合约（'rb2510'/'M2611'，
    → 品种大写+年月）。
    market 判定：47=CFFEX（品种名单）；29=DCE（小写名单）；30=SHFE/INE
    （其余小写 —— INE 在 ExHq 归入上期所，实测原油 SC 在 market=30）；
    28=CZCE（其余大写，如 TA/MA/SR）。
    """
    import re as _re
    m = _re.match(r"^([A-Za-z]+)(\d+)$", str(main_code or "").strip())
    if not m:
        return None, None
    prod, suffix = m.group(1), m.group(2)
    up = prod.upper()
    if suffix == "0":
        tdx_code = up + "L8"                 # 主力连续
    elif len(suffix) == 4:
        tdx_code = up + suffix               # 具体月份合约
    else:
        return None, None                    # CZCE 1 位年等非标格式不硬猜
    if up in _TDX_FUT_CFFEX:
        return 47, tdx_code
    if prod in _TDX_FUT_DCE:
        return 29, tdx_code
    if prod.islower():
        return 30, tdx_code
    return 28, tdx_code


def fetch_futures_daily(main_code, days=300):
    """期货主连/合约日K（ExHq `get_instrument_bars`，TCP 7721 无 WAF 配额）。

    返回 ``(df, err)``，列契约同新浪/东财：trade_time/open/close/high/low/volume。

    ⚠️ **volume = trade × 100**：tdx 的 `trade` 单位是手，东财 volume 单位是
    股/张，同合约逐日比值**精确 0.01**（`audit/_verify_tdx_futures_vol.py`）
    —— 对齐东财是因为它是当前默认源。

    ⚠️ **主连历史深度上限 700 根（2023-11-06 起）**：请求 days 超出深度时返回
    None（宁可不给短序列），由 `futures_data` 落到东财/新浪兜底。
    """
    pre = _preflight()
    if pre:
        return None, pre
    mk, tdx_code = futures_market_and_code(main_code)
    if mk is None:
        return None, f"无法映射通达信期货代码: {main_code}"
    total = max(1, int(days or 300))
    if total > _TDX_FUT_MAX_BARS:
        return None, f"超出通达信期货主连深度上限({total}>{_TDX_FUT_MAX_BARS})"
    pages = max(1, (total + _BARS_PER_REQ - 1) // _BARS_PER_REQ)

    def _work(a):
        out, start = [], 0
        while len(out) < total:
            cnt = min(_BARS_PER_REQ, total - len(out))
            chunk = a.get_instrument_bars(9, mk, tdx_code, start, cnt)
            if not chunk:
                break
            out = chunk + out
            if len(chunk) < cnt:
                break
            start += cnt
        return out

    bars = _ex_call(_work)      # ⛔ 必须走 ExHq 连接（7721），不是股票的 _call
    if not bars:
        return None, f"通达信期货日K为空({main_code}->{tdx_code})"
    if len(bars) < total:
        return None, f"通达信期货主连深度不足({tdx_code}: {len(bars)}/{total} 根)"

    rows = []
    for b in bars:
        try:
            c = float(b.get("close") or 0)
            if c <= 0:
                continue
            rows.append({
                "trade_time": pd.Timestamp(b.get("datetime")),
                "open": float(b.get("open")),
                "high": float(b.get("high")),
                "low": float(b.get("low")),
                "close": c,
                "volume": float(b.get("trade") or 0) * 100.0,
            })
        except Exception:
            continue
    if not rows:
        return None, f"通达信期货日K解析为空({main_code})"

    df = pd.DataFrame(rows)
    df = df.sort_values("trade_time").drop_duplicates(
        subset=["trade_time"], keep="last").reset_index(drop=True)
    return df, None


def _preflight():
    """请求前预检。返回 None 表示通过，否则返回**可直接展示给用户**的错误串。

    区分两种失败（「K线为空」对用户有误导——像是数据源坏了，实际多半是
    启动 M-Bull 的 Python 环境没装 pytdx，与数据源设置无关）：
      1. pytdx 未安装 → 给出明确安装指引
      2. 节点全不可达 → 说明会自动回退其他源
    """
    if _import_api_cls() is None:
        return ("运行环境未安装 pytdx：请在【启动 M-Bull 的那个 Python】里执行 "
                "pip install pytdx==1.72 然后重启工具（这与数据源设置无关）")
    if _ensure_api() is None:
        return ("通达信行情服务器全部不可达（7 节点连接失败）——"
                "将自动回退腾讯/东财/新浪；若持续失败请重扫可用节点")
    return None


def available():
    """源是否可用（pytdx 可导入 + 至少一台服务器能建连）。"""
    if _import_api_cls() is None:
        return False
    return _ensure_api() is not None


def stats():
    """返回诊断信息（供探测 / 故障排查）。"""
    api = getattr(_tls, "api", None)
    pool = tdx_nodes.status()
    return {
        "servers": pool.get("pool_size") or len(_SERVERS),
        "connected": api is not None,
        "server": getattr(_tls, "srv", None),
        "xdxr_cached": len(_xdxr_cache),
        "pool_source": pool.get("pool_source"),
        "pool_updated_at": pool.get("updated_at"),
        "probing": pool.get("probing"),
    }


# ---------------------------------------------------------------------------
# provider
# ---------------------------------------------------------------------------
class TdxSource(DataSource):
    """通达信：日K 前复权（第一手源）。

    ⛔ **必须委托 `KLineFetcher.fetch`，不能直接调 `fetch_daily_qfq`**。
    上层功能（全市场扫描 / 个股分析 / 自选诊断 / K线面板）走的都是
    `DataAPI.get_kline` → `registry.resolve('kline')` → **本方法**，
    **不经过** `_fetch_internal`。所以这里一旦裸抓，就会静默绕过：
      · `qfq_daily_*` 统一 store 的落盘（回测离线模式靠它命中）
      · 内存/磁盘缓存与 `incremental_merge` 增量合并
      · `_replace_today_bar_if_stale` 的盘中快照校正
    实测（`audit/_verify_tdx_upper_path.py`）：裸抓路径跑完 `cache/kline/` 是**空的**，
    一个 pkl 都不产生。与 `TencentSource.get_kline` 保持同构 —— 两者都委托
    `KLineFetcher.fetch`，由它按 `kline_source` 在内部二次分发到 tdx / 腾讯。
    """

    name = "通达信"

    def get_kline(self, stock_code, k_type, days):
        # 延迟 import：registry 由 data_layer 的 _reg() 惰性导入，顶层 import 会成环
        from engine.data_layer import KLineFetcher
        return KLineFetcher.fetch(stock_code, k_type, days)

    def get_realtime(self, code):
        from engine.data_sources.tdx import fetch_realtime_snapshot
        fields, _err = fetch_realtime_snapshot(code)
        return (fields, "通达信") if fields else (None, None)

    def get_kline_full(self, stock_code, k_type, days=300):
        """翻页拉全历史（首次建库场景，实测可达 6000+ 根）。

        同样走 `KLineFetcher._fetch_full` 的分发，不直连抓取函数 ——
        否则拿到的全历史不会写进 qfq_daily 共享 store。
        """
        from engine.data_layer import KLineFetcher
        return KLineFetcher._fetch_full(stock_code, k_type, days)
