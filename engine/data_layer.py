#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""数据层 v3.6 - 腾讯前复权优先"""
import os
import time
import logging
import pickle
import threading
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import pandas as pd
import numpy as np
from datetime import datetime
from engine.exceptions import DataSourceError, NetworkTimeoutError
from engine import net_proxy
from engine.state import (
    state, get_cache_key, get_cached, set_cache, K_TYPE_MAP, get_kline_disk_path,
    get_qfq_daily_disk_path, get_raw_daily_disk_path, MINUTE_K_TYPES,
)
from engine.config import C, STOCK_LIST_CACHE_FILE, CONFIG_DIR

logger = logging.getLogger(__name__)

_breadth_cache = {}
_breadth_cache_time = 0
_breadth_lock = threading.RLock()
# 市场情绪（涨停/跌停家数）缓存：复用 _breadth_lock，TTL 同涨跌家数。
# 涨停池接口稳定可用；跌停池接口若异常（rc!=0）则安全降级为 None。
_sentiment_cache = {}
_sentiment_cache_time = 0
# 统一前复权 store(qfq_daily_{code}.pkl) 的读写锁：扫描写、回测预载读，
# 合并写临界区需串行化避免半写/竞态。
_qfq_store_lock = threading.RLock()
# 新浪未复权 store(raw_daily_{code}.pkl / raw_weekly_{code}.pkl) 的读写锁。
# 与 _qfq_store_lock 分开：两个 store 语义不同、互不干涉，独立锁避免无谓串行。
_raw_store_lock = threading.RLock()

# ---------------------------------------------------------------------------
# 腾讯前复权 K 线「入口候选池」
# ---------------------------------------------------------------------------
# 2026-09-17 实测定案：web.ifzq.gtimg.cn 被腾讯 WAF **按 host 整站封禁**。
#   任何请求一律 HTTP 501 + 326B 挑战页（脚本跳 waf.tencent.com/501page.html?u=...），
#   与请求间隔(0.25/0.5/1.0s)、UA(浏览器/空)、Referer、http/https 全部无关
#   → 「降频躲 WAF」对这类封禁完全无效，只能换入口。
#
# DNS 对照（socket.getaddrinfo）揭示真相：
#   web.ifzq.gtimg.cn     -> 101.91.33.148/243                网页专用边缘节点，非浏览器流量全量拦
#   ifzq.gtimg.cn         -> 114.222.112.45/117.62.241.183
#   proxy.finance.qq.com  -> 114.222.112.45/117.62.241.183   （与上一行同一 IP 组）
# 去掉 `web.` 前缀的入口完全可用：实测连续 20 次 @0.25s 成功率 100%、
# 稳定 301 根前复权日K、中位 0.15s（比原入口还快）。
#
# 【第 2 层病因：按「IP + 入口」的请求量配额】—— 才是「拉几百条就不能用」的真因。
# 实测（audit/_probe_tencent_quota.py，2026-09-17 22:58）：
#   · 连续拉到**第 350 次**触发 501（前 349 次全成功，88 秒内打光）
#   · 撞墙后 300s 仍 501，6 分钟后仍未恢复 → 配额窗口在 10 分钟量级
#   · ⭐ 同一 IP 的另一个入口 proxy.finance.qq.com **仍可用** → 配额按
#     (IP + 入口) 分域，**不是**纯 IP 维度 → 换域名的候选池确实能续命
#   · 同 IP 的 qt.gtimg.cn 实时行情不受影响 → 配额只挂在 fqkline 这条路径
# 日志侧证（%LOCALAPPDATA%\M-Bull\logs\M-Bull_error.log）：真正 501 仅 53 次，
#   而「冷却中」刷了 6092 次（放大 115×），501 间隔精确 30s = 冷却到期又撞一次。
#
# ⚠️ 因此这是**物理配额，不是 bug**：单入口 350 次打不下 5200 只（需 5200 次请求，
#   且 fqkline 实测**不支持多代码批量**，请求数无法压缩）。
#   候选池的正确用法不是「撞墙才换」，而是**按配额主动均匀分摊**：两个入口各用
#   ~300 次 = 600 次/窗口且都不到撞墙点 → 全程无 501、无冷却循环。
#   要真正覆盖全市场仍需组合拳：多 IP（net_proxy 代理池）/ 多源（新浪独立配额）/
#   压缩请求数（当日增量可改走 qt.gtimg.cn 批量实时行情，100 只/次）。
# ⚠️ 两个可用入口路径前缀不同：proxy.finance.qq.com 必须带 `/ifzqgtimg`。
_TENCENT_FQ_ENDPOINTS = (
    'https://ifzq.gtimg.cn/appstock/app/fqkline/get',
    'https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get',
    'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get',
)

_FQ_SOURCE = 'tencent_kline'   # 候选池服务的 source 名（须与 HTTPClient 冷却 key 一致）
# RLock 而非 Lock：fq_endpoint_alternatives() 需在持锁状态下复用取优选逻辑，
# 用普通 Lock 会在单线程下自锁死（2026-09-17 实测踩到）。
_fq_pool_lock = threading.RLock()
_fq_pool_bad_until = {}        # endpoint -> 拉黑到期时间戳
_fq_pool_fails = {}            # endpoint -> 连续失败次数（软失败用）
_fq_pool_used = {}             # endpoint -> [本窗口已用请求数, 窗口起始 ts]（配额记账）
# endpoint -> [观测到的真实配额上限, 学到时刻]。**配额自适应**用：
# 腾讯限流强度会变（实测 08-28/29 可 4800+/小时无限流，9 月中旬收紧到 ~350/窗口），
# 硬编码 300 在对方收紧后会「每个入口白撞一次 501」→ 本窗口报废。
# 故从**真实撞墙事件**反推上限：撞墙时把当次已用次数记为观测值，下个窗口软限制取其 ×0.85。
# 观测值带时刻，超过 _FQ_LEARN_TTL_SEC 丢弃重学 —— 对方放宽后能自动恢复，不会永久保守。
_fq_pool_learned = {}
_FQ_BAD_THRESHOLD = 2          # 软失败（超时/连接错误）连续几次后拉黑
_FQ_LEARN_TTL_SEC = 7 * 86400  # 学到值的有效期（7 天）
_FQ_LEARN_FLOOR = 20           # 软限制下限，避免学到极小值后彻底不可用
# 撞墙时「已用次数」低于此值 → 判定为**入口本身异常**（host 封/RST）而非配额上限，不学习。
# 依据：真实配额上限实测在 350 量级；只用个位数就 501 一定是入口坏了。
_FQ_LEARN_MIN_OBSERVE = 50


def _fq_effective_quota(url):
    """（须在持锁上下文调用）该入口**本窗口的软限制**。

    优先「从真实撞墙中学到的上限 ×0.85」（留 15% 余量给多线程取号竞态）；
    没学到或观测值已过期 → 回退配置默认 `_FQ_QUOTA_PER_ENDPOINT`。
    """
    rec = _fq_pool_learned.get(url)
    if rec:
        learned, at = rec
        if learned > 0 and (time.time() - at) < _FQ_LEARN_TTL_SEC:
            return max(_FQ_LEARN_FLOOR, min(_FQ_QUOTA_PER_ENDPOINT, int(learned * 0.85)))
    return _FQ_QUOTA_PER_ENDPOINT

# 软/硬拉黑时长分开：
#  · 软失败（网络抖动）→ 短拉黑，很快能回来
#  · 硬失败（501 = 配额打光）→ 必须**长于配额恢复窗口**。实测序列：
#    300s 未恢复 → 6 分钟未恢复 → **23 分钟仍未恢复**（22:58 打光，23:23 测还是 501）。
#    旧版设 180s 会导致「拉黑到期 → 再撞 501 → 再拉黑」的死循环，
#    正是日志里 501 间隔精确 30s/60s 的成因。取 30 分钟留足余量。
_FQ_SOFT_BAD_SEC = 60
_FQ_BAD_SEC = 1800

# 单入口单窗口请求量上限：实测撞墙点 350 次，保守留量取 300
# （留 50 次余量给「多线程同时取号」的竞态，避免恰好踩线撞墙）。
_FQ_QUOTA_PER_ENDPOINT = 300
# 配额窗口：实测 ≥23 分钟，取 30 分钟与 _FQ_BAD_SEC 对齐。
# 窗口偏短会让 used 计数提前清零 → 程序以为配额回来了 → 再次撞墙。
_FQ_QUOTA_WINDOW_SEC = 1800


def fq_endpoint_is_tencent(source):
    """该 source 是否由候选池托管（决定 501 后「换入口」还是「冷却整源」）。"""
    return source == _FQ_SOURCE


# ── 配额状态持久化 ────────────────────────────────────────────────────────
# 为什么必须持久化：配额/拉黑是**服务端按 IP 记账**的，而 `_fq_pool_*` 是本进程
# 内存态。不落盘的话，用户关掉 M-Bull 再打开，程序会以为「配额满格」→ 重新
# 打 300 次 → 撞 501 → 又进冷却循环。落盘后重启即知「本窗口已用多少、哪些打光」。
_FQ_STATE_FILENAME = 'fq_quota_state.json'
_fq_state_path_cached = None
_fq_last_save_ts = 0.0


def _fq_state_path():
    global _fq_state_path_cached
    if _fq_state_path_cached is None:
        try:
            _fq_state_path_cached = os.path.join(CONFIG_DIR, _FQ_STATE_FILENAME)
        except Exception:      # pragma: no cover - 极端环境兜底
            _fq_state_path_cached = os.path.join(
                os.getcwd(), 'config', _FQ_STATE_FILENAME)
    return _fq_state_path_cached


def _fq_state_load():
    """进程启动时恢复配额/拉黑状态（只恢复仍在窗口内的记录）。"""
    import json
    path = _fq_state_path()
    try:
        if not os.path.isfile(path):
            return
        with open(path, 'r', encoding='utf-8') as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return
        now = time.time()
        eps = d.get('endpoints')
        if not isinstance(eps, dict):
            return
        restored = []
        with _fq_pool_lock:
            for u, rec in eps.items():
                if u not in _TENCENT_FQ_ENDPOINTS or not isinstance(rec, dict):
                    continue
                bu = float(rec.get('bad_until') or 0)
                if bu > now:
                    _fq_pool_bad_until[u] = bu
                ws = float(rec.get('window_start') or 0)
                used = int(rec.get('used') or 0)
                if ws and (now - ws) < _FQ_QUOTA_WINDOW_SEC and used > 0:
                    _fq_pool_used[u] = [used, ws]
                    if used >= _fq_effective_quota(u):
                        restored.append(u)
                # 恢复「自适应学到的真实上限」（未过期的才恢复）
                lr = rec.get('learned')
                if isinstance(lr, list) and len(lr) == 2:
                    lv, la = int(lr[0] or 0), float(lr[1] or 0)
                    if lv > 0 and (now - la) < _FQ_LEARN_TTL_SEC:
                        _fq_pool_learned[u] = [lv, la]
        if restored:
            logger.warning('腾讯K线配额状态已恢复：以下入口本窗口已打光 —— %s',
                           ', '.join(restored))
    except Exception as e:
        logger.warning('腾讯K线配额状态读取失败（忽略，按满配额处理）: %s', e)


def _fq_state_save():
    """（须已持 _fq_pool_lock）把仍在窗口内的状态落盘。"""
    import json
    global _fq_last_save_ts
    try:
        now = time.time()
        payload = {'saved_at': now, 'endpoints': {}}
        for u in _TENCENT_FQ_ENDPOINTS:
            bu = _fq_pool_bad_until.get(u, 0)
            rec = _fq_pool_used.get(u)
            lr = _fq_pool_learned.get(u)
            in_window = bool(rec) and (now - rec[1]) < _FQ_QUOTA_WINDOW_SEC and rec[0] > 0
            learn_ok = bool(lr) and (now - lr[1]) < _FQ_LEARN_TTL_SEC
            if bu > now or in_window or learn_ok:
                payload['endpoints'][u] = {
                    'bad_until': bu,
                    'used': rec[0] if rec else 0,
                    'window_start': rec[1] if rec else now,
                    'learned': lr if learn_ok else None,   # [观测上限, 学到时刻]
                }
        path = _fq_state_path()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)          # 原子替换，避免半写
        _fq_last_save_ts = now
    except Exception as e:
        logger.debug('腾讯K线配额状态写入失败（忽略）: %s', e)


def _fq_used_locked(url):
    """（持锁）该入口在**当前窗口**内的已用请求数；跨窗口自动清零。"""
    now = time.time()
    rec = _fq_pool_used.get(url)
    if not rec or now - rec[1] >= _FQ_QUOTA_WINDOW_SEC:
        _fq_pool_used[url] = [0, now]
        return 0
    return rec[0]


def _fq_available_locked():
    """（持锁）可用入口列表：未被拉黑 且 本窗口配额未打光，**按已用次数升序**。

    升序 = 均匀分摊配额，这是本模块的核心。不是「撞墙才换」（那样第一个入口
    必然被打光并长时间不可用，后续几百只票全废），而是从第一次请求起就让
    各入口轮流用、各自停在软限制以下 → 全程不触发 501、不进入冷却循环。
    软限制由 `_fq_effective_quota()` 给出（**会随真实撞墙事件自适应下调**）。
    """
    now = time.time()
    cand = []
    for u in _TENCENT_FQ_ENDPOINTS:
        if _fq_pool_bad_until.get(u, 0) > now:
            continue
        used = _fq_used_locked(u)
        if used >= _fq_effective_quota(u):
            continue
        cand.append((used, u))
    cand.sort()
    return [u for _, u in cand]


def _fq_endpoint_locked():
    """（调用方须已持 _fq_pool_lock）取当前最该用的入口；全部不可用时 None。"""
    avail = _fq_available_locked()
    return avail[0] if avail else None


def fq_endpoint():
    """取当前最该用的入口 URL。全部不可用（拉黑中 / 配额打光）时返回 None。"""
    with _fq_pool_lock:
        return _fq_endpoint_locked()


def fq_endpoint_note_used(url):
    """记一次配额消耗。调用方在**每次真正发请求前**调用。"""
    if url not in _TENCENT_FQ_ENDPOINTS:
        return
    with _fq_pool_lock:
        used = _fq_used_locked(url)
        _fq_pool_used[url][0] = used + 1
        total = sum(r[0] for r in _fq_pool_used.values())
        # 节流落盘：每 20 次或每 15 秒写一次，避免每请求一次磁盘 IO
        if total % 20 == 0 or (time.time() - _fq_last_save_ts) > 15:
            _fq_state_save()


def fq_endpoint_quota_exhausted():
    """所有入口是否都已因配额打光/拉黑而不可用（供上层给出明确提示）。"""
    with _fq_pool_lock:
        return not _fq_available_locked()


def fq_endpoint_available_count():
    """仍有几个可用入口（未被拉黑 且 配额未打光）。

    ⚠️ 语义是「可用入口**总数**」，不是「除首选外的备选数」。冷却整源的正确判据是
    「一个可用的都没有」。候选池改成按配额分摊后，`fq_endpoint()` 只是取余量最多的
    那一个，其余 n-1 个同样是活路；若按 n-1 判断，则池里只剩**最后一个**可用入口时
    会被误判为「无路可走」而冷却整源，把最后一根救命稻草也掐掉
    （2026-09-17 由 audit/_verify_tencent_endpoint_pool.py 场景2 抓出）。
    """
    with _fq_pool_lock:
        return len(_fq_available_locked())


def fq_endpoint_mark_ok(url):
    """成功：清软失败计数。

    刻意**不动** `_fq_pool_used`（配额是消耗品，成功不代表配额回来了），也
    **不解封** `bad_until`（拉黑中的入口根本不会被选中，能走到这里的只可能是
    软失败计数；提前解封反而会让打光配额的入口跑去撞墙）。
    """
    if url not in _TENCENT_FQ_ENDPOINTS:
        return
    with _fq_pool_lock:
        _fq_pool_fails.pop(url, None)


def fq_endpoint_mark_bad(url, hard=False):
    """失败记账。hard=True（501 / 拦截页 / 空响应 = 配额打光或 WAF）→ 长拉黑；
    否则累计软失败，达阈值后短拉黑。"""
    if url not in _TENCENT_FQ_ENDPOINTS:
        return
    with _fq_pool_lock:
        now = time.time()
        if hard:
            _fq_pool_bad_until[url] = now + _FQ_BAD_SEC
            _fq_pool_fails.pop(url, None)
            # ⭐ 配额自适应：本次撞墙时该入口已用了多少次，就是真实上限的**观测值**。
            #    取下界（学过的更小值优先），供下个窗口把软限制压下来 ——
            #    这样对方收紧后只需「白撞一次」就能自动适配，不必改代码。
            used_now = _fq_used_locked(url)
            # ⚠️ 只有「已经用掉相当次数」才说明是真撞到配额上限。若只用了几次就 501，
            #    那是**入口本身异常**（host 被封 / 网关 RST，如 web.ifzq.gtimg.cn），
            #    这类情况应由 bad_until 处理，**绝不能**把它当配额上限学进来 ——
            #    否则会把一个坏入口的正常配额误压到极小值（2026-09-18 由验证用例暴露）。
            if used_now >= _FQ_LEARN_MIN_OBSERVE:
                prev = _fq_pool_learned.get(url)
                learned = used_now if not prev else min(prev[0], used_now)
                _fq_pool_learned[url] = [learned, now]
                if not prev or learned < prev[0]:
                    logger.warning('腾讯K线配额自适应：观测到 %s 的真实上限 ≈ %d 次/窗口，'
                                   '下窗口软限制将按 %d 执行',
                                   url, learned, max(_FQ_LEARN_FLOOR,
                                                     min(_FQ_QUOTA_PER_ENDPOINT, int(learned * 0.85))))
            elif used_now > 0:
                logger.info('腾讯K线入口 %s 仅用 %d 次即被拒（< %d），判定为入口异常而非'
                            '配额上限，不参与自适应（已按 bad_until 拉黑）',
                            url, used_now, _FQ_LEARN_MIN_OBSERVE)
            # 配额记账补满：让「该入口本窗口不可用」立刻体现在可用性判断上
            rec = _fq_pool_used.get(url) or [0, now]
            _fq_pool_used[url] = [max(rec[0], _fq_effective_quota(url)), rec[1]]
        else:
            n = _fq_pool_fails.get(url, 0) + 1
            if n >= _FQ_BAD_THRESHOLD:
                _fq_pool_bad_until[url] = now + _FQ_SOFT_BAD_SEC
                _fq_pool_fails.pop(url, None)
            else:
                _fq_pool_fails[url] = n
        _fq_state_save()          # 立刻落盘：重启后才知道这个入口已打光
        n_avail = len(_fq_available_locked())
        logger.warning('腾讯K线入口%s(%s)，剩余可用 %d 个%s',
                       '硬拉黑-配额打光/WAF' if hard else '软失败计数', url, n_avail,
                       '' if n_avail else ' —— 全部不可用：本窗口腾讯K线已无路可用')


