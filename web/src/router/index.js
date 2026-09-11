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
    ],
  },
  { path: "/:pathMatch(.*)*", redirect: "/dashboard" },
];

const router = createRouter({
  history: createWebHashHistory(),
  routes,
});

router.beforeEach((to) => {
  const auth = useAuthStore();
  if (!to.meta.public && !auth.isLoggedIn) {
    return { name: "Login", query: { redirect: to.fullPath } };
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
