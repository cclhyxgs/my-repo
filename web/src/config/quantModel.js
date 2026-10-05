// F-603 配置页 · quantData 视图模型 + config↔form 双向映射。
// 镜像桌面 ui/config_mapper.py 的双层映射与 _default_quant_data 结构，
// 作为 8 个子页签控件的单一数据源（schema 驱动渲染）。
// 存储：F-602 经 PUT /api/scheme 事务化写入完整 config（嵌套结构）。

// ---- 因子注册表（镜像 config_mapper.py factor_rows + engine/factor_registry） ----
export const FACTOR_CATEGORIES = [
  { key: 'momentum', label: '动量类' },
  { key: 'trend', label: '均线/趋势类' },
  { key: 'volume', label: '量价类' },
  { key: 'volatility', label: '波动类' },
  { key: 'pattern', label: '形态/结构类' },
]

// [name, label, cat, weight, dir, mean, std, params, desc]
const FACTOR_ROWS = [
  ['relative_strength_20d', '20日相对强度', 'momentum', 0.167, 1, 0.02, 0.15, { period: 20 }, 'N日涨幅，衡量近期动量强度'],
  ['rsi_value', 'RSI', 'momentum', 0.10, 1, 50, 15, { period: 14 }, '相对强弱指标（v2校准：高RSI预测正收益，方向+1）'],
  ['macd_hist_norm', 'MACD柱状图', 'momentum', 0.123, -1, 0.0, 0.02, { fast: 12, slow: 26, signal: 9 }, 'MACD柱状图归一化值（v2校准：IC为负，方向-1）'],
  ['kdj_signal', 'KDJ信号', 'momentum', 0.10, 1, 0.0, 10.0, { period: 9 }, 'KDJ的K-D差值，正值表示金叉区域'],

  ['ma_slope', 'MA20斜率', 'trend', 0.155, 1, 0.0, 0.03, { period: 20, lookback: 5 }, '均线斜率，衡量趋势方向'],
  ['di_spread', 'DI方向运动', 'trend', 0.146, 1, 0.0, 15.0, { period: 14 }, '+DI与-DI差值，衡量多空力量对比'],
  ['ma_arrangement', '均线排列', 'trend', 0.10, 1, 0.0, 2.0, { period: 20 }, '均线多空排列强度，+3完美多头到-3完美空头'],
  ['bias_value', '乖离率', 'trend', 0.10, -1, 0.0, 5.0, { period: 20 }, '价格偏离均线的百分比，过大有回归需求'],
  ['adx_trend_strength', 'ADX趋势强度', 'trend', 0.10, 1, 25.0, 10.0, { period: 14 }, 'ADX值衡量趋势强度，>25为趋势市'],

  ['volume_ratio', '量比', 'volume', 0.10, -1, 1.0, 0.5, { period: 5 }, '当日成交量与近期均量之比'],
  ['obv_trend', 'OBV趋势', 'volume', 0.10, 1, 0.0, 20.0, { ma_period: 10 }, 'OBV变化率，衡量资金流入流出'],
  ['volume_price_signal', '量价关系', 'volume', 0.10, 1, 0.0, 2.0, {}, '量价配合度，+2配合上涨到-2配合下跌'],

  ['bb_bandwidth', '布林带带宽', 'volatility', 0.149, 1, 15.0, 10.0, { period: 20 }, '布林带宽度，衡量波动率水平'],
  ['atr_norm', 'ATR波动率', 'volatility', 0.10, 1, 0.02, 0.01, { period: 14 }, 'ATR占价格比例，归一化波动率'],
  ['current_drawdown', '当前回撤', 'volatility', 0.119, -1, 8.0, 12.0, { lookback: 250 }, '当前价格相对高点的回撤幅度%'],
  ['volatility_cone', '波动率锥位置', 'volatility', 0.10, -1, 50.0, 25.0, { period: 20 }, '20日波动率在历史分位数，高位注意风险'],

  ['pattern_reverse', 'K线形态(反向)', 'pattern', 0.091, -1, 0.0, 1.5, {}, 'K线形态反向分，看涨形态反而易跌'],
  ['doji', '十字星(变盘)', 'pattern', 0.08, -1, 0.1, 0.3, {}, '十字星变盘信号，方向不明，压低置信度(弱浮动)'],
  ['fib_position', '斐波那契位置', 'pattern', 0.10, 1, 0.5, 0.25, { lookback: 120 }, '当前价在斐波那契回撤区的位置'],
  ['pivot_distance', '轴心点距离', 'pattern', 0.10, 1, 0.0, 5.0, {}, '当前价相对枢轴点的偏离百分比'],
  ['chip_concentration', '筹码集中度', 'pattern', 0.10, 1, 0.3, 0.2, { bins: 50 }, '筹码密集区成交量占比'],
]

// 因子参数名中文映射（镜像 index.html _paramLabelMap）
export const PARAM_LABELS = {
  period: '周期', lookback: '回看', ma_period: '均线周期', bins: '区间数',
  fast: '快线', slow: '慢线', signal: '信号线', threshold: '阈值',
  short_period: '短周期', long_period: '长周期', signal_period: '信号周期',
  deviation: '偏离度', band_width: '带宽', min_period: '最小周期', max_period: '最大周期',
  level: '级别', step: '步长', window: '窗口', rsi_period: 'RSI周期',
  macd_fast: 'MACD快线', macd_slow: 'MACD慢线', macd_signal: 'MACD信号线', kdj_period: 'KDJ周期',
  boll_period: '布林周期', boll_std: '布林倍数', atr_period: 'ATR周期', adx_period: 'ADX周期',
  volume_ratio_min: '量比下限', volume_ratio_max: '量比上限', bias_period: 'BIAS周期', bias_threshold: 'BIAS阈值',
  ma_short: '短期均线', ma_mid: '中期均线', ma_long: '长期均线',
  drawback_lookback: '回看天数', position_threshold: '位置阈值',
  score: '分数', weight: '权重', direction: '方向',
}

