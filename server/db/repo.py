# -*- coding: utf-8 -*-
"""方案 / 因子体仓储（能力 5，F-601 / F-602）。

设计要点（2026-09-13 定版）：
- **乐观锁**：`UPDATE ... WHERE name=:name AND version=:expected`，以受影响行数判冲突；
  `rowcount == 0` 时再查一次区分「不存在（404）」与「版本冲突（409，带最新版本）」。
  该写法在 PG 与 SQLite 上行为一致，可跨库真断言。
- **并发建同名**：以主键约束兜底（`flush()` 抛 IntegrityError → DuplicateError），
  线程各自独立会话，互不回滚对方。
- 时间戳取 `server.core.time.now_iso()`（R-19：禁裸 datetime.now）。
- 批量 `update()` 后 `expire_all()`，避免 ORM 身份映射返回陈旧实例。
"""

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from server.core import time as srv_time
from server.db.models import FactorProfile, Scheme

# 允许通过 CAS 更新的方案列（name 是主键不可改；version/updated_at 由仓储自管）
_SCHEME_UPDATABLE = (
    "label",
    "desc",
    "market",
    "direction",
    "period",
    "mode",
    "factor_profile",
    "config",
    "period_configs",
)


class DuplicateError(Exception):
    """目标已存在（方案/因子体同名）→ 409。"""

    def __init__(self, kind: str, name: str):
        super().__init__(f"{kind} 已存在：{name}")
        self.kind = kind
        self.name = name


class NotFoundError(Exception):
    """目标不存在 → 404。"""

    def __init__(self, kind: str, name: str):
        super().__init__(f"{kind} 不存在：{name}")
        self.kind = kind
        self.name = name


class VersionConflict(Exception):
    """乐观锁版本冲突 → 409（携带最新快照，供前端提示刷新）。"""

    def __init__(self, kind: str, name: str, latest: dict | None):
        super().__init__(f"{kind} 版本冲突：{name}")
        self.kind = kind
        self.name = name
        self.latest = latest or {}


# ---------------------------------------------------------------- 方案壳
def _scheme_dict(row: Scheme) -> dict:
    return {
        "name": row.name,
        "label": row.label,
        "desc": row.desc,
        "market": row.market,
        "direction": row.direction,
        "period": row.period,
        "mode": row.mode,
        "factor_profile": row.factor_profile,
        "config": row.config or {},
        "period_configs": row.period_configs or {},
        "version": row.version,
        "updated_at": row.updated_at,
    }


def list_schemes(session) -> list[dict]:
    """§3.2 契约：`[{name, factor_profile, market, direction, period}]`（字段名逐字）。"""
    rows = session.execute(select(Scheme).order_by(Scheme.name)).scalars().all()
    return [
        {
            "name": r.name,
            "factor_profile": r.factor_profile,
            "market": r.market,
            "direction": r.direction,
            "period": r.period,
        }
        for r in rows
    ]


def get_scheme(session, name: str) -> dict | None:
    """返回完整方案（含 config / version）；无则 None。"""
    row = session.execute(select(Scheme).where(Scheme.name == name)).scalar_one_or_none()
    return _scheme_dict(row) if row is not None else None