def fq_endpoint_status():
    """诊断快照：入口池实时状态（供健康检查 / 故障排查用）。"""
    with _fq_pool_lock:
        now = time.time()
        return {
            'quota_per_endpoint': _FQ_QUOTA_PER_ENDPOINT,
            'quota_window_sec': _FQ_QUOTA_WINDOW_SEC,
            'current': _fq_endpoint_locked(),
            'exhausted': not _fq_available_locked(),
            'endpoints': [{
                'url': u,
                'alive': _fq_pool_bad_until.get(u, 0) <= now,
                'blacklisted_sec': max(0, int(_fq_pool_bad_until.get(u, 0) - now)),
                'soft_fails': _fq_pool_fails.get(u, 0),
                'quota_used': _fq_used_locked(u),
                # 软限制（自适应后可能低于配置默认值）与仍可用余量
                'quota_limit': _fq_effective_quota(u),
                'quota_learned': (_fq_pool_learned.get(u) or [None])[0],
                'quota_left': max(0, _fq_effective_quota(u) - _fq_used_locked(u)),
            } for u in _TENCENT_FQ_ENDPOINTS],
        }


# 进程启动即恢复配额/拉黑状态：必须在任何 fq_endpoint() 调用之前执行，
# 否则重启后又按「满配额」去打，直接再撞一轮 501。
_fq_state_load()


# ---------------------------------------------------------------------------
# 批量实时行情池：把「每日增量」从 5200 次请求压到 ~65 次
# ---------------------------------------------------------------------------
# 背景（2026-09-17 实测定案）：腾讯 fqkline 拉增量是 **1 请求/只**，全市场 5200 只
# = 5200 次请求；而单入口配额实测仅 ~350 次/窗口（窗口 ≥23 分钟）→ 必然撞墙，
# 这正是「扫描一次要 2.5 小时 / 拉到几百条就废」的根因。
#
# 而 `qt.gtimg.cn/q=` 支持**批量实时行情**（实测上限 86~100 只/次，取 80）：
# 全市场 ≈ 65 次请求，且它在 fqkline 撞墙时**仍然 200**（独立配额）。实测 0.13s/次。
#
# 为什么当日 bar 可以这样拿（已实测 6/6 完全一致）：
#   前复权的锚点是「最新一天」，所以前复权序列的**最后一根就是原始价** ——
#   当日 bar 无需任何复权折算；成交量单位亦与 fqkline 一致（实测量比恒 1）。
#
# ⚠️ 适用条件（严格，宁缺毋滥）：
#   `old 缓存末尾那根的收盘 == 实时行情的「昨收」[4]`
#     相等 ⇒ 缓存只落后 **1 个交易日** 且期间**无除权**
#            （有除权则昨收已被调整，必不等）⇒ 尾巴重叠日复权比 r 恒为 1，拼接安全。
#     不等 ⇒ 落后多日（中间那根实时行情拿不到）或已除权 ⇒ **回退逐只 fqkline**。
# ⚠️ 只在 15:00 后启用：盘中当日 bar 未终值，写进缓存会污染前复权序列。
_RT_BATCH_URL = 'http://qt.gtimg.cn/q='
_RT_BATCH_SIZE = 80          # 实测 86~100 为上限，取 80 留余量
_RT_BATCH_WORKERS = 8        # 并发批数：65 批串行 23s → 8 并发约 4s
_RT_POOL_TTL = 600           # 池有效期（秒）：一次扫描内复用，避免重复拉全市场
_RT_POOL_MIN_CLOSE = datetime(2000, 1, 1, 15, 0).time()   # 仅在收盘后启用


class RealtimeBarPool:
    """批量实时行情池（供「当日 bar 增量」用）。

    首次取用时构建（拉全市场），TTL 内复用；构建失败一律返回 None，
    调用方回退到原有的逐只 fqkline 路径 —— **批量是加速，不是依赖**。
    """

    _lock = threading.RLock()
    _bars = {}            # code -> dict(open/high/low/close/volume/trade_time/prev_close)
    _built_at = 0.0
    _build_fails = 0      # 连续构建失败计数（失败 3 次后本次进程不再尝试，免反复卡顿）

    @staticmethod
    def _all_codes():
        """全市场代码：**直接读本地股票列表缓存文件，不联网**。

        刻意不走 `StockListLoader.load()` —— 它返回的是**条数(int)** 而非映射
        （数据经 `_apply_and_cache` 落到缓存文件），且缓存失效时会去联网拉 52 页新浪，
        在扫描热路径里不可接受。这里只读文件、且数量不足就放弃批量（回退逐只）。
        """
        import json
        try:
            if not os.path.isfile(STOCK_LIST_CACHE_FILE):
                return []
            with open(STOCK_LIST_CACHE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            codes = data.get('code_to_name') if isinstance(data, dict) else None
            if not codes or len(codes) < MIN_STOCK_COUNT:
                logger.warning('批量实时行情：股票列表缓存缺失/不足(%s)，回退逐只增量',
                               len(codes) if codes else 0)
                return []
            return [c for c in codes if c]
        except Exception as e:
            logger.warning('批量实时行情：读股票列表缓存失败，回退逐只: %s', e)
            return []

    @staticmethod
    def _parse_batch(text):
        """解析批量响应 → {code: bar}。字段位：[3]现价 [4]昨收 [5]今开 [6]量(手)
        [30]时间 [33]最高 [34]最低（2026-09-17 实测确认，已与 qfq 缓存核对一致）。"""
        out = {}
        for blk in (text or '').split(';'):
            if '=' not in blk or '~' not in blk:
                continue
            head, _, body = blk.partition('=')
            code = head.strip().replace('v_', '')
            parts = body.strip().strip('"').split('~')
            if len(parts) < 35:
                continue
            try:
                close = float(parts[3])
                prev_close = float(parts[4])
                open_ = float(parts[5])
                volume = float(parts[6])
                high = float(parts[33])
                low = float(parts[34])
                ts = parts[30].strip()
            except (ValueError, IndexError):
                continue
            # 停牌/未上市/无成交：现价或昨收或量为 0 的一律丢弃（宁缺毋滥）
            if close <= 0 or prev_close <= 0 or high <= 0 or low <= 0 or volume <= 0:
                continue
            if len(ts) < 8:
                continue
            try:
                trade_time = pd.Timestamp(f'{ts[0:4]}-{ts[4:6]}-{ts[6:8]}')
            except Exception:
                continue
            out[code] = {
                'trade_time': trade_time, 'open': open_, 'close': close,
                'high': high, 'low': low, 'volume': volume,
                'prev_close': prev_close,
            }
        return out

    @staticmethod
    def _fetch_batch(batch):
        """拉取单批实时行情 → {code: bar}。

        刻意**不走 HTTPClient**：它给 `tencent_realtime` 挂的 per-source 串行锁+
        0.15s 间隔会把 65 批串成 23s（实测）。批量池是独立的一次性预热，直接并发
        抓取即可（实测 8 线程并发把 23s 压到 ~4s）；失败时由上层回退逐只，
        故这里不接代理池/冷却逻辑，只保证"要么快、要么放弃"。
        """
        resp = requests.get(_RT_BATCH_URL + ','.join(batch), headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://gu.qq.com/',
        }, timeout=10)
        if resp.status_code != 200:
            raise DataSourceError(f'批量实时行情 HTTP {resp.status_code}')
        resp.encoding = 'gbk'
        return RealtimeBarPool._parse_batch(resp.text)

    @classmethod
    def _fetch_all(cls):
        """分批**并发**拉全市场实时行情 → (bars, ok_batches, n_batches)。

        覆盖率过低视为源异常（可能被限），返回 (None, ...) —— 不用半份池去拼（宁可回退逐只）。
        """
        codes = cls._all_codes()
        if not codes:
            return None, 0, 0
        batches = [codes[i:i + _RT_BATCH_SIZE] for i in range(0, len(codes), _RT_BATCH_SIZE)]
        bars = {}
        ok_batches = 0
        with ThreadPoolExecutor(max_workers=_RT_BATCH_WORKERS) as ex:
            futures = [ex.submit(cls._fetch_batch, b) for b in batches]
            for fut in as_completed(futures):
                try:
                    bars.update(fut.result())
                    ok_batches += 1
                except Exception as e:
                    logger.debug('批量实时行情分批失败: %s', e)
        if ok_batches == 0 or len(bars) < len(codes) * 0.5:
            logger.warning('批量实时行情池覆盖率不足(%d/%d 只，%d/%d 批)，回退逐只增量',
                           len(bars), len(codes), ok_batches, len(batches))
            return None, ok_batches, len(batches)
        return bars, ok_batches, len(batches)

    @classmethod
    def _build(cls):
        """构建池：分批**并发**拉全市场实时行情。失败返回 False。"""
        bars, ok_batches, n_batches = cls._fetch_all()
        if bars is None:
            return False
        with cls._lock:
            cls._bars = bars
            cls._built_at = time.time()
        logger.info('批量实时行情池已构建：%d 只（%d/%d 批成功，%d 次请求）',
                    len(bars), ok_batches, n_batches, n_batches)
        return True

    # ── 盘中快照池（供 `_patch_intraday_bar` 修补「末根 K 线」，**绝不写缓存**）──
    # 与收盘池（_bars）隔离：盘中 bar 未终值，混入会污染 qfq 序列。
    # 背景（2026-10-05）：逐只实时行情在 WAF 整源冷却期会整批快速失败 → 那部分股票的
    # 末根停在昨日 → 触发价/扫描价错成昨收（用户看到的「部分股票错行」）。批量池全市场
    # 仅 ~65 次请求，一次扫描构建一次即可，彻底绕开逐只冷却。
    _live_lock = threading.RLock()
    _live_build_lock = threading.Lock()
    _live_bars = {}
    _live_built_at = 0.0
    _live_next_try = 0.0
    _live_build_fails = 0
    _LIVE_TTL = 120          # 盘中池有效期（秒）：过期后惰性重建，保证现价新鲜
    _LIVE_RETRY_GAP = 60     # 构建失败后的重试间隔（秒）

    @classmethod
    def build_live(cls):
        """构建/刷新盘中快照池（全市场批量）。成功返回 True。

        串行化：并发调用排队；先到的构建，后到的直接复用刚刷新的池（不会重复拉全市场）。
        """
        with cls._live_build_lock:
            with cls._live_lock:
                if cls._live_bars and (time.time() - cls._live_built_at) < cls._LIVE_TTL:
                    return True                     # 已被并发调用刷新
            bars, _ok, _n = cls._fetch_all()
            with cls._live_lock:
                if bars is None:
                    cls._live_build_fails += 1
                    cls._live_next_try = time.time() + cls._LIVE_RETRY_GAP
                    return False
                cls._live_bars = bars
                cls._live_built_at = time.time()
                cls._live_build_fails = 0
            logger.info('盘中实时快照池已构建：%d 只', len(bars))
            return True

    @classmethod
    def get_live(cls, code):
        """取盘中快照 bar；不可用返回 None（调用方回退逐只实时行情）。

        仅当本进程曾构建过盘中池（即跑过扫描）时才惰性重建 —— 纯单股分析不为此
        付出全市场 65 次请求的代价。
        """
        now = time.time()
        with cls._live_lock:
            if cls._live_bars and (now - cls._live_built_at) < cls._LIVE_TTL:
                return cls._live_bars.get(code)
            _ever_built = cls._live_built_at > 0
            _ready = now >= cls._live_next_try
            _give_up = cls._live_build_fails >= 3
        if not (_ever_built and _ready) or _give_up:
            return None
        if not cls.build_live():
            return None
        with cls._live_lock:
            return cls._live_bars.get(code)

    @classmethod
    def get(cls, code):
        """取该 code 的当日 bar；不可用返回 None（调用方回退逐只）。

        仅在 15:00 后启用 —— 盘中当日 bar 未终值。
        """
        if datetime.now().time() < _RT_POOL_MIN_CLOSE:
            return None
        if cls._build_fails >= 3:
            return None
        with cls._lock:
            fresh = (time.time() - cls._built_at) < _RT_POOL_TTL and cls._bars
            if fresh:
                return cls._bars.get(code)
        # 未构建/已过期 → 构建（首次会有 ~10s 阻塞，换来整轮扫描不再逐只联网）
        try:
            built = cls._build()
        except Exception as e:
            logger.warning('批量实时行情池构建异常，回退逐只: %s', e)
            built = False
        if not built:
            with cls._lock:
                cls._build_fails += 1
            logger.warning('批量实时行情池构建失败(%d 次)，本次进程回退逐只增量', cls._build_fails)
            return None
        with cls._lock:
            return cls._bars.get(code)

    @classmethod
    def reset(cls):
        with cls._lock:
            cls._bars = {}
            cls._built_at = 0.0
            cls._build_fails = 0
        with cls._live_lock:
            cls._live_bars = {}
            cls._live_built_at = 0.0
            cls._live_next_try = 0.0
            cls._live_build_fails = 0


# 全市场扫描日线抓取天数；回测复用其缓存（cache/kline/qfq_daily_{code}.pkl），
# 个股取数上限对齐此值：仅扫描缓存、不在线补抓。改这里即可同步扫描与回测窗口。
SCAN_KLINE_DAYS = 300
BREADTH_CACHE_TTL = 30

# 沪深A股全市场合理下限（主板+科创板+创业板）。
# 低于此值视为缓存残缺/接口返回不全，强制走网络重拉，避免残缺缓存被"冻结"。
#
# 2026-09-21 实盘事故：新浪分页按 symbol 升序返回（bj920xxx → sh6x → sz0x → sz3x，
# **创业板永远压在最后一页**），拉到 sz30 中途断掉只得 4356 只（创业板 543/1407）；
# 旧门槛 4000 被这一步越过 → 残缺列表被判「有效」→ 落盘并刷新 saved_at →
# `_cache_stale(1)` 一天内不再重拉 → **残缺池被冻结一整天**，扫描少 864 只创业板。
# 故抬到 5100（实测 5220），并补一道**板块级下限**：只卡总量会在「末段被砍但总量仍够」
# 时漏判，逐段校验才能精准挡住分页截断。
MIN_STOCK_COUNT = 5100
MIN_STOCK_COUNT_BY_BOARD = {
    'sh60': 1600,   # 沪市主板（实测 1702）
    'sh68': 550,    # 科创板（实测 617）
    'sz00': 1400,   # 深市主板（实测 1494）
    'sz30': 1300,   # 创业板（实测 1407）—— 分页断尾必先砍这里
}