// 技术信号选项（镜像 _SIGNAL_BACK_TO_FRONT）
export const SIGNAL_OPTIONS = [
  '无要求', '多头排列', '空头排列', '站上MA5', '跌破MA5', '站上MA20',
  '跌破MA20', '站上MA60', '跌破MA60', 'KDJ金叉', 'KDJ死叉', 'MACD金叉',
  'MACD死叉', '均线金叉', '均线死叉', '布林带上轨突破', '布林带下轨突破',
  'ADX趋势确认', '放量上涨', '放量下跌',
]
export const SIGNAL_BACK_TO_FRONT = {
  none: '无要求', bull_align: '多头排列', bear_align: '空头排列',
  above_ma5: '站上MA5', below_ma5: '跌破MA5',
  above_ma20: '站上MA20', below_ma20: '跌破MA20',
  above_ma60: '站上MA60', below_ma60: '跌破MA60',
  kdj_gold: 'KDJ金叉', kdj_dead: 'KDJ死叉',
  macd_gold: 'MACD金叉', macd_dead: 'MACD死叉',
  ma_gold: '均线金叉', ma_dead: '均线死叉',
  bb_breakout: '布林带上轨突破', bb_breakdown: '布林带下轨突破',
  adx_trend: 'ADX趋势确认', volume_up: '放量上涨', volume_down: '放量下跌',
}
export const SIGNAL_FRONT_TO_BACK = Object.fromEntries(
  Object.entries(SIGNAL_BACK_TO_FRONT).map(([k, v]) => [v, k]),
)

