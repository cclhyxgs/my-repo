#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""全市场扫描的「纯计算内核」（无 UI 依赖）。

设计目的：让多核 worker 进程只需 import 本模块即可算分，
避免把整个 UI 层（ui/web_api.py 等）带进子进程拖慢 spawn 启动。

`_quick_score_core` 是全市场扫描评分的**唯一实现**，调用方：
  - ui/web_api.py（A股扫描 / 期货扫描的进程池 target）
  - server/adapters/engine_bridge.py（H5 侧单只评分薄委托）
函数为纯函数、不持 self/UI 引用，故可在子线程/子进程独立运行
（进程池要求 target 可 pickle）。
"""

from datetime import datetime, timedelta


from engine.scoring_core import compute_stock_score, _rsi_series_fast


def _tech_scalar(v):
    """把 numpy 标量/内置标量转成原生 JSON 可序列化类型。

    必须显式转换（而非依赖缓存写入的 json.dump(default=str) 兜底）：
    default=str 会把 numpy 数值 stringify 成字符串，而
    unified_entry_logic._eval_indicator 对 isinstance(v, str) 判 None，
    会导致「高级技术指标二级过滤」条件恒不满足、误剔除全部股票。
    """
    if v is None:
        return None
    try:
        import numpy as np
        if isinstance(v, np.generic):
            return v.item()
    except Exception:
        pass
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return float(v)
    return str(v)


def build_tech_snapshot(tech, market, adx_result, factor_values=None):
    """构造技术指标快照（纯标量，JSON 可序列化），供「高级模式技术指标二级过滤」即时判定。

    字段覆盖 unified_entry_logic._evaluate_tech_conditions / _evaluate_tech_signal
    依赖的全部信号来源：
      - tech 数值指标键（_NUM_IND_LABELS，做阈值比较）+ 信号键 kdj_signal / macd_status / breakout_signal；
      - market.ma_arrangement / volume_price（均线排列、放量等布尔信号）；
      - adx_state.adx（ADX趋势确认）；
      - factor_values 中的自定义公式因子（cf_ 前缀），供按自定义指标过滤。
    缺键置 None → _eval_indicator 判 None → AND 下"不满足"，不误放行。

    注意：快照构建对所有模式一视同仁（高级做二级过滤用，初级按触发等级分组展示，
    快照字段对初级无副作用、仅多存数据）。
    """
    from engine.unified_entry_logic import UnifiedEntryLogic
    ind_keys = UnifiedEntryLogic._NUM_IND_LABELS
    signal_keys = ('kdj_signal', 'macd_status', 'breakout_signal')
    tech_sub = {}
    for k in list(ind_keys.keys()) + list(signal_keys):
        if tech.get(k) is not None:
            tech_sub[k] = _tech_scalar(tech.get(k))
    # 自定义公式因子的原始值（当前方案启用因子，键为 cf_<公式名>）：判定器对任意键做数值比较，
    # 入快照后「技术指标筛选」即可按自定义指标设阈值。只收 cf_ 前缀，避免内置因子原始值
    # 混进 tech 命名空间与内置键（如 macd/rsi_14）产生同名歧义。
    if factor_values:
        for fk, fv in factor_values.items():
            if isinstance(fk, str) and fk.startswith('cf_') and fv is not None:
                tech_sub[fk] = _tech_scalar(fv)
    market_map = market or {}
    return {
        'tech': tech_sub,
        'market': {
            'ma_arrangement': _tech_scalar(market_map.get('ma_arrangement')),
            'volume_price': _tech_scalar(market_map.get('volume_price')),
        },
        'adx': {'adx': _tech_scalar((adx_result or {}).get('adx', 0))},
    }


def _quick_score_core(stock_code, up_ratio=0.5, sector_map=None, direction='long', period='日K'):
    """单只股票的因子预期分计算（全市场扫描评分的唯一实现，见模块 docstring）。

    返回 result dict 或 None。不含任何 self/UI 引用，可在子线程/子进程独立运行。

    数据一律走 DataAPI.get_kline 实时抓取（内部含磁盘缓存增量刷新与限流保护），
    不使用任何打包内置的离线冻结数据。

    direction: 评分视角，'long'=多头，'short'=空头（期货扫描方向化）。
    period: 分析周期（'日K'/'周K'），透传到评分内核与数据抓取。
    """
    sector_map = sector_map or {}
    try:
        import time
        from engine.data_layer import DataAPI
        df = None
        # 实时抓取；腾讯免费接口偶发 WAF / 网络抖动 → 短暂退避后重试一次，
        # 避免单只股票因瞬时限流被整只丢弃。
        for _attempt in range(2):
            df, err = DataAPI.get_kline(stock_code, period, 300)
            if not err and df is not None and not df.empty:
                break
            if _attempt == 0:
                time.sleep(1)
        if df is None or df.empty:
            return None
        if len(df) < 30:
            return None

        _cutoff = datetime.combine(datetime.now().date() - timedelta(days=365), datetime.min.time())
        dff = df[df['trade_time'] >= _cutoff]
        if len(dff) < 30:
            return None

        # 性能优化：用 to_dict('records') 替代 iterrows()，速度提升约5-10倍
        # iterrows() 每行创建 Series 对象，开销极大；to_dict 直接提取原生 Python 值
        data_list = dff[['close', 'high', 'low', 'volume', 'trade_time', 'open']].to_dict('records')
        # 字段名对齐：trade_time → date
        for _d in data_list:
            _d['date'] = _d.pop('trade_time')

        closes = [_d['close'] for _d in data_list]
        volumes = [_d['volume'] for _d in data_list]
        highs = [_d['high'] for _d in data_list]
        lows = [_d['low'] for _d in data_list]
        opens = [_d['open'] for _d in data_list]

        from engine.indicators import MACDCalculator
        from engine.signal_rating import SignalRating
        from engine import quant_config

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
        accel = MACDCalculator.calc_acceleration(closes)

        stock_score, final_result, tech, market, adx_result, tech_strength = compute_stock_score(
            closes, volumes, highs, lows, opens, data_list, up_ratio, rsi_hist, accel,
            direction=direction, period=period, rsi_period=_rsi_period)
        final_score = final_result['final_score']

        # 触发建仓等级：复用 StockClassifier.classify（内部已按初/高级分流——
        # 初级纯技术定档、高级因子分+技术定档），供扫描结果页「按触发建仓等级分组」展示。
        entry_tier, entry_tier_label = 'weak', '弱势股'
        try:
            from types import SimpleNamespace
            from engine.stock_classifier import StockClassifier
            _cls = StockClassifier.classify(
                SimpleNamespace(
                    final_score=final_score, stock_score=stock_score,
                    market=market or {}, tech=tech or {},
                    latest_price=closes[-1], adx_state=adx_result or {},
                    up_ratio=up_ratio),
                direction=direction)
            entry_tier = _cls.get('type', 'weak')
            entry_tier_label = _cls.get('type_label', entry_tier)
        except Exception:
            pass

        # 提取当前方案启用因子的原始值，供扫描结果页「因子值」展示。
        # 复用评分已算好的 tech/market，不重复联网抓K；未启用因子不在其中（方案含才可筛）。
        factor_values = {}
        try:
            from engine.score_calculator_v2 import ScoreCalculatorV2
            factor_values = ScoreCalculatorV2.extract_factors(
                tech, market, closes, highs, lows, volumes, opens, data_list, closes[-1], period) or {}
        except Exception:
            factor_values = {}

        sr = SignalRating.calculate(stock_score, tech, up_ratio, '', final_score=final_score,
                                    tech_strength=tech_strength)

        stock_name = DataAPI.get_stock_name(stock_code)
        if not stock_name or stock_name == stock_code:
            stock_name = stock_code

        # 与 MarketScan.get_sector 一致：先去 sh/sz 前缀再查裸码
        raw_code = (stock_code.replace('sh', '').replace('sz', '')
                            .replace('SH', '').replace('SZ', ''))
        sector = sector_map.get(raw_code, '') if sector_map else ''

        # 当日涨跌幅（最新收盘 vs 前一交易日收盘）：供扫描结果「涨幅」筛选
        # （典型用法：涨幅 < 9.8 排除涨停股）。数据不足时置 None，前端/筛选按"不可比较"处理。
        change_pct = None
        try:
            if len(closes) >= 2 and closes[-2]:
                change_pct = round((float(closes[-1]) / float(closes[-2]) - 1) * 100, 2)
        except Exception:
            change_pct = None

        return {
            'code': stock_code,
            'name': stock_name,
            'price': closes[-1],
            'change_pct': change_pct,
            'stock_score': stock_score,
            'final_score': final_score,
            'tech_strength': sr.get('tech_strength'),   # 供 assign_tech_stars 做绝对星级映射
            'final_rating': sr.get('score'),
            'stars': sr.get('stars', ''),
            'level': sr.get('level', ''),
            'sector': sector,
            'factor_values': factor_values,      # 当前方案启用因子的原始值
            'entry_tier': entry_tier,            # 触发建仓等级（初级分组展示）
            'entry_tier_label': entry_tier_label,
            'tech_snapshot': build_tech_snapshot(tech, market, adx_result, factor_values),  # 高级技术指标二级过滤用（含自定义指标）
        }
    except (KeyError, IndexError, TypeError, ValueError) as e:
        # 数据不足或格式异常 → 静默跳过该股票
        return None
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            "market_scan_core._quick_score_core 意外异常 stock=%s: %s", stock_code, e, exc_info=True)
        return None


def _ensure_worker_stock_list():
    """worker 线程/进程启动时确保股票列表和量化配置已加载。

    线程池模式下与主线程同进程；此处做兜底。
    每次 scan 启动时 _worker_loaded 会被重置为 False（由 _reset_worker_state），
    确保用户修改配置后重新扫描能读到最新配置。

    重要：worker 线程只 reload _MODEL（方案列表元数据），不覆盖 _cache。
    扫描入口 start_market_scan 已通过 load_market_scheme 把 _cache 设为用户选中的方案，
    此处若调用 load_config(force_reload=True) 会把 _cache 冲回 current_scheme，
    导致 worker 用错方案评分——这是「扫描与个股分析方案不一致」的根因。
    """
    global _worker_loaded
    if _worker_loaded:
        return
    try:
        from engine.data_layer import DataAPI
        DataAPI.load_stock_list()
        from engine import quant_config as _qc_w
        _qc_w.reload_model_only()  # 只更新 _MODEL，保持 _cache 为入口加载的方案
        _worker_loaded = True
    except Exception:
        pass


def _reset_worker_state():
    """每次扫描开始前重置 worker 状态，确保下次扫描重新加载配置。"""
    global _worker_loaded
    _worker_loaded = False


_worker_loaded = False


def _scan_worker(codes_chunk, up_ratio=0.5, sector_map=None, direction='long', period='日K'):
    """扫描 worker：处理一个股票分片，返回该分片的有效 result 列表。

    （线程池模式下与主线程同进程；进程池模式下亦兼容。）整体失败兜底：
    任何异常都返回 []（不抛到池外），由主进程决定跳过，不影响其它分片。
    """
    _ensure_worker_stock_list()
    try:
        results = []
        for code in codes_chunk:
            r = _quick_score_core(code, up_ratio, sector_map, direction=direction, period=period)
            if r is not None:
                results.append(r)
        return results
    except Exception:
        return []


# ── 财务筛选（全市场扫描 TopN 候选的逐票财务判定，见 .trae/documents/扫描财务筛选方案.md）──

def _fnum(v):
    """财务字段安全取数：非数值一律归 0（缺失即 0，由判定语义决定放行/剔除）。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def finance_rules_pass(rules, price, fin):
    """单票财务规则判定：True=保留，False=剔除（供 apply_finance_guard 逐票套用）。

    rules: None 或 dict。dict 键全可选（None/缺键=不限）：
      pb_max（市净率上限）/ pe_max（市盈率上限）/ eps_min（每股收益下限）/
      debt_ratio_max（资产负债率上限 %）/ mcap_min（总市值下限，单位亿）。
    语义=硬过滤：条件不满足即剔除；仅**数据缺失/不可算**才放行该规则（安全降级，不误杀）。

    爆雷硬剔除恒生效（不可配置）：每股净资产<0（资不抵债）或 净利<0（亏损）→ 剔除。
    口径与 veto_registry._finance_risk 一致；市值/EPS 用 ×10000 总量字段同口径相除，
    对绝对单位不敏感（见 tdx.fetch_finance 注释）。
    """
    if not fin or not isinstance(fin, dict):
        return True                       # 无财务数据 → 放行（查询失败分支已在调用方处理）
    jz = _fnum(fin.get('meigujingzichan') or fin.get('jingzichan'))
    jl = _fnum(fin.get('jinglirun'))
    zg = _fnum(fin.get('zongguben'))
    zzc = _fnum(fin.get('zongzichan'))
    ldfz = _fnum(fin.get('liudongfuzhai'))
    cqfz = _fnum(fin.get('changqifuzhai'))
    px = _fnum(price)

    # 爆雷硬剔除（资不抵债 / 亏损）—— 恒生效，不可配置
    if jz < 0 or jl < 0:
        return False

    if not rules or not isinstance(rules, dict):
        return True                       # 仅爆雷硬剔除（初级模式现状）

    # PB 上限：每股净资产≤0（已爆雷剔除）→ 无意义跳过；每股净资产>0 才可比
    pb_max = rules.get('pb_max')
    if pb_max is not None and jz > 0 and px > 0 and (px / jz) > _fnum(pb_max):
        return False

    # PE 上限：净利≤0 无意义 → 隐含剔除（盈利要求）；总股本缺失 → 放行
    pe_max = rules.get('pe_max')
    if pe_max is not None:
        if jl <= 0:
            return False
        if zg > 0 and px > 0 and (px * zg / jl) > _fnum(pe_max):
            return False

    # 总市值下限（亿）：price × 总股本 / 1e8；数据缺失 → 放行
    mcap_min = rules.get('mcap_min')
    if mcap_min is not None and px > 0 and zg > 0:
        if (px * zg / 1e8) < _fnum(mcap_min):
            return False

    # 每股收益下限：EPS = 净利 / 总股本（同口径相除）；总股本缺失 → 放行
    eps_min = rules.get('eps_min')
    if eps_min is not None and zg > 0 and (jl / zg) < _fnum(eps_min):
        return False

    # 资产负债率上限（%）：(流动+长期负债) / 总资产 × 100；总资产≤0 → 放行
    debt_max = rules.get('debt_ratio_max')
    if debt_max is not None and zzc > 0:
        if ((ldfz + cqfz) / zzc * 100.0) > _fnum(debt_max):
            return False

    return True


