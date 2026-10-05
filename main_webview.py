#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""桌面端程序入口 —— pywebview 外壳 + HTML/JS 前端（ui_mockup/index.html）。

打包入口：build_webview.spec（产物 dist/M-Bull.exe）。
本文件同时承载 frozen 态的 `--backtest-cli` 分支路由（见文件底部）。

用法:
    python main_webview.py
"""

import sys
import os
import io
import json
import glob
import tempfile
import logging
import threading
import traceback

# 将项目根目录加入路径，确保 engine/ui 等包可导入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ═══════════════════════════════════════════════════════════
# 冻结后崩溃可见化（console=False 下无 stdout，只能落盘 + 原生弹窗）
# ═══════════════════════════════════════════════════════════
def _crash_path():
    try:
        return os.path.join(tempfile.gettempdir(), "M-Bull_crash.log")
    except Exception:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "crash.log")


def _log_crash(etype, evalue, tb):
    try:
        with open(_crash_path(), "a", encoding="utf-8") as f:
            import datetime as _dt
            f.write("\n=== CRASH %s ===\n" % _dt.datetime.now().isoformat())
            traceback.print_exception(etype, evalue, tb, file=f)
    except Exception:
        pass


def _popup(title, text):
    """Windows 原生弹窗（ctypes MessageBoxW）。不依赖任何 GUI 工具包，
    避免 PyInstaller 打包额外运行时、减小体积并规避启动期依赖缺失崩溃。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, str(text), str(title), 0x10)
    except Exception:
        pass


def _excepthook(etype, evalue, tb):
    _log_crash(etype, evalue, tb)
    try:
        traceback.print_exception(etype, evalue, tb)
    except Exception:
        pass
    _popup("M-Bull 异常", "%s: %s" % (getattr(etype, "__name__", "Error"), evalue))


sys.excepthook = _excepthook


def fatal_error(msg):
    try:
        with open(_crash_path(), "a", encoding="utf-8") as f:
            import datetime as _dt
            f.write("\n=== FATAL %s ===\n%s\n" % (_dt.datetime.now().isoformat(), msg))
    except Exception:
        pass
    _popup("M-Bull 启动失败", msg)


# 日志基础配置
logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logging.getLogger('matplotlib.font_manager').setLevel(logging.ERROR)


class _DevNullWriter(io.RawIOBase):
    """防崩溃的 /dev/null 写入器。

    打包 exe（console=False / windowed）下 sys.stdout/sys.stderr 初始为 None。
    直接 open(os.devnull) + TextIOWrapper 的方案有隐患：
      TextIOWrapper 会在 GC/底层清理时自动 close 底层 buffer，
      一旦关闭，后续所有 print()/logging.write() 均抛
      ValueError: I/O operation on closed file（整进程崩溃）。

    本类继承 RawIOBase，write() 永远返回写入字节数（不报错）；
    即使底层被外部 close，下次 write 自动重新打开。
    兼容 TextIOWrapper 包装（用于 encoding 转换）。
    """
    _path = os.devnull
    _fd = -1

    @classmethod
    def _ensure_fd(cls):
        if cls._fd < 0:
            try:
                cls._fd = os.open(cls._path, os.O_WRONLY)
            except OSError:
                cls._fd = -1
        return cls._fd

    def writable(self):
        return True

    def write(self, b):
        try:
            fd = self._ensure_fd()
            if fd >= 0:
                return os.write(fd, b)
        except OSError:
            self._fd = -1  # 标记失效，下次 write 重开
        return len(b)  # 假装成功，避免上层重试/崩溃

    def close(self):
        fd, self._fd = self._fd, -1
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


# ── stdout/stderr 防崩溃重定向 ────────────────────────────────
# 打包后 console=False 时 sys.stdout/stderr 可能为 None。
# 用 _DevNullWriter 替代直接 open(os.devnull)，彻底消除
# "I/O operation on closed file" 整进程崩溃风险。
def _make_safe_stream():
    return io.TextIOWrapper(_DevNullWriter(), encoding='utf-8', errors='replace',
                            write_through=True)


if sys.stdout is None or not getattr(sys.stdout, 'writable', lambda: False)():
    sys.stdout = _make_safe_stream()
if sys.stderr is None or not getattr(sys.stderr, 'writable', lambda: False)():
    sys.stderr = _make_safe_stream()
# 若已有 stream 但底层已坏（closed），也替换
for _stream_name in ('stdout', 'stderr'):
    _s = getattr(sys, _stream_name, None)
    if _s is not None:
        try:
            _s.writable() and _s.isatty()
        except ValueError:
            setattr(sys, _stream_name, _make_safe_stream())