// 否决项键集（镜像 quant_config VETO_KEYS / SHORT_VETO_KEYS，取自 veto_registry long/short）
const LONG_VETO_KEYS = [
  'rsi_extreme', 'kdj_extreme', 'bias_extreme',
  'ma250_break', 'macd_high_dead_cross', 'ma20_turn_down', 'new_high_20d_retreat', 'ma_dead_cross',
  'volume_stagnant', 'high_volume_top', 'volume_price_divergence',
  'atr_surge', 'gap_high_low', 'gap_down',
  'long_upper_shadow', 'bear_engulf',
  'chip_support_break',
]
const SHORT_VETO_KEYS = [
  'rsi_extreme_low', 'kdj_extreme_low', 'bias_extreme_low',
  'ma250_break_up', 'macd_low_gold_cross', 'ma20_turn_up', 'new_low_20d_retreat', 'ma_gold_cross_up',
  'volume_stagnant_down_hi', 'high_volume_low', 'volume_price_up_shrink',
  'atr_shrink', 'gap_low_high', 'gap_up',
  'long_lower_shadow', 'bull_engulf',
  'chip_resistance_break',
]
// 否决项中文标签（镜像 veto_registry VetoDef.label）
export const VETO_LABELS = {
  rsi_extreme: 'RSI极端高位', kdj_extreme: 'KDJ极端', bias_extreme: '乖离率过大',
  ma250_break: '跌破年线', macd_high_dead_cross: 'MACD高位死叉', ma20_turn_down: 'MA20拐头向下',
  new_high_20d_retreat: '20日新高回落', ma_dead_cross: '均线死叉',
  volume_stagnant: '放量滞涨', high_volume_top: '高位天量', volume_price_divergence: '量价背离',
  atr_surge: 'ATR异常放大', gap_high_low: '跳空高开低走', gap_down: '向下跳空缺口',
  long_upper_shadow: '长上影/吊颈', bear_engulf: '看跌吞没/乌云盖顶', chip_support_break: '跌破筹码密集区',
  rsi_extreme_low: 'RSI极端低位', kdj_extreme_low: 'KDJ极端低位', bias_extreme_low: '乖离率过低',
  ma250_break_up: '突破年线', macd_low_gold_cross: 'MACD低位金叉', ma20_turn_up: 'MA20拐头向上',
  new_low_20d_retreat: '20日新低回升', ma_gold_cross_up: '均线金叉',
  volume_stagnant_down_hi: '放量滞跌', high_volume_low: '低位放量', volume_price_up_shrink: '价升量缩',
  atr_shrink: 'ATR异常收敛', gap_low_high: '跳空低开高走', gap_up: '向上跳空缺口',
  long_lower_shadow: '长下影/锤子', bull_engulf: '看涨吞没', chip_resistance_break: '突破筹码密集区上沿',
}
// 6 大场景（镜像 veto_registry.SCENES）
export const VETO_SCENES = [
  ['extreme', '极值/过热'], ['trend', '趋势破位'], ['volume', '量能陷阱'],
  ['volatility', '波动异动'], ['pattern', '形态见顶'], ['chip', '筹码失守'],
]
// 否决键 → 场景归属（镜像 veto_registry VetoDef.scene，实测 L466-574）
export const VETO_KEY_SCENE = {
  rsi_extreme: 'extreme', kdj_extreme: 'extreme', bias_extreme: 'extreme',
  ma250_break: 'trend', macd_high_dead_cross: 'trend', ma20_turn_down: 'trend',
  new_high_20d_retreat: 'trend', ma_dead_cross: 'trend',
  volume_stagnant: 'volume', high_volume_top: 'volume', volume_price_divergence: 'volume',
  atr_surge: 'volatility', gap_high_low: 'volatility', gap_down: 'volatility',
  long_upper_shadow: 'pattern', bear_engulf: 'pattern',
  chip_support_break: 'chip',
  rsi_extreme_low: 'extreme', kdj_extreme_low: 'extreme', bias_extreme_low: 'extreme',
  ma250_break_up: 'trend', macd_low_gold_cross: 'trend', ma20_turn_up: 'trend',
  new_low_20d_retreat: 'trend', ma_gold_cross_up: 'trend',
  volume_stagnant_down_hi: 'volume', high_volume_low: 'volume', volume_price_up_shrink: 'volume',
  atr_shrink: 'volatility', gap_low_high: 'volatility', gap_up: 'volatility',
  long_lower_shadow: 'pattern', bull_engulf: 'pattern',
  chip_resistance_break: 'chip',
}
// 构建否决项选项集（{key,label,scene,params,params_schema}，镜像 config_mapper._default_veto_options）。
// 阈值参数面板按此渲染；未配置的参数在保存时由引擎注册表 defaults 兜底。
export function buildVetoOptions(direction = 'long') {
  const keys = direction === 'short' ? SHORT_VETO_KEYS : LONG_VETO_KEYS
  return keys.map((key) => ({
    key,
    label: VETO_LABELS[key] || key,
    scene: VETO_KEY_SCENE[key] || 'extreme',
    desc: '',
    params: Object.fromEntries(
      Object.entries(VETO_PARAMS_SCHEMA[key] || {}).map(([pk, p]) => [pk, p.default]),
    ),
    params_schema: VETO_PARAMS_SCHEMA[key] || {},
  }))
}
// 否决项阈值 schema（{key: {param: {label,default,unit}}}，取自 VetoDef.params_schema）
export const VETO_PARAMS_SCHEMA = {
  rsi_extreme: { rsi_threshold: { label: 'RSI阈值', default: 85 } },
  kdj_extreme: { j_threshold: { label: 'J阈值', default: 100 } },
  bias_extreme: { bias_pct: { label: '乖离%', default: 8.0, unit: '%' } },
  ma250_break: { break_pct: { label: '破位%', default: 3.0, unit: '%' } },
  ma20_turn_down: { slope_threshold: { label: '斜率阈值', default: 0.0 } },
  new_high_20d_retreat: { retreat_pct: { label: '回撤%', default: 3.0, unit: '%' } },
  volume_stagnant: { volume_ratio: { label: '放量倍数', default: 3 }, max_change_pct: { label: '涨幅上限%', default: 1.0, unit: '%' } },
  high_volume_top: { volume_ratio: { label: '放量倍数', default: 3 }, near_high_pct: { label: '距高点%', default: 1.0, unit: '%' } },
  atr_surge: { atr_mult: { label: '放大倍数', default: 2.0 } },
  gap_high_low: { gap_pct: { label: '跳空%', default: 1.0, unit: '%' } },
  gap_down: { gap_pct: { label: '跳空%', default: 2.0, unit: '%' } },
  long_upper_shadow: { shadow_ratio: { label: '上影占比', default: 0.6 } },
  chip_support_break: { break_pct: { label: '破位%', default: 1.0, unit: '%' } },
  rsi_extreme_low: { rsi_threshold: { label: 'RSI阈值', default: 15 } },
  kdj_extreme_low: { j_threshold: { label: 'J阈值', default: 0 } },
  bias_extreme_low: { bias_pct: { label: '乖离%', default: 8.0, unit: '%' } },
  ma250_break_up: { break_pct: { label: '突破%', default: 3.0, unit: '%' } },
  ma20_turn_up: { slope_threshold: { label: '斜率阈值', default: 0.0 } },
  new_low_20d_retreat: { retreat_pct: { label: '反弹%', default: 3.0, unit: '%' } },
  volume_stagnant_down_hi: { volume_ratio: { label: '放量倍数', default: 3 }, max_change_pct: { label: '跌幅上限%', default: 1.0, unit: '%' } },
  high_volume_low: { volume_ratio: { label: '放量倍数', default: 3 } },
  atr_shrink: { atr_div: { label: '收缩倍数', default: 2.0 } },
  gap_low_high: { gap_pct: { label: '跳空%', default: 1.0, unit: '%' } },
  gap_up: { gap_pct: { label: '跳空%', default: 2.0, unit: '%' } },
  long_lower_shadow: { shadow_ratio: { label: '下影占比', default: 0.6 } },
  chip_resistance_break: { break_pct: { label: '突破%', default: 1.0, unit: '%' } },
}

// 加仓触发选项（镜像 index.html addTriggerOptions / config_mapper）
const ADD_TRIGGER_OPTIONS = [
  { code: null, label: '无' },
  { code: 'MA5', label: '站上MA5' }, { code: 'MA10', label: '站上MA10' }, { code: 'MA20', label: '站上MA20' },
  { code: 'PREV_HIGH', label: '突破前高' }, { code: 'VOL_BREAK', label: '放量突破' },
  { code: 'BREAKOUT_PCT', label: '突破前高%', param: '%', default: 3 },
  { code: 'DEV_UP', label: '乖离率走强%', param: '%', default: 8 },
  { code: 'RSI_LOW', label: 'RSI超卖', param: 'RSI', default: 30 },
  { code: 'MACD_GOLD', label: 'MACD金叉' }, { code: 'MA_GOLD', label: '均线金叉' },
  { code: 'KDJ_GOLD', label: 'KDJ金叉' }, { code: 'ADX_TREND', label: 'ADX趋势' },
  { code: 'OBV_GOLD', label: 'OBV金叉' }, { code: 'BB_BREAKOUT', label: '布林突破' },
]
const REDUCE_TRIGGER_OPTIONS = [
  { code: null, label: '无' },
  { code: 'MA5', label: '跌破MA5' }, { code: 'MA10', label: '跌破MA10' }, { code: 'MA20', label: '跌破MA20' },
  { code: 'ATR_LOWER', label: '跌破ATR下轨' }, { code: 'PREV_LOW', label: '跌破前低' },
  { code: 'DROP_PCT', label: '跌幅%', param: '%', default: 3 },
  { code: 'PULLBACK_PCT', label: '跌破前低%', param: '破%', default: 3 },
  { code: 'DEV_OVER', label: '乖离率超买%', param: '乖%', default: 15 },
  { code: 'RSI_HIGH', label: 'RSI超买', param: 'RSI', default: 70 },
  { code: 'MACD_DEAD', label: 'MACD死叉' }, { code: 'MA_DEAD', label: '均线死叉' },
  { code: 'KDJ_DEAD', label: 'KDJ死叉' }, { code: 'BB_BREAKDOWN', label: '布林跌破' },
]

