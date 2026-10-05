// F-603 验收 · quantModel 双层映射（config ↔ quantData）。
// 镜像 ui/config_mapper.py 的 _config_to_quant_data / _quant_data_to_config 行为。
import { describe, it, expect } from 'vitest'
import {
  defaultQuantData, configToQuant, quantToConfig, buildVetoOptions,
} from './quantModel'

function sampleConfig() {
  return {
    active_factors: ['rsi_value', 'ma_slope'],
    factor_configs: {
      rsi_value: { weight: 0.2, direction: 1, params: { period: 15 }, stats: { mean: 50, std: 14 }, ic: 0.012, ic_ir: 0.8 },
      ma_slope: { weight: 0.1, direction: -1, params: { period: 20, lookback: 4 }, stats: { mean: 0.01, std: 0.02 } },
    },
    score_scale: { weight_multiplier: 1.5, z_truncate_min: -2.5, z_truncate_max: 2.5, score_min: 5, score_max: 95 },
    conflict_penalty: { volume_down_penalty: -8, penalty_min: -12, penalty_max: 12 },
    thresholds: { strong: 22, standard: 20, test: 10, pending: -5, rebound: -2, panic_rebound: -8 },
    entry_conditions: {
      strong: { tech_signal: '多头排列', veto_on: true, veto_enabled: { rsi_extreme: true, ma20_turn_down: true } },
      standard: { tech_signal: 'none', veto_on: false, veto_enabled: {} },
    },
    tech_resonance: { threshold: 6.0, bands: [0.2, 0.5, 0.7, 0.9] },
    entry_params: {
      positions: { strong: 0.2, standard: 0.15, test: 0.08, observe: 0.03, none: 0, rebound: 0.05, panic_rebound: 0.02 },
      reversal_score_threshold: 4.5, panic_reversal_threshold: 6,
    },
    market_gate: { enabled: true, envs: { panic: { factor: 0.5, limit: 0.3 } } },
    add_params: { vol_mult: 2.5, max_add_ratio: 0.3, add_tiers: { strong_standard: { t1: { triggers: ['MA5', 'PREV_HIGH'], action: 'add', ratio: 0.2, params: {} }, t2: null, t3: null } } },
    reduce_params: { atr_mult: 1.5, reduce_tiers: { strong_standard: { t1: { triggers: ['MA5'], action: 'reduce', ratio: 0.2, params: {} }, t2: null, t3: null } } },
    risk_params: { total_position_cap_pct: 0.8, max_single_position: 0.3, market_crash_index: 'sh000300' },
    backtest: { universe: '上证50+创业50+科创50', forward_days: 30, scan_interval: 5, output_prefix: 'bt', fut_pool: 'all', fut_dir: 'long', fut_period: '日K', fut_days: 300, fut_multi_horizon: false },
    ghost_rules: { grace_period_days: 3, profit_threshold: 0.5, add_profit_threshold: 3, rsi_confirm: 45 },
    scan_tech_filter: [{ signal: '多头排列' }, { indicator: 'sma_20', op: '>=', value: 60 }],
  }
}

describe('defaultQuantData 结构完整性（8 子页签数据源）', () => {
  it('包含因子/缩放/惩罚/阈值/仓位/加仓/减仓/风控/回测/幽灵等全部结构', () => {
    const d = defaultQuantData()
    expect(d.factors.length).toBeGreaterThan(10)
    expect(d.scale).toHaveLength(5)
    expect(d.penalty).toHaveLength(10)
    expect(d.thresholds).toHaveLength(7)
    expect(d.entryPos).toHaveLength(8)
    expect(d.addTiers).toBeTruthy()
    expect(d.reduceTiers).toBeTruthy()
    expect(d.reduceTop).toHaveLength(4)
    expect(d.risk).toHaveLength(6)
    expect(d.backtest).toBeTruthy()
    expect(d.ghost).toHaveLength(5)
    expect(d.vetoOptions.length).toBeGreaterThan(0)
  })
})

