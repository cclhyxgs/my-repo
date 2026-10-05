#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""License Manager - App-side verification module (packaged into app)

Provides:
  - get_machine_id(): Get machine fingerprint
  - activate(code): Verify and store activation code
  - get_license_info(): Get current license status dict
  - can_use(feature): Check if a feature is available
  - consume_use(): Decrement use count (per_use licenses)
  - get_status_text(): Human-readable status for UI
"""

import os
import sys
import json
import base64
import hashlib
import hmac
import uuid
import platform
import threading
import logging
import time
from datetime import datetime, date

logger = logging.getLogger(__name__)

# Windows registry fallback for trial anti-tamper
try:
    import winreg
except Exception:
    winreg = None

# Resolve paths
_LICENSE_DIR = os.path.dirname(os.path.abspath(__file__))
_APP_DIR = os.path.dirname(_LICENSE_DIR)
if getattr(sys, 'frozen', False):
    # 编译态统一用 AppData 持久化目录，与 get_app_dir() 保持一致
    # （Nuitka 4.x onefile 的 sys.executable 指向原始 exe 路径，不能用于落盘）
    try:
        from engine.config import get_app_dir
        _APP_DIR = get_app_dir()
    except Exception:
        _APP_DIR = os.path.dirname(sys.executable)

_LICENSE_FILE = os.path.join(_APP_DIR, 'config', 'license.json')

_MACHINE_ID_FILE = os.path.join(_APP_DIR, 'config', 'machine_id')

_BACKUP_DIR = os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'StockToolData')
_BACKUP_FILE = os.path.join(_BACKUP_DIR, 'license.bak')
# 同目录隐藏备份（防主文件被删）
_BACKUP_FILE2 = os.path.join(os.path.dirname(_LICENSE_FILE), '.lstate')

# Free tier limits
FREE_DAILY_LIMIT = 10       # 每日免费次数（分析/自选诊断/全市场扫描共享）
FREE_TRIAL_DAYS = 30        # 免费试用天数（从首次使用开始计算）

# 线程锁，确保额度检查和扣减的原子性
_state_lock = threading.RLock()


_MACHINE_ID_CACHE = None


def _hardware_secret():
    """基于硬件指纹派生 HMAC 密钥，使机器码缓存 / state 文件与机器绑定。

    安全关键点：密钥来自硬件（MAC/CPU/主机名），不固定。把 config/machine_id
    或 config/license.json 复制到别的机器后，HMAC 校验会因密钥不同失败 →
    重新生成该机的机器码 → 与激活码绑定的机器码不匹配 → 授权拒绝（无法白嫖）。
    注意：机器码本身（_generate_machine_id）只依赖硬件原始组合，不依赖本密钥，
    所以同源机器升级后机器码不变，已激活授权不受影响。
    """
    mac = uuid.getnode()
    cpu = platform.processor() or 'unknown'
    hostname = os.environ.get('COMPUTERNAME', '') or os.environ.get('HOSTNAME', '') or 'unknown'
    return f'stk_{mac}_{cpu}_{hostname}_v1'.encode('utf-8')


def _get_state_hmac_key():
    """机器绑定的HMAC密钥，防止跨机器复制state文件"""
    mid = _get_machine_id()
    return f'stk_{mid}_v1'.encode('utf-8')


def _hash_machine_id(machine_id):
    """对machine_id生成HMAC签名，用于缓存文件校验（密钥基于硬件，防复制）"""
    return hmac.new(_hardware_secret(), machine_id.encode('utf-8'), hashlib.sha256).hexdigest()[:24]


def _get_cached_machine_id():
    """从缓存文件读取machine_id，带HMAC签名校验

    Returns:
        machine_id str 或 None（缓存不存在或校验失败）
    """
    global _MACHINE_ID_CACHE
    if _MACHINE_ID_CACHE is not None:
        return _MACHINE_ID_CACHE

    try:
        if not os.path.exists(_MACHINE_ID_FILE):
            return None
        with open(_MACHINE_ID_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        mid = data.get('machine_id', '')
        sig = data.get('sig', '')
        if not mid or not sig:
            return None
        expected_sig = _hash_machine_id(mid)
        if sig != expected_sig:
            return None
        _MACHINE_ID_CACHE = mid
        return mid
    except Exception:
        return None


def _save_machine_id_cache(machine_id):
    """保存machine_id到缓存文件，带HMAC签名"""
    global _MACHINE_ID_CACHE
    try:
        os.makedirs(os.path.dirname(_MACHINE_ID_FILE), exist_ok=True)
        data = {
            'machine_id': machine_id,
            'sig': _hash_machine_id(machine_id),
        }
        with open(_MACHINE_ID_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        _MACHINE_ID_CACHE = machine_id
    except Exception:
        pass


def _generate_machine_id():
    """直接生成machine fingerprint（不使用缓存）"""
    mac = uuid.getnode()
    cpu = platform.processor() or 'unknown'
    hostname = os.environ.get('COMPUTERNAME', '') or os.environ.get('HOSTNAME', '') or 'unknown'
    raw = f"{mac}-{cpu}-{hostname}"
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]


def _get_machine_id():
    """Generate machine fingerprint (SHA256 hash of hardware identifiers)

    优先从缓存文件读取，读取失败或校验失败才重新生成并写入缓存。
    防止 uuid.getnode() 在无网卡环境下不稳定导致授权失效。
    """
    cached = _get_cached_machine_id()
    if cached:
        return cached
    mid = _generate_machine_id()
    _save_machine_id_cache(mid)
    return mid


def _hash_state(state):
    """对state生成HMAC签名（排除_hash字段本身）"""
    data = {k: v for k, v in state.items() if k != '_hash'}
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False).encode('utf-8')
    return hmac.new(_get_state_hmac_key(), raw, hashlib.sha256).hexdigest()[:24]


# ============================================================
# 注册表状态副本 + 时钟回拨检测（商用加固，2026-08-16）
# ============================================================
# 删文件重置防线：授权状态除 license.json + 双备份外，再同步一份到
# HKCU\Software\M-Bull（用户级注册表），文件全删时从中恢复——提高"无限重置试用"
# 的门槛（删注册表需额外工具/权限，普通删文件不再有效）。
_REG_ROOT = r'Software\M-Bull'
_REG_STATE_NAME = 'state_json'
# 时钟回拨容忍度：系统时间比上次运行回拨超过该秒数（12h）视为可疑
# （正常时区切换/夏令时差异远小于此，避免误锁）
_CLOCK_ROLLBACK_TOLERANCE = 12 * 3600


def _reg_write_state(state):
    """把授权状态写入用户注册表（失败静默，不影响正常路径）。"""
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _REG_ROOT) as k:
            winreg.SetValueEx(k, _REG_STATE_NAME, 0, winreg.REG_SZ,
                              json.dumps(state, ensure_ascii=False))
    except Exception:
        pass


def _reg_read_state():
    """从注册表读取授权状态副本；无/损坏返回 None。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_ROOT) as k:
            v, _ = winreg.QueryValueEx(k, _REG_STATE_NAME)
        return json.loads(v)
    except Exception:
        return None


