#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""配置管理模块"""

import json
import os
import sys
import copy
import shutil
import logging

logger = logging.getLogger(__name__)


def _write_startup_debug(msg):
    """启动诊断：把关键路径/错误追加写入 %TEMP%/M-Bull_startup.log（仅诊断用，绝不抛异常）。

    用途：当 AppData/M-Bull 目录未按预期出现时，靠这份日志确认程序“以为”自己要把
    配置写到哪个路径、sys.executable 解析成什么、以及 makedirs 是否失败。
    """
    try:
        import tempfile as _tf
        import datetime as _dt
        p = os.path.join(_tf.gettempdir(), "M-Bull_startup.log")
        with open(p, "a", encoding="utf-8") as f:
            f.write("%s | %s\n" % (_dt.datetime.now().isoformat(), msg))
    except Exception:
        pass

# ============================================================
# 应用路径配置（支持打包后用户可配置）
# ============================================================

# 产品持久化目录名（与品牌一致；改名只需改这里）。编译/onefile 态统一落
# %LOCALAPPDATA%/<APP_NAME>，与 exe 实际文件名/位置完全解耦。
APP_NAME = "M-Bull"


def _running_in_onefile():
    """检测 Nuitka / PyInstaller onefile 运行时。

    Nuitka 4.x onefile 的真实表现：程序解压到 %TEMP%/onefile_<pid>_<rand>/，
    运行时 sys.executable 指向该临时目录里的 python.exe（**不是**原始 exe 路径），
    __file__ 也落在临时目录内。所以只要 sys.executable 或 __file__ 的某级父目录
    名为 onefile_* 即为 onefile 态。PyInstaller onefile 用 sys._MEIPASS 识别。
    """
    try:
        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            return True
    except Exception:
        pass
    for p in (sys.executable, __file__):
        try:
            cur = os.path.dirname(os.path.abspath(p))
        except Exception:
            continue
        for _ in range(4):
            if not cur or cur in ('/', '\\', ''):
                break
            if os.path.basename(cur).startswith('onefile_'):
                return True
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
    return False


def get_app_dir():
    """获取程序数据目录（编译/onefile 态统一落 AppData 持久化，开发态用项目根目录）。

    运行形态与解析：
    - 环境变量 QUANT_SYSTEM_DIR：优先使用指定目录（便于用户自定义）。
    - Nuitka 4.x onefile：sys.executable 是 %TEMP%/onefile_*/python.exe，不能靠
      exe 名/__file__ 反推项目根，故直接落 %LOCALAPPDATA%/<APP_NAME>（与 exe 位置、
      临时目录完全解耦，重建/改名/换路径都稳定）。
    - 开发环境：sys.executable 为 python*.exe 且不在 onefile 临时目录内 → 项目根目录。
    - 编译态 standalone / 普通 exe：目录名取 exe 文件名主干（如 M-Bull.exe）。
    """
    # 1. 优先使用环境变量指定的目录
    env_dir = os.environ.get('QUANT_SYSTEM_DIR')
    if env_dir and os.path.isdir(env_dir):
        return os.path.abspath(env_dir)

    # 2. onefile 模式：解压在临时目录，必须落到稳定的 AppData/<APP_NAME>
    if _running_in_onefile():
        return os.path.join(
            os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), APP_NAME)

    exe_name = os.path.basename(sys.executable).lower()
    if exe_name.startswith('python') or exe_name == 'py.exe':
        # 开发环境：engine/config.py -> engine/ -> 项目根目录
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # 3. 编译态 standalone / 普通 exe：统一落 APP_NAME，不再用 exe 文件名主干。
    #    ⚠️ 之前用 exe_stem 导致旧名 QuantSystem.exe 落到 %LOCALAPPDATA%\QuantSystem，
    #       与新 M-Bull.exe(onefile 走分支2=APP_NAME) 形成双目录、扫描结果互不相交。
    #       改用 APP_NAME 后，无论 exe 叫什么名都只认 %LOCALAPPDATA%\M-Bull，根除双目录。
    return os.path.join(
        os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), APP_NAME)