def create_scheme(session, name: str, **fields) -> dict:
    """建方案壳（version=1）。同名 → DuplicateError。"""
    row = Scheme(
        name=name,
        label=fields.get("label"),
        desc=fields.get("desc"),
        market=fields.get("market"),
        direction=fields.get("direction"),
        period=fields.get("period"),
        mode=fields.get("mode"),
        factor_profile=fields.get("factor_profile"),
        config=fields.get("config") or {},
        period_configs=fields.get("period_configs") or {},
        version=1,
        updated_at=srv_time.now_iso(),
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise DuplicateError("scheme", name) from None
    return _scheme_dict(row)


def delete_scheme(session, name: str) -> bool:
    """删除方案壳；返回是否确有删除。"""
    res = session.execute(delete(Scheme).where(Scheme.name == name))
    return bool(res.rowcount)


def update_scheme_cas(session, name: str, fields: dict, expected_version: int) -> dict:
    """CAS 更新方案壳（F-602 的 PUT 写入路径）。version 不符 → VersionConflict。"""
    values = {k: v for k, v in fields.items() if k in _SCHEME_UPDATABLE}
    values["version"] = Scheme.version + 1
    values["updated_at"] = srv_time.now_iso()
    res = session.execute(
        update(Scheme)
        .where(Scheme.name == name, Scheme.version == expected_version)
        .values(**values)
    )
    if res.rowcount == 0:
        latest = get_scheme(session, name)
        if latest is None:
            raise NotFoundError("scheme", name)
        raise VersionConflict("scheme", name, latest)
    session.expire_all()
    return get_scheme(session, name)


# ---------------------------------------------------------------- 因子体
def get_profile(session, name: str) -> dict | None:
    row = session.execute(
        select(FactorProfile).where(FactorProfile.name == name)
    ).scalar_one_or_none()
    if row is None:
        return None
    return {"name": row.name, "body": row.body or {}, "version": row.version,
            "updated_at": row.updated_at}


def create_profile(session, name: str, body: dict) -> dict:
    """建因子体（version=1）。同名 → DuplicateError。"""
    row = FactorProfile(name=name, body=body or {}, version=1, updated_at=srv_time.now_iso())
    session.add(row)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise DuplicateError("factor_profile", name) from None
    return {"name": row.name, "body": row.body, "version": row.version,
            "updated_at": row.updated_at}


def update_profile_cas(session, name: str, body: dict, expected_version: int) -> dict:
    """CAS 更新因子体。version 不符 → VersionConflict；不存在 → NotFoundError。

    这是 F-601「并发写同一 factor_profile 一成一败」的判定点：两个写者读到同一
    version 后并发提交，受影响行数只有一个为 1，另一个为 0 → 409。
    """
    res = session.execute(
        update(FactorProfile)
        .where(FactorProfile.name == name, FactorProfile.version == expected_version)
        .values(body=body or {}, version=FactorProfile.version + 1,
                updated_at=srv_time.now_iso())
    )
    if res.rowcount == 0:
        latest = get_profile(session, name)
        if latest is None:
            raise NotFoundError("factor_profile", name)
        raise VersionConflict("factor_profile", name, latest)
    session.expire_all()
    return get_profile(session, name)


def ensure_profile(session, name: str, body: dict) -> dict:
    """不存在则建（version=1），已存在则**不改**并返回现状（导入的幂等语义）。"""
    existing = get_profile(session, name)
    if existing is not None:
        return existing
    return create_profile(session, name, body)


# ---------------------------------------------------------------- 迁移 / 互转
def to_engine_model(session) -> dict:
    """把两表还原成 engine `quant_model.json` 形状（供复用 engine 的三级合并）。"""
    schemes = {}
    for r in session.execute(select(Scheme)).scalars().all():
        entry: dict = {"config": r.config or {}}
        if r.label:
            entry["label"] = r.label
        if r.desc:
            entry["desc"] = r.desc
        meta = {}
        for key in ("market", "direction", "period", "mode"):
            val = getattr(r, key)
            if val:
                meta[key] = val
        if meta:
            entry["_meta"] = meta
        if r.factor_profile:
            entry["factor_profile"] = r.factor_profile
        if r.period_configs:
            entry["period_configs"] = r.period_configs
        schemes[r.name] = entry
    profiles = {
        p.name: (p.body or {})
        for p in session.execute(select(FactorProfile)).scalars().all()
    }
    return {"schemes": schemes, "factor_profiles": profiles}


def seed_from_engine_model(session, model: dict) -> dict:
    """一次性导入：把 engine 形状的量化模型灌入两表。

    已存在的行**不覆盖**（幂等，可重复调用）。返回各类计数。
    """
    created_schemes, created_profiles = 0, 0
    for pname, body in (model.get("factor_profiles") or {}).items():
        if get_profile(session, pname) is None:
            create_profile(session, pname, body if isinstance(body, dict) else {})
            created_profiles += 1
    for sname, sc in (model.get("schemes") or {}).items():
        if not isinstance(sc, dict) or get_scheme(session, sname) is not None:
            continue
        meta = sc.get("_meta") if isinstance(sc.get("_meta"), dict) else {}
        create_scheme(
            session,
            sname,
            label=sc.get("label"),
            desc=sc.get("desc"),
            market=meta.get("market"),
            direction=meta.get("direction"),
            period=meta.get("period"),
            mode=meta.get("mode"),
            factor_profile=sc.get("factor_profile"),
            config=sc.get("config") if isinstance(sc.get("config"), dict) else {},
            period_configs=sc.get("period_configs") if isinstance(sc.get("period_configs"), dict) else {},
        )
        created_schemes += 1
    return {"schemes": created_schemes, "profiles": created_profiles}