# 冻结后切换到 exe 所在目录，确保 config/cache 等相对路径正确
if getattr(sys, 'frozen', False):
    os.chdir(os.path.dirname(sys.executable))

# 统一错误日志：把未捕获异常 / 线程异常 / logging 的 WARNING+ / 标准库 warnings
# 全部落盘到 %LOCALAPPDATA%/M-Bull/logs/M-Bull_error.log（与 devnull 解耦，不触发 I/O 崩溃）
try:
    from engine.error_log import setup_error_logging
    setup_error_logging()
except Exception:
    pass


def resource_path(relative_path):
    """获取资源绝对路径，兼容开发环境、PyInstaller 与 Nuitka 打包后。"""
    candidates = []
    if hasattr(sys, '_MEIPASS'):
        candidates.append(sys._MEIPASS)
    try:
        candidates.append(os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        pass
    try:
        candidates.append(os.path.dirname(os.path.abspath(sys.executable)))
    except Exception:
        pass
    try:
        candidates.append(os.path.dirname(os.path.abspath(sys.argv[0])))
    except Exception:
        pass
    try:
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "main.dist"))
    except Exception:
        pass
    try:
        for d in glob.glob(os.path.join(tempfile.gettempdir(), "onefile_*")):
            if os.path.isdir(d):
                candidates.append(d)
    except Exception:
        pass
    candidates.append(os.path.abspath("."))

    seen = set()
    for base in candidates:
        if not base or base in seen:
            continue
        seen.add(base)
        p = os.path.join(base, relative_path)
        if os.path.exists(p):
            return p
    return os.path.join(candidates[2] if len(candidates) > 2 else os.path.abspath("."), relative_path)


# ═══════════════════════════════════════════════════════════
# 启动 PyWebView
# ═══════════════════════════════════════════════════════════
def _start_h5_server():
    """分支 A 桌面壳：内嵌 FastAPI（SERVE_SPA）并加载 localhost。

    仅在 ``MBULL_H5_SPA=1`` 时启用；默认（关）维持原 ``file:// ui_mockup`` 路径，零回归。
    行为：设置隔离环境变量 → 线程内起 uvicorn（server.main:app，SERVE_SPA=1）→
    轮询 /api/health 就绪 → 返回 SPA 访问 URL（http://127.0.0.1:PORT/）。
    """
    import time
    import urllib.request

    import uvicorn

    # ⚠️ 必须在 import server 之前设置：server.settings 在 import 时读取这些环境变量，
    # 晚于 import 再设则已固化为 False，导致 SPA 不挂载（根路径 404）。
    _proj = os.path.dirname(os.path.abspath(__file__))
    if not os.environ.get("QUANT_SYSTEM_DIR"):
        # 统一数据目录：H5 形态与默认（file:// ui_mockup）形态都落 %LOCALAPPDATA%\M-Bull，
        # 否则同一台机器会因运行形态不同而各攒一份 cache/config —— 缓存互不可见，
        # 全市场扫描「缓存命中 0」就是这种分裂的直接后果。
        # ⚠️ 原实现用「exe 同级 /.mbull_h5_desktop」：onefile 打包时 _proj 指向
        #    %TEMP%\_MEIxxxx（每次启动新建、退出即删）→ 数据等于完全没有持久化。
        os.environ["QUANT_SYSTEM_DIR"] = os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "M-Bull"
        )
    os.environ.setdefault("MBULL_DISABLE_LEGACY_MIGRATE", "1")
    os.environ["MBULL_SERVE_SPA"] = "1"

    from server import settings as srv_settings

    port = srv_settings.PORT
    host = "127.0.0.1"

    def _run():
        uvicorn.run("server.main:app", host=host, port=port, workers=1, reload=False)

    threading.Thread(target=_run, daemon=True).start()

    # 等待端口就绪（最多 ~15s）
    health = "http://%s:%d/api/health" % (host, port)
    for _ in range(150):
        try:
            with urllib.request.urlopen(health, timeout=1) as resp:
                if resp.status == 200:
                    break
        except Exception:
            pass
        time.sleep(0.1)
    else:
        raise RuntimeError("H5 后端服务启动超时（port=%d）" % port)
    return "http://%s:%d/" % (host, port)


