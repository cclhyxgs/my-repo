# -*- coding: utf-8 -*-
"""任务状态机 + 事件缓冲（F-105 任务队列底座，能力 3 完整化）。

进程内实现（B38 方案 1：单副本过渡）。R-09 全局态外置完成后，本模块的**存储介质**
从「进程内 dict」换为 Redis/DB 即可（消费方只经 `TaskStore` 接口，不碰内部实现）。

任务状态机：`pending → running ⇄ paused → cancelled | completed | failed`。
事件缓冲：每任务一条**单调递增 id** 的事件序列（SSE 回放 / 终态补偿用）。
"""

import threading
import time
import uuid
from typing import Any, Optional

# 状态常量
PENDING = "pending"
RUNNING = "running"
PAUSED = "paused"
CANCELLED = "cancelled"
COMPLETED = "completed"
FAILED = "failed"
TERMINAL_STATUSES = frozenset({CANCELLED, COMPLETED, FAILED})

# 事件缓冲上限（SSE 回放只保留最近 N 条，防内存无界）
_MAX_EVENTS = 300


class TaskStore:
    """进程内任务注册表 + 事件缓冲（线程安全）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tasks: dict[str, dict] = {}

    # ---- 任务生命周期 ----
    def create(self, name: str, payload: dict) -> str:
        task_id = uuid.uuid4().hex
        now = time.time()
        with self._lock:
            self._tasks[task_id] = {
                "task_id": task_id,
                "name": name,
                "payload": payload,
                "status": PENDING,
                "current": 0,
                "total": 0,
                "pct": 0.0,
                "message": "",
                "created_at": now,
                "updated_at": now,
                "cancel_requested": False,
                "pause_requested": False,
                "events": [],  # [{id, event, data, ts}]
                "_next_event_id": 0,
            }
        return task_id

    def get(self, task_id: str) -> Optional[dict]:
        with self._lock:
            return self._tasks.get(task_id)

    def status(self, task_id: str) -> dict:
        task = self.get(task_id)
        if task is None:
            return {"task_id": task_id, "status": "unknown"}
        return {
            "task_id": task_id,
            "name": task["name"],
            "status": task["status"],
            "current": task["current"],
            "total": task["total"],
            "pct": task["pct"],
            "message": task["message"],
        }

    # ---- 进度更新 ----
    def update_progress(self, task_id: str, current: int, total: int, message: str = "") -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task["status"] in TERMINAL_STATUSES:
                return
            task["current"] = current
            task["total"] = total
            task["pct"] = round(current / total * 100, 2) if total else 0.0
            task["message"] = message
            task["updated_at"] = time.time()

    def set_status(self, task_id: str, status: str, message: str = "") -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task["status"] = status
            if message:
                task["message"] = message
            task["updated_at"] = time.time()

    def set_result(self, task_id: str, **result) -> None:
        """写任务结果（如 `summary`/`results`），供进度查询/结果读取消费。"""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.update(result)
            task["updated_at"] = time.time()

    # ---- 取消 / 暂停（协作式标记，由 worker 在检查点检查）----
    def request_cancel(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task["status"] in TERMINAL_STATUSES:
                return False
            task["cancel_requested"] = True
            return True

    def request_pause(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task["status"] in TERMINAL_STATUSES:
                return False
            task["pause_requested"] = True
            return True

    def request_resume(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            task["pause_requested"] = False
            return True

    def is_cancel_requested(self, task_id: str) -> bool:
        task = self.get(task_id)
        return bool(task and task["cancel_requested"])

    def is_pause_requested(self, task_id: str) -> bool:
        task = self.get(task_id)
        return bool(task and task["pause_requested"])

    # ---- 事件缓冲（SSE 回放 + 实时）----
    def append_event(self, task_id: str, event: str, data: dict) -> int:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return -1
            task["_next_event_id"] += 1
            eid = task["_next_event_id"]
            task["events"].append({"id": eid, "event": event, "data": data, "ts": time.time()})
            # 环形缓冲：只保留最近 N 条
            if len(task["events"]) > _MAX_EVENTS:
                task["events"] = task["events"][-_MAX_EVENTS:]
            return eid

    def events_since(self, task_id: str, last_id: int) -> list:
        task = self.get(task_id)
        if task is None:
            return []
        return [e for e in task["events"] if e["id"] > last_id]

    def is_terminal(self, task_id: str) -> bool:
        task = self.get(task_id)
        return bool(task and task["status"] in TERMINAL_STATUSES)


# 全局单例（B38 单副本：进程内共享；扩副本后换 Redis 实现，接口不变）
store = TaskStore()
