# -*- coding: utf-8 -*-
"""任务执行器：进程内线程池实现（F-105 任务队列底座，B38 方案 1）。

接口 `TaskExecutor`（submit/status/cancel/pause/resume）保持不变（骨架期已定，
只暴露 task_id 语义、不暴露进程内对象）——后续 R-09 全局态外置完成后，
**只把 `get_executor()` 换成 Celery+Redis 实现**，消费方零改动（C1#2 规格 M 路径）。

本模块是「任务队列底座」，业务任务通过 `_WORKERS` 注册表按 name 分发；
F-105 只实现 `backtest` 载体任务（mock 进度 + 逐只检查点），诊断/扫描后续接入。
"""

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol, runtime_checkable

from server.core import time as srv_time
from server.core import task_store
from server.core.task_store import (
    CANCELLED,
    COMPLETED,
    FAILED,
    PAUSED,
    RUNNING,
)

# 单副本进程内线程池（R-09 未外置前禁止扩副本）
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mbull-task")


def _now() -> str:
    """事件时间戳：走时区唯一入口（§2.13-2 / R-19）。"""
    return srv_time.now_iso()


@runtime_checkable
class TaskExecutor(Protocol):
    def submit(self, name: str, payload: dict) -> str: ...

    def status(self, task_id: str) -> dict: ...

    def cancel(self, task_id: str) -> bool: ...

    def pause(self, task_id: str) -> bool: ...

    def resume(self, task_id: str) -> bool: ...


# ------------------------------------------------------------
# 业务任务（按 name 分发）
# ------------------------------------------------------------

def _run_backtest(task_id: str, payload: dict) -> None:
    """回测任务：mock 逐只进度 + 写产物 + 日志流（F-105 载体 / F-703 子进程编排）。

    检查点语义（对齐桌面 `ui/web_api.py:3210` 的逐只检查点）：
    每只 = 一个检查点；循环**开头**检查取消/暂停 → cancel 后**最多再跑 1 只**即退出。
    取消时**保留已写产物**（F-703 验收：取消后任务 `cancelled` 且产物可保留）。
    产物落对象存储（`server.adapters.backtest.artifact_dir`），以 URL 访问、非 base64。
    """
    from server.adapters import backtest as backtest_adapter  # 懒 import

    total = int(payload.get("total") or 20)
    step_delay = float(payload.get("step_delay") or 0.01)
    mode = payload.get("mode") or "factoric"
    prefix = payload.get("prefix") or f"bt_{mode}"

    task_store.store.set_status(task_id, RUNNING, "回测启动")
    task_store.store.append_event(task_id, "stage", {
        "task_id": task_id, "status": RUNNING, "message": "回测启动", "ts": _now(),
    })

    progress_rows = ["idx,pct"]

    def _write_artifacts():
        if len(progress_rows) > 1:
            backtest_adapter.write_artifact(task_id, f"{prefix}_progress.csv", "\n".join(progress_rows))

    for i in range(1, total + 1):
        # ① 取消检查（协作式，≤1 只检查点）；产物保留（F-703）
        if task_store.store.is_cancel_requested(task_id):
            _write_artifacts()
            task_store.store.set_status(task_id, CANCELLED, f"已取消（第 {i - 1} 只后）")
            task_store.store.append_event(task_id, "done", {
                "task_id": task_id, "status": CANCELLED,
                "current": i - 1, "total": total,
                "pct": round((i - 1) / total * 100, 2),
                "message": "已取消", "ts": _now(),
            })
            return

        # ② 暂停检查（F-506 消费，F-105 先立基础）
        while task_store.store.is_pause_requested(task_id):
            task_store.store.set_status(task_id, PAUSED, "已暂停")
            time.sleep(0.1)
        task_store.store.set_status(task_id, RUNNING, f"第 {i}/{total} 只")

        pct = round(i / total * 100, 2)
        progress_rows.append(f"{i},{pct}")
        task_store.store.update_progress(task_id, i, total, f"第 {i}/{total} 只")
        task_store.store.append_event(task_id, "progress", {
            "task_id": task_id, "status": RUNNING,
            "current": i, "total": total, "pct": pct,
            "message": f"第 {i}/{total} 只", "ts": _now(),
        })
        task_store.store.append_event(task_id, "log", {
            "task_id": task_id, "message": f"第 {i}/{total} 只完成", "ts": _now(),
        })
        time.sleep(step_delay)

    # 完成：写完整产物（report.json + result.csv）→ F-703 产物经 URL 访问
    _write_artifacts()
    backtest_adapter.write_json_artifact(task_id, f"{prefix}_report.json", {
        "mode": mode, "prefix": prefix, "total": total,
        "summary": {"total_items": total},
    })
    backtest_adapter.write_artifact(task_id, f"{prefix}.csv", "idx,pct\n" + "\n".join(progress_rows[1:]))
    task_store.store.set_status(task_id, COMPLETED, "回测完成")
    task_store.store.append_event(task_id, "done", {
        "task_id": task_id, "status": COMPLETED,
        "current": total, "total": total, "pct": 100.0,
        "message": "回测完成", "artifacts": backtest_adapter.list_artifacts(task_id), "ts": _now(),
    })


