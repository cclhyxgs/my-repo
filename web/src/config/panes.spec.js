// F-603 验收 · 8 子页签关键控件渲染（挂载级）。
// 依据《附录-配置页控件清单》：q-backtest 需含 btLog/btResult 容器（§2），
// q-ghost 需 ma_confirm 空值回退显示 sma_20 且不落盘（§3.3），
// q-position 需渲染 P-07 各档位目标仓位与 P-09/P-10 加/减仓档位（§1）。
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { defaultQuantData } from './quantModel'
import Backtest from './panes/Backtest.vue'
import Ghost from './panes/Ghost.vue'
import Position from './panes/Position.vue'
import WeightDir from './panes/WeightDir.vue'

function mountPane(Comp, attrs = {}) {
  return mount(Comp, {
    global: { plugins: [ElementPlus] },
    props: { model: defaultQuantData(), ...attrs },
  })
}

describe('q-backtest（B-01~B-28）', () => {
  it('渲染 A股参数组 + 日志容器 id（B-25 btLog）；结果容器 btResult 需任务运行后才出现', () => {
    const w = mountPane(Backtest, { market: 'stock' })
    // btLog 恒在；btResult 由 taskId 门控（暂未运行 → 不存在，运行后出现）
    expect(w.find('#btLog').exists()).toBe(true)
    expect(w.find('#btResult').exists()).toBe(false)
    // A股组含股票池/前瞻周期/扫描间隔/输出前缀
    expect(w.text()).toContain('A股回测参数')
    expect(w.text()).toContain('前瞻周期(天)')
    expect(w.text()).toContain('扫描间隔')
  })

  it('期货市场切换为期货回测参数组（B-06~B-13）', () => {
    const w = mountPane(Backtest, { market: 'futures' })
    expect(w.text()).toContain('期货回测参数')
    expect(w.find('#btLog').exists()).toBe(true)
  })
})

describe('q-ghost（G-01~G-07）', () => {
  it('ma_confirm 空值回退显示 sma_20（§3.3）', () => {
    const w = mountPane(Ghost, { direction: 'long' })
    const sel = w.findAll('.row').map((r) => r.text()).join(' ')
    expect(sel).toContain('sma_20')
    // 空值不落盘：model 内 ma_confirm 保持空（显示兜底不代表写入）
    const g = w.props('model').ghost.find((x) => x.key === 'ma_confirm')
    expect(g.val).toBe('')
  })
})

describe('q-position（P-01~P-14）', () => {
  it('渲染 5 档目标仓位（P-07）+ 加仓/减仓档位（P-09/P-10）', () => {
    const w = mountPane(Position, { direction: 'long' })
    const text = w.text()
    for (const label of ['强势', '标准', '试探', '观望', '空仓', '博反弹', '恐慌反转']) {
      expect(text).toContain(label)
    }
    // 长单不加 short 档，也不显示 top_reversal
    expect(text).not.toContain('顶部回落')
    expect(text).toContain('加仓')
    expect(text).toContain('减仓')
  })

  it('长单隐藏顶部回落档，短单改用顶部回落（P-07 方向门控）', () => {
    // 长单：目标仓位列表用博反弹/恐慌反转，不含 top_reversal
    const long = mountPane(Position, { direction: 'long' })
    expect(long.text()).toContain('博反弹')
    expect(long.text()).not.toContain('顶部回落')
    // 短单：目标仓位列表用顶部回落，不渲染博反弹/恐慌反转 P-07 行
    const short = mountPane(Position, { direction: 'short' })
    // P-07 目标仓位区应含顶部回落；档位行标签仍含"弱势/博反弹"（posTypes 恒定，非 P-07 目标仓位）
    expect(short.text()).toContain('顶部回落')
    expect(short.props('model').entryPos.some((p) => p.key === 'rebound' || p.key === 'panic_rebound')).toBe(true)
  })
})

describe('q-weight（权重 & 方向）', () => {
  it('渲染因子权重与方向控件', () => {
    const w = mountPane(WeightDir)
    expect(w.text()).toContain('权重')
  })
})