// AND 数值指标（镜像 index.html IND_LABELS）
export const AND_INDICATORS = {
  sma_5: 'MA5', sma_10: 'MA10', sma_20: 'MA20', sma_60: 'MA60', sma_250: '年线MA250',
  rsi_6: 'RSI6', rsi_14: 'RSI14', rsi_24: 'RSI24', kdj_k: 'KDJ-K', kdj_d: 'KDJ-D', kdj_j: 'KDJ-J',
  macd: 'MACD', macd_signal: 'MACD信号', macd_hist: 'MACD柱', volume_ratio: '量比', volume_z_score: '量能Z分',
  bb_pct_b: '布林%BB', atr: 'ATR', atr_ratio: 'ATR比', adx: 'ADX', di_plus: 'DI+', di_minus: 'DI-',
  bias20: '乖离率20', wr: 'WR',
}
export const AND_OPS = ['>=', '<=', '>', '<']

// 默认 quantData（镜像 config_mapper._default_quant_data）
export function defaultQuantData() {
  return {
    categories: FACTOR_CATEGORIES,
    factors: FACTOR_ROWS.map(([name, label, cat, weight, dir_, mean, std, params, desc]) => ({
      name, label, cat, enabled: false, weight, dir: dir_, ic: null, ic_ir: null,
      mean, std, params: { ...params }, desc,
    })),
    scale: [
      { key: 'weight_multiplier', label: '权重乘数', val: 1.0, desc: '放大因子预期分的灵敏度' },
      { key: 'z_truncate_min', label: 'z-score下限', val: -3.0, desc: '截断过小的 z 值' },
      { key: 'z_truncate_max', label: 'z-score上限', val: 3.0, desc: '截断过大的 z 值' },
      { key: 'score_min', label: '因子预期下限', val: 0.0, desc: '因子预期分下限' },
      { key: 'score_max', label: '因子预期上限', val: 100.0, desc: '因子预期分上限' },
    ],
    penalty: [
      { key: 'volume_down_penalty', label: '放量下跌惩罚', val: '', desc: '负值，越小扣分越多' },
      { key: 'volume_up_shrink_penalty', label: '缩量上涨惩罚', val: '', desc: '负值，越小扣分越多' },
      { key: 'deep_drawdown_div_bonus', label: '深度回撤+底背离加分', val: '', desc: '正值，越大加分越多' },
      { key: 'deep_drawdown_threshold', label: '深度回撤阈值(%)', val: '', desc: '回撤超过此值触发加分' },
      { key: 'extreme_drawdown_bonus', label: '极端回撤加分', val: '', desc: '正值，越大加分越多' },
      { key: 'extreme_drawdown_threshold', label: '极端回撤阈值(%)', val: '', desc: '回撤超过此值触发加分' },
      { key: 'cluster_proximity_penalty', label: '密集成交区接近惩罚', val: '', desc: '追高/杀跌贴近密带扣分(负值)' },
      { key: 'cluster_proximity_pct', label: '密带接近阈值(%)', val: '', desc: '距密带边界小于该百分比即视为"靠近"（如0.5）' },
      { key: 'penalty_min', label: '惩罚下限', val: '', desc: '总惩罚不低于此值' },
      { key: 'penalty_max', label: '惩罚上限', val: '', desc: '总惩罚不高于此值' },
    ],
    signalOptions: SIGNAL_OPTIONS,
    thresholds: [
      { key: 'strong', label: '强势线', dir: '≥', signal: '无要求', val: 22, veto_on: true, veto_enabled: Object.fromEntries(LONG_VETO_KEYS.map((k) => [k, true])) },
      { key: 'standard', label: '标准线', dir: '≥', signal: '多头排列', val: 20, veto_on: true, veto_enabled: Object.fromEntries(LONG_VETO_KEYS.map((k) => [k, true])) },
      { key: 'test', label: '试探线', dir: '≥', signal: '站上MA20', val: 10, veto_on: true, veto_enabled: Object.fromEntries(LONG_VETO_KEYS.map((k) => [k, true])) },
      { key: 'pending', label: '观望线', dir: '≥', signal: '无要求', val: -5, veto_on: true, veto_enabled: Object.fromEntries(LONG_VETO_KEYS.map((k) => [k, true])) },
      { key: 'rebound', label: '博反弹', dir: '<', signal: '—', val: -2, veto_on: false, veto_enabled: {} },
      { key: 'panic_rebound', label: '恐慌线', dir: '<', signal: '—', val: -8, veto_on: false, veto_enabled: {} },
      { key: 'top_reversal', label: '顶部回落', dir: '<', signal: '—', val: -22, veto_on: true, veto_enabled: Object.fromEntries(SHORT_VETO_KEYS.map((k) => [k, false])) },
    ],
    thresholdResonance: { significantThreshold: 5.0, starCutoffs: [0.2, 0.4, 0.6, 0.8] },
    scanFilter: [],
    vetoOptions: buildVetoOptions('long'),
    vetoParams: {},
    entryPos: [
      { key: 'strong', label: '强势', val: 0.20 }, { key: 'standard', label: '标准', val: 0.15 },
      { key: 'test', label: '试探', val: 0.08 }, { key: 'observe', label: '观望', val: 0.03 },
      { key: 'none', label: '空仓', val: 0.00 }, { key: 'rebound', label: '博反弹', val: 0.05 },
      { key: 'panic_rebound', label: '恐慌反转', val: 0.02 }, { key: 'top_reversal', label: '顶部回落', val: 0.05 },
    ],
    reversal_score_threshold: 4.0,
    panic_reversal_threshold: 5.0,
    marketGate: [
      { key: 'panic', label: '恐慌', factor: '', limit: '' },
      { key: 'weak', label: '弱势', factor: '', limit: '' },
      { key: 'neutral', label: '中性', factor: '', limit: '' },
      { key: 'strong', label: '强势', factor: '', limit: '' },
      { key: 'overheat', label: '过热', factor: '', limit: '' },
    ],
    marketGateEnabled: false,
    posTypes: [
      { key: 'strong_standard', label: '强势/标准股' },
      { key: 'test_pending', label: '试探/待确认股' },
      { key: 'weak_rebound', label: '弱势/博反弹' },
      { key: 'panic_rebound', label: '恐慌反转' },
    ],
    volumeBreakoutMultiplier: 2.0,
    maxAddPctPerStep: 30,
    addTriggerOptions: ADD_TRIGGER_OPTIONS,
    reduceTriggerOptions: REDUCE_TRIGGER_OPTIONS,
    reduceActions: ['减仓', '清仓'],
    addTiers: {
      strong_standard: { t1: { codes: ['MA5'], params: {}, ratio: 20 }, t2: { codes: ['PREV_HIGH'], params: {}, ratio: 20 }, t3: { codes: [], params: {}, ratio: 20 } },
      test_pending: { t1: { codes: ['MA10'], params: {}, ratio: 15 }, t2: { codes: [], params: {}, ratio: 15 }, t3: { codes: [], params: {}, ratio: 15 } },
      weak_rebound: { t1: { codes: ['MA20'], params: {}, ratio: 15 }, t2: { codes: [], params: {}, ratio: 15 }, t3: { codes: [], params: {}, ratio: 15 } },
      panic_rebound: { t1: { codes: ['VOL_BREAK'], params: {}, ratio: 20 }, t2: { codes: [], params: {}, ratio: 20 }, t3: { codes: [], params: {}, ratio: 20 } },
    },
    reduceTiers: {
      strong_standard: { t1: { codes: ['MA5'], params: {}, action: '减仓', ratio: 20 }, t2: { codes: ['MA10'], params: {}, action: '减仓', ratio: 20 }, t3: { codes: [], params: {}, action: '清仓', ratio: 100 } },
      test_pending: { t1: { codes: ['MA10'], params: {}, action: '减仓', ratio: 30 }, t2: { codes: ['MA20'], params: {}, action: '清仓', ratio: 100 }, t3: { codes: [], params: {}, action: '清仓', ratio: 100 } },
      weak_rebound: { t1: { codes: ['ATR_LOWER'], params: {}, action: '减仓', ratio: 50 }, t2: { codes: ['PREV_LOW'], params: {}, action: '清仓', ratio: 100 }, t3: { codes: [], params: {}, action: '清仓', ratio: 100 } },
      panic_rebound: { t1: { codes: ['DROP_PCT'], params: { DROP_PCT: 0.03 }, action: '减仓', ratio: 50 }, t2: { codes: ['DROP_PCT'], params: { DROP_PCT: 0.05 }, action: '清仓', ratio: 100 }, t3: { codes: [], params: {}, action: '清仓', ratio: 100 } },
    },
    reduceTop: [
      { key: 'atr_mult', label: 'ATR止损倍数', desc: '回撤止损 = 近期高点 - ATR×倍数', enabled: true, suffix: '×', min: 0, max: 2, step: 0.1, val: '' },
      { key: 'recent_low_mult', label: '近低倍数', desc: '跌破近期低点×倍数触发减仓', enabled: true, suffix: '×', min: 0.5, max: 1, step: 0.01, val: '' },
      { key: 'lookback_days', label: '回看天数', desc: '计算近期高/低点的窗口', enabled: true, suffix: '天', min: 1, max: 120, step: 1, val: '' },
      { key: 'atr_fallback', label: 'ATR回退值', desc: '无ATR时按价格×此值估算', enabled: false, suffix: '', min: 0, max: 0.2, step: 0.01, val: '' },
    ],
    risk: [
      { key: 'total_position_cap_pct', label: '总仓位上限(%)', val: '', pct: true, desc: '所有持仓合计不超过净值的此比例（留空=不启用；50=50%）' },
      { key: 'max_single_position', label: '单只最大仓位(%)', val: '', pct: true, desc: '单只股票投入不超过净值的此比例（留空=不启用；30=30%）' },
      { key: 'time_stop_days', label: '时间止损观察天数', val: '', pct: false, desc: '持仓达到此天数后，若收益未达目标则离场（留空=不启用）' },
      { key: 'time_stop_min_profit_pct', label: '时间止损目标收益(%)', val: '', pct: true, desc: '观察期内应达到的最小收益率（留空=不启用；5=5%）' },
      { key: 'single_max_loss_pct', label: '单笔最大亏损(%)', val: '', pct: true, desc: '浮亏超此值强制离场（留空=不启用；8=8%）' },
      { key: 'market_crash_pct', label: '大盘熔断暂停(%)', val: '', pct: true, desc: '宽基指数单日跌幅超此值暂停开新仓（留空=不启用）' },
    ],
    circuitBreakerIndex: '沪深300',
    backtest: { universe: '上证50+创业50+科创50', forwardDays: 30, scanInterval: 5, outputPrefix: 'bt', futPool: 'all', futDir: 'long', futPeriod: '日K', futDays: 300, futMultiHorizon: false },
    ghost: [
      { key: 'grace_period_days', label: '规则一宽限期', suffix: '天', hint: '持仓满此天数才判定清仓；期内任意波动不误杀', val: '' },
      { key: 'profit_threshold', label: '规则一证伪阈值', suffix: '%', hint: '宽限期后全程最高浮盈 < 此值 → 判清仓参考', val: '' },
      { key: 'add_profit_threshold', label: '规则二加仓浮盈阈值', suffix: '%', hint: '浮盈 > 此值 且趋势确认 → 加仓参考', val: '' },
      { key: 'rsi_confirm', label: '加仓 RSI 动量门', suffix: '', hint: '加仓前要求 RSI(14) > 此值，过滤弱势反弹（45 位于超卖线之上）', val: '' },
      { key: 'ma_confirm', label: '加仓确认均线', suffix: '', hint: '价格站上该均线才确认趋势（sma_20 默认；切 ema_20 更灵敏）', val: '', isSelect: true, options: ['sma_5', 'sma_10', 'sma_20', 'sma_60', 'sma_120', 'sma_250', 'ema_5', 'ema_10', 'ema_20', 'ema_60', 'ema_120', 'ema_250'] },
    ],
    ghostDefaults: { grace_period_days: 3, profit_threshold: 0.5, add_profit_threshold: 3, rsi_confirm: 45, ma_confirm: 'sma_20' },
  }
}