class StockListLoader:
    @staticmethod
    def _fetch_from_sina(page, num=100):
        """从新浪API获取单页股票列表，返回list或None"""
        url = 'http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData'
        params = {
            'page': str(page), 'num': str(num), 'sort': 'symbol',
            'asc': '1', 'node': 'hs_a', 'symbol': '', '_s_r_a': 'auto'
        }
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://finance.sina.com.cn/'
        }
        try:
            # 股票池要连续翻 ~52 页（每页 100 条），是最容易触发「同 IP 高频」限流的点，
            # 单独接代理池：没配代理时为 None，行为与改造前一致
            resp = requests.get(url, params=params, headers=headers, timeout=10,
                                proxies=net_proxy.proxies_for('stocklist'))
        except Exception as e:
            logger.warning(f"新浪API第{page}页请求异常: {e}")
            return None
        if resp.status_code != 200:
            logger.warning(f"新浪API返回状态码 {resp.status_code}")
            return None
        text = resp.text.strip()
        if not text or text[0] not in '[{':
            logger.warning("新浪API返回非JSON内容")
            return None
        try:
            return resp.json()
        except Exception:
            logger.warning("新浪API返回JSON解析失败")
            return None

    @staticmethod
    def _save_cache(name_to_code, code_to_name, raw_code_to_name):
        """保存股票列表到本地缓存文件"""
        try:
            import json
            cache_data = {
                'name_to_code': name_to_code,
                'code_to_name': code_to_name,
                'raw_code_to_name': raw_code_to_name,
                'saved_at': datetime.now().isoformat()
            }
            with open(STOCK_LIST_CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, ensure_ascii=False)
            logger.info(f"股票列表已缓存到本地: {len(name_to_code)}只")
        except Exception as e:
            logger.warning(f"缓存股票列表失败: {e}")

    @staticmethod
    def _load_cache():
        """从本地缓存文件加载股票列表，返回(count)或0"""
        import json
        try:
            with open(STOCK_LIST_CACHE_FILE, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)
            state.name_to_code = cache_data['name_to_code']
            state.code_to_name = cache_data['code_to_name']
            state.raw_code_to_name = cache_data['raw_code_to_name']
            count = len(state.name_to_code)
            logger.info(f"从本地缓存加载了{count}只股票")
            return count
        except Exception as e:
            logger.warning(f"加载本地股票列表缓存失败: {e}")
            return 0

    @staticmethod
    def _fetch_from_sina_all():
        """新浪全量分页抓取，返回 (name_to_code, code_to_name, raw_code_to_name) 或 None

        ⛔ **中途失败必须整轮作废**：旧实现某页异常只 `break`，末尾再以
        `if api_ok and name_to_code: return ...` 把「只拉到一半」当成成功返回 ——
        2026-09-21 实盘即由此产出 4356 只的残缺列表（创业板 543/1407），又因门槛过松
        被判「有效」落盘并冻结。现在改为：拿不全就 `return None`，把「定夺」交给上层
        `load()` 的重试/换源逻辑，绝不把半截列表交出去。
        """
        name_to_code, code_to_name, raw_code_to_name = {}, {}, {}
        # 北交所 bj92xxxx 也占分页配额（实测总量 5564 → 约 56 页），上限留足余量
        for page in range(1, 70):
            data = StockListLoader._fetch_from_sina(page)
            if data is None:
                logger.warning(f"新浪股票列表第{page}页请求失败 → 本轮作废，交上层重试/换源")
                return None
            if not data:
                break  # 空页 = 正常末页
            for item in data:
                raw_code = str(item.get('code', '')).strip()
                raw_name = str(item.get('name', '')).strip()
                raw_code_to_name[raw_code] = raw_name
                if raw_code.isdigit() and len(raw_code) == 6:
                    if raw_code.startswith(('6', '5')):
                        sina_code = f"sh{raw_code}"
                    elif raw_code.startswith(('0', '3')):
                        sina_code = f"sz{raw_code}"
                    else:
                        continue
                    name_to_code[raw_name] = sina_code
                    code_to_name[sina_code] = raw_name
            if len(data) < 100:
                break  # 不足一页 = 正常末页
            time.sleep(0.2)
        if name_to_code:
            ok, detail = StockListLoader._pool_integrity(code_to_name)
            if not ok:
                logger.warning(f"新浪股票列表不完整（{detail}）→ 本轮作废，交上层重试/换源")
                return None
            return name_to_code, code_to_name, raw_code_to_name
        return None

    @staticmethod
    def _fetch_from_eastmoney():
        """东方财富全市场A股列表（新浪备用源，抗单点故障），分页拉全，返回三元组或 None"""
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        fs = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"  # 沪市主板+科创 / 深市主板+创业
        name_to_code, code_to_name, raw_code_to_name = {}, {}, {}
        for pn in range(1, 12):  # 最多 11 页 * 5000，远超实际规模
            params = {"pn": str(pn), "pz": "5000", "po": "1", "np": "1",
                      "fltt": "2", "invt": "2", "fid": "f3", "fs": fs,
                      "fields": "f12,f14"}
            headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
            ok = False
            for _r in range(2):  # 单页请求重试
                try:
                    resp = requests.get(url, params=params, headers=headers, timeout=15)
                    ok = True
                    break
                except Exception as e:
                    logger.warning(f"东方财富第{pn}页请求异常(重试{_r + 1}): {e}")
            if not ok:
                break
            if resp.status_code != 200:
                logger.warning(f"东方财富返回状态码 {resp.status_code}")
                break
            try:
                data = resp.json()
            except Exception:
                logger.warning("东方财富返回JSON解析失败")
                break
            diff = (data.get("data") or {}).get("diff") or []
            if not diff:
                break
            for it in diff:
                raw_code = str(it.get("f12", "")).strip()
                raw_name = str(it.get("f14", "")).strip()
                if not raw_code or not raw_name:
                    continue
                raw_code_to_name[raw_code] = raw_name
                if raw_code.isdigit() and len(raw_code) == 6:
                    if raw_code.startswith(('6', '5')):
                        sina_code = f"sh{raw_code}"
                    elif raw_code.startswith(('0', '3')):
                        sina_code = f"sz{raw_code}"
                    else:
                        continue
                    name_to_code[raw_name] = sina_code
                    code_to_name[sina_code] = raw_name
            if len(diff) < 5000:
                break  # 已到末页
        if name_to_code:
            return name_to_code, code_to_name, raw_code_to_name
        return None

    @staticmethod
    def _apply_and_cache(name_to_code, code_to_name, raw_code_to_name, source):
        """把解析结果写入 state 并落盘缓存，返回数量"""
        state.name_to_code = name_to_code
        state.code_to_name = code_to_name
        state.raw_code_to_name = raw_code_to_name
        StockListLoader._save_cache(name_to_code, code_to_name, raw_code_to_name)
        logger.info(f"从{source}加载了{len(name_to_code)}只股票")
        return len(name_to_code)

    @staticmethod
    def _board_stats(code_to_name):
        """按板块统计数量：key 形如 'sh600000'/'sz300001' → {'sh60': 1702, 'sz30': 1407, ...}"""
        stats = {}
        for code in (code_to_name or {}):
            board = str(code)[:4]
            stats[board] = stats.get(board, 0) + 1
        return stats

    @staticmethod
    def _pool_integrity(code_to_name):
        """校验股票池完整性，返回 ``(ok, detail)``。缓存复用与网络拉取共用本判据。

        两道闸缺一不可：
          ① 总量 ≥ MIN_STOCK_COUNT；
          ② 逐板块 ≥ MIN_STOCK_COUNT_BY_BOARD。

        新浪分页按 symbol 升序（bj920xxx → sh6x → sz0x → sz3x），**断尾必然发生在末段
        创业板**；只卡总量会在「末段被砍掉大半、总量却仍过线」时漏判 —— 2026-09-21 实盘
        正是 4356 只（创业板只剩 543/1407）恰好越过旧的 4000 单闸，随后被判「有效」落盘、
        刷新 saved_at，残缺池被冻结一整天。
        """
        total = len(code_to_name or {})
        if total < MIN_STOCK_COUNT:
            return False, f"总量 {total} < {MIN_STOCK_COUNT}"
        stats = StockListLoader._board_stats(code_to_name)
        bad = [f"{b}={stats.get(b, 0)}/{floor}"
               for b, floor in MIN_STOCK_COUNT_BY_BOARD.items()
               if stats.get(b, 0) < floor]
        if bad:
            return False, "板块残缺 " + ", ".join(bad)
        return True, f"总量 {total}，各板块齐备"

    @staticmethod
    def load():
        # 1) 缓存优先：有效（**总量与板块都达标** 且 较新≤1天）则直接用，启动飞快，
        #    避免离线/限流拖慢主工具。
        #    关键修复：缓存数量低于 MIN_STOCK_COUNT（如残缺的670只）即便"较新"也视为无效，
        #    强制重拉，解决"残缺缓存被冻结、UI 显示已加载 670 只却不自动更新"的问题。
        #    2026-09-21 扩到板块级：4356 只（创业板仅 543/1407）越过旧的 4000 单闸被冻结，
        #    故此处与网络拉取共用 _pool_integrity（总量 + 逐板块双闸）。
        cached = StockListLoader._load_cache()
        if cached >= MIN_STOCK_COUNT and not StockListLoader._cache_stale(1):
            ok, detail = StockListLoader._pool_integrity(state.code_to_name)
            if ok:
                logger.info(f"股票列表使用本地缓存（有效，{detail}），跳过网络拉取")
                return cached
            logger.warning(f"本地股票列表完整性不足（{detail}），强制重新拉取")

        # 2) 主源：新浪（带 3 次重试）
        for attempt in range(3):
            triple = StockListLoader._fetch_from_sina_all()
            if triple:
                return StockListLoader._apply_and_cache(*triple, "新浪")
            if attempt < 2:
                logger.info(f"新浪加载失败/数据不足，{0.5 * (attempt + 1):.1f}秒后重试({attempt + 2}/3)")
                time.sleep(0.5 * (attempt + 1))

        # 3) 备用源：东方财富（新浪单点故障兜底，抗 WAF/限流）
        logger.warning("新浪源不可用，尝试东方财富备用源")
        triple = StockListLoader._fetch_from_eastmoney()
        if triple:
            ok, detail = StockListLoader._pool_integrity(triple[1])
            if ok:
                return StockListLoader._apply_and_cache(*triple, "东方财富")
            logger.warning(f"东方财富返回股票列表不完整（{detail}）")

        # 4) 兜底：回退本地缓存（即便较旧/较小，尽力保证可用，UI 会提示数量异常）
        if cached > 0:
            logger.warning(f"全部 API 失败，回退本地缓存（{cached}只，列表可能不全）")
            return cached
        logger.warning("无法加载股票列表，程序将以无列表模式启动（名称搜索不可用）")
        return 0

    @staticmethod
    def _cache_stale(max_age_days=1):
        """股票列表缓存是否过期：文件缺失 / saved_at 早于 max_age_days 天 → 视为过期。"""
        import json
        if not os.path.exists(STOCK_LIST_CACHE_FILE):
            return True
        try:
            with open(STOCK_LIST_CACHE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            saved_at = data.get('saved_at')
            if not saved_at:
                return True
            saved = datetime.fromisoformat(saved_at)
            return (datetime.now() - saved).total_seconds() > max_age_days * 86400
        except Exception:
            return True

class HTTPClient:
    # Per-source 速率限制：各数据源独立最小请求间隔(秒)，互不影响。
    #  - 批量 K 线冷扫 / 回测缺数据预加载(走 tencent_kline)：刻意降频(~0.25s)躲 WAF，
    #    2026-09-12 从 0.5s 收紧：缓存不新鲜时全市场 5200+ 只逐只发增量请求，
    #    0.5s 间隔串行需 40-78 分钟体感超时；0.25s+jitter 约减半。
    #  - 实时行情 / 指数 / 股票列表：单只小批量，用短间隔保持响应快、不被冷扫阻塞。
    _source_min_interval = {
        'tencent_kline': 0.25,    # 腾讯前复权K线：日K 回退通道 + 回测补抓(_fetch_tencent_fq)
        'sina_realtime': 0.15,    # 实时行情 新浪 sinajs
        'tencent_realtime': 0.15, # 实时行情 腾讯 qt
        'tencent_index': 0.2,     # 指数 腾讯 qt
        'eastmoney_index': 0.2,   # 指数 东方财富
        'stocklist': 0.2,         # 股票列表
    }
    _default_min_interval = 0.3   # 未标注 source 的旧调用兜底
    # 各源独立锁 + 独立时间戳：批量冷扫推进自己的时间戳，不阻塞实时行情等其它源
    _source_locks = {}
    _source_last_request = {}
    # per-source 冷却：被 WAF/限流的单个源在冷却期内直接快速失败，不影响其它源
    _source_cooldown = {}  # source(str) -> 冷却到期时间戳(float)
    _waf_detected = False  # 仅作诊断标记，不再用于阻塞请求

    @staticmethod
    def _lock_for(source):
        lock = HTTPClient._source_locks.get(source)
        if lock is None:
            lock = threading.Lock()
            HTTPClient._source_locks[source] = lock
        return lock

    @staticmethod
    def _mark_cooldown(source, base=30):
        """标记某源进入冷却期（批量 K 线用 30s，实时/指数/列表用 10s 更温和）。"""
        if not source:
            return
        cd = 10 if ('realtime' in source or 'index' in source or 'stocklist' in source) else base
        HTTPClient._source_cooldown[source] = time.time() + cd

    @staticmethod
    def _should_cooldown(source):
        """是否该把**整源**冷却。

        冷却整源 = 该源后续所有请求在冷却期内快速失败。只有「真的无路可走」时才该这么做：
          - 配了代理池且还有可用出口 IP   → 不冷却，换 IP 重试（net_proxy 负责）
          - 候选池托管的源**还有任何一个**可用入口 → 不冷却，换入口重试
        否则（例如新浪单入口源）才冷却。

        2026-09-17 修两处：
          ① 改前无代理时一律冷却整源 30s，于是腾讯首个入口一被 WAF 拦，后续 5200 只票
             在 30s 内全部快速失败 —— 冷扫直接整轮停摆，而这本可被「换个可用入口」化解。
          ② 判据一度写成「除首选外还有备选」（n-1），导致池里只剩**最后一个**可用入口
             时被误判无路可走、连它也一起冷却 —— 必须用「可用入口总数 > 0」。
        """
        if net_proxy.has_alive():
            return False
        if fq_endpoint_is_tencent(source) and fq_endpoint_available_count() > 0:
            return False
        return True

    @staticmethod
    def request(url, params=None, headers=None, max_attempts=None, timeout=None, source=None):
        max_attempts = max_attempts or C['retry']['max_attempts']
        timeout = timeout or C['retry']['timeout']
        headers = headers or {'Referer': 'https://finance.sina.com.cn/', 'User-Agent': 'Mozilla/5.0'}
        last_exception = None

        # per-source 冷却：被 WAF 的源在冷却期内直接快速失败（不真正发请求），
        # 让上层 fallback 立即走下一个独立源；不影响其他源（腾讯被墙不拖慢新浪）。
        # 例外：配了代理池且还有可用出口 IP 时**放行** —— 用「换 IP」代替「整源停摆」
        #   （冷扫 5200 只时整源冷却 30s 等于整轮卡死）。
        if source:
            _cd = HTTPClient._source_cooldown.get(source, 0)
            if _cd and _cd > time.time() and not net_proxy.has_alive():
                raise DataSourceError(f"数据源 {source} 冷却中(被WAF限流)")

        # Per-source 速率限制：每个源按自己的间隔 + 独立锁节流，互不阻塞。
        # 关键：不再用全局锁/全局时间戳，批量冷扫推进自己的时间戳，不卡实时行情。
        eff_min = HTTPClient._source_min_interval.get(source, HTTPClient._default_min_interval)
        lock = HTTPClient._lock_for(source)
        jitter = random.uniform(0.0, 0.4)  # 随机抖动打破爬虫节奏特征
        with lock:
            now = time.time()
            last = HTTPClient._source_last_request.get(source, 0)
            earliest = last + eff_min
            wait = max(0.0, earliest - now)
            if wait > 0:
                time.sleep(wait + jitter)
            HTTPClient._source_last_request[source] = time.time()

        for attempt in range(max_attempts):
            # 每轮重新取代理：上一轮被 WAF/失败记账后，这里会换到另一个出口 IP
            # （未配代理时为 None，行为与改造前完全一致）
            _proxies, _proxy_url = net_proxy.acquire_with_meta(source)
            try:
                resp = requests.get(url, params=params, headers=headers, timeout=timeout,
                                    proxies=_proxies)

                # WAF 检测：
                #  - 硬拦截：501 状态码（腾讯 WAF 明确返回）
                #  - 软限流：200 但空 body / 非 JSON（腾讯限流常返回空或 HTML 拦截页，
                #    不触发 501；若不冷却会持续猛打、更快撞硬封）
                if resp.status_code == 501:
                    HTTPClient._waf_detected = True
                    net_proxy.report(source, _proxy_url, ok=False)
                    # 501 是确定性 WAF 拒绝 → 硬拉黑该入口，下一轮 acquire 会换到备用入口
                    fq_endpoint_mark_bad(url, hard=True)
                    if HTTPClient._should_cooldown(source):
                        HTTPClient._mark_cooldown(source, base=30)
                    raise DataSourceError("WAF拦截(501)")
                if resp.status_code == 200 and resp.text and resp.text.strip():
                    text_lower = resp.text.lower()
                    # 仅当响应明显是拦截页/WAF错误页时才标记冷却；实时行情接口(hq.sinajs.cn/qt.gtimg.cn)
                    # 返回的是文本/JS，不是JSON，不能误杀。
                    waf_keywords = ['waf', 'blocked', 'captcha', 'access denied',
                                    'nginx', 'cloudflare', 'interception', 'security check']
                    if any(k in text_lower for k in waf_keywords):
                        HTTPClient._waf_detected = True
                        net_proxy.report(source, _proxy_url, ok=False)
                        fq_endpoint_mark_bad(url, hard=True)
                        if HTTPClient._should_cooldown(source):
                            HTTPClient._mark_cooldown(source, base=30)
                        raise DataSourceError("WAF软限流(拦截页)")
                    HTTPClient._waf_detected = False
                    net_proxy.report(source, _proxy_url, ok=True)
                    fq_endpoint_mark_ok(url)
                    return resp
                # 空 body / 其他状态码 → 换 IP 或冷却，避免持续猛打
                HTTPClient._waf_detected = True
                net_proxy.report(source, _proxy_url, ok=False)
                fq_endpoint_mark_bad(url, hard=True)
                if HTTPClient._should_cooldown(source):
                    HTTPClient._mark_cooldown(source, base=30)
                last_exception = DataSourceError(f"WAF软限流(空响应/HTTP {resp.status_code})")
            except DataSourceError as _de:
                # WAF 分支已按「还有没有可用代理」决定过是否冷却整源，这里只做两件事：
                #   ① 原样保留异常（否则会被下面的 `except Exception` 重新包装成
                #      「未知错误: WAF拦截(501)」，日志文案与类型双失真）；
                #   ② 继续下一轮 —— 有代理时下一轮 acquire 会换出口 IP，属真正的重试。
                last_exception = _de
            except requests.exceptions.Timeout:
                net_proxy.report(source, _proxy_url, ok=False)
                # 网络抖动属「软失败」：累计到阈值才拉黑该入口，不因一次超时就弃用
                fq_endpoint_mark_bad(url, hard=False)
                last_exception = NetworkTimeoutError("请求超时")
            except requests.exceptions.ConnectionError:
                net_proxy.report(source, _proxy_url, ok=False)
                fq_endpoint_mark_bad(url, hard=False)
                last_exception = DataSourceError("连接失败")
            except Exception as e:
                last_exception = DataSourceError(f"未知错误: {str(e)}")

            if attempt < max_attempts - 1:
                # WAF 已退避，普通错误按指数退避
                if last_exception and 'WAF' in str(last_exception):
                    time.sleep(3)
                else:
                    time.sleep(0.5 * (attempt + 1))

        raise last_exception or DataSourceError(f"重试{max_attempts}次后仍失败")


class RealtimeQuoteFetcher:
    @staticmethod
    def fetch_sina(code, timeout=10):
        """新浪实时行情单源尝试：成功返回 (fields, '新浪')，否则 (None, None)。"""
        raw_code = code.replace('sh', '').replace('sz', '')
        if not raw_code.isdigit(): raw_code = code
        try:
            resp = HTTPClient.request(f"http://hq.sinajs.cn/list={code}", timeout=timeout, source='sina_realtime')
            resp.encoding = 'gbk'; content = resp.text
            if '=' in content and '""' not in content and content.strip():
                fields = content.split('=')[1].strip('";\n').split(',')
                if len(fields) >= 32 and fields[3] and fields[3].strip():
                    return fields, '新浪'
        except Exception:
            pass
        return None, None

    @staticmethod
    def fetch_tencent(code, timeout=10):
        """腾讯实时行情单源尝试：成功返回 (fields, '腾讯')，否则 (None, None)。

        fields 归一新浪位序（消费方契约见 `format_realtime` / `_patch_intraday_bar`）：
          0=名称 1=今开 2=昨收 3=现价 4=最高 5=最低 6=买一 7=卖一 8=成交量(股) 9=成交额 30=日期 31=时间

        腾讯原始位序（2026-09-17 实测，与 `RealtimeBarPool._parse_batch` 同源）：
          [3]现价 [4]昨收 [5]今开 [6]成交量(手) [9]买一价 [11]卖一价 [30]时间 [33]最高 [34]最低
        ⚠️ 高/低在 [33]/[34]，**不是** [6]/[7]（[6] 是成交量、[7] 是外盘）——旧映射把量/外盘
        当成了高/低，导致回退腾讯源时末根 K 线的高/低/量错位（触发价错行同源问题）。
        量需 手→股 ×100，与新浪/通达信归一布局（股）保持一致。
        """
        raw_code = code.replace('sh', '').replace('sz', '')
        if not raw_code.isdigit(): raw_code = code
        try:
            resp = HTTPClient.request(f"http://qt.gtimg.cn/q={raw_code}", timeout=timeout, source='tencent_realtime')
            resp.encoding = 'gbk'; content = resp.text
            if '=' in content and '""' not in content and content.strip():
                raw_data = content.split('=')[1].strip('"\n'); parts = raw_data.split('~')
                if len(parts) >= 35 and parts[3] and parts[3].strip():
                    def _p(i):
                        return parts[i] if len(parts) > i else ''
                    try:
                        vol_shares = str(int(float(_p(6) or 0) * 100))   # 手 -> 股
                    except (TypeError, ValueError):
                        vol_shares = ''
                    fields = [_p(1), _p(5), _p(4), _p(3), _p(33), _p(34),
                              _p(9), _p(11), vol_shares, ''] + [''] * 20 + [_p(30), _p(31)]
                    return fields, '腾讯'
        except Exception:
            pass
        return None, None

    @staticmethod
    def fetch(code, timeout=10):
        """实时行情：新浪优先，失败回退腾讯（保持原行为）。"""
        fields, src = RealtimeQuoteFetcher.fetch_sina(code, timeout=timeout)
        if fields is not None:
            return fields, src
        return RealtimeQuoteFetcher.fetch_tencent(code, timeout=timeout)


class IndexFetcher:
    """熔断基准指数行情获取（腾讯实时 + 腾讯前复权历史，回退东方财富）。

    仅用于大盘熔断暂停(market_crash_pct)的基准指数当日涨跌幅与历史序列，
    不参与任何买卖建议，符合合规形态（仅回显用户配置 vs 行情触发状态）。
    """
    # 腾讯代码 -> 友好名（同时兼容东方财富 secid：sh->1. / sz->0.）
    KNOWN = {
        'sh000300': '沪深300',
        'sh000001': '上证指数',
        'sz399001': '深证成指',
        'sh000985': '中证全指',
        'sz399006': '创业板指',
        'sh000016': '上证50',
    }

    @staticmethod
    def name_of(index_code):
        return IndexFetcher.KNOWN.get(index_code, index_code)

    @staticmethod
    def get_daily_return(index_code):
        """返回 (指数友好名, 当日涨跌幅小数 或 None)。失败返回 (名, None)。"""
        name = IndexFetcher.name_of(index_code)
        # 1) 腾讯实时行情：qt.gtimg.cn/q=sh000300 -> 名称~...~昨收~今收...
        try:
            resp = HTTPClient.request("https://qt.gtimg.cn/q=" + index_code, timeout=8, source='tencent_index')
            resp.encoding = 'gbk'
            raw = resp.text.split('=')[1].strip('"\n')
            parts = raw.split('~')
            if len(parts) >= 6 and parts[3] and parts[4]:
                # 腾讯 qt 接口字段顺序：parts[3]=当前价(盘后=今收)，parts[4]=昨收。
                # 注意 prev 必须是昨收、cur 是当前价——顺序取反会得到错误符号与数值
                # （例如真实 -2.83% 会被算成 +2.91%）。
                cur = float(parts[3])
                prev = float(parts[4])
                if prev:
                    return name, (cur - prev) / prev
        except Exception:
            pass
        # 2) 回退东方财富 push2（f3=涨跌幅%，fltt=2 已折算）
        try:
            secid = ('1.' if index_code.startswith('sh') else '0.') \
                    + index_code.replace('sh', '').replace('sz', '')
            url = ("https://push2.eastmoney.com/api/qt/ulist.np/get"
                   "?fltt=2&fields=f2,f3,f12,f14&secids=" + secid)
            resp = HTTPClient.request(url, timeout=8, source='eastmoney_index')
            d = resp.json()
            node = d.get('data', {}).get('diff', [{}])[0]
            if node.get('f3') is not None:
                return name, float(node['f3']) / 100.0
        except Exception:
            pass
        return name, None

    @staticmethod
    def _fetch_index_daily(index_code, days=1500, max_retry=3):
        """指数日线抓取（腾讯 fqkline），返回 (df, err)。仅熔断基准使用。

        ⚠️ **与个股 `KLineFetcher._fetch_tencent_fq` 的关键差异**：指数不复权，
        腾讯对指数返回的是 `day` 键，**永远没有 `qfqday`**（实测 sh000300：
        `data.sh000300` 内部键 = ['day','qt','mx_price','prec','version']）。
        个股那条「只认 qfqday、绝不回退 day」的单源化规则对指数**必然失败** ——
        这正是回测日志恒打印「熔断基准指数 sh000300 历史收益已加载：0 个交易日」
        的根因：基准取空，大盘熔断判定静默退化为股票池均值（熔断实际失效）。
        指数无复权概念，取 `day` 不会引入「未复权脏数据」风险（该风险仅存在于个股）。
        """
        # 0) 通达信优先（TCP 7709 无 WAF 配额；指数无复权概念，直接取）。
        #    ⚠️ 仅当 A股 K线主源=通达信时启用（**一键切换联动**：主源选腾讯/新浪时
        #    指数直接走腾讯原逻辑）。失败（未装 pytdx / 节点失联）静默落回腾讯。
        if KLineFetcher._kline_source_for(240) == 'tdx_qfq':
            try:
                from engine.data_sources.tdx import fetch_index_daily as _tdx_idx
                _tdf, _terr = _tdx_idx(index_code, days)
                if _tdf is not None and len(_tdf):
                    return _tdf, None
                logger.debug("tdx 指数日线未命中(%s): %s", index_code, _terr)
            except Exception as e:
                logger.debug("tdx 指数日线异常(%s): %s", index_code, e)

        last_err = "指数日线获取失败"
        for attempt in range(max_retry):
            url = fq_endpoint()
            if url is None:
                return None, "腾讯K线配额已耗尽"
            fq_endpoint_note_used(url)
            try:
                resp = HTTPClient.request(
                    url, params={'param': f'{index_code},day,,,{days},qfq'},
                    timeout=5, max_attempts=1, source='tencent_index')
                data = resp.json()
            except Exception as e:
                last_err = "指数日线请求失败: %s" % e
                logger.warning("指数日线请求/解析异常(第%d次,%s,%s): %s",
                               attempt + 1, index_code, url, e)
                if '冷却' in str(e):
                    return None, last_err
                if attempt < max_retry - 1:
                    _is_waf = 'WAF' in str(e) or '501' in str(e)
                    time.sleep(0.3 if _is_waf else 1.0 * (attempt + 1))
                    continue
                return None, last_err

            if not isinstance(data, dict) or data.get('code') != 0:
                code = data.get('code') if isinstance(data, dict) else type(data).__name__
                last_err = "指数日线返回异常(code=%s)" % code
                if attempt < max_retry - 1:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                return None, last_err

            raw_data = data.get('data')
            stock_data = raw_data.get(index_code) if isinstance(raw_data, dict) else None
            if not isinstance(stock_data, dict):
                return None, "指数日线数据为空(无%s字段)" % index_code
            # 指数优先取 day；qfqday 仅作兼容兜底（个别入口可能已折算）。
            klines = stock_data.get('day') or stock_data.get('qfqday')
            if not klines:
                return None, "指数日线数据为空(无day字段)"

            rows = []
            for row in klines:
                if len(row) >= 6:
                    rows.append({
                        'trade_time': row[0],
                        'open': float(row[1]),
                        'close': float(row[2]),
                        'high': float(row[3]),
                        'low': float(row[4]),
                        'volume': float(row[5]),
                    })
            if rows:
                fq_endpoint_mark_ok(url)
                df = pd.DataFrame(rows)
                df['trade_time'] = pd.to_datetime(df['trade_time'])
                return df.sort_values('trade_time').reset_index(drop=True), None
            return None, "指数日线数据为空"

        return None, last_err

    @staticmethod
    def get_daily_returns_history(index_code, n=1500):
        """返回 {date_normalized: 当日涨跌幅小数}，用于回测历史熔断判定。

        取数顺序（2026-09-19 修复）：
          1. **本地统一 store 优先** `cache/kline/qfq_daily_{index_code}.pkl` ——
             离线回测不联网、不重复消耗腾讯配额；
          2. 本地缺失才**联网兜底**（`_fetch_index_daily`，认指数的 `day` 键），
             成功后落盘，下次直接命中。

        修复前只调 `_fetch_tencent_fq`（硬性要求 `qfqday`），而指数永远没有 `qfqday`，
        导致本函数**恒返回 {}** → 日志恒打印「已加载：0 个交易日」→ 熔断判定静默退化。

        注：指数写入 `qfq_daily_*` 时同样带 `_qfq_flag=1`，此处该标记仅作「可信缓存」
        语义（保证 `_load_qfq_daily_disk` 能读回），指数本身无复权概念。
        """
        df = None
        # 1) 本地统一 store 优先
        try:
            df = KLineFetcher.load_qfq_daily(index_code, n, offline=True)
        except Exception:
            df = None
        # 2) 本地缺失/过短 → 联网兜底并落盘
        if df is None or len(df) < 2:
            fresh, _err = IndexFetcher._fetch_index_daily(index_code, n)
            if fresh is not None and len(fresh) > 0:
                try:
                    KLineFetcher._save_qfq_daily_disk(index_code, fresh)
                except Exception:
                    pass
                df = fresh
        if df is None or len(df) < 2:
            return {}
        closes = list(df['close'].values)
        dates = list(df['trade_time'].values)
        out = {}
        for i in range(1, len(df)):
            prev = closes[i - 1]
            cur = closes[i]
            d_norm = pd.Timestamp(dates[i]).normalize()
            out[d_norm] = (cur - prev) / prev if prev else 0.0
        return out


class KLineFetcher:
    @staticmethod
    def _load_kline_disk(key):
        """从磁盘加载 K 线历史 pickle（不存在/损坏则返回 None）。

        支持两种存储格式（2026-08-02 起）：
        1. dict(orient='list') —— 新格式，跨 pandas 版本兼容。优先尝试。
        2. DataFrame pickle —— 旧格式（单源化前 / 本次修复前写入）。

        旧版 DataFrame pickle 在跨 pandas 版本时可能因 DatetimeArray/
        BlockManager/StringDtype 内部格式变更而无法加载，或加载后内部结构损坏
        （访问列时触发 "Timestamp has no len()" 或 NDArrayBacked NotImplementedError）。
        dict 加载失败时回退到兼容模式（临时 monkey-patch + DataFrame 重建）。
        """
        path = get_kline_disk_path(key)
        if not os.path.exists(path):
            return None

        # 1) 优先：dict 格式（新写入的都是这个格式，跨 pandas 版本兼容）
        try:
            with open(path, 'rb') as f:
                obj = pickle.load(f)
            if isinstance(obj, dict) and 'trade_time' in obj:
                # 新 dict 格式：重建 DataFrame
                df = pd.DataFrame(obj)
                df['trade_time'] = pd.to_datetime(df['trade_time'])
                if len(df) > 0:
                    return df
        except Exception:
            pass

        # 2) 回退：旧 DataFrame pickle（兼容模式）
        try:
            with open(path, 'rb') as f:
                df = pickle.load(f)
            if hasattr(df, 'columns') and len(df) > 0:
                # 强制重建：旧版 pandas pickle 在 2.x 下 BlockManager 可能不完整，
                # 访问某些列时触发 "Timestamp has no len()" 等内部错误。
                df = pd.DataFrame(df.to_dict(orient='list'))
                return df
        except Exception:
            pass

        # 3) 直接加载/重建均失败：回退到 pandas 2.x 兼容模式
        df = KLineFetcher._load_kline_compat(path)
        if df is not None:
            # 加载成功：用当前 pandas 版本重新保存（新 dict 格式），下次无需再走兼容模式
            try:
                KLineFetcher._save_kline_disk(key, df)
                logger.info(f"K线缓存已用当前pandas版本重新保存: {path}")
            except Exception:
                pass
        return df

    @staticmethod
    def _load_kline_compat(path):
        """pandas 2.x 兼容模式加载旧版 pickle（返回 DataFrame 或 None）。"""
        import numpy as np

        def _make_compat(original):
            def compat(self, state):
                if not isinstance(state, tuple) or len(state) < 2:
                    return original(self, state)
                dtype_item, data_item = state[0], state[1]
                if isinstance(data_item, np.ndarray) and data_item.ndim == 2 and data_item.shape[0] == 1:
                    data_item = data_item[0]
                if isinstance(dtype_item, np.dtype) and dtype_item.kind == 'M' and 'us' in str(dtype_item):
                    data_item = data_item.astype('datetime64[ns]')
                    dtype_item = np.dtype('datetime64[ns]')
                freq_dict = state[2] if len(state) >= 3 and isinstance(state[2], dict) else {}
                new_state = (dtype_item, data_item, freq_dict)
                try:
                    return original(self, new_state)
                except (NotImplementedError, TypeError, AttributeError):
                    return original(self, state)
            return compat

        patches = []
        for mod_name, cls_name in [
            ('pandas.core.arrays.datetimes', 'DatetimeArray'),
            ('pandas.core.arrays.timedeltas', 'TimedeltaArray'),
            ('pandas.core.arrays.period', 'PeriodArray'),
            ('pandas.core.arrays.string_', 'StringArray'),
        ]:
            try:
                mod = __import__(mod_name, fromlist=[cls_name])
                cls = getattr(mod, cls_name)
                orig = cls.__setstate__
                cls.__setstate__ = _make_compat(orig)
                patches.append((cls, orig))
            except Exception:
                pass

        try:
            from pandas.core.internals.managers import BlockManager
            orig_verify = BlockManager._verify_integrity
            def compat_verify(self):
                try:
                    return orig_verify(self)
                except (IndexError, ValueError):
                    pass
            BlockManager._verify_integrity = compat_verify
            patches.append(('BM_verify', orig_verify))
        except Exception:
            pass

        try:
            with open(path, 'rb') as f:
                df = pickle.load(f)
            # 验证 DataFrame 基本可用
            if hasattr(df, 'columns') and len(df) > 0:
                # 重建 DataFrame：旧版 pickle 的内部 BlockManager 在 pandas 2.x 下
                # 结构不完整，直接访问列会触发 "Timestamp has no len()" 等错误。
                # 用 to_dict() 提取数据后重新构造，可彻底消除内部结构差异。
                try:
                    df = pd.DataFrame(df.to_dict(orient='list'))
                    logger.info(f"K线缓存兼容模式加载成功: {path}")
                    return df
                except Exception:
                    # 重建失败：数据已损坏到无法提取，返回 None 让调用方重抓
                    pass
        except Exception as e:
            logger.warning(f"K线缓存兼容模式加载失败 {path}: {e}")
        finally:
            # 恢复 patch
            for item in patches:
                if isinstance(item[0], str) and item[0] == 'BM_verify':
                    try:
                        BlockManager._verify_integrity = item[1]
                    except Exception:
                        pass
                elif len(item) == 2:
                    cls, orig = item
                    try:
                        cls.__setstate__ = orig
                    except Exception:
                        pass
        return None

    @staticmethod
    def _save_kline_disk(key, df, qfq=True):
        """将 K 线历史 pickle 落盘（首次写前确保目录存在）。

        落盘前注入 _qfq_flag 列作为「前复权可信标记」：日K/周K 写入的都是**前复权**
        （腾讯服务端 qfq 或通达信自算，两源实测逐日偏差 0.000%），带标记=1；
        未复权源（新浪 raw store）与历史残留脏 pkl / 旧格式 pkl 无此列或=0。fetch 加载时
        据此识别脏缓存并整段重抓，杜绝 incremental_merge 用错误 r 把脏序列错位缩放进因子评分
        （这正是「同一票两次启动分数翻转」的根因）。

        qfq 参数控制标记值：支持的日K/周K 均为前复权(qfq=True→标记=1)；
        保留 False 分支(标记=0)供未复权源（新浪未复权 store）使用，与前复权(=1)区分避免误缩放。

        存储格式改用 dict(orient='list') 而非直接 pickle DataFrame（2026-08-02）：
        pandas 3.0 默认 future.infer_string=True，列名 Index 用 StringDtype；
        pandas 2.x 的 NDArrayBacked.__setstate__ (Cython 层) 不认识该类型，pickle.load
        直接抛 NotImplementedError。dict 存原生 Python 类型，跨 pandas 版本读写均兼容，
        且 _load_kline_disk 重建后 _qfq_flag 校验逻辑不受影响。"""
        path = get_kline_disk_path(key)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            save_df = df.copy()
            save_df['_qfq_flag'] = 1 if qfq else 0
            # 用 dict 存储，绕开 pandas Index 的 StringDtype 跨版本 pickle 不兼容
            payload = save_df.to_dict(orient='list')
            with open(path, 'wb') as f:
                pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception as e:
            logger.warning(f"写入K线磁盘缓存失败: {e}")

    @staticmethod
    def _fetch_tdx_fq(stock_code, days, full=False):
        """通达信（pytdx）前复权日K。返回 (df, err)，契约同 _fetch_tencent_fq。

        full=True 翻页拉全历史（首次建库，实测 ~3102 根 / 12.7 年）；
        full=False 按 days 抓取（增量刷新，请求数最小）。
        pytdx 未安装 / 7 个可用节点全失联时返回 (None, err)，由调用方回退腾讯。

        ⚠️ pytdx 是**延迟 import**：打包时必须在 build_webview.spec 的
        hiddenimports 里登记，否则 frozen 下 ImportError 会被吞掉、
        静默回退腾讯（表现为「装了 pytdx 却没生效」）。
        """
        try:
            from engine.data_sources.tdx import fetch_daily_qfq
        except Exception as e:
            return None, f"通达信源模块不可用: {e}"
        return fetch_daily_qfq(stock_code, 240, days=days, full=full)

    @staticmethod
    def _fetch_native_tail(stock_code, days):
        """按当前 A股 K线数据源抓日K尾巴（3b 增量刷新用）。

        只拉最近 days 根。增量拼接数学安全的前提是「尾巴覆盖到旧缓存最后一天」，
        days 由调用方按 gap+5 保证（见 _fetch_internal 3b）。
        """
        if KLineFetcher._kline_source_for(240) == 'tdx_qfq':
            return KLineFetcher._fetch_tdx_fq(stock_code, days, full=False)
        return KLineFetcher._fetch_tencent_fq(stock_code, days)

    @staticmethod
    def _fetch_tdx_minute_cached(stock_code, k_type, days):
        """通达信未复权分钟K：内存 TTL 缓存 -> 网络。**不落盘**（与新浪分钟口径一致）。

        volume 单位 = **股** —— 与 `_fetch_sina_minute` 完全一致，下游量能因子无需感知换算。
        注意：分钟缓存 key 与日K共用 `state.cache`（同为 md5(code_ktype_days)），
        新浪/通达信两源分钟都是未复权原始价，切源复用不引入尺度差。
        pytdx 未安装 / 服务器失联时返回 None，由调用方报错（不做新浪兜底 ——
        分钟缺失是显式失败，静默换源会让两源深度差 20 倍的事实被掩盖）。
        """
        key = get_cache_key(stock_code, k_type, days)
        df = get_cached(key)
        if df is not None:
            return df
        try:
            from engine.data_sources.tdx import fetch_minute_raw
        except Exception as e:
            logger.warning("通达信分钟模块不可用: %s", e)
            return None
        df, err = fetch_minute_raw(stock_code, k_type, days)
        if df is not None and len(df):
            set_cache(key, df)
            return df
        logger.warning("通达信分钟K获取失败(%s, k_type=%s): %s", stock_code, k_type, err)
        return None

    @staticmethod
    def _fetch_full(stock_code, k_type, days):
        """全量实时抓取（不落盘，由调用方负责缓存）。返回 (df, err)。

        前复权单一口径：日K 第一手走通达信自算复权（失败回退腾讯 fqkline），
        周K 由 fetch_weekly 按源分发，分钟级走通达信未复权。被墙/冷却时快速失败
        不再 sleep 等待重试（2026-09-12 去阻塞）；失败即返回 None，由调用方
        保留旧缓存或排除该股票。⛔ 不再降级新浪未复权 / 腾讯不复权 —— 避免未复权
        数据污染前复权缓存与因子评分。
        """
        errs = []
        if k_type == 1200:
            df = KLineFetcher.fetch_weekly(stock_code, weeks=days)
            if df is not None:
                return df, None
            return None, "腾讯前复权周K获取失败"
        if k_type == 240:
            # 第一手源：通达信（pytdx）日K 前复权。full=True 翻页拉全历史 ——
            # 首次建库一次拿满 ~3102 根（12.7 年），回测直接受益；请求无配额。
            # 失败（未装 pytdx / 7 节点全失联 / 该 /24 段被封）自动回退腾讯 fqkline，
            # 两源前复权口径实测逐日偏差 0.000%，回退不引入尺度差。
            if KLineFetcher._kline_source_for(k_type) == 'tdx_qfq':
                tdx_df, tdx_err = KLineFetcher._fetch_tdx_fq(stock_code, days, full=True)
                if tdx_df is not None:
                    return tdx_df, None
                if tdx_err:
                    errs.append(f"通达信前复权: {tdx_err}")
                logger.warning("通达信源失败(%s)，回退腾讯: %s", stock_code, tdx_err)
            df, err = KLineFetcher._fetch_tencent_fq(stock_code, days)
            if df is not None:
                return df, None
            if err: errs.append(f"腾讯前复权: {err}")
            # 冷却中（WAF限流）不再 sleep 等待重试：_fetch_tencent_fq 对'冷却'已
            # 快速失败，此处分片能立刻推进，避免多线程在冷却期各自阻塞导致
            # 全市场扫描 watchdog 判定分片超时、整片被放弃（跳过 N 只网络超时）。
            # 失败票由调用方保留旧缓存/下次扫描重试，冷却 30s 内仅影响少量股票。
        # 单源化：日K/周K 前复权——日K 第一手 tdx 自算（回退腾讯），周K 由 fetch_weekly
        #   分发（tdx 自算/bisect 锚定，回退腾讯）；分钟级 K 线走 tdx 未复权。
        return None, "; ".join(errs) if errs else "腾讯前复权数据获取失败"

    @staticmethod
    def incremental_merge(old_df, new_tail_df):
        """把今日锚点的 qfq 尾巴(new_tail_df) 增量拼接到旧缓存(old_df, 旧锚点)。

        前复权(qfq)的锚点是「最新一天」，历史价格会随累计复权因子整体缩放。
        因此两次不同时间取的 qfq 序列只差一个**全局常数 r**（= 间隔期累计复权因子）。
        做法：取重叠日(旧最后一天仍出现在新尾巴里的部分)算 r = 新/旧 的中位数，
        把整段旧序列 × r 对齐到今日锚点，再 append 真正的新交易日。
        这样无论间隔多久、期间有无分红，拼接都数学无缝（修朴素追加的假跳变）。
        无法对齐(源异常/重叠缺失/除权冻结块)时返回 None，由调用方整段重抓兜底。
        """
        if old_df is None or len(old_df) == 0 or new_tail_df is None or len(new_tail_df) == 0:
            return None
        old = old_df.copy()
        old['trade_time'] = pd.to_datetime(old['trade_time'])
        tail = new_tail_df.copy()
        tail['trade_time'] = pd.to_datetime(tail['trade_time'])
        tail = tail[tail['close'] > 0].sort_values('trade_time')
        if old.empty or tail.empty:
            return None

        last_date = old['trade_time'].max()
        # 重叠区：新尾巴里日期 <= 旧最后一天 的部分
        overlap = tail[tail['trade_time'] <= last_date]
        if overlap.empty:
            return None  # 尾巴没覆盖到旧数据末尾 -> 无法对齐

        # 按交易日对齐算比值 r（取中位数，抗单日源异常）
        old_idx = old.set_index(old['trade_time'].dt.normalize())
        ratios = []
        for _, row in overlap.iterrows():
            d = row['trade_time'].normalize()
            if d in old_idx.index:
                oc = float(old_idx.loc[d, 'close'])
                nc = float(row['close'])
                if oc > 0:
                    ratios.append(nc / oc)
        if not ratios:
            return None
        r = float(np.median(ratios))

        # 健全性：合法 qfq 的 r = 新/旧 = 1/累计复权因子，恒 ≤ 1（分红送转只让 r<1；
        # 并股/拆股极端场景亦 ≤1 或仅略>1）。上界收紧到 1.01：r>1 只可能来自未复权
        # 脏缓存或源异常，拒绝以免整段旧序列被错位缩放（前复权 r 正常必 ≤1）。
        # 下界 0.2 容纳极端拆分(3:1 / 10转10+现金≈0.25)。
        if not (0.2 <= r <= 1.01):
            return None

        # 整段旧序列 × r 对齐到今日锚点（仅 OHLC，volume 不变）
        if abs(r - 1.0) > 1e-9:
            for c in ['open', 'close', 'high', 'low']:
                old[c] = old[c].astype(float) * r

        # 拼接真正新增的交易日（新尾巴里 > 旧最后一天）
        new_part = tail[tail['trade_time'] > last_date]
        merged = pd.concat([old, new_part], ignore_index=True) if len(new_part) else old
        merged = merged.drop_duplicates(subset=['trade_time'], keep='last')
        merged = merged.sort_values('trade_time').reset_index(drop=True)
        merged = merged[merged['close'] > 0]
        return merged

    @staticmethod
    def _cache_and_save(key, df, qfq=True, days=None):
        """内存缓存 + 磁盘落盘，并统一注入复权标记列（_qfq_flag）。

        所有写缓存路径（首次全量 / 增量成功 / 整段重抓兜底）都应经此函数，
        保证内存与磁盘的缓存都带标记，供 fetch 的复权校验识别脏缓存。
        qfq=False 用于未来非前复权源扩展，标记=0；当前日K/周K 均为前复权(True)。

        days 非空且 df 更长时，**只把尾部 days 根写进缓存**（返回值仍是完整 df，
        且照旧带 _qfq_flag）。原因：key 是 (code, k_type, days) 的 md5，更长序列
        对这个 key **永远不会被复用**（换个 days 就是另一个 key）—— 存长了纯属
        磁盘浪费。通达信源的 `_fetch_full` 会翻页拉满 ~3102 根，不截尾会让
        `{md5}.pkl` 与 `qfq_daily_*.pkl` 各存一份全历史（实测单只 208KB×2）。
        返回值保持完整，是因为外层 `fetch()` 要靠它把全历史写进
        `qfq_daily_*` 共享 store（回测读的就是那份，见 _save_qfq_daily_disk）。
        """
        out = df.copy()
        out['_qfq_flag'] = 1 if qfq else 0
        save_df = out
        if days and len(out) > days:
            save_df = out.iloc[-days:].reset_index(drop=True)
        set_cache(key, save_df)
        KLineFetcher._save_kline_disk(key, save_df, qfq=qfq)
        return out

    @staticmethod
    def _fetch_sina_minute(stock_code, k_type, days=300, max_retry=3):
        """新浪未复权 K 线（getKLineData）。

        scale 支持 5/15/30/60 分钟、240(日K)、1200(周K)，均为**未复权**
        （真实成交价，无前复权/后复权处理）。上限 1023 根（datalen）。
        期货/股票分钟均适用；股票日K/周K 未复权仅当用户在数据源面板显式
        切换「新浪未复权」时启用（默认是通达信前复权）。

        Returns:
            (df, err)：df 含 trade_time/open/high/low/close/volume，按时间升序；
            失败返回 (None, err)。
        """
        scale = int(k_type)
        if scale not in (1, 5, 15, 30, 60, 240, 1200):
            return None, f"新浪K线不支持该周期(scale={scale})"
        datalen = min(int(days or 300), 1023)
        url = ('https://quotes.sina.cn/cn/api/json_v2.php/'
               'CN_MarketDataService.getKLineData')
        last_err = f"新浪K线获取失败(scale={scale})"
        for attempt in range(max_retry):
            try:
                resp = HTTPClient.request(
                    url,
                    params={'symbol': stock_code, 'scale': scale, 'ma': 'no', 'datalen': datalen},
                    timeout=8, max_attempts=1, source='sina_kline')
                rows = resp.json()
            except Exception as e:
                last_err = f"新浪K线请求/解析异常: {e}"
                logger.warning("新浪K线请求/解析异常(第%d次,%s): %s", attempt + 1, stock_code, e)
                if attempt < max_retry - 1:
                    time.sleep(0.8 * (attempt + 1))
                    continue
                return None, last_err
            if not isinstance(rows, list) or not rows:
                last_err = "新浪K线返回为空"
                if attempt < max_retry - 1:
                    time.sleep(0.8 * (attempt + 1))
                    continue
                return None, last_err
            try:
                data = []
                for r in rows:
                    if not isinstance(r, dict) or 'day' not in r:
                        continue
                    close = float(r.get('close') or 0)
                    if close <= 0:
                        continue
                    data.append({
                        'trade_time': r['day'],
                        'open': float(r.get('open') or 0),
                        'high': float(r.get('high') or 0),
                        'low': float(r.get('low') or 0),
                        'close': close,
                        'volume': float(r.get('volume') or 0),
                    })
                if data:
                    df = pd.DataFrame(data)
                    df['trade_time'] = pd.to_datetime(df['trade_time'])
                    return df.sort_values('trade_time').reset_index(drop=True), None
            except Exception as e:
                last_err = f"新浪K线解析异常: {e}"
                logger.warning("新浪K线解析异常(%s): %s", stock_code, e)
            if attempt < max_retry - 1:
                time.sleep(0.8 * (attempt + 1))
        return None, last_err

    # ── K线数据源（A股，全局单选）────────────────────────────────────
    # data_sources.json 的 kline_source 决定 A股 K线数据源（用户可配置）：
    #   tdx     = 通达信前复权（**默认第一手源**）：日K 自算复权、走 TCP 无 WAF 配额；
    #             周K 走 tdx 自算复权（bisect 锚定，回退腾讯）；分钟级走 tdx 未复权
    #   tencent = 腾讯前复权：仅 日K/周K（无分钟级K线 → 分钟周期不可用）
    #   sina    = 新浪未复权：日K/周K + 1/5/15/30/60分钟 全周期（分钟级分析/自选诊断可用）
    # 模块级缓存 + reset 供 web_api 热重载，重启即重新读取。
    _KLINE_SOURCES = None

    @staticmethod
    def _load_kline_sources():
        """读取 A股 K线数据源（kline_source）→ 周期映射；config 缺失/损坏用默认(通达信)。

        Returns:
            {period_label: source_key|None}；None 表示该周期在当前数据源下不可用。
        """
        if KLineFetcher._KLINE_SOURCES is not None:
            return KLineFetcher._KLINE_SOURCES
        # 默认第一手源 = 通达信（pytdx）。它对腾讯的唯一硬依赖是那批 /24 段节点，
        # 一旦不可用，_fetch_full 会自动回退腾讯 fqkline（两源前复权口径实测一致），
        # 因此把默认值前移到 tdx 不会让用户「拉不到数据」，只是换一条更宽的通道。
        source = 'tdx'
        try:
            import json as _json
            path = os.path.join(CONFIG_DIR, 'data_sources.json')
            with open(path, 'r', encoding='utf-8') as f:
                d = _json.load(f)
            ks = d.get('kline_source')
            if ks in ('tencent', 'sina', 'tdx'):
                source = ks
        except Exception as e:
            logger.warning("读取 data_sources.json 失败，K线数据源用默认(通达信): %s", e)
        if source == 'sina':
            mapping = {p: 'sina_raw' for p in ('日K', '周K', '60分钟', '30分钟', '15分钟', '5分钟', '1分钟')}
            # 新浪是【未复权】数据 → 落到**独立** store raw_{daily,weekly}_{code}.pkl（_qfq_flag=0），
            # 与 qfq_daily_*（前复权）物理隔离：复权尺度不同，混并会让收益/因子错位。
            # ⚠️ 两个 store **不互通**：数据源=sina 时前复权 store 不会被写入，
            # 所以「离线回测 0 命中」与缓存目录位置无关，只看数据源。
            logger.warning(
                "K线数据源=新浪(未复权)：日K/周K 落盘到**独立** store raw_daily_/raw_weekly_*.pkl"
                "（带 _qfq_flag=0 标注）。回测用的前复权 store qfq_daily_* 只在数据源=tencent/tdx 时写入"
                "—— 若回测报「离线模式 0 命中」，就是这个原因")
        elif source == 'tdx':
            # 通达信（pytdx）：**日K** 前复权第一手源，自算复权，写 qfq_daily_* 前复权 store。
            # 2026-09-19 接入：走 TCP 7709，不吃腾讯 fqkline 的「IP+入口」配额
            # （腾讯单入口约 350 次/窗口即 501，5200 只全市场刷新必撞）。
            #
            # ⚠️ **周K 已切 tdx 自算复权**（不是"仍走腾讯"）：通达信周K 的 bar 日期是
            #    「周最后交易日」，除权日若落在周中则用 bisect 找「第一个 >= 除权日的
            #    周K bar」做锚点（见 tdx.compute_qfq_factors），避免精确匹配漏掉停牌除权周。
            #    代价：除权周与腾讯服务端有 0.1%~0.94% 偏差（无除权周逐周完全一致），
            #    用户拍板接受；tdx 失败自动回退腾讯（fetch_weekly 兜底）。周K 请求量
            #    远小于日K（仅自选/诊断触发），不影响配额。
            # 分钟周期走 tdx（未复权）：实测 5分K 23568 根（2024-09 起 ≈2 年）、
            # 1分K 22320 根（2026-05 起），比新浪 datalen 上限 1023 根深约 20 倍；
            # volume 单位=股，与新浪分钟链路口径一致，无需换算。
            mapping = {'日K': 'tdx_qfq', '周K': 'tdx_weekly',
                       '60分钟': 'tdx_minute', '30分钟': 'tdx_minute',
                       '15分钟': 'tdx_minute', '5分钟': 'tdx_minute',
                       '1分钟': 'tdx_minute'}
            logger.warning(
                "K线数据源=通达信(pytdx)：日K 前复权第一手源（TCP 7709，无 WAF 配额），"
                "落盘 qfq_daily_*；周K 走 tdx 自算复权（除权周 ±1% 内，失败回退腾讯）；"
                "分钟级走 tdx 未复权（不落盘）。"
                "⚠️ 可用节点集中在单一 /24 段，腾讯作为失败回退源保留")
        else:
            # 腾讯：仅日K/周K 前复权；分钟周期置 None（不可用，做不了分钟级分析）
            mapping = {'日K': 'tencent_qfq', '周K': 'tencent_qfq'}
            for p in ('60分钟', '30分钟', '15分钟', '5分钟', '1分钟'):
                mapping[p] = None
        KLineFetcher._KLINE_SOURCES = mapping
        return mapping

    @staticmethod
    def reset_kline_sources():
        """清空数据源映射缓存（web_api 保存配置后热重载调用）。"""
        KLineFetcher._KLINE_SOURCES = None

    @staticmethod
    def _kline_source_for(k_type):
        label = next((l for l, v in K_TYPE_MAP.items() if v == k_type), '日K')
        return KLineFetcher._load_kline_sources().get(label, 'tencent_qfq')

    @staticmethod
    def source_label(src):
        """把 `_kline_source_for` 的源标识翻成报告里给用户看的中文标签。

        ⛔ 必须覆盖**全部**源标识：漏一个就会张冠李戴。历史上此逻辑散落在
        analyze_service / web_api 两处，且只做了 `tencent_qfq ? 腾讯前复权 : 新浪未复权`
        的二选一 —— 而默认源早已是 tdx，于是报告长期把通达信前复权数据标成
        「新浪未复权」（前复权/未复权口径完全相反）。新增数据源时务必在此登记。
        """
        if src is None:
            return '当前周期不可用'
        return {
            'tdx_qfq':     '通达信前复权（自算）',
            'tdx_weekly':  '通达信周K前复权（自算）',
            'tdx_minute':  '通达信分钟K（未复权）',
            'tencent_qfq': '腾讯前复权',
            'sina_raw':    '新浪未复权',
        }.get(src, f'未知数据源({src})')

    @staticmethod
    def _raw_store_is_fresh(disk_df):
        """raw store 是否已覆盖「最近一个已收盘的交易日」（可否直接复用）。

        日历逻辑与腾讯分支（`_fetch_internal` 的 3a~3a'''）保持一致 —— 缺了这层判断
        会出现**静默错误**：今天扫完存下 300 行，明天扫描长度仍 ≥300 就直接复用，
        于是分析永远少一根当天 K 线、结果滞后一天。

        · 最后一根 >= 今天（含时钟偏差的未来日期）→ 新鲜；
        · 最后一根 < 今天 → 看 [last+1, check_end] 间有没有**已收盘的交易日**：
          周末/节假日前 → 新鲜；有 → 需补抓；
        · 交易日盘中（<15:00）今日 Bar 未收盘、不完整，不计入，check_end 取昨天。
        """
        try:
            if disk_df is None or len(disk_df) == 0 or 'trade_time' not in disk_df.columns:
                return False
            last = pd.to_datetime(disk_df['trade_time']).max().date()
            now = datetime.now()
            today = now.date()
            if last >= today:
                return True
            from datetime import timedelta as _td
            close_t = datetime(2000, 1, 1, 15, 0).time()
            check_end = today - _td(days=1) if now.time() < close_t else today
            d = last + _td(days=1)
            while d <= check_end:
                if d.weekday() < 5:      # 工作日视为交易日（不处理节假日；多抓一次空数据无害）
                    return False
                d += _td(days=1)
            return True
        except Exception:
            return False

    @staticmethod
    def _fetch_sina_raw_cached(stock_code, k_type, days):
        """新浪未复权 K 线：内存(TTL 300s) → 磁盘 raw store → 网络，三级缓存。

        落盘策略（2026-09-17，用户要求「新浪源全市场扫描也要落盘」）：
        - **仅 日K(240) / 周K(1200) 落盘**；分钟级数量级大且扫描用不到，仍只走内存。
        - 落到**独立 store** `raw_daily_{code}.pkl` / `raw_weekly_{code}.pkl`，
          带 `_qfq_flag=0` 显式标注「未复权」，与 `qfq_daily_*`（前复权）**物理隔离**。
          ⚠️ 两者绝不可混并：复权尺度不同，混一起会让收益/因子错位 —— 这正是旧版
          「一律不落盘」要防的事。隔离 store 后即可安全落盘。
        - 回测的 `load_qfq_daily` 只读 `qfq_daily_*`，不会读到这里（语义保持前复权）。

        磁盘复用需**同时**满足两个条件，缺一不可：
        ① `len(disk) >= days` —— raw store 按**代码维度**建文件（不含 days），
           光是「命中」不够：先扫 300 天再请求 1200 天若直接返回 300 行，永远拿不到长历史；
        ② `_raw_store_is_fresh()` —— 长度够也可能已隔天，直接复用会让结果滞后。
        """
        persistable = k_type in (240, 1200)
        key = get_cache_key(stock_code, k_type, days)
        df = get_cached(key)
        disk_df = None
        if df is None and persistable:
            disk_df = KLineFetcher._load_raw_daily_disk(stock_code, k_type)
            if disk_df is not None and len(disk_df) >= days \
                    and KLineFetcher._raw_store_is_fresh(disk_df):
                set_cache(key, disk_df)
                df = disk_df
        if df is None:
            # 增量抓取：store 已够长、只是陈旧时，只补缺口那几根（新浪按 datalen 取
            # 「最近 N 根」），再与磁盘并集。避免隔天扫描让 5000+ 只整段重抓
            # （实测均 479ms/只 ≈ 41 分钟），把跨天刷新压到差不多一个请求的量级。
            _need = days
            if persistable and disk_df is not None and len(disk_df) >= days:
                try:
                    _last = pd.to_datetime(disk_df['trade_time']).max().date()
                    _gap = max(0, (datetime.now().date() - _last).days)
                    # 自然日 → 交易日留 1.6× 冗余；下限 10 根以覆盖周末与短假
                    _need = min(days, max(10, int(_gap * 1.6) + 5))
                except Exception:
                    _need = days
            fetched, _err = KLineFetcher._fetch_sina_minute(stock_code, k_type, _need)
            if fetched is not None:
                if persistable:
                    # 并集合并：返回合并后的完整序列（可能比本次抓的更长）
                    merged = KLineFetcher._save_raw_daily_disk(stock_code, k_type, fetched)
                    df = merged if merged is not None else fetched
                else:
                    df = fetched
                set_cache(key, df)
            elif disk_df is not None and len(disk_df) > 0:
                # 联网失败 → 降级用磁盘已有的（可能陈旧/不足，但好过没有）
                set_cache(key, disk_df)
                df = disk_df
        return df

    @staticmethod
    def _load_raw_daily_disk(code, k_type=240):
        """载入新浪【未复权】日K/周K store；命中返回 df（去掉 `_qfq_flag` 列），否则 None。"""
        path = get_raw_daily_disk_path(code, k_type)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, 'rb') as f:
                obj = pickle.load(f)
            if isinstance(obj, dict) and 'trade_time' in obj:
                df = pd.DataFrame(obj)
                df['trade_time'] = pd.to_datetime(df['trade_time'])
                if '_qfq_flag' in df.columns:
                    df = df.drop(columns=['_qfq_flag'])
                if len(df) > 0:
                    return df
        except Exception:
            pass
        return None

    @staticmethod
    def _save_raw_daily_disk(code, k_type, df):
        """把新浪【未复权】日K/周K 落盘到独立 store（dict 格式 + `_qfq_flag=0`）。

        合并策略**刻意不同于** qfq store：
        - 前复权序列的复权尺度会随新抓取整体漂移，故那边只能「保留更长的一份、绝不混并」；
        - 未复权价格是**确定性历史**（不复权 → 历史 Bar 永不变动），因此这里可以安全地
          按 `trade_time` **并集合并**（新值覆盖旧值），实现真正的增量累积：
          先抓 300 根、再抓 1200 根，最终得到 1200 根的并集，而不是互相覆盖。
        写入同样走「临时文件 + os.replace」原子替换，并发读者只会看到完整文件。

        Returns:
            合并后的 DataFrame（**不含** `_qfq_flag` 列，可直接复用）；失败返回 None。
        """
        path = get_raw_daily_disk_path(code, k_type)
        try:
            if df is None or len(df) == 0 or 'trade_time' not in df.columns:
                return None
            os.makedirs(os.path.dirname(path), exist_ok=True)
            new_df = df.copy()
            new_df['trade_time'] = pd.to_datetime(new_df['trade_time'])
            with _raw_store_lock:
                old = KLineFetcher._load_raw_daily_disk(code, k_type)
                if old is not None and len(old) > 0:
                    old = old.drop(columns=[c for c in ('_qfq_flag',) if c in old.columns],
                                   errors='ignore')
                    merged = pd.concat([old, new_df], ignore_index=True)
                    merged = merged.drop_duplicates(subset=['trade_time'], keep='last')
                    merged = merged.sort_values('trade_time').reset_index(drop=True)
                else:
                    merged = new_df.reset_index(drop=True)
                out = merged.copy()
                merged['_qfq_flag'] = 0
                payload = merged.to_dict(orient='list')
                tmp_path = path + '.tmp'
                with open(tmp_path, 'wb') as f:
                    pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
                os.replace(tmp_path, path)
                return out
        except Exception:
            return None

    @staticmethod
    def fetch(stock_code, k_type, days=60):
        """公开日线前复权接口。

        除原抓取/增量逻辑外，额外把结果同步写入按代码维度的统一缓存
        cache/kline/qfq_daily_{code}.pkl，供回测直接复用——以此取代独立的
        backtest_data_cache_qfq.pkl 大文件，免去手动更新/打包维护。

        注意：写盘判定**不可**依赖 `_qfq_flag in df.columns`。该列仅由
        `_save_qfq_daily_disk` 在落盘时添加，事前任何路径（在线 _fetch_tencent_fq、
        磁盘热层）返回的 df 都不带它——旧条件导致共享 store 永远写不进、
        回测离线读永远空。fetch 只返回前复权（通达信自算 / 腾讯 qfqday），返回 df 即前复权，
        日线成功(k_type==240 且含 close)即可写共享 store。
        """
        df, err = KLineFetcher._fetch_internal(stock_code, k_type, days)
        # 仅当数据确为「前复权」才落盘 qfq_daily 共享 store。
        # 前复权的两只源：tencent_qfq（腾讯服务端算好）与 tdx_qfq（通达信自算，
        # 实测与腾讯逐日偏差 0.000%）；新浪源返回【未复权】，若写入并打
        # _qfq_flag=1 会冒充前复权喂给回测（未复权 pkl 与日K前复权缓存语义冲突
        # → 收益/因子错位）。
        src = KLineFetcher._kline_source_for(k_type)
        if df is not None and k_type == 240 and len(df) > 0 and 'close' in df.columns \
                and src in ('tencent_qfq', 'tdx_qfq'):
            try:
                KLineFetcher._save_qfq_daily_disk(stock_code, df)
            except Exception:
                pass
        # 对外契约：返回根数不得超过 days。两种来源都会超出：
        #   ① 腾讯 fqkline 的 count 是「请求根数」而非「返回根数」，实测 days=300 → 302 根；
        #   ② 磁盘/内存缓存条目按 (code, k_type, days) 建 key，但增量合并后长度会漂移。
        # 不截尾会让 GET /api/kline?days=300 返回 302 根，直接违反 F-203 验收①。
        # 注意顺序：必须在落盘 qfq_daily 全量 store **之后**再截尾——该 store 是
        # 「按代码维度全历史」，截尾后落盘会把权威长历史削成当日请求窗口。
        if df is not None and days and days > 0 and len(df) > days:
            df = df.iloc[-days:].reset_index(drop=True)
        return df, err

    @staticmethod
    @staticmethod
    def _replace_today_bar_if_stale(stock_code, df):
        """若缓存末根是「盘中快照」，用批量实时行情的**收盘终值**替换它。

        为什么需要：「缓存里有今日 bar」≠「它是最终值」。**盘中扫描**同样会把当时的
        实时快照落盘（成交量只有半天），盘后若无条件复用，量能/振幅类因子会**静默**
        用错数据（2026-09-18 由用户提问暴露）。

        判据：末根与实时行情的 OHLCV **有任一不一致** ⇒ 判为盘中快照。
        依据：收盘后二者必然完全相同 —— 前复权锚点=最新，末根即原始价（实测 6/6 一致）。

        返回替换后的 df；若**已是终值 / 批量池不可用 / 日期对不上**则返回 None，
        调用方按原逻辑复用缓存 —— 本检查失败绝不改变原有行为。
        """
        try:
            bar = RealtimeBarPool.get(stock_code)
            if bar is None or df is None or len(df) == 0:
                return None
            last = df.iloc[-1]
            if pd.Timestamp(bar['trade_time']).date() != pd.Timestamp(last['trade_time']).date():
                return None                      # 非同一交易日，无从比较
            same = (abs(float(last['open']) - float(bar['open'])) <= 1e-6
                    and abs(float(last['close']) - float(bar['close'])) <= 1e-6
                    and abs(float(last['high']) - float(bar['high'])) <= 1e-6
                    and abs(float(last['low']) - float(bar['low'])) <= 1e-6
                    and abs(float(last['volume']) - float(bar['volume'])) <= 1e-6)
            if same:
                return None                      # 已是收盘终值，无需修正
            logger.info('K线修正：%s 的今日 bar 为盘中快照（量 %s → %s），改为收盘终值',
                        stock_code, last.get('volume'), bar['volume'])
            row = {'trade_time': pd.Timestamp(bar['trade_time']),
                   'open': float(bar['open']), 'close': float(bar['close']),
                   'high': float(bar['high']), 'low': float(bar['low']),
                   'volume': float(bar['volume'])}
            if '_qfq_flag' in df.columns:        # 保持复权标记列对齐
                row['_qfq_flag'] = df['_qfq_flag'].iloc[0]
            return pd.concat([df.iloc[:-1], pd.DataFrame([row])], ignore_index=True)
        except Exception as e:
            logger.debug('K线修正：%s 检查失败（忽略，按原缓存复用）: %s', stock_code, e)
            return None

    def _fetch_internal(stock_code, k_type, days=60):
        if k_type == 1200: days = min(days, 200)
        # 分钟级K线（新浪未复权，不落盘）：内存 TTL 300s 缓存，未命中整段重抓。
        # 不做增量合并——分钟粒度跨除权日有跳变，增量 r 对齐无意义。
        if k_type in MINUTE_K_TYPES:
            _msrc = KLineFetcher._kline_source_for(k_type)
            if _msrc is None:
                return None, ("当前数据源为「腾讯」（仅日K/周K），分钟级K线不可用："
                              "请到「数据源设置」切换为「新浪」或「通达信」后再做分钟级分析。")
            if _msrc == 'tdx_minute':
                df = KLineFetcher._fetch_tdx_minute_cached(stock_code, k_type, days)
                return (df, None) if df is not None else (None, f"通达信{k_type}分钟K获取失败")
            df = KLineFetcher._fetch_sina_raw_cached(stock_code, k_type, days)
            return (df, None) if df is not None else (None, f"新浪{k_type}分钟K获取失败")
        # 日K/周K：数据源按配置分流（腾讯前复权默认；新浪模式未复权）
        if KLineFetcher._kline_source_for(k_type) == 'sina_raw':
            df = KLineFetcher._fetch_sina_raw_cached(stock_code, k_type, days)
            return (df, None) if df is not None else (None, f"新浪{k_type}K获取失败")
        key = get_cache_key(stock_code, k_type, days)

        # 1) 热层（内存 TTL 300s）
        df = get_cached(key)
        if df is None:
            # 2) 磁盘层（重启/重复扫描不重抓历史）
            disk_df = KLineFetcher._load_kline_disk(key)
            if disk_df is not None:
                set_cache(key, disk_df)  # 载入内存并带时间戳
                df = disk_df

        if df is not None:
            # 验证 df 列访问可用（BlockManager 损坏时直接删除缓存重抓）
            try:
                latest_date = pd.to_datetime(df['trade_time']).max()
            except Exception:
                logger.warning(f"K线缓存内部损坏，删除并重抓: {stock_code}")
                try:
                    os.remove(get_kline_disk_path(key))
                except Exception:
                    pass
                set_cache(key, None)
                df = None

        if df is not None:
            # 复权标记校验：仅日K/周K(腾讯前复权源)需校验 _qfq_flag；
            # 分钟级K线(新浪未复权)已在上方短路、新浪日K/周K 走内存缓存，
            # 均不落盘，故不会出现无标记 pkl。脏缓存(无标记)触发整段重抓腾讯前复权。
            if k_type in (240, 1200):
                if '_qfq_flag' not in df.columns or not bool(int(df['_qfq_flag'].iloc[0])):
                    logger.warning(f"K线缓存无前复权标记(脏/旧格式)，整段重抓: {stock_code}")
                    df = None

        if df is not None:
            today = datetime.now().date()
            now_time = datetime.now().time()
            # 3a) 未来日期（时钟偏差）-> 零网络
            if latest_date.date() > today:
                if not hasattr(KLineFetcher, '_cache_hit_log'):
                    KLineFetcher._cache_hit_log = 0
                KLineFetcher._cache_hit_log += 1
                return df, None
            # 3a') 今日 Bar 且仍在交易中（未收盘）-> 盘中 Bar 本就不完整，
            #      两次查询天然可能不同属正常，直接复用缓存避免重复抓取整段。
            if latest_date.date() == today and now_time < datetime(2000, 1, 1, 15, 0).time():
                if not hasattr(KLineFetcher, '_cache_hit_log'):
                    KLineFetcher._cache_hit_log = 0
                KLineFetcher._cache_hit_log += 1
                return df, None
            # 3a'') 今日 Bar 且已收盘（≥15:00）-> 当日日线已最终，直接复用缓存，
            #        跳过增量刷新。这是全市场扫描重复执行的核心加速点：
            #        避免对 5000+ 只股票逐只发网络请求（per-source 限速 0.5s/次 → 串行 ~42min）。
            #        ⚠️ 但「缓存里有今日 bar」≠「它已是最终值」：**盘中扫描**同样会把
            #        当时的实时快照落盘（半天成交量），盘后若无条件复用，量能/振幅类因子
            #        会**静默**用错数据。故先用批量实时行情核对末根，不一致即用终值替换。
            if latest_date.date() == today and now_time >= datetime(2000, 1, 1, 15, 0).time():
                _fixed = KLineFetcher._replace_today_bar_if_stale(stock_code, df)
                if _fixed is not None:
                    return KLineFetcher._cache_and_save(
                        key, _fixed, qfq=(k_type in (240, 1200)), days=days), None
                if not hasattr(KLineFetcher, '_cache_hit_log'):
                    KLineFetcher._cache_hit_log = 0
                KLineFetcher._cache_hit_log += 1
                return df, None
            # 3a''') 缓存最新日期 < 今天时，检查 [last_date+1, check_end] 之间是否有已收盘的交易日。
            #        - 周末（周六/周日）：自然无新交易日 → 直接复用缓存
            #        - 交易日盘中（<15:00）：今日 Bar 未收盘不完整，不计入"新交易日"，
            #          check_end = 昨天 → 若缓存已有上一交易日数据，直接复用缓存，
            #          避免交易日上午重复扫描每只股票都发一次无效的增量请求
            #        - 交易日盘后（≥15:00）：今日已收盘，check_end = 今天
            from datetime import timedelta as _td
            _check_end = today - _td(days=1) if now_time < datetime(2000, 1, 1, 15, 0).time() else today
            _has_new_trading_day = False
            _check_d = latest_date.date() + _td(days=1)
            while _check_d <= _check_end:
                if _check_d.weekday() < 5:  # 周一~周五 = 交易日（不处理节假日，节假日拉到空数据无害）
                    _has_new_trading_day = True
                    break
                _check_d += _td(days=1)
            if not _has_new_trading_day:
                if not hasattr(KLineFetcher, '_cache_hit_log'):
                    KLineFetcher._cache_hit_log = 0
                KLineFetcher._cache_hit_log += 1
                return df, None
            # 3b) 增量刷新：无论间隔多久，都**只拉取 [最后一天+1, 今天] 的新增日线**
            #     通过重叠日比值 r 把整段旧序列对齐到今日锚点，避免前复权回朔假跳变。
            #     取代旧逻辑「>2 天就整段全量重抓」——那正是全市场扫描越歇越慢的瓶颈。
            # 3b-0) **快速通道**：用批量实时行情补当日 bar（全市场 ~65 次请求，
            #       而非逐只 5200 次）。判据：`缓存末尾收盘 == 实时行情昨收` ——
            #       相等 ⇒ 只落后 1 个交易日 且期间**无除权**（有除权则昨收已被调整，
            #       必不等），此时尾巴重叠日的复权比 r 恒为 1，拼接数学安全。
            #       不等 ⇒ 落后多日（中间那根实时行情拿不到）或已除权 → 落到 3b-1 逐只。
            #       批量池不可用（构建失败/未收盘）时同样自动落到 3b-1，不影响正确性。
            _bar = RealtimeBarPool.get(stock_code)
            if _bar is not None:
                try:
                    _last_close = float(df['close'].iloc[-1])
                    if abs(float(_bar['prev_close']) - _last_close) <= 1e-6:
                        _cols = ('trade_time', 'open', 'close', 'high', 'low', 'volume')
                        _tail = pd.DataFrame([
                            {c: df[c].iloc[-1] for c in _cols},
                            {c: _bar[c] for c in _cols},
                        ])
                        _m = KLineFetcher.incremental_merge(df, _tail)
                        if _m is not None and len(_m) > len(df):
                            return KLineFetcher._cache_and_save(
                                key, _m, qfq=(k_type in (240, 1200)), days=days), None
                except Exception as e:
                    logger.debug('批量当日 bar 拼接失败(%s)，回退逐只: %s', stock_code, e)

            gap = max(0, (today - latest_date.date()).days)
            fetch_days = max(gap + 5, 5)
            try:
                tail, _ = KLineFetcher._fetch_native_tail(stock_code, fetch_days)
            except Exception as e:
                logger.warning(f"增量尾巴抓取失败({stock_code}): {e}")
                tail = None
            merged = None
            if tail is not None:
                try:
                    merged = KLineFetcher.incremental_merge(df, tail)
                except Exception as e:
                    logger.warning(f"增量拼接失败({stock_code}): {e}")
                    merged = None
            if merged is not None:
                return KLineFetcher._cache_and_save(key, merged, qfq=(k_type in (240, 1200)), days=days), None
            # 3c) 增量失败(源异常/重叠缺失/除权冻结块) -> 整段重抓兜底（前复权单一口径）
            fresh, err = KLineFetcher._fetch_full(stock_code, k_type, days)
            if fresh is not None:
                return KLineFetcher._cache_and_save(key, fresh, qfq=(k_type in (240, 1200)), days=days), None
            # 全失败（通达信与腾讯前复权均失败）-> 保留旧缓存 + 告警（不崩溃、不污染）
            logger.warning(f"K线增量+整段抓取均失败({stock_code})，使用旧缓存，行情可能过期")
            return df, None

        # 4) 无缓存 -> 整段抓取（首次）
        fresh, err = KLineFetcher._fetch_full(stock_code, k_type, days)
        if fresh is not None:
            return KLineFetcher._cache_and_save(key, fresh, qfq=(k_type in (240, 1200)), days=days), None

        return None, err or "所有数据源均失败"

    # ───────────────────────────────────────────────────────────────────
    # 统一日线前复权缓存（按代码维度，不限天数）
    # 回测与全市场扫描共用同一 store：扫描写入、回测读取，
    # 取代独立的 backtest_data_cache_qfq.pkl 大文件，免手动维护。
    # ───────────────────────────────────────────────────────────────────
    @staticmethod
    def _load_qfq_daily_disk(code):
        """从统一 store 载入按代码维度的全历史日线前复权。命中返回 df，否则 None。"""
        path = get_qfq_daily_disk_path(code)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, 'rb') as f:
                obj = pickle.load(f)
            if isinstance(obj, dict) and 'trade_time' in obj and '_qfq_flag' in obj:
                df = pd.DataFrame(obj)
                df['trade_time'] = pd.to_datetime(df['trade_time'])
                if len(df) > 0:
                    return df
        except Exception:
            pass
        return None

    @staticmethod
    def _save_qfq_daily_disk(code, df):
        """把日线前复权合并落盘到统一 store（dict 格式 + _qfq_flag 标记）。

        关键修复：扫描仅抓有限天数(如 300 天)而回测需 1200/1500 天，二者共用同一按
        代码维度的 store。旧实现直接用「本次请求的 df」覆盖写，会把回测已补抓的全历史
        截断回短天数，且扫描/回测反复互相覆盖。改为【并集合并】——保留磁盘已有更长历史，
        仅当本次数据带来新增交易日时才用并集覆盖，保证 store 始终是真正的全历史。

        ⚠️ **本 store 明确「不限长」（2026-09-19 用户拍板）**：不做根数上限、
        不做惰性淘汰，通道给多长就存多长。实测通达信源首建即落 ~3102 根，
        连续/长上市标的可达 6000+ 根；全池 5220 只约 1.06 GB。
        这是「回测深度优先」的取舍 —— 若日后有人要加限长逻辑，请先回看这条决策，
        并注意 key 维度缓存（`{md5}.pkl`）已按 days 截尾、不承担长历史职责。
        """
        path = get_qfq_daily_disk_path(code)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            save_df = df.copy()
            save_df['_qfq_flag'] = 1
            # 整个「读旧值 + 双锚点决策 + 写入」须在锁内原子完成，避免并发写者
            # 都读到同一旧值后互相覆盖（lost update）。读旧值若在锁外，短抓取可能
            # 覆盖长抓取刚写入的权威全历史。
            with _qfq_store_lock:
                old = KLineFetcher._load_qfq_daily_disk(code)
                # 双锚点守卫：不同天数抓取(如 1200d 新锚 vs 300d 旧锚)的复权尺度不同，
                # 若按 trade_time 并集 keep='last' 合并，会把两套复权尺度混入同一缓存，
                # 遇分红导致收益/因子错位（历史 K线缓存一致性问题的变体）。
                # 正确做法：只保留"更长/更全"的一份，绝不混并——长历史=更权威的完整复权，
                # 短抓取不得覆盖长的权威缓存；新抓取更长则整体替换。
                if old is not None and len(old) > 0 and len(old) >= len(save_df):
                    save_df = old
                payload = save_df.to_dict(orient='list')
                # 原子写：先写同目录临时文件再 os.replace，任何并发读者只会看到
                # 完整旧文件或完整新文件，绝不会读到半写入的 pickle。
                tmp_path = path + '.tmp'
                with open(tmp_path, 'wb') as f:
                    pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
                os.replace(tmp_path, path)
        except Exception:
            pass

    @staticmethod
    def load_qfq_daily(code, days, offline=False):
        """统一日线前复权加载器（回测使用）。

        候选源（按顺序）：
          1. 统一 store `cache/kline/qfq_daily_{code}.pkl`（扫描/回测修复后写入，按代码维度、全历史）。
          2. 扫描内部 store `cache/kline/kline_{hash(code,240,days)}.pkl`——**这正是用户最初
             要求的「回测复用全市场扫描写的 cache/kline 缓存」**。修复前的旧扫描只写了这个
             store（qfq_daily 为空），offline 模式下由此兜底，免重扫即可命中。
        - 命中且长度 >= days：直接切片返回；
        - 不足/缺失：online 模式补抓并落盘统一 store；offline 模式缺失返回 None（不联网）。

        补抓走 `_fetch_full`（按当前数据源分发：tdx 优先、失败回退腾讯），
        **不**硬编码腾讯 —— 否则 tdx 模式下回测补抓会又撞回腾讯配额，
        且只能拿 days 根而非通达信能给的全历史。
        """
        # 1) 统一 store 优先
        df = KLineFetcher._load_qfq_daily_disk(code)
        # 2) 回退到扫描内部 store（key = get_cache_key(code, 240, days)，与扫描写入一致）
        if df is None or len(df) == 0:
            df = KLineFetcher._load_kline_disk(get_cache_key(code, 240, days))
        # 统一处理：命中但不足 / 缺失
        if df is not None and len(df) > 0:
            if len(df) >= days:
                return df.iloc[-days:].reset_index(drop=True).copy()
            if offline:
                return df
            fresh, _ = KLineFetcher._fetch_full(code, 240, days)
            if fresh is not None:
                KLineFetcher._save_qfq_daily_disk(code, fresh)
                return fresh.iloc[-days:].reset_index(drop=True).copy() if len(fresh) >= days else fresh
            return df
        if offline:
            return None
        fresh, _ = KLineFetcher._fetch_full(code, 240, days)
        if fresh is not None:
            KLineFetcher._save_qfq_daily_disk(code, fresh)
            return fresh.iloc[-days:].reset_index(drop=True).copy() if len(fresh) >= days else fresh
        return None

    @staticmethod
    def _fetch_tencent_fq(stock_code, days, source='tencent_kline', max_retry=3):
        """腾讯前复权日K - 优先使用。被墙时快速失败(source 冷却)，让上层 fallback 处理。

        容错（2026-08-14 加）：腾讯 fqkline 接口在限流/代理环境会瞬时返回
        code:0,data:[] 或异常结构，使 data 字段非 dict → 旧逻辑直接 fail；而
        单源化后已无新浪兜底，于是该票评分直接挂掉。此处对「瞬时空/异常结构/
        解析异常/网络抖动」做指数退避重试（最多 max_retry 次）；仅对「结构正常
        但 code!=0（代码无效/被墙）」这类确定性失败立即返回，交上层冷却重试逻辑。
        """
        last_err = "腾讯复权数据获取失败"
        for attempt in range(max_retry):
            # 每轮重新取入口：候选池按**配额均匀分摊**（取已用次数最少者），
            # 被 WAF 拦或配额打光的入口已拉黑，这里自然切到下一个候选。
            url = fq_endpoint()
            if url is None:
                # 本窗口所有入口都不可用 → **快速失败，不再发请求**。
                # 旧版此处会回退到首个入口去撞 501，正是日志里
                # 「冷却到期→真发一次→又 501→再冷却」死循环的成因。
                return None, ("腾讯K线配额已耗尽(单入口约%d次/%d秒)，请稍后重试；"
                              "提速方案：配置代理池(net_proxy)或改用新浪源"
                              % (_FQ_QUOTA_PER_ENDPOINT, _FQ_QUOTA_WINDOW_SEC))
            fq_endpoint_note_used(url)
            try:
                resp = HTTPClient.request(url, params={'param': f'{stock_code},day,,,{days},qfq'},
                                          timeout=5, max_attempts=1, source=source)
                data = resp.json()
            except Exception as e:
                last_err = "腾讯复权K线获取失败: %s" % e
                logger.warning("腾讯复权K线请求/解析异常(第%d次,%s,%s): %s",
                               attempt + 1, stock_code, url, e)
                # 源冷却中（WAF限流）为确定性失败：重试只会重复快速失败，
                # 立即返回让调用方走缓存兜底，避免 1s+2s 退避在 5200+ 只规模下
                # 放大成扫描分片超时（2026-09-12 去阻塞）。
                if '冷却' in str(e):
                    return None, last_err
                if attempt < max_retry - 1:
                    # WAF 是确定性拒绝且下一轮已换入口 → 短退避即可；
                    # 只有网络抖动类错误才值得指数退避（否则 5200 只规模下放大成超时）。
                    _is_waf = 'WAF' in str(e) or '501' in str(e)
                    time.sleep(0.3 if _is_waf else 1.0 * (attempt + 1))
                    continue
                return None, last_err

            # 非 dict 顶级结构：限流/代理拦截页，瞬时，重试
            if not isinstance(data, dict):
                last_err = "腾讯复权K线返回结构异常(非dict)"
                logger.warning("腾讯复权K线返回非预期结构(第%d次,%s, 实为%s): %s",
                               attempt + 1, stock_code, type(data).__name__, str(data)[:300])
                if attempt < max_retry - 1:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                return None, last_err

            # 结构正常但 code != 0：确定性失败（代码无效/被墙），不重试，交上层冷却逻辑
            if data.get('code') != 0:
                return None, "腾讯复权数据为空(code=%s)" % data.get('code')

            raw_data = data.get('data')
            if not isinstance(raw_data, dict):
                # 典型为 code=0 但 data=[]（限流/空数据）→ 瞬时，重试
                last_err = "腾讯复权K线data字段结构异常"
                logger.warning("腾讯复权K线 data 字段非dict(第%d次,%s, 实为%s): %s",
                               attempt + 1, stock_code, type(raw_data).__name__, str(raw_data)[:300])
                if attempt < max_retry - 1:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                return None, last_err

            stock_data = raw_data.get(stock_code)
            if not isinstance(stock_data, dict):
                return None, "腾讯复权数据为空(无%s字段)" % stock_code
            # 单源化：只认前复权 qfqday，绝不回退未复权 day（宁德等个别票
            # 腾讯 qfq 请求返回 day 而非 qfqday，回退会静默引入未复权脏数据）
            klines = stock_data.get('qfqday')
            if not klines:
                return None, "腾讯前复权数据为空(无qfqday字段)"

            rows = []
            for row in klines:
                if len(row) >= 6:
                    rows.append({
                        'trade_time': row[0],
                        'open': float(row[1]),
                        'close': float(row[2]),
                        'high': float(row[3]),
                        'low': float(row[4]),
                        'volume': float(row[5])
                    })

            if rows:
                fq_endpoint_mark_ok(url)      # 提升为优选，后续请求直接打这个入口
                df = pd.DataFrame(rows)
                df['trade_time'] = pd.to_datetime(df['trade_time'])
                return df.sort_values('trade_time'), None
            return None, "腾讯复权数据为空"

        return None, last_err

    # 已删除 _fetch_sina（新浪未复权兜底）与 _fetch_tencent（腾讯不复权备源，
    # 且用 raw_code 去前缀请求必崩）两个死分支（2026-07-29）。
    # 本方法只负责「腾讯前复权日K」；日K 的**第一手源**是 _fetch_tdx_fq
    # （见 _fetch_full 的分发），周K 见 fetch_weekly。

    @staticmethod
    def fetch_weekly(stock_code, weeks=60, max_retry=3):
        """周K 按源分发：tdx 源下走通达信自算复权（除权周 ±1% 内，用户拍板接受）；
        其余源走腾讯服务端 qfq（`_fetch_weekly_tencent`）。失败返回 None。

        ⚠️ 周K是「同接口不同语义」的典型：通达信侧须自算复权（bisect 锚定
        「第一个 >= 除权日的周K bar」），不能像日K那样复用 `_fetch_internal`，
        故在此显式分发。通达信失败自动回退腾讯 —— 两源在无除权周逐周一致。
        """
        if KLineFetcher._kline_source_for(1200) == 'tdx_weekly':
            try:
                from engine.data_sources.tdx import fetch_weekly_qfq
                _df, _err = fetch_weekly_qfq(stock_code, weeks)
                if _df is not None and len(_df):
                    return _df
                logger.warning("通达信周K失败(%s): %s，回退腾讯", stock_code, _err)
            except Exception as e:
                logger.warning("通达信周K模块异常(%s): %s，回退腾讯", stock_code, e)
        return KLineFetcher._fetch_weekly_tencent(stock_code, weeks, max_retry)

    @staticmethod
    def _fetch_weekly_tencent(stock_code, weeks=60, max_retry=3):
        """腾讯前复权周K（回退源）。失败返回 None，由调用方保留旧缓存或排除。

        容错（2026-08-14 加）：同 _fetch_tencent_fq，对瞬时空/异常结构做指数退避重试。
        """
        for attempt in range(max_retry):
            # 同 _fetch_tencent_fq：每轮重新取入口（候选池按配额均匀分摊），
            # 配额打光/被 WAF 的入口上一轮已拉黑 → 自动换；全打光则快速失败。
            url = fq_endpoint()
            if url is None:
                return None
            fq_endpoint_note_used(url)
            try:
                resp = HTTPClient.request(url, params={'param': f'{stock_code},week,,,{weeks},qfq'},
                                          timeout=5, max_attempts=1, source='tencent_kline')
                data = resp.json()
            except Exception as e:
                logger.warning("腾讯前复权周K请求/解析异常(第%d次,%s,%s): %s",
                               attempt + 1, stock_code, url, e)
                if attempt < max_retry - 1:
                    _is_waf = 'WAF' in str(e) or '501' in str(e)
                    time.sleep(0.3 if _is_waf else 1.0 * (attempt + 1))
                    continue
                return None
            if not isinstance(data, dict) or data.get('code') != 0:
                if attempt < max_retry - 1:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                return None
            stock_data = data.get('data', {}).get(stock_code, {})
            if not isinstance(stock_data, dict):
                if attempt < max_retry - 1:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                return None
            for k in stock_data:
                if 'week' in k.lower():
                    klines = stock_data[k]
                    if klines:
                        rows = [{'trade_time': r[0], 'open': float(r[1]), 'close': float(r[2]),
                                 'high': float(r[3]), 'low': float(r[4]), 'volume': float(r[5])}
                                for r in klines if len(r) >= 6]
                        if rows:
                            fq_endpoint_mark_ok(url)
                            df = pd.DataFrame(rows)
                            df['trade_time'] = pd.to_datetime(df['trade_time'])
                            return df.sort_values('trade_time')
        return None

    @staticmethod
    def _parse(df):
        # 兼容两种输入：
        #  - 新浪源为 list-of-lists -> pd.DataFrame 后列名是整数索引(RangeIndex，非字符串)
        #  - 其它源可能已带 'open'/'close' 等字符串列名
        # 整数列名按标准 K 线顺序映射：[日期, 开, 收, 高, 低, 量]
        cols = list(df.columns)
        if cols and not isinstance(cols[0], str):
            pos_map = {0: 'trade_time', 1: 'open', 2: 'close', 3: 'high', 4: 'low', 5: 'volume'}
            df = df.rename(columns={c: pos_map[c] for c in cols if c in pos_map})
        date_field = None
        for col in df.columns:
            if str(col).lower() in ('date', 'day', 'time', 'trade_time'):
                date_field = col
                break
        if date_field is None:
            for col in df.columns:
                sample = df[col].iloc[0] if len(df) > 0 else ''
                if isinstance(sample, str) and ('-' in sample or '/' in sample):
                    try:
                        pd.to_datetime(sample)
                        date_field = col
                        break
                    except Exception:
                        continue
        if date_field is None:
            raise Exception("未找到日期字段")
        df['trade_time'] = pd.to_datetime(df[date_field])
        for col in ['open', 'high', 'low', 'close', 'volume']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')
        return df.sort_values('trade_time')


