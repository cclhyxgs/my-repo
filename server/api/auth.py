# -*- coding: utf-8 -*-
"""账号授权 / 状态 API（能力 8：F-1401 / F-104 / F-1402 / F-1406）。

契约（app-architecture.md §3.2）逐字对齐：
  POST /api/auth/register|login|refresh|logout → JWT(Access+Refresh) + HttpOnly device cookie
  GET  /api/app-info        → `{version, account, license, device_id}`（不含 machine_id）
  GET  /api/license/info    → `{status, tier, expires_at, trial_first_use_at, trial_days_left}`
  GET  /api/quota           → `{tier, status, expires_at, unlimited:true}`
  GET  /api/account/devices → `[{device_id, ua, bound_at, last_seen_at, current}]`
  DELETE /api/account/devices/{id} → `204`（解绑）

失败语义：注册重名 409；密码过短 422；登录验证失败 401；绑定超上限 403；
无效会话访问受保护端点 401；订阅/试用到期 403。
"""

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from server import settings
from server.core import auth as s_auth
from server.db import auth_repo
from server.db.models import Device
from server.db.session import session_scope

router = APIRouter(tags=["auth"])


class CredentialBody(BaseModel):
    identifier: str
    pwd: str


def _signed_tokens(account_id: int) -> dict:
    return {
        "access_token": s_auth.encode_access_token(account_id),
        "refresh_token": s_auth.encode_refresh_token(account_id),
        "token_type": "bearer",
    }


def _set_session(request: Request, response: Response, account_id: int,
                 device_token: str) -> dict:
    """回写会话：device token 落 HttpOnly cookie + body 携带 access/refresh JWT。"""
    s_auth.build_device_cookie(request, response, device_token)
    payload = _signed_tokens(account_id)
    payload["device_cookie"] = settings.DEVICE_COOKIE
    return payload


def _license_snapshot(auth: dict) -> dict:
    """`{status, tier, expires_at, trial_first_use_at, trial_days_left}`。"""
    lic = auth.get("license") or {}
    t_start = lic.get("trial_first_use_at") or auth["account"].get("created_at")
    return {
        "status": lic.get("status", "trial"),
        "tier": lic.get("tier"),
        "expires_at": lic.get("expires_at"),
        "trial_first_use_at": t_start,
        "trial_days_left": max(0, s_auth.TRIAL_DAYS - s_auth._used_days(t_start)),
    }


@router.post("/auth/register", summary="注册并首次登录")
def register(req: CredentialBody, request: Request, response: Response) -> dict:
    if not req.identifier or len(req.pwd) < 6:
        raise s_auth.ApiError("INVALID_CREDENTIALS", "密码至少 6 位。", status_code=422)
    acc = s_auth.register(req.identifier, req.pwd)
    return _set_session(request, response, acc["account_id"], acc["device_token"])


@router.post("/auth/login", summary="账号登录（设备绑定/续触）")
def login(req: CredentialBody, request: Request, response: Response) -> dict:
    acc = s_auth.authenticate(req.identifier, req.pwd)
    return _set_session(request, response, acc["account_id"], acc["device_token"])


@router.post("/auth/refresh", summary="刷新令牌")
def refresh(auth: dict = Depends(s_auth.get_required_auth)) -> dict:
    return s_auth.refresh_account(auth)


@router.post("/auth/logout", summary="注销当前设备（解绑）")
def logout(response: Response, auth: dict = Depends(s_auth.get_required_auth)) -> dict:
    s_auth.logout(auth)
    response.delete_cookie(settings.DEVICE_COOKIE, path="/")
    return {"ok": True}


@router.get("/app-info", summary="版本与应用信息（F-104，不含 machine_id）")
def app_info(auth: dict = Depends(s_auth.get_auth)) -> dict:
    if auth is None:
        return {"version": settings.APP_VERSION, "account": None,
                "license": None, "device_id": None}
    return {
        "version": settings.APP_VERSION,
        "account": {"id": auth["account"]["id"], "identifier": auth["account"]["identifier"]},
        "license": _license_snapshot(auth),
        "device_id": auth["device"]["id"],
    }


@router.get("/license/info", summary="授权状态（F-1401 / F-1402）")
def license_info(auth: dict = Depends(s_auth.get_required_auth)) -> dict:
    s_auth.verify_valid_license(auth)
    return _license_snapshot(auth)


@router.get("/quota", summary="订阅配额（F#4 订阅制：无计次分母）")
def quota(auth: dict = Depends(s_auth.get_required_auth)) -> dict:
    lic = s_auth.verify_valid_license(auth)
    return {"tier": lic.get("tier"), "status": lic.get("status"),
            "expires_at": lic.get("expires_at"), "unlimited": True}


@router.get("/account/devices", summary="已绑定设备列表（F-1403）")
def list_devices(auth: dict = Depends(s_auth.get_required_auth)) -> list[dict]:
    with session_scope() as session:
        rows = auth_repo.list_active_devices(session, auth["account"]["id"])
    return [
        {"device_id": r["id"], "ua": r.get("ua") or "", "bound_at": r["bound_at"],
         "last_seen_at": r["last_seen_at"], "current": r["id"] == auth["device"]["id"]}
        for r in rows
    ]


@router.delete("/account/devices/{device_id}", summary="解绑指定设备")
def revoke_device(device_id: int, response: Response,
                  auth: dict = Depends(s_auth.get_required_auth)) -> Response:
    with session_scope() as session:
        rows = auth_repo.list_active_devices(session, auth["account"]["id"])
        target = next((r for r in rows if r["id"] == device_id), None)
        if target is None:
            raise s_auth.ApiError("NOT_FOUND", "设备不存在或不属于当前账号。", status_code=404)
        if target["id"] == auth["device"]["id"]:
            raise s_auth.ApiError("INVALID_OPERATION", "不能解绑当前使用的设备，请用退出登录。",
                                  status_code=422)
        row = session.get(Device, target["id"])
        row.revoked_at = s_auth._utc_now_iso()
        session.commit()
    response.status_code = 204
    return response