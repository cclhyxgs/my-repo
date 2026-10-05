#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""PyWebView JS API 桥接层

将后端 Python 能力暴露给前端 HTML/JS/ECharts，覆盖：
- 股票搜索、实时行情、K 线数据
- 量化分析（跑完整 pipeline）并返回结构化报告
- 量化模型配置读写（quantData <-> quant_model.json）
- 方案管理、授权管理、涨跌家数
"""

import copy
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta

import numpy as np

from engine.state import state
from engine.data_layer import DataAPI, K_TYPE_MAP
from engine.trading_pipeline import TradingPipeline
from engine.report_builder import ReportBuilder, _circuit_breaker_lines, _evaluate_add_tiers_lines, compute_decision, _signals_conclusion
from engine import quant_config
from engine.indicators import MATechnical
from engine.factor_registry import get_factor, REGISTRY, factor_explanation, parse_raw_value
from engine.backtest_launcher import BacktestLauncher
from engine.backtest_protocol import progress_path, stdout_path
from engine.config import WATCHLIST_FILE, CACHE_DIR, SECTOR_MAP_FILE, get_app_dir, CONFIG_DIR
from engine.discipline_log import get_journal
from engine.emotion_log import get_emotion_journal
from ui.config_mapper import (
    _default_quant_data, _config_to_quant_data, _quant_data_to_config,
    _validate_quant_before_save, _norm_num, _to_pct_val, _from_pct_val,
    _factor_default_params, _to_factor,
    _SIGNAL_BACK_TO_FRONT, _SIGNAL_FRONT_TO_BACK,
    _CIRCUIT_INDEX_NAME, _CIRCUIT_NAME_TO_CODE,
)
# 期货自选独立存储，避免与股票混在同一 watchlist.txt（#期货自选隔离）
WATCHLIST_FILE_FUTURES = os.path.join(os.path.dirname(WATCHLIST_FILE), 'watchlist_futures.txt')


def _watchlist_example(market):
    """按市场返回自选示例默认文本（期货模式下不应展示股票示例）。"""
    if market == 'futures':
        return ("螺纹钢 rb2610, 3500, 20\n鸡蛋 jd2609, 3800, 15\n"
                "PTA ta2510, 5500, 10\n沪铜 cu2510, 70000, 5")
    return ("贵州茅台, 1300, 100\n五粮液, 135, 15\n招商银行\n"
            "宁德时代, 180, 30\n中国平安")
from license.license_manager import (
    can_use, consume_use, get_license_info, activate as _activate_license,
    get_machine_id, get_status_text, deactivate,
)
from license.license_guard import LicenseDenied

# 授权自校验（启动期一次性）：冻结态若 license 模块被篡改则整体锁定。
# 放在 license 模块完全导入之后调用，避免导入期模块对象不一致导致误判。
try:
    from license.license_guard import run_startup_check
    run_startup_check()
except RuntimeError:
    import sys as _s
    if getattr(_s, '_MEIPASS', None) and getattr(_s, 'frozen', False):
        raise

logger = logging.getLogger(__name__)


# ── 独立诊断计时日志器 ─────────────────────────────────────
# 不受 root WARNING 级别限制，始终以 INFO 写入
#   %LOCALAPPDATA%/M-Bull/logs/diag_timing.log
# 用于「源码正常、EXE 卡死」类问题的分段计时定位（[diag-timing] 前缀）。
# 该日志器自含 FileHandler，与 error_log 的 RotatingFileHandler 互不干扰。
def _init_diag_logger():
    _dl = logging.getLogger('M-Bull.diag')
    if _dl.handlers:
        return _dl
    _dl.setLevel(logging.INFO)
    _dl.propagate = False  # 不向上冒泡到 root（避免被 WARNING 过滤）

    _candidates = []
    try:
        _candidates.append(os.path.join(get_app_dir(), 'logs', 'diag_timing.log'))
    except Exception:
        pass
    import tempfile
    try:
        _candidates.append(os.path.join(tempfile.gettempdir(), 'M-Bull_diag', 'diag_timing.log'))
    except Exception:
        pass

    for _path in _candidates:
        try:
            _log_dir = os.path.dirname(_path)
            os.makedirs(_log_dir, exist_ok=True)
            _fh = logging.FileHandler(_path, encoding='utf-8', delay=True)
            _fh.setLevel(logging.INFO)
            _fh.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
            _dl.addHandler(_fh)
            # 记录实际落盘路径，便于后续排查
            _dl._diag_log_path = _path
            break
        except Exception:
            continue
    return _dl


diag_logger = _init_diag_logger()


# ═══════════════════════════════════════════════════════════
# 配置映射：backend config <-> frontend quantData
# ═══════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════
# 报告数据结构构建
# ═══════════════════════════════════════════════════════════
def _market_status_label(t=None):
    """（2026-09-12，B24 方案 1）实现已**逐字上移**至 `engine.report_data`，此处为兼容委托。

    保留本名是为了让既有调用方（分析页 / 期货分析 / 诊断）零改动；
    行为与原实现完全一致 —— 上移前后函数体 AST 结构等价，hash 已冻结在
    `tests/h5_skeleton/test_f301_analyze.py`（改动夜盘/报告逻辑会撞该测试）。
    """
    from engine.report_data import _market_status_label as _impl
    return _impl(t)


def _explain_factor(entry_line):
    """（2026-09-12，B24 方案 1）实现已**逐字上移**至 `engine.report_data`，此处为兼容委托。

    保留本名是为了让既有调用方（分析页 / 期货分析 / 诊断）零改动；
    行为与原实现完全一致 —— 上移前后函数体 AST 结构等价，hash 已冻结在
    `tests/h5_skeleton/test_f301_analyze.py`（改动夜盘/报告逻辑会撞该测试）。
    """
    from engine.report_data import _explain_factor as _impl
    return _impl(entry_line)


def _pct_chg(price, ref):
    """（2026-09-12，B24 方案 1）实现已**逐字上移**至 `engine.report_data`，此处为兼容委托。

    保留本名是为了让既有调用方（分析页 / 期货分析 / 诊断）零改动；
    行为与原实现完全一致 —— 上移前后函数体 AST 结构等价，hash 已冻结在
    `tests/h5_skeleton/test_f301_analyze.py`（改动夜盘/报告逻辑会撞该测试）。
    """
    from engine.report_data import _pct_chg as _impl
    return _impl(price, ref)


def _stars_and_ratio(tech_strength, bands):
    """（2026-09-12，B24 方案 1）实现已**逐字上移**至 `engine.report_data`，此处为兼容委托。

    保留本名是为了让既有调用方（分析页 / 期货分析 / 诊断）零改动；
    行为与原实现完全一致 —— 上移前后函数体 AST 结构等价，hash 已冻结在
    `tests/h5_skeleton/test_f301_analyze.py`（改动夜盘/报告逻辑会撞该测试）。
    """
    from engine.report_data import _stars_and_ratio as _impl
    return _impl(tech_strength, bands)


def _parse_star_count(s):
    """（2026-09-12，B24 方案 1）实现已**逐字上移**至 `engine.report_data`，此处为兼容委托。

    保留本名是为了让既有调用方（分析页 / 期货分析 / 诊断）零改动；
    行为与原实现完全一致 —— 上移前后函数体 AST 结构等价，hash 已冻结在
    `tests/h5_skeleton/test_f301_analyze.py`（改动夜盘/报告逻辑会撞该测试）。
    """
    from engine.report_data import _parse_star_count as _impl
    return _impl(s)


def _build_tech_board(ctx):
    """（2026-09-12，B24 方案 1）实现已**逐字上移**至 `engine.report_data`，此处为兼容委托。

    保留本名是为了让既有调用方（分析页 / 期货分析 / 诊断）零改动；
    行为与原实现完全一致 —— 上移前后函数体 AST 结构等价，hash 已冻结在
    `tests/h5_skeleton/test_f301_analyze.py`（改动夜盘/报告逻辑会撞该测试）。
    """
    from engine.report_data import _build_tech_board as _impl
    return _impl(ctx)


def _build_report_data(ctx, stock_name):
    """（2026-09-12，B24 方案 1）实现已**逐字上移**至 `engine.report_data`，此处为兼容委托。

    保留本名是为了让既有调用方（分析页 / 期货分析 / 诊断）零改动；
    行为与原实现完全一致 —— 上移前后函数体 AST 结构等价，hash 已冻结在
    `tests/h5_skeleton/test_f301_analyze.py`（改动夜盘/报告逻辑会撞该测试）。
    """
    from engine.report_data import _build_report_data as _impl
    return _impl(ctx, stock_name)


def _stale_outputs(files_map, start_time_iso, _now=None):
    """挑出「旧文件」：mtime 早于回测启动时间的产物 key 列表。

    用途：本次回测写盘失败时（典型原因：CSV 正被 Excel/WPS 打开占用，
    见 `engine.backtest_strategy._safe_write_csv`），旧文件会残留，前端若不校验就会把
    上次的结果当成本次结果展示 —— 表现为「收益曲线和实际结果对不上」
    （2026-09-19 实锤：22:56 那次三个 CSV 全写失败，UI 显示 21:11 的旧曲线 +46%，
     而指标卡来自新报告的 +3.21%）。

    参数 files_map: {key: 绝对路径}；start_time_iso: task['start_time']（ISO 字符串）。
    容错：start_time 解析失败 / 路径不存在 / 无权限 → 不判定为旧（宁可不报，不误报）。
    """
    stale = []
    try:
        start_ts = datetime.fromisoformat(start_time_iso).timestamp()
    except Exception:
        return stale
    for key, path in (files_map or {}).items():
        try:
            if os.stat(path).st_mtime < start_ts:
                stale.append(key)
        except Exception:
            continue
    return stale


def _open_path_smart(path):
    """用系统默认程序打开文件；该扩展名无关联程序时按类型回退。

    WinError -2147221003 = CO_E_CLASSSTRING（"找不到应用程序"）：机器未给此扩展名
    注册默认程序（.json 最常见），os.startfile 会直接抛 OSError。回退顺序：
      文本类(.json/.csv/.txt/.log/.md/.ini/.yaml/.yml) → notepad
      图片类(.png/.jpg/.jpeg/.bmp/.gif/.webp)        → mspaint
      其它 / 上述也失败                              → 资源管理器中定位(explorer /select,)

    返回 (ok: bool, how: str)；全失败抛最后异常。
    """
    import subprocess
    ext = os.path.splitext(path)[1].lower()
    if hasattr(os, 'startfile'):
        try:
            os.startfile(path)  # type: ignore[attr-defined]
            return True, 'default'
        except OSError:
            pass  # 无关联程序 → 走回退
    _TEXT = ('.json', '.csv', '.txt', '.log', '.md', '.ini', '.yaml', '.yml')
    _IMG = ('.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp')
    try:
        if ext in _TEXT:
            subprocess.Popen(['notepad.exe', path])
            return True, 'notepad'
        if ext in _IMG:
            subprocess.Popen(['mspaint.exe', path])
            return True, 'mspaint'
    except Exception:
        pass
    # 最后兜底：资源管理器定位该文件（至少让用户看到它在哪）
    subprocess.Popen(['explorer.exe', '/select,' + os.path.normpath(path)])
    return True, 'explorer'


# report.json → CSV 的中文标签（未登记的键原样输出，便于引擎新增字段时自动兼容）
_REPORT_LABELS = {
    'total_trades': '总交易笔数',
    'win_rate': '胜率(%)',
    'avg_pnl_simple': '平均收益(简单,%)',
    'avg_pnl_compound': '平均收益(复利,%)',
    'avg_win': '平均盈利(%)',
    'avg_loss': '平均亏损(%)',
    'profit_loss_ratio': '盈亏比',
    'reduce_triggered': '减仓触发次数',
    'reduce_protected': '减仓保护次数',
    'clean_exit': '干净退出次数',
    'add_triggered': '加仓触发次数',
    'sell_price_mode': '卖出价模式',
    'start_date': '开始日期',
    'end_date': '结束日期',
    'compound.final_nav': '最终净值',
    'compound.total_return': '总收益(%)',
    'compound.cagr': '年化收益(%)',
    'compound.max_drawdown': '最大回撤(%)',
    'compound.sharpe': 'Sharpe',
    'compound.calmar': 'Calmar',
    'compound.years_span': '跨度(年)',
    'equity_curve_file': '净值曲线文件',
}


def _report_json_to_csv(json_path, csv_path):
    """把回测 `*_report.json` 转成两列 CSV（指标,值），供「打开报告」按钮使用。

    背景（2026-09-19 用户要求）：`.json` 在用户机器上无默认关联程序，打开必失败；
    报告只有 JSON 产物，故就地转成 CSV（嵌套 dict 用「父.子」扁平化）。
    写出用 utf-8-sig（带 BOM），Excel 直接双击不乱码。
    """
    import csv as _csv
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    rows = []

    def _walk(node, prefix=''):
        if isinstance(node, dict):
            for k, v in node.items():
                _walk(v, f'{prefix}{k}.')
        elif isinstance(node, list):
            rows.append((prefix.rstrip('.') or 'value', json.dumps(node, ensure_ascii=False)))
        else:
            rows.append((prefix.rstrip('.') or 'value', '' if node is None else node))

    _walk(data)
    with open(csv_path, 'w', encoding='utf-8-sig', newline='') as f:
        w = _csv.writer(f)
        w.writerow(['指标', '值'])
        for k, v in rows:
            w.writerow([_REPORT_LABELS.get(k, k), v])
    return csv_path


# ═══════════════════════════════════════════════════════════
# WebAPI 类
# ═══════════════════════════════════════════════════════════
class WebAPI:
    """暴露给前端 JS 调用的 Python API。"""

    def __init__(self):
        self._lock = threading.Lock()
        # 耗时任务状态池（扫描、回测、诊断）
        self._scan_tasks = {}
        self._backtest_tasks = {}
        self._diag_tasks = {}
        # 市场类型：'stock'（A股）或 'futures'（期货）
        self._market_type = 'stock'

    # ── 市场类型切换 ──
    def set_market_type(self, market_type):
        """切换市场类型：'stock'（A股）或 'futures'（期货）。"""
        mt = (market_type or 'stock').strip().lower()
        if mt not in ('stock', 'futures'):
            return {'success': False, 'error': f"未知市场类型: {market_type}"}
        self._market_type = mt
        logger.info(f"市场类型切换 → {mt}")
        return {'success': True, 'market_type': mt}

    def get_market_type(self):
        """返回当前市场类型。"""
        return {'market_type': self._market_type}

    def search_futures(self, query):
        """委托 engine.futures_search.search_futures（2026-09-13 上移，B14 清理）。"""
        from engine.futures_search import search_futures as _impl
        return _impl(query)

    # ── 股票搜索 ──
    def search_stocks(self, query):
        """返回匹配的品种列表 [{name, code}, ...]，最多 20 条。

        根据当前市场类型分流：股票走 state.name_to_code，期货走 futures_pool。
        """
        # 期货模式：走期货品种池搜索
        if self._market_type == 'futures':
            return self.search_futures(query)
        # 股票模式：原有逻辑
        q = (query or '').strip()
        if not q:
            return []
        results = []
        q_compact = q.replace(' ', '')

        # 精确代码前缀匹配
        if q.startswith(('sh', 'sz')):
            name = DataAPI.get_stock_name(q)
            if name and name != q:
                results.append({'name': name, 'code': q})

        # 名称精确/包含匹配
        seen = {r['code'] for r in results}
        for name, code in (state.name_to_code or {}).items():
            if code in seen:
                continue
            name_compact = name.replace(' ', '')
            if q_compact == name_compact or q_compact in name_compact:
                results.append({'name': name, 'code': code})
                seen.add(code)
            if len(results) >= 20:
                break
        return results

    # ── K 线 ──
    def get_kline(self, code, k_type='日K', days=60):
        """返回 ECharts  candlestick 所需结构。

        返回：{dates, data:[[open,close,low,high],...], vol, ma5, ma20, ma60, error}
        """
        code = (code or '').strip()
        if not code:
            return {'error': '请输入代码'}

        # 期货模式：走期货数据层
        if self._market_type == 'futures':
            return self._get_futures_kline(code, k_type, days)

        if k_type not in K_TYPE_MAP:
            k_type = '日K'
        try:
            days = int(days)
        except (ValueError, TypeError):
            days = 60
        try:
            df, err = DataAPI.get_kline(code, k_type, days)
            if err:
                return {'error': str(err)}
            if df is None or df.empty:
                return {'error': '无K线数据'}

            # 日K 盘中修补已统一在 DataAPI.get_kline 出口的 _patch_intraday_bar 完成
            # （含 09:15~15:00 时段闸门 + 批量快照池取价）。此处不再重复修补：
            # 旧本地副本既漏了「通达信」的成交量 股→手 换算（量能柱放大 100 倍），
            # 又缺时段闸门（盘前会凭空拼出一根假 K 线）。

            dates = []
            data = []
            vol = []
            closes = []
            for _, row in df.iterrows():
                tt = row['trade_time']
                if isinstance(tt, datetime):
                    dates.append(tt.strftime('%m-%d'))
                else:
                    dates.append(str(tt)[:5])
                o = float(row['open']); c = float(row['close'])
                l = float(row['low']); h = float(row['high'])
                data.append([round(o, 2), round(c, 2), round(l, 2), round(h, 2)])
                vol.append(int(row['volume']))
                closes.append(c)

            def _sma(arr, period):
                return [round(sum(arr[i - period + 1:i + 1]) / period, 2) if i >= period - 1 else None
                        for i in range(len(arr))]

            return {
                'dates': dates,
                'data': data,
                'vol': vol,
                'ma5': _sma(closes, 5),
                'ma20': _sma(closes, 20),
                'ma60': _sma(closes, 60),
                'error': None,
            }
        except Exception as e:
            logger.exception("get_kline 失败")
            return {'error': str(e)}

    def _get_futures_kline(self, code, k_type='日K', days=60):
        """期货 K 线数据，返回与 get_kline 相同的 ECharts 结构。

        支持：
          - 合约格式：'rb'（主力连续）/ 'jd2609' / 'TA2510'（具体合约）
          - 周期：日K / 60分钟 / 30分钟 / 15分钟（分钟级走新浪 getFewMinLine）
        """
        from engine.futures_pool import parse_contract_code, make_specific_contracts
        from engine.futures_data import (fetch_futures_daily, fetch_futures_minute,
                                         fetch_futures_realtime)

        try:
            days = int(days)
        except (ValueError, TypeError):
            days = 60
        days = max(days, 10)  # 最少 10 根

        # 解析合约：'rb' → 主力连续, 'jd2609' → 具体合约
        contract, month = parse_contract_code(code)
        if not contract:
            return {'error': f'未知期货品种或合约: {code}'}
        if month:
            spec = make_specific_contracts([contract], month)[0]
        else:
            spec = contract

        # 周期映射：分钟级走 getFewMinLine，日K/周K 走 getDailyKLine
        k_type = (k_type or '日K').strip()
        period_map = {'15分钟': 15, '30分钟': 30, '60分钟': 60, '1分钟': 1, '5分钟': 5}
        period = period_map.get(k_type)

        if period:
            # 分钟K：新浪分钟线本身含最新数据，无需实时修补
            df = fetch_futures_minute(spec.symbol, spec.secid, period=period, days=days)
            time_fmt = '%m-%d %H:%M'
        else:
            # 日K（周K 数据源亦为日K，图表按日显示）
            fetch_days = max(days, 300)  # 至少 300 根：日线最长窗口 MA60 只需 60，余量留给指标预热与前端缩放
            df = fetch_futures_daily(spec.symbol, spec.secid, days=fetch_days)
            time_fmt = '%m-%d'
            # 实时修补最后一根K线（对齐A股盘中逻辑）
            # 夜盘活跃期(21:00后 / 凌晨3点前)的行情归属【下一交易日】，不应污染上一交易日已收市的日K，
            # 该时段交由下方夜盘合并逻辑单独生成"下一交易日夜盘"K线并随每次请求实时刷新；
            # 仅日盘活跃/盘中时段按A股方式实时修补当日bar，下周一开盘后由真实日K自然合并，不重复造bar。
            _now = datetime.now()
            _in_night = (_now.hour >= 21) or (_now.hour < 3)
            if df is not None and not df.empty and not _in_night:
                try:
                    rt = fetch_futures_realtime(spec.symbol, spec.secid)
                    if rt and rt.get('last'):
                        last_price = float(rt['last'])
                        if last_price > 0:
                            last_date = df['trade_time'].max()
                            today = datetime.now()
                            if hasattr(last_date, 'date') and last_date.date() == today.date():
                                idx = df['trade_time'].idxmax()
                                df.loc[idx, 'close'] = last_price
                                df.loc[idx, 'high'] = max(df.loc[idx, 'high'], float(rt.get('high', last_price)))
                                df.loc[idx, 'low'] = min(df.loc[idx, 'low'], float(rt.get('low', last_price)))
                            else:
                                # 仅工作日（周一~周五）才允许拼接当日新K线，
                                # 避免周六/周日/节假日凭空编出一根假K线
                                if today.weekday() < 5:
                                    import pandas as _pd
                                    new_row = _pd.DataFrame([{
                                        'trade_time': today,
                                        'open': float(rt.get('open', last_price)),
                                        'high': max(float(rt.get('high', last_price)), last_price),
                                        'low': min(float(rt.get('low', last_price)), last_price),
                                        'close': last_price,
                                        'volume': float(rt.get('volume', 0)),
                                    }])
                                    df = _pd.concat([df, new_row], ignore_index=True)
                except Exception:
                    pass

        if df is None or df.empty:
            return {'error': f'期货K线数据获取失败: {code}'}

        # 截取请求的条数
        df = df.tail(days).reset_index(drop=True)

        # ── 夜盘K线：把"最近一个交易日"的夜盘(21点后/跨零点的凌晨段)单独聚合成一根K线，
        #    满足用户"日K体现当日夜盘、单独一根K线"的需求；供前端显示 + 评分复用（共享方法） ──
        # 仅日K/周K需要此聚合；分钟图表本身已包含夜盘分钟bar（如21:00~23:00），
        # 直接保留即可，避免"分钟级别里冒出日K"（与 _analyze_futures 的分钟分支口径一致）。
        if not period:
            df = self._merge_futures_night(df, spec.symbol, spec.secid)

        dates = []
        data = []
        vol = []
        closes = []
        for _, row in df.iterrows():
            tt = row['trade_time']
            # 只用「值严格为 True」判定夜盘标记。不能用 `if row.get(...)`：
            # 历史行该列为 NaN，而 bool(float('nan'))==True，会误把所有K线都标成夜盘。
            if row.get('_is_night') is True:
                dates.append(tt.strftime('%m-%d') + ' 夜')
            elif isinstance(tt, datetime):
                dates.append(tt.strftime(time_fmt))
            else:
                dates.append(str(tt)[:16] if period else str(tt)[:5])
            o = float(row['open']); c = float(row['close'])
            l = float(row['low']); h = float(row['high'])
            data.append([round(o, 2), round(c, 2), round(l, 2), round(h, 2)])
            vol.append(int(row['volume']))
            closes.append(c)

        def _sma(arr, period):
            return [round(sum(arr[i - period + 1:i + 1]) / period, 2) if i >= period - 1 else None
                    for i in range(len(arr))]

        return {
            'dates': dates,
            'data': data,
            'vol': vol,
            'ma5': _sma(closes, 5),
            'ma20': _sma(closes, 20),
            'ma60': _sma(closes, 60),
            'error': None,
        }

    def _merge_futures_night(self, df, symbol, secid):
        """把"最近一个交易日"的夜盘(21点后/跨零点的凌晨段)单独聚合成一根"下一交易日夜盘"K线。

        ⚠️ 2026-09-12（B7 方案 1）：**实现已逐字上移**到 `engine.futures_night.merge_night_session()`，
        本方法退化为薄委托，行为完全不变（上移前已用 AST 结构比对确认函数体 hash 一致）。
        上移原因：H5 服务端按 §2.8「engine 可直接 import」设计，夜盘聚合此前只在 UI 层，
        导致 F-203「期货日K 夜盘合并到下一交易日」在服务端无法达成。
        本方法保留是为了让三处既有调用方（get_kline / _analyze_futures / 全市场扫描）零改动。
        """
        from engine.futures_night import merge_night_session
        return merge_night_session(df, symbol, secid)

    # ── 量化分析 ──
    def analyze(self, code, name='', k_type='日K', entry_price=None, bars_held=0, up_count=None, down_count=None, _skip_quota=False, direction='long', scheme_name=None):
        """执行完整分析 pipeline，返回结构化报告数据。

        _skip_quota=True 时跳过额度扣减（供 diagnose_watchlist 整批扣 1 次用）。
        direction: 持仓方向，'long'=多头（默认，A 股恒为此值），'short'=空头（期货）。
        scheme_name: 非空时**强制使用指定方案名**（用户从手动分析下拉精确选了哪个方案就用哪个），
            不再靠 resolve_scheme 猜——这是"15分钟方案显示成别的名字"和"切换方案后分析用错"的根因。

        ⚠️ 2026-09-12（B27 方案 1）：A股核心已**上移**到 `engine.analyze_service.analyze_stock()`
        （H5 服务端与桌面共用同一实现，并借此解掉 `self._market_type` 进程级全局态）。
        仍留在本方法的是 UI 侧关切：期货分流 / 初级模式 / 授权准入 / 额度 / 纪律落册，
        通过钩子（authorize / is_basic_mode / basic_preview / on_quota）在**原调用位**注入，
        故行为与调用顺序不变；等价性由真实数据 A/B 对拍证明（见 tests/h5_skeleton/test_f301_analyze.py）。
        """
        code = (code or '').strip()
        if not code:
            return {'error': '请输入代码'}

        # 期货模式：走期货数据层（期货不随本条迁移，见 B14/B17 先例）
        if self._market_type == 'futures':
            return self._analyze_futures(code, name, k_type, entry_price, bars_held, _skip_quota, direction, scheme_name=scheme_name)

        from engine.analyze_service import analyze_stock

        def _consume_quota():
            if not _skip_quota:
                consume_use()

        result = analyze_stock(
            code,
            k_type=k_type,
            name=name,
            entry_price=entry_price,
            bars_held=bars_held,
            up_count=up_count,
            down_count=down_count,
            direction=direction,
            scheme_name=scheme_name,
            authorize=lambda: can_use('query'),
            is_basic_mode=self._is_basic_mode,
            basic_preview=lambda c, n, k: self._analyze_basic_preview(c, n, k),
            on_quota=_consume_quota,
        )

        if isinstance(result, dict) and 'reportData' in result:
            # 执行与复盘闭环：把本次触发的建仓/加仓/减仓/清仓信号落入纪律账本，
            # 并刷新该股已有信号的现价对照（冷却软提醒状态随响应返回给前端）。
            try:
                result['discipline'] = self._maybe_log_signal(
                    result['reportData'], period=k_type or '日K', scheme=result.get('used_scheme_name'))
            except Exception:
                result['discipline'] = {}
        return result

    def _analyze_basic_preview(self, code, name='', k_type='日K'):
        """初级模式·未配置方案：仅出「技术指标看板/解读」。

        产品口径：初级查看指标解读不依赖方案，但建/加/减/清的信号触发必须配置方案。
        此分支只拉行情→算技术指标→给看板与解读，不做评分定档、不触发任何建仓/加减/清仓信号。
        """
        try:
            stock_name = name.strip() or DataAPI.get_stock_name(code)
            df, err = DataAPI.get_kline(code, k_type, 300)
            if err or df is None or df.empty:
                return {'error': '无法获取行情数据（当前数据源暂不可用）。请检查网络，或到「设置-数据源」切换数据源后重试。', 'detail': f"获取K线失败: {err or '无数据'}"}
            data_list = [{'close': r['close'], 'high': r['high'], 'low': r['low'],
                          'volume': r['volume'], 'date': r['trade_time'], 'open': r['open']}
                         for _, r in df.tail(260).iterrows()]
            closes = [d['close'] for d in data_list]
            volumes = [d['volume'] for d in data_list]
            highs = [d['high'] for d in data_list]
            lows = [d['low'] for d in data_list]
            opens = [d['open'] for d in data_list]

            from engine.indicators import MACDCalculator
            from engine.scoring_core import build_tech, build_market_dict, _rsi_series_fast
            rsi_period = 14
            rsi_hist = _rsi_series_fast(closes, rsi_period)
            accel, accel_status = MACDCalculator.calc_acceleration(closes)
            tech = build_tech(closes, volumes, highs, lows, opens, data_list,
                              rsi_hist, (accel, accel_status), rsi_period=rsi_period)
            market = build_market_dict(closes, volumes, 0.5)
            import types
            ctx = types.SimpleNamespace(tech=tech, market=market, latest_price=closes[-1])
            board = _build_tech_board(ctx)
            return {
                'code': code, 'name': stock_name, 'k_type': k_type,
                'latest': closes[-1], 'tech_board': board, 'mode': 'basic_preview',
                'mode_label': '初级 · 指标解读（未配置方案）',
                'notice': '未配置方案：当前展示的是「指标解读」预览，帮助理解K线含义与应对策略；'
                          '建仓/加仓/减仓/清仓信号需到「模型配置」创建方案后才会触发。',
            }
        except Exception as e:
            logger.exception("初级指标预览失败")
            return {'error': f"指标解读失败: {e}"}

    def _analyze_futures(self, symbol, name='', k_type='日K', entry_price=None, bars_held=0, _skip_quota=False, direction='long', scheme_name=None):
        """期货分析：用期货数据源获取 K 线，复用 TradingPipeline 分析流水线。

        核心设计：TradingPipeline.execute() 是市场无关的，只需喂入同构的
        data_list / tech / market 即可复用全部因子评分+决策逻辑。
        direction: 持仓方向，'long'=多，'short'=空（期货持仓专用）。
        scheme_name: 非空时强制使用指定方案名（前端下拉精确选择，不再"猜"）。

        ⚠️ 2026-09-12（B29 方案 1）：实现已**上移**到 `engine.analyze_service.analyze_futures()`
        （与 H5 服务端共用一份）。本方法保留：额度扣减与纪律落册（仍属 UI 侧），
        通过 `on_quota` 钩子在原调用位注入；`now` 不传 → engine 回退 `datetime.now()`，
        与迁移前一致（服务端 F-302 会显式传 Asia/Shanghai，见 §2.13-2）。
        """
        from engine.analyze_service import analyze_futures

        def _consume_quota():
            if not _skip_quota:
                consume_use()

        result = analyze_futures(
            symbol,
            k_type=k_type,
            name=name,
            entry_price=entry_price,
            bars_held=bars_held,
            direction=direction,
            scheme_name=scheme_name,
            on_quota=_consume_quota,
        )

        if isinstance(result, dict) and 'reportData' in result:
            # 执行与复盘闭环：期货分支同样落信号日志 + 刷新现价对照
            try:
                result['discipline'] = self._maybe_log_signal(
                    result['reportData'], period=k_type or '日K', scheme=result.get('used_scheme_name'))
            except Exception:
                result['discipline'] = {}
        return result

    # ── 配置读写 ──
    def load_quant_config(self, market='stock', direction='long', period=None, scheme_name=None):
        """按市场+方向+周期（+可选指定方案名）加载对应方案的 quantData 结构。

        统一返回：{success: bool, data: quantData | None, error: str | None, empty: bool}
        - empty=True：该分类+周期无配置方案，返回种子空白数据，前端应显示"暂无方案"
        - success=True 且 empty=False：正常加载到已有方案
        - success=False：解析/读取/合并失败，error 含详情，前端应显示失败提示而非静默空白
        """
        try:
            # 入口处只 force_reload 一次，后续同线程内操作均走内存最新（问题7：
            # 避免同一 API 内反复 force_reload 导致"刚改内存→被磁盘旧值冲回"）。
            quant_config.load_config(force_reload=True)
            if scheme_name:
                name = scheme_name
                cfg = quant_config.load_market_scheme(market, direction, period, force_reload=False, scheme_name=name)
            else:
                # 优先当前激活方案：仅当其 meta 同时匹配 市场/方向/周期 时复用，
                # 否则按 (市场,方向,周期) 解析叶节点方案（周期分类轴：同市场方向下多周期各自独立）。
                name = None
                cur = quant_config.get_current_scheme_name()
                if cur:
                    cur_meta = quant_config.get_scheme_meta(cur)
                    if cur_meta.get('market') == market and \
                       (market != 'futures' or cur_meta.get('direction') == direction) and \
                       (period is None or cur_meta.get('period') == period):
                        name = cur
                if not name:
                    name = quant_config.resolve_scheme(market, direction, period, strict=True)
                if not name:
                    if market == 'futures':
                        quant_config.ensure_futures_schemes()
                        name = quant_config.resolve_scheme(market, direction, period, strict=True)
                    if not name:
                        quant_config.clear_current_scheme()
                        data = _default_quant_data()
                        # 无方案：显式标记 schemeName=None，前端据此清空选中（周期隔离）
                        data['schemeName'] = None
                        data['period'] = period or '日K'
                        return {'success': True, 'data': data, 'error': None, 'empty': True}
                cfg = quant_config.load_market_scheme(market, direction, period, force_reload=False, scheme_name=name)
            data = _config_to_quant_data(cfg, direction)
            data['schemeName'] = name
            smeta = quant_config.get_scheme_meta(name) if name else {}
            data['period'] = period or smeta.get('period') or '日K'
            data['periodPresets'] = quant_config.get_scheme_periods(name) if name else []
            return {'success': True, 'data': data, 'error': None, 'empty': False}
        except Exception as e:
            logger.exception("load_quant_config 失败")
            return {'success': False, 'data': None, 'error': str(e), 'empty': False}

    # ── 自定义公式因子（用户自建指标） ──

    def get_custom_factor_meta(self):
        """返回公式语言的算子/原始数据/逻辑词清单与预设模板（供编辑器函数库面板）。"""
        try:
            from engine import custom_factor as cf
            meta = cf.get_operator_meta()
            meta['templates'] = cf.get_templates()
            return {'success': True, 'data': meta, 'error': None}
        except Exception as e:
            logger.exception('get_custom_factor_meta 失败')
            return {'success': False, 'data': None, 'error': str(e)}

    def validate_custom_formula(self, formula):
        """校验公式语法，返回 {ok, errors:[{line,col,msg}], variables, output}。"""
        try:
            from engine import custom_factor as cf
            return {'success': True, 'data': cf.validate_formula(formula), 'error': None}
        except Exception as e:
            logger.exception('validate_custom_formula 失败')
            return {'success': False, 'data': None, 'error': str(e)}

    def list_custom_factors(self):
        """返回当前方案已保存的自定义因子列表。"""
        try:
            return {'success': True, 'data': quant_config.list_custom_factors(), 'error': None}
        except Exception as e:
            logger.exception('list_custom_factors 失败')
            return {'success': False, 'data': None, 'error': str(e)}

    def save_custom_factor(self, name, formula, weight=1.0, direction=1, estimate_stats=False):
        """保存自定义公式因子（保存即编译注册，并加入启用因子）。

        estimate_stats=True 时先用自选股样本估算 mean/std，避免新因子 z-score 失衡。
        """
        try:
            stats = None
            if estimate_stats:
                stats = self._estimate_custom_factor_stats(formula)
            ok, err = quant_config.save_custom_factor(
                name, formula, weight=weight, direction=direction, stats=stats)
            return {'success': bool(ok), 'data': {'stats': stats}, 'error': err}
        except Exception as e:
            logger.exception('save_custom_factor 失败')
            return {'success': False, 'data': None, 'error': str(e)}

    def delete_custom_factor(self, name):
        """删除自定义公式因子。"""
        try:
            ok, err = quant_config.delete_custom_factor(name)
            return {'success': bool(ok), 'error': err}
        except Exception as e:
            logger.exception('delete_custom_factor 失败')
            return {'success': False, 'error': str(e)}

    def preview_custom_formula(self, formula, code=None, k_type='日K', days=250):
        """对一只标的试算公式，返回指标序列 + K线（供 ECharts 叠图实时预览）。"""
        try:
            from engine import custom_factor as cf
            check = cf.validate_formula(formula)
            if not check['ok']:
                e0 = check['errors'][0]
                return {'success': False, 'data': None,
                        'error': f"第{e0['line']}行第{e0['col']}列：{e0['msg']}"}
            if not code:
                wl = self.get_watchlist()
                items = wl.get('items') or []
                if not items:
                    return {'success': False, 'data': None, 'error': '请先添加自选股，或指定股票代码'}
                code = items[0]['code']
            ctx, err, dates = self._custom_factor_ctx(code, k_type, days)
            if err:
                return {'success': False, 'data': None, 'error': err}
            series = cf.compile_formula(formula).eval_series(ctx)
            vals = [None if not np.isfinite(v) else round(float(v), 4) for v in series]
            finite = [v for v in vals if v is not None]
            data = {
                'code': code,
                'dates': dates,
                'values': vals,
                'closes': [round(float(c), 2) for c in ctx['closes']],
                'latest': finite[-1] if finite else None,
                'recent': finite[-5:] if finite else [],
                'valid_count': len(finite),
                'total': len(vals),
            }
            return {'success': True, 'data': data, 'error': None}
        except Exception as e:
            logger.exception('preview_custom_formula 失败')
            return {'success': False, 'data': None, 'error': str(e)}

    def try_custom_formula(self, formula, codes=None, limit=20):
        """多股抽查试算：返回每只股票的末值与有效性，用于排查除权/停牌导致的计算异常。"""
        try:
            from engine import custom_factor as cf
            check = cf.validate_formula(formula)
            if not check['ok']:
                e0 = check['errors'][0]
                return {'success': False, 'data': None,
                        'error': f"第{e0['line']}行第{e0['col']}列：{e0['msg']}"}
            if not codes:
                wl = self.get_watchlist()
                codes = [it['code'] for it in (wl.get('items') or [])]
            codes = [str(c).strip() for c in (codes or []) if str(c).strip()][:max(1, int(limit))]
            if not codes:
                return {'success': False, 'data': None, 'error': '没有可试算的标的'}
            compiled = cf.compile_formula(formula)
            rows = []
            for c in codes:
                ctx, err, _ = self._custom_factor_ctx(c, '日K', 250)
                if err:
                    rows.append({'code': c, 'error': err})
                    continue
                series = compiled.eval_series(ctx)
                finite = series[np.isfinite(series)]
                rows.append({
                    'code': c,
                    'latest': round(float(finite[-1]), 4) if finite.size else None,
                    'mean': round(float(finite.mean()), 4) if finite.size else None,
                    'std': round(float(finite.std()), 4) if finite.size else None,
                    'valid_count': int(finite.size),
                    'total': int(series.size),
                })
            return {'success': True, 'data': rows, 'error': None}
        except Exception as e:
            logger.exception('try_custom_formula 失败')
            return {'success': False, 'data': None, 'error': str(e)}

    def _custom_factor_ctx(self, code, k_type='日K', days=250):
        """取一只标的的 K 线并构造公式求值上下文。

        Returns: (ctx, error, dates)
        """
        code = (code or '').strip()
        if not code:
            return None, '请输入代码', None
        try:
            days = int(days)
        except (ValueError, TypeError):
            days = 250
        try:
            if self._market_type == 'futures':
                kd = self._get_futures_kline(code, k_type, days)
                if kd.get('error'):
                    return None, str(kd['error']), None
                rows = kd.get('data') or []
                if not rows:
                    return None, '无K线数据', None
                opens = [float(r[0]) for r in rows]
                closes = [float(r[1]) for r in rows]
                lows = [float(r[2]) for r in rows]
                highs = [float(r[3]) for r in rows]
                vols = [float(v) for v in (kd.get('vol') or [])]
                if len(vols) != len(rows):
                    vols = [0.0] * len(rows)
                dates = kd.get('dates') or []
            else:
                df, err = DataAPI.get_kline(code, k_type, days)
                if err:
                    return None, str(err), None
                if df is None or df.empty:
                    return None, '无K线数据', None
                opens = [float(x) for x in df['open'].tolist()]
                closes = [float(x) for x in df['close'].tolist()]
                lows = [float(x) for x in df['low'].tolist()]
                highs = [float(x) for x in df['high'].tolist()]
                vols = [float(x) for x in df['volume'].tolist()]
                dates = [tt.strftime('%m-%d') if isinstance(tt, datetime) else str(tt)[:5]
                         for tt in df['trade_time'].tolist()]
        except Exception as e:
            logger.exception('_custom_factor_ctx 取数失败')
            return None, str(e), None
        ctx = {
            'closes': closes, 'highs': highs, 'lows': lows,
            'opens': opens, 'volumes': vols,
            'latest_price': closes[-1] if closes else 0.0,
            'data_list': [], 'tech': {}, 'market': {},
        }
        return ctx, None, dates

    def _estimate_custom_factor_stats(self, formula, sample_limit=60):
        """用自选股样本估算自定义因子的 mean/std（供 z-score 校准）。

        样本不足（<5 只有效）返回 None，由保存逻辑回落默认 {mean:0, std:1}。
        """
        from engine import custom_factor as cf
        try:
            compiled = cf.compile_formula(formula)
        except Exception:
            return None
        try:
            wl = self.get_watchlist()
            codes = [it['code'] for it in (wl.get('items') or [])][:max(1, int(sample_limit))]
        except Exception:
            codes = []
        if not codes:
            return None
        values = []
        for c in codes:
            try:
                ctx, err, _ = self._custom_factor_ctx(c, '日K', 250)
                if err:
                    continue
                v = compiled.eval_scalar(ctx)
                if np.isfinite(v):
                    values.append(float(v))
            except Exception:
                continue
        if len(values) < 5:
            return None
        arr = np.asarray(values, dtype=float)
        std = float(arr.std())
        if not np.isfinite(std) or std <= 1e-9:
            std = 1.0
        return {'mean': round(float(arr.mean()), 6), 'std': round(std, 6),
                'sample': len(values)}

    def save_quant_config(self, data, market='stock', direction='long', period=None, scheme_name=None):
        """保存 quantData 到方案（期货多单/空单隔离，因子共用 futures profile）。

        scheme_name：前端明确选定的方案（三层分类叶节点）。指定时直接写该方案；
            若不存在则按其 meta(市场/方向/周期) 新建；并切为当前方案。
        period：仅作为新建方案时的 meta.period 默认值（单周期方案直接写 config，不再写 period_configs 预设层）。

        一致性增强（问题7/问题6）：
          - 同一 API 内只在入口 load_config(force_reload=True) 一次，后续内存连续操作不重复
            force_reload，避免"刚 add_scheme → 下一个操作 force_reload 冲回磁盘旧值"。
          - 新建方案（add_scheme 成功）后续 save 任一环节失败时，回滚删除空壳方案
            （避免 saveAsNewScheme 两步路径残留空壳）。
        """
        try:
            # 初级用法（因子休眠）无因子门槛，允许保存触发指标/持有期配置；高级用法仍校验
            skip_factor = self.get_usage_mode().get('usage_mode') == 'basic'
            err = _validate_quant_before_save(data, direction, skip_factor_check=skip_factor)
            if err:
                return {'success': False, 'error': err}

            quant_config.load_config(force_reload=True)
            if scheme_name:
                # 三层分类轴：以叶节点方案为存储单元
                name = scheme_name
                created = False
                if name not in quant_config.get_schemes():
                    meta = {
                        'market': market,
                        'direction': direction if market == 'futures' else 'long',
                        'period': period or '日K',
                    }
                    ok = quant_config.add_scheme(name, label=name, desc='', meta=meta)
                    if not ok:
                        return {'success': False, 'error': '新建方案失败（名称冲突或非法）'}
                    created = True
                # 问题6：原子回滚 helper——API调用内新建的方案壳失败时删掉
                def _rollback_created_shell():
                    if created and name in (quant_config.get_schemes() or {}):
                        try: quant_config.remove_scheme(name)
                        except Exception: pass
                try:
                    # 同线程内刚 add_scheme → load_market_scheme 不应再 force_reload，
                    # 默认参数 force_reload=False 即沿用内存最新（问题7）
                    quant_config.load_market_scheme(market, direction, period, scheme_name=name)
                    config = _quant_data_to_config(data, direction)
                    fp_name = quant_config.get_schemes().get(name, {}).get('factor_profile')
                    if fp_name:
                        factor_section = {
                            'active_factors': config.pop('active_factors', []),
                            'factor_configs': config.pop('factor_configs', {}),
                            'score_scale': config.pop('score_scale', {}),
                        }
                        if not quant_config.save_factor_profile(fp_name, factor_section):
                            _rollback_created_shell()
                            return {'success': False, 'error': f'保存失败：因子共用配置写入被拒（{fp_name}）'}
                    ok = quant_config.save_scheme_config(name, config)
                    if not ok:
                        _rollback_created_shell()
                        return {'success': False, 'error': f'保存失败：方案写入被拒（{name}）'}
                    # 周期标签随编辑同步（改周期=移动分类轴，确保 meta.period 与界面一致）
                    if period:
                        quant_config.set_scheme_period(name, period)
                    quant_config.switch_scheme(name)
                    return {'success': True, 'scheme': name}
                except Exception:
                    _rollback_created_shell()
                    raise
            # ── 遗留路径（无 scheme_name）：按 市场+方向 解析方案 ──
            name = quant_config.resolve_scheme(market, direction)
            created_default = False
            if not name:
                if market == 'futures':
                    quant_config.ensure_futures_schemes()
                    name = quant_config.resolve_scheme(market, direction)
                if not name:
                    quant_config.add_scheme('default', label='默认方案', desc='')
                    name = 'default'
                    created_default = True
            # 遗留路径原子回滚 helper（问题6）
            def _rollback_default_shell():
                if created_default and name == 'default' and 'default' in (quant_config.get_schemes() or {}):
                    try: quant_config.remove_scheme('default')
                    except Exception: pass
            try:
                quant_config.load_market_scheme(market, direction)
                config = _quant_data_to_config(data, direction)
                if period:
                    ok = quant_config.save_scheme_period_config(name, period, config)
                    if not ok:
                        _rollback_default_shell()
                        return {'success': False, 'error': f'保存失败：周期预设写入被拒（{name}/{period}）'}
                    return {'success': True, 'period': period}
                fp_name = quant_config.get_schemes().get(name, {}).get('factor_profile')
                if fp_name:
                    factor_section = {
                        'active_factors': config.pop('active_factors', []),
                        'factor_configs': config.pop('factor_configs', {}),
                        'score_scale': config.pop('score_scale', {}),
                    }
                    if not quant_config.save_factor_profile(fp_name, factor_section):
                        _rollback_default_shell()
                        return {'success': False, 'error': f'保存失败：因子共用配置写入被拒（{fp_name}）'}
                ok = quant_config.save_scheme_config(name, config)
                if not ok:
                    _rollback_default_shell()
                    return {'success': False, 'error': f'保存失败：方案不存在或写入被拒（{name}）'}
                return {'success': True}
            except Exception:
                _rollback_default_shell()
                raise
        except Exception as e:
            logger.exception("save_quant_config 失败")
            return {'success': False, 'error': str(e)}

    # ── 方案管理 ──
    def get_schemes(self):
        """返回 {current, schemes: [{name, label, desc, meta}]}；meta 含市场/方向标签。"""
        try:
            quant_config.load_config()
            schemes = quant_config.get_schemes()
            return {
                'current': quant_config.get_current_scheme_name(),
                'schemes': [
                    {'name': k, 'label': k, 'desc': v.get('desc', ''),
                     'meta': quant_config.get_scheme_meta(k)}
                    for k, v in (schemes or {}).items()
                ],
            }
        except Exception as e:
            logger.exception("get_schemes 失败")
            return {'current': None, 'schemes': []}

    def get_scheme_periods(self, market='stock', direction=None):
        """返回该分类下已配置方案的 period 去重集合（稳定排序），供前端动态生成周期下拉。

        候选 = 该 market（及可选 direction）下所有方案 _meta.period 的集合；
        未配置任何周期时返回空列表（前端据此隐藏/提示，而非列出不存在的周期）。
        """
        try:
            quant_config.load_config()
            schemes = quant_config.get_schemes() or {}
            periods = set()
            for name, sc in schemes.items():
                if not isinstance(sc, dict):
                    continue
                meta = sc.get('_meta', {})
                if meta.get('market') != market:
                    continue
                if market == 'futures' and direction and meta.get('direction') != direction:
                    continue
                if meta.get('period'):
                    periods.add(meta['period'])
            order = ['日K', '周K', '60分钟', '30分钟', '15分钟', '5分钟', '1分钟']
            sorted_periods = sorted(periods, key=lambda p: order.index(p) if p in order else 99)
            return {'success': True, 'periods': sorted_periods}
        except Exception as e:
            logger.exception("get_scheme_periods 失败")
            return {'success': False, 'error': str(e)}

    def get_schemes_for_market(self, market='stock', direction=None):
        """返回该分类下方案清单（含 name/period/label），供诊断/扫描下拉做「按方案精确选择」。

        同 (市场,方向,周期) 有多个方案时，前端据本返回自行展开为「周期·名称」选项，
        避免 resolve_scheme 静默取第一个导致的方案被吞。
        """
        try:
            quant_config.load_config()
            schemes = quant_config.get_schemes() or {}
            out = []
            for name, sc in schemes.items():
                if not isinstance(sc, dict):
                    continue
                meta = sc.get('_meta', {})
                if meta.get('market') != market:
                    continue
                if market == 'futures' and direction and meta.get('direction') != direction:
                    continue
                out.append({
                    'name': name,
                    'period': meta.get('period') or '日K',
                    'label': name,
                    'direction': meta.get('direction') or 'long',
                    'mode': meta.get('mode'),   # 初/高级独立方案：前端据此做模式隔离下拉
                })
            order = ['日K', '周K', '60分钟', '30分钟', '15分钟', '5分钟', '1分钟']
            out.sort(key=lambda s: (order.index(s['period']) if s['period'] in order else 99, s['name']))
            return {'success': True, 'schemes': out}
        except Exception as e:
            logger.exception("get_schemes_for_market 失败")
            return {'success': False, 'error': str(e)}

    def get_market_config_status(self, market='stock', direction='long', period=None):
        """按市场+方向+周期返回方案配置完整性（门禁分流+周期隔离）。

        period 非空时，与回测/扫描/诊断入口使用同一套「精确到周期」的判定
        （check_scheme_complete 内部走 _merge_scheme_config(name, period) +
         REQUIRED_STRATEGY 统一常量），确保右上角 Banner 状态与运行入口红字 100% 一致。
        """
        try:
            quant_config.load_config(force_reload=True)
            ok, miss = quant_config.check_scheme_complete(market, direction, period=period)
            return {'success': True, 'market': market, 'direction': direction, 'period': period,
                    'complete': ok, 'missing': miss}
        except Exception as e:
            logger.exception("get_market_config_status 失败")
            return {'success': False, 'error': str(e)}

    # ── 快速起步向导（引导式，纯读取，不写参数）──
    def is_first_run(self):
        """是否首次启动（待配置态）。用于前端自动弹向导。"""
        try:
            from ui.onboarding_wizard import first_run
            return {'first_run': bool(first_run())}
        except Exception:
            return {'first_run': False}

    def get_wizard_steps(self):
        """返回向导 7 步定义 + 各步完成态。纯读取，不写盘。"""
        try:
            from ui.onboarding_wizard import STEPS, evaluate_state, overall_progress
            done = evaluate_state()
            n, total = overall_progress()
            steps = [{
                'id': s['id'], 'title': s['title'], 'summary': s['summary'],
                'instruction': s['instruction'], 'btn': s['btn'],
                'action': s['jump']['action'], 'mode': s['jump'].get('mode'),
                'web_pane': s['jump'].get('web_pane'),
                'done': bool(done.get(s['id'], False)),
            } for s in STEPS]
            return {'steps': steps, 'done': n, 'total': total}
        except Exception:
            logger.exception("get_wizard_steps 失败")
            return {'steps': [], 'done': 0, 'total': 0}

    def run_wizard_step(self, step_id):
        """向导"去操作"的后端动作（backtest / apply_ic）。其余动作由前端处理。"""
        try:
            from ui.onboarding_wizard import STEPS
            step = next((s for s in STEPS if s['id'] == step_id), None)
            if not step:
                return {'error': 'unknown step'}
            act = step['jump']['action']
            if act == 'backtest':
                return self.start_backtest({'mode': step['jump']['mode']})
            if act == 'apply_ic':
                return self.apply_factor_ic_result()
            return {'ok': True, 'action': act, 'note': '前端处理'}
        except Exception as e:
            logger.exception("run_wizard_step 失败")
            return {'error': str(e)}

    # ── 授权管理 ──
    def get_license_info(self):
        try:
            return get_license_info()
        except Exception as e:
            return {'status': 'error', 'message': str(e)}

    def get_help_docs(self):
        """返回关于/帮助页四个文档内容。

        优先从 docs/ 目录读取，找不到时回退到内嵌兜底文本。
        """
        import os as _os
        _root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        _docs_dir = _os.path.join(_root, 'docs')

        def _read_doc(filename, fallback):
            for d in [_docs_dir, _os.path.join(_root, 'docs')]:
                path = _os.path.join(d, filename)
                if _os.path.exists(path):
                    try:
                        with open(path, 'r', encoding='utf-8') as f:
                            return f.read()
                    except Exception:
                        pass
            return fallback

        _manual_fallback = """# M-Bull 使用说明书

