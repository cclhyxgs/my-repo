# -*- coding: utf-8 -*-
"""engine_bridge —— engine/* 的唯一 import 收口（§2.8 / T8）。

设计要点：
1. engine 是**纯同步 Python**，按 §2.8「直接 import，不做服务化包装」处理。
2. **engine 一行不改**：应用目录只能靠 `QUANT_SYSTEM_DIR` 注入（补充约束 1）。
3. 启动自检结果作为 `/api/health` 的硬断言输入；导入失败**不吞**，由 health 返回 503。
"""

import copy
import importlib
import os
import sys
import threading
from pathlib import Path

from server import settings
from server.core.logging import get_logger

logger = get_logger(__name__)

SELFCHECK_MODULES = (
    "engine",
    "engine.factor_registry",
    "engine.score_calculator_v2",
    "engine.report_builder",
)
GUARD_FILE = ".skeleton-guard"
GUARD_NOTE = (
    "M-Bull H5 服务端骨架占位文件。\n"
    "用途：让应用目录在 engine 导入前即为『非空』，从而跳过\n"
    "engine/config.py:_migrate_legacy_app_dir() 的迁移分支——该分支在\n"
    "『目标目录为空 且 %LOCALAPPDATA%/QuantSystem 存在』时会 copytree 后\n"
    "shutil.rmtree 掉旧目录（engine/config.py:137-186）。\n"
)

_lock = threading.Lock()
_cache: dict | None = None