def _run_scan(task_id: str, payload: dict) -> None:
    """F-502 扫描 worker：全市场股票池分片并发评分，进度推进 + 暂停/取消检查点。

    market 由 payload 显式携带（不读全局态 → 不串市场，R-09）。
    逐只评分走 `engine.market_scan_core`（经 engine_bridge 薄访问器收口，T8）。
    """
    from server.adapters import engine_bridge  # 懒 import，避免模块级循环依赖
    from server.adapters import scan as scan_adapter

    market = payload.get("market", "stock")
    direction = payload.get("direction") or "long"
    k_type = payload.get("k_type", "日K")
    scheme_name = payload.get("scheme_name")
    up_ratio = float(payload.get("up_ratio") or 0.5)

    # 加载方案到 engine（与桌面 start_market_scan 同路径；单副本写 _cache 全局态）
    try:
        engine_bridge.scan_load_scheme(market, direction, k_type, scheme_name)
    except Exception:
        pass

    # 股票池（测试 monkeypatch engine_bridge.scan_stock_codes 注入小池）
    codes = list(engine_bridge.scan_stock_codes())
    sector_map = engine_bridge.scan_build_sector_map()
    total = len(codes)
    chunk_size = 20

    task_store.store.set_status(task_id, RUNNING, f"扫描启动（{total} 只）")
    task_store.store.append_event(task_id, "stage", {
        "task_id": task_id, "status": RUNNING, "market": market,
        "message": f"扫描启动（{total} 只）", "ts": _now(),
    })

    results = []
    chunks = [codes[i:i + chunk_size] for i in range(0, total, chunk_size)] or [[]]

    for ci, chunk in enumerate(chunks):
        # ① 取消检查（协作式，≤1 片检查点）
        if task_store.store.is_cancel_requested(task_id):
            task_store.store.set_status(task_id, CANCELLED, f"已取消（第 {ci * chunk_size} 只后）")
            task_store.store.append_event(task_id, "done", {
                "task_id": task_id, "status": CANCELLED,
                "current": ci * chunk_size, "total": total,
                "pct": round(ci * chunk_size / total * 100, 2) if total else 0.0,
                "message": "已取消", "ts": _now(),
            })
            return

        # ② 暂停检查（F-506：worker ≤1 片内置 paused；暂停期间持续轮询取消）
        while task_store.store.is_pause_requested(task_id):
            task_store.store.set_status(task_id, PAUSED, "已暂停")
            if task_store.store.is_cancel_requested(task_id):
                task_store.store.set_status(task_id, CANCELLED, f"暂停中已取消（第 {ci * chunk_size} 只后）")
                task_store.store.append_event(task_id, "done", {
                    "task_id": task_id, "status": CANCELLED,
                    "current": ci * chunk_size, "total": total,
                    "pct": round(ci * chunk_size / total * 100, 2) if total else 0.0,
                    "message": "暂停中已取消", "ts": _now(),
                })
                return
            time.sleep(0.1)
        task_store.store.set_status(task_id, RUNNING, f"第 {ci + 1}/{len(chunks)} 片")

        # ③ 分片评分（内核已过滤 None；即便整片失败也返回 []，不阻塞进度）
        try:
            chunk_results = engine_bridge.scan_chunk_score(
                chunk, sector_map, direction=direction, period=k_type, up_ratio=up_ratio
            ) or []
        except Exception:
            chunk_results = []
        results.extend(chunk_results)

        # ④ 进度推进：current 按「已处理片数 × 片大小」累加（含失败只），
        #    保证即便存在被放弃分片，任务仍推进至 99%+（F-502 验收）。
        done = (ci + 1) * chunk_size
        done = min(done, total)
        pct = round(done / total * 100, 2) if total else 0.0
        task_store.store.update_progress(task_id, done, total, f"第 {done}/{total} 只")
        task_store.store.append_event(task_id, "progress", {
            "task_id": task_id, "status": RUNNING,
            "current": done, "total": total, "pct": pct,
            "message": f"第 {done}/{total} 只（有效 {len(results)}）", "ts": _now(),
        })

    # 完成：落盘结果 + 缓存（F-504）
    task_store.store.set_result(task_id, results=results, total_stocks=total)
    try:
        scan_adapter.save_scan_cache(market, results)
    except Exception:
        pass
    task_store.store.set_status(task_id, COMPLETED, "扫描完成")
    task_store.store.append_event(task_id, "done", {
        "task_id": task_id, "status": COMPLETED,
        "current": total, "total": total, "pct": 100.0,
        "message": "扫描完成", "total_stocks": total, "valid": len(results), "ts": _now(),
    })


