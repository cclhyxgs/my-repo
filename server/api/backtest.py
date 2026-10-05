# -*- coding: utf-8 -*-
"""回测端点（F-105 任务队列底座 / 回测子进程协议）。

契约（app-architecture.md:355-358 能力 3）：
  POST `/api/backtest`                → `{task_id}`（非法 mode 4xx）
  GET  `/api/backtest/{id}/events`    → SSE（`progress`/`done`/`error` 事件流）
  POST `/api/backtest/{id}/cancel`    → `{status:'cancelled'}`（协作式中断，≤1 只检查点）

F-105 只交付「协议底座 + mock 载体任务」（验证 task_id / SSE 单调递增 / 取消语义）；
真实回测执行（mode 校验、初级 403、参数映射）由 F-701/F-702 接入。
"""

import asyncio
import copy

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from server.core import auth as s_auth, sse, task_store
from server.core.task_store import TERMINAL_STATUSES
from server.worker.executor import get_executor

router = APIRouter(tags=["backtest"])

_VALID_MODES = ("strategy", "scoreic", "factoric", "futures", "futuresic")  # F-701 5 模式


class BacktestRequest(BaseModel):
    mode: str = Field("factoric", description="回测模式（F-105 仅 factoric）")
    run_params: dict = Field(default_factory=dict, description="运行参数（载体任务取 total/step_delay）")


class IcApplyRequest(BaseModel):
    """应用因子 IC 结果（F-704）：`factor_profiles[fp].factor_configs[f]` ← ic。"""

    profile: str
    version: int = Field(description="乐观锁凭据（客户端读到的因子体版本）")
    factors: dict = Field(default_factory=dict, description="{factor_key: ic_value}")


def _raise_profile_api(exc: Exception) -> None:
    """把因子体仓储异常映射为业务错误码（F-704，与 scheme._raise_api 同口径）。"""
    from server.core.errors import ApiError

    from server.db import repo

    if isinstance(exc, repo.NotFoundError):
        raise ApiError("PROFILE_NOT_FOUND", str(exc), 404) from exc
    if isinstance(exc, repo.VersionConflict):
        latest = exc.latest.get("version")
        raise ApiError(
            "VERSION_CONFLICT",
            f"{exc}（最新版本={latest}，请刷新后重试）",
            409,
            headers={"X-Latest-Version": str(latest)},
        ) from exc
    raise exc


def _get_task_or_404(task_id: str) -> dict:
    task = task_store.store.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    return task


@router.post(
    "/backtest",
    summary="提交回测（F-105 协议底座）",
    response_description="200：{task_id}；非法 mode 422",
)
def submit_backtest(body: BacktestRequest, _auth: dict = Depends(s_auth.require_license)) -> dict:
    from server.core.errors import ApiError

    if body.mode not in _VALID_MODES:
        raise ApiError("INVALID_MODE", f"未知 mode：{body.mode}（可选 {'/'.join(_VALID_MODES)}）", status_code=422)
    # F-701「初级禁用」：初级账号不可提交回测（订阅制，不预扣不计次，仅校验授权越权语义）
    from server.adapters import usage as usage_adapter

    if usage_adapter.is_basic():
        raise ApiError("MODE_FORBIDDEN", "初级模式不可用回测，请切换到高级模式", status_code=403)
    # F-702 运行参数再校验（前端已完成中文项→键映射，后端双端兜底）
    from server.adapters import backtest as backtest_adapter

    try:
        backtest_adapter.validate_run_params(body.run_params, body.mode)
    except backtest_adapter.ParamError as exc:
        raise ApiError("INVALID_RUN_PARAMS", str(exc), status_code=422) from exc
    # F-105 载体任务：payload 透传 run_params（mock 回测读 total/step_delay）
    task_id = get_executor().submit("backtest", body.run_params)
    return {"task_id": task_id}


@router.get(
    "/backtest/modes",
    summary="回测模式与参数约束（F-702 消费面）",
    response_description="200：{modes:[{mode,label,pool,hold,scan}]}",
)
def backtest_modes() -> dict:
    from server.adapters import backtest as backtest_adapter

    return {"ok": True, "modes": backtest_adapter.modes_payload()}