def _get_bundled_dir():
    """获取打包资源所在目录（exe 内部解压后的目录），用于首启种子复制。

    ⚠️ PyInstaller onefile 必须【最先】判断 sys._MEIPASS：
       PyInstaller 会设置 sys._MEIPASS，且 _running_in_onefile() 也会因该属性命中
       onefile 判定；但其解压目录名为 _MEIxxxx（**不是** onefile_*）。若先走
       _running_in_onefile() 分支去扫描 onefile_* 目录，会因找不到而返回 None，
       导致 bundled 种子永远不复制——这正是「出厂 sector_map 变成旧的小表 / 内置种子全不生效」
       的根因。故 _MEIPASS 优先于 onefile 扫描。

    - PyInstaller onefile：sys._MEIPASS。
    - Nuitka 4.x onefile：解压到 %TEMP%/onefile_*，扫描最新含内置 cache 的 onefile_* 目录。
    - Nuitka standalone：资源随 exe 同目录（exe_dir/cache/*.pkl）。
    - 开发环境（python*.exe 且不在 onefile 临时目录）：返回 None（不复制种子）。
    """
    # 1. PyInstaller onefile：资源在 sys._MEIPASS（最先判断，避免被下面 onefile 扫描吞掉）
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        return sys._MEIPASS
    # 2. Nuitka 4.x onefile：解压到 %TEMP%/onefile_*，取最新修改者
    if _running_in_onefile():
        import tempfile as _tf
        temp_dir = os.environ.get('TEMP') or os.environ.get('TMP') or _tf.gettempdir()
        if os.path.isdir(temp_dir):
            matches = []
            for name in os.listdir(temp_dir):
                if name.startswith('onefile_'):
                    cand = os.path.join(temp_dir, name)
                    if os.path.isfile(os.path.join(cand, 'cache', 'stock_list.json')):
                        matches.append(cand)
            if matches:
                matches.sort(key=lambda d: os.path.getmtime(d), reverse=True)
                return matches[0]
        return None
    # 3. Nuitka standalone：资源随 exe 同目录
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    if os.path.isfile(os.path.join(exe_dir, 'cache', 'stock_list.json')):
        return exe_dir
    return None


def _migrate_legacy_app_dir(target_app_dir):
    """改名后首次运行：把旧的 AppData/QuantSystem 数据目录整体迁到新目录名。

    仅当目标目录尚为空（全新安装）时迁移，避免覆盖用户已在新目录产生的数据。
    开发模式（target 为项目根目录，非空）直接跳过。
    """
    try:
        local = os.environ.get('LOCALAPPDATA', os.path.expanduser('~'))
        legacy = os.path.join(local, 'QuantSystem')
        if legacy == target_app_dir or not os.path.isdir(legacy):
            return
        # 目标目录已存在且有内容 -> 视为已有数据，跳过迁移
        if os.path.isdir(target_app_dir) and os.listdir(target_app_dir):
            return
        os.makedirs(target_app_dir, exist_ok=True)
        for name in os.listdir(legacy):
            src = os.path.join(legacy, name)
            dst = os.path.join(target_app_dir, name)
            if os.path.exists(dst):
                continue
            try:
                if os.path.isdir(src):
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
            except Exception as ce:
                logger.warning(f"迁移子项失败 {name}: {ce}")
        logger.info(f"已从旧数据目录迁移: {legacy} -> {target_app_dir}")
        # 迁移完成后删除已成孤儿的旧目录，避免用户以后再看到 QuantSystem 双目录混淆。
        # 仅在目标已非空（说明迁移内容已落地）时清理，且全程容错不阻断主流程。
        try:
            if os.path.isdir(target_app_dir) and os.listdir(target_app_dir):
                shutil.rmtree(legacy, ignore_errors=True)
                logger.info(f"已清理孤儿旧目录: {legacy}")
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"迁移旧数据目录失败(可忽略): {e}")


APP_DIR = get_app_dir()
CONFIG_DIR = os.path.join(APP_DIR, 'config')
CACHE_DIR = os.path.join(APP_DIR, 'cache')

# 启动诊断：记录实际解析出的写入目标（即便后续 makedirs 失败也能在日志里看到路径）
_write_startup_debug("LOCALAPPDATA=%r exe=%r APP_DIR=%r CONFIG_DIR=%r CACHE_DIR=%r"
                     % (os.environ.get('LOCALAPPDATA'), sys.executable, APP_DIR, CONFIG_DIR, CACHE_DIR))

# 改名后首次运行：先把旧 AppData/QuantSystem 数据迁到新目录名。
# ⚠️ 必须在 makedirs(config/cache) 之前执行——否则 target 已被建出空目录，
#    迁移函数会误判为"已有数据"而跳过，导致旧方案配置丢失。
_migrate_legacy_app_dir(APP_DIR)

