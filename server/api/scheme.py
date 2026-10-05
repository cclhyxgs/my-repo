# -*- coding: utf-8 -*-
"""方案管理 API（F-601）—— 能力 5「配置与方案持久化」。

端点（C3 §3.2 逐字对齐）：
  GET    /api/scheme                 → `[{name, factor_profile, market, direction, period}]`
  POST   /api/scheme                 → `201 {name}` ∥ 409 同名 ∥ 422 命名非法
  POST   /api/scheme/import          → `{ok, name, imported[], skipped[]}` ∥ 409 同名 / 422 格式错
  GET    /api/scheme/{name}          → 完整方案（含 config / version）
  PUT    /api/scheme/{name}          → `{ok, version, updated_factors[]}` ∥ 409 版本冲突
  DELETE /api/scheme/{name}          → `204` ∥ `404`
  GET    /api/scheme/{name}/export   → JSON 文件流（`Content-Disposition`，中文名走 RFC 5987）

⚠️ 路由顺序：静态路径 `/scheme/import` 必须注册在 `/scheme/{name}` **之前**，
   否则 FastAPI 会把 `import` 当成 `{name}` 匹配（F-501 的 `/scan/cache` 踩过同坑）。
"""

import json
import threading

from fastapi import APIRouter, File, Form, Response, UploadFile
from pydantic import BaseModel

from server.adapters import engine_bridge
from server.adapters import scheme as scheme_adapter
from server.core.errors import ApiError
from server.core.logging import get_logger
from server.db import repo
from server.db.session import session_scope

logger = get_logger(__name__)
router = APIRouter(tags=["scheme"])


class SchemeCreate(BaseModel):
    """新建方案（C3 契约输入为 `{name}`；其余为可选扩展，便于一次性带上身份标签）。"""

    name: str
    label: str | None = None
    desc: str | None = None
    market: str | None = None
    direction: str | None = None
    period: str | None = None
    mode: str | None = None
    factor_profile: str | None = None
    config: dict | None = None
    period_configs: dict | None = None


class SchemeUpdate(BaseModel):
    """更新方案（`version` 为乐观锁凭据，必填）。"""

    version: int
    label: str | None = None
    desc: str | None = None
    market: str | None = None
    direction: str | None = None
    period: str | None = None
    mode: str | None = None
    factor_profile: str | None = None
    config: dict | None = None
    period_configs: dict | None = None


def _raise_api(exc: Exception) -> None:
    """把仓储异常映射为业务错误码（统一错误体由 errors.py 封装）。"""
    if isinstance(exc, repo.DuplicateError):
        code = "SCHEME_EXISTS" if exc.kind == "scheme" else "PROFILE_EXISTS"
        raise ApiError(code, str(exc), 409) from exc
    if isinstance(exc, repo.NotFoundError):
        code = "SCHEME_NOT_FOUND" if exc.kind == "scheme" else "PROFILE_NOT_FOUND"
        raise ApiError(code, str(exc), 404) from exc
    if isinstance(exc, repo.VersionConflict):
        latest = exc.latest.get("version")
        raise ApiError(
            "VERSION_CONFLICT",
            f"{exc}（最新版本={latest}，请刷新后重试）",
            409,
            headers={"X-Latest-Version": str(latest)},
        ) from exc
    raise exc


# ---------------------------------------------------------------- 首次播种
_seed_lock = threading.Lock()
_seeded = False


def _ensure_seeded() -> None:
    """首次访问时把 engine 现有 `quant_model.json` 灌入两表（幂等、尽力而为）。

    目的：让 F-601 的列表在「有存量配置」的环境里非空。播种失败只记日志、不阻断接口
    ——它不是验收断言，属便捷引导；真正的数据写入由 F-601/F-602 端点负责。
    """
    global _seeded
    if _seeded:
        return
    with _seed_lock:
        if _seeded:
            return
        session = session_scope()
        try:
            if repo.list_schemes(session):
                _seeded = True
                return
            counts = repo.seed_from_engine_model(session, engine_bridge.quant_model_snapshot())
            session.commit()
            if counts["schemes"] or counts["profiles"]:
                logger.info("方案表首次播种：%s", counts)
            _seeded = True
        except Exception:  # noqa: BLE001 —— 播种失败不当硬断言（见 docstring）
            session.rollback()
            logger.exception("方案表播种失败（不影响接口可用性）")
        finally:
            session.close()


# ---------------------------------------------------------------- 端点
@router.get("/scheme", summary="方案列表（F-601）")
def list_scheme() -> list:
    _ensure_seeded()
    session = session_scope()
    try:
        return repo.list_schemes(session)
    finally:
        session.close()


