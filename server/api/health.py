# -*- coding: utf-8 -*-
"""部署健康检查端点（B3 授权新增）。

**不属 C3 §3.2 业务清单，不进 migration-plan 的 F-xxx 排期。**
用途：容器/网关探活 + 把 §2.13 三条硬约束的可验证状态暴露出来。

硬断言纪律（B2）：以下任一不合格 → 503，且 reasons 明确列出，不允许降级为日志：
- §2.13-1 运行时依赖版本 != 锁清单
- §2.8   engine 不可 import（含因子注册表不可枚举）
"""

from fastapi import APIRouter, Response

from server import settings
from server.adapters import engine_bridge
from server.core import lockfile
from server.core import time as srv_time

router = APIRouter(tags=["_internal"])


@router.get(
    "/health",
    summary="部署健康检查（非业务端点）",
    response_description="200=ok；503=degraded（依赖版本漂移或 engine 不可导入）",
)
def health(response: Response) -> dict:
    deps = lockfile.verify_locked_deps()
    engine = engine_bridge.engine_selfcheck()

    reasons = list(deps["reasons"])
    if not engine.get("importable"):
        reasons.append(f"engine 不可导入：{engine.get('error')}")

    ok = not reasons
    response.status_code = 200 if ok else 503

    return {
        "status": "ok" if ok else "degraded",
        "version": settings.APP_VERSION,
        "time": srv_time.now_iso(),
        "tz": settings.TIMEZONE,
        "reasons": reasons,
        "engine": engine,
        "deps": deps["installed"],
    }