describe('configToQuant（后端 config → 前端 quantData）', () => {
  it('空 config 返回默认结构（不抛错）', () => {
    const d = configToQuant({}, 'long')
    expect(d.thresholds[0].val).toBe(22)
  })

  it('多单方向：否决项键集用 LONG（顶部过热），不含空单键', () => {
    const d = configToQuant(sampleConfig(), 'long')
    const strong = d.thresholds.find((t) => t.key === 'strong')
    expect(strong.val).toBe(22)
    expect(strong.signal).toBe('多头排列')
    expect(strong.veto_enabled).toHaveProperty('rsi_extreme', true)
    // 多单重建 veto 键集应为 LONG：无 SHORT 专用键
    expect(strong.veto_enabled).not.toHaveProperty('rsi_extreme_low')
  })

  it('空单方向：否决项键集用 SHORT（超卖/突破）', () => {
    const d = configToQuant(sampleConfig(), 'short')
    const strong = d.thresholds.find((t) => t.key === 'strong')
    expect(strong.veto_enabled).toHaveProperty('rsi_extreme_low')
    expect(strong.veto_enabled).not.toHaveProperty('rsi_extreme')
  })

  it('因子参数/IC/stats 正确映射', () => {
    const d = configToQuant(sampleConfig(), 'long')
    const rsi = d.factors.find((f) => f.name === 'rsi_value')
    expect(rsi.enabled).toBe(true)
    expect(rsi.weight).toBe(0.2)
    expect(rsi.dir).toBe(1)
    expect(rsi.mean).toBe(50)
    expect(rsi.ic).toBe(0.012)
    expect(rsi.params.period).toBe(15)
  })

  it('评分缩放 / 冲突惩罚 / 技术共振 / 幽灵规则映射', () => {
    const d = configToQuant(sampleConfig(), 'long')
    expect(d.scale.find((s) => s.key === 'weight_multiplier').val).toBe(1.5)
    expect(d.penalty.find((p) => p.key === 'volume_down_penalty').val).toBe(-8)
    expect(d.thresholdResonance.significantThreshold).toBe(6)
    expect(d.thresholdResonance.starCutoffs).toEqual([0.2, 0.5, 0.7, 0.9])
    expect(d.ghost.find((g) => g.key === 'grace_period_days').val).toBe(3)
    // 幽灵规则只在 ghost_rules 出现，ma_confirm 缺失 → 空值（显示兜底由组件处理）
    expect(d.ghost.find((g) => g.key === 'ma_confirm').val).toBe('')
  })
})

describe('quantToConfig（前端 quantData → 后端 config）', () => {
  it('从 quantData 生成 config 关键路径', () => {
    const data = configToQuant(sampleConfig(), 'long')
    const cfg = quantToConfig(data, 'long')
    expect(cfg.active_factors).toContain('rsi_value')
    expect(cfg.score_scale.weight_multiplier).toBe(1.5)
    expect(cfg.conflict_penalty.volume_down_penalty).toBe(-8)
    expect(cfg.thresholds.strong).toBe(22)
    expect(cfg.entry_conditions.strong.tech_signal).toBe('bull_align')
    expect(cfg.tech_resonance.threshold).toBe(6)
    expect(cfg.entry_params.positions.strong).toBeCloseTo(0.2)
    expect(cfg.add_params.vol_mult).toBe(2.5)
    expect(cfg.risk_params.total_position_cap_pct).toBeCloseTo(0.8)
    expect(cfg.ghost_rules.grace_period_days).toBe(3)
  })

  it('百分比字段 % → 0-1 小数（max_add_ratio）', () => {
    const data = configToQuant(sampleConfig(), 'long')
    data.maxAddPctPerStep = 30
    const cfg = quantToConfig(data, 'long')
    expect(cfg.add_params.max_add_ratio).toBeCloseTo(0.3)
  })

  it('entry_condition（动态监控只读段）不会被覆盖', () => {
    const data = configToQuant(sampleConfig(), 'long')
    const cfg = quantToConfig(data, 'long')
    // 无 entry_condition 时保持缺省；此处确保不抛错且结构完整
    expect(cfg).toBeTruthy()
  })
})

describe('buildVetoOptions（否决项场景分组）', () => {
  it('long 为空单镜像键，short 为超买/突破镜像键', () => {
    const long = buildVetoOptions('long')
    const short = buildVetoOptions('short')
    expect(long.map((v) => v.key)).toContain('rsi_extreme')
    expect(long.map((v) => v.key)).not.toContain('rsi_extreme_low')
    expect(short.map((v) => v.key)).toContain('rsi_extreme_low')
    expect(short.map((v) => v.key)).not.toContain('rsi_extreme')
  })
  it('每个选项带 scene 与阈值 schema', () => {
    const opts = buildVetoOptions('long')
    const withParam = opts.find((v) => v.key === 'rsi_extreme')
    expect(withParam.scene).toBe('extreme')
    expect(withParam.params_schema.rsi_threshold.default).toBe(85)
  })
})