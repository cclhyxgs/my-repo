import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Vite + Vitest 合并配置：`test` 字段由 Vitest 消费，Vite 构建时忽略。
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    // 后端 FastAPI 走独立端口；开发期代理 /api 到后端（按 §3.2 契约）。
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['src/**/*.{test,spec}.{js,ts}'],
    // html2canvas 在 jsdom 下无法真实渲染，F-304 测试只测导出编排层（纯函数 + mock canvas）。
  },
})