def _detect_clock_rollback():
    """检测系统时钟回拨（防"回拨时间无限续期授权/试用"）。

    记录最近一次运行时间戳（epoch 秒，写入 state）；下次运行若当前时间
    比记录回拨超过容忍阈值 → 返回 True（调用方锁定授权）。
    正常情况每次调用都会刷新时间戳（不触发）。
    """
    try:
        state = _load_state()
        last = float(state.get('_last_run_ts') or 0)
        now = time.time()
        if last > 0 and now < last - _CLOCK_ROLLBACK_TOLERANCE:
            logger.warning("检测到系统时间回拨 %.0f 秒，授权锁定（防无限续期）", last - now)
            return True
        if abs(now - last) > 60:
            state['_last_run_ts'] = now
            _save_state(state)
    except Exception:
        pass
    return False


# ============================================================
# 授权防绕过加固
# ============================================================

def _dev_mode_enabled():
    """仅开发者显式开启时进入 dev_mode（源码免授权调试）。

    默认关闭：原为 `not sys.frozen` 即判 dev_mode，导致攻击者解包 EXE 后
    直接以源码直跑入口脚本（sys.frozen 不置位）即可绕过全部授权。
    现改为必须显式标记才进 dev_mode，否则源码直跑也走完整授权校验。
    """
    if os.environ.get('STK_LICENSE_DEV', '') == '1':
        return True
    try:
        marker = os.path.join(_APP_DIR, '.devmode')
        if os.path.exists(marker):
            return True
    except Exception:
        pass
    return False