def _load_stock_list(api):
    """后台线程加载股票列表，并通过 JS 通知前端状态。"""
    try:
        from engine.data_layer import DataAPI, MIN_STOCK_COUNT
        count = DataAPI.load_stock_list()
        if count > 0:
            msg = (f"✅ 当前已加载 {count} 只股票" if count >= MIN_STOCK_COUNT
                   else f"⚠️ 当前已加载 {count} 只股票（列表可能不全）")
        else:
            msg = "⚠️ 股票列表加载失败（API不可用），可手动输入代码查询"
    except Exception as e:
        msg = f"❌ 加载失败: {str(e)[:80]}"
        logging.getLogger(__name__).exception("加载股票列表失败")

    try:
        # 通过 evaluate_js 调用前端状态更新函数（若前端已暴露）
        if hasattr(api, '_window') and api._window:
            api._window.evaluate_js(f"window.updateStatus && updateStatus({json.dumps(msg)})")
    except Exception:
        pass


def main():
    # 强制只用 edgechromium 后端（WebView2）：Windows 10/11 自带 WebView2 Runtime，
    # 单窗口干净；避免 winforms/mshtml 回退在某些打包环境下额外弹窗/双窗口。
    os.environ.setdefault('PYWEBVIEW_GUI', 'edgechromium')
    try:
        import webview
    except ImportError as e:
        fatal_error("缺少依赖 pywebview，请先安装：pip install pywebview\n%s" % e)
        sys.exit(1)

    from ui.web_api import WebAPI

    # ── H5 接入桌面壳（分支 A）──
    # MBULL_H5_SPA=1：内嵌 FastAPI 同源托管 SPA，加载 http://localhost:PORT；
    # 默认（关）：维持原 file:// ui_mockup 路径，行为完全不变。
    if os.environ.get("MBULL_H5_SPA") == "1":
        try:
            url = _start_h5_server()
        except Exception as e:
            fatal_error("H5 后端启动失败：%s" % e)
            sys.exit(1)
        api = None
    else:
        api = WebAPI()
        index_path = resource_path("ui_mockup/index.html")
        if not os.path.exists(index_path):
            fatal_error("未找到前端入口文件：%s" % index_path)
            sys.exit(1)

        # 使用 file:// 协议加载本地 HTML（file:// URL 不能带 query string，否则 WebView2 报未找到文件）
        url = "file:///" + index_path.replace("\\", "/")

    window_kwargs = dict(
        title='M-Bull · 量化分析工具',
        url=url,
        width=1480,
        height=920,
        min_size=(1280, 720),
        text_select=True,
    )
    if api is not None:
        window_kwargs["js_api"] = api
    window = webview.create_window(**window_kwargs)
    if api is not None:
        api._window = window

    # ── L3 监控守护（形态 A）：app 常驻时后台按交易时段跑自选诊断、触发信号推送 ──
    # 后台线程在 webview 主循环之前启动；app 最小化/前台都正常扫描，关窗(os._exit)即停。
    # （SPA 模式下前端经 /api 驱动，监控守护随服务端运行，此处跳过 WebAPI 桥接）
    if api is not None:
        try:
            api.start_monitor()
        except Exception:
            pass

    # ── 窗口关闭时强制退出进程（解决"点 × 关了窗口但 exe 仍在后台跑"）──
    # PyWebView 在 Windows 经 pythonnet/clr 加载 .NET WinForms 后端，.NET 运行时会派生
    # 非 daemon 线程；点 × 关闭窗口后 webview.start() 虽返回、主线程结束，但进程因这些
    # 非 daemon 线程而残留。os._exit(0) 绕过线程等待强制退出。同时杀掉仍在跑的回测子进程，
    # 避免其成为孤儿 M-Bull.exe 继续占用（也是此前 dist/M-Bull.exe 被占用的一大来源）。
    def _cleanup_and_exit():
        # 1) 终止仍在运行的回测子进程（避免孤儿进程）
        try:
            for _t in getattr(api, '_backtest_tasks', {}).values():
                _launcher = _t.get('launcher') if isinstance(_t, dict) else None
                if _launcher is not None:
                    try:
                        _launcher.stop()
                    except Exception:
                        pass
                    try:
                        _launcher.kill_tree()
                    except Exception:
                        pass
        except Exception:
            pass
        # 2) 刷新日志（否则最后若干行可能丢）
        try:
            import logging
            logging.shutdown()
        except Exception:
            pass
        # 3) 强制退出（绕过 .NET 非 daemon 线程）
        import os
        os._exit(0)

    try:
        window.events.closed += _cleanup_and_exit
    except Exception:
        pass

    # 抑制 pythonnet/.NET 在 windowed 模式分配的额外控制台窗口（避免"双窗口"）：
    # PyWebView 在 Windows 上通过 clr 调用 .NET WinForms 承载 WebView 控件；pythonnet
    # 初始化 .NET 运行时会自动 AllocConsole() 弹出一个黑框。FreeConsole() 关闭它，
    # 不影响应用主窗口。某些 PyWebView/pythonnet 版本组合下内置 FreeConsole 时机不准，
    # 这里兜底再关一次（幂等，无副作用）。
    try:
        import ctypes
        ctypes.windll.kernel32.FreeConsole()
    except Exception:
        pass

    # 后台加载股票列表（SPA 模式下无 WebAPI 桥接，跳过）
    if api is not None:
        threading.Thread(target=_load_stock_list, args=(api,), daemon=True).start()

    # 内置 K 线定时刷新：daemon 线程，收盘后（≥15:30）自动补齐当日 K 线，
    # 当天只跑一次（状态文件记录）。全市场约 50 秒（走批量实时行情快速通道）。
    # 只在 app 内做「日常保鲜」；首次全市场预热交给 CLI/外部定时任务。
    # 关掉：环境变量 MBULL_DISABLE_KLINE_SCHEDULER=1
    try:
        from engine.kline_scheduler import maybe_start as _start_kline_scheduler
        _start_kline_scheduler()
    except Exception as _e:
        # 调度起不来绝不能影响主流程。⚠️ 必须走 logger 而不是 print：打包为
        # windowed（console=False）时 stdout 无处可去，print 等于丢弃 ——
        # 2026-09-18 实测正是因此「调度没生效却零线索」。
        logging.getLogger('M-Bull').warning(
            'K线调度：导入/启动失败（忽略，不影响主流程）: %s', _e, exc_info=True)

    # 调试开关：仅当环境变量 M_BULL_DEBUG=1 时开启 DevTools（F12 可用）。
    # 生产/打包默认关闭（debug=False）。开发期调试可：
    #   $env:M_BULL_DEBUG=1; python main_webview.py   (PowerShell)
    #   set M_BULL_DEBUG=1 && python main_webview.py  (CMD)
    _debug = os.environ.get('M_BULL_DEBUG', '0') == '1'
    webview.start(debug=_debug, icon=resource_path('favicon.ico'), gui='edgechromium')

    # start() 返回后兜底强制退出（点 × 时 closed 事件已触发则此处不会执行到）。
    # 注意：os._exit 会跳过 atexit，PyInstaller onefile 的 _MEIxxxx 临时目录可能残留，
    # 但相比"窗口关了 exe 还在后台跑、且锁住 dist/M-Bull.exe 导致重打包 WinError 5"，
    # 彻底退出更优先；残留临时目录无害，下一次启动会被 PyInstaller 自行清理。
    _cleanup_and_exit()


