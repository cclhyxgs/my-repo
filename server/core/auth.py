# -*- coding: utf-8 -*-
"""账号维度认证依赖与工具（能力 8，F-1401 / P2a）。

判定主体 = 账号维度（F#2 试用按首次注册时间）；鉴权载体 = JWT(Access+Refresh)
+ HttpOnly 设备 cookie（app-architecture.md:69/435-441）。

JWT 用标准库自研 HS256（零依赖，避免给 lockfile 的 VERIFIABLE 增项 → 强制安装）：
    header.payload.signature (base64url, 无 padding)；
    签名 = HMAC-SHA256(secret, f"{header_b64}.{payload_b64}")。

设备 cookie：HttpOnly 明文 device token（随机 32B）；服务端仅存其 SHA-256
（db/auth_repo 唯一键），不落明文。请求读取 cookie → sha256 → 命中 device 行。

只做 F-1401 范围（register/login/refresh/logout + app-info/license/quota/devices）。
3 处 adapter 的强制执行迁移刻意排除（见 Feature F-1401 验收口径），P0 的设备维度
`server.adapters.auth.enforce` 在就位前保留，复用其 monkeypatch 不冲突。
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from fastapi import Request

from server import settings
from server.core.errors import ApiError
from server.db import auth_repo
from server.db.session import session_scope

# ---------------------------------------------------------------- 密码
_PBKDF2_ITERATIONS = 200_000

# F#2 免费试用时长（天，按账号首次注册时间起算；换设备不重置）
TRIAL_DAYS = 30


def hash_password(pwd: str) -> str:
    """self-describing PBKDF2 摘要：`pbkdf2:sha256:200000:<salt_hex>:<hash_hex>`。"""
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", pwd.encode("utf-8"), salt, _PBKDF2_ITERATIONS
    )
    return (
        f"pbkdf2:sha256:{_PBKDF2_ITERATIONS}:"
        f"{salt.hex()}:{digest.hex()}"
    )


def verify_password(pwd: str, stored: str) -> bool:
    """常数时间比对签名，内容不可伪造；格式不符一律 False。"""
    try:
        algo, name, iterations_s, salt_hex, hash_hex = stored.split(":")
        if (algo, name) != ("pbkdf2", "sha256"):
            return False
        iterations = int(iterations_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


# ---------------------------------------------------------------- JWT (stdlib HS256)
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _sign(payload_b64: str, secret: str) -> str:
    return _b64url(hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"),
                            hashlib.sha256).digest())


def encode_jwt(claims: dict, secret: str, ttl: int) -> str:
    """签发 HS256 JWT：负载含 `exp`（绝对到期秒）。"""
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"},
                                separators=(",", ":")).encode("utf-8"))
    payload = claims | {"iat": int(time.time()), "exp": int(time.time()) + ttl}
    payload_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{header}.{payload_b64}.{_sign(header + '.' + payload_b64, secret)}"


def decode_jwt(token: str, secret: str) -> dict | None:
    """验签+过期解析；不合法/过期 → None。"""
    try:
        header_b64, payload_b64, sig = token.split(".")
    except ValueError:
        return None
    if not hmac.compare_digest(_sign(f"{header_b64}.{payload_b64}", secret), sig):
        return None
    try:
        claims = json.loads(_b64url_decode(payload_b64))
    except (json.JSONDecodeError, base64.binascii.Error, ValueError):
        return None
    if "exp" in claims and int(claims["exp"]) < int(time.time()):
        return None
    return claims


def encode_access_token(account_id: int, secret: str = "") -> str:
    return encode_jwt({"sub": str(account_id), "typ": "access"},
                      secret or settings.resolve_auth_secret(), settings.AUTH_ACCESS_TTL)


def encode_refresh_token(account_id: int, secret: str = "") -> str:
    return encode_jwt({"sub": str(account_id), "typ": "refresh"},
                      secret or settings.resolve_auth_secret(), settings.AUTH_REFRESH_TTL)


# ---------------------------------------------------------------- 设备 token
def new_device_token() -> str:
    return secrets.token_hex(32)


def hash_device_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def build_device_cookie(request: Request, response, device_token: str) -> None:
    """HttpOnly + SameSite=Lax + Path=/ 设备 cookie（明文 token；服务端只存 hash）。"""
    response.set_cookie(
        key=settings.DEVICE_COOKIE,
        value=device_token,
        max_age=settings.AUTH_REFRESH_TTL,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )


# ---------------------------------------------------------------- 服务层（register/login/…）
def register(identifier: str, pwd: str) -> dict:
    """注册 + 建默认试用授权 + 绑定首台设备。identifier 已存在 → ApiError 409。"""
    device_token = new_device_token()
    try:
        with session_scope() as session:
            acc = auth_repo.create_account(session, identifier, hash_password(pwd))
            auth_repo.create_license(session, acc["id"], tier="trial")
            try:
                auth_repo.bind_or_touch_device(session, acc["id"], hash_device_token(device_token), "register")
            except auth_repo.DeviceLimit:
                pass  # 首台必然在 N=2 内，不会触发；留兜底避免在注册路径抛 403
            session.commit()
    except auth_repo.DuplicateError:
        raise ApiError("ACCOUNT_EXISTS", "该账号已注册，请直接登录。", status_code=409) from None
    return {"account_id": acc["id"], "identifier": identifier, "device_token": device_token}


def authenticate(identifier: str, pwd: str) -> dict:
    """登录校验密码 → 生成新设备 token 并绑定/续触（每次登录轮换 cookie）。"""
    with session_scope() as session:
        acc = auth_repo.find_by_identifier(session, identifier)
        if acc is None or not verify_password(pwd, acc["pwd_hash"]):
            raise ApiError("INVALID_CREDENTIALS", "账号或密码错误。", status_code=401)
        if acc["status"] != "active":
            raise ApiError("ACCOUNT_DISABLED", "账号已停用。", status_code=403)
        device_token = new_device_token()
        try:
            auth_repo.bind_or_touch_device(session, acc["id"], hash_device_token(device_token), "")
            session.commit()
        except auth_repo.DeviceLimit as exc:
            raise ApiError("DEVICE_LIMIT", str(exc), status_code=403) from None
    return {"account_id": acc["id"], "identifier": acc["identifier"], "device_token": device_token}


def refresh_account(auth: dict) -> dict:
    """刷新令牌（受保护端点校验会话有效性后调用）。返回新 access_token。"""
    return {"access_token": encode_access_token(auth["account"]["id"])}


def logout(auth: dict) -> None:
    """注销当前设备（软删除 revoked_at）。"""
    with session_scope() as session:
        auth_repo.revoke_device(session, auth["account"]["id"], auth["device_token_hash"])
        session.commit()


# ---------------------------------------------------------------- FastAPI 依赖
def _materialize(request: Request) -> dict | None:
    """从 HttpOnly cookie 读 device token → 命中 device 行 → 物化 account+license。

    返回 `AuthContext`；无 cookie 或设备失效 → None（调用方决定放行/401）。
    """
    raw = request.cookies.get(settings.DEVICE_COOKIE)
    if not raw:
        return None
    dh = hash_device_token(raw)
    with session_scope() as session:
        dv = auth_repo.find_device_by_hash(session, dh)
        if dv is None or dv.get("revoked_at"):
            return None
        account = auth_repo.get_account(session, dv["account_id"])
        if account is None:
            return None
        license_ = auth_repo.get_account_license(session, account["id"])
        return {
            "account": account,
            "device": dv,
            "device_token": raw,
            "device_token_hash": dh,
            "license": license_ or {"status": "none", "tier": None,
                                    "trial_first_use_at": None, "expires_at": None},
        }


def get_auth(request: Request) -> dict | None:
    """宽松依赖：有合法会话返回 AuthContext，无则 None（供 app-info 等降级）。"""
    return _materialize(request)


def get_required_auth(request: Request) -> dict:
    """强依赖：无合法会话 → 401 AUTH_REQUIRED。受保护端点（license/info、devices）用。"""
    ctx = _materialize(request)
    if ctx is None:
        raise ApiError("AUTH_REQUIRED", "未登录或会话已失效，请先登录。", status_code=401)
    return ctx


def verify_valid_license(auth: dict) -> dict:
    """订阅/试用有效性判定 → 到期 403。供受保护端点复用。

    - trial（F#2）：`trial_first_use_at`（= 首注时间，账号维度）起算 TRIAL_DAYS 天，
      超期 → 403 LICENSE_EXPIRED。
    - 订阅制（F#4）：`expires_at` 存 `now_iso()` ISO 串（统一 +08:00，可字典序比较），
      未过当前 ISO 即有效。
    """
    lic = auth.get("license") or {}
    status = lic.get("status")
    if status in (None, "none"):
        raise ApiError("LICENSE_EXPIRED", "无有效授权，请激活后继续使用。", status_code=403)
    if status == "trial":
        start = lic.get("trial_first_use_at") or auth.get("account", {}).get("created_at")
        if not start or _used_days(start) >= TRIAL_DAYS:
            raise ApiError(
                "LICENSE_EXPIRED",
                f"免费试用已到期（{TRIAL_DAYS} 天），请激活后继续使用。",
                status_code=403,
            )
        return lic
    expires_at = lic.get("expires_at")
    if not expires_at or expires_at < _utc_now_iso():
        raise ApiError("LICENSE_EXPIRED", "订阅已到期，请续费后继续使用。", status_code=403)
    return lic


def require_license(request: Request) -> dict:
    """业务端点授权强制依赖（F-1404）：无会话 → 401；有会话但到期 → 403。

    F-1404 把 3 处 adapter 的设备维度 `authorize_query` 迁移到账号维度端点依赖：
    免登录访问 → 401 AUTH_REQUIRED；登录但试用/订阅到期 → 403 LICENSE_EXPIRED。
    订阅制下不按次扣减，只做有效性判定。
    """
    return _license_gate(request)


def _license_gate(request: Request) -> dict:
    """依赖开关函数（隔离 seam）：即物化会话 → 401/到期 403。

    `require_license` 在路由注册期被 `Depends` 绑定为函数对象，monkeypatch
    其模块属性不生效；因此真实逻辑收敛到本函数（运行时按模块全局查表），
    供测试 `_allow_license` 与 `test_license_auth.py` 精确替换触发路径。
    """
    ctx = _materialize(request)
    if ctx is None:
        raise ApiError("AUTH_REQUIRED", "未登录或会话已失效，请先登录。", status_code=401)
    verify_valid_license(ctx)
    return ctx


def _used_days(iso_start: str) -> int:
    """自 ISO 起算已用天数（对齐 core/time 时区语义）。"""
    from datetime import datetime
    try:
        start = datetime.fromisoformat(iso_start)
        return max(0, (datetime.now(start.tzinfo) - start).days)
    except (ValueError, TypeError):
        return 0


def _utc_now_iso() -> str:
    """当前 ISO 串（与列同源 now_iso()，供字典序比较）。"""
    from server.core import time as srv_time
    return srv_time.now_iso()