# 公钥完整性指纹：SHA256(真实 Ed25519 公钥字节)，分片 base64 存储，防一键 grep/replace。
# ⚠️ 若用 keygen.py init 重新生成密钥对，必须同步更新下方两片（重算命令见文件末尾注释）。
# 常量内联在 _expected_pubkey_hash 函数体内（2026-08-16 商用加固）：
# 模块级常量不进任何函数 __code__，seal 指纹保护不到 → 攻击者可改常量+keys.py 换密钥自签；
# 内联后常量进入函数 __code__ 常量池，seal 覆盖（_seal_spec CRITICAL_FUNCS 已含本函数）。


def _expected_pubkey_hash():
    """还原公钥 SHA256 指纹（hex）。与 _load_public_key 中的实时校验对照。"""
    _a = "qJCSHf8OSp/NUKJ359xkO8"
    _b = "3u0/7eSt2GazuI8jyixgs="
    raw = (_a + _b).encode('utf-8')
    return base64.b64decode(raw).hex()


# ============================================================
# Signature verification
# ============================================================

def _load_public_key():
    """Load Ed25519 public key from keys.py
    
    Raises RuntimeError if public key cannot be loaded (fail-closed security)
    """
    try:
        from license.keys import PUBLIC_KEY_HEX
    except ImportError:
        raise RuntimeError("授权系统配置错误：无法加载公钥（keys.py 缺失或损坏）")

    if not PUBLIC_KEY_HEX:
        raise RuntimeError("授权系统配置错误：公钥为空")

    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        pub_bytes = bytes.fromhex(PUBLIC_KEY_HEX)
    except Exception as e:
        raise RuntimeError(f"授权系统配置错误：公钥解析失败 - {e}")

    # 公钥完整性校验：防止攻击者把 keys.py 公钥替换为自己的（自签激活码绕过）。
    # 仅改 PUBLIC_KEY_HEX 不够——指纹对不上会 fail-closed 锁定。
    if hashlib.sha256(pub_bytes).hexdigest() != _expected_pubkey_hash():
        raise RuntimeError("授权系统完整性校验失败：公钥被篡改")

    return Ed25519PublicKey.from_public_bytes(pub_bytes)


def _verify_code(code):
    """Verify activation code signature and return payload

    Returns:
        payload dict if valid, None otherwise
    """
    public_key = _load_public_key()

    try:
        parts = code.strip().split('.')
        if len(parts) != 2:
            return None

        payload_bytes = base64.b64decode(parts[0])
        signature = base64.b64decode(parts[1])

        # Verify signature (raises InvalidSignature if fails)
        public_key.verify(signature, payload_bytes)

        payload = json.loads(payload_bytes.decode('utf-8'))
        return payload

    except Exception:
        return None


# ============================================================
# License state storage
# ============================================================

