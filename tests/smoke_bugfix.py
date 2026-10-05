"""冒烟测试：覆盖本次 BUG1~BUG6 修复点的边界场景。

不依赖真实行情数据，直接构造异常输入（0价、负价、0均值、空配置、非dict band等）。
"""
import sys
import os
import math
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd  # noqa: F401  (factor_tech 内部依赖)


PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


# ============================================================
# BUG1: indicators.py 均线排列 / 黏合 除零
# ============================================================
def test_bug1_ma_zero():
    from engine.indicators import MATechnical
    print("\n[BUG1] 均线排列/黏合 除零守卫")

    # 构造 260 根 0 价格（模拟未上市/异常），应不抛 ZeroDivisionError
    zeros = [0.0] * 260
    try:
        res = MATechnical.judge_arrangement(zeros)
        check("全部价格为0 不崩溃", isinstance(res, str), f"got={res!r}")
    except ZeroDivisionError as e:
        check("全部价格为0 不崩溃", False, f"ZeroDivisionError: {e}")
    except Exception as e:
        check("全部价格为0 不崩溃", False, f"{type(e).__name__}: {e}")

    # 构造 ma_min=0 的场景：ma5/ma10/ma20 正常，ma60=0 → 黏合分支除零
    closes = [10.0] * 60 + [0.0]  # 前60根10，第61根0 => ma60≈9.83，不满足ma60=0
    # 更直接：前60根为0，再给 250-60 根 10 → ma60初期会是0
    closes2 = [0.0] * 60 + [10.0] * 220
    try:
        res = MATechnical.judge_arrangement(closes2)
        check("ma60/ma20 包含0值 不崩溃", isinstance(res, str), f"got={res!r}")
    except ZeroDivisionError as e:
        check("ma60/ma20 包含0值 不崩溃", False, f"ZeroDivisionError: {e}")


# ============================================================
# BUG2: factor_tech.py 布林带 / z-score / 乖离率 除零 + inf
# ============================================================
def test_bug2_factor_tech_safe():
    from engine.factor_tech import _bb_series, _volume_z_series, _bias_series
    print("\n[BUG2] factor_tech 除零/inf 守卫")

    n = 300
    # 构造全同价序列：std=0 → z-score 分母为0；ma=price → bias分母=price(>0)；布林带带宽分母=ma>0
    flat_closes = [10.0] * n
    flat_vols = [1000.0] * n

    try:
        # 2a. _bb_series 全同价：带宽计算中 ma=10 不为0，但 upper-lower=0 → 带宽0  OK
        bb = _bb_series(pd.Series(flat_closes))
        # 注意：rolling 窗口不足 period 时 upper/middle/lower 含 NaN 属正常；我们只禁止 inf
        has_inf = any(np.isinf(x) for arr in bb.values() for x in np.asarray(arr, dtype=float).ravel())
        # bandwidth/pct_b 不允许 NaN（这两个是修复重点）
        bw_nan = np.isnan(np.asarray(bb['bandwidth'], dtype=float)).any()
        pctb_nan = np.isnan(np.asarray(bb['pct_b'], dtype=float)).any()
        check("布林带_全同价_无inf_带宽和pctb无nan",
              not has_inf and not bw_nan and not pctb_nan,
              f"inf={has_inf}, bw_nan={bw_nan}, pctb_nan={pctb_nan}")

        # 2b. _bb_series 含 0 价格段 → ma 可能为0 → bandwidth 不应 inf，pct_b/bandwidth 无 nan
        zero_then = [0.0] * 100 + [10.0] * 200
        bb2 = _bb_series(pd.Series(zero_then))
        has_inf = any(np.isinf(x) for arr in bb2.values() for x in np.asarray(arr, dtype=float).ravel())
        bw_nan = np.isnan(np.asarray(bb2['bandwidth'], dtype=float)).any()
        pctb_nan = np.isnan(np.asarray(bb2['pct_b'], dtype=float)).any()
        check("布林带_前100根价格0_无inf_带宽pctb无nan",
              not has_inf and not bw_nan and not pctb_nan,
              f"inf={has_inf} bw样例={bb2['bandwidth'][95:115]}")

        # 2c. _volume_z_series 全同量 → std=0 → 不应 inf
        z, m = _volume_z_series(flat_vols)
        check("量z_全同量_std0_无inf_nan",
              np.isfinite(z).all() and np.isfinite(m).all(),
              f"z[:25]={z[:25]}")

        # 2d. _bias_series 含 0 价格段 → ma 可能为0 → 不应 inf
        bs = _bias_series(zero_then)
        bad = []
        for k, v in bs.items():
            if not np.isfinite(v).all():
                bad.append((k, v[:110]))
        check("乖离率_含0价段_无inf_nan", not bad, f"非法字段={bad[:2]}")

        # 2e. 用 scoring_core.build_tech 端到端（构造 tech 字典，应无异常/无inf）
        from engine.scoring_core import build_tech
        highs = [11.0] * n
        lows = [9.0] * n
        opens = [10.0] * n
        data_list = [{'open': opens[i], 'high': highs[i], 'low': lows[i],
                      'close': flat_closes[i], 'volume': flat_vols[i]} for i in range(n)]
        rsi_hist = [50.0] * n
        # build_tech 要求 macd_accel 是二元组：(数值, 状态标签)
        macd_accel_pair = (0.0, '中性')
        tech = build_tech(flat_closes, flat_vols, highs, lows, opens, data_list, rsi_hist, macd_accel_pair)
        inf_keys = []
        for k, v in tech.items():
            try:
                arr = np.asarray(v, dtype=float).ravel()
                if np.isinf(arr).any():
                    inf_keys.append(k)
            except (TypeError, ValueError):
                pass  # 字符串/dict 等非数值跳过
        check("build_tech_全同价_数值字段无inf", not inf_keys,
              f"含inf的键（len={len(inf_keys)}）={inf_keys[:5]}")
    except Exception as e:
        check("factor_tech 总体不崩溃", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()}")