# 确保用户配置目录存在（失败也写诊断日志，便于定位无写权/路径异常）
try:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)
    _write_startup_debug("makedirs OK")
except Exception as e:
    _write_startup_debug("makedirs FAILED: %r" % (e,))
    raise


def _count_sector_map_stocks(path):
    """统计 sector_map.json 中的股票总数，用于判断哪份映射更全。"""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return 0
        return sum(len(v) for v in data.values() if isinstance(v, list))
    except Exception:
        return 0


def _migrate_sector_map_from_temp(dst_path):
    """从旧 Nuitka onefile 临时目录找回用户编辑过的 sector_map.json。

    当 onefile 数据目录检测修复后，持久化目录可能只剩首次复制的种子模板；
    用户此前在旧 exe 运行期编辑的全市场映射实际躺在 %TEMP%/onefile_*/config/ 下。
    本函数扫描这些残留目录，选择股票总数最多的一份迁移到持久目录。
    """
    try:
        temp_dir = os.environ.get('TEMP') or os.environ.get('TMP') or os.path.expanduser('~')
        if not os.path.isdir(temp_dir):
            return False
        best_path = None
        best_count = _count_sector_map_stocks(dst_path) if os.path.exists(dst_path) else 0
        for name in os.listdir(temp_dir):
            if not name.startswith('onefile_'):
                continue
            candidate = os.path.join(temp_dir, name, 'config', 'sector_map.json')
            if os.path.isfile(candidate):
                cnt = _count_sector_map_stocks(candidate)
                if cnt > best_count:
                    best_count = cnt
                    best_path = candidate
        if best_path:
            os.makedirs(os.path.dirname(dst_path), exist_ok=True)
            _atomic_copy(best_path, dst_path)
            logger.info(f"已从旧 onefile 临时目录迁移 sector_map.json: "
                        f"{best_path} -> {dst_path} ({best_count} 只)")
            return True
    except Exception as e:
        logger.warning(f"迁移旧 sector_map.json 失败: {e}")
    return False


def _has_schemes(path):
    """判断 quant_model.json 是否含用户方案（schemes 非空或 current_scheme 非空）。"""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            d = json.load(f)
        return bool(d.get('schemes')) or bool(d.get('current_scheme'))
    except Exception:
        return False


def _migrate_userdata_from_temp(app_dir):
    """从残留 Nuitka onefile 临时目录找回用户编辑过的自选股/策略方案。

    旧版 exe 曾把配置写到 Nuitka 解压的 %TEMP%/onefile_*/config/（临时目录，
    退出即丢），改名+持久化修复后配置改落 AppData/<exe名>。本函数在种子复制
    【之前】运行，把残留 onefile 临时目录里的用户数据迁到 AppData，避免升级后丢数据。
    仅补缺（本地已有对应文件则不覆盖）。
    """
    temp_dir = os.environ.get('TEMP') or os.environ.get('TMP') or os.path.expanduser('~')
    if not os.path.isdir(temp_dir):
        return
    cands = []
    for name in os.listdir(temp_dir):
        if name.startswith('onefile_'):
            c = os.path.join(temp_dir, name, 'config')
            if os.path.isdir(c):
                cands.append(c)
    if not cands:
        return
    dst_cfg = os.path.join(app_dir, 'config')
    os.makedirs(dst_cfg, exist_ok=True)
    # 自选股：取最新修改的 watchlist.txt
    _migrate_pick(cands, dst_cfg, 'watchlist.txt', 'mtime')
    # 策略方案：仅迁"方案非空"的 quant_model.json（本地无方案才迁）
    _migrate_pick(cands, dst_cfg, 'quant_model.json', 'schemes')


def _migrate_pick(cands, dst_cfg, fname, mode):
    dst = os.path.join(dst_cfg, fname)
    if mode == 'schemes':
        if _has_schemes(dst):
            return
    elif os.path.exists(dst) and os.path.getsize(dst) > 0:
        return
    best = None
    for c in cands:
        src = os.path.join(c, fname)
        if not os.path.isfile(src):
            continue
        if mode == 'schemes' and not _has_schemes(src):
            continue
        if best is None or os.path.getmtime(src) > os.path.getmtime(best):
            best = src
    if best:
        _atomic_copy(best, dst)
        logger.info(f"已从旧 onefile 临时目录迁移 {fname}: {best}")