class MarketBreadthFetcher:
    # 东财多镜像：push2 主站偶发被远端断连(RemoteDisconnected)，push2delay 等同源接口可用，
    # 逐个尝试直到拿到数据，避免单域名抖动就静默退回 0.5。
    _HOSTS = [
        "https://push2.eastmoney.com/api/qt/ulist.np/get",
        "https://push2delay.eastmoney.com/api/qt/ulist.np/get",
        "https://82.push2.eastmoney.com/api/qt/ulist.np/get",
        "https://push2his.eastmoney.com/api/qt/ulist.np/get",
    ]

    @staticmethod
    def fetch():
        global _breadth_cache, _breadth_cache_time
        now = time.time()
        with _breadth_lock:
            if _breadth_cache and (now - _breadth_cache_time) < BREADTH_CACHE_TTL:
                return _breadth_cache['up'], _breadth_cache['down'], _breadth_cache['source']
        params = {'fltt': '2', 'fields': 'f2,f3,f4,f12,f14,f104,f105,f106', 'secids': '1.000001,0.399001'}
        headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://quote.eastmoney.com/'}
        last_err = None
        for url in MarketBreadthFetcher._HOSTS:
            try:
                resp = requests.get(url, params=params, headers=headers, timeout=5)
                data = resp.json()
                if data.get('data') and data['data'].get('diff'):
                    diffs = data['data']['diff']
                    up = sum(d.get('f104', 0) for d in diffs)
                    down = sum(d.get('f105', 0) for d in diffs)
                    if up > 0 or down > 0:
                        with _breadth_lock:
                            _breadth_cache = {'up': up, 'down': down, 'source': '沪深合计'}
                            _breadth_cache_time = now
                        return up, down, '沪深合计'
            except Exception as e:
                last_err = e
                continue
        if last_err is not None:
            logger.warning(f"涨跌家数获取失败: {last_err}")
        return None, None, None

    # 东财涨停/跌停池（push2ex）：一次请求返回 data.tc = 当日家数。
    # 涨停池实测可用；跌停池接口异常时 rc!=0/data=null，安全降级 limit_down=None。
    _SENTIMENT_POOL_URL = "https://push2ex.eastmoney.com/getTopic{pool}Pool"
    _SENTIMENT_HEADERS = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://quote.eastmoney.com/'}

    @classmethod
    def _sentiment_count(cls, pool, market_type):
        """拉指定池(zt=涨停 / dt=跌停)当日家数；失败返回 None。"""
        try:
            url = cls._SENTIMENT_POOL_URL.format(pool="ZT" if market_type == "zt" else "DT")
            params = {
                'ut': '7eea3edcaed734bea9cbfc24409ed989',
                'dpt': 'wz.ztzt' if market_type == "zt" else 'wz.dtzt',
                'Pageindex': 0, 'pagesize': 5, 'sort': 'fbt:asc',
                'date': datetime.now().strftime('%Y%m%d'),
            }
            resp = requests.get(url, params=params, headers=cls._SENTIMENT_HEADERS, timeout=5)
            data = resp.json()
            inner = (data or {}).get('data') or {}
            tc = inner.get('tc')
            if tc is None:
                return None
            return int(tc)
        except Exception as e:
            logger.debug(f"涨停/跌停池取数失败({pool}): {e}")
            return None

    @staticmethod
    def fetch_sentiment():
        """取市场情绪（涨停/跌停家数），带 TTL 缓存。

        Returns:
            (limit_up, limit_down)：两者可能为 None —— 缺数表示情绪增强不可用，
            调用方应安全降级（与现状逐位一致）。
        """
        global _sentiment_cache, _sentiment_cache_time
        now = time.time()
        with _breadth_lock:
            if _sentiment_cache and (now - _sentiment_cache_time) < BREADTH_CACHE_TTL:
                return _sentiment_cache['limit_up'], _sentiment_cache['limit_down']
        lu = MarketBreadthFetcher._sentiment_count("zt", "zt")
        ld = MarketBreadthFetcher._sentiment_count("dt", "dt")
        with _breadth_lock:
            _sentiment_cache = {'limit_up': lu, 'limit_down': ld}
            _sentiment_cache_time = now
        return lu, ld