# ============================================================
# BUG3: unified_entry_logic.py positions KeyError
# ============================================================
def test_bug3_positions_defaults():
    from engine import quant_config
    from engine.unified_entry_logic import UnifiedEntryLogic
    print("\n[BUG3] positions 缺省 KeyError 守卫")

    # 模拟 quant_config 返回不完整配置（少 standard/test 等键）
    original = quant_config.get_entry_params

    def fake_partial():
        return {'positions': {'strong': 0.25}}  # 只给 strong，其它缺

    def fake_empty():
        return {'positions': None}

    def fake_none():
        return None

    def fake_bad_values():
        return {'positions': {'strong': 'abc', 'observe': None}}

    fake_closes = [10.0 + i * 0.01 for i in range(260)]
    fake_vols = [1000.0] * 260
    fake_highs = [x + 0.2 for x in fake_closes]
    fake_lows = [x - 0.2 for x in fake_closes]
    fake_opens = list(fake_closes)
    fake_data_list = [{'open': fake_opens[i], 'high': fake_highs[i], 'low': fake_lows[i],
                       'close': fake_closes[i], 'volume': fake_vols[i]} for i in range(260)]

    from engine.scoring_core import build_tech
    rsi_hist = [50.0] * 260
    tech = build_tech(fake_closes, fake_vols, fake_highs, fake_lows, fake_opens, fake_data_list, rsi_hist, (0.0, '中性'))
    market = {'up_ratio': 0.5, 'ma_arrangement': '中性震荡', 'volume_price': '正常'}

    for name, fake in [("部分配置", fake_partial),
                       ("positions=None", fake_empty),
                       ("entry_params=None", fake_none),
                       ("配置值非法", fake_bad_values)]:
        quant_config.get_entry_params = fake
        try:
            pos = UnifiedEntryLogic._get_positions()
            ok_all = all(k in pos for k in ['strong', 'standard', 'test', 'observe'])
            check(f"{name} → positions字典完整", ok_all, f"pos keys={list(pos.keys())}")
            # 再走 check_entry 端到端，应不抛 KeyError
            r = UnifiedEntryLogic.check_entry(
                stock_score=60.0, final_score=60.0, tech=tech, market=market,
                up_ratio=0.5, latest_price=fake_closes[-1], adx_state={'state': 'trending_up'},
            )
            check(f"{name} → check_entry 不抛异常",
                  isinstance(r, dict) and 'position' in r,
                  f"r={r!r}")
        except KeyError as e:
            check(f"{name} → 无 KeyError", False, f"KeyError: {e}")
        except Exception as e:
            check(f"{name} → 无异常", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
        finally:
            quant_config.get_entry_params = original


# ============================================================
# BUG4: env_config / env 直接索引健壮性
# BUG5: quant_config get_market_gate_config None 守卫
# ============================================================
def test_bug4_bug5_env_config_robust():
    from engine import quant_config
    from engine.market_gate import MarketGate
    print("\n[BUG4/5] env_config 健壮性 & band None 守卫")

    # 模拟 _cfg 中 market_gate 结构异常（envs 全非 dict 或全空）
    original_cfg = quant_config._cfg
    original_market_gate_cfg = quant_config.get_market_gate_config

    def fake_cfg_bad_structure():
        return {
            'market_gate': {
                'enabled': True,
                'envs': [None, "我不是dict", 123, 3.14],  # envs 里没有 dict
            }
        }

    def fake_cfg_no_envs_key():
        return {'market_gate': {'enabled': True}}  # 根本没有 envs

    quant_config._cfg = fake_cfg_bad_structure
    try:
        env = quant_config.get_market_gate_config(up_ratio=0.3)
        ok = (isinstance(env, dict)
              and 'factor' in env and 'limit' in env
              and isinstance(env['factor'], (int, float))
              and math.isfinite(env['factor']))
        check("envs全非dict → 返回合法中性dict", ok, f"env={env}")

        env2 = MarketGate.get_environment(0.3)
        ok2 = all(k in env2 for k in ['factor', 'limit', 'label', 'desc'])
        check("MarketGate.get_environment(envs全非dict) 字段完整", ok2, f"env2={env2}")

        pos, used_env = MarketGate.apply_gate(0.5, 0.3)
        check("MarketGate.apply_gate(envs全非dict) 不崩溃",
              isinstance(pos, (int, float)) and math.isfinite(pos),
              f"pos={pos}, used_env={used_env}")
    except (KeyError, AttributeError, TypeError) as e:
        check("异常结构不抛 Key/Attr/Type Error", False, f"{type(e).__name__}: {e}")
    finally:
        quant_config._cfg = original_cfg

    # 另一种情况：cfg里没market_gate键
    quant_config._cfg = fake_cfg_no_envs_key
    try:
        env = quant_config.get_market_gate_config(0.5)
        ok = isinstance(env, dict) and 'factor' in env
        check("market_gate缺envs键 → 合法返回", ok, f"env={env}")
    except Exception as e:
        check("market_gate缺envs键不崩溃", False, f"{type(e).__name__}: {e}")
    finally:
        quant_config._cfg = original_cfg

    # futures 路径恒中性
    env_f = MarketGate.get_environment(0.0, market='futures')
    check("期货市场恒返回中性(factor=1,limit=1)",
          env_f.get('factor') == 1.0 and env_f.get('limit') == 1.0,
          f"env_f={env_f}")


# ============================================================
# BUG6: VolatilityCone log(≤0) + scoring_core 量比
# ============================================================
def test_bug6_log_nonpositive_and_zscore():
    from engine.indicators import VolatilityCone
    print("\n[BUG6] VolatilityCone log(价格≤0) 守卫")

    # 6a. 价格含 0
    closes_zero = [0.0] * 20 + [10.0 + i * 0.1 for i in range(100)]
    try:
        cone = VolatilityCone.calc(closes_zero)
        ok = True
        bad_kv = []
        for k, v in cone.items():
            for subk, subv in v.items():
                if not math.isfinite(float(subv)):
                    ok = False
                    bad_kv.append((k, subk, subv))
        check("价格含0段 → 无inf/nan", ok, f"非法={bad_kv[:3]}")
    except (ValueError, RuntimeWarning, FloatingPointError) as e:
        check("价格含0段 不抛log负/零异常", False, f"{type(e).__name__}: {e}")

    # 6b. 价格含负值（异常脏数据）
    closes_neg = list(closes_zero)
    closes_neg[5] = -2.0
    closes_neg[50] = 0.0
    try:
        cone2 = VolatilityCone.calc(closes_neg)
        ok = all(math.isfinite(float(subv))
                 for v in cone2.values() for subv in v.values())
        check("价格含负值 → 无inf/nan", ok, f"cone2 keys={list(cone2.keys())}")
    except Exception as e:
        check("价格含负值 不崩溃", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()}")

    # 6c. 正常价格仍能正常算（回归验证未破坏逻辑）
    normal = [10.0 + 0.1 * i + 0.05 * math.sin(i) for i in range(200)]
    cone3 = VolatilityCone.calc(normal)
    check("正常价格 → 生成 vol_5d/10d/20d/60d",
          all(f'vol_{p}d' in cone3 for p in (5, 10, 20, 60)))


# ============================================================
# 第二轮：RSI 递推不冻结 / ADX 平滑 / OBV off-by-one / weight None
# ============================================================
def test_rsi_series_not_frozen():
    from engine.factor_tech import _rsi_series
    print("\n[RSI] seed_loss==0 时不再整段冻结为100")

    # 构造：前 14 个 delta 全收涨（seed_loss==0），之后出现下跌 → RSI 应回落，而非恒 100
    closes = [10.0 + i for i in range(20)]  # 前20根全涨
    closes = closes + [closes[-1] - 5, closes[-1] - 5, closes[-1] - 5]  # 后3根大跌
    rsi = _rsi_series(closes, 14)
    check("整段非恒100（尾部有下跌）", float(rsi[-1]) < 100.0, f"rsi[-1]={rsi[-1]:.2f}")
    check("seed点置100", float(rsi[14]) == 100.0, f"rsi[14]={rsi[14]:.2f}")
    # 与参考版 calc_series 逐点一致
    from engine.indicators import RSICalculator
    ref = np.array(RSICalculator.calc_series(closes, 14))
    check("与 calc_series 逐点一致", np.allclose(rsi, ref, atol=1e-9),
          f"max_diff={np.max(np.abs(rsi - ref)):.1e}")


def test_adx_wilder_smoothed():
    from engine.indicators import ADXCalculator
    print("\n[ADX] Wilder 平滑")

    n = 200
    base = [10.0 + 0.05 * i for i in range(n)]
    # 强趋势：一路涨
    highs = [b + 0.3 for b in base]
    lows = [b - 0.1 for b in base]
    closes = base
    res = ADXCalculator.calc_adx(highs, lows, closes)
    check("强趋势 ADX 应偏高(>30)",
          res.get('adx', 0) > 30, f"adx={res.get('adx')}")
    # 震荡市：来回波动 → ADX 应低
    import random
    random.seed(1)
    closes2 = [10.0]
    for i in range(1, n):
        closes2.append(closes2[-1] + random.uniform(-1, 1))
    highs2 = [c + 0.5 for c in closes2]
    lows2 = [c - 0.5 for c in closes2]
    res2 = ADXCalculator.calc_adx(highs2, lows2, closes2)
    check("震荡市 ADX 应偏低(<30)",
          res2.get('adx', 99) < 30, f"adx={res2.get('adx')}")


def test_obv_offbyone():
    from engine.factor_tech import _rebuild_obv_analyze
    print("\n[OBV] off-by-one 修正")

    n = 120
    closes = [10.0 + 0.1 * i for i in range(n)]
    volumes = [1000.0 + i for i in range(n)]
    state = {'obv_ma10': np.full(n, 0.0)}
    pre = {'obv': None}
    # 用真实 pre['obv'] 构造
    from engine.factor_tech import _obv_series
    pre['obv'] = _obv_series(closes, volumes)
    state['obv_ma10'] = np.array([0.0] * n)
    res = _rebuild_obv_analyze(state, n - 1, closes, volumes, pre)
    check("obv_trend 有合法输出", res.get('obv_trend') in ('上升', '下降', '持平'),
          f"obv_trend={res.get('obv_trend')}")


def test_weight_none_no_crash():
    from engine import quant_config
    from engine.score_calculator_v2 import ScoreCalculatorV2
    print("\n[weight] None 不崩链")

    original = quant_config.get_factor_weights
    original_direction = quant_config.get_factor_direction
    original_cfg = quant_config._cfg
    try:
        # 模拟某因子 weight 键值为 None
        quant_config._cfg = lambda: {
            'factor_configs': {
                'rsi_14': {'weight': None, 'direction': 1, 'params': {'period': 14}},
                'macd_hist': {'weight': 0.5, 'direction': 1, 'params': {}},
            }
        }
        # 触发真实评分链
        tech = {'rsi_14': 70.0, 'macd_hist': 0.5}
        factors = {'rsi_14': 70.0, 'macd_hist': 0.5}
        val = ScoreCalculatorV2.calc_single(tech, factors) if hasattr(ScoreCalculatorV2, 'calc_single') else None
        # 若不存在 calc_single，直接验证权重提取不含 None
        if val is None:
            w = quant_config.get_factor_weights()
            check("get_factor_weights 过滤 None", 'rsi_14' not in w and 'macd_hist' in w,
                  f"weights={w}")
        else:
            check("评分链不因 None 崩溃", isinstance(val, (int, float)))
    finally:
        quant_config.get_factor_weights = original
        quant_config.get_factor_direction = original_direction
        quant_config._cfg = original_cfg


def test_build_tech_has_kdj_adx():
    from engine.scoring_core import build_tech
    print("\n[_build_tech] 补齐 kdj/adx 键")

    n = 200
    closes = [10.0 + 0.1 * i for i in range(n)]
    vols = [1000.0] * n
    highs = [c + 0.3 for c in closes]
    lows = [c - 0.3 for c in closes]
    opens = list(closes)
    data_list = [{'open': opens[i], 'high': highs[i], 'low': lows[i],
                  'close': closes[i], 'volume': vols[i]} for i in range(n)]
    tech = build_tech(closes, vols, highs, lows, opens, data_list, [50.0] * n, (0.0, '中性'))
    for key in ['kdj_k', 'kdj_d', 'kdj_j', 'adx', 'adx_state', 'di_plus', 'di_minus']:
        check(f"tech 含 {key}", key in tech, f"keys={list(tech.keys())}")


if __name__ == '__main__':
    print("=" * 60)
    print("stock_tool BUG 修复冒烟测试（BUG1~BUG6 边界场景）")
    print("=" * 60)

    # 让 warnings 抛错，便于抓 log 负值警告（默认 log(0) 仅 warning，不抛异常）
    import warnings
    warnings.filterwarnings('error', category=RuntimeWarning)

    test_bug1_ma_zero()
    test_bug2_factor_tech_safe()
    test_bug3_positions_defaults()
    test_bug4_bug5_env_config_robust()
    test_bug6_log_nonpositive_and_zscore()
    test_rsi_series_not_frozen()
    test_adx_wilder_smoothed()
    test_obv_offbyone()
    test_weight_none_no_crash()
    test_build_tech_has_kdj_adx()

    print("\n" + "=" * 60)
    print(f"结果: 通过 {PASS} / 失败 {FAIL}")
    print("=" * 60)
    sys.exit(0 if FAIL == 0 else 1)