def _ensure_config_dir():
    """首次启动（打包后）将内置 config 种子复制到持久化目录。

    支持 PyInstaller / Nuitka onefile / Nuitka standalone 三种打包模式。
    - 幂等：只复制持久化目录中【缺失】的文件，绝不覆盖用户已编辑的文件
      （如 watchlist.txt / quant_model.json / license.json）。
    - sector_map.json 特殊处理：优先从旧 onefile 临时目录迁移用户全市场映射；
      若 bundled 模板比本地旧种子更全，则覆盖本地旧版本（避免显示过小的模板）。
    - 开发模式：不执行，行为不变。
    """
    bundled_dir = _get_bundled_dir()
    if not bundled_dir:
        return
    src = os.path.join(bundled_dir, 'config')
    dst = os.path.join(get_app_dir(), 'config')
    if not os.path.isdir(src):
        return
    os.makedirs(dst, exist_ok=True)

    # sector_map.json：先尝试从旧 Temp 目录找回用户编辑的全市场映射，
    # 但【无论迁移是否成功】都再用 bundled 覆盖「残缺/过小」的本地版——
    # 避免「残留 onefile 临时目录里的小表」或旧本地版长期霸占出厂映射，
    # 也避免 _get_bundled_dir 修复前遗留在持久化目录里的旧小表不被刷新。
    # 仅当用户本地/残留副本【比 bundled 更大】时才保留（尊重用户扩展），否则一律以 bundled 为准。
    src_sector = os.path.join(src, 'sector_map.json')
    dst_sector = os.path.join(dst, 'sector_map.json')
    if os.path.isfile(src_sector):
        _migrate_sector_map_from_temp(dst_sector)  # 优先恢复用户编辑（若残留副本更大）
        bundled_cnt = _count_sector_map_stocks(src_sector)
        local_cnt = _count_sector_map_stocks(dst_sector) if os.path.exists(dst_sector) else 0
        if bundled_cnt > local_cnt:
            _atomic_copy(src_sector, dst_sector)
            logger.info(f"已用 bundled sector_map.json 覆盖本地旧版本 "
                        f"({local_cnt} -> {bundled_cnt} 只)")

    # 其他配置：幂等复制，不覆盖用户已有文件
    for name in os.listdir(src):
        s = os.path.join(src, name)
        d = os.path.join(dst, name)
        if name == 'sector_map.json':
            continue
        if os.path.isfile(s) and not os.path.exists(d):
            shutil.copy2(s, d)


def _read_saved_at(path):
    """读取缓存文件里的 saved_at / scan_time 字段（用于判断哪个更新）。失败返回 None。"""
    try:
        import json
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data.get('saved_at') or data.get('scan_time')
    except Exception:
        return None


def _atomic_copy(s, d):
    """原子复制（先写 .tmp 再 os.replace），避免复制中断留下半截文件导致下次启动读损坏。"""
    import tempfile
    ddir = os.path.dirname(d) or '.'
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=ddir, suffix='.tmp')
        os.close(fd)
        shutil.copy2(s, tmp)
        os.replace(tmp, d)
    except Exception:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass


def _ensure_runtime_cache():
    """首次启动（打包后）把内置 cache 种子复制到持久化目录的 cache/。

    支持 PyInstaller / Nuitka onefile / Nuitka standalone 三种打包模式。

    为什么必须做：运行时加载路径是 <app_dir>/cache/*，而打包种子在 bundled_dir/cache/*，
    _ensure_config_dir 只复制 config 不复制 cache，导致首次运行缺这些文件。

    复制策略（只补缺失 / 自愈，绝不盲目覆盖用户运行时数据）：
    - stock_list.json：saved_at 对比，bundled 较新则覆盖（自愈旧的部分列表），否则保留运行时已拉到的。
    - 回测行情缓存：不再内置单一 pkl 种子。回测现直接复用全市场扫描写入的
      cache/kline/qfq_daily_{code}.pkl（按代码维度），由扫描/回测自动维护，
      免手动更新种子包；首启联网即能回测（offline_mode=False 时）。
    - market_scan_cache.json：不再内置种子。出厂版仅含测试残留 96 条，不应在启动时展示；
      用户首次扫描后由 save_cache 自行写入（带 source='user' 标记）。
      启动时 load_cache 仅展示 source=='user' 的缓存，避免一打开就显示内置测试数据。
    - 复制用原子写（.tmp + replace），中断不残留半截。
    """
    bundled_dir = _get_bundled_dir()
    if not bundled_dir:
        return
    src_cache = os.path.join(bundled_dir, 'cache')
    dst_cache = os.path.join(get_app_dir(), 'cache')
    if not os.path.isdir(src_cache):
        return
    os.makedirs(dst_cache, exist_ok=True)
    for name, use_saved_at in (
        ('stock_list.json', True),
    ):
        s = os.path.join(src_cache, name)
        d = os.path.join(dst_cache, name)
        if not os.path.isfile(s):
            continue
        copy = False
        if not os.path.exists(d):
            copy = True
        elif use_saved_at:
            sb = _read_saved_at(s)
            dr = _read_saved_at(d)
            if sb and (dr is None or sb > dr):
                copy = True
        if copy:
            _atomic_copy(s, d)