def apply_finance_guard(results, topn, rules):
    """对排序后 results 的前 topn 只逐票查财务并套用财务筛选，剔除后不补位。

    rules: None → 仅爆雷硬剔除（初级模式现状，回退后与旧财务护栏等价）；
           dict 且 enabled 显式为 False → 整个财务筛选关闭，短路不查财务（性能）；
           空 dict / enabled 缺省 → 查财务，爆雷硬剔除 + 已配置阈值。
    单只查询失败（fin 缺失）→ 安全放行，不误杀整批（与旧护栏行为一致）。
    返回 (new_results, dropped)。
    """
    topn = int(topn or 0)
    if topn <= 0 or not results:
        return results, 0
    if isinstance(rules, dict) and rules.get('enabled') is False:
        return results, 0
    try:
        from engine.data_sources import tdx
    except Exception:
        return results, 0
    kept, dropped = [], 0
    for r in results[:topn]:
        fin = None
        try:
            fin, _err = tdx.fetch_finance(r.get('code') or '')
        except Exception:
            fin = None
        if not isinstance(fin, dict):
            kept.append(r)                # 查询失败/无数据 → 放行
            continue
        if not finance_rules_pass(rules, r.get('price'), fin):
            dropped += 1
            continue
        kept.append(r)
    return kept + results[topn:], dropped
