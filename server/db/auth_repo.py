# -*- coding: utf-8 -*-
"""授权三表仓储（能力 8，F-1401 / P2a）。

判定主体 = 账号维度：
- F#2 试用按首次注册时间，换设备不重置 → `license.trial_first_use_at` 只在首次注册时写。
- F#3 设备绑定上限 N=2 → 以「未 revoked 的 device 行数」判定，超出抛 `DeviceLimit`。
- F#4 订阅制：`license` 无计次列。

对比 repo.py（方案/因子体）：
- 账号无乐观锁版本列，唯一性靠 `identifier` 唯一索引（IntegrityError → DuplicateError）。
- `Device` 的单设备唯一键 = HttpOnly cookie 明文 token 的 SHA-256（不落明文）。
时间列统一 `server.core.time.now_iso()`。
"""

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from server.core import time as srv_time
from server.db.models import Account, Device, License
from server.db.repo import DuplicateError, NotFoundError

# 设备绑定上限（F#3 冻结决策）
DEVICE_BIND_LIMIT = 2


class DeviceLimit(Exception):
    """绑定设备已达上限 → 403。"""

    def __init__(self, limit: int = DEVICE_BIND_LIMIT):
        super().__init__(f"绑定设备已达上限（{limit} 台），请先在旧设备退出或解绑。")
        self.limit = limit


def _account_dict(row) -> dict:
    return {
        "id": row.id,
        "identifier": row.identifier,
        "status": row.status,
        "created_at": row.created_at,
    }


def _device_dict(row) -> dict:
    return {
        "id": row.id,
        "account_id": row.account_id,
        "ua": row.ua,
        "bound_at": row.bound_at,
        "last_seen_at": row.last_seen_at,
        "revoked_at": row.revoked_at,
    }


def _license_dict(row) -> dict:
    return {
        "account_id": row.account_id,
        "tier": row.tier,
        "status": row.status,
        "expires_at": row.expires_at,
        "trial_first_use_at": row.trial_first_use_at,
        "sub_cycle": row.sub_cycle,
    }


# ---------------------------------------------------------------- 账号
def create_account(session, identifier: str, pwd_hash: str) -> dict:
    """建账号。identifier 已存在 → DuplicateError（409）。"""
    row = Account(
        identifier=identifier,
        pwd_hash=pwd_hash,
        status="active",
        created_at=srv_time.now_iso(),
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise DuplicateError("account", identifier) from None
    return _account_dict(row)


def find_by_identifier(session, identifier: str) -> dict | None:
    """按登录标识取账号（含 id / pwd_hash）；无则 None。"""
    row = session.execute(
        select(Account).where(Account.identifier == identifier)
    ).scalar_one_or_none()
    if row is None:
        return None
    d = _account_dict(row)
    d["pwd_hash"] = row.pwd_hash
    return d


def get_account(session, account_id: int) -> dict | None:
    row = session.get(Account, account_id)
    return _account_dict(row) if row is not None else None


# ---------------------------------------------------------------- 授权
def create_license(session, account_id: int, tier: str = "trial") -> dict:
    """为新账号建默认授权（免费试用；trial_first_use_at 记首次注册时间 F#2）。"""
    now = srv_time.now_iso()
    row = License(account_id=account_id, tier=tier, status="trial",
                  trial_first_use_at=now)
    session.add(row)
    session.flush()
    return _license_dict(row)


def get_account_license(session, account_id: int) -> dict | None:
    row = session.execute(
        select(License).where(License.account_id == account_id)
    ).scalar_one_or_none()
    return _license_dict(row) if row is not None else None


# ---------------------------------------------------------------- 设备
def find_device_by_hash(session, device_token_hash: str) -> dict | None:
    row = session.execute(
        select(Device).where(Device.device_token_hash == device_token_hash)
    ).scalar_one_or_none()
    return _device_dict(row) if row is not None else None


def active_device_count(session, account_id: int) -> int:
    """未 revoked 的设备数（F#3：绑定计数按此判定）。"""
    return session.execute(
        select(func.count(Device.id)).where(
            Device.account_id == account_id, Device.revoked_at.is_(None)
        )
    ).scalar_one()


def list_active_devices(session, account_id: int) -> list[dict]:
    rows = session.execute(
        select(Device)
        .where(Device.account_id == account_id, Device.revoked_at.is_(None))
        .order_by(Device.bound_at.desc())
    ).scalars().all()
    return [_device_dict(r) for r in rows]


def bind_or_touch_device(session, account_id: int, device_token_hash: str,
                         ua: str | None) -> dict:
    """绑定或续触：已存在 → 更新 last_seen_at；否则新建（超上限 → DeviceLimit）。"""
    existing = find_device_by_hash(session, device_token_hash)
    now = srv_time.now_iso()
    if existing is not None:
        if existing["account_id"] != account_id:
            raise NotFoundError("device", device_token_hash)
        row = session.get(Device, existing["id"])
        row.last_seen_at = now
        session.flush()
        return _device_dict(row)

    if active_device_count(session, account_id) >= DEVICE_BIND_LIMIT:
        raise DeviceLimit(DEVICE_BIND_LIMIT)

    row = Device(
        account_id=account_id,
        device_token_hash=device_token_hash,
        ua=(ua or "")[:255],
        bound_at=now,
        last_seen_at=now,
        revoked_at=None,
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise NotFoundError("device", device_token_hash) from None
    return _device_dict(row)


def revoke_device(session, account_id: int, device_token_hash: str) -> bool:
    """注销设备（软删除 revoked_at）。返回是否确有注销。"""
    cur = session.execute(
        select(Device).where(
            Device.account_id == account_id,
            Device.device_token_hash == device_token_hash,
            Device.revoked_at.is_(None),
        )
    ).scalar_one_or_none()
    if cur is None:
        return False
    cur.revoked_at = srv_time.now_iso()
    session.flush()
    return True