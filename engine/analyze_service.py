#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""A 股分析流程核心（自 UI 层上移，2026-09-12，B27 方案 1）。

来源与用途
----------
`analyze_stock()` 由 `ui/web_api.py::WebAPI.analyze()`（原 :561-818，258 行）的
**A 股核心**上移而来，供桌面端与 H5 服务端共用一份实现。

上移动机（两条）：
1. **单一真相源**：H5 服务端按 §2.8「engine 可直接 import」设计；若服务端另写编排，
   会与桌面实现分叉（F-303 明令警告的同类问题）。
2. **拔掉 R-09 全局态**：原 `analyze()` 读 `self._market_type`（进程级可变状态），
   迁移目标要求「同账号并发不同 market 不串市场」（F-401 验收）。本函数只做 A 股，
   市场分流由调用方在**进入前**决定，不再读任何全局态。

与 UI 层的分工（刻意保留在 UI 侧的分支）
----------------------------------------
通过**钩子**在同原位调用，从而保持行为与调用顺序不变：
  `authorize`       → 原 `can_use('query')`（授权准入；B25 已单列，不在 F-301 范围）
  `is_basic_mode`   → 原 `self._is_basic_mode()`
  `basic_preview`   → 原 `self._analyze_basic_preview(...)`
  `on_quota`        → 原 `if not _skip_quota: consume_use()`
钩子缺省为 None（服务端不传），即跳过对应分支。`discipline`（纪律落册）不在本函数内，
由调用方在拿到 `reportData` 后自行落册并补进返回体。

