# -*- coding: utf-8 -*-
"""统一错误响应（骨架期）。

约定：错误体固定为 `{"error": {"code", "message"}, "ts"}`，便于 H5 端统一处理。
业务端点（F-xxx）落地时复用本模块，不要各自拼错误结构。
"""

from server.core import time as srv_time
from server.core.logging import get_logger

logger = get_logger(__name__)

# ⚠️ fastapi / starlette 一律【函数内延迟导入】，不得放模块顶层（2026-09-27）。
# 原因：桌面端 ui/web_api.py 只用到本文件的 ApiError（纯异常类，零依赖），但它
# 走的是「函数内 from server.core.errors import ApiError」——PyInstaller 静态扫描
# 不到，一旦顶层挂着 fastapi，就会把 fastapi + starlette + pydantic 整条依赖树
# 拖进 dist/M-Bull.exe（体积上涨、且这些包桌面端根本用不到）。
# 更致命的是：server 包此前未登记进 hiddenimports，frozen 下 import 直接失败，
# 表现为桌面端「AI 调用失败/配置保存报错」而非明确的 ModuleNotFoundError。


class ApiError(Exception):
    """业务可预期错误。

    `headers` 用于需要附加响应头的场景（如限流的 `Retry-After`，F-305）。
    """

    def __init__(self, code: str, message: str, status_code: int = 400, headers: dict = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.headers = headers or {}


def _payload(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}, "ts": srv_time.now_iso()}


def register_error_handlers(app) -> None:
    # 延迟导入：仅 FastAPI 服务（server/main.py）会走这里，桌面端无需这些依赖。
    from fastapi import Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from starlette.exceptions import HTTPException as StarletteHTTPException

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError):
        return JSONResponse(
            status_code=exc.status_code,
            content=_payload(exc.code, exc.message),
            headers=exc.headers or None,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content=_payload(f"HTTP_{exc.status_code}", str(exc.detail)),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content=_payload("VALIDATION_ERROR", str(exc.errors())))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        # 不吞：落结构化日志（含堆栈）+ 返回 500
        logger.error("未处理异常 %s %s", request.method, request.url.path, exc_info=exc)
        return JSONResponse(status_code=500, content=_payload("INTERNAL_ERROR", f"{type(exc).__name__}"))
