<script setup>
import { ref } from 'vue'
import { exportDecisionCard } from './exportCard'

// F-304 决策卡组件：从 reportData 构建 540×960 竖版卡片，供 html2canvas 截图导出。
// 数据源字段（与桌面 _buildDecisionCardHTML 对齐）：
//   code/stock/factorScore/status/decisionConclusion/plus/minus/resonance/bullBearSummary/techBoard
const props = defineProps({
  reportData: { type: Object, required: true },
})

const cardRef = ref(null)
const exporting = ref(false)
const lastExport = ref('')

const scoreText = (s) => (s > 0 ? `+${s}` : `${s}`)
const signClass = (s) => (s > 0 ? 'up' : 'down')

async function handleExport() {
  if (!cardRef.value || exporting.value) return
  exporting.value = true
  try {
    const { filename, mime } = await exportDecisionCard(cardRef.value, {
      code: props.reportData.code || 'decision',
    })
    lastExport.value = `${filename}（${mime}）`
  } catch (e) {
    lastExport.value = `导出失败：${e.message}`
  } finally {
    exporting.value = false
  }
}
</script>

<template>
  <div class="dc-wrap">
    <!-- 截图目标：540×960 竖版，白底（html2canvas backgroundColor:#fff） -->
    <div ref="cardRef" class="decision-card">
      <div class="dc-head">
        <div class="dc-code">{{ reportData.code || '--' }}</div>
        <div class="dc-name">{{ reportData.stock || '--' }}</div>
      </div>

      <div class="dc-score-row">
        <span class="dc-score-label">因子分</span>
        <span class="dc-score" :class="signClass(reportData.factorScore)">
          {{ reportData.factorScore ?? '--' }}
        </span>
      </div>

      <div class="dc-conclusion">{{ reportData.decisionConclusion || '—' }}</div>

      <div class="dc-section" v-if="reportData.plus?.length || reportData.minus?.length">
        <h3>多空信号</h3>
        <div class="dc-item" v-for="(f, i) in reportData.plus || []" :key="'p' + i">
          <span class="dc-tag up">{{ scoreText(f.score) }}</span>
          <span class="dc-desc">{{ f.desc }}</span>
        </div>
        <div class="dc-item" v-for="(f, i) in reportData.minus || []" :key="'m' + i">
          <span class="dc-tag down">{{ scoreText(f.score) }}</span>
          <span class="dc-desc">{{ f.desc }}</span>
        </div>
      </div>

      <div class="dc-section" v-if="reportData.resonance">
        <h3>技术共振</h3>
        <div class="dc-reso">{{ reportData.resonance.strength || '★★★' }}</div>
        <div class="dc-reso-ratio">{{ reportData.resonance.ratio || '—' }}</div>
      </div>

      <div class="dc-section" v-if="reportData.bullBearSummary">
        <h3>多空结论</h3>
        <div class="dc-bullbear">{{ reportData.bullBearSummary }}</div>
      </div>

      <div class="dc-footer">M-Bull · 决策依据卡</div>
    </div>

    <button class="dc-export-btn" :disabled="exporting" @click="handleExport">
      {{ exporting ? '生成中…' : '导出决策卡' }}
    </button>
    <div v-if="lastExport" class="dc-export-log">{{ lastExport }}</div>
  </div>
</template>

<style scoped>
.dc-wrap {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
}
/* 540×960 竖版（F-304 规格） */
.decision-card {
  width: 540px;
  height: 960px;
  background: #fff;
  color: #1a1a1a;
  padding: 40px;
  box-sizing: border-box;
  display: flex;
  flex-direction: column;
  gap: 20px;
  border-radius: 8px;
  box-shadow: 0 4px 24px rgba(0, 0, 0, 0.35);
}
.dc-head {
  display: flex;
  align-items: baseline;
  gap: 12px;
}
.dc-code {
  font-size: 32px;
  font-weight: 700;
}
.dc-name {
  font-size: 20px;
  color: #555;
}
.dc-score-row {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 18px;
}
.dc-score {
  font-size: 48px;
  font-weight: 800;
}
.up {
  color: #ef5350;
}
.down {
  color: #26a69a;
}
.dc-conclusion {
  font-size: 18px;
  padding: 12px 16px;
  background: #f5f5f5;
  border-radius: 6px;
}
.dc-section h3 {
  font-size: 15px;
  color: #666;
  margin: 0 0 8px;
}
.dc-item {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 6px 0;
  font-size: 15px;
}
.dc-tag {
  min-width: 48px;
  font-weight: 700;
}
.dc-desc {
  color: #333;
}
.dc-reso {
  font-size: 24px;
  font-weight: 700;
}
.dc-reso-ratio,
.dc-bullbear {
  font-size: 15px;
  color: #333;
}
.dc-footer {
  margin-top: auto;
  text-align: center;
  font-size: 12px;
  color: #999;
}
.dc-export-btn {
  padding: 8px 20px;
  border-radius: 6px;
  border: 1px solid rgba(255, 255, 255, 0.15);
  background: var(--mbull-accent);
  color: #fff;
  cursor: pointer;
  font-size: 13px;
}
.dc-export-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
.dc-export-log {
  font-size: 12px;
  color: var(--mbull-text-dim);
}
</style>
