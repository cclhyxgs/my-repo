# -*- coding: utf-8 -*-
"""FastAPI 应用工厂与启动入口。

启动：`uvicorn server.main:app --host 0.0.0.0 --port 8000 --workers 1`
（**必须从项目根启动**，保证 `import engine.*` 可用；容器 WORKDIR=/app 即项目根）

单副本约束：C1#2 规格 S —— R-09（quant_config._cache / _diag_tasks / _scan_tasks /
self._market_type 等进程级全局状态）未全部外置前，禁止 `--workers>1` 或多副本。
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from server import settings
from server.adapters import engine_bridge
from server.api.router import api_router, internal_router
from server.core.errors import register_error_handlers
from server.core.logging import configure_logging, get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "启动 M-Bull H5 服务：version=%s tz=%s env=%s probe=%s",
        settings.APP_VERSION,
        settings.TIMEZONE,
        "production" if settings.IS_PRODUCTION else "development",
        settings.INTERNAL_PROBE_ENABLED,
    )

    check = engine_bridge.engine_selfcheck()
    if check.get("importable"):
        logger.info(
            "engine 自检通过：因子 %s 个 / py 模块 %s 个 / app_dir=%s",
            check["factors"],
            check["py_modules"],
            check["app_dir"],
        )
    else:
        # 不抛异常、不吞错：显式 CRITICAL 落日志，并由 /api/health 返回 503 暴露给运维
        logger.critical("engine 自检失败，/api/health 将返回 503：%s", check.get("error"))

    yield
    logger.info("停止 M-Bull H5 服务")


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(
        title="M-Bull H5 API",
        version=settings.APP_VERSION,
        openapi_url=settings.OPENAPI_URL,
        # 交互式文档本次不开：避免占用 /api/docs 命名空间（该路径 §3.1 能力 9 另有归属）
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    register_error_handlers(app)
    app.include_router(api_router, prefix=settings.API_PREFIX)
    app.include_router(internal_router, prefix=settings.INTERNAL_PREFIX)
    _mount_spa(app)
    return app


def _mount_spa(app: FastAPI) -> None:
    """同源托管 H5 SPA（分支 A：FastAPI 既提供 /api 又提供静态前端）。

    开关：MBULL_SERVE_SPA=1 才启用；默认关闭（纯 API 模式由 nginx/CDN 托管前端）。
    安全要点：
      - catch-all 对 /api 前缀路径返回 404 JSON，不吞掉 API 的未知路由；
      - 静态文件回退前校验候选路径落在 dist 内，防 ``..`` 路径穿越。
    """
    if not settings.SERVE_SPA:
        return

    dist_root = settings.SPA_DIST
    index_html = dist_root / "index.html"
    if not index_html.is_file():
        logger.warning("SERVE_SPA 已开启但找不到 %s，跳过 SPA 托管", index_html)
        return

    assets_dir = dist_root / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="spa-assets")

    dist_root_resolved = dist_root.resolve()

    def _resolve_static(rel_path: str) -> Path | None:
        if not rel_path:
            return None
        candidate = (dist_root / rel_path).resolve()
        # 防路径穿越：候选必须是 dist 目录的直接/间接子文件
        if candidate.is_file() and dist_root_resolved in candidate.parents:
            return candidate
        return None

    @app.get("/")
    async def _spa_root(request: Request):
        if request.url.path.startswith(settings.API_PREFIX):
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(str(index_html))

    @app.get("/{full_path:path}")
    async def _spa_catch_all(request: Request, full_path: str = ""):
        # /api 空间（含 /api/_probe）的未知路径必须保留 404 JSON 行为，不能被 SPA 吞掉
        if request.url.path.startswith(settings.API_PREFIX):
            raise HTTPException(status_code=404, detail="Not Found")
        static_file = _resolve_static(request.url.path.strip("/"))
        if static_file is not None:
            return FileResponse(str(static_file))
        return FileResponse(str(index_html))


app = create_app()
