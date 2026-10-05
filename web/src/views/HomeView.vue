<script setup>
import { ref } from 'vue'
import DecisionCard from '../decision-card/DecisionCard.vue'
import DiagnosisReport from '../diagnosis/DiagnosisReport.vue'
import AiInterpret from '../components/AiInterpret.vue'

// P0 首页：挂载 F-304 决策卡作为首个落地组件（供验证导出链路）。
// 后续 F-xxx 前端接入在此扩充。

// F-404 演示数据：真实场景由诊断页把 summary/results 传入
const sampleSummary = ref({ buy: 1, watch: 2, avoid: 0, position: 0, error: 0 })
const sampleResults = ref([
  { code: 'sh600519', name: '贵州茅台', category: 'buy' },
  { code: 'sz000001', name: '平安银行', category: 'watch' },
  { code: 'sh600023', name: '浙能电力', category: 'watch' },
])

const aiCode = ref('sh600519')

const reportData = ref({
  code: 'sh600519',
  stock: '贵州茅台',
  factorScore: -7.8,
  status: '观望',
  decisionConclusion: '⏳ 观望 — 未达观望线，等待信号',
  plus: [
    { score: 2, desc: 'RSI 超卖修复中' },
    { score: 1, desc: 'MACD 柱体收敛' },
  ],
  minus: [
    { score: -3, desc: '均线空头排列' },
    { score: -2, desc: '量能持续萎缩' },
  ],
  resonance: { strength: '★★★ 中等', ratio: '1.4:1' },
  bullBearSummary: '空方占优',
  techBoard: [
    { label: 'MA5', value: '1280.1', bias: 'down' },
    { label: 'MA20', value: '1295.4', bias: 'down' },
  ],
})
</script>

<template>
  <div class="home">
    <header class="home-header">M-Bull · H5 前端骨架（P0）</header>

    <!-- ui-design-tokens 视觉示例：近黑深蓝终端风格行情面板 -->
    <section class="ticker-panel mbull-card">
      <div class="ticker-title-row">
        <span class="ticker-title">行情终端 · 视觉示例</span>
        <span class="ticker-sub">等宽数字 + 涨跌底块（ui-design-tokens §2.2/§5.2）</span>
      </div>
      <div class="ticker-row">
        <span class="ticker-name">贵州茅台</span>
        <span class="ticker-code num">600519</span>
        <span class="ticker-price num up">1,280.10</span>
        <span class="pct-block up-bg">+2.35%</span>
      </div>
      <div class="ticker-row">
        <span class="ticker-name">平安银行</span>
        <span class="ticker-code num">000001</span>
        <span class="ticker-price num down">11.42</span>
        <span class="pct-block down-bg">-1.72%</span>
      </div>
      <div class="ticker-row">
        <span class="ticker-name">浙能电力</span>
        <span class="ticker-code num">600023</span>
        <span class="ticker-price num down">5.63</span>
        <span class="pct-block down-bg">-0.88%</span>
      </div>
    </section>

    <DecisionCard :report-data="reportData" />

    <!-- AI 解读：个股分析 + 情绪账本复盘 -->
    <section class="diag-demo">
      <h3 class="diag-demo-title">AI 解读（DeepSeek）</h3>
      <p class="diag-demo-hint">
        个股分析与情绪账本复盘；未配置 Key 请在「模型配置」页填写
      </p>
      <div class="row">
        <el-input v-model="aiCode" placeholder="输入代码，如 sh600519 / 600519" class="ai-code" clearable />
        <AiInterpret scene="analyze" :payload="{ code: aiCode, k_type: '日K' }" label="个股解析" />
      </div>
      <div class="row">
        <AiInterpret scene="ledger" label="情绪账本复盘" />
      </div>
    </section>

    <section class="diag-demo">
      <h3 class="diag-demo-title">批量诊断 · 报告导出（F-404）</h3>
      <p class="diag-demo-hint">
        导出触发浏览器下载 自选股诊断报告_{时间戳}.txt（UTF-8，含 5 组表头）
      </p>
      <DiagnosisReport :summary="sampleSummary" :results="sampleResults" />
    </section>
  </div>
</template>

<style scoped>
.home {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 16px;
  padding: 24px;
}
.home-header {
  color: var(--mbull-text-dim);
  font-size: 13px;
}
.diag-demo {
  width: 100%;
  max-width: 480px;
  margin-top: 24px;
  padding: 16px;
  border: 1px solid var(--mbull-border);
  border-radius: 8px;
  background: var(--mbull-panel);
}
.diag-demo-title {
  margin: 0 0 6px;
  font-size: 15px;
  color: var(--mbull-text);
}
.diag-demo-hint {
  margin: 0 0 12px;
  font-size: 12px;
  color: var(--mbull-text-dim);
}
.row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
  flex-wrap: wrap;
}
.ai-code {
  width: 220px;
}

/* —— 终端风格行情面板（ui-design-tokens 示例）—— */
.ticker-panel {
  width: 100%;
  max-width: 480px;
  padding: 12px 16px;
  background: var(--mbull-panel);
}
.ticker-title-row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 8px;
}
.ticker-title {
  font-size: 13px;
  font-weight: 700;
  color: var(--mbull-text);
}
.ticker-sub {
  font-size: 11px;
  color: var(--mbull-text-dim);
}
.ticker-row {
  display: grid;
  grid-template-columns: 1fr auto auto;
  align-items: center;
  gap: 12px;
  padding: 8px 0;
  border-top: 1px solid var(--mbull-border);
  font-size: 14px;
}
.ticker-name {
  color: var(--mbull-text);
  font-weight: 500;
}
.ticker-code {
  color: var(--mbull-text-3);
  font-size: 12px;
}
.ticker-price {
  font-size: 18px;
  font-weight: 700;
  text-align: right;
  min-width: 96px;
}
</style>