⚠️ 行为等价性
--------------
上移与 `analyze()` 的等价性由**真实数据 A/B 对拍**证明（同一数据源、同一只票，
旧 `WebAPI.analyze` 与本函数的 `reportData` 逐字段比对），见
`tests/h5_skeleton/test_f301_analyze.py`。
"""

import logging
import time
from datetime import datetime, timedelta

from engine import quant_config
from engine.data_layer import DataAPI, K_TYPE_MAP
from engine.report_data import build_report_data
from engine.trading_pipeline import TradingPipeline

logger = logging.getLogger(__name__)
diag_logger = logging.getLogger("M-Bull.diag")


def analyze_stock(
    code,
    *,
    k_type='日K',
    name='',
    entry_price=None,
    bars_held=0,
    up_count=None,
    down_count=None,
    direction='long',
    scheme_name=None,
    authorize=None,
    is_basic_mode=None,
    basic_preview=None,
    on_quota=None,
):
    """执行 A 股完整分析 pipeline，返回结构化报告数据（不含 `discipline`）。

    参数与 `WebAPI.analyze()` 一致，另外把原 `self.*` 依赖显式化为钩子（见模块 docstring）。
    """
    code = (code or '').strip()
    if not code:
        return {'error': '请输入代码'}

    logger.info(f"[analyze] 开始分析 code={code} market=stock k_type={k_type} direction={direction} scheme_name={scheme_name}")
    _t0 = time.time()

    def _ts(tag):
        diag_logger.info(f"[diag-timing][A股] {tag} +{time.time() - _t0:.2f}s code={code}")

    _ts('enter')

    # A股数据源限制：腾讯源无分钟级K线，分钟级分析明确报错（双保险，前端已按数据源过滤周期）
    if k_type in ('1分钟', '5分钟', '15分钟', '30分钟', '60分钟'):
        try:
            from engine.data_layer import KLineFetcher
            if KLineFetcher._kline_source_for(K_TYPE_MAP.get(k_type, 15)) is None:
                return {'error': f"当前数据源为「腾讯」（仅日K/周K），无法做 {k_type} 级分析："
                                 f"请到「数据源设置」切换为「新浪（未复权）」后再试。"}
        except Exception:
            pass

    # 方案选择：前端 Web 入口全部显式传 scheme_name（方案驱动下拉），走 A 路径。
    # B/C 兜底路径仅 API 直调/无下拉入口触发，保留但简化——不再记无用日志。
    _used_name = None
    _used_label = None
    try:
        _scheme_name = scheme_name
        # 身份校验：显式 scheme_name 必须是 A股方案，否则放弃→自动匹配
        if _scheme_name:
            _meta = quant_config.get_scheme_meta(_scheme_name) or {}
            if _meta.get('market') not in ('stock', None):
                _scheme_name = None
        if not _scheme_name:
            _cur_name = quant_config.get_current_scheme_name()
            if _cur_name:
                _cur_meta = quant_config.get_scheme_meta(_cur_name) or {}
                if _cur_meta.get('market') == 'stock':
                    _cur_period = _cur_meta.get('period')
                    if not _cur_period or _cur_period == (k_type or '日K'):
                        _scheme_name = _cur_name
        _scfg = quant_config.load_market_scheme(
            'stock', 'long', k_type or '日K',
            force_reload=True, scheme_name=_scheme_name, strict_period=True)
        # 记录"本次分析实际使用的方案"：必须在 load_market_scheme 之后读取，
        # 因为 load_market_scheme 内部可能因 period 不匹配拒绝显式 _scheme_name，
        # 转而用 resolve_scheme 找到该周期的正确方案，并更新 _CURRENT（仅 _explicit 分支）。
        # 对 strict_period=True 的自动解析分支，_CURRENT 不会被自动更新，
        # 此时回退 quant_config.resolve_scheme(market, dir, period) 拿到实际命中的方案名。
        _actual_name = quant_config.get_current_scheme_name()
        # 如果 resolve_scheme 精确命中某周期方案但非 _explicit 路径（_CURRENT 未刷新），
        # 手动再按周期解析一次，拿到真实命中的方案名
        if _actual_name is None:
            _actual_name = quant_config.resolve_scheme('stock', 'long', k_type or '日K', strict=False)
        _used_name = _actual_name
        # 统一只用方案名(name)，不再取 label（label 已废弃，name 即唯一显示名）
        _used_label = _used_name
        _used_label_alt = None
        if _scfg is None:
            # 初级模式未配置方案：仍放行「指标解读」预览（查看含义不依赖因子/方案）
            if is_basic_mode is not None and is_basic_mode():
                if basic_preview is not None:
                    return basic_preview(code, name, k_type)
            return {'error': f"未配置 A股·{k_type or '日K'} 方案：量化分析按所选周期评分，请到「模型配置」创建该周期方案。"}
    except quant_config.ConfigIncompleteError as e:
        return {'error': f"配置不完整: {'、'.join(e.missing)}，请到「模型配置」补全。"}
    except Exception as e:
        logger.exception("A股方案加载异常")
        return {'error': f"方案加载失败: {e}"}

    # 授权检查（钩子：授权归属见 B25，不在 F-301 范围）
    if authorize is not None:
        allowed, reason = authorize()
        _ts('auth_ok')
        if not allowed:
            return {'error': f"授权受限: {reason}"}

    stock_name = name.strip() or DataAPI.get_stock_name(code)
    _ts('name_ok')

    try:
        ep = float(entry_price) if entry_price not in (None, '') else 0
        if ep <= 0:
            ep = 0
    except (ValueError, TypeError):
        ep = 0
    try:
        bh = int(bars_held) if bars_held not in (None, '') else 0
    except (ValueError, TypeError):
        bh = 0

    try:
        # ── 因子评分使用用户所选周期（与全市场扫描/期货分支对齐）──
        # 各源的支持集见 KLineFetcher._load_kline_sources()：默认 tdx 全周期可用
        # （日K 前复权 / 周K 自算复权 / 分钟未复权）；tencent 仅日K/周K；sina 全周期未复权。
        # 周期决定数据粒度，方案由 (市场,long,周期) 严格匹配（上方已校验存在）。
        # 分钟级统一抓 1023 根：这是**跨源下限**（新浪 datalen 上限 1023 根），
        # 取它可保证任何数据源下缩放后的因子窗口都算得出来。
        score_k_type = k_type
        _fetch_days = 1023 if k_type in ('1分钟', '5分钟', '15分钟', '30分钟', '60分钟') else 300
        df_score, err = DataAPI.get_kline(code, score_k_type, _fetch_days)
        if err or df_score is None or df_score.empty:
            return {'error': '无法获取行情数据（当前数据源暂不可用）。请检查网络，或到「设置-数据源」切换数据源后重试。', 'detail': f"获取K线失败(评分): {err}"}
        _ts('kline_score_ok')

        # 图表用同一份数据（与评分同周期，不再额外抓日K）
        df_chart = df_score
        _ts('kline_chart_ok')
        df = df_chart if (df_chart is not None and not df_chart.empty) else df_score

        suspended = False
        warn_msg = ''
        latest_date = df['trade_time'].max()
        days_gap = (datetime.now() - latest_date).days
        if days_gap > 2:
            suspended = True
            warn_msg = f"最新K线: {latest_date.strftime('%Y-%m-%d')}（已{days_gap}天未更新，可能停牌）"

        # 市场宽度
        up_ratio = 0.5
        breadth_fetched = False
        try:
            auto_up, auto_down, _ = DataAPI.get_market_breadth()
            if auto_up is not None and auto_down is not None:
                up_count, down_count = auto_up, auto_down
        except Exception:
            pass
        try:
            if up_count is not None and down_count is not None and (up_count + down_count) > 0:
                up_ratio = up_count / (up_count + down_count)
                breadth_fetched = True
        except Exception:
            pass

        # 市场情绪（涨停/跌停家数）：仅 A 股实时分析注入，供 MarketGate 情绪修正；
        # 缺数安全降级为 None（与现状逐位一致）。additive：不进全市场扫描/回测。
        sentiment = None
        try:
            from engine.data_layer import MarketBreadthFetcher
            _lu, _ld = MarketBreadthFetcher.fetch_sentiment()
            if _lu is not None or _ld is not None:
                sentiment = {'limit_up': _lu, 'limit_down': _ld}
        except Exception:
            sentiment = None

        # ── 评分数据按周期截断：日K/周K 取近365天（与全市场扫描一致）；分钟保留全量
        #（1023根≈17个交易日，缩放后的长窗口因子需要完整序列，不截断）──
        if k_type in ('1分钟', '5分钟', '15分钟', '30分钟', '60分钟'):
            dff_score = df_score
        else:
            _cutoff = datetime.combine(datetime.now().date() - timedelta(days=365), datetime.min.time())
            dff_score = df_score[df_score['trade_time'] >= _cutoff]
        if len(dff_score) < 30:
            return {'error': f'{score_k_type}数据不足30条，无法评分'}

        data_list = [{
            'close': r['close'], 'high': r['high'], 'low': r['low'],
            'volume': r['volume'], 'date': r['trade_time'], 'open': r['open']
        } for _, r in dff_score.iterrows()]

        closes = [d['close'] for d in data_list]
        volumes = [d['volume'] for d in data_list]
        highs = [d['high'] for d in data_list]
        lows = [d['low'] for d in data_list]
        opens = [d['open'] for d in data_list]

        from engine.indicators import MACDCalculator
        from engine.factor_tech import compute_extreme_vetos
        from engine.scoring_core import build_tech, build_market_dict, _rsi_series_fast

        # 读取用户配置的 RSI 周期（默认 14），用于报告显示值和背离检测
        _rsi_period = 14
        try:
            _fcfg = quant_config.get_factor_configs() or {}
            _rsi_fcfg = _fcfg.get('rsi_value', {})
            _rsi_p = (_rsi_fcfg.get('params') or {}).get('period', 14)
            if isinstance(_rsi_p, (int, float)) and int(_rsi_p) >= 2:
                _rsi_period = int(_rsi_p)
        except Exception:
            pass
        # 统一走 scoring_core 构建 tech/market（与全市场扫描/回测完全一致），
        # 消除「手写 tech 漏键 vs build_tech 完整版」导致的因子预期分不一致。
        rsi_hist = _rsi_series_fast(closes, _rsi_period)
        accel, accel_status = MACDCalculator.calc_acceleration(closes)
        tech = build_tech(closes, volumes, highs, lows, opens, data_list, rsi_hist, (accel, accel_status), rsi_period=_rsi_period)
        market = build_market_dict(closes, volumes, up_ratio)
        # 补充 analyze 路径专有信号（build_tech 不含，但 TradingPipeline 下游会读）；
        # 传入股票代码以注入财务，驱动 finance_risk 否决项（仅 A 股）。
        tech.update(compute_extreme_vetos(closes, highs, lows, volumes, opens, data_list, tech,
                                          stock_code=code))

        ctx = TradingPipeline.execute(
            stock_code=code,
            data_list=data_list,
            up_ratio=up_ratio,
            tech=tech,
            market=market,
            has_position=ep > 0,
            entry_price=ep,
            bars_held=bh,
            breadth_fetched=breadth_fetched,
            market_type='stock',
            period=score_k_type,   # 周期透传：因子窗口按周期缩放 + 校准分桶（分钟/周K 生效）
            sentiment=sentiment,   # 涨停/跌停家数 → MarketGate 情绪修正（缺数 None 不变）
        )
        _ts('pipeline_ok')
        ctx.stock_name = stock_name
        ctx.suspended = suspended
        ctx.suspension_msg = warn_msg

        # 额度钩子（原 `if not _skip_quota: consume_use()`，调用点与顺序保持不变）
        if on_quota is not None:
            on_quota()
        report_data = build_report_data(ctx, stock_name)
        _ts('report_ok')
        # 「本次分析实际使用方案」摘要：按刚 load 完的 _safe_cfg() 实时产出，
        # 保证报告顶部方案卡的 因子数/风控启用 与评分逻辑完全一致，
        # 不依赖前端编辑器 quantData（它可能是用户另一个方案）。
        _scheme_summary = {'active_factor_count': 0, 'active_factor_names': [], 'risk_enabled': False}
        try:
            _runcfg = quant_config._safe_cfg() or {}
            _af = list(_runcfg.get('active_factors') or [])
            _scheme_summary['active_factor_count'] = len(_af)
            _scheme_summary['active_factor_names'] = [str(x) for x in _af[:5]]
            if len(_af) > 5:
                _scheme_summary['active_factor_names'].append(f'…+{len(_af) - 5}')
            # 风控/冲突惩罚任一有内容就算启用（空值/0值=未启用，与 REQUIRED_STRATEGY SSOT 一致）
            _rp = _runcfg.get('risk_params') if isinstance(_runcfg.get('risk_params'), dict) else {}
            _cp = _runcfg.get('conflict_penalty') if isinstance(_runcfg.get('conflict_penalty'), dict) else {}
            _risk_keys_nonzero = [k for k in ('maxDrawdown', 'maxLoss', 'maxPosition', 'stopLoss', 'takeProfit')
                                  if k in _rp and _rp[k] not in (None, '', 0) and (not isinstance(_rp[k], (int, float)) or _rp[k] != 0)]
            _scheme_summary['risk_enabled'] = bool(_risk_keys_nonzero or bool(_cp))
        except Exception:
            pass
        # 数据源标注：报告尾部注明当前分析周期的 K线来源，用户切换数据源后可见。
        # ⛔ 标签必须走 KLineFetcher.source_label（三源全覆盖），不可在此重新二分。
        try:
            from engine.data_layer import KLineFetcher, K_TYPE_MAP as _KTM
            _kt = k_type if k_type in _KTM else '日K'
            _src = KLineFetcher._kline_source_for(_KTM.get(_kt, 240))
            _label = KLineFetcher.source_label(_src)
            _suffix = f"\n\n> 数据来源：{_kt} · {_label}"
            if _kt not in ('日K', '周K') and _src in ('sina_raw', 'tdx_minute'):
                _suffix += '（分钟未复权，跨除权日价格可能跳变，与日K前复权不衔接）'
            report_data['report_text'] = (report_data.get('report_text') or '') + _suffix
        except Exception:
            pass
        return {'success': True, 'reportData': report_data, 'report_text': report_data.get('report_text', ''),
                'used_scheme_name': _used_name, 'used_scheme_label': _used_label,
                'used_scheme_label_alt': _used_label_alt,
                'used_scheme_period': k_type or '日K',
                'used_scheme_market': 'stock',
                'used_scheme_direction': 'long',
                'scheme_summary': _scheme_summary}
    except quant_config.ConfigIncompleteError as e:
        return {'error': f"配置不完整: {'、'.join(e.missing)}，请到「模型配置」补全。"}
    except Exception as e:
        # 授权类异常由钩子（如额度扣减）抛出时，保持与上移前一致的措辞，
        # 且不把 license 依赖引入 engine（按类名判定，避免依赖方向反转）
        if type(e).__name__ == 'LicenseDenied':
            return {'error': str(e)}
        logger.exception("analyze 失败")
        return {'error': f"分析失败: {e}"}


def analyze_futures(
    symbol,
    *,
    k_type='日K',
    name='',
    entry_price=None,
    bars_held=0,
    direction='long',
    scheme_name=None,
    now=None,
    on_quota=None,
):
    """期货分析：用期货数据源获取 K 线，复用 TradingPipeline 分析流水线。

    由 `ui/web_api.py::WebAPI._analyze_futures()`（原 :657-884，228 行）上移而来（B29 方案 1）。

    ⚠️ `now` 参数注入（时区由调用方决定）
    ------------------------------------
    过期合约判定（`YYMM < 当前的 YYMM`）与 365 天截断都依赖"当前时间"。
    engine 不得依赖服务端的时区模块（依赖方向），故由调用方注入：
      H5 服务端 → `server.core.time.now()`（**Asia/Shanghai**，§2.13-2，F-302 平台差异列明确要求）
      桌面端   → `datetime.now()`（保持原行为）
    缺省 `None` 时回退 `datetime.now()`，与迁移前一致。
    """
    from datetime import datetime as _dt

    from engine.futures_data import fetch_futures_daily, fetch_futures_minute
    from engine.futures_night import merge_night_session
    from engine.futures_pool import make_specific_contracts, parse_contract_code

    symbol = (symbol or '').strip()
    if not symbol:
        return {'error': '请输入期货品种代码'}

    # 解析合约：'rb' → 主力连续, 'jd2609' → 具体合约
    contract, month = parse_contract_code(symbol)
    if not contract:
        return {'error': f'未知期货品种或合约: {symbol}'}
    if month:
        spec = make_specific_contracts([contract], month)[0]
    else:
        spec = contract

    # 过期合约（合约月份早于当前）已交割/摘牌，无实时行情；提前给出明确提示，
    # 避免与「网络错误/数据源失败」混淆（如 cu2510/TA2510 这类 2025 年合约）。
    # 注意：回测路径走独立 fetch，不在此拦截（历史数据仍需可用）。
    # ⚠️ 当前时间由调用方注入（服务端传 Asia/Shanghai），见函数 docstring。
    _now = now or _dt.now()
    if month:
        _now_yymm = int(_now.strftime('%y%m'))
        try:
            _month_int = int(month)
        except (ValueError, TypeError):
            _month_int = None
        if _month_int is not None and _month_int < _now_yymm:
            return {'error': f'合约 {symbol} 已到期/已交割（合约月份 {month} 早于当前 {_now_yymm}），无实时行情。请改用同品种更近月份的活跃合约。'}

    stock_name = (name or '').strip() or contract.name
    _t0 = time.time()

    def _ts(tag):
        diag_logger.info(f"[diag-timing][期货] {tag} +{time.time() - _t0:.2f}s symbol={spec.symbol}")

    _ts('enter')

    try:
        ep = float(entry_price) if entry_price not in (None, '') else 0
    except (ValueError, TypeError):
        ep = 0
    try:
        bh = int(bars_held) if bars_held not in (None, '') else 0
    except (ValueError, TypeError):
        bh = 0

    try:
        logger.info(f"[_analyze_futures] 开始获取K线 symbol={spec.symbol} secid={spec.secid} k_type={k_type} direction={direction}")
        # 解析周期 → 分钟线走新浪 getFewMinLine，日线/周线走日K（getDailyKLine）
        _period_map = {'15分钟': 15, '30分钟': 30, '60分钟': 60, '1分钟': 1, '5分钟': 5}
        _is_minute = k_type in _period_map
        _period = _period_map.get(k_type)
        if _is_minute:
            # 分钟因子窗口按周期缩放(clamp 达 600/320)，300 根不够 → 与 A股分钟路径
            # 对齐抓 1023 根（新浪 getFewMinLine 上限），否则 5/15分钟下
            # relative_strength/ma_slope/bias_value 恒 0 静默失真。
            df_score = fetch_futures_minute(spec.symbol, spec.secid, period=_period, days=1023)
        else:
            df_score = fetch_futures_daily(spec.symbol, spec.secid, days=300)
            # 并入"下一交易日夜盘"bar（与 get_kline 显示的夜盘K线同口径）；
            # 下一交易日开盘后真实日K天然合并、不再重复。
            df_score = merge_night_session(df_score, spec.symbol, spec.secid)
        logger.info(f"[_analyze_futures] K线返回 rows={len(df_score) if df_score is not None else None} symbol={spec.symbol}")
        _ts('kline_ok')
        if df_score is None or df_score.empty:
            return {'error': f'期货K线数据获取失败: {symbol}'}
        if len(df_score) < 30:
            return {'error': f'{k_type}数据不足30条，无法评分'}

        # 截断到一年内
        _cutoff = _dt.combine((_now.date() if hasattr(_now, "date") else _now) - timedelta(days=365), _dt.min.time())
        dff_score = df_score[df_score['trade_time'] >= _cutoff]
        if len(dff_score) < 30:
            dff_score = df_score.tail(300)  # 不足一年就用全部

        # 构造 data_list（与股票完全同构）
        data_list = [{
            'close': r['close'], 'high': r['high'], 'low': r['low'],
            'volume': r['volume'], 'date': r['trade_time'], 'open': r['open']
        } for _, r in dff_score.iterrows()]

        closes = [d['close'] for d in data_list]
        volumes = [d['volume'] for d in data_list]
        highs = [d['high'] for d in data_list]
        lows = [d['low'] for d in data_list]
        opens = [d['open'] for d in data_list]

        from engine.indicators import MACDCalculator
        from engine.factor_tech import compute_extreme_vetos
        from engine.scoring_core import build_tech, build_market_dict, _rsi_series_fast

        # 读取用户配置的 RSI 周期（默认 14），用于报告显示值和背离检测
        _rsi_period = 14
        try:
            _fcfg = quant_config.get_factor_configs() or {}
            _rsi_fcfg = _fcfg.get('rsi_value', {})
            _rsi_p = (_rsi_fcfg.get('params') or {}).get('period', 14)
            if isinstance(_rsi_p, (int, float)) and int(_rsi_p) >= 2:
                _rsi_period = int(_rsi_p)
        except Exception:
            pass
        rsi_hist = _rsi_series_fast(closes, _rsi_period)
        accel, accel_status = MACDCalculator.calc_acceleration(closes)
        tech = build_tech(closes, volumes, highs, lows, opens, data_list, rsi_hist, (accel, accel_status), rsi_period=_rsi_period)
        # 期货无市场宽度概念，up_ratio 用 0.5 中性值
        up_ratio = 0.5
        market = build_market_dict(closes, volumes, up_ratio)
        tech.update(compute_extreme_vetos(closes, highs, lows, volumes, opens, data_list, tech))

        # 按市场+方向+周期加载对应方案（期货多单/空单隔离，且叠加该周期专属预设）。
        _fused_name = None
        _fused_label = None
        try:
            _fscheme = scheme_name
            # 身份校验：显式 scheme_name 必须是期货方案且方向匹配，否则放弃→自动匹配
            if _fscheme:
                _fmeta_ex = quant_config.get_scheme_meta(_fscheme) or {}
                _fm_market = _fmeta_ex.get('market')
                _fm_dir = _fmeta_ex.get('direction')
                if _fm_market not in ('futures', None) or (_fm_dir and _fm_dir != direction):
                    _fscheme = None
            if not _fscheme:
                _fcur = quant_config.get_current_scheme_name()
                if _fcur:
                    _fmeta = quant_config.get_scheme_meta(_fcur) or {}
                    if _fmeta.get('market') == 'futures' and _fmeta.get('direction') == direction:
                        _fper = _fmeta.get('period')
                        if not _fper or not k_type or _fper == k_type:
                            _fscheme = _fcur
            quant_config.load_market_scheme('futures', direction, k_type, force_reload=True, scheme_name=_fscheme, strict_period=True)
            _fused_name = quant_config.get_current_scheme_name() or _fscheme
            _fused_label = _fused_name
            _fused_label_alt = None
            if _fused_name is None:
                dir_label = '多单' if direction == 'long' else '空单'
                return {'error': f"未配置 期货·{dir_label}·{k_type} 方案：请到「模型配置」创建该周期方案。"}
        except quant_config.ConfigIncompleteError as e:
            return {'error': f"配置不完整: {'、'.join(e.missing)}，请到「模型配置」补全。"}
        except Exception as e:
            logger.exception("期货方案加载异常")
            return {'error': f"期货方案加载失败: {e}"}

        ctx = TradingPipeline.execute(
            stock_code=symbol,
            data_list=data_list,
            up_ratio=up_ratio,
            tech=tech,
            market=market,
            has_position=ep > 0,
            entry_price=ep,
            bars_held=bh,
            breadth_fetched=True,  # 期货无市场宽度，标记为已获取避免重试
            direction=direction,
            market_type='futures',
            period=k_type,
        )
        _ts('pipeline_ok')
        ctx.stock_name = stock_name
        ctx.suspended = False
        ctx.suspension_msg = ''

        if on_quota is not None:
            on_quota()
        report_data = build_report_data(ctx, stock_name)
        _ts('report_ok')
        # 期货分析也补方案摘要（与 A股 同结构）
        _scheme_summary = {'active_factor_count': 0, 'active_factor_names': [], 'risk_enabled': False}
        try:
            _runcfg = quant_config._safe_cfg() or {}
            _af = list(_runcfg.get('active_factors') or [])
            _scheme_summary['active_factor_count'] = len(_af)
            _scheme_summary['active_factor_names'] = [str(x) for x in _af[:5]]
            if len(_af) > 5:
                _scheme_summary['active_factor_names'].append(f'…+{len(_af) - 5}')
            _rp = _runcfg.get('risk_params') if isinstance(_runcfg.get('risk_params'), dict) else {}
            _cp = _runcfg.get('conflict_penalty') if isinstance(_runcfg.get('conflict_penalty'), dict) else {}
            _risk_keys_nonzero = [k for k in ('maxDrawdown', 'maxLoss', 'maxPosition', 'stopLoss', 'takeProfit')
                                  if k in _rp and _rp[k] not in (None, '', 0) and (not isinstance(_rp[k], (int, float)) or _rp[k] != 0)]
            _scheme_summary['risk_enabled'] = bool(_risk_keys_nonzero or bool(_cp))
        except Exception:
            pass
        # 追加期货元信息
        report_data['market_type'] = 'futures'
        report_data['period'] = k_type
        # 该周期是否使用了用户专属预设（区别于 base 默认）
        _cur_name = quant_config.get_current_scheme_name()
        report_data['period_preset'] = 'custom' if k_type in (quant_config.get_scheme_periods(_cur_name or '')) else 'base'
        # 分钟周期因子窗口已按周期缩放，但校准沿用日线（未独立 IC 验证）→ 标注指示性
        report_data['period_calibration'] = 'indicative' if k_type not in ('日K', '周K') else 'calibrated'
        report_data['futures_contract'] = {
            'symbol': contract.symbol,
            'name': contract.name,
            'exchange': contract.exchange,
            'multiplier': contract.multiplier,
            'margin_rate': contract.margin_rate,
            'tick_size': contract.tick_size,
            'main_code': spec.main_code,
            'contract_month': spec.contract_month,
            'is_continuous': spec.is_continuous,
        }
        return {'success': True, 'reportData': report_data, 'report_text': report_data.get('report_text', ''),
                'used_scheme_name': _fused_name, 'used_scheme_label': _fused_label,
                'used_scheme_label_alt': _fused_label_alt,
                'used_scheme_period': k_type or '日K',
                'used_scheme_market': 'futures',
                'used_scheme_direction': direction,
                'scheme_summary': _scheme_summary}
    except quant_config.ConfigIncompleteError as e:
        return {'error': f"配置不完整: {'、'.join(e.missing)}，请到「模型配置」补全。"}
    except Exception as e:
        if type(e).__name__ == 'LicenseDenied':
            return {'error': str(e)}
        logger.exception("_analyze_futures 失败")
        return {'error': f"期货分析失败: {e}"}