// ---- % 值互转（镜像 _to_pct_val / _from_pct_val） ----
export function toPctVal(v) {
  if (v === null || v === undefined || v === '') return 0
  const n = Number(v)
  if (Number.isNaN(n)) return 0
  return n / 100
}
export function fromPctVal(v) {
  if (v === null || v === undefined) return ''
  const n = Number(v)
  if (Number.isNaN(n)) return ''
  if (n === 0) return ''
  return Number((n * 100).toFixed(1))
}
function normNum(v) {
  if (v === null || v === undefined || v === '') return null
  const n = Number(v)
  return Number.isNaN(n) ? null : n
}

// ---- config → quantData（镜像 _config_to_quant_data） ----
export function configToQuant(config, direction = 'long') {
  const data = defaultQuantData()
  if (!config || typeof config !== 'object') return data

  const active = new Set(config.active_factors || [])
  const fconfigs = config.factor_configs || {}
  for (const f of data.factors) {
    const fc = fconfigs[f.name]
    if (fc && typeof fc === 'object') {
      f.enabled = active.has(f.name)
      f.weight = fc.weight ?? f.weight
      f.dir = fc.direction ?? f.dir
      f.ic = fc.ic ?? f.ic
      f.ic_ir = fc.ic_ir ?? f.ic_ir
      const stats = fc.stats || {}
      f.mean = stats.mean ?? f.mean
      f.std = stats.std ?? f.std
      f.params = { ...f.params, ...(fc.params || {}) }
    } else {
      f.enabled = active.has(f.name)
    }
  }

  const ss = config.score_scale || {}
  const scaleMap = {
    weight_multiplier: ss.weight_multiplier, z_truncate_min: ss.z_truncate_min,
    z_truncate_max: ss.z_truncate_max, score_min: ss.score_min, score_max: ss.score_max,
  }
  for (const s of data.scale) s.val = scaleMap[s.key] ?? s.val

  const cp = config.conflict_penalty || {}
  for (const p of data.penalty) p.val = cp[p.key] === undefined ? '' : cp[p.key]

  const thresholds = config.thresholds || {}
  const entryConditions = config.entry_conditions || {}
  const isShort = direction === 'short'
  const vetoKeys = isShort ? SHORT_VETO_KEYS : LONG_VETO_KEYS
  for (const t of data.thresholds) {
    const ec = entryConditions[t.key] || {}
    t.val = thresholds[t.key] ?? t.val
    const sig = ec.tech_signal
    if (Array.isArray(sig)) t.signal = sig.length ? sig.map(signalCondToFront) : '无要求'
    else t.signal = sig ? SIGNAL_BACK_TO_FRONT[sig] || sig : '无要求'
    t.veto_on = !!ec.veto_on
    const ve = ec.veto_enabled || {}
    t.veto_enabled = Object.fromEntries(vetoKeys.map((k) => [k, !!ve[k]]))
  }

  data.vetoOptions = buildVetoOptions(direction)
  data.vetoParams = config.veto_params || {}
  const tr = config.tech_resonance || {}
  data.thresholdResonance = {
    significantThreshold: tr.threshold ?? 5.0,
    starCutoffs: Array.isArray(tr.bands) ? tr.bands : [0.2, 0.4, 0.6, 0.8],
  }

  const ep = config.entry_params || {}
  const positions = ep.positions || {}
  for (const p of data.entryPos) p.val = positions[p.key] ?? p.val ?? 0
  data.reversal_score_threshold = ep.reversal_score_threshold ?? 4.0
  data.panic_reversal_threshold = ep.panic_reversal_threshold ?? 5.0

  const mg = config.market_gate || {}
  data.marketGateEnabled = !!mg.enabled
  const envs = mg.envs || {}
  for (const g of data.marketGate) {
    const e = envs[g.key]
    g.factor = e && e.factor !== null && e.factor !== undefined ? e.factor : ''
    g.limit = e && e.limit !== null && e.limit !== undefined ? e.limit : ''
  }

  const addp = config.add_params || {}
  data.volumeBreakoutMultiplier = addp.vol_mult ?? 2.0
  data.maxAddPctPerStep = fromPctVal(addp.max_add_ratio ?? 0.3)
  const addTiers = addp.add_tiers || {}
  for (const [rowKey, tiers] of Object.entries(data.addTiers)) {
    const src = addTiers[rowKey] || {}
    for (const tk of ['t1', 't2', 't3']) {
      const st = src[tk]
      if (st && typeof st === 'object') {
        const trig = st.triggers ?? st.trigger
        tiers[tk] = { codes: Array.isArray(trig) ? trig : trig ? [trig] : [], params: { ...(st.params || {}) }, ratio: fromPctVal(st.ratio ?? 0) }
      } else {
        tiers[tk] = { codes: [], params: {}, ratio: 0 }
      }
    }
  }

  const redp = config.reduce_params || {}
  for (const rt of data.reduceTop) {
    rt.val = redp[rt.key] ?? ''
    if (rt.key in redp) rt.enabled = true
  }
  const reduceTiers = redp.reduce_tiers || {}
  for (const [rowKey, tiers] of Object.entries(data.reduceTiers)) {
    const src = reduceTiers[rowKey] || {}
    for (const tk of ['t1', 't2', 't3']) {
      const st = src[tk]
      if (st && typeof st === 'object') {
        const trig = st.triggers ?? st.trigger
        tiers[tk] = {
          codes: Array.isArray(trig) ? trig : trig ? [trig] : [],
          params: { ...(st.params || {}) },
          action: st.action === 'reduce' ? '减仓' : '清仓',
          ratio: fromPctVal(st.ratio ?? 0),
        }
      } else {
        tiers[tk] = { codes: [], params: {}, action: '清仓', ratio: 100 }
      }
    }
  }

  const rp = config.risk_params || {}
  for (const r of data.risk) {
    const raw = rp[r.key]
    if (raw === null || raw === undefined) r.val = ''
    else if (r.pct) r.val = fromPctVal(raw)
    else r.val = Number(raw) > 0 ? raw : ''
  }
  const idx = rp.market_crash_index ?? 'sh000300'
  data.circuitBreakerIndex = CIRCUIT_NAME[idx] || idx

  const bt = config.backtest
  if (bt) {
    data.backtest = {
      universe: bt.universe ?? data.backtest.universe,
      forwardDays: bt.forward_days ?? data.backtest.forwardDays,
      scanInterval: bt.scan_interval ?? data.backtest.scanInterval,
      outputPrefix: bt.output_prefix ?? data.backtest.outputPrefix,
      futPool: bt.fut_pool ?? data.backtest.futPool,
      futDir: bt.fut_dir ?? data.backtest.futDir,
      futPeriod: bt.fut_period ?? data.backtest.futPeriod,
      futDays: bt.fut_days ?? data.backtest.futDays,
      futMultiHorizon: !!bt.fut_multi_horizon,
    }
  }

  const gr = config.ghost_rules || {}
  for (const g of data.ghost) {
    g.val = gr[g.key] ?? ''
    if (g.key === 'rsi_confirm') {
      g.hint = direction === 'short' ? '加仓前要求 RSI(14) < 此值，过滤弱势反抽（空头加仓在超卖后动量衰竭时更优）' : '加仓前要求 RSI(14) > 此值，过滤弱势反弹（45 位于超卖线之上）'
    } else if (g.key === 'ma_confirm') {
      g.hint = direction === 'short' ? '价格跌破该均线才确认下跌趋势（sma_20 默认；空头加仓要求趋势向下）' : '价格站上该均线才确认趋势（sma_20 默认；切 ema_20 更灵敏）'
    }
  }

  data.scanFilter = config.scan_tech_filter || []
  return data
}