@router.post("/scheme", status_code=201, summary="新建方案（F-601）")
def create_scheme(payload: SchemeCreate) -> dict:
    err = scheme_adapter.validate_scheme_name(payload.name)
    if err:
        raise ApiError("SCHEME_NAME_INVALID", err, 422)
    session = session_scope()
    try:
        try:
            row = repo.create_scheme(
                session,
                payload.name,
                label=payload.label,
                desc=payload.desc,
                market=payload.market,
                direction=payload.direction,
                period=payload.period,
                mode=payload.mode,
                factor_profile=payload.factor_profile,
                config=payload.config or {},
                period_configs=payload.period_configs or {},
            )
            session.commit()
        except Exception as exc:  # noqa: BLE001 —— 统一映射
            session.rollback()
            _raise_api(exc)
        return {"name": row["name"]}
    finally:
        session.close()


@router.post("/scheme/import", summary="导入方案（F-601，multipart，3 格式兼容）")
async def import_scheme(
    file: UploadFile = File(...),
    market: str | None = Form(None),
    direction: str | None = Form(None),
    period: str | None = Form(None),
    scheme_name: str | None = Form(None),
) -> dict:
    raw = await file.read()
    text = raw.decode("utf-8-sig", errors="replace")
    try:
        doc = scheme_adapter.parse_json_text(text)
    except ValueError as exc:
        raise ApiError("SCHEME_IMPORT_INVALID", str(exc), 422) from exc

    session = session_scope()
    try:
        try:
            plan = scheme_adapter.plan_import(
                doc,
                market=market,
                direction=direction,
                period=period,
                scheme_name=scheme_name,
                name_exists=lambda n: repo.get_scheme(session, n) is not None,
            )
        except ValueError as exc:
            raise ApiError("SCHEME_IMPORT_INVALID", str(exc), 422) from exc

        if plan["duplicate"]:
            raise ApiError(
                "SCHEME_EXISTS",
                f"方案名已存在：{plan['duplicate']}，请换一个名称或改用方案库格式导入",
                409,
            )

        imported: list = []
        try:
            # 因子体只做「不存在则建」，不静默覆盖共享 profile（见 adapters/scheme.py 说明）
            for pname, body in plan["profiles"].items():
                repo.ensure_profile(session, pname, body)
            for entry in plan["entries"]:
                fields = {
                    "label": entry["label"],
                    "desc": entry["desc"],
                    "market": entry["meta"].get("market"),
                    "direction": entry["meta"].get("direction"),
                    "period": entry["meta"].get("period"),
                    "mode": entry["meta"].get("mode"),
                    "factor_profile": entry["factor_profile"],
                    "config": entry["config"],
                }
                existing = repo.get_scheme(session, entry["name"]) if entry["overwrite"] else None
                if existing is not None:
                    repo.update_scheme_cas(session, entry["name"], fields, existing["version"])
                else:
                    repo.create_scheme(session, entry["name"], **fields)
                imported.append(entry["name"])
            session.commit()
        except Exception as exc:  # noqa: BLE001 —— 整请求事务：任一步失败整体回滚，无残留
            session.rollback()
            _raise_api(exc)
    finally:
        session.close()

    return {
        "ok": True,
        "name": imported[0] if imported else None,
        "imported": imported,
        "skipped": plan["skipped"],
    }


@router.get("/scheme/{name}", summary="方案详情（F-601 / F-602 数据面）")
def get_scheme(name: str) -> dict:
    session = session_scope()
    try:
        row = repo.get_scheme(session, name)
    finally:
        session.close()
    if row is None:
        raise ApiError("SCHEME_NOT_FOUND", f"方案不存在：{name}", 404)
    return row


@router.put("/scheme/{name}", summary="更新方案（F-601，乐观锁 CAS）")
def update_scheme(name: str, payload: SchemeUpdate) -> dict:
    fields = payload.model_dump(exclude_unset=True, exclude={"version"})
    session = session_scope()
    try:
        try:
            row = repo.update_scheme_cas(session, name, fields, payload.version)
            session.commit()
        except Exception as exc:  # noqa: BLE001 —— 统一映射
            session.rollback()
            _raise_api(exc)
    finally:
        session.close()
    config = fields.get("config") or {}
    factors = sorted((config.get("factor_configs") or {}).keys()) if isinstance(config, dict) else []
    return {"ok": True, "version": row["version"], "updated_factors": factors}


@router.delete("/scheme/{name}", status_code=204, summary="删除方案（F-601）")
def delete_scheme(name: str) -> Response:
    session = session_scope()
    try:
        ok = repo.delete_scheme(session, name)
        session.commit()
    finally:
        session.close()
    if not ok:
        raise ApiError("SCHEME_NOT_FOUND", f"方案不存在：{name}", 404)
    return Response(status_code=204)


@router.get("/scheme/{name}/export", summary="导出方案 JSON（F-601）")
def export_scheme(name: str) -> Response:
    session = session_scope()
    try:
        row = repo.get_scheme(session, name)
    finally:
        session.close()
    if row is None:
        raise ApiError("SCHEME_NOT_FOUND", f"方案不存在：{name}", 404)
    body = json.dumps(
        scheme_adapter.export_payload(row), ensure_ascii=False, indent=2
    ).encode("utf-8")
    filename = scheme_adapter.export_filename(name)
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": scheme_adapter.content_disposition(filename)},
    )
