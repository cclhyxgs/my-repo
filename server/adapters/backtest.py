# -*- coding: utf-8 -*-
"""回测模式目录与运行参数校验（F-701 / F-702）。

口径来源（逐字对齐，勿自创）：
- 池映射：`完整规格_合并版.md:432` —— A股 全市场→full / 自选股列表→watchlist /
  上证50+创业50+科创50→148 / 其他原样（F-702 属 A：纯前端映射，后端只做再校验）；
  期货 全市场→all / 自选→watchlist。
- run_params：`:433` —— `hold`（前向持有天数，默认 30，须 5-60）、`scan`（扫描间隔，
  默认 5，须 1-20）、`prefix`（默认 `bt_{mode}`）、`offline`。
"""

from __future__ import annotations

import json
from pathlib import Path

from server import settings

# 合法 pool 值（A 股三态 + 期货两态；前端把中文点击项映射成这些键）
POOL_VALUES = ("full", "watchlist", "148", "all")
HOLD_RANGE = (5, 60)
SCAN_RANGE = (1, 20)

# 各模式的池选项（futures 系用 all/watchlist，股票系用 full/watchlist/148）
MODE_META = {
    "strategy": {"label": "策略回测", "pool": ("full", "watchlist", "148")},
    "scoreic": {"label": "评分IC回测", "pool": ("full", "watchlist", "148")},
    "factoric": {"label": "因子IC回测", "pool": ("full", "watchlist", "148")},
    "futures": {"label": "期货回测", "pool": ("all", "watchlist")},
    "futuresic": {"label": "期货因子IC回测", "pool": ("all", "watchlist")},
}


def modes_payload() -> list[dict]:
    """`GET /api/backtest/modes` 消费面（F-702 前置）：模式 → 参数约束（供前端渲染/校验）。"""
    return [
        {
            "mode": m,
            "label": meta["label"],
            "pool": {"options": list(meta["pool"]), "default": meta["pool"][0]},
            "hold": {"min": HOLD_RANGE[0], "max": HOLD_RANGE[1], "default": 30},
            "scan": {"min": SCAN_RANGE[0], "max": SCAN_RANGE[1], "default": 5},
        }
        for m, meta in MODE_META.items()
    ]


class ParamError(ValueError):
    """回测运行参数非法（调用方映射 422 + 业务可读提示）。"""


def validate_run_params(run_params: dict, mode: str) -> None:
    """对 call 侧显式携带的运行参数做再校验；缺省项交给引擎默认。

    只校验已存在键 → 协议载体任务（run_params 里只有 total/step_delay）不受影响。
    """
    rp = run_params or {}
    allowed = MODE_META.get(mode, {}).get("pool") or POOL_VALUES

    pool = rp.get("pool")
    if pool is not None and pool not in allowed:
        raise ParamError(f"pool 不支持：{pool}（模式 {mode} 可选 {'/'.join(allowed)}）")

    hold = rp.get("hold")
    if hold is not None:
        try:
            hv = int(hold)
        except (TypeError, ValueError):
            raise ParamError(f"hold 须为整数（可选 {HOLD_RANGE[0]}-{HOLD_RANGE[1]}）") from None
        if not (HOLD_RANGE[0] <= hv <= HOLD_RANGE[1]):
            raise ParamError(f"hold 须在 {HOLD_RANGE[0]}-{HOLD_RANGE[1]} 范围内（前向持有天数）")

    scan = rp.get("scan")
    if scan is not None:
        try:
            sv = int(scan)
        except (TypeError, ValueError):
            raise ParamError(f"scan 须为整数（可选 {SCAN_RANGE[0]}-{SCAN_RANGE[1]}）") from None
        if not (SCAN_RANGE[0] <= sv <= SCAN_RANGE[1]):
            raise ParamError(f"scan 须在 {SCAN_RANGE[0]}-{SCAN_RANGE[1]} 范围内（扫描间隔）")


# ---------------------------------------------------------------- 产物存储（F-703）
# 对象存储的本地落地：`QUANT_SYSTEM_DIR/backtest_artifacts/{task_id}/`。
# 产物以 URL 访问（非 base64 内联，F-703 验收）；取消后产物**保留**（不随任务终态删除）。

def artifact_root() -> Path:
    base = settings.resolve_quant_system_dir() / "backtest_artifacts"
    base.mkdir(parents=True, exist_ok=True)
    return base


def artifact_dir(task_id: str) -> Path:
    d = artifact_root() / task_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_artifact(task_id: str, name: str, content: bytes | str) -> Path:
    """写一个产物文件；返回其路径。"""
    data = content.encode("utf-8") if isinstance(content, str) else content
    p = artifact_dir(task_id) / name
    p.write_bytes(data)
    return p


def write_json_artifact(task_id: str, name: str, obj: dict) -> Path:
    return write_artifact(task_id, name, json.dumps(obj, ensure_ascii=False, indent=2))


def artifact_path(task_id: str, name: str) -> Path | None:
    """按名查产物路径；不存在（含目录穿越防护不在白名单）→ None。"""
    if "/" in name or "\\" in name or name in ("", ".", ".."):
        return None
    p = artifact_dir(task_id) / name
    return p if p.is_file() else None


def list_artifacts(task_id: str) -> list[dict]:
    """列出任务产物：`[{name, size, url}]`，url 为可下载/预览的绝对路径。"""
    d = artifact_dir(task_id)
    out: list[dict] = []
    if d.is_dir():
        for p in sorted(d.iterdir()):
            if p.is_file():
                out.append({
                    "name": p.name,
                    "size": p.stat().st_size,
                    "url": f"/api/backtest/{task_id}/artifacts/{p.name}",
                })
    return out