def _run_diagnosis(task_id: str, payload: dict) -> None:
    """F-402 诊断 worker：逐只串行分析，summary 五组计数（buy/watch/avoid/position/error）。

    market 由 payload 显式携带（不读全局态 → 不串市场，R-09）。
    逐只调 `analyze_stock` / `analyze_futures`（F-301/F-302 已上移），
    分类走 `engine.report_classify`（F-402 上移）。额度：订阅制不扣减（B42）。
    """
    from server.adapters import engine_bridge  # 懒 import，避免模块级循环依赖

    items = payload.get("items") or []
    market = payload.get("market", "stock")
    scheme_name = payload.get("scheme_name")
    k_type = payload.get("k_type", "日K")
    total = len(items)

    summary = {"buy": 0, "watch": 0, "avoid": 0, "position": 0, "error": 0}
    results = []

    task_store.store.set_status(task_id, RUNNING, f"诊断启动（{total} 只）")
    task_store.store.append_event(task_id, "stage", {
        "task_id": task_id, "status": RUNNING, "market": market,
        "message": f"诊断启动（{total} 只）", "ts": _now(),
    })

    for idx, item in enumerate(items, 1):
        # 取消检查（协作式，≤1 只检查点）
        if task_store.store.is_cancel_requested(task_id):
            task_store.store.set_status(task_id, CANCELLED, f"已取消（第 {idx - 1} 只后）")
            task_store.store.append_event(task_id, "done", {
                "task_id": task_id, "status": CANCELLED,
                "current": idx - 1, "total": total,
                "pct": round((idx - 1) / total * 100, 2) if total else 0.0,
                "message": "已取消", "summary": summary, "ts": _now(),
            })
            return

        code = item.get("code") or ""
        item_direction = item.get("direction", "long")
        category = "error"
        try:
            if market == "futures":
                result = engine_bridge.analyze_futures(
                    code, k_type=k_type, name=item.get("name", ""),
                    entry_price=item.get("entry_price"), bars_held=item.get("bars_held", 0),
                    direction=item_direction, scheme_name=scheme_name, now=srv_time.now(),
                )
            else:
                result = engine_bridge.analyze_stock(
                    code, k_type=k_type, name=item.get("name", ""),
                    entry_price=item.get("entry_price"), bars_held=item.get("bars_held", 0),
                    direction=item_direction, scheme_name=scheme_name,
                )
            if result.get("error"):
                category = "error"
            else:
                report_data = result.get("reportData") or {}
                category = engine_bridge.classify_report(report_data)
        except Exception:
            category = "error"

        summary[category] = summary.get(category, 0) + 1
        results.append({"code": code, "name": item.get("name", ""), "category": category})

        task_store.store.update_progress(task_id, idx, total, f"第 {idx}/{total} 只")
        task_store.store.append_event(task_id, "progress", {
            "task_id": task_id, "status": RUNNING,
            "current": idx, "total": total, "pct": round(idx / total * 100, 2),
            "message": f"第 {idx}/{total} 只", "ts": _now(),
        })

    task_store.store.set_result(task_id, summary=summary, results=results)
    task_store.store.set_status(task_id, COMPLETED, "诊断完成")
    task_store.store.append_event(task_id, "done", {
        "task_id": task_id, "status": COMPLETED,
        "current": total, "total": total, "pct": 100.0,
        "message": "诊断完成", "summary": summary, "ts": _now(),
    })


_WORKERS = {
    "backtest": _run_backtest,
    "diagnosis": _run_diagnosis,
    "scan": _run_scan,
}


class ThreadPoolTaskExecutor:
    """进程内线程池执行器（实现 `TaskExecutor` 接缝）。"""

    def submit(self, name: str, payload: dict) -> str:
        worker = _WORKERS.get(name)
        if worker is None:
            raise ValueError(f"未知任务类型：{name}（可用：{'、'.join(_WORKERS)}）")
        task_id = task_store.store.create(name, payload)
        _executor.submit(worker, task_id, payload)
        return task_id

    def status(self, task_id: str) -> dict:
        return task_store.store.status(task_id)

    def cancel(self, task_id: str) -> bool:
        return task_store.store.request_cancel(task_id)

    def pause(self, task_id: str) -> bool:
        return task_store.store.request_pause(task_id)

    def resume(self, task_id: str) -> bool:
        return task_store.store.request_resume(task_id)


_executor_instance = ThreadPoolTaskExecutor()


def get_executor() -> TaskExecutor:
    """当前执行器（B38：进程内线程池；后续换 Celery+Redis 只改这里）。"""
    return _executor_instance
