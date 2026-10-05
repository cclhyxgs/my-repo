import { createRouter, createWebHistory } from 'vue-router'

// 路由骨架：P0 挂决策卡（F-304）+ 批量诊断页（F-401~404）；
// 其余页面（分析/回测等）随后续 F-xxx 前端接入逐个补充。
const routes = [
  {
    path: '/',
    name: 'home',
    component: () => import('../views/HomeView.vue'),
  },
  {
    path: '/diagnosis',
    name: 'diagnosis',
    component: () => import('../views/DiagnosisView.vue'),
  },
  {
    path: '/scan',
    name: 'scan',
    component: () => import('../views/ScanView.vue'),
  },
  {
    path: '/config',
    name: 'config',
    component: () => import('../views/ConfigView.vue'),
  },
  {
    path: '/backtest',
    name: 'backtest',
    component: () => import('../views/BacktestView.vue'),
  },
  {
    path: '/account',
    name: 'account',
    component: () => import('../views/AccountView.vue'),
  },
]

export default createRouter({
  history: createWebHistory(),
  routes,
})
