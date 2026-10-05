#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""回测子进程 CLI 入口（由 main_webview.py 的 --backtest-cli 分支自举启动）。

职责：
  1. 解析 argv（--config / --mode / --progress / --cancel / --dry-run）
  2. 加载 config JSON → 注入 quant_config._cache（**在 import/call backtest_runner 之前**）
  3. 用 FlagFileEvent 作为 cancel_event（跨进程复用现有 `cancel_event.is_set()` 检查点）
  4. 用 ProgressWriter 回传进度 JSONL
  5. 终态：
       - BacktestCancelled → 写 cancel 事件 + exit(1)
       - 其它 Exception   → 写 error 事件 + exit(2)
       - 正常             → 写 done 事件 + exit(0)
       - 启动/导入失败     → 写 error 事件 + exit(3)
  6. finally：仅在 run_* 调用已返回之后（其内部 ProcessPoolExecutor 已退出），
     对 multiprocessing.active_children() 逐一 terminate() 兜底清理泄漏 worker

**绝不 import ui / 任何 GUI 模块**，避免 Windows spawn 重导 GUI。
"""
import sys
import json
import argparse
import multiprocessing


from engine.backtest_protocol import (
    ExitCode,
    FlagFileEvent,
    ProgressWriter,
)


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog='backtest_cli',
        description='回测子进程（内部使用，由 --backtest-cli 触发）',
    )
    # --backtest-cli 仅作哨兵标志，触发 self-spawn 分支
    parser.add_argument('--backtest-cli', action='store_true',
                        help='内部使用：以子进程回测 CLI 模式启动')
    parser.add_argument('--config', required=True, help='临时 config JSON 路径')
    parser.add_argument('--mode', required=True,
                        choices=['strategy', 'scoreic', 'factoric', 'futures', 'futuresic', 'refresh'])
    parser.add_argument('--progress', required=True, help='进度 JSONL 文件路径')
    parser.add_argument('--cancel', required=True, help='取消 flag 文件路径')
    parser.add_argument('--offline', action='store_true',
                        help='离线模式：仅使用本地缓存，未命中缓存的股票直接跳过，不联网抓取')
    parser.add_argument('--dry-run', action='store_true',
                        help='仅加载 config、注入 _cache、打印摘要后 exit(0)，用于调试配置传递')
    return parser.parse_args(argv)


def _emit_error(progress_path, message):
    """启动阶段（ProgressWriter 尚未构造或构造失败）的兜底错误写入。"""
    try:
        ProgressWriter(progress_path).error(message)
    except Exception:
        pass


def _config_summary(loaded):
    if not isinstance(loaded, dict):
        return f"config 非 dict 类型: {type(loaded)}"
    parts = [
        f"preset      = {loaded.get('preset')}",
        f"active_factors = {loaded.get('active_factors')}",
    ]
    rp = loaded.get('_run_params')
    if rp:
        parts.append(f"run_params  = {rp}")
    return "\n".join(parts)


def _run_mode(mode, loaded, cancel_event, progress_writer, args):
    """按 mode 调用对应的 run_*（backtest_runner 在调用前才 import）。

    运行参数（years/full_market/hold/scan/prefix）由父进程打包进 config 的
    `_run_params` 子键，避免污染 quant_config._cache 顶层结构。
    """
    from engine import backtest_runner

    params = loaded.get('_run_params', {}) if isinstance(loaded, dict) else {}
    # years 来自 UI 运行面板：UI 不传回测年数时为 None，由 backtest_runner 走「数据驱动
    # 区间」（回测区间 = 扫描缓存实际可用范围，约300交易日，用满数据）；传数字则由上层
    # 按 years 推算区间（开发者/CLI 手动覆盖用）。
    years = params.get('years')
    full_market = bool(params.get('full_market', False))
    pool = params.get('pool', None)  # '148' | 'full' | 'watchlist' | None(兼容旧配置)
    hold = int(params.get('hold', 30))
    scan = int(params.get('scan', 5))
    prefix = str(params.get('prefix', 'bt'))
    # 离线模式：仅使用本地缓存，未命中缓存的股票直接跳过，不联网抓取
    offline = bool(params.get('offline', False)) or getattr(args, 'offline', False)
    direction = str(params.get('direction', 'long'))
    period = str(params.get('period', '日线'))
    progress_cb = progress_writer.log

    if mode == 'strategy':
        # 策略时间止损固定从 config 的 risk_params.time_stop_days 读取（见
        # StrategyBacktester.__init__ 与 run_strategy 文档），不再从面板 hold 覆盖
        return backtest_runner.run_strategy(
            years=years, full_market=full_market,
            scan_interval=scan, prefix=prefix, progress_cb=progress_cb,
            cancel_event=cancel_event, pool=pool,
            offline=offline,
            # 净值实时点：写独立 type='equity' 事件，供 UI 边跑边画曲线
            equity_cb=progress_writer.equity,
        )
    if mode == 'scoreic':
        return backtest_runner.run_score_ic(
            years=years, full_market=full_market, hold_days=hold,
            sample_interval=scan, prefix=prefix, progress_cb=progress_cb,
            cancel_event=cancel_event, pool=pool,
            offline=offline,
        )
    if mode == 'futures':
        # 期货策略 PnL 回测：全生命周期，方向/周期取自对应期货 scheme
        return backtest_runner.run_futures(
            pool=pool or 'all', direction=direction, period=period,
            days=int(params.get('days', 300)), prefix=prefix,
            progress_cb=progress_cb, cancel_event=cancel_event,
            offline=offline,
        )
    if mode == 'futuresic':
        # 期货因子 IC 回测：复用 A股 IC 同套截面IC逻辑（market='futures' 开关，非 fork）
        return backtest_runner.run_futures_factor_ic(
            years=years, pool=pool or 'all', hold_days=hold,
            sample_interval=scan, direction=direction, period=period,
            prefix=prefix, progress_cb=progress_cb,
            cancel_event=cancel_event, offline=offline,
            multi_horizon=bool(params.get('multi_horizon', False)),
        )
    # factoric（A股）
    return backtest_runner.run_factor_ic(
        years=years, full_market=full_market, hold_days=hold,
        sample_interval=scan, prefix=prefix, progress_cb=progress_cb,
        cancel_event=cancel_event, pool=pool,
        offline=offline,
    )


def main(argv=None):
    args = _parse_args(argv)

    # —— 启动阶段：加载 config（失败 → error + exit(3)）——
    try:
        with open(args.config, 'r', encoding='utf-8') as f:
            loaded = json.load(f)
    except Exception as e:
        _emit_error(args.progress, f"配置加载失败: {e}")
        sys.exit(ExitCode.STARTUP_FAILED)

    # —— 注入 _cache：必须在 import/call backtest_runner 之前 ——
    try:
        from engine import quant_config
        quant_config._cache = loaded
    except Exception as e:
        _emit_error(args.progress, f"配置注入失败（quant_config._cache）: {e}")
        sys.exit(ExitCode.STARTUP_FAILED)

    cancel_event = FlagFileEvent(args.cancel)
    progress_writer = ProgressWriter(args.progress)

    # —— 调试入口：仅打印配置摘要后退出（不跑回测）——
    if args.dry_run:
        try:
            print("[dry-run] 配置摘要:\n" + _config_summary(loaded))
        except Exception as e:
            print(f"[dry-run] 摘要生成失败(不影响): {e}")
        sys.exit(ExitCode.SUCCESS)

    # —— 授权硬门禁（引擎层，回测子进程）：即便父进程 can_use 被 patch，子进程也拒绝 ——
    try:
        from license.license_guard import enforce as _enforce_license
        _enforce_license('query')
    except Exception as _lic_e:
        _emit_error(args.progress, '授权受限: ' + str(_lic_e))
        sys.exit(ExitCode.STARTUP_FAILED)

    # —— 运行（try/finally 保证 run_* 返回后才清理泄漏 worker）——
    # 先导入 BacktestCancelled 供 except 子句使用
    from engine.backtest_cancel import BacktestCancelled
    try:
        _run_mode(args.mode, loaded, cancel_event, progress_writer, args)
    except BacktestCancelled:
        progress_writer.cancel('用户取消（BacktestCancelled）')
        sys.exit(ExitCode.CANCELLED)
    except Exception as e:
        import traceback
        progress_writer.error(f"{e}\n{traceback.format_exc()}")
        sys.exit(ExitCode.ERROR)
    else:
        progress_writer.done('回测完成')
        sys.exit(ExitCode.SUCCESS)
    finally:
        # 仅在 run_* 调用已返回之后执行（内层 with ProcessPoolExecutor 必已退出）。
        # 兜底清理「泄漏到 run_* 之外的僵尸 worker」，绝不提前 terminate 正在运行的 worker（见 R3）。
        for child in multiprocessing.active_children():
            try:
                child.terminate()
            except Exception:
                pass


if __name__ == '__main__':
    main()
