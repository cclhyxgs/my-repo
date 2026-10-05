#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""回测跨进程共享约定（父进程 launcher 与子进程 CLI 共用）。

本模块仅依赖标准库 + engine.config，**绝不 import ui / 任何 GUI 模块**，
因此可在子进程（CLI 分支）安全导入，避免 Windows spawn 重导入 GUI。

包含：
  - 退出码枚举 ExitCode
  - 临时目录 / 文件命名约定（.backtest_tmp、taskid、路径构造辅助）
  - NumpySafeEncoder（numpy 标量/数组 → 原生类型，非 default=str）
  - FlagFileEvent（duck-typed 取消事件，兼容 threading.Event 接口，直接复用现有 cancel_event.is_set() 检查点）
  - ProgressWriter（进度 JSONL 写入，含 file-like 适配器 _LineWriter）
"""
import os
import json
from datetime import datetime
from enum import IntEnum
from typing import Any, Dict, Optional


from engine.config import get_app_dir


# ============================================================
# 临时目录与命名约定
# ============================================================
TMP_DIR_NAME = '.backtest_tmp'


def get_tmp_dir() -> str:
    """回测临时文件根目录（与 report 同根，便于统一清理）。"""
    return os.path.join(get_app_dir(), TMP_DIR_NAME)


def ensure_tmp_dir() -> str:
    """确保临时目录存在，返回其路径。"""
    d = get_tmp_dir()
    os.makedirs(d, exist_ok=True)
    return d


def make_taskid(mode: str) -> str:
    """taskid 命名：{mode}_{YYYYMMDD_HHMMSS}_{pid}

    mode ∈ strategy|scoreic|factoric；pid=os.getpid() 防并发冲突。
    """
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    return f"{mode}_{ts}_{os.getpid()}"


# ============================================================
# 退出码约定
# ============================================================
class ExitCode(IntEnum):
    SUCCESS = 0        # 成功完成（report 已落盘）
    CANCELLED = 1      # 用户取消（BacktestCancelled，干净中止）
    ERROR = 2          # 非预期异常（message 写入 progress error 事件）
    STARTUP_FAILED = 3 # 子进程启动/导入失败（config 加载失败 / _cache 注入异常）


# ============================================================
# 文件名构造辅助
# ============================================================
def config_path(taskid: str) -> str:
    return os.path.join(get_tmp_dir(), f'bt_{taskid}.config.json')


def progress_path(taskid: str) -> str:
    return os.path.join(get_tmp_dir(), f'bt_{taskid}.progress.jsonl')


def cancel_path(taskid: str) -> str:
    return os.path.join(get_tmp_dir(), f'bt_{taskid}.cancel')


def stdout_path(taskid: str) -> str:
    return os.path.join(get_tmp_dir(), f'bt_{taskid}.stdout.log')


# ============================================================
# numpy 标量安全序列化（非 default=str）
# ============================================================
class NumpySafeEncoder(json.JSONEncoder):
    """将 numpy 标量/数组归一成原生 Python 类型。

    替代 `default=str`：default=str 会把 numpy 标量字符串化，导致子进程
    json.load 后深度逻辑拿到字符串而非数值，引发类型错误。
    这里把 np.integer→int、np.floating→float、np.bool_→bool、np.ndarray→list。
    """

    def default(self, o: Any) -> Any:
        try:
            import numpy as np
            if isinstance(o, np.generic):
                # np.integer / np.floating / np.bool_ 等标量
                return o.item()
            if isinstance(o, np.ndarray):
                return o.tolist()
        except ImportError:
            pass
        # 兜底（理论上不会到达：_validate_config 已返回纯 dict）
        return super().default(o)


# ============================================================
# Flag 文件事件（duck-typed 替代 threading.Event）
# ============================================================
class FlagFileEvent:
    """跨进程取消标志：以「文件是否存在」表示事件是否已 set。

    接口与 threading.Event 兼容（is_set / set / clear），直接作为 cancel_event
    传给 backtest_runner.run_*，复用现有 `cancel_event.is_set()` 检查点
    （backtest.py / backtest_strategy.py / factor_ic_backtest.py / parallel_utils.py），
    回测逻辑零改动。
    """

    def __init__(self, path: str) -> None:
        self.path = path

    def is_set(self) -> bool:
        return os.path.exists(self.path)

    def set(self) -> None:
        if not os.path.exists(self.path):
            with open(self.path, 'w', encoding='utf-8') as f:
                pass

    def clear(self) -> None:
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass


# ============================================================
# 进度写入器（JSONL）
# ============================================================
class _LineWriter:
    """file-like 适配器：把 stdout 逐行转发给 ProgressWriter.log，供 redirect_stdout 使用。

    仅透传「完整行」（遇 '\\n' 才提交一行），避免半行冲刷。
    """

    def __init__(self, writer: 'ProgressWriter') -> None:
        self._writer = writer
        self._buf = ''

    def write(self, s: str) -> int:
        if not s:
            return 0
        self._buf += s
        while '\n' in self._buf:
            line, self._buf = self._buf.split('\n', 1)
            if line:
                self._writer.log(line)
        return len(s)

    def flush(self) -> None:
        if self._buf:
            self._writer.log(self._buf)
            self._buf = ''

    def isatty(self) -> bool:
        return False

    def fileno(self):
        raise OSError("ProgressWriter._LineWriter has no real file descriptor")


class ProgressWriter:
    """向进度 JSONL 文件追加一行 JSON（type/line/ts/...）。

    事件类型：log | done | cancel | error。
    v1 仅保证 type + line + ts（stage/current/total/percent 预留为 null），
    向后兼容现有实时日志显示。
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._pid = os.getpid()
        self._fp = None  # 延迟打开，进程内持续 append

    # —— 内部写入 ——
    def _emit(self, obj: Dict[str, Any]) -> None:
        try:
            if self._fp is None or self._fp.closed:
                self._fp = open(self.path, 'a', encoding='utf-8')
            self._fp.write(json.dumps(obj, ensure_ascii=False) + '\n')
            self._fp.flush()
        except Exception:
            # 进度写入失败绝不应影响回测主流程
            pass

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec='milliseconds')

    def _base(self) -> Dict[str, Any]:
        return {
            'pid': self._pid,
            'ts': self._now(),
            'stage': None, 'current': None, 'total': None,
            'percent': None,
        }

    # —— 供 run_* 的 progress_cb 使用（单参数 line:str）——
    def log(self, line: str) -> None:
        obj = self._base()
        obj['type'] = 'log'
        obj['line'] = line if isinstance(line, str) else str(line)
        obj['message'] = None
        self._emit(obj)

    # —— 净值实时点（回测过程中可视化用，非终态产物）——
    def equity(self, point: Dict[str, Any]) -> None:
        """发一个净值采样点，供 UI 边跑边画曲线。

        point: {'d': 'YYYY-MM-DD', 'nav': float, 'dd': 回撤(0~1), 'dr': 当日收益率, 'pos': 持仓数}
        ⛔ 仅供过程展示：终态曲线仍以 equity.csv（回测结束一次性落盘）为准，
           前端在 done 时会全量替换，因此这里允许节流（每 N 个交易日一个点）。
        """
        obj = self._base()
        obj['type'] = 'equity'
        obj['line'] = None
        obj['message'] = None
        obj['point'] = point
        self._emit(obj)

    def done(self, message: Optional[str] = None) -> None:
        obj = self._base()
        obj['type'] = 'done'
        obj['line'] = None
        obj['message'] = message
        self._emit(obj)
        self.close()

    def cancel(self, message: Optional[str] = None) -> None:
        obj = self._base()
        obj['type'] = 'cancel'
        obj['line'] = None
        obj['message'] = message
        self._emit(obj)
        self.close()

    def error(self, message: str) -> None:
        obj = self._base()
        obj['type'] = 'error'
        obj['line'] = None
        obj['message'] = message if isinstance(message, str) else str(message)
        self._emit(obj)
        self.close()

    # file-like 适配器（redirect_stdout 可选使用）
    @property
    def line_writer(self) -> _LineWriter:
        return _LineWriter(self)

    def close(self) -> None:
        if self._fp is not None and not self._fp.closed:
            try:
                self._fp.close()
            except Exception:
                pass
        self._fp = None