def _ensure_project_root_on_path() -> str:
    """保证 `import engine.*` 可用（开发态从项目根启动；容器内 /app 即项目根）。"""
    root = str(settings.PROJECT_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    return root


def _ensure_quant_system_dir() -> Path:
    """把 QUANT_SYSTEM_DIR 注入 engine（唯一注入口），并预写哨兵文件。"""
    target = settings.resolve_quant_system_dir()
    try:
        empty = (not target.exists()) or (not any(target.iterdir()))
    except OSError:
        empty = True
    if empty:
        target.mkdir(parents=True, exist_ok=True)
        guard = target / GUARD_FILE
        if not guard.exists():
            guard.write_text(GUARD_NOTE, encoding="utf-8")
    os.environ[settings.QUANT_SYSTEM_DIR_ENV] = str(target)
    return target


def engine_selfcheck(force: bool = False) -> dict:
    """导入 engine 并汇报关键事实。结果缓存；失败也缓存（force=True 可重试）。"""
    global _cache
    with _lock:
        if _cache is not None and not force:
            return _cache

        result = {
            "importable": False,
            "error": None,
            "factors": 0,
            "factor_sample": [],
            "py_modules": 0,
            "app_dir": None,
            "project_root": str(settings.PROJECT_ROOT),
        }
        try:
            _ensure_project_root_on_path()
            app_dir = _ensure_quant_system_dir()
            result["app_dir"] = str(app_dir)

            for module_name in SELFCHECK_MODULES:
                importlib.import_module(module_name)

            registry = sys.modules["engine.factor_registry"]
            factors = registry.get_all_factors()
            # 健康口径为内置因子数（21）：自定义公式因子是方案级动态注册项，不计入骨架契约
            builtin = [n for n in factors if not registry.is_custom_factor(n)]
            engine_pkg = sys.modules["engine"]

            result["importable"] = True
            result["factors"] = len(builtin)
            result["factor_sample"] = sorted(builtin)[:5]
            result["py_modules"] = len(list(Path(engine_pkg.__file__).resolve().parent.rglob("*.py")))
        except Exception as exc:  # 不吞：记录完整信息，交给 health 判 503
            result["error"] = f"{type(exc).__name__}: {exc}"
            logger.critical("engine 导入自检失败：%s", result["error"], exc_info=exc)

        _cache = result
        return result


def get_factor_registry():
    """因子注册表模块（供后续 F-xxx 复用；本次无业务调用方）。"""
    importlib.import_module("engine.factor_registry")
    return sys.modules["engine.factor_registry"]


def list_factor_names() -> list:
    return sorted(get_factor_registry().get_all_factors())


# ============================================================
# 取值门面（F-203 能力 1 消费面）
# ------------------------------------------------------------
# 说明：T8 规定 engine 只允许在本文件被引用，故所有 engine 取值入口都在此
# 收口为薄访问器；业务层（server/adapters/market/*）只调这些访问器。
# 一律走 importlib 在**调用时**导入，避免 bridge 模块导入即触发 engine 副作用。
# ============================================================

def _module(name: str):
    return importlib.import_module(name)


def data_api():
    """A股 K线入口：`engine.data_layer.DataAPI`（内部走可插拔数据源 registry）。"""
    return _module("engine.data_layer").DataAPI


def kline_period_sources() -> dict:
    """A股 {周期: 源标识|None} —— **权威支持集**。

    由 `engine.data_layer.KLineFetcher._load_kline_sources()` 读
    `config/data_sources.json` 的 `kline_source` 得出：
      通达信源（tdx，**默认**）→ 7 个周期全部有效
        （日K=tdx_qfq 自算前复权 / 周K=tdx_weekly / 分钟=tdx_minute 未复权）
      腾讯源（tencent）→ 日K/周K 有效，分钟级为 None（不可用）
      新浪源（sina）→ 7 个周期全部有效（均为未复权）
    服务端据此判定周期是否受支持，不另立一套硬编码表。
    ⛔ 新增数据源时只需在 `_load_kline_sources()` 登记，本函数与调用方自动跟随。
    """
    return dict(_module("engine.data_layer").KLineFetcher._load_kline_sources())


def qfq_daily_disk_path(code: str) -> str:
    """前复权日线磁盘 store 路径（`cache/kline/qfq_daily_{code}.pkl`）。"""
    return _module("engine.state").get_qfq_daily_disk_path(code)


def resolve_futures_contract(code: str):
    """解析期货合约查询串，返回 (contract, month)。

    兼容两种记法（B10）：UI 形态 `'rb'` / `'jd2609'`，以及数据层「主力连续」记法
    `'rb0'`（`engine/futures_data.py:133 _is_continuous_contract` 认 `rb0/TA0/cu0`，
    也是 §3.2 `/api/futures/contracts?symbol=rb0` 与 F-203 验收标准的写法）。
    """
    pool = _module("engine.futures_pool")
    code = (code or "").strip()
    contract, month = pool.parse_contract_code(code)
    if contract is None and len(code) > 1 and code[-1] == "0":
        alt, alt_month = pool.parse_contract_code(code[:-1])
        if alt is not None and not alt_month:
            return alt, None
    return contract, month


def futures_spec(code: str):
    """期货合约规格对象（含 `.symbol` / `.secid`）；无法解析返回 None。"""
    contract, month = resolve_futures_contract(code)
    if contract is None:
        return None
    if month:
        specs = _module("engine.futures_pool").make_specific_contracts([contract], month)
        return specs[0] if specs else None
    return contract


def futures_kline_source() -> str:
    """期货 K线当前生效源名（`eastmoney` / `sina`），用于响应 source 字段。"""
    try:
        return str(_module("engine.futures_data")._get_futures_kline_source())
    except Exception:
        return "unknown"


def fetch_futures_daily(symbol: str, secid: str, days: int):
    return _module("engine.futures_data").fetch_futures_daily(symbol, secid, days=days)


def fetch_futures_minute(symbol: str, secid: str, period: int, days: int):
    return _module("engine.futures_data").fetch_futures_minute(symbol, secid, period=period, days=days)


def merge_night_session(df, symbol: str, secid: str):
    """夜盘聚合成"下一交易日夜盘"K线（F-203 验收②；逻辑已于 B7 上移至 engine）。"""
    return _module("engine.futures_night").merge_night_session(df, symbol, secid)


# ------------------------------------------------------------
# 股票池访问器（F-204 搜索消费面）
# ------------------------------------------------------------

def load_stock_list() -> int:
    """加载全市场股票列表到 `engine.state`（缓存优先），返回条数（0=失败）。"""
    return _module("engine.data_layer").DataAPI.load_stock_list()


def _state():
    """`engine.state.state` —— 注意 `state` 是**模块内的实例**（`from engine.state import state`），
    池映射挂在该实例上（`data_layer.py:192 state.name_to_code = ...`），非模块级变量。"""
    return _module("engine.state").state


def stock_name_to_code() -> dict:
    """`{名称: 代码}` 池映射。"""
    return dict(getattr(_state(), "name_to_code", None) or {})


def stock_code_to_name() -> dict:
    """`{代码: 名称}` 池映射。"""
    return dict(getattr(_state(), "code_to_name", None) or {})


def stock_name_of(code: str):
    """按代码取名称；未知返回 None（`DataAPI.get_stock_name`）。"""
    try:
        return _module("engine.data_layer").DataAPI.get_stock_name(code)
    except Exception:
        return None


# ------------------------------------------------------------
# 指数 / 市场宽度访问器（F-201 消费面）
# ------------------------------------------------------------

def index_daily_return(code: str):
    """指数当日涨跌幅 → `(友好名, 小数 或 None)`（`IndexFetcher.get_daily_return`）。"""
    return _module("engine.data_layer").IndexFetcher.get_daily_return(code)


def index_name_of(code: str) -> str:
    """指数友好名（本地常量表，不触网）。"""
    return _module("engine.data_layer").IndexFetcher.name_of(code)


def market_breadth():
    """涨跌家数 → `(up, down, source)`（`DataAPI.get_market_breadth`）。"""
    return _module("engine.data_layer").DataAPI.get_market_breadth()


# ------------------------------------------------------------
# 实时行情访问器（F-202 消费面）
# ------------------------------------------------------------

def realtime_quote(code: str):
    """实时行情 → `(fields, source)`；失败 `(None, None)`。

    engine 已把腾讯源**手工重排**到与新浪相同的归一布局（`RealtimeQuoteFetcher.fetch_tencent`），
    故此处拿到的是统一位序：0=name 1=open 2=prev_close 3=price 4=high 5=low
    6=买一 7=卖一 8=volume 9=amount 30=date 31=time。
    取数策略：新浪优先 → 腾讯兜底。
    """
    return _module("engine.data_layer").DataAPI.get_realtime_quote(code)


# ------------------------------------------------------------
# 分析流程访问器（F-301 消费面）
# ------------------------------------------------------------

def analyze_stock(code: str, *, k_type: str = '日K', scheme_name=None, **kwargs) -> dict:
    """A 股分析核心（`engine.analyze_service.analyze_stock`）。

    该函数 2026-09-12 由 `ui/web_api.py::WebAPI.analyze()` 主体上移（B27 方案 1），
    与桌面共用同一实现，且函数体内不读任何进程级全局态（R-09）。

    ⚠️ 服务端**不传** `authorize` / `is_basic_mode` / `basic_preview` / `on_quota` 四个钩子：
    授权准入已按 B25 单列，初级模式属 F-604，额度按 `F#4` 订阅制退役，纪律落册属 F-1103。
    """
    return _module("engine.analyze_service").analyze_stock(
        code, k_type=k_type, scheme_name=scheme_name, **kwargs
    )


def analyze_futures(symbol: str, *, k_type: str = '日K', scheme_name=None, now=None, **kwargs) -> dict:
    """期货分析核心（`engine.analyze_service.analyze_futures`）。

    2026-09-12 由 `ui/web_api.py::WebAPI._analyze_futures()` 上移（B29 方案 1）。

    ⚠️ `now` 必须由服务端注入 `server.core.time.now()`（Asia/Shanghai，§2.13-2）：
    过期合约判定（`YYMM < 当前 YYMM`）依赖服务端时区，而 engine 不得反向依赖 server 的时区模块。
    """
    return _module("engine.analyze_service").analyze_futures(
        symbol, k_type=k_type, scheme_name=scheme_name, now=now, **kwargs
    )

# ------------------------------------------------------------
# 期货搜索 / 期货行情条访问器（B14 / B17 清理）
# ------------------------------------------------------------

def futures_search(query: str) -> list:
    """期货品种搜索（`engine.futures_search.search_futures`）。

    2026-09-13 由 `ui/web_api.py::WebAPI.search_futures` 上移（B14 清理）。
    返回 `[{name, code, symbol, exchange, multiplier, contract_month}]`。
    """
    return _module("engine.futures_search").search_futures(query)


def futures_indices() -> dict:
    """期货行情条（`engine.futures_indices.get_futures_indices`）。

    2026-09-13 由 `ui/web_api.py::WebAPI._get_futures_indices` 上移（B17 清理）。
    返回桌面结构 `{'indices': [...], 'up': None, 'down': None, 'source': 'futures'}`，
    服务端 adapter 层再映射为 §3.2 契约（advance/decline/ts）。
    """
    return _module("engine.futures_indices").get_futures_indices()

def parse_watchlist(text: str, market: str) -> list:
    """自选列表解析（`engine.watchlist_parser.parse_watchlist`）。

    2026-09-13 由 `ui/web_api.py::WebAPI._parse_watchlist` 上移（F-401），
    `self._market_type` 全局态改为显式 `market` 参数（R-09 逐个拔除）。
    返回 `[{code,name,entry_price,bars_held,direction}]`。
    """
    return _module("engine.watchlist_parser").parse_watchlist(text, market)

def classify_report(report_data: dict) -> str:
    """报告五组分类（`engine.report_classify._classify_for_report`）。

    2026-09-13 由 `ui/report_classify.py::_classify_for_report` 上移（F-402）。
    返回 error/position/buy/watch/avoid 之一。
    """
    return _module("engine.report_classify")._classify_for_report(report_data)


# ------------------------------------------------------------
# 全市场扫描访问器（F-501~506 消费面；mirror 诊断分析访问器）
# ------------------------------------------------------------

def scan_load_scheme(market: str, direction: str = "long", period: str = "日K", scheme_name=None) -> None:
    """扫描前加载方案到 engine（与桌面 `start_market_scan` 同路径）。

    ⚠️ 直接写 `quant_config._cache` 全局态（单副本 R-09 前提）；扩副本前须外置。
    参数与桌面 `engine.quant_config.load_market_scheme` 一致。
    """
    _module("engine.quant_config").load_market_scheme(
        market, direction, period, force_reload=True, scheme_name=scheme_name
    )


def scan_stock_codes() -> list:
    """全市场股票池代码列表（扫描对象，来源 `state.code_to_name` 的键）。

    先 `load_stock_list()`（缓存优先，条数达标且 saved_at≤1 天即不发网络请求）。
    返回裸代码列表（含 sh/sz 前缀，如 `sh600519`）——`_quick_score_core` 内部会剥前缀查板块。
    """
    load_stock_list()
    return list(getattr(_state(), "code_to_name", None) or {}).keys()


def scan_build_sector_map() -> dict:
    """板块映射：`{裸码: 板块名}`，合并优先级 **用户映射 > 内置 26 板块**（F#7）。

    - 内置：bundled 种子 `engine/config/sector_map.json`（格式 `{板块:[codes]}`）。
    - 用户：`QUANT_SYSTEM_DIR/config/sector_map.json`，存在则覆盖同名裸码（按账号隔离由
      目录隔离天然保证；MVP 单文件即可）。
    """
    import json as _json

    merged: dict = {}
    # 内置种子
    try:
        eng_pkg = _module("engine")
        builtin_path = os.path.join(os.path.dirname(eng_pkg.__file__), "config", "sector_map.json")
        if os.path.exists(builtin_path):
            with open(builtin_path, "r", encoding="utf-8") as f:
                data = _json.load(f)
            for sec, codes in data.items():
                for c in (codes or []):
                    merged[str(c)] = sec
    except Exception:
        pass
    # 用户覆盖
    try:
        user_path = os.path.join(settings.resolve_quant_system_dir(), "config", "sector_map.json")
        if os.path.exists(user_path):
            with open(user_path, "r", encoding="utf-8") as f:
                data = _json.load(f)
            for sec, codes in data.items():
                for c in (codes or []):
                    merged[str(c)] = sec  # 用户 > 内置
    except Exception:
        pass
    return merged


def scan_score_one(code: str, sector_map: dict, direction: str = "long", period: str = "日K", up_ratio: float = 0.5) -> dict:
    """单只评分（`engine.market_scan_core._quick_score_core` 薄委托）。

    返回结果 dict（含 code/name/price/final_score/tech_strength/stars/level/sector/entry_tier/tech_snapshot）
    或 None（数据不足/异常，由内核静默跳过）。
    """
    return _module("engine.market_scan_core")._quick_score_core(
        code, up_ratio, sector_map, direction=direction, period=period
    )


def scan_chunk_score(codes, sector_map: dict, direction: str = "long", period: str = "日K", up_ratio: float = 0.5) -> list:
    """分片评分（`engine.market_scan_core._scan_worker` 薄委托）。

    返回该分片的有效结果列表（内核已过滤 None）。
    """
    return _module("engine.market_scan_core")._scan_worker(
        codes, up_ratio, sector_map, direction=direction, period=period
    )


def quant_model_snapshot() -> dict:
    """engine 当前量化模型快照（`quant_model.json` 形状，F-601 首次 seed 用）。

    返回 `{"schemes": {...}, "factor_profiles": {...}}` 的**深拷贝**——engine 的
    `get_schemes()` 返回的是模块内部 `_MODEL` 的活引用，直接外传会被调用方改坏。
    文件缺失/损坏时 engine 返回空 dict，本函数同样返回空结构（不报错）。
    """
    qc = _module("engine.quant_config")
    qc.load_config(force_reload=True)
    schemes = qc.get_schemes() or {}
    profiles = qc.get_factor_profiles() or {}
    return {
        "schemes": copy.deepcopy(schemes),
        "factor_profiles": copy.deepcopy(profiles),
    }
