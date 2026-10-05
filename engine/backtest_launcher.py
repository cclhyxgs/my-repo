#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""父进程（GUI 侧）回测启动器：封装 subprocess.Popen 启动独立子进程。

职责：
  - start(mode, validated_config_dict, params_dict)：写临时 config JSON（NumpySafeEncoder），
    Popen 启动子进程（Windows CREATE_NO_WINDOW 避免弹黑框），返回 taskid
  - read_progress()：按**字节偏移**增量读取进度 JSONL 新行，返回事件 dict 列表
  - stop()：touch 取消 flag 文件
  - is_alive() / exit_code() / was_cancelled()：进程与取消状态
  - cleanup()：删临时 config + cancel flag（progress / stdout 保留至应用退出）
  - kill_tree()：进程树强杀（优先 psutil，降级系统命令）
  - cleanup_all()：应用退出时清理整个 .backtest_tmp（含 7 天轮转）

**绝不 import ui / 任何 GUI 模块**，可在 GUI 线程安全调用。

说明（dev / frozen 通用）：dev 下子进程以 `python -m engine.backtest_cli --backtest-cli ...`
直接拉起（原 main.py 入口已整体下线）；frozen 下 `sys.executable` 即打包后的 exe，
main_webview 已路由 `--backtest-cli` 分支，直接用 `exe --backtest-cli` 即可。
"""
import os
import sys
import json
import time
import subprocess


from engine.backtest_protocol import (
    get_tmp_dir,
    ensure_tmp_dir,
    make_taskid,
    config_path,
    progress_path,
    cancel_path,
    stdout_path,
    NumpySafeEncoder,
)


# 7 天轮转阈值（秒）
ROTATE_SEC = 7 * 24 * 3600


def _cleanup_old_tmp():
    """启动/退出时清理超过 7 天的遗留临时文件（防止长期运行累积）。"""
    try:
        d = get_tmp_dir()
        if not os.path.isdir(d):
            return
        now = time.time()
        for name in os.listdir(d):
            p = os.path.join(d, name)
            try:
                if now - os.path.getmtime(p) > ROTATE_SEC and os.path.isfile(p):
                    os.remove(p)
            except Exception:
                pass
    except Exception:
        pass


class BacktestLauncher:
    """单次回测的父进程启动器（每个回测实例一个对象）。"""

    def __init__(self):
        self.taskid = None
        self.mode = None
        self._config_path = None
        self._progress_path = None
        self._cancel_path = None
        self._stdout_path = None
        self._process = None
        self._progress_offset = 0   # 字节偏移，增量读取进度 JSONL
        self._terminal_seen = False
        ensure_tmp_dir()

    # ============================================================
    # 启动
    # ============================================================
    def start(self, mode, validated_config_dict, params_dict):
        """启动子进程，返回 taskid。

        Args:
            mode: 'strategy' | 'scoreic' | 'factoric'
            validated_config_dict: quant_config._validate_config 校验后的纯 dict
            params_dict: {'years', 'full_market', 'hold', 'scan', 'prefix'}

        成功 Popen 后立即返回；调用方（GUI）据此立即显示「正在启动回测进程…」过渡提示，
        覆盖 spawn 冷启动窗口。
        """
        _cleanup_old_tmp()
        self.mode = mode
        self.taskid = make_taskid(mode)
        self._config_path = config_path(self.taskid)
        self._progress_path = progress_path(self.taskid)
        self._cancel_path = cancel_path(self.taskid)
        self._stdout_path = stdout_path(self.taskid)
        self._progress_offset = 0
        self._terminal_seen = False

        # 合并运行参数（打包进 config JSON，子进程读取后传给 run_*）
        cfg = dict(validated_config_dict) if isinstance(validated_config_dict, dict) else {}
        cfg['_run_params'] = dict(params_dict) if isinstance(params_dict, dict) else {}

        with open(self._config_path, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2, cls=NumpySafeEncoder)

        # 子进程 stdout/stderr 重定向到日志文件（非管道，规避管道缓冲死锁）
        stdout_f = open(self._stdout_path, 'w', encoding='utf-8', buffering=1)

        cmd = self._build_cmd()
        creationflags = 0
        if sys.platform == 'win32':
            creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

        self._process = subprocess.Popen(
            cmd,
            stdout=stdout_f,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
        # 父进程关闭自己持有的 fd 副本（子进程已继承独立句柄/描述符，不受影响）
        try:
            stdout_f.close()
        except Exception:
            pass
        return self.taskid

    def _build_cmd(self):
        """构造子进程命令行。

        回测子进程由 engine.backtest_cli 直接承载，入口分两种形态：
        - 编译形态（frozen exe，含 PyInstaller / Nuitka）：exe 自身经 main_webview 的
          `--backtest-cli` 分支路由到 engine.backtest_cli，故直接用 `exe --backtest-cli ...`。
        - 开发形态（源码 `python main_webview.py`）：用 `python -m engine.backtest_cli`
          直接拉起子进程（原 main.py 已整体下线，不再作为回测入口）。
        """
        base = [
            '--backtest-cli',
            '--config', self._config_path,
            '--mode', self.mode,
            '--progress', self._progress_path,
            '--cancel', self._cancel_path,
        ]
        if getattr(sys, 'frozen', False):
            # 编译形态：exe 自身即入口，main_webview 已路由 --backtest-cli
            return [sys.executable] + base
        # 开发形态：直接以模块方式拉起 backtest_cli（不再依赖已下线的 main.py）
        return [sys.executable, '-m', 'engine.backtest_cli'] + base

    # ============================================================
    # 进度读取（增量，按字节偏移）
    # ============================================================
    def read_progress(self):
        """增量读取进度 JSONL 新行，返回事件 dict 列表（仅完整行）。

        以二进制模式读取并按字节偏移定位，避免 Windows 文本模式 tell/seek 错位；
        末尾不完整行（子进程正在写）留待下次读取。
        """
        if self._progress_path is None or not os.path.exists(self._progress_path):
            return []
        events = []
        try:
            with open(self._progress_path, 'rb') as f:
                f.seek(self._progress_offset)
                data = f.read()
            if not data:
                return events
            last_nl = data.rfind(b'\n')
            if last_nl == -1:
                # 尚未出现任何完整行
                return events
            complete = data[:last_nl]
            self._progress_offset += last_nl + 1
            text = complete.decode('utf-8', errors='replace')
            for line in text.split('\n'):
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    # 半行或非法行：跳过（理论上不会发生，因只取完整行）
                    continue
                events.append(ev)
                if ev.get('type') in ('done', 'cancel', 'error'):
                    self._terminal_seen = True
        except Exception:
            pass
        return events

    # ============================================================
    # 取消 / 状态
    # ============================================================
    def stop(self):
        """touch 取消 flag 文件（父侧取消信号）。"""
        if self._cancel_path:
            try:
                if not os.path.exists(self._cancel_path):
                    with open(self._cancel_path, 'w', encoding='utf-8') as f:
                        pass
            except Exception:
                pass

    def is_alive(self):
        """子进程是否仍在运行。"""
        return self._process is not None and self._process.poll() is None

    def exit_code(self):
        """子进程退出码（None 表示仍在运行）。"""
        if self._process is None:
            return None
        return self._process.poll()

    def was_cancelled(self):
        """取消 flag 文件是否仍存在（用户主动停止判定，供「取消优先」裁决）。"""
        return self._cancel_path is not None and os.path.exists(self._cancel_path)

    # ============================================================
    # 清理
    # ============================================================
    def cleanup(self):
        """删临时 config + cancel flag（progress / stdout 保留至应用退出）。"""
        for p in (self._config_path, self._cancel_path):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

    def kill_tree(self):
        """进程树强杀（兜底）。优先 psutil 递归，降级系统命令。

        psutil 为可选依赖：未安装时 Windows 用 `taskkill /T /F /PID`，
        POSIX 用 `os.killpg`。
        """
        if self._process is None:
            return
        pid = self._process.pid
        killed = False
        # 优先 psutil
        try:
            import psutil as _psutil
            try:
                proc = _psutil.Process(pid)
            except (_psutil.NoSuchProcess, _psutil.ZombieProcess, ProcessLookupError):
                # 进程本身已经不存在了 = 被杀干净，不用再降级 taskkill 弹窗
                self._process = None
                return
            try:
                for child in proc.children(recursive=True):
                    try:
                        child.kill()
                    except (_psutil.NoSuchProcess, _psutil.ZombieProcess, ProcessLookupError):
                        pass
                    except Exception:
                        pass
                try:
                    proc.kill()
                except (_psutil.NoSuchProcess, _psutil.ZombieProcess, ProcessLookupError):
                    pass
                except Exception:
                    pass
                killed = True
            except Exception:
                # psutil 执行过程异常（如权限）→ 仍然尝试降级 taskkill
                killed = False
        except ImportError:
            killed = False
        # 降级系统命令
        if not killed:
            try:
                if sys.platform == 'win32':
                    # Windows 下 CREATE_NO_WINDOW 避免 taskkill.exe 弹出黑色控制台窗口
                    _flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                    subprocess.run(
                        ['taskkill', '/T', '/F', '/PID', str(pid)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=_flags,
                    )
                else:
                    import os as _os
                    import signal
                    try:
                        _os.killpg(_os.getpgid(pid), signal.SIGKILL)
                    except Exception:
                        _os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
        # 清掉引用（幂等，避免后续二次 kill 无谓跑 taskkill）
        try:
            if self._process is not None and self._process.pid == pid:
                self._process = None
        except Exception:
            pass

    @staticmethod
    def cleanup_all():
        """应用退出时清理整个 .backtest_tmp（含当前/历史遗留文件）。"""
        _cleanup_old_tmp()
        try:
            d = get_tmp_dir()
            if not os.path.isdir(d):
                return
            for name in os.listdir(d):
                p = os.path.join(d, name)
                try:
                    if os.path.isfile(p):
                        os.remove(p)
                except Exception:
                    pass
        except Exception:
            pass