const CIRCUIT_NAME = {
  sh000300: '沪深300', sh000001: '上证指数', sz399001: '深证成指',
  sh000985: '中证全指', sz399006: '创业板指', sh000016: '上证50',
}
export const CIRCUIT_NAME_OPTIONS = Object.values(CIRCUIT_NAME)
const CIRCUIT_CODE = Object.fromEntries(Object.entries(CIRCUIT_NAME).map(([k, v]) => [v, k]))

// AND 条件单元素 config↔front（镜像 _signal_cond_to_config / _signal_cond_to_front）
export function signalCondToConfig(cond) {
  if (!cond || typeof cond !== 'object') return cond
  const out = { ...cond }
  if ('signal' in cond) {
    const s = String(cond.signal)
    out.signal = SIGNAL_FRONT_TO_BACK[s] || s || 'none'
  }
  return out
}
export function signalCondToFront(cond) {
  if (!cond || typeof cond !== 'object') return cond
  const out = { ...cond }
  if ('signal' in cond) {
    const s = String(cond.signal)
    out.signal = SIGNAL_BACK_TO_FRONT[s] || s
  }
  return out
}

// 根据方案方向确定否决键集与标签
export function vetoKeysFor(direction = 'long') {
  return (direction === 'short' ? SHORT_VETO_KEYS : LONG_VETO_KEYS).slice()
}

