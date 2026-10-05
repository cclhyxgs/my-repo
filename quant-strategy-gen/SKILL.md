---
name: quant-strategy-gen
description: Generate a valid quant_model.json scheme (config consumed by the project's quant-model configuration tool) driven by the live A-share market regime from a finance-data connector such as 腾讯自选股 (westock-mcp). Use when the user asks to derive, design, or create a new technical / multi-factor trading strategy and produce the JSON the quant tool imports. This skill encodes the full reproducible workflow — read the tool's config schema + factor registry, classify the current regime, build an IC-weighted factor set with normalized weights and IC-calibrated directions, map entry/add/reduce/risk/veto rules, verify engine-compatibility, and write the scheme into config/quant_model.json. Reusable so any finance-capable AI can regenerate a strategy and import it into the tool.
agent_created: true
---

# Quant Strategy Gen

## Overview

Turn a natural-language request ("帮我分析出一套当下有用且适合 A 股市场的技术面策略，按量化模型配置工具生成 JSON")
into a **valid, engine-compatible scheme** written into `config/quant_model.json` — the single source of truth for the
project's quant-model configuration generator.

The deliverable is a `schemes.<name>.config` object the tool loads. The generator's engine (`engine/`) consumes it
without modification, so the JSON must satisfy the factor registry (`engine/factor_registry.py`) and the veto-key
whitelist (`engine/quant_config.py`).

## When to use

- User asks to create, derive, or design a new trading-strategy config for the quant tool.
- User asks for a "当下有用 / 适合 X 市场" strategy and wants a JSON to import into the tool.
- User wants to (re)generate `quant_model.json` from live market data.
- User wants the strategy-derivation workflow packaged for reuse by another finance-capable AI.

Do **not** use for: backtesting (separate tooling), single-stock picks, or any non-config deliverable.

## Source of truth (read FIRST)

Before generating anything, parse these two files from the current project:

1. `config/quant_model.json` — the schema (full field reference in `references/schema.md`). Note the top-level
   `current_scheme` and the `schemes` map. Never overwrite a scheme blindly: **back up the original**
   (`config/quant_model.json.bak`) before writing.
2. `engine/factor_registry.py` — `REGISTRY` of candidate factors. Each factor declares `ic`, `default_direction`,
   `default_weight`, `default_stats`, `params_schema`. **Only factor names present in REGISTRY may appear in
   `active_factors`.**

Load `references/schema.md` whenever exact field names or the factor table are needed.

## Workflow

### Step 1 — Classify the live market regime (finance-data connector)

Pull a real snapshot; do not assume. With the 腾讯自选股 connector (`westock-mcp`, alias `westock-data`):

- `data_market_overview` → breadth `up_ratio` (上涨家数 / 总数). The config's `market_env.brackets` use `up_ratio_min`
  thresholds (0.8 / 0.65 / 0.5 / 0.4 / 0.3 / 0.2 / 0.1 / 0.0).
- `data_quote` on a benchmark (e.g. 上证指数 `000001`) → level, RSI_6, BOLL lower band.
- `data_technical` on representative sectors → 20d return, overbought / oversold state.
- `data_sector` → rotating vs. trending leadership.

Map snapshot → regime label (e.g. "轮动+均值回归市", "趋势市", "恐慌市"). The regime drives both **factor selection**
(momentum factor `relative_strength_20d` has IC≈0 in A-shares → drop it in mean-reversion regimes) and the
**`market_env` brackets / `thresholds`** (if the current `up_ratio` < 0.3, the scheme should auto "停新开" via a
negative `adjust`).

If no finance connector is available, state the limitation and proceed with explicit, clearly-marked assumptions — never
fabricate numbers.

### Step 2 — Derive the factor set (IC-weighted, direction-calibrated)

Build `active_factors` + `factor_configs` from REGISTRY:

1. **Select** factors whose `ic` (or v2-calibrated economic sense) supports the regime thesis. Prefer higher-`ic`
   factors: OBV trend (`ic=0.042`), BB bandwidth (`ic=0.038`), volatility cone (`ic=-0.035`), chip concentration
   (`ic=0.022`), ATR (`ic=0.021`), RSI (`ic=0.020`), fib (`ic=0.019`).
2. **Direction** = the factor's v2-calibrated direction, NOT the user's intuition. Key calibs: `rsi_value +1`,
   `volatility_cone -1`, `current_drawdown -1`, `atr_norm +1`, `obv_trend +1`, `bb_bandwidth +1`,
   `chip_concentration +1`, `fib_position +1`, `di_spread +1`, `ma_slope +1`. Express a mean-reversion tilt via
   **weights**, never by flipping directions.
3. **Normalize weights** so they sum to exactly **1.0** (engine divides by the sum internally, but keep sum = 1.0 for
   clarity and to pass verification). Round to 2–4 decimals; total must equal 1.0 within 1e-6.
4. **stats** = `{mean, std}` from REGISTRY `default_stats` (used for z-score standardization). Keep them; do not zero them.
5. **params** = each factor's `params_schema` defaults.

(Optional, recommended for non-trivial requests) Run the multi-expert roundtable via the `stock-partner-team` expert to
collect sector / contrarian / short-term views, then reconcile them into the factor set at the integration step. The
roundtable is a *reasoning* aid — the final JSON is still produced by this skill's steps.

### Step 3 — Map execution rules to schema

Fill the remaining config blocks (full field list in `references/schema.md`):

- `conflict_penalty`: volume-down / up-shrink penalties, deep-drawdown bonus + threshold, extreme-drawdown bonus +
  threshold, penalty clamp min / max.
- `score_scale`: `weight_multiplier: 30.0`, z-truncate ±3.0, score clamp ±100.0 (matches engine math:
  `score = clamp(Σ clamp(z·dir, ±3)·weight·30, ±100)`).
- `market_env.brackets`: 8 ascending `up_ratio_min` → `adjust` (integer added to the entry score). Panic regimes get the
  most negative `adjust`.
- `thresholds`: `strong / standard / test / pending / rebound / panic_rebound` score cutoffs. Consensus mean-reversion
  defaults: `22 / 20 / 10 / -5 / -2 / -8`.
- `entry_params.positions`: per-class position fraction (strong 0.2 … none 0.0).
- `entry_conditions`: per class, `tech_signal` ∈ {`bull_align`, `above_ma20`, `volume_up`, `none`} and `veto_on` +
  `veto_enabled`. **`veto_enabled` keys MUST be a subset of the fixed VETO_KEYS** (see below). For `rebound` /
  `panic_rebound` leave `veto_on:false` and `veto_enabled:{}`.
- `add_params` / `reduce_params`: `type_to_row`, `trigger_defs`, `add_tiers` / `reduce_tiers`. Copy the trigger
  vocabulary from an existing scheme (e.g. the delivered `A股轮动均值回归技术面` scheme in `config/quant_model.json`) and
  tune ratios.
- `risk_params`: `max_single_position` (0.30), `total_position_cap_pct` (0.60),
  `single_max_loss_pct` (0.08), `market_crash_pct` (0.05), `market_crash_index` ('sh000300'，大盘熔断基准指数：sh000300沪深300/sh000001上证/sh000985中证全指等).
- `entry_condition`: `volatility` (ATR hi / lo thresholds, MA shrink / stretch) + `score_bias` (amplify / dampen).

**VETO_KEYS (the only valid `veto_enabled` keys):**
`rsi_extreme`, `kdj_extreme`, `macd_high_dead_cross`, `ma250_break`, `bb_lower_break_widen`, `volume_stagnant`.
Any other key is invalid and will be flagged by the verifier.

### Step 4 — Verify engine-compatibility

Run the bundled verifier (stdlib only; no `pandas` needed):

```bash
python scripts/verify_quant_config.py config/quant_model.json [SCHEME_NAME]
```

For the target scheme (default = `current_scheme`) it checks:

- every `active_factors` name exists in REGISTRY;
- factor weights sum to 1.0 (±1e-6);
- every `direction` ∈ {+1, −1};
- every factor_config has `weight`, `direction`, `params` (dict), `stats.{mean,std}`;
- every `entry_conditions.*.veto_enabled` key ∈ VETO_KEYS.

Fix all reported errors before writing. **Never write a config that fails verification.**

### Step 5 — Write the scheme

1. Back up: `cp config/quant_model.json config/quant_model.json.bak`.
2. Insert the new scheme under `schemes.<name>` with `label` + `desc`, and set top-level `current_scheme` to `<name>`.
   Preserve all other existing schemes.
3. Re-run the verifier on the written file to confirm.

### Step 6 — (Optional) Render a report

If the user wants a human-readable rationale, produce a 4-module roundtable report
(结论卡 / 子专家观点 / 深度思考 / 后续关注) and render it via the `md-to-html` skill. Keep this optional — the JSON is the
primary deliverable.

## Pitfalls (learned)

- **Managed Python lacks `pandas`** — `engine/` imports pandas transitively. Do NOT `import` engine modules in
  verification scripts; hardcode the REGISTRY names + VETO_KEYS instead (as `verify_quant_config.py` does).
- **`init_task` may be unavailable** — treat the roundtable session-init as best-effort / fail-safe; do not block on it.
- **Weights must sum to 1.0** — the engine normalizes by sum, but verification and human review expect exactly 1.0.
- **Direction ≠ intuition** — keep IC-calibrated directions; express style tilt via weights only. A frequent dispute:
  should `current_drawdown` flip to +1 for "buy the dip"? No — keep −1; the mean-reversion tilt comes from weighting
  `volatility_cone` / drawdown / RSI appropriately, not from flipping the sign.
- **`up_ratio` brackets drive auto "停新开"** — if the live snapshot shows a breadth collapse (< 0.3), the scheme's
  `adjust` must go negative so the engine stops opening new positions automatically.

## Resources

- `references/schema.md` — full `quant_model.json` field reference + the 20-factor REGISTRY table (name / label /
  category / ic / v2 direction / default weight / default stats) + VETO_KEYS.
- `scripts/verify_quant_config.py` — stdlib-only verifier for engine-compatibility (weights, factors, directions,
  veto keys).
