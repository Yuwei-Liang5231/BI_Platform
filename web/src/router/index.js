import { createRouter, createWebHashHistory } from "vue-router";

import { useAuthStore } from "@/stores/auth";
import BlankLayout from "@/layouts/BlankLayout.vue";
import DefaultLayout from "@/layouts/DefaultLayout.vue";

const routes = [
  {
    path: "/login",
    name: "Login",
    component: () => import("@/views/auth/Login.vue"),
    meta: { layout: BlankLayout, public: true, title: "登录" },
  },
  {
    path: "/",
    component: DefaultLayout,
    redirect: "/dashboard",
    children: [
      {
        path: "dashboard",
        name: "Dashboard",
        component: () => import("@/views/dashboard/Dashboard.vue"),
        meta: { title: "统一看板" },
      },
      {
        path: "overview",
        name: "Overview",
        component: () => import("@/views/overview/Overview.vue"),
        meta: { title: "经营总览" },
      },
      {
        path: "reports",
        name: "Reports",
        component: () => import("@/views/report/Reports.vue"),
        meta: { title: "报告中心" },
      },
      {
        path: "ask",
        name: "Ask",
        component: () => import("@/views/ask/Ask.vue"),
        meta: { title: "AI 问数" },
      },
      {
        path: "metrics",
        name: "MetricCatalog",
        component: () => import("@/views/metric/MetricCatalog.vue"),
        meta: { title: "指标目录" },
      },
      {
        path: "metrics/admin",
        name: "MetricAdmin",
        component: () => import("@/views/metric/MetricAdmin.vue"),
        meta: { title: "指标管理" },
      },
      {
        path: "metrics/:id",
        name: "MetricDetail",
        component: () => import("@/views/metric/MetricDetail.vue"),
        meta: { title: "指标详情" },
      },
      {
        path: "datasets",
        name: "DatasetManage",
        component: () => import("@/views/dataset/DatasetManage.vue"),
        meta: { title: "数据集与表关系" },
      },
      {
        path: "llm-models",
        name: "LlmModels",
        component: () => import("@/views/llm/LlmModels.vue"),
        meta: { title: "模型管理", adminOnly: true },
      },
    ],
  },
  { path: "/:pathMatch(.*)*", redirect: "/dashboard" },
];

const router = createRouter({
  history: createWebHashHistory(),
  routes,
});

router.beforeEach(async (to) => {
  const auth = useAuthStore();
  // 有 token 但 user 丢失（重开浏览器）：先经 /auth/me 恢复角色，再判定权限
  await auth.ensureUser();
  if (!to.meta.public && !auth.isLoggedIn) {
    return { name: "Login", query: { redirect: to.fullPath } };
  }
  if (to.meta.adminOnly && !auth.isAdmin) {
    return { path: "/dashboard" };
  }
  if (to.name === "Login" && auth.isLoggedIn) {
    return { path: "/" };
  }
  return true;
});

router.afterEach((to) => {
  document.title = to.meta.title ? `${to.meta.title} · AI 原生 BI 平台` : "AI 原生 BI 平台";
});

export default router;
