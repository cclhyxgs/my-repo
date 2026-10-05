# quant_model.json — Schema Reference & Factor Registry

This file is the detailed reference backing `SKILL.md`. Load it when exact field names, the factor table, or the
veto-key whitelist are needed. All numbers below are read from `engine/factor_registry.py` and the delivered
`A股轮动均值回归技术面` scheme in `config/quant_model.json`.

## 1. Top-level structure

```jsonc
{
  "current_scheme": "<scheme-name>",          // which scheme the tool loads by default
  "schemes": {
    "<scheme-name>": {
      "label": "人类可读名",
      "desc": "一句话说明策略逻辑",
      "config": { /* see §2 */ }
    }
  }
}
```

- Preserve all pre-existing schemes when inserting a new one.
- Back up the file (`config/quant_model.json.bak`) before any write.

## 2. `config` block (field-by-field)

| Field | Type | Notes |
|-------|------|-------|
| `preset` | string | `"custom"` for hand-built schemes |
| `active_factors` | string[] | Subset of REGISTRY names (see §3). **Required.** |
| `factor_configs` | object | `{name: {weight, direction, params, stats}}` for every active factor |
| `conflict_penalty` | object | volume-down / up-shrink penalties; deep+extreme drawdown bonus+threshold; penalty clamp |
| `score_scale` | object | `weight_multiplier:30`, `z_truncate_min/max:±3`, `score_min/max:±100` |
| `market_env` | object | `{brackets:[{up_ratio_min, adjust}]}` — 8 ascending thresholds |
| `thresholds` | object | `strong/standard/test/pending/rebound/panic_rebound` score cutoffs |
| `entry_params` | object | `positions` map + reversal thresholds + rebound factors |
| `entry_conditions` | object | per class `{tech_signal, veto_on, veto_enabled:{...}}` |
| `add_params` | object | `type_to_row` + `trigger_defs` + `add_tiers` |
| `reduce_params` | object | `type_to_row` + `trigger_defs` + `reduce_tiers` |
| `risk_params` | object | drawdown / position caps / stop thresholds |
| `entry_condition` | object | `volatility` + `score_bias` sub-blocks |

### `factor_configs.<name>` shape

```jsonc
"<name>": {
  "weight": 0.18,                 // must sum to 1.0 across active_factors
  "direction": 1,                 // +1 or -1 only (IC-calibrated, see §3)
  "params": { "ma_period": 10 }, // from REGISTRY params_schema defaults
  "stats": { "mean": 0.0, "std": 20.0 }  // z-score standardization params
}
```

### `conflict_penalty` (consensus defaults)

```jsonc
{
  "volume_down_penalty": -8.0,
  "volume_up_shrink_penalty": -6.0,
  "deep_drawdown_div_bonus": 10.0,
  "deep_drawdown_threshold": 15.0,
  "extreme_drawdown_bonus": 5.0,
  "extreme_drawdown_threshold": 20.0,
  "penalty_min": -12.0,
  "penalty_max": 12.0
}
```

### `market_env.brackets` (ascending `up_ratio_min`)

```jsonc
[
  {"up_ratio_min": 0.8, "adjust": 8},
  {"up_ratio_min": 0.65, "adjust": 12},
  {"up_ratio_min": 0.5, "adjust": 4},
  {"up_ratio_min": 0.4, "adjust": 0},
  {"up_ratio_min": 0.3, "adjust": -4},
  {"up_ratio_min": 0.2, "adjust": -8},
  {"up_ratio_min": 0.1, "adjust": -12},
  {"up_ratio_min": 0.0, "adjust": -15}
]
```
Breadth collapse (< 0.3) → negative `adjust` → engine auto "停新开".

### `thresholds` (consensus mean-reversion defaults)

```jsonc
{"strong": 22, "standard": 20, "test": 10, "pending": -5, "rebound": -2, "panic_rebound": -8}
```

### `entry_conditions` — `tech_signal` whitelist

Valid values per class: `bull_align` | `above_ma20` | `volume_up` | `none`.

Veto keys allowed in `veto_enabled`: **VETO_KEYS** =
`rsi_extreme`, `kdj_extreme`, `macd_high_dead_cross`, `ma250_break`, `bb_lower_break_widen`, `volume_stagnant`.

Strong/standard/test/pending set `veto_on:true` with the 5 active keys above (`ma250_break:false`).
`rebound` / `panic_rebound` set `veto_on:false`, `veto_enabled:{}`.

### `risk_params` (consensus)

```jsonc
{
  "max_single_position": 0.30,
  "total_position_cap_pct": 0.60,
  "time_stop_days": 10,
  "time_stop_min_profit_pct": 0.05,
  "single_max_loss_pct": 0.08,
  "market_crash_pct": 0.05,
  "market_crash_index": "sh000300"
}
```