@router.post(
    "/backtest/ic-apply",
    summary="应用因子IC回测结果（F-704，version CAS）",
    response_description="200：{ok, updated, version}；profile 不存在 404；版本冲突 409",
)
def apply_factor_ic(payload: IcApplyRequest) -> dict:
    from server.db import repo
    from server.db.session import session_scope

    session = session_scope()
    try:
        try:
            row = repo.get_profile(session, payload.profile)
            if row is None:
                raise repo.NotFoundError("factor_profile", payload.profile)
            expected = payload.version
            new_body = copy.deepcopy(row["body"] or {})
            fconfig = new_body.setdefault("factor_configs", {})
            if not isinstance(fconfig, dict):
                fconfig = {}
                new_body["factor_configs"] = fconfig
            updated = len(payload.factors)
            for f, ic in (payload.factors or {}).items():
                fconfig[f] = ic
            result = repo.update_profile_cas(session, payload.profile, new_body, expected)
            session.commit()
        except Exception as exc:  # noqa: BLE001 —— 统一映射（404 / 409）
            session.rollback()
            _raise_profile_api(exc)
    finally:
        session.close()
    return {"ok": True, "updated": updated, "version": result["version"]}


@router.get(
    "/backtest/{task_id}/events",
    summary="回测进度 SSE（F-105）",
    response_description="text/event-stream：progress（单调递增）→ done/error",
)
async def backtest_events(task_id: str, request: Request):
    _get_task_or_404(task_id)
    last_event_id = sse.parse_last_event_id(request)

    # 只用一个 `messages` 生成器：从 last_event_id 起读历史事件 + 轮询新事件，
    # 终态后 return。不复用 `replay`/`terminal` 钩子 —— 那两处会与 messages 重复回放
    # （sse.py 里 replay 回放后 messages 又从 0 读，导致事件发两遍）。
    async def messages():
        last_id = last_event_id or 0
        while True:
            task = task_store.store.get(task_id)
            if task is None:
                return
            for e in task_store.store.events_since(task_id, last_id):
                last_id = e["id"]
                yield sse.SSEMessage(event=e["event"], data=e["data"], id=e["id"])
            if task["status"] in TERMINAL_STATUSES:
                return
            await asyncio.sleep(0.1)

    return sse.sse_response(request, channel=f"backtest:{task_id}", messages=messages())


@router.post(
    "/backtest/{task_id}/cancel",
    summary="取消回测（F-105，协作式 ≤1 只检查点）",
    response_description="200：{status:'cancelled'}；任务不存在/已终态 404",
)
def cancel_backtest(task_id: str) -> dict:
    task = _get_task_or_404(task_id)
    if task["status"] in TERMINAL_STATUSES:
        raise HTTPException(status_code=409, detail=f"任务已终态（{task['status']}），无法取消")
    get_executor().cancel(task_id)
    return {"task_id": task_id, "status": "cancelling"}


@router.get(
    "/backtest/{task_id}/artifacts",
    summary="回测产物清单（F-703，URL 访问非 base64）",
    response_description="200：{artifacts:[{name,size,url}]}；任务不存在 404",
)
def list_backtest_artifacts(task_id: str) -> dict:
    from server.adapters import backtest as backtest_adapter

    _get_task_or_404(task_id)
    return {"task_id": task_id, "artifacts": backtest_adapter.list_artifacts(task_id)}


@router.get(
    "/backtest/{task_id}/artifacts/{name}",
    summary="回测产物下载/预览（F-703）",
    response_description="200：文件流；任务或产物不存在 404",
)
def get_backtest_artifact(task_id: str, name: str) -> Response:
    from fastapi.responses import FileResponse

    from server.adapters import backtest as backtest_adapter

    _get_task_or_404(task_id)
    path = backtest_adapter.artifact_path(task_id, name)
    if path is None:
        raise HTTPException(status_code=404, detail=f"产物不存在：{name}")
    is_json = name.endswith(".json")
    media_type = "application/json; charset=utf-8" if is_json else "text/csv; charset=utf-8"
    # 在线预览 + 下载兜底（F-703/F-705：弃用 os.startfile，改 a[download] / 在线渲染）
    disp = f'attachment; filename="{name}"'
    return FileResponse(path, media_type=media_type, headers={"Content-Disposition": disp})