def _save_backup(state):
    """保存备份到两处（AppData + 同目录隐藏文件）"""
    # 备份1：AppData
    try:
        os.makedirs(_BACKUP_DIR, exist_ok=True)
        state_to_save = dict(state)
        state_to_save['_hash'] = _hash_state(state_to_save)
        with open(_BACKUP_FILE, 'w', encoding='utf-8') as f:
            json.dump(state_to_save, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    # 备份2：同目录隐藏文件
    try:
        os.makedirs(os.path.dirname(_BACKUP_FILE2), exist_ok=True)
        state_to_save2 = dict(state)
        state_to_save2['_hash'] = _hash_state(state_to_save2)
        with open(_BACKUP_FILE2, 'w', encoding='utf-8') as f:
            json.dump(state_to_save2, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _load_state():
    """Load license state from local file (with tamper detection)

    商用加固（2026-08-16）：
    - 文件/双备份全部删除时，尝试从注册表 HKCU\\Software\\M-Bull 恢复（防"删文件重置试用/额度"）
    - 状态含 _last_run_ts，供时钟回拨检测（防"回拨系统时间无限续期"）
    """
    default = {
        'activation_code': '',
        'uses_remaining': 0,
        'activated_at': '',
        'free_queries_today': 0,
        'free_queries_date': '',
        'total_free_uses': 0,
        'last_seen_date': '',
        'first_use_date': '',
        '_tampered': False,
        '_last_run_ts': 0,
    }

    def _try_load(path):
        """尝试从指定路径加载并验证，返回 state dict 或 'tampered' 或 None"""
        try:
            if not os.path.exists(path):
                return None
            with open(path, 'r', encoding='utf-8') as f:
                state = json.load(f)
            stored_hash = state.pop('_hash', '')
            expected_hash = _hash_state(state)
            if stored_hash != expected_hash:
                return 'tampered'
            for k, v in default.items():
                if k != '_tampered':
                    state.setdefault(k, v)
            state['_tampered'] = False
            return state
        except Exception:
            return None

    # 1. 尝试主文件
    result = _try_load(_LICENSE_FILE)

    if result == 'tampered':
        # 主文件被篡改，直接锁定，不从备份恢复（防止重置免费额度）
        state = dict(default)
        state['_tampered'] = True
        try:
            with open(_LICENSE_FILE, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            state['activation_code'] = raw.get('activation_code', '')
        except Exception:
            pass
        return state

    if result and isinstance(result, dict):
        _save_backup(result)
        return result

    # 2. 主文件不存在/损坏，尝试备份
    for bk_path in [_BACKUP_FILE, _BACKUP_FILE2]:
        backup_result = _try_load(bk_path)
        if backup_result and isinstance(backup_result, dict):
            _save_state(backup_result)
            return backup_result

    # 3. 全部文件缺失：尝试注册表副本（防"删文件即重置"）
    reg_state = _reg_read_state()
    if reg_state and isinstance(reg_state, dict):
        for k, v in default.items():
            if k != '_tampered':
                reg_state.setdefault(k, v)
        reg_state['_tampered'] = False
        logger.warning("本地授权状态文件缺失，已从注册表恢复（删文件重置防护）")
        _save_state(reg_state)
        return reg_state

    return default


def _save_state(state):
    """Save license state with HMAC hash + sync backups"""
    try:
        os.makedirs(os.path.dirname(_LICENSE_FILE), exist_ok=True)
        state_to_save = dict(state)
        state_to_save['_hash'] = _hash_state(state_to_save)
        with open(_LICENSE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state_to_save, f, ensure_ascii=False, indent=2)
        # 同步双备份 + 注册表副本（防删文件重置）
        _save_backup(state)
        _reg_write_state(state_to_save)
    except Exception:
        pass


# ============================================================
# Public API
# ============================================================

def get_machine_id():
    """Get this machine's fingerprint (user sends this to developer)"""
    return _get_machine_id()


def is_licensing_enabled():
    """Check if licensing is active (public key configured)

    Returns False ONLY in explicit dev mode (source code + STK_LICENSE_DEV=1
    or .devmode marker). Otherwise always returns True (fail-closed), even for
    source runs without the dev marker — closing the "unpack EXE and run with
    python" bypass.
    """
    # 源码运行 + 显式开发标记 → 开发模式（免授权）；否则一律走完整授权校验
    if not getattr(sys, 'frozen', False) and _dev_mode_enabled():
        return False
    _load_public_key()  # 含公钥完整性校验（fail-closed）
    return True


def activate(code):
    """Verify and activate a license code

    Returns:
        (success: bool, message: str)
    """
    # 公钥完整性校验（fail-closed）：防攻击者替换 keys.py 公钥后自签激活码绕过
    try:
        _load_public_key()
    except RuntimeError as e:
        return False, str(e)
    payload = _verify_code(code)
    if payload is None:
        return False, "激活码无效或已损坏"

    # Check machine binding
    machine_id = _get_machine_id()
    if payload.get('machine_id', '') != machine_id:
        return False, "激活码已绑定其他设备"

    # Store activation
    state = _load_state()
    state['activation_code'] = code.strip()
    state['activated_at'] = datetime.now().isoformat()
    state['_tampered'] = False

    # Set uses_remaining for per_use licenses
    if payload.get('type') == 'per_use':
        state['uses_remaining'] = payload.get('uses', 0)
    else:
        state['uses_remaining'] = 0

    _save_state(state)

    # Build success message
    license_type = payload.get('type', 'unknown')
    type_labels = {
        'trial': '试用版（30天）',
        'monthly': '月度订阅',
        'yearly': '年度订阅',
        'per_use': f"按次计费（{payload.get('uses', 0)}次）",
        'lifetime': '永久授权',
    }
    label = type_labels.get(license_type, license_type)
    expiry = payload.get('expiry', '')
    msg = f"激活成功：{label}"
    if expiry:
        msg += f"（到期日 {expiry}）"

    return True, msg


def get_license_info():
    """Get current license information

    Returns dict with:
        - status: 'active' / 'expired' / 'no_uses' / 'unlicensed' / 'dev_mode' / 'tampered'
        - type: license type or None
        - expiry: expiry date string or None
        - uses_remaining: int or None
        - days_remaining: int or None

    dev_mode: 源码运行（非 PyInstaller 打包）时自动进入，所有功能不受限制，
    不受激活码影响。仅打包后的 EXE 走正常授权流程。
    """
    state = _load_state()

    # 源码运行（非 PyInstaller 打包）→ 开发模式（最高优先级，豁免 license 文件问题）
    # 防开发调试时被 license.json HMAC 不匹配阻塞；仅 frozen（打包后）才走严格校验。
    if not getattr(sys, 'frozen', False) and _dev_mode_enabled():
        return {
            'status': 'dev_mode',
            'type': 'dev',
            'expiry': None,
            'uses_remaining': None,
            'days_remaining': None,
        }

    # 公钥完整性校验（fail-closed）：即使 _verify_code 被 patch，也先过此关
    try:
        _load_public_key()
    except RuntimeError:
        return {
            'status': 'tampered',
            'type': None,
            'expiry': None,
            'uses_remaining': None,
            'days_remaining': None,
        }

    # 检测到文件篡改（仅 frozen 检查）
    if state.get('_tampered', False):
        return {
            'status': 'tampered',
            'type': None,
            'expiry': None,
            'uses_remaining': None,
            'days_remaining': None,
        }

    # 以下仅打包后（frozen）执行
    code = state.get('activation_code', '')

    if not code:
        return {
            'status': 'unlicensed',
            'type': None,
            'expiry': None,
            'uses_remaining': None,
            'days_remaining': None,
        }

    payload = _verify_code(code)
    if payload is None:
        return {
            'status': 'unlicensed',
            'type': None,
            'expiry': None,
            'uses_remaining': None,
            'days_remaining': None,
        }

    # 机器绑定复查：激活码绑定的 machine_id 须与当前机器一致。
    # 防「复制整个 exe 目录（含 config/license.json）给别人」白嫖已激活授权。
    # 即使 machine_id 缓存被复制、state 文件 HMAC 被绕过，此处也兜底拦截。
    if payload.get('machine_id') and payload['machine_id'] != _get_machine_id():
        return {
            'status': 'unlicensed',
            'type': None,
            'expiry': None,
            'uses_remaining': None,
            'days_remaining': None,
        }

    license_type = payload.get('type', 'unknown')

    # Check expiry for time-based licenses
    if 'expiry' in payload:
        try:
            expiry_date = datetime.strptime(payload['expiry'], '%Y-%m-%d').date()
            today = date.today()
            days_remaining = (expiry_date - today).days

            if days_remaining <= 0:
                return {
                    'status': 'expired',
                    'type': license_type,
                    'expiry': payload['expiry'],
                    'uses_remaining': 0,
                    'days_remaining': 0,
                }

            return {
                'status': 'active',
                'type': license_type,
                'expiry': payload['expiry'],
                'uses_remaining': None,
                'days_remaining': days_remaining,
            }
        except (ValueError, KeyError):
            # 日期格式异常或缺少字段 → 按过期/无效处理
            return {
                'status': 'expired',
                'type': license_type,
                'expiry': payload.get('expiry', 'unknown'),
                'uses_remaining': 0,
                'days_remaining': 0,
            }

    # Check uses for per_use licenses
    if license_type == 'per_use':
        uses = state.get('uses_remaining', 0)
        if uses <= 0:
            return {
                'status': 'no_uses',
                'type': license_type,
                'expiry': None,
                'uses_remaining': 0,
                'days_remaining': None,
            }
        return {
            'status': 'active',
            'type': license_type,
            'expiry': None,
            'uses_remaining': uses,
            'days_remaining': None,
        }

    # Lifetime: 永不过期
    if license_type == 'lifetime':
        return {
            'status': 'active',
            'type': license_type,
            'expiry': None,
            'uses_remaining': None,
            'days_remaining': None,
        }

    # Fallback
    return {
        'status': 'active',
        'type': license_type,
        'expiry': payload.get('expiry'),
        'uses_remaining': None,
        'days_remaining': None,
    }


def _reg_load_date():
    """从注册表读取 first_use_date，失败返回空字符串"""
    if winreg is None:
        return ''
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\M-Bull", 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, "first_use")
            return value if isinstance(value, str) else ''
    except Exception:
        return ''


def _reg_save_date(value):
    """保存 first_use_date 到注册表，失败静默"""
    if winreg is None:
        return
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\M-Bull") as key:
            winreg.SetValueEx(key, "first_use", 0, winreg.REG_SZ, value)
    except Exception:
        pass


def can_use(feature='query'):
    """Check if the given feature can be used.

    All features (quant analysis, watchlist diagnosis, market scan) share
    the same free quota: 10 uses/day during a 7-day trial period.
    After the trial expires, a license is required.

    Note: Real-time quotes and K-line charts are always free (just API data).

    Returns:
        (allowed: bool, reason: str)
    """
    with _state_lock:
        return _can_use_locked(feature)


def _can_use_locked(feature='query'):
    """can_use 的内部实现（调用方须持有 _state_lock）"""
    # 时钟回拨检测（商用加固）：系统时间比上次运行回拨超阈值 → 拒绝（防回拨续期）
    if _detect_clock_rollback():
        return False, '检测到系统时间异常回拨，已冻结授权（请校准系统时间后重启）'

    info = get_license_info()

    # 检测到文件篡改，直接拒绝
    if info['status'] == 'tampered':
        return False, "检测到授权文件被篡改，请联系客服或重新激活。"

    # Active license - all features allowed
    if info['status'] == 'active':
        return True, "已授权"

    # Dev mode - all features allowed, no tracking
    if info['status'] == 'dev_mode':
        return True, "开发模式"

    # Expired / no_uses / unlicensed - free trial tier (all features share quota)
    state = _load_state()
    today_str = date.today().isoformat()
    last_seen = state.get('last_seen_date', '')

    # 系统时间回拨检测
    if last_seen and last_seen > today_str:
        return False, "检测到系统时间异常，请校正系统时间后重启。"

    state['last_seen_date'] = today_str

    # 7天免费试用检查（注册表防删：文件被删时从注册表恢复）
    first_use = state.get('first_use_date', '')
    if not first_use:
        first_use = _reg_load_date()
        if first_use:
            state['first_use_date'] = first_use
    if not first_use:
        first_use = today_str
        state['first_use_date'] = first_use
        _reg_save_date(first_use)
    else:
        # 确保注册表同步有值（防用户仅删文件）
        if not _reg_load_date():
            _reg_save_date(first_use)

    try:
        first_date = datetime.strptime(first_use, '%Y-%m-%d').date()
        trial_days = (date.today() - first_date).days
    except Exception:
        trial_days = 0

    if trial_days >= FREE_TRIAL_DAYS:
        _save_state(state)
        return False, f"免费试用已到期（{FREE_TRIAL_DAYS}天），请激活后继续使用。"

    # 试用期内不限制使用次数（仅剩天数限制）
    if trial_days < FREE_TRIAL_DAYS:
        _save_state(state)
        remaining_trial = FREE_TRIAL_DAYS - trial_days
        return True, f"免费试用（剩余{remaining_trial}天，不限次数）"


def consume_use():
    """Consume one use (for per_use licenses) or increment free counter

    Call this AFTER a successful analysis.
    """
    info = get_license_info()

    # Development mode - no tracking
    if info['status'] == 'dev_mode':
        return

    # 检测到文件篡改，不做任何操作
    if info['status'] == 'tampered':
        return

    # Per_use license - decrement (带二次检查防止超额)
    if info['type'] == 'per_use' and info['status'] == 'active':
        with _state_lock:
            state = _load_state()
            if state.get('uses_remaining', 0) <= 0:
                return
            state['uses_remaining'] = max(0, state.get('uses_remaining', 0) - 1)
            _save_state(state)
        return

    # Unlicensed - increment free counter (带二次检查防止超额)
    if info['status'] in ('unlicensed', 'expired', 'no_uses'):
        with _state_lock:
            state = _load_state()
            today_str = date.today().isoformat()

            if state.get('free_queries_date', '') != today_str:
                state['free_queries_today'] = 0
                state['free_queries_date'] = today_str

            # 二次检查，防止超额
            if state.get('free_queries_today', 0) >= FREE_DAILY_LIMIT:
                return

            state['free_queries_today'] = state.get('free_queries_today', 0) + 1
            state['total_free_uses'] = state.get('total_free_uses', 0) + 1
            _save_state(state)


def try_use(feature='query'):
    """原子化检查并消耗一次使用额度（推荐用于多线程环境）

    合并 can_use + consume_use 为一个原子操作，避免 TOCTOU 竞态条件。

    Returns:
        (allowed: bool, reason: str)
    """
    with _state_lock:
        allowed, reason = can_use(feature)
        if not allowed:
            return False, reason
        consume_use()
        return True, reason


def get_status_text():
    """Get human-readable license status for UI display"""
    info = get_license_info()

    if info['status'] == 'dev_mode':
        return "开发模式"

    if info['status'] == 'tampered':
        return "授权文件异常"

    if info['status'] == 'unlicensed':
        state = _load_state()
        today_str = date.today().isoformat()

        # 计算试用剩余天数
        first_use = state.get('first_use_date', '')
        remaining_trial = FREE_TRIAL_DAYS
        if first_use:
            try:
                first_date = datetime.strptime(first_use, '%Y-%m-%d').date()
                trial_days = (date.today() - first_date).days
                remaining_trial = max(0, FREE_TRIAL_DAYS - trial_days)
            except Exception:
                pass

        if remaining_trial <= 0:
            return "试用已到期"

        return f"试用{remaining_trial}天 不限次数"

    if info['status'] == 'expired':
        # 授权过期后仍可使用免费试用额度
        state = _load_state()
        first_use = state.get('first_use_date', '')
        remaining_trial = FREE_TRIAL_DAYS
        if first_use:
            try:
                first_date = datetime.strptime(first_use, '%Y-%m-%d').date()
                trial_days = (date.today() - first_date).days
                remaining_trial = max(0, FREE_TRIAL_DAYS - trial_days)
            except Exception:
                pass
        if remaining_trial > 0:
            return f"已过期 试用{remaining_trial}天 不限次数"
        return "已过期"

    if info['status'] == 'no_uses':
        # 按次用完后仍可使用免费试用额度
        state = _load_state()
        first_use = state.get('first_use_date', '')
        remaining_trial = FREE_TRIAL_DAYS
        if first_use:
            try:
                first_date = datetime.strptime(first_use, '%Y-%m-%d').date()
                trial_days = (date.today() - first_date).days
                remaining_trial = max(0, FREE_TRIAL_DAYS - trial_days)
            except Exception:
                pass
        if remaining_trial > 0:
            return f"次数用完 试用{remaining_trial}天 不限次数"
        return "次数用完"

    # Active
    type_labels = {
        'trial': '试用',
        'monthly': '月度',
        'yearly': '年度',
        'per_use': '按次',
        'lifetime': '永久',
    }
    label = type_labels.get(info['type'], info['type'] or '已授权')

    if info['days_remaining'] is not None:
        return f"{label} {info['days_remaining']}天"

    if info['uses_remaining'] is not None:
        return f"{label} x{info['uses_remaining']}"

    return label


def deactivate():
    """Remove current license (logout)"""
    state = _load_state()
    state['activation_code'] = ''
    state['uses_remaining'] = 0
    state['activated_at'] = ''
    _save_state(state)


# ============================================================
# 维护说明：重新生成密钥对后必须同步公钥指纹
# ============================================================
# 若用 `python license/keygen.py init` 重新生成 Ed25519 密钥对，
# license/keys.py 的 PUBLIC_KEY_HEX 会变化，本文件的 _EXP_PKH_A/_EXP_PKH_B
# 必须同步更新，否则全部用户会因完整性校验失败被锁定。
# 重算命令（在仓库根目录执行）：
#   python -c "import hashlib,base64; \
#   k=open('license/keys.py').read().split('PUBLIC_KEY_HEX = ')[1].split('\"')[1]; \
#   d=hashlib.sha256(bytes.fromhex(k)).digest(); \
#   b=base64.b64encode(d).decode(); m=len(b)//2; \
#   print('_EXP_PKH_A =', repr(b[:m])); print('_EXP_PKH_B =', repr(b[m:]))"
# 将输出的值代入上方两常量即可。