class DataAPI:
    """数据访问门面：网络方法经数据源注册表分发，失败时回退原 data_layer 实现。

    第一刀仅引入分发层，默认配置下底层仍是原腾讯/新浪/东财实现，
    对外方法签名与产出结构完全不变（零侵入既有调用方）。

    回退策略：仅当注册表对该类别解析不到任何 provider（即注册表本身损坏）
    才回退到原 data_layer 直连实现；若 provider 已正常返回（含“无数据”），
    则尊重其结果，不再重复调用底层（避免 kline 等单源场景的二次联网）。
    """
    _registry = None

    @classmethod
    def _reg(cls):
        if cls._registry is None:
            try:
                from engine.data_sources.registry import DataSourceRegistry
                cls._registry = DataSourceRegistry.load()
            except Exception as e:
                logger.warning("数据源注册表加载失败，回退内置默认: %s", e)
                cls._registry = None
        return cls._registry

    @classmethod
    def load_stock_list(cls):
        reg = cls._reg()
        providers = reg.resolve('stock_list') if reg else []
        if providers:
            for p in providers:
                try:
                    count = p.get_stock_list()
                    if count:
                        return count
                except Exception as e:
                    logger.warning("数据源 %s 加载股票列表失败: %s", p.name, e)
            return 0
        return StockListLoader.load()

    @classmethod
    def get_realtime_quote(cls, code, ui=None):
        reg = cls._reg()
        providers = reg.resolve('realtime') if reg else []
        if providers:
            last = (None, None)
            for p in providers:
                try:
                    fields, src = p.get_realtime(code)
                    if fields is not None:
                        return fields, src
                    last = (fields, src)
                except Exception as e:
                    logger.warning("数据源 %s 实时行情失败(%s): %s", p.name, code, e)
            return last
        return RealtimeQuoteFetcher.fetch(code)

    @classmethod
    def get_kline(cls, stock_code, k_type_label, days=60):
        k_type = K_TYPE_MAP.get(k_type_label, 240)
        reg = cls._reg()
        providers = reg.resolve('kline') if reg else []
        if providers:
            last_err = None
            for p in providers:
                try:
                    df, err = p.get_kline(stock_code, k_type, days)
                    if df is not None:
                        return cls._patch_intraday_bar(df, stock_code, k_type_label), err
                    last_err = err
                except Exception as e:
                    logger.warning("数据源 %s K线失败(%s): %s", p.name, stock_code, e)
            return None, last_err
        df, err = KLineFetcher.fetch(stock_code, k_type, days)
        return (cls._patch_intraday_bar(df, stock_code, k_type_label), err) if df is not None else (None, err)

    @classmethod
    def _patch_intraday_bar(cls, df, stock_code, k_type_label='日K'):
        """盘中实时修补（2026-09-23）：把 K 线末根刷成当日实时价。

        背景：`_fetch_internal` 盘中分支为省请求量复用昨日缓存，导致分析/扫描的
        `ctx.latest_price` 取到昨日价，信号触发价随之错成昨日价（K线面板原有一层
        本地修补，但分析/扫描路径没吃到）。统一在 get_kline 出口修补一次，
        分析、扫描、面板共享同一「现价=末根」口径。

        仅对「日K + 交易时段（09:15 ≤ now < 15:00）+ 工作日」生效；盘后当日 bar 已终值、
        周末/节假日不拼假 bar；**盘前凌晨（0:00~9:15）也不拼** —— 该时段市场尚未开盘，
        实时行情仍是上一交易日冻结价，强行拼一根「今日」bar 会让用户在收盘后次日上午
        看到凭空多出一条 K 线（2026-09-24 用户反馈）。失败静默返回原 df，绝不改变调用方行为。

        取价优先走 `RealtimeBarPool.get_live`（盘中批量快照池），池不可用再逐只
        `get_realtime_quote` 兜底 —— 逐只在 WAF 整源冷却期会整批失败，导致部分股票
        末根停在昨日、触发价错成昨收（2026-10-05 用户反馈「部分股票错行」）。
        """
        try:
            if df is None or df.empty or k_type_label != '日K':
                return df
            from datetime import time as _dtime
            now = datetime.now()
            # 只收窄到真实交易时段：开盘集合竞价起（09:15）到收盘（15:00）。
            # 旧逻辑是 <15:00，把 凌晨0点~9:15 的盘前也当作「盘中」，导致未开盘时拼假 bar。
            if now.time() < _dtime(9, 15) or now.time() >= _dtime(15, 0):
                return df                      # 盘前 或 已收盘：当日 bar 非有效盘中价
            if now.weekday() >= 5:
                return df                      # 周末/节假日不编假 bar
            _bar = RealtimeBarPool.get_live(stock_code)
            if _bar is not None:
                # 批量快照池：一次扫描仅 ~65 次请求，绕开逐只实时在 WAF 整源冷却期的
                # 整批失败（逐只失败会让末根停在昨日 → 触发价/扫描价错成昨收）。
                current_price = float(_bar['close'])
                if current_price <= 0:
                    return df
                today_open = float(_bar['open']) or current_price
                today_high = float(_bar['high']) or current_price
                today_low = float(_bar['low']) or current_price
                today_volume = float(_bar['volume'])   # 手（与 K 线同单位，无需换算）
            else:
                fields, _src = cls.get_realtime_quote(stock_code)
                if not fields or len(fields) < 32:
                    return df
                current_price = float(fields[3]) if fields[3] else 0
                if current_price <= 0:
                    return df
                today_open = float(fields[1]) if fields[1] else current_price
                today_high = float(fields[4]) if fields[4] else current_price
                today_low = float(fields[5]) if fields[5] else current_price
                today_volume = float(fields[8]) if fields[8] else 0
                # 归一布局 volume 一律为「股」(新浪/通达信/腾讯同口径)，K线为「手」(1手=100股)
                if today_volume > 0:
                    today_volume = today_volume / 100
            df = df.copy()
            last_date = pd.to_datetime(df['trade_time']).max()
            if last_date.date() == now.date():
                # 当日 bar 已存在 → 刷新 OHLCV（保留历史，量能柱与现价同步）
                idx = df['trade_time'].idxmax()
                df.loc[idx, 'close'] = current_price
                df.loc[idx, 'high'] = max(float(df.loc[idx, 'high']), today_high)
                df.loc[idx, 'low'] = min(float(df.loc[idx, 'low']), today_low)
                if today_volume > 0:
                    df.loc[idx, 'volume'] = today_volume
            else:
                # 缓存末根为昨日（或更早）→ 拼接当日盘中 bar，使末根 = 现价
                new_row = pd.DataFrame([{
                    'trade_time': pd.Timestamp(now.date()),
                    'open': today_open,
                    'high': max(today_high, current_price),
                    'low': min(today_low, current_price),
                    'close': current_price,
                    'volume': today_volume,
                }])
                # 对齐其它可能存在的列（如 _qfq_flag），缺失列填默认
                for _c in df.columns:
                    if _c not in new_row.columns:
                        new_row[_c] = df[_c].iloc[-1]
                df = pd.concat([df, new_row[df.columns]], ignore_index=True)
            return df
        except Exception as e:
            logger.debug("盘中实时修补失败(%s)，按原K线返回: %s", stock_code, e)
            return df

    @classmethod
    def get_weekly_kline(cls, stock_code, weeks=60):
        # 周K 保持原直连实现（不经注册表，避免引入缓存语义差异）
        return KLineFetcher.fetch_weekly(stock_code, weeks)

    @classmethod
    def get_market_breadth(cls):
        reg = cls._reg()
        providers = reg.resolve('market_breadth') if reg else []
        if providers:
            last = (None, None, None)
            for p in providers:
                try:
                    up, down, src = p.get_market_breadth()
                    if up is not None:
                        return up, down, src
                    last = (up, down, src)
                except Exception as e:
                    logger.warning("数据源 %s 市场宽度失败: %s", p.name, e)
            return last
        return MarketBreadthFetcher.fetch()
    @staticmethod
    def get_stock_code(query):
        q = query.strip()
        if q.startswith(('sh', 'sz')): return q
        code = state.name_to_code.get(q)
        if code is not None:
            return code
        # 兼容 Sina API 中部分股票名称带空格的情况（如 "五 粮 液"）
        q_compact = q.replace(' ', '')
        for name, c in state.name_to_code.items():
            if name.replace(' ', '') == q_compact:
                return c
        # 模糊兜底：名称包含查询子串（如输入"茅台"匹配"贵州茅台"）
        for name, c in state.name_to_code.items():
            if q_compact and q_compact in name.replace(' ', ''):
                return c
        return None
    @staticmethod
    def get_stock_name(code):
        if code in state.code_to_name: return state.code_to_name[code]
        raw_code = code.replace('sh', '').replace('sz', '')
        if raw_code in state.raw_code_to_name: return state.raw_code_to_name[raw_code]
        return code
    @staticmethod
    def format_realtime(fields, code):
        # 逐字段安全解析：单个字段非法只以占位符替代，不再让整段行情因一处异常而空白；
        # 同时记录 warning 便于排查脏数据源。
        def _safe_num(raw, label):
            if raw is None or raw == '':
                return "—"
            try:
                return f"{int(float(raw)):,}"
            except (ValueError, TypeError):
                logger.warning("format_realtime: %s 字段无法解析为数字: %r（已用占位符替代）", label, raw)
                return "—"

        def _get(idx):
            return fields[idx] if len(fields) > idx else "—"

        lines = [
            f"📊 {_get(0)}（{code}）实时行情", "=" * 40,
            f"当前价格：{_get(3)} 元", f"今日开盘：{_get(1)} 元",
            f"昨日收盘：{_get(2)} 元", f"今日最高：{_get(4)} 元",
            f"今日最低：{_get(5)} 元", f"成交量：{_safe_num(_get(8), '成交量')} 股",
            f"成交额：{_safe_num(_get(9), '成交额')} 元", f"更新时间：{_get(30)} {_get(31)}",
        ]
        return "\n".join(lines)



