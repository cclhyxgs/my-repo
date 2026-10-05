#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""量化模型配置加载器 v2 - 纯用户自定义 / 无内置方案。

设计原则（2026-07-29 落地）：
- 不内置任何方案：load_config 无文件 / 空 schemes / 损坏 → 返回 None（待配置态）。
- 纯去默认：所有子参数组在空白种子里都是空 `{}`，引擎不回退内置默认；
  用户未配置的项保持 None，由对应功能入口的 `require_config` 自检拦截。
- 门禁已移除：不再有中央 `_scheme_incomplete` / `requires_scheme_setup`。
  每个功能只在自己的入口声明所需核心配置；核心配置缺失时引擎自然抛
  `ConfigIncompleteError`（或由运行入口捕获并提示「XX 需要配置」）。
  改一个功能逻辑只动它自己的入口，不波及别处（满足「改一功能不波及其他」）。

合规：规则 authorship 归用户；本报告/引擎只比对「用户配置 vs 行情触发状态」，
不输出买卖/目标价建议。
"""

import json
import os
import copy
import logging
import math

from engine.config import CONFIG_DIR

# 否决项枚举由 veto_registry 注册表派生（可插拔：新增否决项=注册表加一条，
# 此处自动获得 keys/labels，无需手工维护集合）。
from engine import veto_registry

logger = logging.getLogger(__name__)

QUANT_MODEL_FILE = os.path.join(CONFIG_DIR, 'quant_model.json')

# ============================================================
# 否决项（极端过热/破位）— 强/标/探/望 档使用，博反弹/恐慌档不使用
# ============================================================
# 否决项集合：由 veto_registry.VETO_REGISTRY 派生（多头/空头按 direction 分发）。
# 新增否决项只需在 veto_registry 加一条 VetoDef，无需改这里。
VETO_KEYS = veto_registry.get_veto_keys('long')
VETO_FLAG_LABELS = veto_registry.get_veto_labels('long')

# 空头专属否决项（镜像多头集，语义相反：多头「过热/破位=否决买入」→
# 空头「超卖/突破=否决做空」）。仅空单模式(direction='short')使用。
SHORT_VETO_KEYS = veto_registry.get_veto_keys('short')
SHORT_VETO_FLAG_LABELS = veto_registry.get_veto_labels('short')

# 否决项集合按方向区分：多头模式用 VETO_KEYS；空单模式(direction='short')
# 用 SHORT_VETO_KEYS（语义镜像）。各入场档在给定方向下共享同一否决集。

def veto_keys_for_level(level_key, direction='long'):
    """返回该入场档在指定方向下应当生效的否决项键集合。

    direction='short' 时返回空头专属否决集（语义镜像多头集），
    避免空单模式下把「高位死叉/跌破年线」等做空良机误判为否决。
    """
    if direction == 'short':
        return set(SHORT_VETO_KEYS)
    return set(VETO_KEYS)

# ============================================================
# 子系统开关（默认全部关闭；注意：enabled 字典当前由 UI 写入才生效，
# 引擎只读 cfg['enabled']，未写入时为全 False —— 配置即启用是更稳健的口径，
# 可选子系统 getter 在配置存在时直接返回，不再依赖此开关门控）
# ============================================================
DEFAULT_ENABLED = {
    'tech_resonance': False,
    'ghost_rules': False,
    'add_engine': False,
    'reduce_engine': False,
    'entry_conditions': False,
    'market_gate': False,
}

# ============================================================
# 市场门控（环境参考系数）— 单一数据源
# ============================================================
MARKET_GATE_CONFIG = [
    {'min': 0.00, 'max': 0.20, 'factor': 0.60, 'limit': 0.20,
     'label': '极端恐慌', 'desc': '系统性风险，仅轻仓试探'},
    {'min': 0.20, 'max': 0.35, 'factor': 0.75, 'limit': 0.35,
     'label': '恐慌', 'desc': '观望为主，严格控制仓位'},
    {'min': 0.35, 'max': 0.50, 'factor': 0.90, 'limit': 0.50,
     'label': '弱势', 'desc': '中性偏谨慎，仓位参考下调'},
    {'min': 0.50, 'max': 0.65, 'factor': 1.00, 'limit': 0.70,
     'label': '中性', 'desc': '常态仓位'},
    {'min': 0.65, 'max': 1.01, 'factor': 1.10, 'limit': 0.90,
     'label': '强势', 'desc': '情绪活跃，可积极配置'},
]

# ============================================================
# UI 预填模板（仅 DEFAULT_ENTRY_CONDITIONS / DEFAULT_GHOST_RULES 被对话框当初始值）
# 纯去默认：引擎 getter 不回退任何默认；其余历史 DEFAULT_* 已于 2026-07-29 清理（零引用）。
# ============================================================

DEFAULT_ENTRY_CONDITIONS = {
    'strong': {'veto_on': True, 'veto_enabled': {k: True for k in VETO_KEYS}, 'tech_signal': 'bull_align'},
    'standard': {'veto_on': True, 'veto_enabled': {k: True for k in VETO_KEYS}, 'tech_signal': 'bull_align'},
    'test': {'veto_on': True, 'veto_enabled': {k: True for k in VETO_KEYS}, 'tech_signal': 'above_ma20'},
    'pending': {'veto_on': True, 'veto_enabled': {k: True for k in VETO_KEYS}, 'tech_signal': 'none'},
    'rebound': {'veto_on': True, 'veto_enabled': {k: False for k in VETO_KEYS}, 'tech_signal': 'none'},
    'panic_rebound': {'veto_on': True, 'veto_enabled': {k: False for k in VETO_KEYS}, 'tech_signal': 'none'},
    'top_reversal': {'veto_on': True, 'veto_enabled': {k: False for k in SHORT_VETO_KEYS}, 'tech_signal': 'none'},
}

DEFAULT_GHOST_RULES = {
    'grace_period_days': 3,
    'profit_threshold': 0.5,
    'add_profit_threshold': 3,
    'rsi_confirm': 45,
    'ma_confirm': 'sma_20',
}

# ============================================================
# 模块级状态
# ============================================================
_MODEL = None          # 完整文件 dict：{'schemes': {...}, 'current_scheme': name}
_cache = None          # 当前激活方案的 config dict（经 _validate_config）
_CURRENT = None        # 当前方案名

# ============================================================
# 错误类型 / 局部自检（替代原中央门禁）
# ============================================================
# ───────────────────────────────────────────────────────────
# 配置完整性统一常量（SSOT：所有前端 Banner/回测/诊断/扫描入口 MUST 用同一份标准）
# ───────────────────────────────────────────────────────────
# 核心必填：没有就完全跑不起来——引擎评分/进场/加减仓 直接 KeyError 或无输出。
REQUIRED_CORE = [
    ('因子配置', 'factor_configs'),
    ('评分缩放', 'score_scale'),
    ('状态阈值', 'thresholds'),
    ('进场触发', 'entry_conditions'),
    ('建仓参数', 'entry_params'),
    ('加仓参数', 'add_params'),
    ('减仓参数', 'reduce_params'),
]
# 可选项（留空 = 不启用，引擎跳过）：冲突惩罚 / 风控（个股仓位上限/止损等）/
# 技术共振 / 幽灵规则。之前回测入口把 conflict_penalty 列必填是 Bug，导致
# 「Banner 显示✓完整但回测红字报冲突惩罚缺失」的矛盾症状。
OPTIONAL_MODULES = ('conflict_penalty', 'risk_params', 'tech_resonance', 'ghost_rules')
# 快捷：完整方案的官方统一 required（所有入口直接引用，保证 Banner=回测=诊断=扫描 一致）
REQUIRED_STRATEGY = list(REQUIRED_CORE)


class ConfigIncompleteError(Exception):
    """配置不完整异常。

    由对应功能在入口「自检」后抛出（见 require_config）；运行入口捕获后
    提示用户「XX 需要配置」。各功能只声明自己所需配置，互不牵连——
    缺失时引擎直接抛 ConfigIncompleteError（或自然 KeyError），由运行该功能处捕获并提示用户补全。
    """

    def __init__(self, missing):
        self.missing = list(missing) if missing else []
        super().__init__('配置不完整，缺少：' + '、'.join(self.missing))

def require_config(cfg, required):
    """功能入口自检：required 中任一配置缺失/为空则抛 ConfigIncompleteError。

    Args:
        cfg: 当前方案配置 dict（通常用 _safe_cfg() 传入）。
        required: list of (中文名, 点号路径)，例如
            [('因子配置(factor_configs)', 'factor_configs'),
             ('评分缩放(score_scale)', 'score_scale')]
            点号路径支持嵌套，如 'market_gate.envs'。
            空 dict / 空 list 视为未配置（核心项必须非空）。
    """
    if cfg is None:
        raise ConfigIncompleteError([name for name, _ in required])
    miss = []
    for name, path in required:
        cur = cfg
        found = True
        for part in path.split('.'):
            if isinstance(cur, dict) and part in cur and cur[part] is not None:
                cur = cur[part]
            else:
                found = False
                break
        if not found or (isinstance(cur, (dict, list)) and len(cur) == 0):
            miss.append(name)
    if miss:
        raise ConfigIncompleteError(miss)

def _safe_cfg():
    """返回当前配置；无激活方案时返回空白种子（供 require_config 判定待配置态）。"""
    return _cache if isinstance(_cache, dict) else _build_blank_config()

# ============================================================
# 空白种子 / 校验
# ============================================================
def _build_blank_config():
    """空白种子：所有子参数组均为空 {}（纯去默认，不填任何 DEFAULT_*）。

    用户必须自行创建并配置方案（见 get_blank_config；各功能入口 require_config 自检所需配置）。
    """
    return {
        'active_factors': [],
        'factor_configs': {},
        'score_scale': {},
        'thresholds': {},
        'entry_params': {},
        'entry_conditions': {},
        'conflict_penalty': {},
        'tech_resonance': {},
        'ghost_rules': {},
        'add_params': {},
        'reduce_params': {},
        'risk_params': {},
        'market_gate': {},
        # 全市场扫描「技术指标二级过滤」条件（AND 组合，随方案保存）。
        # ⚠️ 这个键曾长期缺席种子：_validate_config 只遍历种子键做骨架，
        # 于是方案里明明存了条件却被静默丢弃 → get_scan_tech_filter() 恒为 None
        # → 「应用筛选」点了完全没反应（2026-09-17 用户实报）。
        'scan_tech_filter': [],
        'enabled': dict(DEFAULT_ENABLED),
    }

def _build_basic_default_config():
    """初级方案种子：空白骨架 + 建仓档位默认值（「开箱即用」）。

    背景：初级用法定位「因子休眠、打开即用」，但 TradingPipeline.execute 无条件
    require_config(entry_params)（无模式豁免），于是「切初级 → 新建方案 → 个股分析」
    必报「配置不完整: 建仓参数(entry_params)」——新建方案的 entry_params 是空 {}。
    初级仍按「强势/标准/试探/观望」技术条件定档 → 各档买多少必须由用户/默认值给出，
    故为初级方案预置一套默认档位（用户可随时在「仓位管理」改）。

    ⛔ 默认值必须与 ui/config_mapper._default_quant_data['entryPos'] 保持一致
    （单位：0-1 小数，0.20 = 20% 仓位）；改一处要改两处。
    """
    cfg = _build_blank_config()
    cfg['entry_params'] = {
        'positions': {
            'strong': 0.20, 'standard': 0.15, 'test': 0.08, 'observe': 0.03,
            'none': 0.0, 'rebound': 0.05, 'panic_rebound': 0.02, 'top_reversal': 0.05,
        },
        'reversal_score_threshold': 4.0,
        'panic_reversal_threshold': 5.0,
    }
    return cfg


def _seed_config_for(meta=None, config=None):
    """新建方案的 config 种子：显式 config 优先；初级方案用带默认档位的种子；其余空白。"""
    if isinstance(config, dict):
        return config
    if isinstance(meta, dict) and meta.get('mode') == 'basic':
        return _build_basic_default_config()
    return _build_blank_config()


def _validate_config(raw):
    """校验/规范化用户方案配置（以空白种子为骨架+用户值叠加，保证键集完整）。

    - 顶层键：用 _build_blank_config() 的完整键集做骨架，用户已有值覆盖，
      缺失的键保持空白种子默认（不强迫用户填，但杜绝 KeyError 崩）。
    - 兼容性修复：若 active_factors 为空但 factor_configs 已有配置，
      自动启用这些因子，避免用户配置后因子预期分始终为 0。
    - 纯去默认原则不变：用户显式未配置的项（空 dict/空 list）引擎会透过
      require_config 提示，不会凭空给假默认阈值。
    """
    seed = _build_blank_config()
    if not isinstance(raw, dict):
        return seed
    # 以空白种子为底，用户实际配置逐项叠加（仅顶层——嵌套结构保持用户原样，
    # 避免因子参数被种子清掉）。
    for k, v_seed in seed.items():
        if k in raw:
            # 用户有 -> 用用户的（允许空 dict，引擎会 require_config 提示）
            seed[k] = copy.deepcopy(raw[k])
    # 种子键集之外的键必须原样带上：种子是「骨架」而不是「白名单」。
    # 旧实现只遍历 seed，任何种子没预置的键（scan_tech_filter / backtest 等）都会被
    # 静默丢弃，表现为「UI 保存成功、配置里也有、引擎却读不到 → 功能点了没反应」。
    for k, v_raw in raw.items():
        if k not in seed:
            seed[k] = copy.deepcopy(v_raw)
        # 用户没有 -> 保持 seed 填入的占位（{} / [] / DEFAULT_ENABLED 等）
    cfg = seed
    # 兼容性修复：若 active_factors 为空但 factor_configs 已有配置，
    # 自动启用这些因子，避免用户配置后因子预期分始终为 0。
    active = cfg.get('active_factors')
    fconfigs = cfg.get('factor_configs') or {}
    if (not isinstance(active, list) or not active) and isinstance(fconfigs, dict) and fconfigs:
        cfg['active_factors'] = list(fconfigs.keys())
    return cfg

def get_blank_config():
    """返回一份空白种子副本（UI 新建方案时预填）。"""
    return copy.deepcopy(_build_blank_config())

# ============================================================
# 文件加载 / 保存
# ============================================================
def _merge_scheme_config(name, period=None):
    """返回某 scheme 合并后的 config（内联因子段 + factor_profile 引用段 + 周期覆盖）。

    - 有 factor_profile 引用：以该 profile 的 active_factors/factor_configs/score_scale 为基底，
      再叠加 scheme 内联（非空）配置项覆盖。实现「因子共用一套、阈值/加减仓/风控按方向隔离」。
    - 无 factor_profile（遗留 A股 平铺方案）：直接返回方案内联 config。
    - period：若方案含 period_configs[period]（完整预设覆盖），在 base 之上深合并，
      实现「按周期存多套预设」——该周期的分析/评分使用其专属阈值/权重/规则。
    返回 None 表示 scheme 不存在。
    """
    global _MODEL
    if not isinstance(_MODEL, dict):
        return None
    sc = (_MODEL.get('schemes') or {}).get(name)
    if not isinstance(sc, dict):
        return None
    cfg = sc.get('config')
    if not isinstance(cfg, dict):
        cfg = {}
    fp_name = sc.get('factor_profile')
    profiles = _MODEL.get('factor_profiles') or {}
    if fp_name and isinstance(profiles.get(fp_name), dict):
        # 因子键由 profile 独占：引用 profile 的方案内联不应携带 active_factors/
        # factor_configs/score_scale（ensure_futures_schemes 用 blank() 填充会带空 {}，
        # 若直接 merged.update 会清空共享因子）。故内联因子键一律忽略，仅用 profile。
        _FACTOR_KEYS = ('active_factors', 'factor_configs', 'score_scale')
        merged = copy.deepcopy(profiles[fp_name])
        for k, v in cfg.items():
            if v is None:
                continue
            if k in _FACTOR_KEYS:
                logger.info("方案 %s 引用 profile %s，忽略内联因子键 %s（由 profile 独占）", name, fp_name, k)
                continue
            merged[k] = v
        base = merged
    else:
        base = cfg
    # 周期维度预设覆盖：仅对「未标记 _meta.period 的遗留方案」生效（兼容旧数据）。
    # 新模型（每周期=独立方案）的方案自带 _meta.period，其 config 即该周期配置，
    # 不再叠加 period_configs，避免与单周期方案语义冲突。
    if period and not (isinstance(sc.get('_meta'), dict) and sc['_meta'].get('period')):
        po = (sc.get('period_configs') or {}).get(period)
        if isinstance(po, dict) and po:
            base = _deep_merge(base, po)
    cfg_out = _validate_config(base)
    _sync_custom_factors(cfg_out)
    return cfg_out


def _sync_custom_factors(cfg):
    """把 cfg 里的自定义公式因子编译注册进 factor_registry。

    调用点是 _merge_scheme_config（配置合并的唯一收口），因此 load_config /
    load_market_scheme / save_current_scheme / save_scheme_period_config 及
    worker 子进程加载配置时都会自动同步，无需各处重复挂钩。
    失败不抛异常，避免自定义因子拖垮配置加载。
    """
    try:
        from engine import factor_registry
        factor_registry.sync_custom_factors((cfg or {}).get('factor_configs') or {})
    except Exception:
        logger.exception("同步自定义因子失败（不影响配置加载）")


# 周期排序键（用于 get_scheme_periods 稳定输出顺序：日K/周K 在前，分钟级升序）
_PERIOD_ORDER = {'日K': 0, '周K': 1, '60分钟': 2, '30分钟': 3, '15分钟': 4, '5分钟': 5, '1分钟': 6}


def _deep_merge(base, override):
    """深合并：dict 递归合并，其余类型（含 list）以 override 为准。"""
    if not isinstance(base, dict) or not isinstance(override, dict):
        return copy.deepcopy(override)
    out = copy.deepcopy(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _default_futures_profile():
    """期货因子 profile 默认：复制当前 A股 方案的因子段（可后续单独调参）。"""
    stock_cfg = _merge_scheme_config(resolve_scheme('stock')) or {}
    return {
        'active_factors': list(stock_cfg.get('active_factors', []) or []),
        'factor_configs': copy.deepcopy(stock_cfg.get('factor_configs', {}) or {}),
        'score_scale': copy.deepcopy(stock_cfg.get('score_scale', {}) or {}),
    }


def load_config(force_reload=False):
    """加载 quant_model.json，设置当前方案 _cache。

    无文件 / 空 schemes / 损坏 / 无 current_scheme → 返回 None（待配置态），_cache 置 None。
    否则返回当前方案的 config dict（= _cache，已合并 factor_profile）。
    """
    global _MODEL, _cache, _CURRENT
    if _MODEL is not None and not force_reload:
        return _cache
    path = QUANT_MODEL_FILE
    if not os.path.isfile(path):
        _MODEL = None
        _cache = None
        _CURRENT = None
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        logger.exception("load_config 失败（%s）", path)
        _MODEL = None
        _cache = None
        _CURRENT = None
        return None
    if not isinstance(data, dict):
        _MODEL = None
        _cache = None
        _CURRENT = None
        return None
    _MODEL = data
    _CURRENT = data.get('current_scheme')
    schemes = data.get('schemes') or {}
    # 旧方案自动迁移：缺 _meta 的遗留方案补全默认身份（A股/多/日K），
    # 使其能被 resolve_scheme / load_market_scheme 按 (市场,方向,周期) 精确匹配，
    # 避免「无 _meta → 遗留兜底分支 → 按字典序误选第一个」的歧义。
    # 仅在存在缺 _meta 方案时补全并写回一次。
    _migrated = False
    for _sn, _sc in schemes.items():
        if not isinstance(_sc, dict):
            continue
        if '_meta' not in _sc:
            _sc['_meta'] = {'market': 'stock', 'direction': 'long', 'period': '日K'}
            _migrated = True
        else:
            # 补全 _meta 中缺失的字段（如 market 为空的旧方案）
            _m = _sc['_meta']
            if not isinstance(_m, dict):
                _sc['_meta'] = {'market': 'stock', 'direction': 'long', 'period': '日K'}
                _migrated = True
            else:
                if not _m.get('market'):
                    _m['market'] = 'stock'
                    _migrated = True
                if not _m.get('direction'):
                    _m['direction'] = 'long'
                    _migrated = True
                if not _m.get('period'):
                    _m['period'] = '日K'
                    _migrated = True
    if _migrated:
        try:
            _write_model()
        except Exception:
            logger.warning("自动补全方案 _meta 写回失败（不影响内存运行）")
    if not _CURRENT or _CURRENT not in schemes:
        _cache = None
        return None
    _cache = _merge_scheme_config(_CURRENT)
    return _cache


def reload_model_only():
    """仅从磁盘重载 _MODEL（方案元数据/列表），但不覆盖 _cache（当前激活方案）。

    供 worker 线程使用：扫描入口已通过 load_market_scheme 设置好 _cache 为用户选中的方案，
    worker 线程只需要确保 _MODEL 是最新的（拿到最新方案列表），不能把 _cache 冲回
    current_scheme，否则会出现「扫描用 A 方案、分析用 B 方案」的错乱。
    """
    global _MODEL
    path = QUANT_MODEL_FILE
    if not os.path.isfile(path):
        return False
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            _MODEL = data
            return True
    except Exception as e:
        logger.exception("reload_model_only 失败（%s）", path)
    return False

def _write_model():
    """原子写回 _MODEL 到 quant_model.json。"""
    global _MODEL
    if _MODEL is None:
        return False
    path = QUANT_MODEL_FILE
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except Exception as e:
        logger.error("无法创建配置目录：%s", e)
        return False
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(_MODEL, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception as e:
        logger.error("写回 quant_model.json 失败：%s", e)
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass
        return False

def save_current_scheme(config):
    """保存当前方案的 config（覆盖写），并刷新 _cache。

    一致性保证：先备份 _MODEL → 改内存 → 写磁盘 → 成功才刷新 _cache；
    失败则回滚内存到备份，避免「磁盘是旧的、内存是新的」不一致窗口。
    """
    global _MODEL, _cache
    import copy as _copy
    if _MODEL is None:
        load_config()
    if _MODEL is None or _CURRENT is None:
        return False
    if not isinstance(config, dict):
        return False
    # 快照备份
    _snapshot = _copy.deepcopy(_MODEL)
    try:
        schemes = _MODEL.setdefault('schemes', {})
        existing = schemes.get(_CURRENT, {}) if isinstance(schemes.get(_CURRENT), dict) else {}
        existing['config'] = config
        schemes[_CURRENT] = existing
        if not _write_model():
            # 写盘失败：回滚内存快照
            _MODEL = _snapshot
            return False
        # 写盘成功后才合并 _cache，确保后续分析/评分与磁盘内容一致
        _cache = _merge_scheme_config(_CURRENT)
        return True
    except Exception:
        # 任何未预期异常都回滚快照，避免半改状态
        _MODEL = _snapshot
        logger.exception("save_current_scheme 异常")
        return False

# ============================================================
# 方案管理
# ============================================================
def get_schemes():
    """返回全部方案 dict（name -> {label, desc, _meta, factor_profile, config}）。"""
    return (_MODEL or {}).get('schemes', {}) or {}

def get_scheme_meta(name):
    """返回某方案的 _meta（市场/方向标签）；无则空 dict。"""
    sc = (_MODEL or {}).get('schemes', {}).get(name, {})
    return sc.get('_meta', {}) if isinstance(sc, dict) else {}

def resolve_scheme(market='stock', direction='long', period=None, strict=False, mode=None):
    """按 市场 + 方向（+可选周期）+ 用法模式 解析对应方案名。

    周期分类轴（每周期=独立方案）：当 period 给定时，优先返回 _meta 同时匹配
    (market, direction, period) 的方案；否则回落该市场+方向的第一个方案。

    用法模式轴（初/高级独立方案）：当 mode 非空时，仅匹配 _meta.mode == mode 的
    方案（初/高级方案隔离，避免「高级分析却用到初级方案」的错配）。mode 为空
    时不按模式过滤（兼容旧调用/遗留方案）。

    strict：为 True 时，若 period 给定但无精确 (market,direction,period) 匹配，
        直接返回 None（不回落），由调用方明确报错——
        避免「用户选了15分钟却偷偷用日K方案」这类静默错配。

    - stock：遗留方案（无 _meta）或 _meta.market=='stock' 的方案。
    - futures：_meta.market=='futures' 的方案；direction 精确匹配，否则回落该市场第一个。
    返回方案名；无匹配返回 None。
    """
    if not isinstance(_MODEL, dict):
        return None
    schemes = _MODEL.get('schemes') or {}
    if market == 'futures':
        best = None
        for name, sc in schemes.items():
            meta = sc.get('_meta', {}) if isinstance(sc, dict) else {}
            if meta.get('market') != 'futures':
                continue
            # 用法模式隔离：未标 mode 的 legacy 方案兜底归属「高级」（沿用旧版量化配置，
            # 初级按技术条件独立建档），故只有 mode 为 advanced 或空（视作 advanced）才命中
            # 高级模式；标了 mode 则必须与目标模式一致。
            if mode and (meta.get('mode') or 'advanced') != mode:
                continue
            if meta.get('direction') == direction:
                if period and meta.get('period') == period:
                    return name
                if best is None:
                    best = name
        if strict and period:
            return None
        return best
    stock_best = None
    for name, sc in schemes.items():
        if not isinstance(sc, dict):
            continue
        meta = sc.get('_meta', {})
        if meta.get('market') != 'stock':
            continue
        if mode and (meta.get('mode') or 'advanced') != mode:
            continue
        if period and meta.get('period') == period:
            return name
        if stock_best is None:
            stock_best = name
    if strict and period:
        return None
    if stock_best:
        return stock_best
    # 遗留无 _meta 方案兜底（无 period 过滤）：仅当未按 mode 过滤（mode 为空）时回溯，
    # 保证按 mode 隔离时不落入无归属的旧方案。
    if not period and not mode:
        for name, sc in schemes.items():
            if isinstance(sc, dict) and '_meta' not in sc:
                return name
    return None

def load_market_scheme(market='stock', direction='long', period=None, force_reload=False, scheme_name=None, strict_period=False, mode=None):
    """按市场 + 方向（可选周期）+ 用法模式 加载对应方案到 _cache（不持久化 current_scheme，避免配置编辑器跳转）。

    period：非空时叠加该周期的专属预设覆盖（period_configs[period]），
    实现「分析/评分按周期取对应预设」。
    scheme_name：非空时**强制使用指定方案名**（监控可让用户自选策略，而非永远按
        市场+方向自动解析）；若该名不存在则回落 resolve_scheme（保持兼容）。
    strict_period：为 True 时，给定 period 但无精确 (市场,方向,period) 方案则
        返回 None（不静默回落），由调用方明确报错（诊断场景用，避免错配周期方案）。
    force_reload：True 时强制从磁盘重载 quant_model.json，确保分析/评分入口总能
        读到用户刚保存的最新配置（用户可能在编辑器保存后立刻跑分析）。
    mode：用法模式（'basic'/'advanced'）过滤。入参缺省 None 时取当前全局模式
        （get_usage_mode()）作为隔离轴——实现「初/高级两套独立方案」：初级分析
        只解析初级方案、高级分析只解析高级方案，杜绝跨模式错配。
        mode 显式传 None（如兼容遗留无归属方案）时不做模式过滤。
    返回合并后的 config；解析不到方案时返回 None（待配置态）。
    所有 getter 随后读取该 _cache，实现「分析/门禁按市场+模式分流」。
    """
    global _MODEL, _cache, _CURRENT
    if _MODEL is None or force_reload:
        load_config(force_reload=True)
    # 用法模式轴：默认取当前全局模式隔离初/高级方案；显式传 ''/False 表示不隔离。
    if mode == '' or mode is False:
        mode = None
    elif mode is None:
        mode = get_usage_mode()
    name = None
    _explicit = False
    if scheme_name:
        schemes = (_MODEL or {}).get('schemes', {})
        if isinstance(schemes, dict) and scheme_name in schemes:
            # 校验方案方案的市场+周期身份与目标（market, period）匹配：
            #   1) 避免跨市场错配（如 A股方案被期货分析入口引用）
            #   2) 避免跨周期错配（如用户选日K，却用了激活的周K方案——本 bug 根因）。
            #      方案 _meta.period 存在且与目标 period 不一致时**拒绝**命中该方案，
            #      由上层 resolve_scheme 按 (market,dir,period) 匹配正确的周期方案。
            #   3) 方案 _meta.period 为 None 视为"全周期基底方案"，允许任意周期复用，
            #      由 _merge_scheme_config(period) 叠加周期预设（period_configs[period]）。
            # 4) 用法模式轴：显式指定方案时也校验 _meta.mode 与当前模式一致，
            #      避免「高级模式用户手动选了初级方案」被加载——前端按模式隔离下拉，
            #      此处兜底防止跨模式显式引用。未标 mode 的 legacy 方案兜底归「高级」。
            _sc = schemes[scheme_name]
            _m = _sc.get('_meta', {}) if isinstance(_sc, dict) else {}
            _mode_ok = (mode is None) or ((_m.get('mode') or 'advanced') == mode)
            if _m.get('market') in (None, market) and _mode_ok:
                _s_period = _m.get('period')
                if _s_period and period and _s_period != period:
                    logger.info("load_market_scheme: 方案 %s 周期=%s ≠ 目标周期=%s，跳过",
                                scheme_name, _s_period, period)
                else:
                    name = scheme_name
                    _explicit = True
    if name is None and period:
        # 周期分类轴：给定周期时优先按 (市场,方向,周期) 定位专属方案
        name = resolve_scheme(market, direction, period, strict=strict_period, mode=mode)
    if name is None and not strict_period:
        # 回落分支恒用非严格解析（保持兼容：A股/无周期场景取首个匹配）。
        # 注意：strict_period=True 时保持 None 不回落到第一个方案——
        # 否则「用户选15分钟但无15分钟方案」会静默用日K方案（strict 形同虚设）。
        name = resolve_scheme(market, direction, mode=mode)
    if not name or name not in ((_MODEL or {}).get('schemes') or {}):
        _CURRENT = None
        _cache = None
        return None
    if _explicit:
        # 仅「调用方显式指定且市场匹配」时才更新当前激活方案
        # （openScheme / 诊断 / 扫描下拉选择、switch_scheme 等用户显式动作）。
        # 无 scheme_name 的自动解析（如诊断预览、保存遗留路径）不覆盖 _CURRENT，
        # 避免把用户刚激活的方案悄悄改成「按市场解析的第一个」，导致手动分析
        # 切换方案后仍沿用旧 _CURRENT（如一直用 555）。
        _CURRENT = name
    # 关键：每次分析入口都重新合并配置（含周期覆盖），不直接复用陈旧的 _cache。
    # _cache 随后被各 getter（get_thresholds/get_entry_params 等）读取，
    # 这就是「切换方案 / 修改配置后立即做分析能否生效」的唯一根路径。
    _cache = _merge_scheme_config(name, period)
    return _cache


def save_scheme_period_config(name, period, config):
    """把某周期的完整预设写入方案的 period_configs[period]（按周期存多套预设）。

    与 save_scheme_config（写 base 内联 config）不同：本函数不触碰共享因子 profile、
    也不改 base，仅存该周期的独立覆盖。period 为该周期选择器字符串（如 '15分钟'）。
    成功返回 True。

    一致性：改内存快照→写盘→成功才刷新 _cache；失败回滚快照。
    """
    global _MODEL, _cache
    import copy as _copy
    if _MODEL is None:
        load_config(force_reload=True)
    if _MODEL is None or name not in (_MODEL.get('schemes') or {}):
        return False
    if not isinstance(config, dict) or not period:
        return False
    sc = _MODEL['schemes'][name]
    if not isinstance(sc, dict):
        return False
    _snapshot = _copy.deepcopy(_MODEL)
    try:
        sc.setdefault('period_configs', {})[period] = config
        if not _write_model():
            _MODEL = _snapshot
            return False
        if _CURRENT == name:
            # 写盘成功后再刷新 _cache，让进程内 getter 读到新值
            _cache = _merge_scheme_config(name, period)
        return True
    except Exception:
        _MODEL = _snapshot
        logger.exception("save_scheme_period_config 异常")
        return False


def get_scheme_periods(name):
    """返回某方案已配置的周期预设列表（按 _PERIOD_ORDER 稳定排序）。无则返回 []。"""
    if not isinstance(_MODEL, dict):
        return []
    sc = (_MODEL.get('schemes') or {}).get(name)
    if not isinstance(sc, dict):
        return []
    pc = sc.get('period_configs')
    if not isinstance(pc, dict):
        return []
    return sorted(pc.keys(), key=lambda p: _PERIOD_ORDER.get(p, 99))

def ensure_futures_schemes():
    """确保期货多单/空单方案存在（缺失则创建，引用共享 'futures' 因子 profile）。返回新建的方案名列表。"""
    global _MODEL
    import copy as _copy
    if _MODEL is None:
        load_config(force_reload=True)
    if _MODEL is None:
        _MODEL = {'schemes': {}, 'current_scheme': None, 'factor_profiles': {}}
    _snapshot = _copy.deepcopy(_MODEL)
    try:
        schemes = _MODEL.setdefault('schemes', {})
        profiles = _MODEL.setdefault('factor_profiles', {})
        if 'futures' not in profiles:
            profiles['futures'] = _default_futures_profile()
        created = []
        for direction in ('long', 'short'):
            name = '期货_多单' if direction == 'long' else '期货_空单'
            if name not in schemes:
                schemes[name] = {
                    'desc': '',
                    '_meta': {'market': 'futures', 'direction': direction, 'period': '日K'},
                    'factor_profile': 'futures',
                    'config': _build_blank_config(),
                }
                created.append(name)
        if created:
            if not _write_model():
                _MODEL = _snapshot
                return []
        return created
    except Exception:
        _MODEL = _snapshot
        logger.exception("ensure_futures_schemes 异常")
        return []

def get_factor_profiles():
    """返回全部因子 profile（name -> {active_factors, factor_configs, score_scale}）。"""
    return (_MODEL or {}).get('factor_profiles', {}) or {}

def save_factor_profile(name, cfg):
    """保存某个因子 profile（供期货因子共用调参）。刷新引用该 profile 的当前 _cache。

    一致性：先快照→改内存→写盘→成功再刷 cache；失败回滚。
    """
    global _MODEL, _cache
    import copy as _copy
    if _MODEL is None:
        load_config(force_reload=True)
    if _MODEL is None:
        return False
    if not isinstance(cfg, dict):
        return False
    _snapshot = _copy.deepcopy(_MODEL)
    try:
        _MODEL.setdefault('factor_profiles', {})[name] = cfg
        if not _write_model():
            _MODEL = _snapshot
            return False
        if _CURRENT and isinstance(_MODEL['schemes'].get(_CURRENT), dict) \
                and _MODEL['schemes'][_CURRENT].get('factor_profile') == name:
            _cache = _merge_scheme_config(_CURRENT)
        return True
    except Exception:
        _MODEL = _snapshot
        logger.exception("save_factor_profile 异常")
        return False

def save_scheme_config(name, config):
    """保存指定方案的内联 config（配置编辑器按市场/方向编辑用，不依赖 current_scheme）。

    一致性：快照→改内存→写盘→成功再刷 cache；失败回滚。
    """
    global _MODEL, _cache
    import copy as _copy
    if _MODEL is None:
        load_config(force_reload=True)
    if _MODEL is None or name not in (_MODEL.get('schemes') or {}):
        return False
    if not isinstance(config, dict):
        return False
    _snapshot = _copy.deepcopy(_MODEL)
    try:
        _MODEL['schemes'][name]['config'] = config
        if not _write_model():
            _MODEL = _snapshot
            return False
        if _CURRENT == name:
            # 写盘成功后才刷 _cache；分析/诊断/扫描入口每次再调 load_market_scheme(force_reload=True)
            _cache = _merge_scheme_config(name)
        return True
    except Exception:
        _MODEL = _snapshot
        logger.exception("save_scheme_config 异常")
        return False

def set_scheme_period(name, period):
    """更新指定方案的周期标签 _meta.period（周期=方案身份属性，改之即移动分类轴）。

    一致性：快照→改内存→写盘；失败回滚。
    """
    global _MODEL
    import copy as _copy
    if _MODEL is None:
        load_config(force_reload=True)
    if _MODEL is None or name not in (_MODEL.get('schemes') or {}):
        return False
    sc = _MODEL['schemes'][name]
    if not isinstance(sc, dict):
        return False
    _snapshot = _copy.deepcopy(_MODEL)
    try:
        sc.setdefault('_meta', {})['period'] = period
        if not _write_model():
            _MODEL = _snapshot
            return False
        return True
    except Exception:
        _MODEL = _snapshot
        logger.exception("set_scheme_period 异常")
        return False

def check_scheme_complete(market='stock', direction='long', period=None):
    """校验某市场/方向（+周期，可选）方案的配置完整性（核心项）。返回 (ok, missing_list)。

    统一使用 REQUIRED_STRATEGY 常量，保证前端 Banner「✓方案完整」与回测/扫描/诊断
    入口红字报错 100% 一致。之前两处 required 列表写死不同（Banner 要 risk_params，
    回测要 conflict_penalty），导致「Banner 绿但回测红」症状。

    period：非空时按 (market,dir,period) 精确匹配方案，并在 _merge_scheme_config
    时叠加该周期预设覆盖，与回测入口同周期判定。
    """
    name = None
    if _CURRENT and isinstance(_MODEL, dict):
        cur_sc = (_MODEL.get('schemes') or {}).get(_CURRENT)
        if isinstance(cur_sc, dict):
            cur_meta = cur_sc.get('_meta') or {}
            _cur_period = cur_meta.get('period')
            # 市场/方向必须匹配；周期：如果传了 period 就也要一致，否则放行（旧兼容）
            period_ok = (period is None) or (_cur_period == period) or (_cur_period is None)
            if cur_meta.get('market') == market and period_ok and \
               (market != 'futures' or cur_meta.get('direction') == direction):
                name = _CURRENT
    if not name:
        name = resolve_scheme(market, direction, period, strict=False) if period \
            else resolve_scheme(market, direction)
    if not name:
        return False, ['对应方案不存在（请先创建该分类+周期方案）']
    cfg = _merge_scheme_config(name, period) or {}
    # 若激活因子列表为空，即便 factor_configs 非空也评不了分，提前列为缺失。
    miss = []
    if not isinstance(cfg.get('active_factors'), list) or len(cfg['active_factors']) == 0:
        miss.append('激活因子')
    # 复用 require_config 的判定逻辑（空 dict/空 list 视为未配置）
    try:
        require_config(cfg, REQUIRED_STRATEGY)
    except ConfigIncompleteError as e:
        miss.extend(e.missing)
    return (len(miss) == 0), miss

def get_current_scheme_name():
    """返回当前激活方案名；无则 None。"""
    return _CURRENT

def clear_current_scheme():
    """清空当前激活方案（内存态，不持久化）。"""
    global _CURRENT, _cache
    _CURRENT = None
    _cache = None

def switch_scheme(name):
    """切换当前方案，持久化并刷新 _cache（合并 factor_profile）。成功返回 True。

    一致性：先改 current_scheme→写盘→成功再更新内存 _CURRENT/_cache；失败回滚。
    """
    global _MODEL, _cache, _CURRENT
    import copy as _copy
    if _MODEL is None:
        load_config()
    if not _MODEL or name not in _MODEL.get('schemes', {}):
        return False
    _snapshot = _copy.deepcopy(_MODEL)
    _prev_current = _CURRENT
    _prev_cache = _cache
    try:
        _MODEL['current_scheme'] = name
        if not _write_model():
            _MODEL = _snapshot
            return False
        # 写盘成功后才更新进程内 _CURRENT 与 _cache，避免磁盘/内存错位
        _CURRENT = name
        _cache = _merge_scheme_config(name)
        return True
    except Exception:
        _MODEL = _snapshot
        _CURRENT = _prev_current
        _cache = _prev_cache
        logger.exception("switch_scheme 异常")
        return False

def add_scheme(name, desc=None, config=None, meta=None, factor_profile=None, label=None):
    """新增方案（同名已存在返回 False）。config 缺省用空白种子。
    meta: 方案市场/方向标签 {market:'stock'|'futures', direction:'long'|'short'}；
    factor_profile: 引用的因子 profile 名（'stock'/'futures'），用于因子共用。
    label: 方案显示名（状态栏优先展示），缺省不写，运行时回退到方案名。

    问题15修复：显式未指定 factor_profile 时自动关联：
      - meta.market=='futures' → 引用共享 'futures' 因子 profile；
      - 确保期货多/空单方案调参时共用一套因子，用户改一处同步到另一处。
    """
    global _MODEL
    import copy as _copy
    if _MODEL is None:
        load_config()
    if _MODEL is None:
        _MODEL = {'schemes': {}, 'current_scheme': None, 'factor_profiles': {}}
    _snapshot = _copy.deepcopy(_MODEL)
    try:
        schemes = _MODEL.setdefault('schemes', {})
        if name in schemes:
            return False
        scheme_obj = {
            'desc': desc or '',
            'config': _seed_config_for(meta, config),
        }
        if meta:
            scheme_obj['_meta'] = dict(meta)
        if label:
            scheme_obj['label'] = label
        # 自动关联 factor_profile：期货方案引用 'futures'（需 ensure 过 profiles 存在）
        if factor_profile:
            scheme_obj['factor_profile'] = factor_profile
        elif isinstance(meta, dict) and meta.get('market') == 'futures':
            profiles = _MODEL.setdefault('factor_profiles', {})
            if 'futures' not in profiles:
                profiles['futures'] = _default_futures_profile()
            scheme_obj['factor_profile'] = 'futures'
        schemes[name] = scheme_obj
        if not _write_model():
            _MODEL = _snapshot
            return False
        return True
    except Exception:
        _MODEL = _snapshot
        logger.exception("add_scheme 异常")
        return False

def duplicate_scheme(source, new_name, new_label=None):
    """复制方案（源不存在/目标已存在返回 False）。保留 _meta 与 factor_profile；
    new_label 为新方案的显示名 label（缺省沿用空，回退到方案名）。

    一致性：快照→改内存→写盘；失败回滚。
    """
    global _MODEL
    import copy as _copy
    if _MODEL is None:
        load_config()
    _snapshot = _copy.deepcopy(_MODEL)
    try:
        schemes = (_MODEL or {}).get('schemes', {})
        if source not in schemes or new_name in schemes:
            return False
        src = schemes[source]
        new_obj = {
            'desc': src.get('desc', ''),
            'config': copy.deepcopy(src.get('config', {})),
        }
        if new_label:
            new_obj['label'] = new_label
        if isinstance(src, dict):
            if '_meta' in src:
                new_obj['_meta'] = copy.deepcopy(src['_meta'])
            if 'factor_profile' in src:
                new_obj['factor_profile'] = src['factor_profile']
        schemes[new_name] = new_obj
        _MODEL['schemes'] = schemes
        if not _write_model():
            _MODEL = _snapshot
            return False
        return True
    except Exception:
        _MODEL = _snapshot
        logger.exception("duplicate_scheme 异常")
        return False

def remove_scheme(name):
    """删除方案；若删除的是当前方案，优先切到被删方案同 (market,dir,period) 的剩余方案，
    否则回落同 (market,dir) 第一个，再否则回落剩余任意一个，都没有则置 None。

    问题11修复：此前 next(iter(_MODEL['schemes'])) 可能切到其他分类，导致用户感觉"删A后跳到B方案界面"
    """
    global _MODEL, _cache, _CURRENT
    import copy as _copy
    if _MODEL is None:
        load_config()
    if not _MODEL or name not in _MODEL.get('schemes', {}):
        return False
    deleted_meta = None
    sc = _MODEL['schemes'].get(name)
    if isinstance(sc, dict):
        deleted_meta = sc.get('_meta') or {}
    _snapshot = _copy.deepcopy(_MODEL)
    _prev_current = _CURRENT
    _prev_cache = _cache
    try:
        del _MODEL['schemes'][name]
        if _MODEL.get('current_scheme') == name:
            if _MODEL['schemes']:
                # 智能选择下一个 current_scheme（按 _meta 匹配度优先级）
                candidates = list(_MODEL['schemes'].keys())
                picked = None
                dm = deleted_meta or {}
                d_mkt = dm.get('market')
                d_dir = dm.get('direction')
                d_per = dm.get('period')
                # 优先级1：精确 (market, direction, period)
                if d_mkt and d_per:
                    for c in candidates:
                        c_sc = _MODEL['schemes'].get(c)
                        c_m = c_sc.get('_meta', {}) if isinstance(c_sc, dict) else {}
                        if c_m.get('market') == d_mkt and \
                           (d_mkt != 'futures' or c_m.get('direction') == d_dir) and \
                           c_m.get('period') == d_per:
                            picked = c
                            break
                # 优先级2：(market, direction) 同
                if picked is None and d_mkt:
                    for c in candidates:
                        c_sc = _MODEL['schemes'].get(c)
                        c_m = c_sc.get('_meta', {}) if isinstance(c_sc, dict) else {}
                        if c_m.get('market') == d_mkt and \
                           (d_mkt != 'futures' or c_m.get('direction') == d_dir):
                            picked = c
                            break
                # 优先级3：同 market
                if picked is None and d_mkt:
                    for c in candidates:
                        c_sc = _MODEL['schemes'].get(c)
                        c_m = c_sc.get('_meta', {}) if isinstance(c_sc, dict) else {}
                        if c_m.get('market') == d_mkt:
                            picked = c
                            break
                if picked is None:
                    picked = candidates[0]
                if not _write_model():
                    _MODEL = _snapshot
                    return False
                _CURRENT = picked
                _MODEL['current_scheme'] = picked
                _cache = _merge_scheme_config(picked)
            else:
                if not _write_model():
                    _MODEL = _snapshot
                    return False
                _CURRENT = None
                _MODEL['current_scheme'] = None
                _cache = None
            return True
        # 删的不是 current_scheme：直接写盘
        if not _write_model():
            _MODEL = _snapshot
            return False
        return True
    except Exception:
        _MODEL = _snapshot
        _CURRENT = _prev_current
        _cache = _prev_cache
        logger.exception("remove_scheme 异常")
        return False

def rename_scheme(old_name, new_name):
    """重命名方案（键名变更，保留其全部内容与 _meta）。

    成功返回 (True, '')；失败返回 (False, 错误说明)。
    若 new_name 与某方案重名或非法，拒绝并说明；若 old==new 视为成功（无操作）。

    一致性：快照→改内存→写盘→成功再刷 _cache/_CURRENT；失败回滚。
    """
    global _MODEL, _cache, _CURRENT
    import copy as _copy
    if _MODEL is None:
        load_config()
    schemes = (_MODEL or {}).get('schemes', {})
    if not isinstance(schemes, dict) or old_name not in schemes:
        return (False, f'源方案不存在：{old_name}')
    if old_name == new_name:
        return (True, '')
    if not new_name or not str(new_name).strip():
        return (False, '方案名不能为空')
    if new_name in schemes:
        return (False, f'方案名已存在：{new_name}')
    _snapshot = _copy.deepcopy(_MODEL)
    _prev_current = _CURRENT
    _prev_cache = _cache
    try:
        schemes[new_name] = schemes.pop(old_name)
        if _MODEL.get('current_scheme') == old_name:
            _MODEL['current_scheme'] = new_name
            if not _write_model():
                _MODEL = _snapshot
                return (False, '方案写回失败（重命名未生效）')
            _CURRENT = new_name
            _cache = _merge_scheme_config(new_name)
            return (True, '')
        if not _write_model():
            _MODEL = _snapshot
            return (False, '方案写回失败（重命名未生效）')
        return (True, '')
    except Exception:
        _MODEL = _snapshot
        _CURRENT = _prev_current
        _cache = _prev_cache
        logger.exception("rename_scheme 异常")
        return (False, f'重命名异常：未知错误')

# ============================================================
# 因子 / 评分相关 getter
# ============================================================
def _cfg():
    return _cache if isinstance(_cache, dict) else {}

def get_active_factors():
    af = _cfg().get('active_factors')
    return list(af) if isinstance(af, list) else []

def get_factor_configs():
    return _cfg().get('factor_configs')

def get_factor_weights():
    fcs = get_factor_configs() or {}
    # 过滤掉 weight 缺失/非法（None）的因子，避免下游 z*None TypeError 崩链
    out = {}
    for f, fc in fcs.items():
        if not isinstance(fc, dict):
            continue
        w = fc.get('weight')
        if isinstance(w, (int, float)) and not (isinstance(w, float) and math.isnan(w)):
            out[f] = w
    return out

def get_factor_direction():
    fcs = get_factor_configs() or {}
    out = {}
    for f, fc in fcs.items():
        if not isinstance(fc, dict):
            continue
        d = fc.get('direction', 1)
        if isinstance(d, (int, float)):
            out[f] = d
    return out

def get_factor_stats(period=None):
    """返回因子 z-score 校准表 {因子: {mean, std}}。

    period 为 None/'日K'/'周K' 时返回日线校准（默认）。
    分钟周期：若方案配置了 factor_period_stats[period][因子] 覆盖则用之，
    否则回落日线校准（报告会标注『指示性』，因分钟因子未独立 IC 标定）。
    """
    fcs = get_factor_configs() or {}
    base = {f: fc.get('stats') for f, fc in fcs.items() if isinstance(fc, dict)}
    if period in (None, '日K', '周K'):
        return base
    overrides = _cfg().get('factor_period_stats') or {}
    bucket = overrides.get(period) or {}
    if not bucket:
        return base
    merged = {}
    for f, s in base.items():
        merged[f] = bucket[f] if (f in bucket and isinstance(bucket[f], dict)) else s
    return merged


# ============================================================
# 自定义公式因子（用户自建指标）读写
# ============================================================

def _custom_factor_target():
    """定位自定义因子应写入的容器：profile 型方案写 factor_profiles[fp]，
    平铺型方案写 schemes[cur].config（与 save_factor_stats 的写侧契约一致）。

    Returns:
        (factor_configs_dict, active_factors_list) —— 均为 _MODEL 内的可变引用；
        无可用方案时返回 None。
    """
    if _MODEL is None:
        load_config(force_reload=True)
    if not isinstance(_MODEL, dict) or not _CURRENT:
        return None
    sc = (_MODEL.get('schemes') or {}).get(_CURRENT)
    if not isinstance(sc, dict):
        return None
    fp = sc.get('factor_profile')
    profiles = _MODEL.get('factor_profiles')
    if fp and isinstance(profiles, dict) and isinstance(profiles.get(fp), dict):
        p = profiles[fp]
        if not isinstance(p.get('factor_configs'), dict):
            p['factor_configs'] = {}
        if not isinstance(p.get('active_factors'), list):
            p['active_factors'] = []
        return p['factor_configs'], p['active_factors']
    cfg = sc.get('config')
    if not isinstance(cfg, dict):
        cfg = {}
        sc['config'] = cfg
    if not isinstance(cfg.get('factor_configs'), dict):
        cfg['factor_configs'] = {}
    if not isinstance(cfg.get('active_factors'), list):
        cfg['active_factors'] = []
    return cfg['factor_configs'], cfg['active_factors']


def _commit_custom_factors():
    """写盘并刷新 _cache（_cache 刷新会触发 factor_registry 重新同步注册）。"""
    global _cache
    if not _write_model():
        return False
    _cache = _merge_scheme_config(_CURRENT)
    return True


def list_custom_factors():
    """返回当前方案的自定义因子列表（含公式/权重/方向/统计/启用态）。"""
    cfg = _safe_cfg() or {}
    fcs = cfg.get('factor_configs') or {}
    active = set(cfg.get('active_factors') or [])
    out = []
    for name, fc in fcs.items():
        if not isinstance(fc, dict) or not fc.get('custom'):
            continue
        out.append({
            'name': name,
            'label': fc.get('label') or name[len('cf_'):],
            'formula': fc.get('formula') or '',
            'weight': fc.get('weight', 1.0),
            'direction': fc.get('direction', 1),
            'stats': fc.get('stats') or {'mean': 0.0, 'std': 1.0},
            'enabled': name in active,
        })
    return sorted(out, key=lambda x: x['label'])


def save_custom_factor(label, formula, weight=1.0, direction=1, stats=None, activate=True):
    """保存（新建/更新）自定义公式因子。

    Returns:
        (ok: bool, error_or_none)
    """
    from engine import custom_factor, factor_registry

    global _MODEL
    label = str(label or '').strip()
    if not label:
        return False, '指标名不能为空'
    if len(label) > 20:
        return False, '指标名不能超过 20 个字符'
    check = custom_factor.validate_formula(formula)
    if not check['ok']:
        e = check['errors'][0]
        return False, f"第{e['line']}行第{e['col']}列：{e['msg']}"

    if _MODEL is None:
        load_config(force_reload=True)
    target = _custom_factor_target()
    if target is None:
        return False, '请先创建并激活一个方案'
    fconfigs, active = target

    name = factor_registry.custom_factor_name(label)
    try:
        weight = float(weight)
    except (TypeError, ValueError):
        weight = 1.0
    try:
        direction = int(direction)
    except (TypeError, ValueError):
        direction = 1
    if direction not in (1, -1):
        direction = 1
    st = stats if isinstance(stats, dict) else {}
    try:
        mean = float(st.get('mean', 0.0))
        std = float(st.get('std', 1.0))
    except (TypeError, ValueError):
        mean, std = 0.0, 1.0
    if not math.isfinite(std) or std <= 0:
        std = 1.0
    if not math.isfinite(mean):
        mean = 0.0

    import copy as _copy
    _snapshot = _copy.deepcopy(_MODEL)
    try:
        fconfigs[name] = {
            'weight': weight,
            'direction': direction,
            'params': {},
            'stats': {'mean': mean, 'std': std},
            'custom': True,
            'label': label,
            'formula': formula,
        }
        if activate and name not in active:
            active.append(name)
        if not _commit_custom_factors():
            _MODEL = _snapshot
            return False, '配置写入失败'
        return True, None
    except Exception:
        _MODEL = _snapshot
        logger.exception("save_custom_factor 异常")
        return False, '保存自定义因子时发生异常'


def delete_custom_factor(label_or_name):
    """删除自定义公式因子（同时从 active_factors 移除）。

    Returns:
        (ok: bool, error_or_none)
    """
    from engine import factor_registry

    global _MODEL
    key = str(label_or_name or '').strip()
    if not key:
        return False, '缺少指标名'
    name = key if factor_registry.is_custom_factor(key) else factor_registry.custom_factor_name(key)

    if _MODEL is None:
        load_config(force_reload=True)
    target = _custom_factor_target()
    if target is None:
        return False, '请先创建并激活一个方案'
    fconfigs, active = target
    if name not in fconfigs:
        return False, '该自定义因子不存在'

    import copy as _copy
    _snapshot = _copy.deepcopy(_MODEL)
    try:
        fconfigs.pop(name, None)
        if name in active:
            active.remove(name)
        if not _commit_custom_factors():
            _MODEL = _snapshot
            return False, '配置写入失败'
        return True, None
    except Exception:
        _MODEL = _snapshot
        logger.exception("delete_custom_factor 异常")
        return False, '删除自定义因子时发生异常'


# ── 周期感知因子窗口：时间等价缩放 ──
# 期货分钟线分析时，因子窗口参数(period/lookback/ma_period)按"自然时间等价"缩放，
# 使『20日相对强度』在任何周期都表示约 20 个交易日，而非 20 根 bar。
# 日盘约 4 小时 ≈ 240 分钟作为 1 交易日近似；bins(价格分档)等非时间参数不缩放。
PERIOD_MINUTES = {'1分钟': 1, '5分钟': 5, '15分钟': 15, '30分钟': 30,
                  '60分钟': 60, '日K': 240, '周K': 1200}
TRADING_MINUTES_PER_DAY = 240
# 时间类窗口参数（需随周期缩放）；bins 等为价格/计数类，不缩放
TIME_WINDOW_PARAM_KEYS = {'period', 'lookback', 'ma_period'}


def get_period_scaled_factor_configs(base_configs, k_type='日K'):
    """按周期缩放因子窗口参数，返回新 dict（不修改入参）。

    - 日线/周线：原样返回（深拷贝）。
    - 分钟线：时间类窗口(period/lookback/ma_period)按
      scale=TRADING_MINUTES_PER_DAY/PERIOD_MINUTES[k_type] 缩放，clamp 到 [2, 600]
      （600 根 ≈ 落在新浪分钟线 1023 根上限内，且远小于部分因子 schema.max，
      内部缩放允许超过 UI 输入上限以保住周期语义）。
    - 非时间参数(bins 等)与 stats 保留不变；stats 的周期分桶由 get_factor_stats 处理。
    """
    if k_type in (None, '日K', '周K'):
        return {f: (dict(c) if isinstance(c, dict) else c) for f, c in (base_configs or {}).items()}
    scale = TRADING_MINUTES_PER_DAY / PERIOD_MINUTES.get(k_type, 240)
    out = {}
    for f, c in (base_configs or {}).items():
        if not isinstance(c, dict):
            out[f] = c
            continue
        nc = dict(c)
        params = dict(nc.get('params') or {})
        for pk in TIME_WINDOW_PARAM_KEYS:
            v = params.get(pk)
            if isinstance(v, (int, float)):
                scaled = int(round(v * scale))
                params[pk] = max(2, min(600, scaled))
        nc['params'] = params
        out[f] = nc
    return out

def get_score_scale():
    return _cfg().get('score_scale')

def get_thresholds():
    """返回阈值配置，自动兼容历史 `{val: int}` 形态与现行 `int` 形态（兼容 #2026-08-16）。

    历史部分方案（早期手动录入/导入）的 `thresholds` 字段为 `{key: {val: int}}` dict 形态；
    现行 web_api 保存路径生成 `{key: int}` 形态。引擎读取方统一依赖 int 数值，
    这里归一化抽取出 `val` 字段，避免 '>' not supported between 'int' and 'dict' 抛错。
    """
    raw = _cfg().get('thresholds') or {}
    if not isinstance(raw, dict):
        return {}
    norm = {}
    for k, v in raw.items():
        if isinstance(v, dict) and 'val' in v:
            norm[k] = v['val']
        else:
            norm[k] = v
    return norm

def get_conflict_penalty():
    return _cfg().get('conflict_penalty')

def get_entry_params():
    return _cfg().get('entry_params')

def get_entry_conditions():
    return _cfg().get('entry_conditions')

def get_scan_tech_filter():
    """读取「技术指标二级过滤」条件（高级模式全市场扫描·方案级）。

    结构为条件列表（支持 AND 组合，语义与 _evaluate_tech_conditions / 建仓技术条件一致）。
    未配置返回 None → 引擎跳过过滤（全保留）；空列表同样等同于不启用。"""
    return _cfg().get('scan_tech_filter')

def get_veto_params():
    """读取否决项阈值参数（方案级，全局一套，随方案切换）。

    结构：{否决项key: {参数名: 值}}，与 veto_registry.evaluate_vetos 的 params 一致；
    缺失项由注册表 defaults 兜底。未配置返回 {}（全部用默认阈值）。"""
    c = _cfg().get('veto_params')
    return c if isinstance(c, dict) else {}

def get_usage_mode():
    """全局用法模式（唯一权威裁判）：'basic' 初级=因子休眠·只做持有期纪律 | 'advanced' 高级=完整。
    与 web_api（config/usage.json）同一持久化文件，engine 各入口据此判断是否走因子/建仓。
    默认 'advanced'，保证初/高级不影响已保存的因子与仓位方案数据。"""
    try:
        _p = os.path.join(CONFIG_DIR, 'usage.json')
        if os.path.exists(_p):
            with open(_p, 'r', encoding='utf-8') as _f:
                _d = json.load(_f)
            _m = _d.get('usage_mode')
            if _m in ('basic', 'advanced'):
                return _m
    except Exception:
        pass
    return 'advanced'

def load_entry_condition():
    """返回入场条件配置（report_builder 用）。

    配置存在即返回（配置即启用）；未配置返回 None（报告对应段落返回空列表）。
    """
    return get_entry_conditions()

def get_tech_resonance_config():
    return _cfg().get('tech_resonance')

def get_ghost_params():
    """幽灵规则参数（配置即启用；未配置返回 None → 引擎跳过）。"""
    return _cfg().get('ghost_rules')

def get_add_params():
    """加仓参数（配置即启用；未配置返回 None → 引擎返回「未启用」）。"""
    return _cfg().get('add_params')

def get_reduce_params():
    """减仓参数（配置即启用；未配置返回 None → 引擎返回「未启用」）。"""
    return _cfg().get('reduce_params')

def get_risk_params():
    """风控参数（可选：不配置即不启用，返回 None）。"""
    return _cfg().get('risk_params')

# ============================================================
# 市场门控
# ============================================================
def is_subsystem_enabled(key):
    """读取 cfg['enabled'][key]（UI 写入才生效；未写入时为全 False）。"""
    cfg = _cfg()
    en = cfg.get('enabled')
    if isinstance(en, dict):
        return bool(en.get(key, False))
    return False

def is_market_gate_enabled(market='stock'):
    """市场门控是否启用：子系统级(enabled.market_gate) 或 门控级(market_gate.enabled) 任一为真。

    期货(多/空)不使用 A 股「全市场上涨家数占比」环境门控，恒返回 False（中性）。
    """
    if market != 'stock':
        return False
    sub = is_subsystem_enabled('market_gate')
    mg = _cfg().get('market_gate') or {}
    gate = bool(mg.get('enabled'))
    return bool(sub or gate)

def get_market_gate_config(up_ratio=None, market='stock'):
    """根据上涨比例返回环境配置 {factor, limit, label, desc}。

    门控未启用时仍返回对应环境标签（供报告展示），但 factor/limit 置 1，
    确保仓位不随环境调整；启用时按用户 envs 生效。

    期货(多/空)无全局环境门控：恒返回中性（系数 1.0、不限仓、标签「期货中性」）。
    """
    if market != 'stock':
        return {
            'factor': 1.0, 'limit': 1.0,
            'label': '期货中性', 'desc': '期货不使用 A 股全局环境门控系数',
        }
    cfg = _cfg()
    mg = cfg.get('market_gate')
    enabled = isinstance(mg, dict) and bool(mg.get('enabled'))
    envs = (mg.get('envs') if isinstance(mg, dict) else None) or MARKET_GATE_CONFIG
    if not isinstance(envs, list) or not envs:
        envs = MARKET_GATE_CONFIG
    # up_ratio NaN/None/inf 守卫：统一归中性值 0.5，与 rebound 逻辑兜底一致，
    # 消除「门控判最熊 / rebound 判非恐慌」的自相矛盾（NaN/None 不再落入最熊 band）。
    if up_ratio is None or not isinstance(up_ratio, (int, float)) \
            or math.isnan(up_ratio) or math.isinf(up_ratio):
        up_ratio = 0.5
    # 中性兜底 band（envs 全部非 dict 或完全非法时用此结构，避免 band.get 抛 AttributeError）
    _NEUTRAL_BAND = {'factor': 1.0, 'limit': 1.0, 'label': '中性兜底', 'desc': '配置异常，回落中性'}
    band = None
    for b in envs:
        if isinstance(b, dict) and b.get('min', 0) <= up_ratio < b.get('max', 1):
            band = b
            break
    if not isinstance(band, dict):
        # 遍历筛选出 envs 中最后一个合法 dict；若都不是 dict 则兜底
        last_valid = None
        for b in reversed(envs):
            if isinstance(b, dict):
                last_valid = b
                break
        band = last_valid if isinstance(last_valid, dict) else _NEUTRAL_BAND
    return {
        'factor': float(band.get('factor', 1.0)) if enabled else 1.0,
        'limit': float(band.get('limit', 1.0)) if enabled else 1.0,
        'label': band.get('label', '') or '',
        'desc': band.get('desc', '') or '',
    }

# ============================================================
# UI 辅助
# ============================================================
def normalize_weights(weights):
    """归一化权重使之和=1.0（容错：全 0 时均分）。"""
    if not isinstance(weights, dict):
        return {}
    total = 0.0
    for v in weights.values():
        if isinstance(v, (int, float)):
            total += float(v)
    if total <= 0:
        n = len(weights)
        return {k: round(1.0 / n, 6) for k in weights} if n else {}
    return {k: round(float(v) / total, 6) for k, v in weights.items()}

def save_factor_stats(stats):
    """把因子 IC 统计（stats: {factor: {mean, std, rank_ic, ic, ic_ir, ...}}）写回当前方案。

    返回 (ok, updated)：ok 为是否成功落盘（_write_model 结果）；updated 为实际写入的
    因子数（供 UI 文案使用——此前返回 bool，文案会把 True 直接代入「已将 {n} 个…」）。

    写入目标按方案类型路由（与 _merge_scheme_config 的读侧契约对齐）：
      - profile 型方案（有 factor_profile）：写 factor_profiles[<fp>].factor_configs[<f>]。
        方案内联的因子键会被 _merge_scheme_config 静默忽略（:279-281），故写内联必然落空。
      - 平铺型方案（无 factor_profile）：写 schemes[_CURRENT].config.factor_configs（原逻辑）。

    写入内容：stats 归一为 {mean, std}（与 get_factor_stats / config_mapper 的消费口径一致），
    并同步只读 IC 元数据 ic / ic_ir / ic_weighted_value，供「按IC加权」按钮读取；
    报告字段 rank_ic 映射为 ic。
    """
    global _MODEL, _cache
    if _MODEL is None:
        load_config()
    if _MODEL is None or _CURRENT is None:
        return False, 0
    schemes = _MODEL.setdefault('schemes', {})
    sc = schemes.get(_CURRENT)
    if not isinstance(sc, dict):
        return False, 0

    def _apply(fcs):
        """把 stats 写入指定 factor_configs，返回写入因子数（无匹配则 0）。"""
        n = 0
        for f, st in (stats or {}).items():
            cur = fcs.get(f)
            if not isinstance(cur, dict) or not isinstance(st, dict):
                continue
            cur['stats'] = {'mean': st.get('mean'), 'std': st.get('std')}
            for meta in ('ic', 'ic_ir', 'ic_weighted_value'):
                if meta in st:
                    cur[meta] = st[meta]
            # 因子IC回测报告使用 rank_ic 而非 ic，需映射
            if 'rank_ic' in st and 'ic' not in st:
                cur['ic'] = st['rank_ic']
            n += 1
        return n

    fp_name = sc.get('factor_profile')
    if fp_name:
        # profile 型：因子键由共享 profile 独占，必须写到 profile 才生效
        prof = (_MODEL.get('factor_profiles') or {}).get(fp_name)
        if not isinstance(prof, dict):
            logger.warning("[save_factor_stats] 方案 %s 引用的 factor_profile=%s 不存在，写入中止",
                           _CURRENT, fp_name)
            return False, 0
        fcs = prof.get('factor_configs')
        if not isinstance(fcs, dict):
            fcs = {}
            prof['factor_configs'] = fcs
        updated = _apply(fcs)
        if updated == 0 and stats:
            logger.warning(
                "[save_factor_stats] 方案 %s（profile=%s）无因子匹配：stats 键=%s，profile 现有因子=%s",
                _CURRENT, fp_name, list((stats or {}).keys()), list(fcs.keys())[:5])
    else:
        # 平铺型（遗留方案）：原逻辑
        cfg = sc.get('config', {}) if isinstance(sc.get('config'), dict) else {}
        fcs = cfg.get('factor_configs', {}) if isinstance(cfg.get('factor_configs'), dict) else {}
        updated = _apply(fcs)
        cfg['factor_configs'] = fcs
        sc['config'] = cfg
        schemes[_CURRENT] = sc

    ok = _write_model()
    # 统一 merged 口径刷新 _cache（原实现用 _validate_config(raw cfg)，会把 profile 型方案的
    # 因子段覆成空态，与读侧 _merge_scheme_config 口径不一致）
    _cache = _merge_scheme_config(_CURRENT)
    return ok, updated