> 版本：v2.0
> 适用对象：适合希望用规则约束交易并自定义分析模型的理性投资者

## 1. 这个软件能干什么
- 看行情、看 K 线
- 量化打分：因子预期分 + 星级评级
- 批量筛选：全市场扫描
- 纪律执行：加减仓参考条件
- 模型打造：因子管理 + 回测验证

## 2. 快速上手
[1] 输入股票名称或代码 → 点击「查询」
[2] 切换「K线图」或「量化分析」页签
[3] 「模型配置」中组合因子、调参
[4] 「全市场分析」批量筛选
[5] 「自选股诊断」批量评估

> 重要声明：本软件是技术分析工具，不是投资顾问。所有输出均基于用户配置与当前行情的对比触发结果，不构成投资建议。
"""

        _eval_fallback = """# M-Bull · 用户视角评测

> 一句话：把它当"带风控的量化参谋"很称职；别当"印钞机"。

## 评分（10分制）
- 风控能力 9 分：否决项 + 环境门控 + 抄底三维打分
- 数据稳定性 9 分：三源容灾
- 易用性 8.5 分：说明书全，但概念有门槛
- 回测速度 5.5 分：全市场数小时
- 基本面覆盖 0 分 / 实盘交易 0 分（设计如此）
"""

        _agreement_fallback = """# 用户协议与风险揭示书

## 一、软件性质
本软件是基于技术分析指标的辅助研究工具，不是投资顾问，不具备证券投资咨询资格，不提供任何投资建议、买卖指令或收益承诺。

## 二、数据来源与免责
行情数据来源于第三方公开接口，可能存在延迟、缺失或错误。

## 三、关于"参考条件"
报告中的"建仓 / 加仓 / 减仓 / 清仓参考条件"等表述，仅表示您自行设定的参数条件是否被行情满足的客观状态描述，并非本软件发出的任何交易建议或指令。

## 四、风险揭示
证券投资存在市场、流动性等风险，可能导致本金损失；历史表现不代表未来收益。

## 五、用户义务
您确认已具备相应风险承受能力，应仅使用可承受损失的资金投资，并遵守所在地区证券法律法规；因使用本软件产生的损益由您自行承担。

> 投资有风险，入市需谨慎。
"""

        _disclaimer_fallback = """# 免责声明

1. 非投资建议：本软件所有输出均仅供个人研究参考，不构成任何投资建议、投资咨询或买卖指令。
2. 非收益承诺：本软件不承诺、不保证任何收益或胜率；历史回测表现不代表未来收益。
3. 数据免责：行情数据来源于第三方公开接口，本软件不对数据的准确性、完整性、及时性承担责任。
4. 风险自担：证券投资决策及后果由用户独立承担。
5. 责任限制：在法律允许的最大范围内，本软件开发者不对因使用或无法使用本软件而造成的任何直接或间接损失承担责任。

