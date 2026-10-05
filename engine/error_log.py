# -*- coding: utf-8 -*-
"""统一错误日志：把 Python 日志(WARNING+)、未捕获异常、线程内异常、标准库 warnings
全部落盘到 %LOCALAPPDATA%/M-Bull/logs/M-Bull_error.log（带轮转）。

设计要点：
- 走 logging 的 RotatingFileHandler，与 stdout/stderr（打包后被重定向到 /dev/null）完全
  解耦，因此不会触发 "I/O operation on closed file" 整进程崩溃。
- 幂等：重复调用只在缺失时追加 handler / 钩子，不会重复写。
- 保留调用方已设置的 sys.excepthook（如弹窗 + 临时崩溃日志），只是链式追加落盘。
- 落盘位置：%LOCALAPPDATA%/M-Bull/logs/M-Bull_error.log（单文件默认 2MB，保留 5 个备份）。
"""
import os
import sys
import logging
import logging.handlers
import threading
import traceback
import warnings
import atexit

_ERROR_LOG_PATH = None
_INSTALLED = False


def get_error_log_path():
    """返回错误日志文件绝对路径（确保目录存在）。"""
    global _ERROR_LOG_PATH
    if _ERROR_LOG_PATH:
        return _ERROR_LOG_PATH
    try:
        from engine.config import get_app_dir
        base = os.path.join(get_app_dir(), "logs")
    except Exception:
        base = os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
            "M-Bull", "logs")
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        base = os.path.dirname(os.path.abspath(__file__))
        try:
            os.makedirs(base, exist_ok=True)
        except Exception:
            pass
    _ERROR_LOG_PATH = os.path.join(base, "M-Bull_error.log")
    return _ERROR_LOG_PATH


def setup_error_logging(level=logging.WARNING, max_bytes=2 * 1024 * 1024,
                        backups=5):
    """安装统一错误日志。幂等，重复调用安全。返回日志文件路径。"""
    global _INSTALLED
    path = get_error_log_path()

    root = logging.getLogger()
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)

    # 1) 根日志挂一个轮转文件 handler（去重：已装过则跳过）
    for h in root.handlers:
        if getattr(h, "_mbull_errlog", False):
            break
    else:
        try:
            fh = logging.handlers.RotatingFileHandler(
                path, maxBytes=max_bytes, backupCount=backups,
                encoding="utf-8", errors="replace")
            fh.setLevel(level)
            fh.setFormatter(logging.Formatter(
                "%(asctime)s - %(levelname)s - %(name)s - %(message)s"))
            fh._mbull_errlog = True
            root.addHandler(fh)
        except Exception:
            pass

    # 2) 标准库 warnings → logging（覆盖 urllib3 / pandas 等的 stderr 告警）
    try:
        warnings.simplefilter("default")
        logging.captureWarnings(True)
        logging.getLogger("py.warnings").setLevel(level)
    except Exception:
        pass

    # 2.1) 过滤第三方库（pywebview/pythonnet 等）良性噪声：
    #      tempfile.TemporaryDirectory 未用 with 包裹，GC 时隐式清理触发的
    #      ResourceWarning —— 目录最终仍被清理，无害，且非本工程代码可控。
    #      注：warnings.filterwarnings 在 captureWarnings 下可能不生效，
    #      故同时在 py.warnings logger 上加 Filter 双重保险。
    try:
        warnings.filterwarnings(
            "ignore",
            message="Implicitly cleaning up <TemporaryDirectory",
            category=ResourceWarning)
    except Exception:
        pass
    try:
        class _DropTempDirFilter(logging.Filter):
            def filter(self, record):
                msg = record.getMessage()
                return "Implicitly cleaning up <TemporaryDirectory" not in msg
        logging.getLogger("py.warnings").addFilter(_DropTempDirFilter())
    except Exception:
        pass

    # 3) 主线程 + 线程内未捕获异常 → 落盘（链式保留原有 excepthook）
    _install_excepthook()
    _install_thread_excepthook()

    if not _INSTALLED:
        _INSTALLED = True
        # 注册退出清理：关闭日志文件句柄，避免 PyInstaller onefile 清理临时目录时
        # 因文件句柄未释放而报 "Failed to remove temporary directory"
        atexit.register(_shutdown_error_log)
        try:
            logging.getLogger("M-Bull").warning("错误日志已启用: %s", path)
        except Exception:
            pass
    return path


def _shutdown_error_log():
    """退出时关闭错误日志文件句柄（atexit 钩子）。
    PyInstaller onefile 模式下，若 RotatingFileHandler 仍持有打开的文件句柄，
    会导致临时目录 _MEIxxxxx 无法删除。"""
    try:
        root = logging.getLogger()
        for h in list(root.handlers):
            if getattr(h, "_mbull_errlog", False):
                try:
                    h.flush()
                    h.close()
                except Exception:
                    pass
                root.handlers.remove(h)
    except Exception:
        pass


def _install_excepthook():
    orig = sys.excepthook

    def hook(etype, evalue, tb):
        try:
            logging.getLogger("uncaught").error(
                "未捕获异常:\n%s",
                "".join(traceback.format_exception(etype, evalue, tb)))
        except Exception:
            pass
        if callable(orig):
            try:
                orig(etype, evalue, tb)
            except Exception:
                pass

    if orig is not hook:
        sys.excepthook = hook


def _install_thread_excepthook():
    if not hasattr(threading, "excepthook"):
        return
    orig = threading.excepthook

    def thook(args):
        try:
            logging.getLogger("thread").error(
                "线程未捕获异常: %s",
                "".join(traceback.format_exception(
                    args.exc_type, args.exc_value, args.exc_traceback)))
        except Exception:
            pass
        if callable(orig):
            try:
                orig(args)
            except Exception:
                pass

    if orig is not thook:
        threading.excepthook = thook