_migrate_userdata_from_temp(APP_DIR)   # 种子前：从残留 onefile 临时目录迁回用户自选股/方案
_ensure_config_dir()       # 模块加载时调一次（仅 frozen 时真正执行）
_ensure_runtime_cache()    # 复制打包种子缓存（stock_list / 回测离线缓存）到 AppData 持久化目录

# 用户可配置文件路径
SECTOR_MAP_FILE = os.path.join(CONFIG_DIR, 'sector_map.json')
WATCHLIST_FILE = os.path.join(CONFIG_DIR, 'watchlist.txt')
CACHE_FILE = os.path.join(CACHE_DIR, 'market_scan_cache.json')
STOCK_LIST_CACHE_FILE = os.path.join(CACHE_DIR, 'stock_list.json')

# 策略配置文件路径（兼容旧路径）
CONFIG_FILE = os.path.join(CONFIG_DIR, 'strategy_config.json')
CONFIG_BACKUP_FILE = os.path.join(CONFIG_DIR, 'strategy_config_backup.json')


def _migrate_legacy_strategy_config():
    """把历史遗留的根目录 strategy_config.json / _backup.json 迁移到 config/（只读+拷贝）。

    老版本 CONFIG_FILE = APP_DIR/strategy_config.json（数据目录根）；2026-08-16 起统一进
    config/ 目录。迁移仅做一次：config/ 无同名文件且根目录存在时拷贝+删除旧文件，
    保证用户手改参数不丢失；失败静默（后续读取自动回退旧位置）。
    """
    try:
        old_main = os.path.join(get_app_dir(), 'strategy_config.json')
        old_bak = os.path.join(get_app_dir(), 'strategy_config_backup.json')
        if os.path.isfile(old_main) and not os.path.isfile(CONFIG_FILE):
            os.makedirs(CONFIG_DIR, exist_ok=True)
            import shutil as _sh
            _sh.copy2(old_main, CONFIG_FILE)
            try:
                os.remove(old_main)
            except Exception:
                pass
            logger.info("已迁移 strategy_config.json 到 config/")
        if os.path.isfile(old_bak) and not os.path.isfile(CONFIG_BACKUP_FILE):
            os.makedirs(CONFIG_DIR, exist_ok=True)
            import shutil as _sh
            _sh.copy2(old_bak, CONFIG_BACKUP_FILE)
            try:
                os.remove(old_bak)
            except Exception:
                pass
            logger.info("已迁移 strategy_config_backup.json 到 config/")
    except Exception as e:
        logger.warning("strategy_config.json 迁移失败（回退旧位置读取）: %s", e)


# ============================================================
# 默认配置
# ============================================================

DEFAULT_CONFIG = {
    "short": {
        "stop_loss_pct": 0.06,
        "target1_pct": 0.08,
        "target2_pct": 0.15,
        "trailing_retrace": 0.06,
        "rsi_threshold": 55,
        "rsi_overbought": 75,
        "volume_spike": 1.2,
        "volume_anomaly": 2.0,
        "atr_multiplier": 1.5,
        "reduce_ratio1": 0.33,
        "reduce_ratio2": 0.33,
        "add_ratio": 0.30
    },
    "long": {
        "stop_loss_atr": 3.0,
        "target_pct": 0.35,
        "rsi_period": 24,
        "macd_fast": 24,
        "macd_slow": 52,
        "macd_signal": 18,
        "momentum_threshold": 3.0,
        "add_ratio": 0.20,
        "reduce_ratio1": 0.25,
        "reduce_ratio2": 0.25,
        "reduce_trigger_pct1": 0.03,
        "reduce_trigger_pct2": 0.07
    },
    "retry": {"max_attempts": 3, "timeout": 10},
    "confidence": {
        "short_weights": {
            "ma_bull": 12,
            "ma_bear": -12,
            "rsi_mid": 5,
            "macd_bull": 8,
            "trend_up": 8,
            "volume_healthy": 5,
            "position_correct": 10,
            "position_wrong": -10,
            "mtf_up": 8
        },
        "long_weights": {
            "trend_up": 10,
            "mtf_up": 12,
            "vol_low": 5,
            "rsi_low": 5,
            "position_correct": 10,
            "position_wrong": -10
        },
        "min": 20,
        "max": 80
    },
    "cache": {"ttl_seconds": 300, "max_size": 100},
    "risk_management": {
        "max_position_pct": 0.30,
        "max_single_loss_pct": 0.02,
        "max_drawdown_pct": 0.15,
        "time_stop_days": 20,
        "kelly_fraction": 0.5
    }
    # entry_thresholds 和 market_gate 已迁移到 quant_config.py（V5统一管理）
}


