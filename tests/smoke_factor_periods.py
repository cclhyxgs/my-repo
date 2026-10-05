# -*- coding: utf-8 -*-
"""冒烟：均线排列 / 波动率锥 / 当前回撤 的周期参数生效验证。
分别验证：默认周期 = 原固定逻辑（零回归），自定义周期改变计算口径（配置即生效）。
"""
import numpy as np
from engine.indicators import MATechnical, VolatilityCone, DrawdownCalculator
from engine.factor_registry import calc_factor_value


def _rising(n=400, start=10.0, step=0.05, noise=0.0):
    rng = np.random.default_rng(0)
    base = start + np.arange(n) * step
    return base + rng.normal(0, noise, n)


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"冒烟失败: {name}")


def main():
    closes = _rising(400).astype(float)

    print("== 1) 均线排列 ==")
    # 默认基准 20 派生档位应等于原固定档位
    check("ma_periods(20) == [5,10,20,60,120,250]",
          MATechnical.ma_periods(20) == [5, 10, 20, 60, 120, 250])
    ma_def = MATechnical.judge_arrangement(closes, 20)
    # 原固定判据 (5/10/20/60/120/250) 仍应判多头
    check(f"period=20 -> 多头排列 (got {ma_def})", '多头' in ma_def)
    # 配置更大基准（如 60 → 15/30/60/180/360/750）不报错且细条均线组不同
    ma_big = MATechnical.judge_arrangement(closes, 60)
    check(f"period=60 可运行 (got {ma_big})", isinstance(ma_big, str) and ma_big)
    # 因子打分走配置周期
    ctx = {'closes': closes, 'highs': closes, 'lows': closes, 'volumes': np.ones(len(closes)),
           'opens': closes, 'data_list': [], 'latest_price': float(closes[-1]), 'tech': {}, 'market': {}}
    check("calc_factor_value(ma_arrangement, period=20) 可运行",
          isinstance(calc_factor_value('ma_arrangement', ctx, {'period': 20}), float))

    print("== 2) 波动率锥 ==")
    from engine.indicators import VolatilityCone
    cone20 = VolatilityCone.calc(closes, lookback=20)
    cone60 = VolatilityCone.calc(closes, lookback=60)
    check("lookback=20 => 含 vol_20d", 'vol_20d' in cone20)
    check("lookback=60 => 含 vol_60d", 'vol_60d' in cone60)
    v20 = calc_factor_value('volatility_cone', ctx, {'period': 20})
    v60 = calc_factor_value('volatility_cone', ctx, {'period': 60})
    check(f"period=20 读出 vol_20d 分位值 {v20:.2f}",
          abs(v20 - cone20['vol_20d']['percentile']) < 1e-9)
    check(f"period=60 读出 vol_60d 分位值 {v60:.2f}",
          abs(v60 - cone60['vol_60d']['percentile']) < 1e-9)

    print("== 3) 当前回撤 ==")
    dd_full = calc_factor_value('current_drawdown', ctx, {})
    dd_win = calc_factor_value('current_drawdown', ctx, {'lookback': 20})
    check(f"缺省回看 = 整段历史回撤 {dd_full:.2f}%",
          abs(dd_full - DrawdownCalculator.calc(closes)['current_dd']) < 1e-9)
    check(f"lookback=20 口径 {dd_win:.2f}%",
          abs(dd_win - DrawdownCalculator.calc(closes, lookback=20)['current_dd']) < 1e-9)

    print("\n全部通过。")


if __name__ == '__main__':
    main()