> 投资有风险，入市需谨慎。
"""

        return {
            'manual': _read_doc('使用说明书.md', _manual_fallback),
            'eval': _read_doc('工具评测_用户视角.md', _eval_fallback),
            'agreement': _read_doc('USER_AGREEMENT.md', _agreement_fallback),
            'disclaimer': _read_doc('DISCLAIMER.md', _disclaimer_fallback),
        }

    def get_machine_id(self):
        return get_machine_id()

    def get_license_status_text(self):
        return get_status_text()

    # ── 市场宽度 ──
    def get_market_breadth(self):
        try:
            up, down, source = DataAPI.get_market_breadth()
            return {'up': up, 'down': down, 'source': source}
        except Exception as e:
            return {'up': None, 'down': None, 'source': None, 'error': str(e)}

    # ── 顶部指数行情 ──
    def get_market_indices(self):
        """返回顶部行情条数据。

        股票模式：上证指数/深证成指/创业板指/沪深300 + 涨跌家数。
        期货模式：文华商品指数替代 + 主力合约涨跌。
        """
        if self._market_type == 'futures':
            return self._get_futures_indices()

        from engine.data_layer import IndexFetcher
        codes = ['sh000001', 'sz399001', 'sz399006', 'sh000300']
        indices = []
        for code in codes:
            try:
                name, ret = IndexFetcher.get_daily_return(code)
                indices.append({
                    'name': name,
                    'code': code,
                    'change_pct': round(ret * 100, 2) if ret is not None else None,
                })
            except Exception as e:
                indices.append({'name': IndexFetcher.name_of(code), 'code': code, 'change_pct': None, 'error': str(e)})
        try:
            up, down, source = DataAPI.get_market_breadth()
        except Exception:
            up, down, source = None, None, None
        return {'indices': indices, 'up': up, 'down': down, 'source': source}

    def _get_futures_indices(self):
        """委托 engine.futures_indices.get_futures_indices（2026-09-13 上移，B17 清理）。"""
        from engine.futures_indices import get_futures_indices as _impl
        return _impl()

    def get_realtime_quote(self, code):
        """返回实时行情字段，用于右侧行情面板。

        根据市场类型分流：股票走 DataAPI，期货走 futures_data。
        """
        # 期货模式
        if self._market_type == 'futures':
            return self._get_futures_realtime_quote(code)
        # 股票模式：原有逻辑
        from engine.data_layer import DataAPI
        try:
            fields, source = DataAPI.get_realtime_quote(code)
            if not fields:
                return {'success': False, 'error': '行情数据不可用'}
            def _f(idx, default='—'):
                return fields[idx] if len(fields) > idx and fields[idx] != '' else default
            def _n(idx):
                try:
                    v = fields[idx]
                    return float(v) if v != '' else None
                except Exception:
                    return None
            name = _f(0, code)
            open_ = _n(1)
            prev_close = _n(2)
            price = _n(3)
            high = _n(4)
            low = _n(5)
            volume = _n(8)
            amount = _n(9)
            change = round(price - prev_close, 2) if price is not None and prev_close is not None else None
            change_pct = round((price - prev_close) / prev_close * 100, 2) if price is not None and prev_close else None
            # 新浪字段：30=日期，31=时间；腾讯兜底时 30/31 可能为空
            time_str = f"{_f(30)} {_f(31)}".strip() or '—'

            # 轻量停牌检测：平盘时拉 5 日 K 线判断
            suspension_warn = ''
            if price is not None and prev_close is not None and price == prev_close and price > 0:
                try:
                    df, _ = DataAPI.get_kline(code, "日K", 5)
                    if df is not None and len(df) > 0:
                        latest_date = df['trade_time'].max()
                        days_gap = (datetime.now() - latest_date).days
                        if days_gap > 1:
                            suspension_warn = f"注意：最新K线日期 {latest_date.strftime('%Y-%m-%d')}，已{days_gap}天未更新，可能停牌！"
                except Exception:
                    pass

            return {
                'success': True, 'source': source, 'name': name, 'code': code,
                'open': open_, 'prev_close': prev_close, 'price': price,
                'high': high, 'low': low, 'volume': volume, 'amount': amount,
                'change': change, 'change_pct': change_pct, 'time': time_str,
                'suspension_warn': suspension_warn,
            }
        except Exception as e:
            logger.exception('get_realtime_quote failed: %s', code)
            return {'success': False, 'error': str(e)}

    def _get_futures_realtime_quote(self, symbol):
        """期货实时行情，返回与 get_realtime_quote 相同结构。"""
        from engine.futures_pool import get_contract
        from engine.futures_data import fetch_futures_realtime

        try:
            contract = get_contract(symbol)
            if not contract:
                return {'success': False, 'error': f'未知期货品种: {symbol}'}

            rt = fetch_futures_realtime(symbol, contract.secid)
            if not rt:
                return {'success': False, 'error': '期货行情数据不可用'}

            price = float(rt.get('last', 0))
            prev_settle = float(rt.get('prev_settle', 0))
            open_ = float(rt.get('open', 0))
            high = float(rt.get('high', 0))
            low = float(rt.get('low', 0))
            volume = float(rt.get('volume', 0))
            amount = float(rt.get('amount', 0))
            open_interest = float(rt.get('open_interest', 0))

            change = round(price - prev_settle, 2) if price and prev_settle else None
            change_pct = round((price - prev_settle) / prev_settle * 100, 2) if price and prev_settle else None

            return {
                'success': True,
                'name': rt.get('name', contract.name),
                'code': symbol,
                'open': open_,
                'prev_close': prev_settle,
                'price': price,
                'high': high,
                'low': low,
                'volume': volume,
                'amount': amount,
                'change': change,
                'change_pct': change_pct,
                'time': datetime.now().strftime('%Y-%m-%d %H:%M'),
                'open_interest': open_interest,
                'market_type': 'futures',
            }
        except Exception as e:
            logger.exception('_get_futures_realtime_quote failed: %s', symbol)
            return {'success': False, 'error': str(e)}

    # ── 应用状态 ──
    def get_app_state(self):
        """返回应用状态：股票列表、当前方案、市场类型。"""
        try:
            quant_config.load_config()
        except Exception as e:
            logger.exception("get_app_state 重载配置失败")
            return {'error': 'config_load_failed', 'message': f'配置加载失败: {e}'}
        cur_name = quant_config.get_current_scheme_name()
        cur_label = cur_name
        if self._market_type == 'futures':
            from engine.futures_pool import all_contracts
            return {
                'stock_count': len(all_contracts()),
                'current_scheme': cur_name,
                'current_scheme_label': cur_label,
                'market_type': 'futures',
            }
        return {
            'stock_count': len(state.name_to_code),
            'current_scheme': cur_name,
            'current_scheme_label': cur_label,
            'market_type': 'stock',
        }

    # ═══════════════════════════════════════════════════════════
    # 自选股诊断
    # ═══════════════════════════════════════════════════════════
    def load_watchlist(self, market=None):
        """返回文件内容和示例默认。按市场隔离：股票→watchlist.txt，期货→watchlist_futures.txt。"""
        try:
            market = market or self._market_type
            path = WATCHLIST_FILE_FUTURES if market == 'futures' else WATCHLIST_FILE
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8-sig') as f:
                    content = f.read().strip()
                    return {'content': content, 'example': _watchlist_example(market)}
            return {'content': '', 'example': _watchlist_example(market)}
        except Exception as e:
            return {'content': '', 'example': _watchlist_example(market or self._market_type), 'error': str(e)}

    def save_watchlist(self, text, market=None):
        """按市场保存到 config/watchlist[_futures].txt。"""
        try:
            market = market or self._market_type
            path = WATCHLIST_FILE_FUTURES if market == 'futures' else WATCHLIST_FILE
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                f.write((text or '').strip())
            return {'success': True}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def save_decision_image(self, b64, filename):
        """将前端 html2canvas 生成的 PNG(base64) 保存到 M-Bull 数据目录 / decision_cards/。"""
        try:
            import base64
            root = get_app_dir()
            out_dir = os.path.join(root, 'decision_cards')
            os.makedirs(out_dir, exist_ok=True)
            fname = (filename or 'decision_card').strip().replace('/', '_').replace('\\', '_')
            if not fname.lower().endswith('.png'):
                fname += '.png'
            path = os.path.join(out_dir, fname)
            # b64 可能带 data:image/png;base64, 前缀，去掉
            if ',' in b64:
                b64 = b64.split(',', 1)[1]
            with open(path, 'wb') as f:
                f.write(base64.b64decode(b64))
            return {'success': True, 'path': path}
        except Exception as e:
            logger.exception('save_decision_image failed')
            return {'success': False, 'error': str(e)}

    def save_diagnosis_report(self, text, filename=None):
        """把自选诊断报告文本保存到 %LOCALAPPDATA%\\M-Bull\\diagnosis_reports\\。

        对齐 decision_cards/reports 的落盘范式：打包版 get_app_dir()=%LOCALAPPDATA%\\M-Bull，
        开发态为项目根目录。返回 {success, path} 供前端提示实际保存位置。
        """
        try:
            text = text or ''
            out_dir = os.path.join(get_app_dir(), 'diagnosis_reports')
            os.makedirs(out_dir, exist_ok=True)
            fname = (filename or '').strip() or '自选股诊断报告'
            fname = fname.replace('/', '_').replace('\\', '_')
            if not fname.lower().endswith('.txt'):
                fname += '.txt'
            path = os.path.join(out_dir, fname)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(text)
            return {'success': True, 'path': path}
        except Exception as e:
            logger.exception('save_diagnosis_report failed')
            return {'success': False, 'error': str(e)}

    def get_watchlist(self, market=None):
        """返回解析后的自选列表（供查询下拉使用），按市场隔离文件。"""
        try:
            market = market or self._market_type
            path = WATCHLIST_FILE_FUTURES if market == 'futures' else WATCHLIST_FILE
            content = ''
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8-sig') as f:
                    content = f.read()
            # 解析需按目标市场分流（_parse_watchlist 内部依赖 self._market_type）
            saved_mt = self._market_type
            self._market_type = market
            try:
                items = self._parse_watchlist(content)
            finally:
                self._market_type = saved_mt
            result = []
            for it in items:
                code = it.get('code') or ''
                name = it.get('name') or ''
                if not code:
                    continue
                result.append({
                    'code': code,
                    'name': name,
                    'label': f"{name} {code.replace('sh', '').replace('sz', '')}",
                })
            return {'success': True, 'items': result}
        except Exception as e:
            logger.exception('get_watchlist 失败')
            return {'success': False, 'error': str(e), 'items': []}

    def _parse_watchlist(self, text):
        """委托 engine.watchlist_parser.parse_watchlist（2026-09-13 上移，F-401）。"""
        from engine.watchlist_parser import parse_watchlist
        return parse_watchlist(text, self._market_type)

    def _is_valid_code(self, code):
        """按市场类型校验品种代码是否可识别。

        股票：sh/sz 前缀或 6 位数字；期货：futures_pool 可解析（rb / jd2609 等）。
        """
        if not code:
            return False
        if self._market_type == 'futures':
            from engine.futures_pool import parse_contract_code
            return parse_contract_code(str(code))[0] is not None
        code = str(code)
        return code.startswith(('sh', 'sz')) or (len(code) == 6 and code.isdigit())

    @staticmethod
    def _build_diagnosis_report(results, summary, market_type='stock', k_type='日K'):
        """生成文本诊断报告（等宽固定排版）。

        k_type：本次诊断的分析周期。A股分钟周期(新浪未复权)时在报告尾部标注数据来源，
        与日K前复权区分（未复权跨除权日跳变、跨周期不衔接）；期货无复权概念不标注。
        """
        W = 95
        lines = []
        lines.append("=" * W)
        title = '自选品种综合报告' if market_type == 'futures' else '自选股综合报告'
        lines.append(f"  {title}  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        lines.append("-" * W)
        # 环境标签 + 仓位上限
        env_label = summary.get('env_label', '')
        pos_limit = summary.get('pos_limit', '')
        if env_label or pos_limit:
            lines.append(f"  环境：{env_label}  |  仓位上限：{pos_limit}")
        # 大盘熔断状态
        circuit = summary.get('circuit_breaker', '')
        if circuit:
            lines.append(f"  大盘熔断：{circuit}")
        # 合规提示
        lines.append(f"  持仓: {summary.get('position', 0)}只  |  建仓参考条件: {summary.get('buy', 0)}只  |  观察: {summary.get('watch', 0)}只  |  回避: {summary.get('avoid', 0)}只")
        if summary.get('error'):
            lines.append(f"  失败: {summary.get('error')}只")
        # 涨跌家数未获取时警告
        if not summary.get('breadth_fetched'):
            lines.append("  ⚠ 未获取涨跌家数，市场宽度按中性(50%)计算，结果仅供参考")
        # 数据来源标注（A股；期货无复权概念不标注）：
        # 按当前周期的数据源映射显示（通达信自算前复权 / 腾讯前复权 / 新浪未复权），
        # 用户切换数据源后在报告里可见；分钟未复权额外提示跨除权日跳变。
        if market_type == 'stock':
            try:
                from engine.data_layer import KLineFetcher
                _kt = k_type if k_type in K_TYPE_MAP else '日K'
                _src = KLineFetcher._kline_source_for(K_TYPE_MAP.get(_kt, 240))
                _label = KLineFetcher.source_label(_src)
                _note = f"  ※ 数据来源：{_kt} · {_label}"
                if _kt not in ('日K', '周K') and _src in ('sina_raw', 'tdx_minute'):
                    _note += '（分钟未复权，跨除权日价格可能跳变，与日K前复权不衔接）'
                lines.append(_note)
            except Exception:
                pass
        lines.append("  ※ 以上结论仅供参考，不构成投资建议")
        lines.append("-" * W)

        position = [r for r in results if r.get('category') == 'position']
        buy = [r for r in results if r.get('category') == 'buy']
        watch = [r for r in results if r.get('category') == 'watch']
        avoid = [r for r in results if r.get('category') == 'avoid']
        error = [r for r in results if r.get('category') == 'error']

        if position:
            lines.append("\n[持仓管理]")
            if market_type == 'futures':
                lines.append(f"{'代码':<10} {'名称':<8} {'方向':<4} {'持仓价':>8} {'现价':>8} {'浮盈':>8} {'因子预期分':>10} {'已触发的条件':<24} {'股票当前类型':<12}")
                for r in position:
                    _d = '空' if r.get('direction', 'long') == 'short' else '多'
                    lines.append(f"{r['code']:<10} {r['name']:<8} {_d:<4} {r.get('entry_price', 0):>8.2f} {r['price']:>8.2f} {r.get('pnl', '—'):>8} {r.get('factorScore', 0):>10.0f} {r.get('triggeredConditions', '正常持有'):<24} {r.get('stockCurrentType', ''):<12}")
            else:
                lines.append(f"{'代码':<10} {'名称':<8} {'持仓价':>8} {'现价':>8} {'浮盈':>8} {'因子预期分':>10} {'已触发的条件':<24} {'股票当前类型':<12}")
                for r in position:
                    lines.append(f"{r['code']:<10} {r['name']:<8} {r.get('entry_price', 0):>8.2f} {r['price']:>8.2f} {r.get('pnl', '—'):>8} {r.get('factorScore', 0):>10.0f} {r.get('triggeredConditions', '正常持有'):<24} {r.get('stockCurrentType', ''):<12}")
        if buy:
            lines.append("\n[建仓参考条件]")
            lines.append(f"{'代码':<10} {'名称':<8} {'现价':>8} {'因子预期分':>10} {'已触发的条件':<24} {'仓位':>8} {'股票当前类型':<12}")
            for r in buy:
                lines.append(f"{r['code']:<10} {r['name']:<8} {r['price']:>8.2f} {r.get('factorScore', 0):>10.0f} {r.get('triggeredConditions', '未触及'):<24} {r.get('positionAllocation', '—'):>8} {r.get('stockCurrentType', ''):<12}")
        if watch:
            lines.append("\n[观察]")
            lines.append(f"{'代码':<10} {'名称':<8} {'现价':>8} {'因子预期分':>10} {'已触发的条件':<24} {'股票当前类型':<12}")
            for r in watch:
                lines.append(f"{r['code']:<10} {r['name']:<8} {r['price']:>8.2f} {r.get('factorScore', 0):>10.0f} {r.get('triggeredConditions', '未触及'):<24} {r.get('stockCurrentType', ''):<12}")
        if avoid:
            lines.append("\n[回避]")
            lines.append(f"{'代码':<10} {'名称':<8} {'现价':>8} {'因子预期分':>10} {'已触发的条件':<24} {'股票当前类型':<12}")
            for r in avoid:
                lines.append(f"{r['code']:<10} {r['name']:<8} {r['price']:>8.2f} {r.get('factorScore', 0):>10.0f} {r.get('triggeredConditions', '回避'):<24} {r.get('stockCurrentType', ''):<12}")
        if error:
            lines.append("\n[获取失败]")
            for r in error:
                lines.append(f"  {r['code']} {r['name']} -> {r.get('error', '未知错误')}")
        lines.append("\n" + "=" * W)
        return "\n".join(lines)

    def diagnose_watchlist(self, text, up_count=None, down_count=None):
        """解析 text，按行诊断，返回分类结果。

        支持前端手动传入涨跌家数；配置缺失时返回具体缺失项。
        """
        from ui.report_classify import _classify_for_report
        try:
            allowed, reason = can_use('query')
            if not allowed:
                return {'error': f"授权受限: {reason}"}

            items = self._parse_watchlist(text)
            if not items:
                return {'error': '未识别到有效股票'}

            # 配置完整性校验（按市场+方向加载对应方案；门禁分流）——与回测/诊断/Banner统一SSOT
            try:
                first_dir = items[0].get('direction', 'long') if items else 'long'
                quant_config.load_market_scheme(self._market_type, first_dir, force_reload=True)
                _cfg = quant_config._safe_cfg()
                # 初级用法（因子休眠）：诊断降级为技术指标+加减仓提示，跳过因子门槛
                if not self._is_basic_mode():
                    if not (_cfg.get('active_factors') or []):
                        return {'error': '配置不完整：无启用因子，请到「模型配置」勾选因子并保存。'}
                    quant_config.require_config(_cfg, quant_config.REQUIRED_STRATEGY)
            except quant_config.ConfigIncompleteError as e:
                return {'error': f"配置不完整：{'、'.join(e.missing)}，请到「模型配置」补全。"}

            # 市场宽度（期货多空双向无市场宽度概念，直接按中性处理）
            is_futures = self._market_type == 'futures'
            up_ratio = 0.5
            breadth_fetched = is_futures  # 期货视为已获取，避免"未获取涨跌家数"警告
            if not is_futures:
                try:
                    if up_count is not None and down_count is not None and (up_count + down_count) > 0:
                        up_ratio = up_count / (up_count + down_count)
                        breadth_fetched = True
                    else:
                        auto_up, auto_down, _ = DataAPI.get_market_breadth()
                        if auto_up is not None and auto_down is not None:
                            up_ratio = auto_up / (auto_up + auto_down) if (auto_up + auto_down) > 0 else 0.5
                            breadth_fetched = True
                except Exception:
                    pass

            # 环境标签
            env_label = '中性'
            try:
                from engine.market_gate import MarketGate
                env_label = MarketGate.get_environment(up_ratio).get('label', '中性')
            except Exception:
                pass

            # 仓位上限
            max_pos = '—'
            try:
                risk_params = quant_config.get_risk_params() or {}
                msp = float(risk_params.get('max_single_position', 0) or 0)
                if msp > 0:
                    max_pos = f"{msp * 100:.0f}%"
            except Exception:
                pass

            # 大盘熔断状态（期货无大盘指数，标记不适用）
            crash_status = '不适用' if is_futures else '未启用'
            if not is_futures:
                try:
                    risk_params = quant_config.get_risk_params() or {}
                    crash_pct = float(risk_params.get('market_crash_pct', 0) or 0)
                    if crash_pct > 0:
                        crash_status = '未触发'
                        # 简单判断：取基准指数当日涨跌幅
                except Exception:
                    pass

            results = []
            summary = {
                'total': len(items), 'buy': 0, 'watch': 0, 'avoid': 0, 'position': 0, 'error': 0,
                'env_label': env_label, 'pos_limit': max_pos,
                'circuit_breaker': crash_status, 'breadth_fetched': breadth_fetched,
            }

            for item in items:
                try:
                    code = item['code']
                    if not self._is_valid_code(code):
                        results.append({
                            'code': item['code'], 'name': item['name'], 'price': 0, 'score': 0,
                            'status': 'error', 'category': 'error', 'error': '未找到品种' if is_futures else '未找到股票'
                        })
                        summary['error'] += 1
                        continue

                    logger.info(f"[diagnose] 开始第 {len(results)+1}/{len(items)} 只: {code} market={self._market_type} direction={item.get('direction','long')}")
                    diag_logger.info(f"[diagnose] 开始第 {len(results)+1}/{len(items)} 只: {code} market={self._market_type} direction={item.get('direction','long')}")
                    if is_futures:
                        result = self._analyze_futures(
                            symbol=code, name=item['name'],
                            entry_price=item.get('entry_price') if item.get('entry_price', 0) > 0 else None,
                            bars_held=item.get('bars_held', 0),
                            _skip_quota=True,
                            direction=item.get('direction', 'long'),
                        )
                    else:
                        result = self.analyze(
                            code, name=item['name'],
                            entry_price=item.get('entry_price') if item.get('entry_price', 0) > 0 else None,
                            bars_held=item.get('bars_held', 0),
                            _skip_quota=True,
                            direction=item.get('direction', 'long'),
                        )
                    logger.info(f"[diagnose] 完成第 {len(results)+1} 只: {code} result={bool(result and result.get('success'))} error={result.get('error') if result else None}")
                    diag_logger.info(f"[diagnose] 完成第 {len(results)+1} 只: {code} result={bool(result and result.get('success'))} error={result.get('error') if result else None}")
                    if result.get('error'):
                        results.append({
                            'code': code, 'name': item['name'], 'price': 0, 'score': 0,
                            'status': 'error', 'category': 'error', 'error': result['error']
                        })
                        summary['error'] += 1
                        continue

                    report_data = result.get('reportData', {})
                    category = _classify_for_report(report_data)
                    summary[category] = summary.get(category, 0) + 1

                    # 持仓浮盈（按方向计算：多头 (现价-成本)/成本，空头反向）
                    entry_price = item.get('entry_price', 0)
                    direction = item.get('direction', 'long')
                    price = report_data.get('keyLevels', {}).get('current', 0)
                    pnl = ''
                    if entry_price > 0 and price > 0:
                        pnl_pct = ((entry_price - price) / entry_price * 100) if direction == 'short' else ((price - entry_price) / entry_price * 100)
                        pnl = f"{pnl_pct:+.1f}%"

                    advice = ''
                    if category == 'position':
                        advice = report_data.get('positionConclusion', '')
                    elif category == 'buy':
                        advice = report_data.get('decisionConclusion', '') or report_data.get('entry_action', '')
                    elif category == 'avoid':
                        advice = report_data.get('decisionConclusion', '') or '回避'
                    else:
                        advice = report_data.get('decisionConclusion', '') or '观望等待'

                    results.append({
                        'code': code,
                        'name': item['name'],
                        'price': price,
                        'factorScore': report_data.get('factorScore', 0) or 0,
                        'triggeredConditions': report_data.get('triggeredConditions', advice),
                        'positionAllocation': report_data.get('positionAllocation', '—'),
                        'stockCurrentType': report_data.get('stockCurrentType', report_data.get('stockType', '')),
                        'category': category,
                        'entry_price': entry_price,
                        'bars_held': item.get('bars_held', 0),
                        'direction': direction,
                        'pnl': pnl,
                        'error': None,
                        'advice': advice,
                    })
                except Exception as e:
                    results.append({
                        'code': item.get('code', ''), 'name': item.get('name', ''),
                        'price': 0, 'score': 0, 'status': 'error',
                        'category': 'error', 'error': str(e)
                    })
                    summary['error'] += 1

            # 整批诊断仅扣 1 次额度（而非每只扣 1 次）
            consume_use()

            return {
                'results': results,
                'summary': summary,
                'report_text': self._build_diagnosis_report(results, summary, market_type='futures' if is_futures else 'stock', k_type='日K'),
                'env_label': env_label,
                'max_pos': max_pos,
                'crash_status': crash_status,
                'breadth_fetched': breadth_fetched,
            }
        except Exception as e:
            logger.exception("diagnose_watchlist 失败")
            return {'error': f"诊断失败: {e}"}

    # ── 异步诊断（逐只进度反馈 + 中途取消）──

    def start_diagnosis(self, text, up_count=None, down_count=None, market=None, k_type='日K', scheme_name=None, direction=None, source='manual'):
        """启动异步诊断，返回 task_id。

        k_type：分析周期（期货按周期评分，A 股固定日 K 由 analyze 内部处理）。
        scheme_name：非空时强制使用指定方案（监控让用户自选策略，而非按市场+方向
            自动解析的当前方案）。为空则回落默认的「市场+方向」解析。
        direction：仅 'long'/'short' 时生效——只分析自选中该方向的标的（监控
            多单/空单双槽位用：多单槽位传 long、空单槽位传 short）；None 表示不过滤
            （手动诊断全量分析，方向由各标的自选文本第4字段决定）。
        source：'manual'（用户手动诊断）或 'monitor'（监控后台扫描）——
            监控扫描避开手动诊断运行期（资源/额度不抢占）；任务按 source 标记。

        前端通过 get_diagnosis_progress(task_id) 轮询进度，
        通过 cancel_diagnosis(task_id) 中途停止。
        """
        try:
            # 显式市场参数双保险（前端切期货后诊断应按期货分析，而非依赖当前 self._market_type）
            if market:
                self._market_type = market
            diag_logger.info(f"[diag-entry] start_diagnosis 进入 market={self._market_type} text_len={len(text or '')}")
            allowed, reason = can_use('query')
            diag_logger.info(f"[diag-entry] can_use('query') 返回 allowed={allowed} reason={reason}")
            if not allowed:
                return {'error': f"授权受限: {reason}"}

            items = self._parse_watchlist(text)
            diag_logger.info(f"[diag-entry] _parse_watchlist 返回 n={len(items)}")
            if not items:
                return {'error': '未识别到有效股票'}
            # 监控多空双槽位：direction 指定时只分析该方向标的（first_dir 同步覆盖）
            if direction in ('long', 'short'):
                items = [it for it in items if it.get('direction', 'long') == direction]
                diag_logger.info(f"[diag-entry] 按方向 {direction} 过滤后 n={len(items)}")
                if not items:
                    return {'error': f'自选中无{direction}方向标的，跳过该方向扫描'}

            # 配置完整性校验（按市场+方向/或指定方案名加载对应方案；门禁分流）
            try:
                first_dir = direction or (items[0].get('direction', 'long') if items else 'long')
                # ①′ 数据源限制：腾讯源无分钟级K线，A股分钟级诊断明确报错（而非"未配置方案"误导）
                if self._market_type == 'stock' and k_type in ('1分钟', '5分钟', '15分钟', '30分钟', '60分钟'):
                    from engine.data_layer import KLineFetcher
                    if KLineFetcher._kline_source_for(K_TYPE_MAP.get(k_type, 15)) is None:
                        return {'error': f"当前数据源为「腾讯」（仅日K/周K），无法做 {k_type} 级诊断："
                                         f"请到「数据源设置」切换为「新浪（未复权）」后再试。"}
                # ① 尊重当前激活方案：其 市场/方向(期货)/周期(期货) 与本次诊断一致时复用。
                # （缺 _meta 的旧方案已自动补全为 stock/日K，严格检查不误伤；
                #   保留市场/方向/周期校验可防止跨市场、跨周期错配。）
                diag_logger.info(f"[diag-entry] scheme_name 传入={scheme_name}, k_type={k_type}, market={self._market_type}")
                if not scheme_name:
                    cur = quant_config.get_current_scheme_name()
                    diag_logger.info(f"[diag-entry] 当前激活方案 cur={cur}")
                    if cur:
                        cur_meta = quant_config.get_scheme_meta(cur) or {}
                        if cur_meta.get('market') == self._market_type:
                            dir_ok = (self._market_type != 'futures') or (cur_meta.get('direction') == first_dir)
                            per_ok = (self._market_type != 'futures') or (not k_type) or (cur_meta.get('period') == k_type)
                            if dir_ok and per_ok:
                                scheme_name = cur
                # ② 加载（期货按周期精确匹配；A股自 2026-08-15 起与期货对齐，
                #    同样按 (A股,long,k_type) 精确匹配周期方案——无精确匹配明确报错，
                #    不静默回落日K方案（避免「选15分钟却用日K参数」错配））
                if self._market_type == 'futures':
                    cfg = quant_config.load_market_scheme(
                        self._market_type, first_dir, k_type or None,
                        force_reload=True, scheme_name=scheme_name, strict_period=True)
                else:
                    cfg = quant_config.load_market_scheme(
                        self._market_type, first_dir, k_type or None,
                        force_reload=True, scheme_name=scheme_name, strict_period=True)
                diag_logger.info(f"[diag-entry] cfg={'found' if cfg else 'None'}, scheme_name={scheme_name}")
                if cfg is None:
                    dir_label = '多单' if first_dir == 'long' else '空单'
                    if self._market_type == 'futures':
                        if k_type and k_type != '日K':
                            return {'error': f"未配置 期货·{dir_label}·{k_type} 方案：请到「模型配置」先创建该周期方案，再做诊断。"}
                        return {'error': f"未配置 期货·{dir_label} 方案：请到「模型配置」创建。"}
                    if k_type and k_type != '日K':
                        return {'error': f"未配置 A股·{k_type} 方案：请到「模型配置」先创建该周期方案，再做诊断。"}
                    return {'error': f"未配置 A股 方案：请到「模型配置」创建。"}
                _cfg = quant_config._safe_cfg()
                # 初级用法（因子休眠）：诊断降级为技术指标+加减仓提示，跳过因子门槛
                if not self._is_basic_mode():
                    if not (_cfg.get('active_factors') or []):
                        return {'error': '配置不完整：无启用因子，请到「模型配置」勾选因子并保存。'}
                    quant_config.require_config(_cfg, quant_config.REQUIRED_STRATEGY)
                diag_logger.info(f"[diag-entry] load_market_scheme+require_config 通过 first_dir={first_dir}")
            except quant_config.ConfigIncompleteError as e:
                return {'error': f"配置不完整：{'、'.join(e.missing)}，请到「模型配置」补全。"}

            task_id = str(uuid.uuid4())
            task = {
                'task_id': task_id,
                'is_running': True,
                'is_cancelled': False,
                'source': source,   # 'manual' | 'monitor'（监控避开手动诊断运行期）
                'total': len(items),
                'current': 0,
                'results': [],
                'summary': {},
                'env_label': '',
                'max_pos': '—',
                'crash_status': '未启用',
                'breadth_fetched': False,
                'report_text': '',
                'error': None,
                'up_count': up_count,
                'down_count': down_count,
                'used_scheme_name': scheme_name,  # 本次诊断使用的方案名（前端结果区显示）
                'used_scheme_period': k_type,
            }
            self._diag_tasks[task_id] = task

            def _diag_worker():
                """后台诊断线程，逐只分析并更新进度。"""
                from ui.report_classify import _classify_for_report
                try:
                    is_futures = self._market_type == 'futures'
                    diag_logger.info(f"[diag_worker] 开始诊断任务 total={len(items)} market={self._market_type}")
                    # 市场宽度（期货多空双向无市场宽度概念，直接按中性处理）
                    up_ratio = 0.5
                    breadth_fetched = is_futures  # 期货视为已获取，避免"未获取涨跌家数"警告
                    if not is_futures:
                        try:
                            uc = task.get('up_count')
                            dc = task.get('down_count')
                            if uc is not None and dc is not None and (uc + dc) > 0:
                                up_ratio = uc / (uc + dc)
                                breadth_fetched = True
                                logger.info(f"[diag_worker] 使用手动涨跌家数 up={uc} down={dc}")
                            else:
                                diag_logger.info("[diag_worker] 获取市场宽度...")
                                auto_up, auto_down, _ = DataAPI.get_market_breadth()
                                diag_logger.info(f"[diag_worker] 市场宽度返回 up={auto_up} down={auto_down}")
                                if auto_up is not None and auto_down is not None:
                                    up_ratio = auto_up / (auto_up + auto_down) if (auto_up + auto_down) > 0 else 0.5
                                    breadth_fetched = True
                        except Exception as e:
                            logger.warning(f"[diag_worker] 市场宽度获取异常: {e}")
                            pass

                    # 环境标签
                    env_label = '中性'
                    try:
                        from engine.market_gate import MarketGate
                        env_label = MarketGate.get_environment(up_ratio).get('label', '中性')
                    except Exception:
                        pass

                    # 仓位上限
                    max_pos = '—'
                    try:
                        risk_params = quant_config.get_risk_params() or {}
                        msp = float(risk_params.get('max_single_position', 0) or 0)
                        if msp > 0:
                            max_pos = f"{msp * 100:.0f}%"
                    except Exception:
                        pass

                    # 大盘熔断状态（期货无大盘指数，标记不适用）
                    crash_status = '不适用' if is_futures else '未启用'
                    if not is_futures:
                        try:
                            risk_params = quant_config.get_risk_params() or {}
                            crash_pct = float(risk_params.get('market_crash_pct', 0) or 0)
                            if crash_pct > 0:
                                diag_logger.info(f"[diag_worker] 检查大盘熔断 index={risk_params.get('market_crash_index','sh000300')}")
                                crash_status = '未触发'
                                try:
                                    from engine.data_layer import IndexFetcher
                                    idx = risk_params.get('market_crash_index', 'sh000300') or 'sh000300'
                                    _name, _ret = IndexFetcher.get_daily_return(idx)
                                    diag_logger.info(f"[diag_worker] 大盘熔断指数返回 {_name} {_ret}")
                                    if _ret is not None and _ret <= -crash_pct:
                                        crash_status = f'已触发（{_name} {_ret*100:+.2f}%）'
                                    elif _ret is not None:
                                        crash_status = f'未触发（{_name} {_ret*100:+.2f}%）'
                                except Exception as e:
                                    logger.warning(f"[diag_worker] 熔断指数获取异常: {e}")
                                    pass
                        except Exception:
                            pass

                    diag_logger.info(f"[diag_worker] 初始化完成 up_ratio={up_ratio:.3f} breadth={breadth_fetched} env={env_label} max_pos={max_pos} crash={crash_status}")
                    task['env_label'] = env_label
                    task['max_pos'] = max_pos
                    task['crash_status'] = crash_status
                    task['breadth_fetched'] = breadth_fetched

                    summary = {
                        'total': len(items), 'buy': 0, 'watch': 0, 'avoid': 0, 'position': 0, 'error': 0,
                        'env_label': env_label, 'pos_limit': max_pos,
                        'circuit_breaker': crash_status, 'breadth_fetched': breadth_fetched,
                    }
                    results = []

                    for idx_i, item in enumerate(items):
                        # 取消检查
                        if task.get('is_cancelled'):
                            break
                        try:
                            code = item['code']
                            diag_logger.info(f"[diag_worker] 开始第 {idx_i+1}/{len(items)} 只: {code} market={self._market_type} direction={item.get('direction','long')}")
                            if not self._is_valid_code(code):
                                logger.info(f"[diag_worker] 第 {idx_i+1} 只 {code} 校验失败")
                                results.append({
                                    'code': item['code'], 'name': item['name'], 'price': 0, 'score': 0,
                                    'status': 'error', 'category': 'error', 'error': '未找到品种' if is_futures else '未找到股票'
                                })
                                summary['error'] += 1
                            else:
                                if is_futures:
                                    result = self._analyze_futures(
                                        symbol=code, name=item['name'],
                                        entry_price=item.get('entry_price') if item.get('entry_price', 0) > 0 else None,
                                        bars_held=item.get('bars_held', 0),
                                        _skip_quota=True,
                                        direction=item.get('direction', 'long'),
                                        k_type=k_type,
                                        scheme_name=scheme_name,
                                    )
                                else:
                                    result = self.analyze(
                                        code, name=item['name'],
                                        entry_price=item.get('entry_price') if item.get('entry_price', 0) > 0 else None,
                                        bars_held=item.get('bars_held', 0),
                                        _skip_quota=True,
                                        direction=item.get('direction', 'long'),
                                        k_type=k_type,
                                        scheme_name=scheme_name,
                                    )
                                diag_logger.info(f"[diag_worker] 完成第 {idx_i+1} 只: {code} result={bool(result and result.get('success'))} error={result.get('error') if result else None}")
                                if result.get('error'):
                                    results.append({
                                        'code': code, 'name': item['name'], 'price': 0, 'score': 0,
                                        'status': 'error', 'category': 'error', 'error': result['error']
                                    })
                                    summary['error'] += 1
                                else:
                                    report_data = result.get('reportData', {})
                                    category = _classify_for_report(report_data)
                                    summary[category] = summary.get(category, 0) + 1

                                    entry_price = item.get('entry_price', 0)
                                    direction = item.get('direction', 'long')
                                    price = report_data.get('keyLevels', {}).get('current', 0)
                                    pnl = ''
                                    if entry_price > 0 and price > 0:
                                        pnl_pct = ((entry_price - price) / entry_price * 100) if direction == 'short' else ((price - entry_price) / entry_price * 100)
                                        pnl = f"{pnl_pct:+.1f}%"

                                    advice = ''
                                    if category == 'position':
                                        advice = report_data.get('positionConclusion', '')
                                    elif category == 'buy':
                                        advice = report_data.get('decisionConclusion', '') or report_data.get('entry_action', '')
                                    elif category == 'avoid':
                                        advice = report_data.get('decisionConclusion', '') or '回避'
                                    else:
                                        advice = report_data.get('decisionConclusion', '') or '观望等待'

                                    results.append({
                                        'code': code,
                                        'name': item['name'],
                                        'price': price,
                                        'factorScore': report_data.get('factorScore', 0) or 0,
                                        'triggeredConditions': report_data.get('triggeredConditions', advice),
                                        'positionAllocation': report_data.get('positionAllocation', '—'),
                                        'stockCurrentType': report_data.get('stockCurrentType', report_data.get('stockType', '')),
                                        'category': category,
                                        'entry_price': entry_price,
                                        'bars_held': item.get('bars_held', 0),
                                        'direction': direction,
                                        'pnl': pnl,
                                        'error': None,
                                        'advice': advice,
                                    })
                        except Exception as e:
                            results.append({
                                'code': item.get('code', ''), 'name': item.get('name', ''),
                                'price': 0, 'score': 0, 'status': 'error',
                                'category': 'error', 'error': str(e)
                            })
                            summary['error'] += 1

                        task['current'] = idx_i + 1
                        task['results'] = results
                        task['summary'] = dict(summary)

                    # 整批诊断仅扣 1 次额度
                    if not task.get('is_cancelled'):
                        consume_use()

                    task['report_text'] = self._build_diagnosis_report(results, summary, market_type='futures' if is_futures else 'stock', k_type=k_type)
                    task['summary'] = dict(summary)
                    task['is_running'] = False
                except Exception as e:
                    logger.exception("异步诊断失败")
                    task['error'] = str(e)
                    task['is_running'] = False

            diag_logger.info(f"[diag-entry] 启动 _diag_worker 线程 total={len(items)}")
            threading.Thread(target=_diag_worker, daemon=True).start()
            return {'task_id': task_id}
        except Exception as e:
            logger.exception("start_diagnosis 失败")
            return {'error': f"启动诊断失败: {e}"}

    def start_monitor(self):
        """启动 L3 后台监控守护（形态 A：app 内常驻线程）。

        定时按交易时段跑自选诊断，提取触发的信号，去重后推送。
        幂等：重复调用不会起多个线程。
        """
        try:
            from ui.monitor import Monitor
            if getattr(self, '_monitor', None) is None:
                self._monitor = Monitor(self)
            self._monitor.start()
            logger.info("L3 监控守护已启动（形态 A：app 常驻后台线程）")
        except Exception as e:
            logger.warning("启动 L3 监控失败: %s", e)

    def stop_monitor(self):
        """停止 L3 后台监控守护线程（关闭窗口即失效，属形态 A 已知边界）。"""
        try:
            mon = getattr(self, '_monitor', None)
            if mon is not None:
                mon.stop()
                logger.info("L3 监控守护已请求停止")
        except Exception as e:
            logger.warning("停止 L3 监控失败: %s", e)

    # ── L3 监控配置（策略 + 频率用户可配置）────────────────────────────
    def get_monitor_config(self):
        """返回监控配置（含按市场筛选的可选策略列表），供前端「监控设置」面板渲染。

        每个市场给出：enabled（开关）、strategy（当前方案名，None=自动）、
        k_type（分析周期）、strategies（下拉选项：自动 + 该市场全部方案）。
        另含全局 interval_seconds、align_to_grid、running（是否运行中）。
        """
        try:
            from ui.monitor import DEFAULT_INTERVAL_SECONDS, DEFAULT_SESSIONS
            p = os.path.join(CONFIG_DIR, "monitor.json")
            cfg = {}
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    cfg = json.load(f) or {}
            interval = int(cfg.get("interval_seconds", DEFAULT_INTERVAL_SECONDS))
            align = bool(cfg.get("align_to_grid", True))
            markets_src = cfg.get("markets") or {}
            # 可选策略：来自 get_schemes（与「模型配置」同一套方案）
            all_schemes = self.get_schemes().get("schemes", [])

            def opts(market_key):
                # 不再有「自动」占位项：用户必须显式选方案（若无方案则下拉为空，提示去创建）
                out = []
                for s in all_schemes:
                    meta = (s.get("meta") or {})
                    scheme_market = meta.get("market", "")
                    if market_key == "futures":
                        # 严格匹配 futures + 兜底无 market 字段的旧方案
                        if scheme_market == "futures" or not scheme_market:
                            out.append({"name": s["name"], "label": s["name"],
                                        "period": meta.get("period") or "日K",
                                        "direction": meta.get("direction") or "long"})
                    else:  # stock
                        # 严格匹配 stock + 兜底无 market 字段的旧方案（默认归为 A股）
                        if scheme_market == "stock" or not scheme_market:
                            out.append({"name": s["name"], "label": s["name"],
                                        "period": meta.get("period") or "日K",
                                        "direction": "long"})
                return out

            markets = {}
            for mk, mkey in (("stock", "stock"), ("futures", "futures")):
                m = (markets_src.get(mk) or {})
                if mk == "futures":
                    # 期货多空双槽位：long/short 各自配置方案+周期（旧格式顶层 strategy 迁移到 long）
                    long_cfg = m.get("long") or {}
                    short_cfg = m.get("short") or {}
                    legacy_strategy = m.get("strategy") or None
                    if "long" not in m and "short" not in m and legacy_strategy:
                        # 旧格式：按方案 direction 推断归属；无法推断放 long
                        _m = None
                        for s in all_schemes:
                            if s["name"] == legacy_strategy:
                                _m = s.get("meta") or {}
                                break
                        if _m and _m.get("direction") == "short":
                            short_cfg = {"strategy": legacy_strategy}
                        else:
                            long_cfg = {"strategy": legacy_strategy}
                    markets[mk] = {
                        "enabled": bool(m.get("enabled", True)),
                        "long": {
                            "enabled": bool(long_cfg.get("enabled", True)),
                            "strategy": long_cfg.get("strategy") or None,
                            "k_type": long_cfg.get("k_type", "日K"),
                        },
                        "short": {
                            "enabled": bool(short_cfg.get("enabled", True)),
                            "strategy": short_cfg.get("strategy") or None,
                            "k_type": short_cfg.get("k_type", "日K"),
                        },
                        "strategies": opts(mkey),
                    }
                else:
                    markets[mk] = {
                        "enabled": bool(m.get("enabled", True)),
                        "strategy": m.get("strategy") or None,
                        "k_type": m.get("k_type", "日K"),
                        "strategies": opts(mkey),
                    }
            running = bool(getattr(self, '_monitor', None)
                           and self._monitor._thread and self._monitor._thread.is_alive())
            return {"ok": True, "interval_seconds": interval, "align_to_grid": align,
                    "markets": markets, "running": running}
        except Exception as e:
            logger.exception("get_monitor_config 失败")
            return {"ok": False, "error": str(e)}

    def save_monitor_config(self, cfg):
        """保存监控配置到 monitor.json 并热重载（无需重启 app）。

        cfg: {interval_seconds, align_to_grid, markets:{stock:{enabled,strategy,k_type},
             futures:{enabled, long:{enabled,strategy,k_type}, short:{enabled,strategy,k_type}}}}
        sessions 保留内置默认（用户在面板只配 策略/周期/频率/开关）。
        """
        try:
            from ui.monitor import DEFAULT_SESSIONS
            os.makedirs(CONFIG_DIR, exist_ok=True)
            p = os.path.join(CONFIG_DIR, "monitor.json")
            interval = int((cfg or {}).get("interval_seconds", 900))
            align = bool((cfg or {}).get("align_to_grid", True))
            src_markets = (cfg or {}).get("markets") or {}
            markets = {}
            for mk in ("stock", "futures"):
                m = (src_markets.get(mk) or {})
                default_sessions = (DEFAULT_SESSIONS["stock"] if mk == "stock"
                                    else DEFAULT_SESSIONS["futures_day"] + DEFAULT_SESSIONS["futures_night"])
                if mk == "futures":
                    # 多空双槽位（兼容旧前端仅传顶层 strategy）
                    long_cfg = m.get("long") or {}
                    short_cfg = m.get("short") or {}
                    legacy_strategy = m.get("strategy") or None
                    if "long" not in m and "short" not in m and legacy_strategy:
                        long_cfg = {"strategy": legacy_strategy}
                    markets[mk] = {
                        "enabled": bool(m.get("enabled", True)),
                        "long": {
                            "enabled": bool(long_cfg.get("enabled", True)),
                            "strategy": long_cfg.get("strategy") or None,
                            "k_type": long_cfg.get("k_type", "日K"),
                            "sessions": long_cfg.get("sessions") or default_sessions,
                        },
                        "short": {
                            "enabled": bool(short_cfg.get("enabled", True)),
                            "strategy": short_cfg.get("strategy") or None,
                            "k_type": short_cfg.get("k_type", "日K"),
                            "sessions": short_cfg.get("sessions") or default_sessions,
                        },
                    }
                else:
                    markets[mk] = {
                        "enabled": bool(m.get("enabled", True)),
                        "strategy": m.get("strategy") or None,
                        "k_type": m.get("k_type", "日K"),
                        "sessions": m.get("sessions") or default_sessions,
                    }
            out = {"version": 2, "interval_seconds": interval,
                   "align_to_grid": align, "markets": markets}
            with open(p, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=2)
            mon = getattr(self, '_monitor', None)
            if mon is not None:
                mon.reload_config()  # 间隔/策略/开关即时生效
                mon.reload_notifiers()
            return {"ok": True}
        except Exception as e:
            logger.exception("save_monitor_config 失败")
            return {"ok": False, "error": str(e)}

    # ── L4 通知配置（渠道用户可配置）───────────────────────────────────
    def _notif_config_dir(self):
        try:
            return CONFIG_DIR
        except Exception:
            return os.path.join(os.getcwd(), "config")

    def get_notifiers_config(self):
        """返回渠道列表（不含凭据明文，只给 has_credential）供 UI 渲染。"""
        try:
            from engine.notifiers.registry import NotifierRegistry
            reg = NotifierRegistry.load(config_dir=self._notif_config_dir(), api=self)
            return {"ok": True, "channels": reg.describe()}
        except Exception as e:
            logger.warning("读取通知配置失败: %s", e)
            return {"ok": False, "error": str(e), "channels": []}

    def save_notifiers_config(self, channels):
        """保存渠道启用状态 + 凭据（仅更新提供的 key，不回显凭据）。

        channels: {key: {"enabled": bool, "credential": "..."(可选)}}
        返回 {"ok": bool, "error": str?}。保存后热重载运行中的监控注册表。
        """
        try:
            cfg_dir = self._notif_config_dir()
            nj_path = os.path.join(cfg_dir, "notifiers.json")
            cred_path = os.path.join(cfg_dir, "credentials.json")
            nj = self._read_json(nj_path) or {"version": 1, "channels": {}}
            chans = nj.get("channels") or {}
            creds = self._read_json(cred_path) or {}
            updates = channels if isinstance(channels, dict) else {}
            for key, upd in updates.items():
                if key not in chans or not isinstance(upd, dict):
                    continue
                if "enabled" in upd:
                    chans[key]["enabled"] = bool(upd["enabled"])
                cred = (upd.get("credential") or "").strip()
                if cred:
                    ck = chans[key].get("credential_key")
                    if ck:
                        creds[ck] = cred
            nj["channels"] = chans
            self._write_json(nj_path, nj)
            self._write_json(cred_path, creds)
            # 热重载运行中的监控注册表（无需重启 app）
            mon = getattr(self, "_monitor", None)
            if mon is not None:
                mon.reload_notifiers()
            return {"ok": True}
        except Exception as e:
            logger.warning("保存通知配置失败: %s", e)
            return {"ok": False, "error": str(e)}

    def test_notifier(self, channel):
        """构造假事件测试指定渠道（尤其微信/Server酱验证 webhook 可达）。"""
        try:
            from engine.notifiers.registry import NotifierRegistry
            reg = NotifierRegistry.load(config_dir=self._notif_config_dir(), api=self)
            event = {
                "market": "stock", "code": "600000", "name": "测试标的", "direction": "long",
                "triggered": "【测试】加仓1档 → 加仓15%",
                "price": 12.34, "score": 85, "pnl": "+1.2%", "time": "2026-08-15 14:30",
            }
            reg.test(channel, event)
            return {"ok": True, "message": "推送已发送，请检查对应渠道是否收到"}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    # ── K线数据源（A股，全局单选：通达信前复权 日K第一手 | 腾讯前复权 | 新浪未复权）──
    _KLINE_SOURCE_OPTIONS = {
        'tdx': {'display_name': '通达信（前复权·第一手）',
                'desc': '日K 前复权（通达信自算，与腾讯口径逐日偏差 0.000%），走通达信行情协议（TCP 7709）不受腾讯接口请求配额限制；'
                        '周K 同为通达信自算复权（除权周与腾讯服务端偏差 0.1%~0.94%，无除权周逐周一致，失败自动回退腾讯）；'
                        '分钟级（1/5/15/30/60分，未复权）深度远超新浪——实测 5 分 K 约 2.3 万根（≈2 年），新浪上限仅 1023 根（≈21 个交易日）',
                'periods': ['日K', '周K', '60分钟', '30分钟', '15分钟', '5分钟', '1分钟']},
        'tencent': {'display_name': '腾讯（前复权）',
                    'desc': '日K/周K 前复权，跨周期连续。无分钟级K线——不能做分钟级量化分析与自选诊断（周期范围仅日K/周K，与旧版一致）。⚠️ 受接口请求配额限制，全市场刷新增量会较快消耗',
                    'periods': ['日K', '周K']},
        'sina': {'display_name': '新浪（未复权）',
                 'desc': '含分钟级K线（未复权）：1/5/15/30/60分钟。可做分钟级量化分析与自选诊断；注意日K/周K 也为未复权，跨除权日价格跳变',
                 'periods': ['日K', '周K', '60分钟', '30分钟', '15分钟', '5分钟', '1分钟']},
    }

    # ── K线数据源（期货，独立于 A股：tdx=ExHq 第一手 | sina=HTTP 兜底 | eastmoney=HTTPS）──
    _FUTURES_KLINE_SOURCE_OPTIONS = {
        'tdx': {'display_name': '通达信（ExHq·第一手）',
                'desc': '期货日K走通达信扩展行情（TCP 7721），无 WAF 配额。主连 rb0 等仅通达信/新浪支持（东财对主连直接返回空）。⚠️ 主连历史深度上限 700 根（2023-11 起），请求超出自动落东财/新浪',
                'periods': ['日K']},
        'eastmoney': {'display_name': '东财（HTTPS）',
                      'desc': '东方财富 push2his，HTTPS 全周期。原生支持 113/114/115/8/42/142 期货 secid。注意：主力连续合约（rb0）东财不支持，会自动落到新浪/通达信',
                      'periods': ['日K', '60分钟', '30分钟', '15分钟', '5分钟', '1分钟']},
        'sina': {'display_name': '新浪（HTTP）',
                 'desc': '新浪 JSONP，HTTP/80 全周期。反爬较强，部分运行环境下不稳定（建议仅在东财/通达信不可达时切换）',
                 'periods': ['日K', '60分钟', '30分钟', '15分钟', '5分钟', '1分钟']},
    }

    def _kline_source_path(self):
        return os.path.join(self._notif_config_dir(), "data_sources.json")

    def get_data_sources(self):
        """返回 A股 K线数据源配置（全局单选 + 各源说明/周期范围）供 UI 渲染。"""
        try:
            from engine.data_layer import KLineFetcher
            d = self._read_json(self._kline_source_path()) or {}
            source = d.get('kline_source')
            if source not in ('tencent', 'sina', 'tdx'):
                source = 'tdx'
            # 同步实际生效映射（读取时以引擎为准，避免 config 与引擎缓存不一致）
            mapping = KLineFetcher._load_kline_sources()
            return {
                "ok": True,
                "source": source,
                "options": self._KLINE_SOURCE_OPTIONS,
                "periods": [{"period": p, "source": s}
                            for p, s in mapping.items() if s is not None],
            }
        except Exception as e:
            logger.warning("读取数据源配置失败: %s", e)
            return {"ok": False, "error": str(e), "source": "tdx", "options": {}}

    def save_data_sources(self, source):
        """保存 A股 K线**主数据源**（一键联动），写 data_sources.json 并热重载。

        联动规则（2026-09-19，用户要求的「一键切换」）：
          - kline_source 管 A股 日K/分钟/周K（KLineFetcher 内部分发）
          - 指数 IndexFetcher 读 kline_source：主源=tdx 走 tdx，否则走腾讯
          - 实时候选 registry.load 动态把主源插最前（能力矩阵判定）
          - 期货 futures_kline_source：主源支持期货（tdx/sina/eastmoney）则同步写；
            tencent 无期货源 → 保持原值不动
        """
        try:
            source = (source or 'tdx').strip().lower()
            if source not in ('tencent', 'sina', 'tdx'):
                return {"ok": False, "error": "无效的数据源：" + str(source)}
            path = self._kline_source_path()
            d = self._read_json(path) or {}
            d.setdefault("version", 3)
            d["kline_source"] = source
            # 一键联动 ①：期货源跟随主源（tencent 无期货源，保持原值）
            if source in ('tdx', 'sina', 'eastmoney'):
                d["futures_kline_source"] = source
            self._write_json(path, d)
            # 热重载：清空 KLineFetcher 源映射缓存与期货源缓存，无需重启
            from engine.data_layer import KLineFetcher
            KLineFetcher.reset_kline_sources()
            try:
                from engine.futures_data import reset_futures_kline_source
                reset_futures_kline_source()
            except Exception:
                pass
            return {"ok": True}
        except Exception as e:
            logger.warning("保存数据源配置失败: %s", e)
            return {"ok": False, "error": str(e)}

    def test_data_source(self, source=None, period=None):
        """实测指定数据源连通性（sh600000）。
        source: 'tencent'|'sina'|'tdx'——优先用调用方传入（前端当前 UI 选中的源），否则读磁盘持久化值。
        period: 仅 sina 时生效（测哪个分钟周期），tencent/tdx 固定测日K。
        """
        try:
            from engine.data_layer import KLineFetcher, K_TYPE_MAP
            d = self._read_json(self._kline_source_path()) or {}
            persisted = d.get('kline_source')
            source = (source or persisted or 'tdx').strip().lower()
            if source not in ('tencent', 'sina', 'tdx'):
                source = 'tdx'
            _names = {'tencent': '腾讯（前复权）', 'sina': '新浪（未复权）',
                      'tdx': '通达信（前复权）'}
            name = _names.get(source, source)
            pool_note = self._tdx_pool_note() if source == 'tdx' else ''
            if source == 'tdx':
                df, err = KLineFetcher._fetch_tdx_fq("sh600000", 10, full=False)
                p_label = '日K'
            elif source == 'tencent':
                df, err = KLineFetcher._fetch_tencent_fq("sh600000", 10)
                p_label = '日K'
            else:
                kt = K_TYPE_MAP.get(period or '15分钟', 15)
                df, err = KLineFetcher._fetch_sina_minute("sh600000", kt, days=10)
                p_label = period or '15分钟'
            extra = '' if source == persisted else f'（持久化值仍为{_names.get(persisted, persisted)}，需点保存才能生效）'
            if df is None or len(df) == 0:
                return {"ok": False, "message": f"{name} 获取失败: {err}{pool_note}"}
            last = df.iloc[-1]
            return {"ok": True,
                    "message": f"{name} 连通正常：{p_label} {len(df)}根，最新 {last['trade_time']} 收盘 {last['close']}{extra}{pool_note}"}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def _tdx_pool_note(self):
        """通达信节点池健康摘要，追加在「数据源测试」结果后（不新增 UI 结构）。"""
        try:
            from engine.data_sources import tdx_nodes
            st = tdx_nodes.status()
            src = {'persisted': '探测持久化', 'seed': '种子兜底'}.get(
                st.get('pool_source'), st.get('pool_source') or '未知')
            when = st.get('updated_at') or '未探测'
            return f"；节点池 {st.get('pool_size')} 台（{src}，更新于 {when}）"
        except Exception:
            return ''

    # ── 期货 K线数据源（独立于 A股 K线源：'sina' | 'eastmoney'，默认 eastmoney）──

    def get_futures_data_sources(self):
        """返回期货 K线数据源配置供 UI 渲染。"""
        try:
            d = self._read_json(self._kline_source_path()) or {}
            source = d.get('futures_kline_source')
            if source not in ('tdx', 'sina', 'eastmoney'):
                source = 'tdx'  # 一键联动后默认第一手
            return {
                "ok": True,
                "source": source,
                "options": self._FUTURES_KLINE_SOURCE_OPTIONS,
            }
        except Exception as e:
            logger.warning("读取期货数据源配置失败: %s", e)
            return {"ok": False, "error": str(e),
                    "source": "tdx", "options": self._FUTURES_KLINE_SOURCE_OPTIONS}

    def save_futures_data_sources(self, source):
        """保存期货 K线数据源（'tdx' | 'sina' | 'eastmoney'），写 data_sources.json 并热重载。"""
        try:
            source = (source or 'tdx').strip().lower()
            if source not in ('tdx', 'sina', 'eastmoney'):
                return {"ok": False, "error": "无效的期货数据源：" + str(source)}
            path = self._kline_source_path()
            d = self._read_json(path) or {}
            d.setdefault("version", 3)
            d["futures_kline_source"] = source
            self._write_json(path, d)
            # 热重载：清空期货数据源缓存
            from engine.futures_data import reset_futures_kline_source
            reset_futures_kline_source()
            return {"ok": True}
        except Exception as e:
            logger.warning("保存期货数据源配置失败: %s", e)
            return {"ok": False, "error": str(e)}

    # ── 用法模式（初级/高级 · 全局开关，解耦仓位执行与因子方案）──────
    #   初级：因子休眠、全市场一套统一加减仓规则（散户低门槛入口）；
    #   高级：因子分级路由、现有全功能照旧。模式是唯一权威裁判，所有方案服从之。

    def _is_basic_mode(self):
        """初级用法（因子休眠）判断：前端可返回 basic 时，各功能按门控清单分流。"""
        return self.get_usage_mode().get('usage_mode') == 'basic'

    def _usage_path(self):
        return os.path.join(self._notif_config_dir(), "usage.json")

    def get_usage_mode(self):
        """返回当前用法模式：'basic'(初级/低门槛仓位执行) | 'advanced'(高级/因子精细化)。
        默认 basic——契合「散户低门槛、打开即用」定位，因子/高级为可选进阶；
        初/高级不影响已保存的因子与仓位方案数据。"""
        try:
            d = self._read_json(self._usage_path()) or {}
            mode = d.get("usage_mode")
            if mode not in ("basic", "advanced"):
                mode = "basic"
            return {"ok": True, "usage_mode": mode}
        except Exception as e:
            logger.warning("读取用法模式失败: %s", e)
            return {"ok": False, "usage_mode": "basic", "error": str(e)}

    def save_usage_mode(self, mode):
        """保存用法模式（'basic' | 'advanced'），写 usage.json。"""
        try:
            mode = (mode or "advanced").strip().lower()
            if mode not in ("basic", "advanced"):
                return {"ok": False, "error": "无效的用法模式：" + str(mode)}
            d = self._read_json(self._usage_path()) or {}
            d["usage_mode"] = mode
            self._write_json(self._usage_path(), d)
            return {"ok": True, "usage_mode": mode}
        except Exception as e:
            logger.warning("保存用法模式失败: %s", e)
            return {"ok": False, "error": str(e)}


    def test_futures_data_source(self, source=None, period=None):
        """实测期货 K线数据源连通性（用 rb0 主力连续作为探针）。

        source: 'sina'|'eastmoney'——优先用调用方传入（UI 选中），否则读磁盘持久化值。
        period: 仅记录展示用（'日K' 或分钟），不影响主路径选择。
        """
        try:
            from engine.futures_data import (fetch_futures_daily, fetch_futures_minute)
            d = self._read_json(self._kline_source_path()) or {}
            persisted = d.get('futures_kline_source')
            source = (source or persisted or 'tdx').strip().lower()
            if source not in ('tdx', 'sina', 'eastmoney'):
                source = 'tdx'
            persisted = persisted if persisted in ('tdx', 'sina', 'eastmoney') else 'tdx'
            _fut_names = {'tdx': '通达信（ExHq）', 'eastmoney': '东财（HTTPS）',
                          'sina': '新浪（HTTP）'}
            name = _fut_names.get(source, source)
            # 探针：rb0 主力连续（secid=113.rb0）
            df = fetch_futures_daily('rb', '113.rb0', days=10)
            if df is None or len(df) == 0:
                return {"ok": False, "message": f"{name} 期货K线 获取失败"}
            last = df.iloc[-1]
            extra = ('' if source == persisted else
                     f'（持久化值仍为{_fut_names.get(persisted, persisted)}，需点保存才能生效）')
            return {"ok": True,
                    "message": f"{name} 连通正常：日K {len(df)}根，最新 {last['trade_time'].date()} 收盘 {last['close']}{extra}"}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def _journal(self):
        """纪律账本单例（写入用户可写 config 目录）。"""
        try:
            return get_journal(config_dir=self._notif_config_dir())
        except Exception as e:
            logger.warning("纪律账本初始化失败: %s", e)
            return None

    def _emotion_journal(self):
        """情绪化操作记录单例（独立存储，与纪律账本并列）。"""
        try:
            return get_emotion_journal(config_dir=self._notif_config_dir())
        except Exception as e:
            logger.warning("情绪化操作记录初始化失败: %s", e)
            return None

    # ── 情绪化操作记录（复盘独立小节：记录/差值/刷新）──────────────
    def add_emotion_record(self, code, name='', market='stock', action='buy',
                           tag='其他', note='', op_price=0.0, latest_price=None):
        """登记一笔情绪化操作（锁操作价，现价由刷新时回填）。"""
        journal = self._emotion_journal()
        if not journal:
            return {'error': '情绪账本不可用'}
        return journal.add_record(code=code, name=name, market=market, action=action,
                                  tag=tag, note=note, op_price=op_price,
                                  latest_price=latest_price)

    def list_emotion_records(self, limit=200):
        journal = self._emotion_journal()
        if not journal:
            return {'error': '情绪账本不可用'}
        return {'ok': True, 'records': journal.list_records(limit),
                'summary': journal.summary()}

    def delete_emotion_record(self, record_id):
        journal = self._emotion_journal()
        if not journal:
            return {'error': '情绪账本不可用'}
        return journal.delete_record(record_id)

    def refresh_emotion_prices(self):
        """刷新账本里所有情绪化操作记录的现价（盘中实时 / 盘后收盘价）。

        只更新 latest_price（作为「操作价 → 现价」情绪化差对比的现价），
        绝不改动 op_price（记录时锁定的操作价）。按 market 分流：stock 走
        DataAPI 实时行情，futures 走期货最新价。单只失败跳过不阻断。
        """
        journal = self._emotion_journal()
        if not journal:
            return {'error': '情绪账本不可用'}
        try:
            recs = journal.list_records(100000)
        except Exception as e:
            return {'error': f'读取情绪记录失败: {e}'}
        stock_codes, fut_codes = {}, {}
        for r in recs:
            code = r.get('code')
            if not code or r.get('op_price', 0) <= 0:
                continue
            if r.get('market') == 'futures':
                fut_codes[code] = 1
            else:
                stock_codes[code] = 1
        updated, failed = 0, []
        from engine.data_layer import DataAPI
        for code in stock_codes:
            try:
                fields, _src = DataAPI.get_realtime_quote(code)
                price = float(fields[3]) if (fields and len(fields) > 3 and fields[3] not in ('', '-')) else None
                if price and price > 0:
                    journal.update_price_by_code(code, price)
                    updated += 1
                else:
                    failed.append(code)
            except Exception:
                failed.append(code)
        from engine.futures_pool import parse_contract_code, make_specific_contracts
        from engine.futures_data import fetch_futures_realtime
        for sym in fut_codes:
            try:
                c, month = parse_contract_code(sym)
                if not c:
                    failed.append(sym)
                    continue
                if month:
                    spec = make_specific_contracts([c], month)[0]
                else:
                    spec = c
                rt = fetch_futures_realtime(spec.symbol, spec.secid) if spec and spec.secid else None
                price = float(rt.get('last', 0)) if rt else 0
                if price and price > 0:
                    journal.update_price_by_code(sym, price)
                    updated += 1
                else:
                    failed.append(sym)
            except Exception:
                failed.append(sym)
        return {'ok': True, 'updated': updated,
                'failed_count': len(failed), 'failed': failed[:10]}

    # ── 执行与复盘闭环：信号落日志 + 对外 API ─────────────────────

    def _maybe_log_signal(self, report_data, period='日K', scheme=None):
        """从分析报告提取可执行的信号并落入纪律账本，同时刷新同一股的现价对照。

        report_data: _build_report_data(ctx) 的返回结构。
        return: 冷却状态 dict（供前端软提醒红条）。
        """
        journal = self._journal()
        if not journal or not report_data:
            return {}
        try:
            code = report_data.get('code') or ''
            latest = float((report_data.get('keyLevels') or {}).get('current') or 0)
            if code and latest:
                journal.update_price_by_code(code, latest)  # 刷新该股已有信号的现价对照

            has_pos = bool(report_data.get('has_position'))
            action = report_data.get('entry_action') or ''
            pos_concl = report_data.get('positionConclusion') or ''

            sig_type = None
            label = ''
            ratio = None
            if not has_pos and action in ('关注建仓', '博反弹', '恐慌反转', '顶部回落'):
                sig_type, label = 'buy', action
                try:
                    ratio = float((report_data.get('positionAllocation') or '0%').rstrip('%')) / 100
                except Exception:
                    ratio = None
            elif pos_concl.startswith('🔴 清仓'):
                sig_type, label = 'clear', '清仓'
            elif pos_concl.startswith('🔴 减仓'):
                sig_type, label = 'reduce', '减仓'
            elif pos_concl.startswith('🟢 触发加仓'):
                sig_type, label = 'add', '加仓'

            if sig_type:
                # 比例：建仓用 positionAllocation；加仓/减仓从结论文本提取首个百分比；清仓固定全仓
                if sig_type == 'clear':
                    ratio = 1.0
                elif ratio is None and pos_concl:
                    m = re.search(r'(\d+(?:\.\d+)?)\s*%', pos_concl)
                    if m:
                        try:
                            ratio = float(m.group(1)) / 100
                        except Exception:
                            ratio = None
                journal.append_signal(
                    code=code,
                    name=report_data.get('stock', ''),
                    market=('futures' if report_data.get('viewDirection') in ('short',) and self._market_type == 'futures' else self._market_type),
                    direction=report_data.get('viewDirection', 'long'),
                    period=period or '日K',
                    scheme=scheme or '',
                    signal_type=sig_type,
                    signal_label=label,
                    price=latest,
                    factor_score=report_data.get('factorScore'),
                    position_ratio=ratio,
                )
            return journal.cooldown_status()
        except Exception as e:
            logger.warning("落信号日志失败(忽略): %s", e)
            return {}

    def get_discipline_ledger(self, market='all'):
        """情绪偏差账本 + 冷却状态（供复盘弹窗加载）。market: all/stock/futures 分流汇总。"""
        journal = self._journal()
        if not journal:
            return {'error': '纪律账本不可用'}
        return {'ok': True, **journal.ledger_summary(market),
                'config': journal.get_cooldown_config()}

    def get_ledger_attribution(self, market='all'):
        """复盘归因（F-1106）：按信号类型聚合「你最不执行哪类信号」。

        纯读现有信号（signal_type + execution + 现价），不新增采集；
        样本不足的类型不下结论（min_samples），避免把噪音当洞察。
        """
        journal = self._journal()
        if not journal:
            return {'error': '纪律账本不可用'}
        return {'ok': True, **journal.attribution(market)}

    def get_ledger_pattern(self, market='all'):
        """模式识别（F-1107）：跨维度识别「舒适区」。

        纯读现有信号；样本不足时返回 ready=False + headline 提示还差多少条。
        """
        journal = self._journal()
        if not journal:
            return {'error': '纪律账本不可用'}
        return {'ok': True, **journal.pattern(market)}

    def get_ledger_linkage(self, market='all'):
        """纪律联动（F-1108）：把归因结论转成执行干预（仅提示层，不动交易逻辑）。

        盯防对象 = 归因里最不执行的信号类型；未达样本门槛时给「还差多少条」提示。
        """
        journal = self._journal()
        if not journal:
            return {'error': '纪律账本不可用'}
        return {'ok': True, **journal.linkage(market)}

    def list_signals(self, limit=100, market=None):
        """信号日志列表（含执行 vs 现价 对照）。market: all/stock/futures 过滤。"""
        journal = self._journal()
        if not journal:
            return {'error': '纪律账本不可用'}
        return {'ok': True, 'signals': journal.list_signals(limit, market)}

    def set_signal_execution(self, signal_id, executed, exec_price=None, exec_ratio=None):
        """用户回填：勾选是否执行 + 执行成交价 + 实际执行比例%(相对建议比例)。"""
        journal = self._journal()
        if not journal:
            return {'error': '纪律账本不可用'}
        return journal.set_execution(signal_id, executed, exec_price, exec_ratio)

    def set_signal_settled(self, signal_id, pnl_pct, note=''):
        """(已废弃) 原资金盈亏结算接口，保留占位避免历史调用崩溃；情绪复盘改用执行对照。"""
        return {'error': '已改用情绪复盘口径，无需登记了结盈亏'}

    def delete_signal(self, signal_id):
        journal = self._journal()
        if not journal:
            return {'error': '纪律账本不可用'}
        r = journal.delete_signal(signal_id)
        r['cooldown'] = journal.cooldown_status()
        return r

    def set_cooldown_config(self, threshold_pct=None, duration_days=None):
        journal = self._journal()
        if not journal:
            return {'error': '纪律账本不可用'}
        return journal.set_cooldown_config(threshold_pct, duration_days)

    def get_discipline_status(self):
        """冷却软提醒状态（顶部红条轮询用，轻量）。"""
        journal = self._journal()
        if not journal:
            return {'active': False}
        return journal.cooldown_status()

    def refresh_signal_prices(self):
        """手动刷新账本里所有信号的现价（盘中实时 / 盘后收盘价）。

        只更新 latest_price（作为「执行价/触发价 → 现价」对比的现价），
        绝不改动 trigger_price（信号触发时锁定）。按信号 market 分流：
        stock 走 DataAPI 实时行情，futures 走期货最新价。单只失败跳过不阻断。
        """
        journal = self._journal()
        if not journal:
            return {'error': '纪律账本不可用'}
        try:
            sigs = journal.list_signals(100000)
        except Exception as e:
            return {'error': f'读取信号失败: {e}'}
        stock_codes, fut_codes = {}, {}
        for s in sigs:
            code = s.get('code')
            if not code:
                continue
            if s.get('market') == 'futures':
                fut_codes[code] = 1
            else:
                stock_codes[code] = 1
        updated, failed = 0, []
        from engine.data_layer import DataAPI
        from engine.futures_pool import parse_contract_code, make_specific_contracts
        from engine.futures_data import fetch_futures_realtime
        for code in stock_codes:
            try:
                fields, _src = DataAPI.get_realtime_quote(code)
                price = float(fields[3]) if (fields and len(fields) > 3 and fields[3] not in ('', '-')) else None
                if price and price > 0:
                    journal.update_price_by_code(code, price)
                    updated += 1
                else:
                    failed.append(code)
            except Exception:
                failed.append(code)
        for sym in fut_codes:
            try:
                # 信号 code 形态可能是主力连续 ('rb'/'TA') 或具体合约 ('rb2610'/'TA2510')，
                # 统一走 parse_contract_code + make_specific_contracts 构造 secid。
                # 之前直接 get_contract(sym) 只查品种级，遇到具体合约会返回 None → AttributeError
                # 导致 rb2610/jd2609 等具体合约被记为失败却没拿到价。
                c, month = parse_contract_code(sym)
                if not c:
                    failed.append(sym); continue
                if month:
                    spec = make_specific_contracts([c], month)[0]
                else:
                    spec = c
                rt = fetch_futures_realtime(spec.symbol, spec.secid) if spec and spec.secid else None
                price = float(rt.get('last', 0)) if rt else 0
                if price and price > 0:
                    journal.update_price_by_code(sym, price)
                    updated += 1
                else:
                    failed.append(sym)
            except Exception:
                failed.append(sym)
        return {'ok': True, 'updated': updated,
                'failed_count': len(failed), 'failed': failed[:10]}

    # 「读 JSON」的指纹缓存：(路径, mtime_ns, size) → 已解析对象。
    #
    # ⚠️ 为什么必须有（2026-09-19 cProfile 实测）：
    #   `_scan_rank(r)` 每行都会调 `self._is_basic_mode()` → `get_usage_mode()`
    #   → `_read_json(usage.json)`。一次「应用筛选」要对 5121 行算排名，
    #   于是对同一个 32 字节的 usage.json **读盘 25215 次**：
    #     nt._path_exists 3.22s + open 2.46s + json.load 1.45s ≈ **7.9 秒**
    #   —— 这才是「点应用筛选要等 8 秒」的真正来源（与分页、与 limit 无关：
    #   limit=200 也要 7.66s，所以当年为「避免卡顿」引入的分页从未解决它）。
    # 以 mtime+size 为键：任何外部写入都会改变指纹 → 缓存自动失效，语义零变化。
    _json_cache = {}

    def _read_json(self, path):
        try:
            st = os.stat(path)
            key = (path, st.st_mtime_ns, st.st_size)
            cached = self._json_cache.get(key)
            if cached is not None:
                # 深拷贝：调用方拿到的仍是「全新对象」，与优化前语义完全一致，
                # 避免某个调用方就地改写对象污染后续读者。
                return copy.deepcopy(cached)
            with open(path, "r", encoding="utf-8") as f:
                obj = json.load(f)
            if len(self._json_cache) > 64:      # 长期运行不无界增长
                self._json_cache.clear()
            self._json_cache[key] = obj
            return copy.deepcopy(obj)
        except FileNotFoundError:
            return None
        except Exception as e:
            logger.warning("读取 JSON 失败 %s: %s", path, e)
        return None

    def _write_json(self, path, obj):
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)

    def get_diagnosis_progress(self, task_id):
        """返回诊断进度（前端每 1.5s 轮询）。"""
        task = self._diag_tasks.get(task_id)
        if not task:
            return {'error': '无效的 task_id'}
        return {
            'done': not task['is_running'],
            'current': task['current'],
            'total': task['total'],
            'results': task['results'],
            'summary': task['summary'],
            'env_label': task['env_label'],
            'max_pos': task['max_pos'],
            'crash_status': task['crash_status'],
            'breadth_fetched': task['breadth_fetched'],
            'report_text': task['report_text'],
            'is_cancelled': task.get('is_cancelled', False),
            'error': task.get('error'),
            'used_scheme_name': task.get('used_scheme_name'),
            'used_scheme_period': task.get('used_scheme_period'),
        }

    def cancel_diagnosis(self, task_id):
        """中途取消诊断。"""
        task = self._diag_tasks.get(task_id)
        if task:
            task['is_cancelled'] = True
            task['is_running'] = False
            return {'success': True}
        return {'success': False, 'error': '无效的 task_id'}

    # ═══════════════════════════════════════════════════════════
    def _scan_rank(self, r):
        """扫描结果排序/聚合依据键（0-100 标尺）。

        依据架构基准 §4.3：初级（因子休眠）用「技术条件/技术强度」驱动排序，
        高级仍按因子预期分排序。初级下各档位因子分无意义，取 tech_strength×100，
        使其与 sector 级别阈值（≥30 强/≥20 中/≥10 弱）及前端 0-100 分数输入口径对齐。
        """
        if self._is_basic_mode():
            return (r.get('tech_strength') or 0) * 100
        return r.get('final_score') or 0

    @staticmethod
    def _scan_cmp(op, a, b):
        """扫描筛选比较算子：ge/gt/eq/le/lt（其余/None 视为不比较→返回 True）。

        a/b 均为数值；调用方负责把缺值行先排除（缺值不可比较，不应放行）。
        """
        if op == 'gt':
            return a > b
        if op == 'eq':
            return a == b
        if op == 'le':
            return a <= b
        if op == 'lt':
            return a < b
        if op == 'ge':
            return a >= b
        return True

    # 全市场扫描 + 板块强度
    # ═══════════════════════════════════════════════════════════
    def start_market_scan(self, min_score=0, topn=100, rating='全部', k_type='日K', scheme_name=None, max_score=None,
                          rating_op='ge', chg_op=None, chg_val=None):
        """启动后台 daemon 线程扫描，返回 task_id。

        根据市场类型分流：股票走 A 股列表 + _scan_worker，期货走品种池 + _futures_scan_worker。
        k_type: 分析周期（期货支持日K/分钟线；分钟线因子窗口按周期缩放）。
        scheme_name: 非空时强制使用指定方案（诊断/扫描下拉按方案精确选择，避免同周期多方案静默错配）。
        max_score: 因子预期分区间上限（None=不限，配合 min_score 做区间筛选）。
        rating_op: 星级比较方式 ge/gt/eq/le/lt（rating='全部' 时不生效）。
        chg_op/chg_val: 涨幅比较方式与阈值（如 chg_op='lt', chg_val=9.8 排除涨停股；chg_val=None 关闭）。
        """
        try:
            allowed, reason = can_use('query')
            if not allowed:
                return {'error': f"授权受限: {reason}"}

            # 期货模式：用期货品种池
            if self._market_type == 'futures':
                return self._start_futures_scan(min_score, topn, rating, k_type, scheme_name, max_score,
                                                rating_op, chg_op, chg_val)

            # A股全市场扫描：禁止分钟级（用户决策：5000+ 票分钟抓取会被限流），
            # 日K/周K 允许（用户明确"可以选择周K和日K方案进行扫描"——之前锁日K已放宽）
            if k_type in ('1分钟', '5分钟', '15分钟', '30分钟', '60分钟'):
                return {'error': f'全市场扫描仅支持「日K/周K」周期；{k_type} 分钟级分析请改用「自选诊断」或「监控设置」。'}

            stock_list = list(state.code_to_name.keys())
            if not stock_list:
                return {'error': '股票列表为空，请稍等后台加载完成'}

            # 配置完整性检查（按市场加载对应方案；门禁分流；统一 REQUIRED_STRATEGY SSOT）
            # A股扫描严格匹配所选周期方案：无对应周期方案→报错（用户已有日K 方案「策略四_轮动均值回归」）
            # 尊重当前激活方案：其市场身份为 A股时优先使用（缺 _meta 已自动补全为 stock）
            try:
                if not scheme_name:
                    cur = quant_config.get_current_scheme_name()
                    if cur:
                        cur_meta = quant_config.get_scheme_meta(cur) or {}
                        if cur_meta.get('market') == 'stock':
                            scheme_name = cur
                cfg = quant_config.load_market_scheme('stock', 'long', k_type or '日K', force_reload=True, scheme_name=scheme_name, strict_period=True)
                if cfg is None:
                    return {'error': f'未配置 A股·{k_type or "日K"} 方案：全市场扫描使用该周期方案，请到「模型配置」创建。'}
                _cfg = quant_config._safe_cfg()
                # 初级用法（因子休眠）开放全市场扫描：无因子时不校验 active_factors，
                # 排序/筛选改用 tech_strength（见 §4.3：初级用技术条件驱动排序）。
                if not self._is_basic_mode() and not (_cfg.get('active_factors') or []):
                    return {'error': '配置不完整：无启用因子，请到「模型配置」勾选因子并保存。'}
                # 初级下无需因子即可扫描（技术门槛/否决项来自方案），任意模式下 require_config 兜底必要参数
                quant_config.require_config(_cfg, quant_config.REQUIRED_STRATEGY)
            except quant_config.ConfigIncompleteError as e:
                return {'error': f"配置不完整: {'、'.join(e.missing)}，请到「模型配置」补全。"}

            consume_use()  # 预扣额度（扫描失败/取消时无法回退，故在启动前预扣）

            task_id = str(uuid.uuid4())
            task = {
                'task_id': task_id,
                'is_running': True,
                'is_cancelled': False,
                'is_paused': False,
                'pause_event': threading.Event(),
                'total': len(stock_list),
                'progress': 0,
                'success_count': 0,
                'results': [],
                'sectors': [],
                'status': 'running',
                'error': None,
                'min_score': float(min_score or 0),
                'max_score': None if max_score is None else float(max_score),
                'topn': int(topn or 0),
                'rating': rating or '全部',
                'rating_op': rating_op or 'ge',
                'chg_op': chg_op or None,
                'chg_val': None if chg_val in (None, '') else float(chg_val),
                'start_time': datetime.now().isoformat(),
                'rate': 0.0,
                'eta': 0.0,
                'elapsed': 0.0,
                'used_scheme_name': scheme_name,  # 本次扫描使用的方案名
                'used_scheme_period': k_type,
            }
            task['pause_event'].set()  # set=运行中, clear=暂停
            self._scan_tasks[task_id] = task

            def _scan():
                try:
                    from engine.market_scan_core import _quick_score_core, _scan_worker, _reset_worker_state
                    from engine.signal_rating import SignalRating
                    _reset_worker_state()  # 确保重新加载最新配置
                    # 重置缓存命中计数器（本次扫描的统计基线）
                    try:
                        from engine.data_layer import KLineFetcher
                        KLineFetcher._cache_hit_log = 0
                    except Exception:
                        pass
                    # 盘中：预热批量实时快照池（全市场 ~65 次请求），供 get_kline 出口的
                    # _patch_intraday_bar 修补末根 K 线。逐只实时在 WAF 整源冷却期会整批
                    # 失败，导致这部分股票末根停在昨日、触发价/扫描价错成昨收（差一天）。
                    try:
                        _nw = datetime.now()
                        if _nw.weekday() < 5 and (9 * 60 + 15) <= (_nw.hour * 60 + _nw.minute) < 15 * 60:
                            from engine.data_layer import RealtimeBarPool
                            RealtimeBarPool.build_live()
                    except Exception:
                        pass
                    up_ratio = 0.5
                    try:
                        auto_up, auto_down, _ = DataAPI.get_market_breadth()
                        if auto_up is not None and auto_down is not None and (auto_up + auto_down) > 0:
                            up_ratio = auto_up / (auto_up + auto_down)
                    except Exception:
                        pass

                    # 加载板块映射（优先用户配置，回退种子文件）
                    code_to_sector = {}
                    try:
                        _sector_file = SECTOR_MAP_FILE if os.path.exists(SECTOR_MAP_FILE) else os.path.join(
                            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'engine', 'config', 'sector_map.json')
                        if os.path.exists(_sector_file):
                            with open(_sector_file, 'r', encoding='utf-8') as f:
                                sm = json.load(f)
                            for sector, codes in (sm or {}).items():
                                for c in codes:
                                    code_to_sector[str(c).zfill(6)] = sector
                    except Exception:
                        pass

                    total = len(stock_list)
                    scan_start = datetime.now()
                    # 全量扫描：用 worker 分片，每片 20 只
                    chunk = 20
                    chunks = [stock_list[i:i + chunk] for i in range(0, total, chunk)]
                    # 动态线程数（machine_probe.auto_n_workers）
                    try:
                        from engine.machine_probe import auto_n_workers
                        n_workers = auto_n_workers()
                    except Exception:
                        n_workers = max(1, min((os.cpu_count() or 2) - 1, 8))
                    n_threads = max(1, min(n_workers, 16))

                    from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
                    # 小列表串行回退（total<50 走串行，避免线程开销）
                    if n_threads > 1 and total >= 50:
                        ex = ThreadPoolExecutor(max_workers=n_threads)
                        try:
                            futures = {ex.submit(_scan_worker, c, up_ratio, code_to_sector, direction='long', period=k_type): len(c) for c in chunks}
                            pending = set(futures.keys())
                            stall = {}              # watchdog：分片连续「无完成」的轮数（30s/轮）
                            done = 0
                            last_sector_update = 0
                            last_sector_time = time.time()
                            last_save_count = 0
                            while pending:
                                t = self._scan_tasks.get(task_id)
                                if not t or t.get('is_cancelled'):
                                    for f in pending:
                                        f.cancel()
                                    break
                                t['pause_event'].wait()  # 暂停时阻塞，恢复后继续
                                if t.get('is_paused'):
                                    t['status'] = 'paused'
                                    continue
                                t['status'] = 'running'
                                # 超时 30s + 卡顿提示
                                finished, pending = wait(pending, timeout=30, return_when=FIRST_COMPLETED)
                                if not finished:
                                    task['status'] = 'running'
                                    # 速率/ETA 仍随流逝时间刷新，避免 UI 停在陈旧的「剩余0秒」
                                    _el = (datetime.now() - scan_start).total_seconds()
                                    if _el > 0 and done > 0:
                                        task['elapsed'] = round(_el, 1)
                                        task['rate'] = round(done / _el, 1)
                                        task['eta'] = round((total - done) / (done / _el), 1)
                                    # watchdog：连续 8 轮（≈4 分钟）仍无进展 → 放弃该分片，
                                    # 避免个别坏分片（网络请求重试拖长）让整个扫描永久卡在 99%。
                                    for _f in pending:
                                        stall[_f] = stall.get(_f, 0) + 1
                                    _give = [_f for _f in pending if stall[_f] >= 8]
                                    if _give:
                                        for _f in _give:
                                            pending.discard(_f)
                                            task['skipped_count'] = task.get('skipped_count', 0) + futures[_f]
                                        logger.warning("全市场扫描: %d 个分片（共 %d 只）超 4 分钟无进展已放弃",
                                                       len(_give), sum(futures.get(_f, 0) for _f in _give))
                                    continue
                                for fut in finished:
                                    try:
                                        batch = fut.result() or []
                                    except Exception:
                                        batch = []
                                    done += futures.get(fut, 0)
                                    task['results'].extend(batch)
                                    task['success_count'] += len(batch)
                                    task['progress'] = int(done / total * 100)
                                # 速率/ETA
                                elapsed = (datetime.now() - scan_start).total_seconds()
                                if elapsed > 0 and done > 0:
                                    task['elapsed'] = round(elapsed, 1)
                                    task['rate'] = round(done / elapsed, 1)
                                    remaining = total - done
                                    task['eta'] = round(remaining / (done / elapsed), 1) if done > 0 else 0
                                # 每 200 只落盘一次
                                if task['success_count'] - last_save_count >= 200:
                                    last_save_count = task['success_count']
                                    try:
                                        self._save_scan_cache('stock', task['results'],
                                                              task.get('used_scheme_name'),
                                                              task.get('used_scheme_period'),
                                                              task=task)
                                    except Exception:
                                        pass
                                # 实时板块聚合：每 3 秒或每 100 只更新一次，让板块强度随
                                # 扫描即时刷新（不再等全市场扫描完成）。原为每 500 只，5000 只
                                # 规模下要 ~100s 才首次出现，体感像"必须扫完才能看板块"。
                                _sector_now = time.time()
                                if (task['success_count'] - last_sector_update >= 100
                                        or _sector_now - last_sector_time >= 3):
                                    last_sector_update = task['success_count']
                                    last_sector_time = _sector_now
                                    task['sectors'] = self._aggregate_sectors(task['results'])
                        finally:
                            # 收尾不等慢分片：卡住的分片不再阻塞扫描定完成（其 daemon 线程自生自灭）
                            ex.shutdown(wait=False)
                    else:
                        # 串行路径
                        done = 0
                        last_sector_update = 0
                        last_sector_time = time.time()
                        last_save_count = 0
                        for c in chunks:
                            t = self._scan_tasks.get(task_id)
                            if not t or t.get('is_cancelled'):
                                break
                            t['pause_event'].wait()
                            if t.get('is_paused'):
                                t['status'] = 'paused'
                                continue
                            t['status'] = 'running'
                            try:
                                batch = _scan_worker(c, up_ratio, code_to_sector, direction='long', period=k_type) or []
                            except Exception:
                                batch = []
                            done += len(c)
                            task['results'].extend(batch)
                            task['success_count'] += len(batch)
                            task['progress'] = int(done / total * 100)
                            # 速率/ETA
                            elapsed = (datetime.now() - scan_start).total_seconds()
                            if elapsed > 0 and done > 0:
                                task['elapsed'] = round(elapsed, 1)
                                task['rate'] = round(done / elapsed, 1)
                                remaining = total - done
                                task['eta'] = round(remaining / (done / elapsed), 1) if done > 0 else 0
                            # 每 200 只落盘一次
                            if task['success_count'] - last_save_count >= 200:
                                last_save_count = task['success_count']
                                try:
                                    self._save_scan_cache('stock', task['results'],
                                                          task.get('used_scheme_name'),
                                                          task.get('used_scheme_period'),
                                                          task=task)
                                except Exception:
                                    pass
                            # 实时板块聚合（串行路径原无中间聚合，板块强度要等完成才出现）
                            _sector_now = time.time()
                            if (task['success_count'] - last_sector_update >= 100
                                    or _sector_now - last_sector_time >= 3):
                                last_sector_update = task['success_count']
                                last_sector_time = _sector_now
                                task['sectors'] = self._aggregate_sectors(task['results'])

                    # 排序 + 星级（初级=技术强度驱动，高级=因子预期分驱动）
                    task['results'].sort(key=lambda x: self._scan_rank(x), reverse=True)
                    try:
                        SignalRating.assign_tech_stars(
                            task['results'],
                            (quant_config.get_tech_resonance_config() or {}).get('bands'))
                    except Exception:
                        pass

                    # 财务筛选：对前 TopN 候选票逐票补查财务——爆雷硬剔除（每股净资产/净利<0）
                    # + 高级模式可配置阈值（PB/PE/市值/EPS/资产负债率，见 get_scan_finance_filter）。
                    # 只处理 topn 数量级（不查全市场，守扫描性能/限流约束）；单只查询失败
                    # 安全放行，避免误杀整批。初级模式 rules=None → 仅保留爆雷硬剔除（现状不变）。
                    if task.get('topn'):
                        try:
                            from engine.market_scan_core import apply_finance_guard
                            _rules = None if self._is_basic_mode() else quant_config.get_scan_finance_filter()
                            task['results'], task['finance_filtered'] = apply_finance_guard(
                                task['results'], task['topn'], _rules)
                            if task.get('finance_filtered'):
                                logger.info("财务筛选剔除 %s 只（TopN 候选中的爆雷/不满足阈值票）",
                                            task['finance_filtered'])
                        except Exception as _e:
                            logger.warning("财务筛选执行异常，已跳过：%s", _e)

                    # 板块聚合
                    task['sectors'] = self._aggregate_sectors(task['results'])
                    task['status'] = 'completed'
                    task['is_running'] = False
                    # 若因网络超时放弃过个别分片：进度置满（避免 UI 停在 99%），并落日志
                    if task.get('skipped_count'):
                        task['progress'] = 100
                        logger.warning("全市场扫描完成，但 %d 只因分片超时被跳过", task['skipped_count'])
                    # 保存缓存
                    self._save_scan_cache('stock', task['results'],
                                          task.get('used_scheme_name'),
                                          task.get('used_scheme_period'),
                                          task=task)
                    # 缓存命中诊断日志
                    try:
                        from engine.data_layer import KLineFetcher
                        _hit = getattr(KLineFetcher, '_cache_hit_log', 0)
                        _total = len(stock_list)
                        _pct = (_hit / _total * 100) if _total > 0 else 0
                        logger.warning(f"扫描完成: 缓存命中 {_hit}/{_total} ({_pct:.0f}%)")
                    except Exception:
                        pass
                except Exception as e:
                    task['error'] = str(e)
                    task['status'] = 'error'
                    task['is_running'] = False

            threading.Thread(target=_scan, daemon=True).start()
            return {'task_id': task_id, 'total': len(stock_list)}
        except Exception as e:
            logger.exception("start_market_scan 失败")
            return {'error': f"启动扫描失败: {e}"}

    @staticmethod
    def _scan_task_direction(task):
        """扫描任务方向：期货多/空方案隔离，股票恒为 'long'。"""
        return (task or {}).get('scan_direction') or 'long'

    def _current_scan_direction(self, market_type=None):
        """不持有 task 的调用点（板块刷新/钻取）用：取该市场下已有结果任务的方向。

        期货多单/空单是两份隔离方案，方向错会让信号翻转失效、筛选语义反向。
        找不到任务时回退 'long'（股票口径）。
        """
        mt = market_type or self._market_type
        for t in self._scan_tasks.values():
            if t.get('market_type') == mt and t.get('results'):
                return self._scan_task_direction(t)
        return 'long'

    def _apply_scan_tech_filter(self, results, direction='long'):
        """技术指标二级过滤（结果态、即时、不清扫）：仅高级模式生效。

        - 初级已按「触发建仓等级」分组展示（分组即筛选），故**跳过**本过滤；
        - 股票与期货都适用（2026-10-05 起期货扫描结果也带 tech_snapshot）；
        - direction 必须传扫描任务的方向：期货多/空方案隔离，空单方案要传 'short'，
          否则 _evaluate_tech_signal 不做空头信号翻转 → 筛选语义与实际入场方向相反；
        - 用与建仓一致的技术条件判定 UnifiedEntryLogic._evaluate_tech_conditions，
          保证筛选取值与定档同为权威判定（消费 tech_snapshot 里 tech/market/adx 快照）。
        - 旧缓存无快照：保留但不套过滤并打 scan_filter_skipped，不静默丢票。
        """
        if self._is_basic_mode() or not results:
            return results, False
        try:
            from engine import quant_config
            scan_filter = quant_config.get_scan_tech_filter()
        except Exception:
            scan_filter = None
        if not (isinstance(scan_filter, list) and scan_filter):
            return results, False
        from engine.unified_entry_logic import UnifiedEntryLogic
        filtered = []
        any_skipped = False
        for r in results:
            snap = r.get('tech_snapshot') or {}
            snap_tech = snap.get('tech') or {}
            if not snap_tech:
                r['scan_filter_skipped'] = True
                any_skipped = True
                filtered.append(r)
                continue
            try:
                hit = UnifiedEntryLogic._evaluate_tech_conditions(
                    scan_filter, snap_tech, snap.get('market') or {},
                    r.get('price') or 0, snap.get('adx') or {}, direction=direction)
            except Exception:
                hit = True  # 判定异常不误杀
            if hit:
                filtered.append(r)
        return filtered, any_skipped

    def _ensure_scan_tech_pass(self, task, results, direction=None):
        """技术二级过滤判定结果打标记（增量缓存），供 get_scan_progress 秒开筛选。

        判定结果只依赖每条结果的 tech_snapshot、scan_filter 配置与扫描方向：
          - 标记写在结果 dict 上（_tech_pass / _tech_skip），随任务结果存活；
          - 配置签名（repr(scan_filter)）比对：用户改配置 → 清标记重判，不误用旧结果；
          - 扫描中 results 持续增长：只对无标记的新结果增量判定，轮询不重复全量判定。

        direction 缺省取 task['scan_direction']（期货多/空方案隔离；股票恒 'long'）。
        空单方案必须按 'short' 判定，否则信号不做空头翻转、筛选语义与入场方向相反。

        返回 (results, any_skipped)：results 原样返回（含标记），
        由调用方在裁剪后用标记过滤；any_skipped=旧缓存缺技术快照（二级过滤未完全生效）。
        """
        if direction is None:
            direction = (task or {}).get('scan_direction') or 'long'
        if self._is_basic_mode() or not results:
            return results, False
        try:
            from engine import quant_config
            scan_filter = quant_config.get_scan_tech_filter()
        except Exception:
            scan_filter = None
        if not (isinstance(scan_filter, list) and scan_filter):
            # 配置被清空/禁用：必须把上一次筛选留下的 _tech_pass/_tech_skip 标记一并清掉。
            # 否则 get_scan_progress 仍按旧标记裁剪（filtered = [_tech_pass]），
            # 表现为「条件清空后点应用筛选，结果不恢复」（2026-09-19 探针实测复现：
            # 站上MA5 筛出 928 只 → 清空条件后仍是 928 只，而非全量 2163 只）。
            if task.get('_tech_filter_sig'):
                for r in results:
                    r.pop('_tech_pass', None)
                    r.pop('_tech_skip', None)
                task['_tech_filter_sig'] = None
            return results, False
        sig = repr(scan_filter)
        if task.get('_tech_filter_sig') != sig:
            for r in results:
                r.pop('_tech_pass', None)
                r.pop('_tech_skip', None)
            task['_tech_filter_sig'] = sig
        pending = [r for r in results if '_tech_pass' not in r]
        if pending:
            from engine.unified_entry_logic import UnifiedEntryLogic
            for r in pending:
                snap = r.get('tech_snapshot') or {}
                snap_tech = snap.get('tech') or {}
                if not snap_tech:
                    # 旧缓存无快照：保留不误杀，并标记 partial（提示二级过滤未完全生效）
                    r['_tech_pass'] = True
                    r['_tech_skip'] = True
                    continue
                try:
                    hit = UnifiedEntryLogic._evaluate_tech_conditions(
                        scan_filter, snap_tech, snap.get('market') or {},
                        r.get('price') or 0, snap.get('adx') or {}, direction=direction)
                except Exception:
                    hit = True  # 判定异常不误杀
                r['_tech_pass'] = hit
        return results, any(r.get('_tech_skip') for r in results)

    def _aggregate_sectors(self, results):
        """按板块聚合扫描结果（排序依据与 _scan_rank 一致：初/高级各自口径）。"""
        stats = {}
        for r in results:
            sector = r.get('sector') or '未分类'
            if sector == '未分类':
                continue
            if sector not in stats:
                stats[sector] = {'scores': [], 'stocks': []}
            stats[sector]['scores'].append(self._scan_rank(r))
            stats[sector]['stocks'].append(r)
        sectors = []
        for sector, data in stats.items():
            scores = data['scores']
            if len(scores) < 3:
                continue
            avg = sum(scores) / len(scores)
            sorted_stocks = sorted(data['stocks'], key=lambda x: self._scan_rank(x), reverse=True)
            level = '强' if avg >= 30 else ('中' if avg >= 20 else ('弱' if avg >= 10 else '极弱'))
            # 触发建仓等级计数：初级板块强度按此分布展示（高级不显示，仅传输）
            tier_counts = {'strong': 0, 'standard': 0, 'test': 0, 'pending': 0,
                           'weak_rebound': 0, 'panic_rebound': 0, 'weak': 0}
            for _r in data['stocks']:
                _t = _r.get('entry_tier')
                tier_counts[_t if _t in tier_counts else 'weak'] += 1
            sectors.append({
                'sector': sector,
                'avg_score': round(avg, 1),
                'count': len(scores),
                'strong_count': sum(1 for s in scores if s >= 30),
                'level': level,
                'tier_counts': tier_counts,
                'max_stock': sorted_stocks[0],
                'min_stock': sorted_stocks[-1],
            })
        sectors.sort(key=lambda x: x['avg_score'], reverse=True)
        return sectors

    def _start_futures_scan(self, min_score=0, topn=100, rating='全部', k_type='日K', scheme_name=None, max_score=None,
                            rating_op='ge', chg_op=None, chg_val=None):
        """启动期货全市场扫描。k_type: 分析周期（日K/分钟线）。scheme_name: 指定方案。max_score: 分数区间上限。"""
        from engine.futures_pool import all_contracts

        try:
            quant_config.load_market_scheme('futures', 'long', k_type, force_reload=True, scheme_name=scheme_name)
            _scan_direction = 'long'
            _cfg = quant_config._safe_cfg()
            if scheme_name:
                _scan_meta = quant_config.get_scheme_meta(scheme_name) or {}
                _scan_direction = _scan_meta.get('direction', 'long')
            # 初级用法（因子休眠）：全市场扫描无因子无排序依据，明确禁用（而非无因子误导）
            if self._is_basic_mode():
                return {'error': '初级用法暂不开放「全市场扫描」，可切到「设置 → 用法设置 → 高级用法」后使用。'}
            if not (_cfg.get('active_factors') or []):
                return {'error': '配置不完整：无启用因子，请到「模型配置」勾选因子并保存。'}
            quant_config.require_config(_cfg, quant_config.REQUIRED_STRATEGY)
        except quant_config.ConfigIncompleteError as e:
            return {'error': f"配置不完整: {'、'.join(e.missing)}，请到「模型配置」补全。"}

        consume_use()

        contracts = all_contracts()
        contract_list = [(c.symbol, c.name, c.secid, c.exchange) for c in contracts]

        task_id = str(uuid.uuid4())
        task = {
            'task_id': task_id,
            'is_running': True,
            'is_cancelled': False,
            'is_paused': False,
            'pause_event': threading.Event(),
            'total': len(contract_list),
            'progress': 0,
            'success_count': 0,
            'results': [],
            'sectors': [],
            'status': 'running',
            'error': None,
            'min_score': float(min_score or 0),
            'max_score': None if max_score is None else float(max_score),
            'topn': int(topn or 0),
            'rating': rating or '全部',
            'rating_op': rating_op or 'ge',
            'chg_op': chg_op or None,
            'chg_val': None if chg_val in (None, '') else float(chg_val),
            'k_type': k_type,
            'start_time': datetime.now().isoformat(),
            'rate': 0.0,
            'eta': 0.0,
            'elapsed': 0.0,
            'market_type': 'futures',
            'scan_direction': _scan_direction,   # 期货多/空方案隔离：二级过滤按同方向判定
            'used_scheme_name': scheme_name,
            'used_scheme_period': k_type,
        }
        task['pause_event'].set()
        self._scan_tasks[task_id] = task

        def _futures_scan():
            try:
                from engine.futures_data import fetch_futures_daily, fetch_futures_minute
                from engine.signal_rating import SignalRating
                from engine.scoring_core import compute_stock_score, _rsi_series_fast
                from engine.indicators import MACDCalculator
                from engine.market_scan_core import build_tech_snapshot

                total = len(contract_list)
                scan_start = datetime.now()
                up_ratio = 0.5  # 期货无市场宽度
                _period_map = {'15分钟': 15, '30分钟': 30, '60分钟': 60, '1分钟': 1, '5分钟': 5}
                _is_minute = k_type in _period_map
                _period = _period_map.get(k_type)
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
                processed = 0

                for sym, name, secid, exchange in contract_list:
                    t = self._scan_tasks.get(task_id)
                    if not t or t.get('is_cancelled'):
                        break
                    t['pause_event'].wait()
                    if t.get('is_paused'):
                        t['status'] = 'paused'
                        continue
                    t['status'] = 'running'
                    processed += 1

                    try:
                        # 周期分支：分钟线走新浪 getFewMinLine，日线走日K。
                        # 分钟因子窗口缩放(clamp 600/320)需足量K线 → 抓 1023（与 A股/单股分析一致），
                        # 否则 relative_strength/ma_slope/bias_value 在 5/15分钟 下恒 0 静默失真。
                        if _is_minute:
                            df = fetch_futures_minute(sym, secid, period=_period, days=1023)
                        else:
                            df = fetch_futures_daily(sym, secid, days=300)
                            # 并入"下一交易日夜盘"bar（与 get_kline/_analyze_futures 同口径），
                            # 让全市场扫描评分同样覆盖最新夜盘行情；下一交易日日K生成后真实合并、不再重复。
                            df = self._merge_futures_night(df, sym, secid)
                        if df is None or df.empty or len(df) < 30:
                            t['progress'] = int(processed / total * 100) if total > 0 else 100
                            continue

                        _cutoff = datetime.combine(datetime.now().date() - timedelta(days=365), datetime.min.time())
                        dff = df[df['trade_time'] >= _cutoff]
                        if len(dff) < 30:
                            dff = df.tail(300)

                        data_list = dff[['close', 'high', 'low', 'volume', 'trade_time', 'open']].to_dict('records')
                        for _d in data_list:
                            _d['date'] = _d.pop('trade_time')

                        closes = [_d['close'] for _d in data_list]
                        volumes = [_d['volume'] for _d in data_list]
                        highs = [_d['high'] for _d in data_list]
                        lows = [_d['low'] for _d in data_list]
                        opens = [_d['open'] for _d in data_list]

                        rsi_hist = _rsi_series_fast(closes, _rsi_period)
                        accel = MACDCalculator.calc_acceleration(closes)

                        # 期货扫描：周期缩放因子窗口 + 中性门控（market_type='futures'）
                        stock_score, final_result, tech, market, adx_result, tech_strength = compute_stock_score(
                            closes, volumes, highs, lows, opens, data_list, up_ratio, rsi_hist, accel,
                            direction=_scan_direction, period=k_type, market_type='futures', rsi_period=_rsi_period)
                        final_score = final_result['final_score']

                        # 板块：优先按品种归类到中文板块（股指/有色/黑色系等），未归类统一归「其他」
                        # （不再兜底为交易所名，避免板块列混入"上海期货交易所"等交易所名称）
                        from engine.futures_pool import symbol_sector
                        _sector = symbol_sector(sym) or '其他'

                        sr = SignalRating.calculate(stock_score, tech, up_ratio, '', final_score=final_score,
                                                    tech_strength=tech_strength, market='futures')

                        # 提取当前方案启用因子的原始值（技术条件第二道筛选）。
                        # 复用评分已算好的 tech/market，不重复联网抓K；与 A股扫描 _quick_score_core 一致。
                        factor_values = {}
                        try:
                            from engine.score_calculator_v2 import ScoreCalculatorV2
                            factor_values = ScoreCalculatorV2.extract_factors(
                                tech, market, closes, highs, lows, volumes, opens, data_list, closes[-1], k_type) or {}
                        except Exception:
                            factor_values = {}

                        # 当日涨跌幅（最新收盘 vs 前一交易日收盘）：与 A股扫描同一口径，供「涨幅」筛选
                        _chg_pct = None
                        try:
                            if len(closes) >= 2 and closes[-2]:
                                _chg_pct = round((float(closes[-1]) / float(closes[-2]) - 1) * 100, 2)
                        except Exception:
                            _chg_pct = None

                        t['results'].append({
                            'code': sym,
                            'name': name,
                            'price': closes[-1],
                            'change_pct': _chg_pct,
                            'stock_score': stock_score,
                            'final_score': final_score,
                            'tech_strength': sr.get('tech_strength'),
                            'final_rating': sr.get('score'),
                            'stars': sr.get('stars', ''),
                            'level': sr.get('level', ''),
                            'sector': _sector,  # 中文板块（品种归类，未归类兜底为交易所中文名）
                            'period': k_type,    # 扫描所用周期（分钟线因子窗口已缩放）
                            'factor_values': factor_values,  # 当前方案启用因子的原始值（技术条件第二道筛选）
                            # 技术指标二级过滤用（与 A股 _quick_score_core 同口径，含自定义指标）；
                            # 空单方案下判定按 task['scan_direction']='short' 做信号翻转
                            'tech_snapshot': build_tech_snapshot(tech, market, adx_result, factor_values),
                        })
                        t['success_count'] += 1
                    except Exception as e:
                        logger.warning("期货扫描 %s(%s) 失败: %s", sym, name, e)

                    t['progress'] = int(processed / total * 100) if total > 0 else 100
                    elapsed = (datetime.now() - scan_start).total_seconds()
                    if elapsed > 0 and t['success_count'] > 0:
                        t['elapsed'] = round(elapsed, 1)
                        t['rate'] = round(t['success_count'] / elapsed, 1)
                        remaining = total - t['success_count']
                        t['eta'] = round(remaining / (t['success_count'] / elapsed), 1) if t['success_count'] > 0 else 0

                # 排序 + 星级
                task['results'].sort(key=lambda x: x.get('final_score', 0), reverse=True)
                try:
                    SignalRating.assign_tech_stars(task['results'],
                        (quant_config.get_tech_resonance_config() or {}).get('bands'))
                except Exception:
                    pass
                # 按交易所聚合
                task['sectors'] = self._aggregate_futures_sectors(task['results'])
                # 落盘期货扫描缓存（与股票隔离），切回期货时可直接加载上次结果
                try:
                    self._save_scan_cache('futures', task['results'],
                                          task.get('used_scheme_name'),
                                          task.get('used_scheme_period'),
                                          task=task)
                except Exception:
                    pass
                task['is_running'] = False
                task['status'] = 'completed' if not task.get('is_cancelled') else 'cancelled'
                task['eta'] = 0
            except Exception as e:
                logger.exception("_futures_scan 失败")
                task['is_running'] = False
                task['status'] = 'error'
                task['error'] = str(e)

        threading.Thread(target=_futures_scan, daemon=True, name='futures-scan').start()
        return {'task_id': task_id, 'total': len(contract_list)}

    @staticmethod
    def _aggregate_futures_sectors(results):
        """按品种板块聚合期货扫描结果。

        兼容旧缓存：英文交易所短码(SHFE)或中文交易所全名(上海期货交易所)均归「其他」，
        与扫描端未归类兜底一致，确保板块列/板块强度不再出现交易所名称。
        """
        from collections import defaultdict
        # 交易所短码 + 中文全名 → 归「其他」（新扫描结果未归类已是「其他」，此处兜底旧缓存）
        _EXCHANGE_CN = {
            'SHFE': '其他', 'DCE': '其他', 'CZCE': '其他',
            'CFFEX': '其他', 'INE': '其他', 'GFEX': '其他',
            '上海期货交易所': '其他', '大连商品交易所': '其他',
            '郑州商品交易所': '其他', '中国金融期货交易所': '其他',
            '上海国际能源交易中心': '其他', '广州期货交易所': '其他',
        }
        sector_data = defaultdict(lambda: {'scores': [], 'stocks': []})
        for r in results:
            se = r.get('sector', '其他')
            sector = _EXCHANGE_CN.get(se, se)  # 交易所名归「其他」，中文板块名原样保留
            score = r.get('final_score', 0)
            sector_data[sector]['scores'].append(score)
            sector_data[sector]['stocks'].append(r)
        sectors = []
        for sector, data in sector_data.items():
            scores = data['scores']
            if not scores:
                continue
            avg = sum(scores) / len(scores)
            sorted_stocks = sorted(data['stocks'], key=lambda x: x.get('final_score', 0), reverse=True)
            level = '强' if avg >= 30 else ('中' if avg >= 20 else ('弱' if avg >= 10 else '极弱'))
            # 触发建仓等级计数：期货初级板块强度按此分布展示（高级不显示，仅传输）
            tier_counts = {'strong': 0, 'standard': 0, 'test': 0, 'pending': 0,
                           'weak_rebound': 0, 'panic_rebound': 0, 'weak': 0}
            for _r in data['stocks']:
                _t = _r.get('entry_tier')
                tier_counts[_t if _t in tier_counts else 'weak'] += 1
            sectors.append({
                'sector': sector,
                'avg_score': round(avg, 1),
                'count': len(scores),
                'strong_count': sum(1 for s in scores if s >= 30),
                'level': level,
                'tier_counts': tier_counts,
                'max_stock': sorted_stocks[0],
                'min_stock': sorted_stocks[-1],
            })
        sectors.sort(key=lambda x: x['avg_score'], reverse=True)
        return sectors

    @staticmethod
    def _scan_market_key(market_type):
        """扫描缓存按市场隔离：期货/股票分别落盘。"""
        return 'futures' if market_type == 'futures' else 'stock'

    @staticmethod
    def _scan_cache_file(market):
        return os.path.join(CACHE_DIR, f'market_scan_cache_{market}.json')

    @staticmethod
    def _scan_cache_file_bak(market):
        """上一次成功落盘的缓存备份（原子替换前保留），供 load_scan_cache 损坏自愈。"""
        return WebAPI._scan_cache_file(market) + '.bak'

    @staticmethod
    def _cache_status_from_time(scan_time_str):
        """由 scan_time 计算缓存新鲜度，返回 ``(cache_status, age_hours)``。

        仅此一处实现，`load_scan_cache`（读磁盘）与 `get_scan_progress`（读内存任务）
        共用同一口径，避免两条路径对「同一份结果」给出不同的过期结论。

        判定顺序必须 **先 >72 再 >24**：原实现写成 `if >24 ... elif >72`，而 >72 必然
        先命中 >24，导致 `very_stale` 分支恒不可达、「已过期超过3天」的提示是死代码
        （2026-09-21 修正，原登记见 docs/h5-feasibility.md:426）。
        """
        if not scan_time_str:
            return 'unknown_time', None
        try:
            from dateutil.parser import parse as parse_iso
            scan_time = parse_iso(scan_time_str)
            age_hours = (datetime.now(scan_time.tzinfo) - scan_time).total_seconds() / 3600
        except Exception:
            return 'unknown_time', None
        if age_hours > 72:
            return 'very_stale', age_hours
        if age_hours > 24:
            return 'stale', age_hours
        return 'fresh', age_hours

    @staticmethod
    def _save_scan_cache(market, results, used_scheme_name=None, used_scheme_period=None, task=None):
        """保存扫描结果到缓存文件（按市场隔离）。

        原子写：先写同目录临时文件，fsync 后 os.replace 覆盖目标，杜绝「写中途进程被关/崩溃/
        并发写」留下半截文件（2026-09-19 实盘复现：market_scan_cache_stock.json 在 1.77MB 处
        截断 → 下次 json.load 失败 → load_scan_cache 走异常分支 → 前端静默 return → 界面显示空）。
        替换前若当前目标为有效 JSON，先 rename 为 .bak 作为自愈备份；当前已损坏则跳过，避免用坏覆盖好。

        used_scheme_name/used_scheme_period 来自 task 字典，写进缓存用于事后取证：
        用户扫描结果与个股分析方案不一致时，直接读这两个字段即可确认「扫描到底用了哪个方案」，
        不用再复现或加运行时日志。允许 None：老 EXE / 未传字段时不报错。

        task 非 None 时回写 ``task['scan_time']``：`get_scan_progress` 据此把「这份结果的
        数据时刻」告诉前端，前端才能刷新状态栏的过期标记。缺了这一步，扫描完成后前端仍
        挂着**进程启动时读到的旧缓存**的 stale 标记（2026-09-21 实盘：刚扫完却提示
        「数据超过24小时」，见 load_scan_cache / _scanCacheInfo 的状态残留问题）。
        """
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            cache_file = WebAPI._scan_cache_file(market)
            bak_file = WebAPI._scan_cache_file_bak(market)
            scan_time_str = datetime.now().isoformat()
            if task is not None:
                # 落盘成败都回写：这份 results 确实是此刻生成的，与是否写盘无关
                task['scan_time'] = scan_time_str
            payload = {
                'source': 'user',
                'market': market,
                'scan_time': scan_time_str,
                'total_stocks': len(results),
                'results': results,
            }
            if used_scheme_name:
                payload['used_scheme_name'] = used_scheme_name
            if used_scheme_period:
                payload['used_scheme_period'] = used_scheme_period
            # 扫描方向落盘：期货多/空方案隔离，读缓存时二级过滤需按同方向判定
            # （否则空单方案的条件翻转失效 → 筛选语义反向）。股票恒 'long'。
            payload['scan_direction'] = WebAPI._scan_task_direction(task)
            # 当前目标若为有效缓存，先保留为 .bak（损坏则跳过，避免用坏覆盖好）
            if os.path.exists(cache_file):
                try:
                    json.loads(open(cache_file, 'rb').read())
                    try:
                        os.replace(cache_file, bak_file)
                    except OSError:
                        pass
                except Exception:
                    pass
            # 原子写：临时文件同盘，os.replace 为重命名（Windows 亦原子），读者永远看到完整文件
            fd, tmp = tempfile.mkstemp(dir=CACHE_DIR, suffix='.tmp')
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, cache_file)
            except Exception:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
        except Exception as e:
            # 不再静默吞错——写入失败意味着下次加载仍是旧数据，必须告警
            logger.error("_save_scan_cache 写入失败: %s (market=%s, results=%d条)", e, market, len(results))

    def get_scan_progress(self, task_id, min_score=None, topn=None, rating=None, offset=0, limit=0, sort='score', max_score=None,
                          rating_op=None, chg_op=None, chg_val=None):
        """返回扫描进度。

        min_score/max_score/topn/rating 显式传入时覆盖任务启动时的筛选值（并写回任务，
        使后续轮询与导出口径一致）；不传则沿用任务存储值。
        min_score/max_score 组成因子预期分筛选区间 [min, max]；max_score=None 表示不限上限。
        rating_op：星级比较方式 ge/gt/eq/le/lt（配合 rating='N星'；'全部' 时不生效）。
        chg_op/chg_val：涨幅比较方式与阈值（如 chg_op='lt', chg_val=9.8 排除涨停股）；
        chg_val 传 '' 表示清除该条件。
        offset/limit 支持分页加载；limit=0 表示不限制（返回全部）。
        sort：排序方式——'score'=因子预期分降序（默认）；'percentile'=因子预期分全样本
        百分位降序（分位越高代表在全市场扫描样本中越靠前）。percentile 基于扫描全量结果计算
        （与分页/筛选无关），使「分位」排序反映样本内相对位置。
        """
        task = self._scan_tasks.get(task_id)
        if not task:
            return {'error': '无效的 task_id'}
        # 显式筛选参数覆盖（前端「应用筛选」）
        if min_score is not None:
            task['min_score'] = float(min_score or 0)
        if max_score is not None:
            task['max_score'] = None if max_score == '' else float(max_score)
        if topn is not None:
            task['topn'] = int(topn or 0)
        if rating is not None:
            task['rating'] = rating or '全部'
        if rating_op is not None:
            task['rating_op'] = rating_op or 'ge'
        if chg_op is not None:
            task['chg_op'] = chg_op or None
        if chg_val is not None:
            task['chg_val'] = None if chg_val == '' else float(chg_val)
        # 快照副本：扫描 worker 线程仍在后台对 task['results'] 原地 extend/sort，
        # 若直接持引用在此排序会与 worker 并发修改同一 list 触发
        # “list modified during sort”。复制隔离后，本读路径与 worker 各操作各自副本。
        results = list(task['results'])

        is_futures = task.get('market_type') == 'futures'
        min_score = task.get('min_score', 0)
        max_score = task.get('max_score')
        topn = task.get('topn', 0)
        rating = task.get('rating', '全部')
        min_stars = {'5星': 5, '4星': 4, '3星': 3, '2星': 2, '1星': 1}.get(rating, 0)
        rating_op = task.get('rating_op') or 'ge'
        chg_op = task.get('chg_op')
        chg_val = task.get('chg_val')

        # ── 技术二级过滤判定结果打标记（全量、增量缓存）：同任务同配置只逐条判定一次，
        # 后续筛选/翻页/轮询直接按 _tech_pass 标记 O(n) 过滤，任何筛选条件都能秒开。
        # 初级已按触发档位分组（分组即筛选），故不套用；方向由 task 自带（期货多/空隔离）。
        results, scan_filter_partial = self._ensure_scan_tech_pass(task, results)

        # ── 廉价裁剪（O(n)，不依赖排序）：分数区间 + 星级 + 涨幅先行，
        # 全市场 5000+ 条先缩到候选集，排序/板块聚合只处理候选集。
        # 期货多空双向：final_score 正负代表多/空方向，负分（看空）是有效信号而非噪声，
        # 不做 min_score 分数剔除（2026-08-13 修复"仅匹配 27 个品种"根因）。
        if is_futures:
            filtered = list(results)
        else:
            # 区间筛选：[min_score, max_score]，max_score=None 表示不限上限。
            # 初级=按技术强度(_scan_rank)口径筛选；高级=按因子预期分。
            filtered = [r for r in results
                        if self._scan_rank(r) >= min_score
                        and (max_score is None or self._scan_rank(r) <= max_score)]
        if min_stars > 0:
            # 星号两种写法都要认：engine/signal_rating 实际产出 emoji ⭐（U+2B50），
            # 历史/外部数据可能是 ★（U+2605）。只认一种会让星级过滤整批清空。
            # 比较方式由 rating_op 决定：≥N（默认）/ >N / =N（精确，只看该星级）/ ≤N / <N。
            filtered = [r for r in filtered
                        if self._scan_cmp(rating_op,
                                          sum(1 for ch in (r.get('stars') or '') if ch in '★⭐'),
                                          min_stars)]
        # 涨幅筛选：典型用法 chg_op='lt', chg_val=9.8 → 排除涨停股。
        # change_pct 缺失（旧缓存未含该字段）视为不可比较 → 排除，避免误放行。
        if chg_val is not None and chg_op in ('ge', 'gt', 'eq', 'le', 'lt'):
            filtered = [r for r in filtered
                        if r.get('change_pct') is not None
                        and self._scan_cmp(chg_op, float(r.get('change_pct')), chg_val)]

        # 技术条件过滤：用已打好的标记裁剪候选集（O(m)，m=候选集规模）
        if not is_futures and not self._is_basic_mode():
            filtered = [r for r in filtered if r.get('_tech_pass', True)]

        # 排序（候选集规模已缩小；percentile 基于全样本缓存值，不随筛选变化）
        _sort = sort or 'score'
        if _sort == 'percentile':
            # 惰性计算一次并缓存：分位基于扫描全量结果（全样本），不随分页/筛选变化
            if not task.get('_pct_ready'):
                self._attach_percentile(list(task['results']))
                task['_pct_ready'] = True
            _key = lambda r: r.get('percentile', 0)
        else:
            _key = lambda r: self._scan_rank(r)
        filtered.sort(key=_key, reverse=True)

        # 板块聚合：基于裁剪+技术过滤后的候选集（topn 截断前），与页面展示一致
        sector_source = filtered
        if topn > 0:
            filtered = filtered[:topn]
        # 分页切片（#019）；limit=0 表示不限制（返回全部）
        offset = max(0, int(offset or 0))
        limit = max(0, int(limit or 0))
        if limit > 0:
            paged = filtered[offset:offset + limit]
        else:
            paged = filtered[offset:]
        # 剥离 tech_snapshot：仅服务端过滤用，前端展示/排序不需要（每条可能带完整 K线/市场/ADX 快照，
        # 全市场 5000+ 条会使单次 get_scan_progress 载荷膨胀到数十 MB，导致「应用筛选」卡顿不响应）。
        if paged:
            paged = [{k: v for k, v in r.items() if k != 'tech_snapshot'} for r in paged]
        # 数据时刻 / 新鲜度：由本次任务落盘时回写的 scan_time 算出（_save_scan_cache(task=...)）。
        # 前端据此刷新状态栏过期标记 —— 缺了它，扫描完成后前端仍会沿用**进程启动时读到的
        # 旧缓存**的 stale 标记，出现「刚扫完却提示数据超过24小时」（2026-09-21 实盘）。
        _scan_time_str = task.get('scan_time')
        _cache_status, _cache_age = WebAPI._cache_status_from_time(_scan_time_str)
        return {
            'done': not task['is_running'],
            'total': task['total'],
            'progress': task['progress'],
            'success_count': task['success_count'],
            'matched_count': len(filtered),
            'has_more': offset + len(paged) < len(filtered),
            'is_paused': bool(task.get('is_paused')),
            'results': paged,
            'sectors': self._aggregate_sectors(sector_source),  # 板块基于候选集（topn 前），与筛选结果一致
            'scan_filter_partial': scan_filter_partial,   # 技术二级过滤未完全生效（旧缓存缺快照）
            'finance_filtered': task.get('finance_filtered', 0),  # 财务筛选剔除数（前端完成提示用）
            'skipped_count': task.get('skipped_count', 0),  # watchdog 放弃的网络超时分片股票数
            'status': task['status'],
            'error': task.get('error'),
            'rate': task.get('rate', 0.0),
            'eta': task.get('eta', 0.0),
            'elapsed': task.get('elapsed', 0.0),
            'cache_hit': self._get_cache_hit_count() if task.get('status') == 'completed' else 0,
            'used_scheme_name': task.get('used_scheme_name'),
            'used_scheme_period': task.get('used_scheme_period'),
            'scan_time': _scan_time_str,
            'cache_status': _cache_status,
            'cache_age_hours': round(_cache_age, 1) if _cache_age is not None else None,
        }

    def _get_cache_hit_count(self):
        """获取本次扫描的K线缓存命中次数"""
        try:
            from engine.data_layer import KLineFetcher
            return getattr(KLineFetcher, '_cache_hit_log', 0)
        except Exception:
            return 0


    def _attach_percentile(self, results):
        """给扫描结果批量附加 percentile 字段（排序依据分全样本百分位，0-100）。

        - 基于扫描全量结果的 _scan_rank 排序位置，分位越高代表该票在样本中越靠前；
        - O(n log n)（序数映射），避免逐票 O(n²) 比较（5000+ 只会卡死）；
        - 并列分按序数分配（稳定），空/异常分数按 0 处理。
        """
        n = len(results)
        if n == 0:
            return
        try:
            # (score, index) 排序，索引保证稳定性；percentile = 升序序数/总数*100
            ordered = sorted(
                ((self._scan_rank(r), i) for i, r in enumerate(results)),
                key=lambda x: x[0])
            for rank, (_, i) in enumerate(ordered):
                results[i]['percentile'] = round((rank + 1) / n * 100, 1)
        except Exception:
            for r in results:
                r['percentile'] = 0

    def refresh_sectors(self, task_id=None):
        """实时聚合板块强度（不需等扫描完成）。

        前端点击「刷新板块」按钮时调用，从当前已有扫描结果即时聚合。
        """
        # 找到指定 task 或任意有结果的 task
        tasks_to_check = []
        if task_id and task_id in self._scan_tasks:
            tasks_to_check.append(self._scan_tasks[task_id])
        else:
            # 优先运行中的任务，其次已完成的任务
            for t in self._scan_tasks.values():
                if t.get('results'):
                    tasks_to_check.append(t)
        if not tasks_to_check:
            # 尝试缓存
            try:
                cache_file = WebAPI._scan_cache_file(WebAPI._scan_market_key(self._market_type))
                if os.path.exists(cache_file):
                    with open(cache_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    if data.get('results'):
                        _r = data['results']
                        _r, _partial = self._apply_scan_tech_filter(
                            _r, direction=data.get('scan_direction') or 'long')
                        return {'sectors': self._aggregate_sectors(_r), 'count': len(_r)}
            except Exception:
                pass
            return {'sectors': [], 'count': 0}
        # 取结果最多的 task
        best_task = max(tasks_to_check, key=lambda t: len(t.get('results', [])))
        # 快照副本：与 worker 线程对 task['results'] 的并发写隔离（同 get_scan_progress）
        results = list(best_task.get('results', []))
        results, _partial = self._apply_scan_tech_filter(
            results, direction=self._scan_task_direction(best_task))
        sectors = self._aggregate_sectors(results)
        # 更新 task 中的缓存
        best_task['sectors'] = sectors
        return {'sectors': sectors, 'count': len(results)}

    def get_sector_stocks(self, sector_name, min_score=0):
        """返回某板块下已扫描的股票列表（按板块/分数筛选）。"""
        # 查找任意运行中/已完成的任务
        candidates = []
        seen_codes = set()  # 按代码去重，避免多次扫描结果叠加导致重复
        for task in self._scan_tasks.values():
            for r in task.get('results', []):
                code = r.get('code', '')
                if code and code not in seen_codes:
                    seen_codes.add(code)
                    candidates.append(r)
        if not candidates:
            # 尝试缓存
            try:
                cache_file = WebAPI._scan_cache_file(WebAPI._scan_market_key(self._market_type))
                if os.path.exists(cache_file):
                    with open(cache_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                for r in data.get('results', []):
                    code = r.get('code', '')
                    if code and code not in seen_codes:
                        seen_codes.add(code)
                        candidates.append(r)
            except Exception:
                pass
        # 高级技术指标二级过滤（高级生效；对 basic 短路），保证板块成分与过滤后 grid 一致
        candidates, _partial = self._apply_scan_tech_filter(
            candidates, direction=self._current_scan_direction())
        # 按板块/分数筛选（结果已按触发档位分组，无需二级过滤）
        stocks = [r for r in candidates
                  if r.get('sector') == sector_name and r.get('final_score', 0) >= float(min_score or 0)]
        stocks.sort(key=lambda x: x.get('final_score', 0), reverse=True)
        return {'stocks': stocks}

    def get_sector_map(self):
        """加载板块映射文件，返回 JSON 字符串和统计信息。"""
        try:
            if os.path.exists(SECTOR_MAP_FILE):
                with open(SECTOR_MAP_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                total = sum(len(v) for v in data.values())
                return {'loaded': True, 'data': data, 'sectors': len(data), 'total_codes': total}
            # 尝试从 engine/config 种子文件加载
            seed = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'engine', 'config', 'sector_map.json')
            if os.path.exists(seed):
                with open(seed, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                total = sum(len(v) for v in data.values())
                return {'loaded': True, 'data': data, 'sectors': len(data), 'total_codes': total, 'source': 'seed'}
            return {'loaded': False, 'data': {}, 'sectors': 0, 'total_codes': 0}
        except Exception as e:
            return {'loaded': False, 'data': {}, 'sectors': 0, 'total_codes': 0, 'error': str(e)}

    def save_sector_map(self, json_str):
        """保存板块映射到 sector_map.json。"""
        try:
            data = json.loads(json_str)
            if not isinstance(data, dict):
                return {'success': False, 'error': '数据格式错误：应为 {板块名: [代码列表]}'}
            os.makedirs(os.path.dirname(SECTOR_MAP_FILE), exist_ok=True)
            with open(SECTOR_MAP_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            total = sum(len(v) for v in data.values())
            return {'success': True, 'sectors': len(data), 'total_codes': total}
        except json.JSONDecodeError as e:
            return {'success': False, 'error': f'JSON格式错误: {e}'}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def create_sector_template(self):
        """创建板块映射模板文件。"""
        if os.path.exists(SECTOR_MAP_FILE):
            return {'success': False, 'error': 'sector_map.json 已存在，请使用编辑修改'}
        template = {
            "酿酒": ["600519", "000858", "000568", "600809", "002304"],
            "证券": ["600030", "600837", "601688", "000776", "601211"],
            "银行": ["600036", "601398", "601288", "601328", "600000"],
            "半导体": ["688981", "688012", "688041", "688008", "688256"],
            "新能源": ["300750", "002594", "601012", "600438", "300274"],
            "医药": ["600276", "000538", "300760", "600196", "002001"],
            "房地产": ["000002", "600048", "001979", "600383", "000069"],
            "保险": ["601318", "601628", "601601"],
            "家电": ["000333", "600690", "000651"],
            "汽车": ["002594", "601127", "600104", "000625"],
            "食品饮料": ["600887", "603288", "600298"],
            "通信": ["600050", "300308", "002475"],
            "计算机": ["300033", "002230", "300454"],
            "军工": ["600150", "601989", "600760"],
            "有色": ["600111", "600547", "601600"],
            "煤炭": ["601088", "601225", "600188"],
            "石油": ["601857", "600028", "601808"],
            "电力": ["600900", "601985", "600011"],
            "化工": ["600309", "002648", "600426"],
            "钢铁": ["600019", "000932", "002318"],
            "建筑": ["601668", "601186", "601390"],
            "交通": ["601816", "601006", "600009"],
            "传媒": ["300413", "002027", "601888"],
            "机械": ["000425", "600031", "601100"],
            "建材": ["600585", "000786", "002271"],
            "农林渔牧": ["000876", "300498", "002714"]
        }
        try:
            os.makedirs(os.path.dirname(SECTOR_MAP_FILE), exist_ok=True)
            with open(SECTOR_MAP_FILE, 'w', encoding='utf-8') as f:
                json.dump(template, f, ensure_ascii=False, indent=2)
            return {'success': True, 'sectors': len(template), 'total_codes': sum(len(v) for v in template.values())}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def export_scan_results(self, task_id, path):
        """导出 CSV/文本（前端传入完整路径）。"""
        task = self._scan_tasks.get(task_id)
        if not task:
            return {'success': False, 'error': '无效的 task_id'}
        results = task.get('results', [])
        if not results:
            return {'success': False, 'error': '无扫描结果'}
        try:
            import csv as _csv
            with open(path, 'w', encoding='utf-8-sig', newline='') as f:
                writer = _csv.writer(f)
                writer.writerow(['排名', '代码', '名称', '现价', '因子预期', '评级', '板块'])
                for i, r in enumerate(results, 1):
                    writer.writerow([i, r['code'], r['name'], f"{r.get('price', 0):.2f}",
                                     f"{r.get('final_score', 0):.0f}", r.get('stars', ''),
                                     r.get('sector', '')])
            return {'success': True, 'count': len(results)}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def show_save_dialog(self, suggested_name='scan_results.csv', file_types=None):
        """弹出原生文件保存对话框，返回用户选择的路径（对齐 #021）。

        统一使用 pywebview 原生对话框；不可用时返回 None，前端回退到 prompt 手动输入。
        file_types：可选文件类型过滤器元组，默认 CSV；导出方案 JSON 时传 JSON 过滤器。

        ⛔ 不做「原生对话框不可用 → 命令行/其它对话框」的降级：冻结态已排除 GUI 工具包，
        任何降级分支在 EXE 下恒失败，属死代码。
        """
        try:
            import webview
            if self._window:
                if not file_types:
                    file_types = ('CSV Files (*.csv)', 'All Files (*.*)')
                result = self._window.create_file_dialog(
                    webview.SAVE_DIALOG,
                    save_filename=suggested_name,
                    file_types=file_types,
                )
                if result:
                    path = result[0] if isinstance(result, (list, tuple)) else result
                    return {'path': path}
                return {'path': None}  # 用户取消
        except Exception as e:
            logger.warning('pywebview save dialog failed: %s', e)
        return {'path': None}

    def save_scheme_to_file(self, path, mode='current', scheme_name=None):
        """将方案导出为 JSON 并写入指定路径（前端已通过原生保存对话框确定路径）。

        复用 export_scheme 的 JSON 生成逻辑，成功写盘后返回完整路径，
        供前端明确提示用户「方案已保存到哪个文件」。
        """
        try:
            r = self.export_scheme(mode, scheme_name)
            if not r.get('success'):
                return {'success': False, 'error': r.get('error', '生成方案 JSON 失败')}
            with open(path, 'w', encoding='utf-8', newline='') as f:
                f.write(r['json'])
            return {'success': True, 'path': path, 'mode': mode}
        except Exception as e:
            logger.exception('save_scheme_to_file 失败')
            return {'success': False, 'error': f'保存方案失败：{e}'}

    def load_scan_cache(self):
        """加载 cache/market_scan_cache_{market}.json（按市场隔离），附带过期校验。"""
        try:
            cache_file = WebAPI._scan_cache_file(WebAPI._scan_market_key(self._market_type))
            if not os.path.exists(cache_file):
                return {'results': [], 'sectors': [], 'cache_status': 'no_cache'}
            recovered = False
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception:
                # 自愈：当前缓存损坏 → 尝试 .bak 备份（原子写遗留），避免直接空白
                bak_file = WebAPI._scan_cache_file_bak(WebAPI._scan_market_key(self._market_type))
                if os.path.exists(bak_file):
                    try:
                        with open(bak_file, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                        recovered = True
                    except Exception:
                        data = None
                if data is None:
                    return {'results': [], 'sectors': [], 'cache_status': 'corrupt',
                            'message': '扫描缓存文件损坏（JSON 解析失败），请重新点击「全市场扫描」生成新缓存。'}
            if data.get('source') != 'user':
                return {'results': [], 'sectors': [], 'cache_status': 'invalid_source'}
            results = data.get('results', [])
            # 高级模式技术指标二级过滤（对 basic 短路；方向取缓存落盘的 scan_direction）；
            # 旧缓存无快照 → scan_filter_partial
            results_filtered, _filter_partial = self._apply_scan_tech_filter(
                results, direction=data.get('scan_direction') or 'long')
            scan_time_str = data.get('scan_time', '')

            # 过期校验：统一走 _cache_status_from_time，与 get_scan_progress 同一口径。
            # （原实现 `if >24 ... elif >72` 使 very_stale 恒不可达，已修正）
            cache_status, cache_age_hours = WebAPI._cache_status_from_time(scan_time_str)
            if recovered:
                cache_status = 'recovered'

            # 注入到任务池，方便前端统一读取
            task_id = 'cache_' + str(uuid.uuid4())[:8]
            self._scan_tasks[task_id] = {
                'task_id': task_id, 'is_running': False, 'is_cancelled': False,
                'total': len(results_filtered), 'progress': 100, 'success_count': len(results_filtered),
                'results': results_filtered, 'sectors': self._aggregate_sectors(results_filtered),
                'status': 'completed', 'error': None,
                'min_score': 0, 'topn': 0, 'rating': '全部',
                'market_type': self._market_type,
                'scan_direction': data.get('scan_direction') or 'long',
            }
            return {'task_id': task_id, 'results': results_filtered,
                    'sectors': self._aggregate_sectors(results_filtered),
                    'scan_time': scan_time_str,
                    'cache_status': cache_status,
                    'scan_filter_partial': _filter_partial,
                    'cache_age_hours': round(cache_age_hours, 1) if cache_age_hours is not None else None,
                    'message': '当前缓存曾损坏，已从上一可用备份恢复' if recovered else None}
        except Exception as e:
            logger.warning("load_scan_cache 异常: %s", e)
            return {'error': str(e), 'cache_status': 'error'}

    def cancel_market_scan(self, task_id):
        """取消扫描任务。"""
        task = self._scan_tasks.get(task_id)
        if task:
            task['is_cancelled'] = True
            task['is_running'] = False
            task['status'] = 'cancelled'
            pe = task.get('pause_event')
            if pe:
                pe.set()  # 解除暂停阻塞，让循环能看到 cancel 标志
            return {'success': True}
        return {'success': False, 'error': '无效的 task_id'}

    def pause_market_scan(self, task_id, paused=True):
        """暂停/继续扫描任务。"""
        task = self._scan_tasks.get(task_id)
        if not task:
            return {'success': False, 'error': '无效的 task_id'}
        if not task.get('is_running'):
            return {'success': False, 'error': '任务已结束'}
        pe = task.get('pause_event')
        if not pe:
            return {'success': False, 'error': '任务不支持暂停'}
        if paused:
            pe.clear()
            task['is_paused'] = True
            task['status'] = 'paused'
        else:
            pe.set()
            task['is_paused'] = False
            task['status'] = 'running'
        return {'success': True, 'is_paused': task['is_paused']}

    # ═══════════════════════════════════════════════════════════
    # 授权管理
    # ═══════════════════════════════════════════════════════════
    def activate_license(self, code):
        """激活授权码，返回 {success, message}。"""
        try:
            ok, msg = _activate_license(code)
            return {'success': ok, 'message': msg}
        except Exception as e:
            return {'success': False, 'message': f"激活异常: {e}"}

    def deactivate_license(self):
        """注销当前授权。"""
        try:
            deactivate()
            return {'success': True}
        except Exception as e:
            return {'success': False, 'message': str(e)}

    # ═══════════════════════════════════════════════════════════
    # 模型配置方案管理
    # ═══════════════════════════════════════════════════════════
    def switch_scheme(self, name):
        """切换当前方案。"""
        try:
            quant_config.load_config(force_reload=True)
            ok = quant_config.switch_scheme(name)
            if ok:
                return {'success': True}
            schemes = quant_config.get_schemes() or {}
            if name not in schemes:
                return {'success': False, 'error': f'方案不存在：{name}'}
            return {'success': False, 'error': f'切换失败：方案写回异常（{name}）'}
        except Exception as e:
            logger.exception("switch_scheme 失败")
            return {'success': False, 'error': f'切换异常：{e}'}

    @staticmethod
    def _validate_scheme_name(name):
        """方案名校验（非空/可打印/无连字符/长度≤50）。"""
        if not name or not name.strip():
            return '请输入方案名称'
        if not name.isprintable():
            return '方案名称不能包含控制字符'
        if '-' in name:
            return "方案名称不能包含 '-' 字符"
        if len(name) > 50:
            return '方案名称不能超过50个字符'
        return None

    def add_scheme(self, name, label=None, desc=None, meta=None, factor_profile=None):
        """新增方案。meta 用于写入市场/方向/周期标签；factor_profile 用于期货因子共用。"""
        try:
            err = self._validate_scheme_name(name)
            if err:
                return {'success': False, 'error': err}
            quant_config.load_config(force_reload=True)
            if name in quant_config.get_schemes():
                return {'success': False, 'error': f'方案名已存在：{name}'}
            ok = quant_config.add_scheme(
                name, label=label, desc=desc,
                meta=meta, factor_profile=factor_profile)
            if not ok:
                return {'success': False, 'error': '新增失败：未知原因'}
            return {'success': True}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def duplicate_scheme(self, source, new_name, new_label=None):
        """复制方案。"""
        try:
            err = self._validate_scheme_name(new_name)
            if err:
                return {'success': False, 'error': err}
            quant_config.load_config(force_reload=True)
            schemes = quant_config.get_schemes()
            if source not in schemes:
                return {'success': False, 'error': f'源方案不存在：{source}'}
            if new_name in schemes:
                return {'success': False, 'error': f'方案名已存在：{new_name}'}
            ok = quant_config.duplicate_scheme(source, new_name, new_label=new_label)
            if not ok:
                return {'success': False, 'error': '复制失败：未知原因（源方案可能已损坏）'}
            return {'success': True}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def remove_scheme(self, name):
        """删除方案。"""
        try:
            quant_config.load_config(force_reload=True)
            schemes = quant_config.get_schemes() or {}
            if name not in schemes:
                return {'success': False, 'error': f'方案不存在：{name}'}
            ok = quant_config.remove_scheme(name)
            if ok:
                return {'success': True}
            return {'success': False, 'error': f'删除失败：方案写回异常（{name}）'}
        except Exception as e:
            logger.exception("remove_scheme 失败")
            return {'success': False, 'error': f'删除异常：{e}'}

    def rename_scheme(self, old_name, new_name):
        """重命名方案（保留内容与 _meta）。"""
        try:
            err = self._validate_scheme_name(new_name)
            if err:
                return {'success': False, 'error': err}
            quant_config.load_config(force_reload=True)
            ok, e = quant_config.rename_scheme(old_name, new_name)
            if not ok:
                return {'success': False, 'error': e}
            return {'success': True}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    # ── 方案文件契约（v2，自包含） ──────────────────────────────
    # {"format":"mbull-scheme","version":2,"kind":"scheme"|"library","exported_at":...,
    #  "source":{"app":"M-Bull","current_scheme":...},
    #  "schemes":{name:{label,desc,_meta,factor_profile,config,period_configs}},
    #  "factor_profiles":{被引用的 profile 快照}}
    # ⛔ 关键：导出时把引用的 factor_profile 的因子三键**内联展开进 config**。
    #    否则引用 profile 的方案（因子只存在 factor_profiles 里、内联 config 无因子键）
    #    导出的文件再导入时会因子段为空 —— 即「自己导出的方案自己导不进来」。
    SCHEME_FILE_FORMAT = 'mbull-scheme'
    SCHEME_FILE_VERSION = 2
    # 因子段三键：由 factor_profile 独占，导入/导出都要围绕它做内联与回写
    _FACTOR_KEYS = ('active_factors', 'factor_configs', 'score_scale')
    # 方案配置的特征键（用于判断「这个文件到底是不是方案」）
    _CONFIG_HINT_KEYS = _FACTOR_KEYS + (
        'thresholds', 'entry_params', 'entry_conditions', 'add_params', 'reduce_params',
        'risk_params', 'conflict_penalty', 'tech_resonance', 'market_gate',
        'ghost_rules', 'scan_tech_filter', 'veto_params', 'enabled', 'backtest',
    )

    def _scheme_export_body(self, sc):
        """构造单个方案的导出体（因子段内联展开，保证文件自包含可迁移）。"""
        cfg = copy.deepcopy(sc.get('config')) if isinstance(sc.get('config'), dict) else {}
        fp = sc.get('factor_profile')
        profiles = quant_config.get_factor_profiles() or {}
        if fp and isinstance(profiles.get(fp), dict):
            for k in self._FACTOR_KEYS:
                v = profiles[fp].get(k)
                if v:  # 仅展开非空段，避免空 {} 覆盖内联
                    cfg[k] = copy.deepcopy(v)
        body = {
            'label': sc.get('label', '') or '',
            'desc': sc.get('desc', '') or '',
            '_meta': copy.deepcopy(sc.get('_meta') or {}),
            'config': cfg,
        }
        if fp:
            body['factor_profile'] = fp
        pcs = sc.get('period_configs')
        if isinstance(pcs, dict) and pcs:
            body['period_configs'] = copy.deepcopy(pcs)
        return body

    def export_scheme(self, mode='current', scheme_name=None):
        """导出方案 JSON（v2 自包含格式，单方案 / 完整策略库结构一致）。

        mode='all'：全部方案（kind='library'）；mode='current'：单个方案（kind='scheme'）。
        两种模式产出**同一种文件结构**，只是 schemes 里条目数不同 —— 导入端因此只有一条
        代码路径，不再有「单/双格式分叉」的校验漏洞。
        """
        try:
            quant_config.load_config(force_reload=True)
            schemes = quant_config.get_schemes()
            if not isinstance(schemes, dict) or not schemes:
                return {'success': False, 'error': '当前没有可导出的方案'}
            if mode == 'all':
                names = list(schemes.keys())
                kind = 'library'
            else:
                name = scheme_name or quant_config.get_current_scheme_name()
                if not name or name not in schemes:
                    return {'success': False, 'error': f'方案不存在：{name}'}
                names = [name]
                kind = 'scheme'

            out_schemes = {}
            used_fp = set()
            for n in names:
                sc = schemes.get(n)
                if not isinstance(sc, dict):
                    continue
                out_schemes[n] = self._scheme_export_body(sc)
                if sc.get('factor_profile'):
                    used_fp.add(sc['factor_profile'])
            if not out_schemes:
                return {'success': False, 'error': '没有可导出的方案内容'}

            profiles = quant_config.get_factor_profiles() or {}
            payload = {
                'format': self.SCHEME_FILE_FORMAT,
                'version': self.SCHEME_FILE_VERSION,
                'kind': kind,
                'exported_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'source': {
                    'app': 'M-Bull',
                    'current_scheme': quant_config.get_current_scheme_name(),
                },
                'schemes': out_schemes,
                'factor_profiles': {
                    k: copy.deepcopy(profiles[k]) for k in used_fp if isinstance(profiles.get(k), dict)
                },
            }
            return {
                'success': True,
                'name': names[0] if kind == 'scheme' else '完整策略库',
                'kind': kind,
                'count': len(out_schemes),
                'json': json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                'mode': mode,
            }
        except Exception as e:
            logger.exception("export_scheme 失败")
            return {'success': False, 'error': f'导出异常：{e}'}

    def _import_sync_factor_profile(self, s_config, effective_fp):
        """导入方案时，把内联 config 中的因子段(active_factors/factor_configs/score_scale)
        同步写入共享 factor_profiles[fname]。

        与 save_quant_config 的「因子段/方案段分离存储」对齐，修复：
        引用 factor_profile 的方案，导入时若只把 active_factors 写进内联 config，
        _merge_scheme_config 会忽略内联因子键、改读（空的）共享 profile，导致
        「导入落盘有 8 个因子、加载却全部禁用」。

        返回 (s_config, ok)：ok=False 表示 profile 写盘失败；成功时已从 s_config 剔除因子键，
        使方案内联不再冗余携带因子段（与正常保存路径一致）。
        effective_fp 为 None（遗留 A股 平铺方案）时原样返回 s_config，不做任何处理。
        """
        _FACTOR_KEYS = self._FACTOR_KEYS
        if not effective_fp:
            return s_config, True
        factor_section = dict(quant_config.get_factor_profiles().get(effective_fp) or {})
        for k in _FACTOR_KEYS:
            if k in s_config and s_config[k] is not None:
                factor_section[k] = s_config[k]
        if not quant_config.save_factor_profile(effective_fp, factor_section):
            return s_config, False
        for k in _FACTOR_KEYS:
            s_config.pop(k, None)
        return s_config, True

    @staticmethod
    def _resolve_effective_fp(meta, s_fp):
        """计算方案实际引用的因子 profile 名：显式 s_fp 优先；期货方案回落 'futures'；否则 None。"""
        if s_fp:
            return s_fp
        if isinstance(meta, dict) and meta.get('market') == 'futures':
            return 'futures'
        return None

    # ── 导入：归一化解析（兼容 4 种历史格式） ──────────────────
    @staticmethod
    def _json_equal(a, b):
        """结构等价比较（忽略 dict 顺序 / 数值类型差异）。"""
        try:
            return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)
        except Exception:
            return a == b

    def _scheme_body_from(self, sdata):
        """把任意写法的方案节点归一化为 {label,desc,_meta,factor_profile,config,period_configs}。"""
        empty = {'label': '', 'desc': '', '_meta': None, 'factor_profile': None,
                 'config': {}, 'period_configs': {}}
        if not isinstance(sdata, dict):
            return empty
        _STRUCT_KEYS = ('config', '_meta', 'desc', 'label', 'factor_profile', 'period_configs')
        if any(k in sdata for k in _STRUCT_KEYS):
            cfg_in = sdata.get('config')
            return {
                'label': sdata.get('label') or '',
                'desc': sdata.get('desc') or '',
                '_meta': dict(sdata['_meta']) if isinstance(sdata.get('_meta'), dict) else None,
                'factor_profile': sdata.get('factor_profile') or None,
                'config': copy.deepcopy(cfg_in) if isinstance(cfg_in, dict) else {},
                'period_configs': copy.deepcopy(sdata['period_configs'])
                if isinstance(sdata.get('period_configs'), dict) else {},
            }
        # 整个对象就是 config（旧 flat 写法）
        return dict(empty, config=copy.deepcopy(sdata))

    def _normalize_scheme_doc(self, cfg):
        """归一化 → {'kind','schemes':{name|None: body},'profiles':{}}。

        兼容 4 种写法：
          v2   {"format":"mbull-scheme","schemes":{...},"factor_profiles":{...}}
          v1a  {"schemes":{...}}（旧完整策略库）
          v1b  {desc,_meta,factor_profile,config}（旧单方案完整对象）
          v1c  {active_factors, factor_configs, ...}（旧 flat config）
        返回 (doc, None) 或 (None, 错误信息)；schemes 的 key 为 None 表示待自动命名。
        """
        if not isinstance(cfg, dict):
            return None, '配置格式错误：文件内容应为 JSON 对象'
        profiles = cfg.get('factor_profiles') if isinstance(cfg.get('factor_profiles'), dict) else {}
        schemes_raw = cfg.get('schemes')
        if isinstance(schemes_raw, dict) and schemes_raw:
            entries = {n: self._scheme_body_from(v) for n, v in schemes_raw.items()}
            kind = cfg.get('kind') or ('library' if len(entries) > 1 else 'scheme')
            return {'kind': kind, 'schemes': entries, 'profiles': profiles}, None
        body = self._scheme_body_from(cfg)
        # flat config 必须至少含一个已知配置键，否则是别的文件（自选股 / 回测产物…）
        if not body['config'] or not any(k in body['config'] for k in self._CONFIG_HINT_KEYS):
            return None, ('无法识别的方案文件：既不是方案库（{"schemes": {...}}），'
                          '也不含 config / active_factors 等方案配置内容。'
                          '请确认选择的是本工具【导出配置】生成的 .json 文件。')
        return {'kind': 'scheme', 'schemes': {None: body}, 'profiles': profiles}, None

    def _unique_scheme_name(self, base, existing=None):
        """已存在同名时生成 base_1 / base_2 … 唯一名。"""
        existing = quant_config.get_schemes() or {} if existing is None else existing
        if base not in existing:
            return base
        for i in range(1, 1000):
            cand = f"{base}_{i}"
            if cand not in existing:
                return cand
        return f"{base}_{uuid.uuid4().hex[:6]}"

    def _resolve_import_profile(self, fp, cfg_in, sname):
        """决定导入方案引用哪个 factor_profile。

        - 本地无同名 profile → 用原名（随后由导入内容创建）；
        - 本地有且因子段一致 → 沿用（共享，符合 profile 设计）；
        - 本地有但内容不同 → 派生名 `{fp}@{方案名}`：⛔ 绝不静默覆盖共享 profile，
          否则会连带改掉同机其它引用同一 profile 的方案。
        """
        if not fp:
            return None
        local = quant_config.get_factor_profiles() or {}
        cur = local.get(fp)
        if not isinstance(cur, dict):
            return fp
        incoming = {k: cfg_in.get(k) for k in self._FACTOR_KEYS if cfg_in.get(k)}
        if not incoming:
            return fp  # 导入内容不带因子段 → 沿用本地，不动共享 profile
        if all(self._json_equal(cur.get(k), v) for k, v in incoming.items()):
            return fp
        derived = f"{fp}@{sname}"[:50]
        i = 1
        while derived in local:
            derived = f"{fp}@{sname}_{i}"[:50]
            i += 1
        logger.info("导入方案 %s：因子档 %s 与本机不同，改用派生名 %s（避免覆盖共享因子）",
                    sname, fp, derived)
        return derived

    def _restore_scheme(self, name, body):
        """覆盖导入失败时把原方案恢复回去（尽力而为）。"""
        if not name or not isinstance(body, dict):
            return False
        try:
            quant_config.remove_scheme(name)
            return quant_config.add_scheme(
                name, desc=body.get('desc', ''), config=body.get('config') or {},
                meta=body.get('_meta'), factor_profile=body.get('factor_profile'),
                label=body.get('label'))
        except Exception:
            logger.exception("回滚恢复方案失败：%s", name)
            return False

    def _import_one_scheme(self, raw_name, body, profiles, market=None, direction=None,
                           period=None, scheme_name=None, overwrite=False):
        """导入单个方案。返回 {'ok':True,'name','overwritten','warning'} 或 {'ok':False,'name','reason'}。

        原子性：任一步失败即删除已建的方案壳；覆盖模式下还会把原方案恢复回去。
        """
        meta = dict(body.get('_meta') or {})
        if meta.get('market') not in ('stock', 'futures'):
            meta['market'] = market or 'stock'
        if not meta.get('direction'):
            meta['direction'] = (direction or 'long') if meta['market'] == 'futures' else 'long'
        if not meta.get('period'):
            meta['period'] = period or '日K'

        cfg_in = copy.deepcopy(body.get('config') or {})
        if not isinstance(cfg_in, dict):
            cfg_in = {}
        fp = body.get('factor_profile') or self._resolve_effective_fp(meta, None)
        # 文件自带 profile 快照 → 回填缺失的因子段（旧导出文件 / 跨机迁移救命）
        if fp and isinstance(profiles.get(fp), dict):
            for k in self._FACTOR_KEYS:
                if not cfg_in.get(k) and profiles[fp].get(k):
                    cfg_in[k] = copy.deepcopy(profiles[fp][k])

        # ── 命名与覆盖策略 ──
        base_name = (scheme_name or raw_name or '').strip()
        if base_name:
            err = self._validate_scheme_name(base_name)
            if err:
                return {'ok': False, 'name': base_name, 'reason': f'方案名非法：{err}'}
        else:
            base_name = f"导入_{meta['market']}_{meta['direction']}_{meta['period']}".replace('/', '_')[:40]
        existing = quant_config.get_schemes() or {}
        existed = base_name in existing
        name = base_name
        warning = None
        backup = None
        if existed:
            if overwrite:
                backup = copy.deepcopy(existing[base_name])
            else:
                name = self._unique_scheme_name(base_name, existing)
                warning = f'已存在同名方案「{base_name}」，本次导入为「{name}」'

        created = False
        try:
            if existed and overwrite:
                if not quant_config.remove_scheme(name):
                    return {'ok': False, 'name': name, 'reason': '覆盖失败：原方案删除失败'}
            target_fp = self._resolve_import_profile(fp, cfg_in, name)
            if target_fp and fp and target_fp != fp:
                extra = f'因子档「{fp}」与本机不同，已独立存为「{target_fp}」'
                warning = f'{warning}；{extra}' if warning else extra
            if not quant_config.add_scheme(
                    name, desc=body.get('desc', ''), meta=meta,
                    factor_profile=target_fp, label=body.get('label') or None):
                self._restore_scheme(name, backup)
                return {'ok': False, 'name': name, 'reason': '新建方案失败（写盘异常）'}
            created = True
            if not quant_config.switch_scheme(name):
                raise RuntimeError('切换到新方案失败')
            # 因子段写回（引用 profile 时必须，否则加载时因子被判空）
            cfg_in, fp_ok = self._import_sync_factor_profile(cfg_in, target_fp)
            if not fp_ok:
                raise RuntimeError('因子档保存失败（写盘异常）')
            if not quant_config.save_current_scheme(cfg_in):
                raise RuntimeError('方案配置保存失败（写盘异常）')
            for p, pc in (body.get('period_configs') or {}).items():
                quant_config.save_scheme_period_config(name, p, pc)
            if meta.get('period'):
                quant_config.set_scheme_period(name, meta['period'])
            return {'ok': True, 'name': name,
                    'overwritten': bool(existed and overwrite), 'warning': warning}
        except Exception as e:
            logger.exception("导入方案 %s 失败", name)
            if created:
                quant_config.remove_scheme(name)
            self._restore_scheme(name, backup)
            return {'ok': False, 'name': name, 'reason': f'{e}（已回滚，未改动现有方案）'}

    def import_scheme(self, json_text, market=None, direction=None, period=None,
                      scheme_name=None, overwrite=False):
        """导入方案 JSON（v2 自包含格式 + 4 种历史格式兼容）。

        overwrite=True：同名覆盖（方案库导入默认 True，前端已二次确认）；
        否则自动改名，绝不静默覆盖用户已有方案。

        返回 {success, mode:'multi'|'single', imported, overwritten,
              skipped:[{name,reason}], warnings:[...], switched_to}
        """
        try:
            quant_config.load_config(force_reload=True)
            try:
                cfg = json.loads(json_text)
            except json.JSONDecodeError as e:
                return {'success': False, 'error': (
                    f'JSON 解析失败：{e}（请确认选择的是本工具导出的 .json 方案文件）')}
            doc, err = self._normalize_scheme_doc(cfg)
            if err:
                return {'success': False, 'error': err}

            entries = doc['schemes']
            multi = len(entries) > 1
            imported = 0
            overwritten = 0
            skipped = []
            warnings = []
            first_name = None
            for raw_name, body in entries.items():
                res = self._import_one_scheme(
                    raw_name, body, doc['profiles'],
                    market=market, direction=direction, period=period,
                    scheme_name=scheme_name if not multi else None,
                    overwrite=bool(overwrite) or multi,
                )
                if res.get('ok'):
                    if res.get('overwritten'):
                        overwritten += 1
                    else:
                        imported += 1
                    if first_name is None:
                        first_name = res['name']
                    if res.get('warning'):
                        warnings.append(res['warning'])
                else:
                    skipped.append({'name': res.get('name') or '(未命名)',
                                    'reason': res.get('reason', '未知原因')})

            if first_name is None:
                detail = '；'.join(f"{s['name']}（{s['reason']}）" for s in skipped[:5])
                return {
                    'success': False,
                    'error': f'没有任何方案导入成功：{detail or "文件内容为空"}',
                    'skipped': skipped,
                }
            # 切到第一个导入成功的方案
            quant_config.switch_scheme(first_name)
            return {
                'success': True,
                'mode': 'multi' if multi else 'single',
                'imported': imported,
                'overwritten': overwritten,
                'skipped': skipped,
                'warnings': warnings,
                'switched_to': first_name,
            }
        except Exception as e:
            logger.exception("import_scheme 失败")
            return {'success': False, 'error': f'导入异常：{e}'}

    # ═══════════════════════════════════════════════════════════
    # 回测可视化
    # ═══════════════════════════════════════════════════════════
    def start_backtest(self, params):
        """启动子进程回测，返回 task_id。

        params: {mode, stock_pool, years, forward_days, scan_interval, prefix, offline}
        mode: 'strategy' | 'scoreic' | 'factoric'
        """
        try:
            allowed, reason = can_use('query')
            if not allowed:
                return {'error': f"授权受限: {reason}"}

            mode = (params.get('mode') or 'strategy').lower()
            # 期货回测与 A股回测隔离：两种市场各有独立的回测管线与产物
            is_futures = mode in ('futures', 'futuresic')
            if mode not in ('strategy', 'scoreic', 'factoric', 'futures', 'futuresic'):
                return {'error': 'mode 必须是 strategy/scoreic/factoric/futures/futuresic 之一'}

            quant_config.load_config(force_reload=True)
            # 回测入口必须按当前激活方案+市场方向+日K载入配置到 _cache，
            # 否则 _safe_cfg() 只会返回空白种子（导致明明已配置冲突惩罚但 require_config 判空）。
            # 修复：切换方案后，回测读取的是刚切换的方案配置，而非进程缓存的旧方案。
            if not is_futures and mode in ('strategy', 'scoreic'):
                _cur_scheme = quant_config.get_current_scheme_name()
                if _cur_scheme:
                    _cur_meta = quant_config.get_scheme_meta(_cur_scheme) or {}
                    _m = _cur_meta.get('market') or 'stock'
                    _d = _cur_meta.get('direction') or 'long'
                    _p = _cur_meta.get('period') or '日K'
                else:
                    _m, _d, _p = 'stock', 'long', '日K'
                # A股策略/评分IC回测必须强制重载确保取到用户刚保存/切换的方案
                _loaded = quant_config.load_market_scheme(
                    _m, _d, _p, force_reload=True, scheme_name=_cur_scheme)
                if _loaded is None:
                    return {'error': f'未找到 {_m}·{_p} 方案，请先在【模型配置】中创建该周期方案。'}
            elif is_futures and mode in ('futures',):
                _cur_scheme = quant_config.get_current_scheme_name()
                if _cur_scheme:
                    _cur_meta = quant_config.get_scheme_meta(_cur_scheme) or {}
                    _m = _cur_meta.get('market') or 'futures'
                    _d = _cur_meta.get('direction') or 'long'
                    _p = _cur_meta.get('period') or '日K'
                else:
                    _m, _d, _p = 'futures', 'long', '日K'
                _loaded = quant_config.load_market_scheme(
                    _m, _d, _p, force_reload=True, scheme_name=_cur_scheme)
                if _loaded is None:
                    return {'error': f'未找到 {_m}·{_d}·{_p} 方案，请先在【模型配置】中创建。'}
            cfg = quant_config.load_config() or {}
            if not isinstance(cfg, dict):
                return {'error': '当前无有效量化配置'}

            # 配置完整性校验（与诊断/扫描/Banner 统一使用 REQUIRED_STRATEGY SSOT）
            # A股 factoric / 全部期货 mode: factoric 仅遍历注册表原始因子计算IC，不依赖A股方案配置，放行。
            # 可选项（冲突惩罚/风控/幽灵规则等）不计入强制：留空即引擎跳过，不报错——
            # 之前硬要求 conflict_penalty 是 Bug，导致「Banner✓完整但回测红字缺冲突惩罚」。
            if mode in ('strategy', 'scoreic', 'futures'):
                try:
                    _cfg = quant_config._safe_cfg()
                    # 初级用法（因子休眠）：无因子无分级可验证，回测明确禁用（而非无因子误导）
                    if self._is_basic_mode():
                        return {'error': '初级用法暂不开放「回测验证」，可切到「设置 → 用法设置 → 高级用法」后使用。'}
                    if not (_cfg.get('active_factors') or []):
                        return {'error': '配置不完整：无启用因子，请到「模型配置」勾选因子并保存。'}
                    quant_config.require_config(_cfg, quant_config.REQUIRED_STRATEGY)
                except quant_config.ConfigIncompleteError as e:
                    return {'error': f"配置不完整：{'、'.join(e.missing)}，请先在【模型配置】中完成方案核心配置。"}

            consume_use()

            # 股票池映射（隔离：A股↔期货 各自映射；缺省回落各自标准池）
            pool = str(params.get('stock_pool', '') or params.get('pool') or '')
            if is_futures:
                # 期货池：全市场→'all'，自选→'watchlist'，其余（板块/逗号代码）原样
                if pool in ('全市场', '期货全市场'):
                    pool_code = 'all'
                elif pool in ('自选股列表', '期货自选', '自选'):
                    pool_code = 'watchlist'
                elif pool:
                    pool_code = pool
                else:
                    pool_code = 'all'
            else:
                if pool == '全市场':
                    pool_code = 'full'
                elif pool == '自选股列表':
                    pool_code = 'watchlist'
                elif pool and pool != '上证50+创业50+科创50':
                    pool_code = pool
                else:
                    pool_code = '148'

            _years = params.get('years')
            run_params = {
                'full_market': pool_code == 'full',
                'pool': pool_code,
                'years': int(_years) if _years is not None else 3,
                'hold': int(params.get('forward_days', cfg.get('backtest', {}).get('forward_days', 30))),
                'scan': int(params.get('scan_interval', cfg.get('backtest', {}).get('scan_interval', 5))),
                'prefix': params.get('prefix', cfg.get('backtest', {}).get('output_prefix', 'bt')),
                'offline': bool(params.get('offline', False)),
            }
            # 期货模式：方向/周期/多周期衰减/回看天数（隔离于 A股，取自前端或当前期货方案）
            if is_futures:
                run_params['direction'] = str(params.get('direction') or 'long')
                run_params['period'] = str(params.get('period') or '日线')
                run_params['multi_horizon'] = bool(params.get('multi_horizon', False))
                run_params['days'] = int(params.get('days', 300))
            # 参数范围校验（#023）
            if not (5 <= run_params['hold'] <= 60):
                return {'error': f"前瞻天数应在 5~60 之间，当前 {run_params['hold']}"}
            if not (1 <= run_params['scan'] <= 20):
                return {'error': f"扫描间隔应在 1~20 之间，当前 {run_params['scan']}"}
            # 三个回测模式共用默认前缀 'bt' 会导致产物互相覆盖：
            # 仅默认 'bt' 时按模式后缀区分；用户显式填写的前缀原样保留
            if str(run_params['prefix']).strip() == 'bt':
                run_params['prefix'] = f"bt_{mode}"

            launcher = BacktestLauncher()
            task_id = launcher.start(mode, cfg, run_params)
            self._backtest_tasks[task_id] = {
                'launcher': launcher,
                'mode': mode,
                'prefix': run_params['prefix'],
                'hold': run_params['hold'],
                'start_time': datetime.now().isoformat(),
                'logs': [],
                'done': False,
                'error': None,
            }
            # 产物可写性预检：目标文件若正被 Excel/WPS/记事本 打开占用，本次回测末尾的
            # 写盘会 PermissionError → 旧文件残留 → UI 把上次结果当成本次结果展示。
            # 提前提示，避免跑完几十分钟才发现产物没保存。
            locked = self._bt_check_outputs_writable(run_params['prefix'], mode,
                                                     run_params.get('hold'))
            return {'task_id': task_id, 'locked_files': locked}
        except Exception as e:
            logger.exception("start_backtest 失败")
            return {'error': f"启动回测失败: {e}"}

    def _bt_check_outputs_writable(self, prefix, mode='strategy', hold=None):
        """回测启动前的产物可写性预检，返回「被其他程序占用」的文件名列表（空=全部可写）。

        详见 engine/backtest_strategy._safe_write_csv：写盘失败会被记录进 report.json 的
        export_errors，但那时已跑完；这里提前探测，能让用户在开跑前就关掉 Excel。
        """
        root = os.path.join(get_app_dir(), 'reports')
        names = [f'{prefix}_report.json']
        if mode in ('strategy', 'futures'):
            names += [f'{prefix}_equity.csv', f'{prefix}_trades.csv',
                      f'{prefix}_trades_detail.csv', f'{prefix}_trades.json']
        elif mode == 'scoreic':
            names += [f'{prefix}_decile_equity_h{int(hold or 30)}.csv']
        else:  # factoric / futuresic
            names += [f'{prefix}.csv', f'{prefix}.png']
        locked = []
        for n in names:
            p = os.path.join(root, n)
            if not os.path.exists(p):
                continue          # 不存在的文件不存在被占用问题
            try:
                with open(p, 'a', encoding='utf-8'):
                    pass
            except Exception:
                locked.append(n)
        return locked

    # ── 回测产物定位（2026-08-16 起统一输出到 reports/，兼容旧版根目录遗留）──
    def _bt_locate_file(self, fname):
        """定位回测产物：优先 reports/（新），回退数据目录根（旧版遗留），
        再回退项目根（源码 dev 手动跑回测时）。找不到返回 None。"""
        try:
            proj_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            for base in (os.path.join(get_app_dir(), 'reports'),
                         get_app_dir(),
                         proj_root):
                p = os.path.join(base, fname)
                if os.path.isfile(p):
                    return p
        except Exception:
            pass
        return None

    def get_backtest_progress(self, task_id):
        """读取回测进度 JSONL。"""
        task = self._backtest_tasks.get(task_id)
        if not task:
            return {'error': '无效的 task_id'}
        launcher = task['launcher']
        try:
            events = launcher.read_progress()
            has_terminal = False
            eq_delta = []   # 实时净值点增量（type='equity'），供前端边跑边画曲线
            for ev in events:
                _t = ev.get('type')
                if _t == 'equity':
                    # 结构化净值点：不进日志文本，单独归集（read_progress 本身是增量的）
                    _pt = ev.get('point')
                    if isinstance(_pt, dict) and _pt.get('d'):
                        eq_delta.append(_pt)
                    continue
                if ev.get('line'):
                    task['logs'].append(ev['line'])
                if _t in ('done', 'cancel', 'error'):
                    task['done'] = True
                    has_terminal = True
                if _t == 'error':
                    task['error'] = ev.get('message', '回测异常')
            # 崩溃/OOM 宽限检测：
            # 子进程已退出但无终态事件时，宽限 3 次轮询避免误报
            alive = launcher.is_alive()
            if not alive and not task['done']:
                task.setdefault('_grace_count', 0)
                task['_grace_count'] += 1
                if task['_grace_count'] >= 3:
                    task['done'] = True
                    exit_code = launcher.exit_code()
                    task['error'] = f'子进程异常退出（退出码 {exit_code}），可能因内存不足或崩溃'
            elif alive:
                task['_grace_count'] = 0
            return {
                'done': task['done'] or not launcher.is_alive(),
                'alive': launcher.is_alive(),
                'exit_code': launcher.exit_code(),
                'logs': task['logs'][-300:],
                'equity_delta': eq_delta,
                'error': task['error'],
            }
        except Exception as e:
            return {'error': str(e)}

    def get_backtest_result(self, task_id):
        """读取回测结果文件。

        按回测模式映射产物路径：
          - strategy: report(json) + equity(csv) + trades(json)
          - scoreic : report(json) + data(decile_equity_h{hold}.csv)，无 trades
          - factoric: report(json) + data({prefix}.csv) + chart({prefix}.png)
        """
        task = self._backtest_tasks.get(task_id)
        if not task:
            return {'error': '无效的 task_id'}
        prefix = task['prefix']
        mode = task.get('mode', 'strategy')
        root = os.path.join(get_app_dir(), 'reports')

        report_path = self._bt_locate_file(f'{prefix}_report.json') or os.path.join(root, f'{prefix}_report.json')
        # 按模式构造产物路径
        if mode in ('strategy', 'futures'):
            equity_path = self._bt_locate_file(f'{prefix}_equity.csv') or os.path.join(root, f'{prefix}_equity.csv')
            trades_path = self._bt_locate_file(f'{prefix}_trades.json') or os.path.join(root, f'{prefix}_trades.json')
            data_path = None
            chart_path = None
        elif mode == 'scoreic':
            # hold 来自回测配置（start_backtest 时已写入 task）
            hold = task.get('hold') or 30
            data_path = self._bt_locate_file(f'{prefix}_decile_equity_h{hold}.csv') or os.path.join(root, f'{prefix}_decile_equity_h{hold}.csv')
            equity_path = None
            trades_path = None
            chart_path = None
        else:  # factoric / futuresic：CSV 全因子明细 + PNG 柱状图
            data_path = self._bt_locate_file(f'{prefix}.csv') or os.path.join(root, f'{prefix}.csv')
            chart_path = self._bt_locate_file(f'{prefix}.png') or os.path.join(root, f'{prefix}.png')
            equity_path = None
            trades_path = None

        result = {'report': None, 'mode': mode, 'files': {}}

        # report（JSON 内容读取，保留原有逻辑）
        try:
            if os.path.exists(report_path):
                with open(report_path, 'r', encoding='utf-8') as f:
                    result['report'] = json.load(f)
                result['files']['report'] = report_path
        except Exception as e:
            result['report_error'] = str(e)

        # strategy / futures 模式：读取 equity csv + trades json
        if mode in ('strategy', 'futures'):
            result['equity'] = []
            try:
                if equity_path and os.path.exists(equity_path):
                    import csv as _csv
                    with open(equity_path, 'r', encoding='utf-8') as f:
                        result['equity'] = list(_csv.DictReader(f))
                    result['files']['equity'] = equity_path
            except Exception as e:
                result['equity_error'] = str(e)
            result['trades'] = []
            try:
                if trades_path and os.path.exists(trades_path):
                    with open(trades_path, 'r', encoding='utf-8') as f:
                        result['trades'] = json.load(f)
                    result['files']['trades'] = trades_path
            except Exception as e:
                result['trades_error'] = str(e)
            # 交易流水（一笔成交一行，人读版）：只登记路径供「打开交易流水CSV」按钮使用，
            # 不下发内容（体积大且前端不渲染该表）
            try:
                fills_path = self._bt_locate_file(f'{prefix}_trades_detail.csv')
                if fills_path:
                    result['files']['fills'] = fills_path
            except Exception:
                pass
        else:
            # scoreic / factoric 模式：读取 data csv（不返回 equity/trades 错误路径）
            result['data'] = []
            try:
                if data_path and os.path.exists(data_path):
                    import csv as _csv
                    with open(data_path, 'r', encoding='utf-8') as f:
                        result['data'] = list(_csv.DictReader(f))
                    result['files']['data'] = data_path
            except Exception as e:
                result['data_error'] = str(e)
            # factoric / futuresic 模式：读取 PNG 图表（base64，供前端直接渲染）
            if mode in ('factoric', 'futuresic'):
                result['chart'] = None
                try:
                    if chart_path and os.path.exists(chart_path):
                        import base64 as _b64
                        with open(chart_path, 'rb') as f:
                            b64 = _b64.b64encode(f.read()).decode('ascii')
                        result['chart'] = f'data:image/png;base64,{b64}'
                        result['files']['chart'] = chart_path
                except Exception as e:
                    result['chart_error'] = str(e)

        # ── 产物新鲜度校验（防止把「上次的旧文件」当成本次结果展示）──
        # 场景：本次写盘失败（典型：CSV 正被 Excel/WPS 打开占用，见
        # engine/backtest_strategy._safe_write_csv），旧文件残留，前端就会画出
        # "旧净值曲线 + 新指标卡"，表现成「收益曲线和实际结果对不上」
        # （2026-09-19 实锤：22:56 那次三个 CSV 全写失败，UI 显示 21:11 的曲线 +46%
        #  配新报告的 +3.21%）。这里按「文件 mtime < 回测启动时间」判定为旧文件。
        _stale = _stale_outputs(result.get('files'), task.get('start_time'))
        result['stale_files'] = _stale
        _exp = (result.get('report') or {}).get('export_errors') or []
        if _exp:
            result['export_errors'] = _exp

        # 模式化摘要（#022）
        result['mode_summary'] = self._build_bt_summary(mode, result.get('report'))
        return result

    @staticmethod
    def _build_bt_summary(mode, report):
        """按回测模式生成差异化摘要文本。"""
        if not report:
            return '运行完成，但无有效结果（可能样本不足）。'
        if report.get('cancelled'):
            return '⏹ 已取消（用户点击「停止」）。本次回测未产生完整结果，可重新运行或改用更小的股票池/年数。'
        if mode == 'strategy':
            c = report.get('compound', {})
            return (f"策略回测完成 | 总收益 {c.get('total_return', 0):+.2f}%  CAGR {c.get('cagr', 0):+.2f}%  "
                    f"最大回撤 {c.get('max_drawdown', 0):.2f}%  Sharpe {c.get('sharpe', 0):.2f}  "
                    f"Calmar {c.get('calmar', 0):.2f}\n"
                    f"交易 {report.get('total_trades', 0)} 笔 | 胜率 {report.get('win_rate', 0):.1f}% | "
                    f"盈亏比 {report.get('profit_loss_ratio', 0)} | 加仓 {report.get('add_triggered', 0)} / "
                    f"减仓 {report.get('reduce_triggered', 0)} / 减仓保护 {report.get('reduce_protected', 0)} / 干净退出 {report.get('clean_exit', 0)}")
        if mode == 'futures':
            return (f"期货策略回测完成 | 总收益 {report.get('total_return_pct', 0):+.2f}%  "
                    f"年化 {report.get('cagr_pct', 0):+.2f}%  最大回撤 {report.get('max_drawdown_pct', 0):.2f}%  "
                    f"Sharpe {report.get('sharpe_ratio', 0):.2f}  Calmar {report.get('calmar_ratio', 0):.2f}\n"
                    f"交易 {report.get('total_trades', 0)} 笔 (多{report.get('long_trades', 0)}/空{report.get('short_trades', 0)}) | "
                    f"胜率 {report.get('win_rate', 0):.1f}% | 平均盈亏(保) {report.get('avg_pnl_pct', 0):.2f}% | "
                    f"总盈亏 {report.get('total_pnl', 0):,.0f} | 手续费 {report.get('total_commission', 0):,.0f}")
        if mode == 'scoreic':
            return (f"评分IC回测完成 | 样本 {report.get('total_samples', 0)} | "
                    f"相关性 {report.get('correlation', 0):.4f} | Rank-IC {report.get('avg_rank_ic', 0):.4f}  "
                    f"IC_std {report.get('ic_std', 0):.4f}\n"
                    f"强势收益 {report.get('strong_group_return', 0):.2f}%  弱势收益 {report.get('weak_group_return', 0):.2f}%  "
                    f"收益差 {report.get('return_diff', 0):.2f}%")
        # factoric / futuresic
        rows = report.get('factors', [])
        _ic_label = '期货因子IC回测完成' if mode == 'futuresic' else '因子IC回测完成'
        if not rows:
            return f'{_ic_label}，但无有效因子（样本不足）。'
        top = rows[:5]
        lines = [f"{_ic_label} | {report.get('n_factors', 0)} 个因子 | {report.get('sample_dates', 0)} 采样点 | 预测 {report.get('hold_days', 10)}日"]
        for r in top:
            lines.append(f"  {r.get('label', '')}({r.get('factor', '')}): Rank-IC={r.get('rank_ic', 0):+.4f}  IC_IR={r.get('ic_ir', 0):+.2f}  N={r.get('n_samples', 0)}")
        return '\n'.join(lines)

    def cancel_backtest(self, task_id):
        """终止子进程。"""
        task = self._backtest_tasks.get(task_id)
        if not task:
            return {'success': False, 'error': '无效的 task_id'}
        try:
            task['launcher'].stop()
            task['launcher'].kill_tree()
            task['done'] = True
            return {'success': True}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def apply_factor_ic_result(self):
        """将最近一次「③ 因子IC回测」报告的 mean/std/IC/IC_IR 写回当前方案。

        读 bt_factoric_report.json 的 factor_stats → save_factor_stats → 强制重载。
        返回 {success, message, updated, missing}。
        """
        try:
            candidates = [
                self._bt_locate_file('bt_factoric_report.json'),
                os.path.join(get_app_dir(), 'reports', 'bt_factoric_report.json'),
                os.path.join(get_app_dir(), 'bt_factoric_report.json'),
                os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             'bt_factoric_report.json'),
            ]
            rep = None
            found = None
            for p in candidates:
                if p and os.path.exists(p):
                    with open(p, 'r', encoding='utf-8') as f:
                        rep = json.load(f)
                    found = p
                    break
            if rep is None:
                return {'success': False, 'error': '未找到因子IC报告，请先运行「③ 因子IC回测」'}
            stats = rep.get('factor_stats')
            if not stats:
                return {'success': False, 'error': '报告中无因子统计，无法更新'}

            try:
                cur_name = quant_config.get_current_scheme_name()
                fcs = quant_config.get_factor_configs()
            except Exception:
                cur_name, fcs = None, {}
            if not fcs:
                return {'success': False,
                        'error': f'当前方案「{cur_name or "无"}」尚未配置任何因子，无法写入 IC 统计。'
                                 f'请先到「因子管理」勾选因子并保存方案。'}

            ok, n = quant_config.save_factor_stats(stats)
            # 写盘失败单独分支（避免 n 语义混淆）：前端失败分支读 r.error（index.html:7589）
            if not ok:
                return {'success': False, 'updated': 0,
                        'error': '因子统计写盘失败（quant_model.json 写入被拒），当前方案未更新。'}
            quant_config.load_config(force_reload=True)
            if n > 0:
                active_names = set(quant_config.get_active_factors() or [])
                missing = sorted(set(stats.keys()) - active_names)
                msg = f'已将 {n} 个已启用因子的 mean/std/IC/IC_IR 写入当前方案'
                if missing:
                    msg += (f'；报告中另有 {len(missing)} 个因子未加入当前方案'
                            f'（{", ".join(missing[:5])}{" 等" if len(missing) > 5 else ""}），'
                            f'如需使用请先在「因子管理」勾选保存')
                msg += '。请到「权重 & 方向」Tab 点击「按IC加权」按钮调整权重'
                return {'success': True, 'message': msg, 'updated': n, 'missing': missing}
            return {'success': False, 'updated': 0,
                    'error': f'报告中无与当前方案因子匹配的统计（报告 {len(stats)} 个 / 方案 {len(fcs)} 个），未更新。'}
        except Exception as e:
            logger.exception("apply_factor_ic_result 失败")
            return {'success': False, 'error': str(e)}

    def open_backtest_file(self, task_id, key):
        """用系统默认程序打开回测产物文件。key: report | equity | trades | chart | data。
        模式感知（不同模式产出不同文件，
        文件名与 get_backtest_result 保持一致）。

        2026-09-19 调整（用户要求「不要打开 .json，改成打开 .csv」）：
          - trades：直接指向引擎已产出的 {prefix}_trades.csv（不再开 _trades.json）
          - report：报告仅 JSON 产物，按需转成 {prefix}_report.csv 两列 CSV 后再打开
        """
        task = self._backtest_tasks.get(task_id)
        if not task:
            return {'success': False, 'error': '无效的 task_id'}
        try:
            from engine.config import get_app_dir
            prefix = task.get('prefix', 'bt')
            mode = task.get('mode', 'strategy')
            hold = task.get('hold') or 30
            # 模式感知文件映射（统一 CSV；文件名与 get_backtest_result 对齐）
            if mode == 'scoreic':
                name_map = {
                    'report': f'{prefix}_report.csv',
                    'data': f'{prefix}_decile_equity_h{hold}.csv',
                }
            elif mode == 'factoric':
                name_map = {
                    'report': f'{prefix}_report.csv',
                    'data': f'{prefix}.csv',
                    'chart': f'{prefix}.png',
                }
            else:  # strategy / futures
                name_map = {
                    'report': f'{prefix}_report.csv',
                    'equity': f'{prefix}_equity.csv',
                    'data': f'{prefix}_equity.csv',
                    'trades': f'{prefix}_trades.csv',
                    'fills': f'{prefix}_trades_detail.csv',
                }
            fname = name_map.get(key)
            if not fname:
                return {'success': False, 'error': f'不支持的文件类型: {key}（模式: {mode}）'}
            path = self._bt_locate_file(fname)
            # 报告只有 JSON 产物：就地转成两列 CSV（指标,值）再打开
            if not path and key == 'report':
                jpath = self._bt_locate_file(f'{prefix}_report.json')
                if jpath:
                    try:
                        csv_path = os.path.join(os.path.dirname(jpath), fname)
                        _report_json_to_csv(jpath, csv_path)
                        path = csv_path
                    except Exception as e:
                        logger.warning('report JSON→CSV 转换失败: %s', e)
                        path = None
            if not path:
                return {'success': False, 'error': f'文件不存在: {fname}'}
            # 无默认关联程序（.json 常见）时回退到 notepad / mspaint / 资源管理器定位，
            # 避免直接抛 WinError -2147221003「找不到应用程序」。
            ok, how = _open_path_smart(path)
            return {'success': bool(ok), 'path': path, 'via': how}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    # ═══════════════════════════════════════════════════════════
    # 关于/帮助
    # ═══════════════════════════════════════════════════════════
    def _js_error(self, info):
        """前端 JS 异常上报，写入 logs/js_errors.log。"""
        try:
            log_path = os.path.join(get_app_dir(), 'logs', 'js_errors.log')
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, 'a', encoding='utf-8') as f:
                ts = datetime.now().isoformat()
                f.write(f"\n=== JS ERROR {ts} ===\n")
                f.write(json.dumps(info, ensure_ascii=False, indent=2))
                f.write("\n")
        except Exception:
            pass
        return True

    def get_js_error_log(self):
        """返回前端报错日志内容，方便用户复制。"""
        try:
            log_path = os.path.join(get_app_dir(), 'logs', 'js_errors.log')
            if not os.path.exists(log_path):
                return '暂无报错日志。'
            with open(log_path, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception as e:
            return f'读取日志失败: {e}'

    def get_error_log(self):
        """返回 Python 统一错误日志（M-Bull_error.log）内容，含所有后端报错。"""
        try:
            from engine.error_log import get_error_log_path
            path = get_error_log_path()
            if not os.path.exists(path):
                return '暂无错误日志。（首次启动或未触发任何 WARNING+ / 异常 / warnings）'
            # 只读最后 200KB（防止超大文件撑爆前端）
            max_bytes = 200 * 1024
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                f.seek(0, 2)
                size = f.tell()
                if size > max_bytes:
                    f.seek(size - max_bytes)
                    f.readline()  # 跳过可能截断的首行
                    content = f.read()
                    return f'[... 日志过大，仅显示最后 {max_bytes//1024}KB ...]\n\n{content}'
                else:
                    f.seek(0)
                    return f.read()
        except Exception as e:
            return f'读取错误日志失败: {e}'

    def get_app_info(self):
        """返回 {version, license_status, machine_id}。"""
        try:
            info = get_license_info()
            return {
                'version': 'v2.0',
                'license_status': info.get('status', 'unknown'),
                'license_text': get_status_text(),
                'machine_id': get_machine_id(),
            }
        except Exception as e:
            return {'version': 'v2.0', 'license_status': 'error', 'error': str(e)}