# ============================================================
# 配置加载
# ============================================================

def legacy_load_config():
    """遗留配置加载（只读，不写盘）。

    仅供 data_layer / ghost_engine / state 等旧模块使用的「旧版短长线策略」配置，
    **与用户量化策略（engine.quant_config / config/quant_model.json）无关**。

    设计：只从磁盘读取已有配置；若不存在或损坏，直接返回 DEFAULT_CONFIG 的副本，
    **不再自动创建 / 写入 strategy_config.json 与 strategy_config_backup.json**，
    避免运行时在 exe 目录产生多余文件。若想自定义 ghost/retry/cache，可手动放置
    strategy_config.json，程序会读取它（但绝不回写或覆盖）。
    """
    # 0. 迁移历史遗留的根目录 strategy_config.json → config/（幂等，只做一次）
    _migrate_legacy_strategy_config()

    # 1. 优先读取主配置（存在且合法）
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
            if not isinstance(config, dict):
                raise ValueError(f"配置文件格式错误：应为 dict，实际为 {type(config).__name__}")
            # 内存补齐缺失键（不写盘）
            missing_keys = set(DEFAULT_CONFIG.keys()) - set(config.keys())
            if missing_keys:
                logger.warning(f"配置缺少以下键: {missing_keys}，使用默认值补充（不写盘）")
                for key in missing_keys:
                    config[key] = copy.deepcopy(DEFAULT_CONFIG[key])
            return config
        except Exception as e:
            logger.error(f"读取主配置失败，尝试备份: {e}")

    # 1b. 兼容：迁移失败/尚未迁移时，直接读旧根目录位置（只读，不写盘）
    _legacy_root_main = os.path.join(get_app_dir(), 'strategy_config.json')
    if os.path.exists(_legacy_root_main):
        try:
            with open(_legacy_root_main, 'r', encoding='utf-8') as f:
                config = json.load(f)
            if isinstance(config, dict):
                return config
        except Exception as e:
            logger.error(f"读取根目录遗留主配置失败: {e}")

    # 2. 主配置缺失/损坏，尝试读备份（只读，不写回、不生成 .corrupt）
    if os.path.exists(CONFIG_BACKUP_FILE):
        try:
            with open(CONFIG_BACKUP_FILE, 'r', encoding='utf-8') as f:
                backup_config = json.load(f)
            if isinstance(backup_config, dict):
                logger.info("从备份读取遗留配置（只读）")
                return backup_config
        except Exception as e:
            logger.error(f"读取备份失败: {e}")

    # 2b. 兼容：根目录旧备份（迁移失败场景）
    _legacy_root_bak = os.path.join(get_app_dir(), 'strategy_config_backup.json')
    if os.path.exists(_legacy_root_bak):
        try:
            with open(_legacy_root_bak, 'r', encoding='utf-8') as f:
                backup_config = json.load(f)
            if isinstance(backup_config, dict):
                logger.info("从根目录遗留备份读取配置（只读）")
                return backup_config
        except Exception as e:
            logger.error(f"读取根目录遗留备份失败: {e}")

    # 3. 都没有：返回内置默认配置副本，不写盘
    # 注：无遗留文件属设计正常态（只读不写盘），故用 debug 而非 warning，避免每次启动误报
    logger.debug("未找到遗留配置文件，使用内置默认配置（不写盘）")
    return copy.deepcopy(DEFAULT_CONFIG)


# ============================================================
# 初始化
# ============================================================

# 加载策略配置（遗留）
C = legacy_load_config()

# 确保板块映射存在（市场扫描模块自行管理 sector_map.json）