## 3. Factor Registry (REGISTRY in `engine/factor_registry.py`)

20 candidate factors. Only these names may appear in `active_factors`. `v2方向` is the calibrated direction used by
the engine (do not flip for style tilt). `ic` is the information coefficient from v2 backtests (None = no backtest).

| name | 中文 | 分类 | ic | v2方向 | 默认权重 | stats(mean/std) | 说明 |
|------|------|------|----|--------|----------|-----------------|------|
| `relative_strength_20d` | 20日相对强度 | momentum | 0.002 | +1 | 0.167 | 0.02 / 0.15 | IC≈0，A股10日动量无预测力 → 均值回归市剔除 |
| `rsi_value` | RSI | momentum | 0.020 | +1 | 0.10 | 50 / 15 | 高RSI预测正收益，动量>回归 |
| `macd_hist_norm` | MACD柱 | momentum | -0.012 | -1 | 0.123 | 0.0 / 0.02 | 高MACD柱预测负收益 |
| `kdj_signal` | KDJ信号 | momentum | None | +1 | 0.10 | 0.0 / 10.0 | K-D差值，金叉区为正 |
| `ma_slope` | MA20斜率 | trend | 0.011 | +1 | 0.155 | 0.0 / 0.03 | 趋势方向 |
| `di_spread` | DI方向运动 | trend | 0.013 | +1 | 0.146 | 0.0 / 15.0 | +DI−-DI多空对比 |
| `ma_arrangement` | 均线排列 | trend | 0.010 | +1 | 0.10 | 0.0 / 2.0 | +3完美多头 ~ -3完美空头 |
| `bias_value` | 乖离率 | trend | 0.003 | -1 | 0.10 | 0.0 / 5.0 | 偏离均线过大有回归需求 |
| `adx_trend_strength` | ADX趋势强度 | trend | None | +1 | 0.10 | 25 / 10 | >25为趋势市 |
| `volume_ratio` | 量比 | volume | -0.009 | -1 | 0.10 | 1.0 / 0.5 | 放量预测负收益 |
| `obv_trend` | OBV趋势 | volume | 0.042 | +1 | 0.10 | 0.0 / 20.0 | **最强因子** t=2.92 |
| `volume_price_signal` | 量价关系 | volume | -0.003 | +1 | 0.10 | 0.0 / 2.0 | +2配合上涨 ~ -2配合下跌 |
| `bb_bandwidth` | 布林带宽 | volatility | 0.038 | +1 | 0.149 | 15 / 10 | **第二强因子** |
| `atr_norm` | ATR波动率 | volatility | 0.021 | +1 | 0.10 | 0.02 / 0.01 | 归一化波动率 |
| `current_drawdown` | 当前回撤 | volatility | -0.018 | -1 | 0.119 | 8 / 12 | 高回撤预测负收益 |
| `volatility_cone` | 波动率锥 | volatility | -0.035 | -1 | 0.10 | 50 / 25 | **第三强因子** t=-2.60 |
| `pattern_reverse` | K线形态(反向) | pattern | -0.009 | -1 | 0.091 | 0.0 / 1.5 | 看涨形态反而易跌(contrarian) |
| `fib_position` | 斐波那契位置 | pattern | 0.019 | +1 | 0.10 | 0.5 / 0.25 | 0=强势 1=弱势 |
| `pivot_distance` | 轴心点距离 | pattern | -0.002 | +1 | 0.10 | 0.0 / 5.0 | 相对枢轴点偏离% |
| `chip_concentration` | 筹码集中度 | pattern | 0.022 | +1 | 0.10 | 0.3 / 0.2 | 密集区成交量占比，正向预测 |

### Delivered consensus set (for reference)

The `A股轮动均值回归技术面` scheme uses exactly these 10 factors, weights summing to 1.0, directions per the table above:

```
obv_trend        0.18  +1
bb_bandwidth     0.16  +1
volatility_cone  0.14  -1
chip_concentration 0.11 +1
atr_norm         0.09  +1
rsi_value        0.09  +1
current_drawdown 0.08  -1
fib_position     0.06  +1
di_spread        0.05  +1
ma_slope         0.04  +1
```

## 4. Scoring math (for sanity-checking)

```
stock_score = clamp( Σ clamp(z·direction, ±3) · weight · 30 , ±100 )
where z = (factor_value − stats.mean) / stats.std
```

`conflict_penalty` is applied separately (not inside the score). `market_env.adjust` is added to the entry score
before comparing against `thresholds`.
