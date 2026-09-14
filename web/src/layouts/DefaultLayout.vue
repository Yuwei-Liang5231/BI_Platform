<!-- pwc-regime: product-ui -->
<script setup>
import { useRoute, useRouter } from "vue-router";
import { ElMessage } from "element-plus";

import PwcLogo from "@/components/base/PwcLogo.vue";
import { useAuthStore } from "@/stores/auth";

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();

const navItems = [
  { path: "/dashboard", label: "统一看板" },
  { path: "/ask", label: "AI 问数" },
  { path: "/metrics", label: "指标目录" },
  { path: "/metrics/admin", label: "指标管理", writeOnly: true },
  { path: "/datasets", label: "数据集与表关系" },
  { path: "/llm-models", label: "模型管理", adminOnly: true },
];

const visibleNav = navItems.filter(
  (item) => (!item.writeOnly || auth.canWrite) && (!item.adminOnly || auth.isAdmin),
);

function isActive(path) {
  if (path === "/metrics") {
    return route.path === "/metrics" || (route.path.startsWith("/metrics/") && route.path !== "/metrics/admin");
  }
  return route.path.startsWith(path);
}

function handleLogout() {
  auth.logout();
  ElMessage.success("已退出登录");
  router.push({ name: "Login" });
}
</script>

<template>
  <div class="layout">
    <header class="layout__header">
      <div class="layout__brand">
        <PwcLogo :height="48" />
        <span class="layout__divider" aria-hidden="true"></span>
        <span class="layout__product">AI 原生 BI 平台</span>
      </div>
      <nav class="layout__nav" aria-label="主导航">
        <router-link
          v-for="item in visibleNav"
          :key="item.path"
          :to="item.path"
          class="layout__nav-link"
          :class="{ 'is-active': isActive(item.path) }"
        >
          {{ item.label }}
        </router-link>
      </nav>
      <div class="layout__user">
        <span class="layout__user-name">{{ auth.user?.username }}</span>
        <span class="pwc-badge pwc-badge--grey">{{ auth.role }}</span>
        <el-button text @click="handleLogout">退出</el-button>
      </div>
    </header>
    <main class="layout__main">
      <router-view />
    </main>
  </div>
</template>

<style scoped>
.layout {
  display: flex;
  flex-direction: column;
  min-height: 100vh;
  background: var(--pwc-bg-layer-1);
}

.layout__header {
  display: flex;
  align-items: center;
  gap: var(--pwc-space-8);
  height: 72px;
  padding: 0 var(--pwc-space-8);
  background: var(--pwc-bg-base);
  border-bottom: 1px solid var(--pwc-border-subtle);
  position: sticky;
  top: 0;
  z-index: 10;
}

.layout__brand {
  display: flex;
  align-items: center;
  gap: var(--pwc-space-5);
}

.layout__divider {
  width: 1px;
  height: 32px;
  background: var(--pwc-border-subtle);
}

.layout__product {
  font-family: var(--pwc-font-serif);
  font-size: var(--pwc-font-heading-m);
  white-space: nowrap;
}

.layout__nav {
  display: flex;
  gap: var(--pwc-space-2);
  flex: 1;
}

.layout__nav-link {
  display: inline-flex;
  align-items: center;
  height: 44px;
  padding: 0 var(--pwc-space-5);
  font-size: var(--pwc-font-body-m);
  color: var(--pwc-text-primary);
  border-bottom: 2px solid transparent;
  transition: color 0.15s, border-color 0.15s;
}

.layout__nav-link:hover {
  color: var(--pwc-bg-brand);
}

.layout__nav-link.is-active {
  color: var(--pwc-text-primary);
  font-weight: 700;
  border-bottom-color: var(--pwc-bg-brand);
}

.layout__user {
  display: flex;
  align-items: center;
  gap: var(--pwc-space-3);
}

.layout__user-name {
  font-size: var(--pwc-font-body-m);
  font-weight: 500;
}

.layout__main {
  flex: 1;
}
</style>
