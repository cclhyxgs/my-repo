<script setup>
// 根组件：深色骨架 + 固定顶部导航（page-tab 固定在根层，内容区独立滚动）。
</script>

<template>
  <div class="app-shell">
    <nav class="app-nav">
      <span class="app-brand">M-Bull</span>
      <router-link to="/" class="nav-link">首页</router-link>
      <router-link to="/diagnosis" class="nav-link">批量诊断</router-link>
      <router-link to="/scan" class="nav-link">全市场扫描</router-link>
      <router-link to="/config" class="nav-link">模型配置</router-link>
      <router-link to="/account" class="nav-link">授权</router-link>
    </nav>
    <main class="app-main">
      <router-view />
    </main>
  </div>
</template>

<style scoped>
.app-shell {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}
.app-nav {
  position: sticky;
  top: 0;
  z-index: 10;
  display: flex;
  align-items: center;
  gap: 18px;
  height: 52px;
  padding: 0 20px;
  background: var(--mbull-bg-soft);
  border-bottom: 1px solid var(--mbull-border);
}
.app-brand {
  font-weight: 700;
  color: var(--mbull-accent);
  letter-spacing: 0.5px;
}
.nav-link {
  color: var(--mbull-text-dim);
  text-decoration: none;
  font-size: 14px;
  padding: 4px 2px;
  border-bottom: 2px solid transparent;
  transition: color 0.15s;
}
.nav-link:hover {
  color: var(--mbull-text);
}
.nav-link.router-link-active {
  color: var(--mbull-text);
  border-bottom-color: var(--mbull-accent);
}
.app-main {
  flex: 1;
  overflow-y: auto;
}
</style>