def _ensure_single_instance():
    """单实例锁（仅主进程，回测子进程跳过）：用 Windows 命名 Mutex 防止重复启动。

    作用：
      1. 彻底杜绝"误启多个实例 → 出现多个一模一样的窗口"。
      2. 避免多个实例争用 %LOCALAPPDATA%/M-Bull 下的 config/cache/pkl（正是此前
         dist/M-Bull.exe 被占用（PermissionError [WinError 5]）的根因之一）。
    回测子进程带 --backtest-cli，由父进程启动，必须放行，故调用方在 main 分支才调本函数。
    """
    try:
        import ctypes
        mutex = ctypes.windll.kernel32.CreateMutexW(
            None, True, "Global\\M-Bull-SingleInstance"
        )
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            ctypes.windll.user32.MessageBoxW(
                0, "M-Bull 已在运行，请勿重复启动。", "M-Bull", 0x40
            )
            sys.exit(0)
    except Exception:
        pass


if __name__ == "__main__":
    # ⛔ PyInstaller/冻结环境 spawn 多进程必需，必须放在最前、先于任何业务分支。
    # 回测多核扫描用 ProcessPoolExecutor(mp_context='spawn')，worker 是新的 exe 进程；
    # 缺此调用时 worker 会重跑本入口 → RuntimeError「An attempt has been made to start a
    # new process before the current process has finished its bootstrapping phase /
    # freeze_support()」（audit/_bt_verify_out.log 实录）→ 并行扫描失效；在部分时序下
    # worker 启动即死、as_completed 永久等待 → UI 进度停在「1/243 · 0%」一动不动
    # （2026-09-19 实证：19:36 回测 26 分钟无推进、无 worker 子进程）。
    # worker 子进程会在此被 multiprocessing 接管并直接运行目标函数，不再走到下方分支。
    try:
        import multiprocessing
        multiprocessing.freeze_support()
    except Exception:
        pass

    # 回测子进程协议：打包为 EXE 后 backtest_launcher 以
    # `[exe, --backtest-cli, ...]` 启动子进程，需在此分发到 engine.backtest_cli。
    if '--backtest-cli' in sys.argv:
        from engine import backtest_cli
        backtest_cli.main()
    else:
        _ensure_single_instance()
        main()
