#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_quant_config.py — stdlib-only verifier for quant_model.json engine-compatibility.

Usage:
    python verify_quant_config.py [JSON_PATH] [SCHEME_NAME]

    JSON_PATH    path to config/quant_model.json (default: config/quant_model.json, relative to cwd)
    SCHEME_NAME  scheme key under "schemes" (default: the file's "current_scheme")

This script deliberately does NOT import engine.* (managed Python lacks pandas). The REGISTRY
factor names and VETO_KEYS are hardcoded from engine/factor_registry.py and engine/quant_config.py.

Checks performed on the target scheme:
    1. every active_factors name exists in REGISTRY
    2. factor weights sum to 1.0 (±1e-6)
    3. every direction is +1 or -1
    4. every factor_config has weight, direction, params(dict), stats.{mean,std}
    5. every entry_conditions.*.veto_enabled key is in VETO_KEYS

Exit code 0 = OK, 1 = errors found.
"""
import json
import os
import sys

# ---- Hardcoded from engine/factor_registry.py (REGISTRY) ----
REGISTRY = {
    "relative_strength_20d", "rsi_value", "macd_hist_norm", "kdj_signal",
    "ma_slope", "di_spread", "ma_arrangement", "bias_value", "adx_trend_strength",
    "volume_ratio", "obv_trend", "volume_price_signal",
    "bb_bandwidth", "atr_norm", "current_drawdown", "volatility_cone",
    "pattern_reverse", "fib_position", "pivot_distance", "chip_concentration",
    "doji",
}

# ---- Hardcoded from engine/veto_registry.py (VETO_REGISTRY, 12项 = 6多 + 6空) ----
VETO_KEYS = {
    "rsi_extreme", "kdj_extreme", "macd_high_dead_cross",
    "ma250_break", "bb_lower_break_widen", "volume_stagnant",
    "rsi_extreme_low", "kdj_extreme_low", "macd_low_gold_cross",
    "ma250_break_up", "bb_upper_break_widen", "volume_stagnant_down",
}

WEIGHT_EPS = 1e-6


def err(errors, msg):
    errors.append(msg)


def verify_scheme(name, scheme, errors):
    cfg = scheme.get("config", {})
    if not isinstance(cfg, dict):
        err(errors, f"[{name}] 'config' is missing or not an object")
        return

    active = cfg.get("active_factors", [])
    if not isinstance(active, list) or not active:
        err(errors, f"[{name}] 'active_factors' must be a non-empty list")
    for f in active:
        if f not in REGISTRY:
            err(errors, f"[{name}] active_factors contains unknown factor '{f}' (not in REGISTRY)")

    fconfigs = cfg.get("factor_configs", {})
    total_w = 0.0
    for f in active:
        fc = fconfigs.get(f)
        if not isinstance(fc, dict):
            err(errors, f"[{name}] factor_configs['{f}'] missing")
            continue
        w = fc.get("weight")
        if not isinstance(w, (int, float)):
            err(errors, f"[{name}] factor '{f}' weight missing/non-numeric")
        else:
            total_w += float(w)
        d = fc.get("direction")
        if d not in (1, -1, 1.0, -1.0):
            err(errors, f"[{name}] factor '{f}' direction must be +1 or -1 (got {d!r})")
        if not isinstance(fc.get("params"), dict):
            err(errors, f"[{name}] factor '{f}' params must be an object")
        stats = fc.get("stats")
        if not isinstance(stats, dict) or "mean" not in stats or "std" not in stats:
            err(errors, f"[{name}] factor '{f}' stats must have mean & std")
    if abs(total_w - 1.0) > WEIGHT_EPS:
        err(errors, f"[{name}] factor weights sum = {total_w:.6f} (must be 1.0 ± {WEIGHT_EPS})")

    # entry_conditions veto keys
    ecs = cfg.get("entry_conditions", {})
    if isinstance(ecs, dict):
        for cls, ec in ecs.items():
            if not isinstance(ec, dict):
                continue
            ve = ec.get("veto_enabled", {})
            if not isinstance(ve, dict):
                err(errors, f"[{name}] entry_conditions.{cls}.veto_enabled must be an object")
                continue
            for k in ve.keys():
                if k not in VETO_KEYS:
                    err(errors, f"[{name}] entry_conditions.{cls}.veto_enabled has invalid key '{k}' "
                                f"(not in VETO_KEYS)")


def main():
    json_path = sys.argv[1] if len(sys.argv) > 1 else "config/quant_model.json"
    scheme_arg = sys.argv[2] if len(sys.argv) > 2 else None

    if not os.path.isfile(json_path):
        print(f"ERROR: file not found: {json_path}")
        sys.exit(1)

    with open(json_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    errors = []
    schemes = data.get("schemes", {})
    if not isinstance(schemes, dict) or not schemes:
        err(errors, "top-level 'schemes' missing or empty")
    else:
        target = scheme_arg or data.get("current_scheme")
        if target is None:
            err(errors, "'current_scheme' not set and no SCHEME_NAME given; verifying ALL schemes")
            targets = list(schemes.keys())
        elif target not in schemes:
            err(errors, f"current_scheme/SCHEME_NAME '{target}' not found under 'schemes'")
            targets = []
        else:
            targets = [target]

        for t in targets:
            verify_scheme(t, schemes[t], errors)

    if errors:
        print(f"FAIL: {json_path}")
        for e in errors:
            print("  - " + e)
        sys.exit(1)
    print(f"OK: {json_path} 结构/权重/因子/否决项 全部校验通过"
          + (f" (scheme={target})" if 'target' in dir() and targets else ""))


if __name__ == "__main__":
    main()