// ---- quantData → config（镜像 _quant_data_to_config） ----
export function quantToConfig(data, direction = 'long') {
  const config = {}

  const active = (data.factors || []).filter((f) => f.enabled)
  if (active.length) config.active_factors = active.map((f) => f.name)
  const fconfigs = {}
  for (const f of data.factors || []) {
    const old = {}
    const fc = { name: f.name, weight: f.weight, direction: f.dir, params: { ...(f.params || {}) }, ic: old.ic, ic_ir: old.ic_ir, ic_weighted_value: old.ic_weighted_value, stats: { mean: f.mean, std: f.std } }
    fconfigs[f.name] = fc
  }
  config.factor_configs = fconfigs

  const scale = {}
  for (const s of data.scale || []) scale[s.key] = s.val
  config.score_scale = {
    weight_multiplier: normNum(scale.weight_multiplier) || 1.0,
    z_truncate_min: normNum(scale.z_truncate_min) ?? -3.0,
    z_truncate_max: normNum(scale.z_truncate_max) ?? 3.0,
    score_min: normNum(scale.score_min) || 0.0,
    score_max: normNum(scale.score_max) || 100.0,
  }

  config.conflict_penalty = {}
  for (const p of data.penalty || []) {
    if (p.val !== '' && p.val !== null && p.val !== undefined) {
      const nv = normNum(p.val)
      if (nv !== null) config.conflict_penalty[p.key] = nv
    }
  }

  config.thresholds = {}
  for (const t of data.thresholds || []) config.thresholds[t.key] = Math.round(Number(t.val ?? 0))

  const isShort = direction === 'short'
  const entryConditions = {}
  for (const t of data.thresholds || []) {
    const raw = t.signal
    let sig
    if (Array.isArray(raw)) sig = raw.length ? raw.map(signalCondToConfig) : 'none'
    else sig = SIGNAL_FRONT_TO_BACK[raw] || 'none'
    const vkeys = isShort ? SHORT_VETO_KEYS : LONG_VETO_KEYS
    entryConditions[t.key] = {
      tech_signal: sig,
      veto_on: !!t.veto_on,
      veto_enabled: Object.fromEntries(vkeys.map((k) => [k, !!((t.veto_enabled || {})[k])])),
    }
  }
  config.entry_conditions = entryConditions
  config.scan_tech_filter = data.scanFilter || []

  const vp = data.vetoParams
  if (vp && typeof vp === 'object' && Object.keys(vp).length) {
    config.veto_params = Object.fromEntries(Object.entries(vp).filter(([, v]) => v && typeof v === 'object').map(([k, v]) => [k, { ...v }]))
  }

  const tr = data.thresholdResonance || {}
  config.tech_resonance = {
    threshold: normNum(tr.significantThreshold) ?? 5.0,
    bands: Array.isArray(tr.starCutoffs) ? tr.starCutoffs.map(Number) : [0.2, 0.4, 0.6, 0.8],
  }

  const positions = {}
  for (const p of data.entryPos || []) positions[p.key] = normNum(p.val) ?? 0
  config.entry_params = {
    positions,
    reversal_score_threshold: normNum(data.reversal_score_threshold) ?? 4.0,
    panic_reversal_threshold: normNum(data.panic_reversal_threshold) ?? 5.0,
  }

  const marketGate = { enabled: !!data.marketGateEnabled }
  const envs = {}
  for (const g of data.marketGate || []) {
    const f = g.factor
    const l = g.limit
    envs[g.key] = {
      factor: f !== '' && f !== null && f !== undefined ? normNum(f) : null,
      limit: l !== '' && l !== null && l !== undefined ? normNum(l) : null,
    }
  }
  marketGate.envs = envs
  config.market_gate = marketGate

  const addTiers = {}
  for (const [rowKey, tiers] of Object.entries(data.addTiers || {})) {
    const row = {}
    for (const tk of ['t1', 't2', 't3']) {
      const t = tiers[tk]
      const codes = (t.codes || []).filter(Boolean)
      if (codes.length) {
        const normP = {}
        for (const [pk, pv] of Object.entries(t.params || {})) {
          const nv = normNum(pv)
          if (nv !== null) normP[pk] = nv
        }
        row[tk] = { triggers: codes, action: 'add', ratio: toPctVal(t.ratio ?? 0), params: normP }
      } else {
        row[tk] = null
      }
    }
    addTiers[rowKey] = row
  }
  config.add_params = {
    vol_mult: normNum(data.volumeBreakoutMultiplier) ?? 2.0,
    max_add_ratio: toPctVal(data.maxAddPctPerStep ?? 30),
    add_tiers: addTiers,
  }

  const reduceTiers = {}
  for (const [rowKey, tiers] of Object.entries(data.reduceTiers || {})) {
    const row = {}
    for (const tk of ['t1', 't2', 't3']) {
      const t = tiers[tk]
      const codes = (t.codes || []).filter(Boolean)
      if (codes.length) {
        const normP = {}
        for (const [pk, pv] of Object.entries(t.params || {})) {
          const nv = normNum(pv)
          if (nv !== null) normP[pk] = nv
        }
        row[tk] = { triggers: codes, action: t.action === '清仓' ? 'clear' : 'reduce', ratio: toPctVal(t.ratio ?? 0), params: normP }
      } else {
        row[tk] = null
      }
    }
    reduceTiers[rowKey] = row
  }
  config.reduce_params = { reduce_tiers: reduceTiers }
  for (const rt of data.reduceTop || []) {
    const v = normNum(rt.val)
    if (v !== null && rt.enabled) config.reduce_params[rt.key] = v
  }

  const rp = data.risk || []
  if (rp.some((r) => normNum(r.val) !== null)) {
    const riskParams = {}
    for (const r of rp) {
      const nv = normNum(r.val)
      if (nv !== null) riskParams[r.key] = r.pct ? toPctVal(nv) : nv
    }
    riskParams.market_crash_index = CIRCUIT_CODE[data.circuitBreakerIndex] || 'sh000300'
    config.risk_params = riskParams
  }

  const bt = data.backtest || {}
  config.backtest = {
    universe: bt.universe,
    forward_days: bt.forwardDays,
    scan_interval: bt.scanInterval,
    output_prefix: bt.outputPrefix,
    fut_pool: bt.futPool,
    fut_dir: bt.futDir,
    fut_period: bt.futPeriod,
    fut_days: bt.futDays,
    fut_multi_horizon: !!bt.futMultiHorizon,
  }

  const ghostRules = {}
  for (const g of data.ghost || []) {
    const v = g.val
    if (v !== '' && v !== null && v !== undefined) ghostRules[g.key] = g.key === 'grace_period_days' ? Math.trunc(Number(v)) : String(v).trim() === '' ? v : Number.isNaN(Number(v)) ? v : Number(v)
  }
  config.ghost_rules = ghostRules
  // 5 项全空则 enabled.ghost_rules = false
  config.enabled = { market_gate: !!data.marketGateEnabled, add_engine: true, ghost_rules: Object.keys(ghostRules).length > 0 }

  return config
}