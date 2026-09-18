<!-- pwc-regime: product-ui -->
<script setup>
import { onMounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import { Bell, Folder, ArrowDown, User as UserIcon, QuestionFilled } from "@element-plus/icons-vue";

import PwcLogo from "@/components/base/PwcLogo.vue";
import GlossaryDrawer from "@/components/glossary/GlossaryDrawer.vue";
import { useAuthStore } from "@/stores/auth";
import { useProjectStore } from "@/stores/project";
import {
  listNotifications as listNotificationsApi,
  markAllRead as markAllReadApi,
  markRead as markReadApi,
  unreadCount as unreadCountApi,
} from "@/api/notifications";

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();
const projectStore = useProjectStore();

const navItems = [
  { path: "/overview", label: "经营总览" },
  { path: "/reports", label: "报告中心", writeOnly: true },
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

function onUserCommand(command) {
  if (command === "manage-projects") openManage();
  else if (command === "logout") handleLogout();
}

/* ---------- 项目工作区（B9.3）：全局切换器 + 项目管理 ---------- */
onMounted(() => {
  projectStore.fetchProjects();
  refreshUnread();
});

/* ---------- 站内通知（B10-3）：铃铛 + 小红点 ---------- */
const notifications = ref([]);
const unreadCount = ref(0);
const showGlossary = ref(false); // 全局名词解释抽屉

async function refreshUnread() {
  try {
    const res = await unreadCountApi(projectStore.lockedId ?? undefined);
    unreadCount.value = res?.count ?? 0;
  } catch {
    unreadCount.value = 0;
  }
}

async function fetchNotifications() {
  try {
    notifications.value = await listNotificationsApi({
      project_id: projectStore.lockedId ?? undefined,
    });
  } catch {
    notifications.value = [];
  }
}

function shortTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  const now = new Date();
  if (d.toDateString() === now.toDateString()) return hm;
  return `${d.getMonth() + 1}-${d.getDate()} ${hm}`;
}

async function handleReadAll() {
  await markAllReadApi(projectStore.lockedId ?? undefined);
  await fetchNotifications();
  refreshUnread();
}

function openNotificationMetric(n) {
  if (!n.read) markReadApi(n.id).then(refreshUnread).catch(() => {});
  n.read = true;
  router.push(`/metrics/${n.metric_id}`);
}

// 切换项目：未读数按项目范围刷新
watch(
  () => projectStore.lockedId,
  () => {
    refreshUnread();
    fetchNotifications();
  },
);

const ALL_PROJECTS = -1; // 切换器内部值：全部项目视图（store 中存 null）

const manageVisible = ref(false);
const manageForm = reactive({ name: "", description: "" });
const managing = ref(false);

function switchProject(value) {
  projectStore.setCurrent(value === ALL_PROJECTS ? null : value);
}

async function openManage() {
  manageVisible.value = true;
  await projectStore.fetchProjects();
}

async function handleCreateProject() {
  if (!manageForm.name.trim()) {
    ElMessage.warning("请输入项目名称");
    return;
  }
  managing.value = true;
  try {
    await projectStore.create({ name: manageForm.name, description: manageForm.description });
    ElMessage.success("项目已创建");
    manageForm.name = "";
    manageForm.description = "";
  } finally {
    managing.value = false;
  }
}

async function handleRenameProject(p) {
  try {
    const { value } = await ElMessageBox.prompt("新的项目名称：", `重命名「${p.name}」`, {
      inputValue: p.name,
      inputValidator: (v) => (v && v.trim() ? true : "名称不能为空"),
    });
    await projectStore.update(p.id, { name: value.trim() });
    ElMessage.success("已重命名");
  } catch {
    /* 用户取消 */
  }
}

async function handleDeleteProject(p) {
  try {
    await ElMessageBox.confirm(
      `确定删除项目「${p.name}」？（仅允许删除空项目）`,
      "删除确认",
      { type: "warning" },
    );
  } catch {
    return;
  }
  try {
    await projectStore.remove(p.id);
    ElMessage.success("已删除");
  } catch {
    /* 拦截器已提示 */
  }
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
      <div class="layout__context">
        <!-- 全局名词解释：业务人员可随时查看平台术语定义 -->
        <el-tooltip content="名词解释" placement="bottom">
          <el-icon
            :size="20"
            class="layout__help"
            role="button"
            tabindex="0"
            @click="showGlossary = true"
          >
            <QuestionFilled />
          </el-icon>
        </el-tooltip>
        <!-- B10-3 站内通知：铃铛 + 未读小红点 + 下拉列表 -->
        <el-popover placement="bottom-end" :width="380" trigger="click" @show="fetchNotifications">
          <template #reference>
            <el-badge :value="unreadCount" :hidden="!unreadCount" :max="99" class="layout__bell">
              <el-icon :size="20"><Bell /></el-icon>
            </el-badge>
          </template>
          <div class="notif__panel">
            <div class="notif__head">
              <span>异动提醒</span>
              <el-button
                v-if="notifications.length"
                text
                type="primary"
                size="small"
                @click="handleReadAll"
              >
                全部已读
              </el-button>
            </div>
            <el-empty
              v-if="!notifications.length"
              description="暂无异动提醒"
              :image-size="60"
            />
            <div
              v-for="n in notifications"
              :key="n.id"
              class="notif__item"
              :class="{ 'is-unread': !n.read }"
              role="button"
              @click="openNotificationMetric(n)"
            >
              <p class="notif__title">{{ n.title }}</p>
              <p class="notif__body">{{ n.body }}</p>
              <p class="notif__time">{{ shortTime(n.created_at) }}</p>
            </div>
          </div>
        </el-popover>
        <!-- B9.3 项目上下文：图标 + 固定宽切换器，与导航区拉开层级 -->
        <el-select
          :model-value="projectStore.currentId ?? ALL_PROJECTS"
          class="layout__project"
          @update:model-value="switchProject"
        >
          <template #label="{ label }">
            <span class="layout__project-label">
              <el-icon class="layout__project-icon"><Folder /></el-icon>{{ label }}
            </span>
          </template>
          <el-option label="全部项目（浏览）" :value="ALL_PROJECTS" />
          <el-option v-for="p in projectStore.sortedProjects" :key="p.id" :label="p.name" :value="p.id" />
        </el-select>
      </div>
      <div class="layout__user">
        <el-dropdown trigger="click" @command="onUserCommand">
          <span class="layout__user-trigger" role="button">
            <el-icon class="layout__user-avatar"><UserIcon /></el-icon>
            <span class="layout__user-name">{{ auth.user?.username }}</span>
            <el-icon class="layout__user-caret"><ArrowDown /></el-icon>
          </span>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item disabled>
                角色：{{ auth.role }}
              </el-dropdown-item>
              <el-dropdown-item v-if="auth.isAdmin" divided command="manage-projects">
                管理项目…
              </el-dropdown-item>
              <el-dropdown-item divided command="logout">退出登录</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </header>
    <main class="layout__main">
      <router-view />
    </main>

    <!-- 项目管理（B9.3，admin） -->
    <el-dialog v-model="manageVisible" title="项目管理" width="560px">
      <div class="proj-manage__create">
        <el-input v-model="manageForm.name" placeholder="新项目名称，如：审计 / 零售" style="width: 220px" />
        <el-input v-model="manageForm.description" placeholder="描述（可选）" style="flex: 1" />
        <el-button type="primary" :loading="managing" @click="handleCreateProject">新建项目</el-button>
      </div>
      <el-table :data="projectStore.projects" size="small">
        <el-table-column prop="name" label="项目" min-width="140" />
        <el-table-column prop="description" label="描述" min-width="140" show-overflow-tooltip />
        <el-table-column label="操作" width="150">
          <template #default="{ row }">
            <el-button text type="primary" size="small" @click="handleRenameProject(row)">改名</el-button>
            <el-button
              text
              type="danger"
              size="small"
              :disabled="row.name === '默认项目'"
              @click="handleDeleteProject(row)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>
      <p class="proj-manage__hint">
        项目用于隔离不同行业/业务的数据与指标：切换项目后，数据集、指标、看板与问数仅展示当前项目内容；删除仅允许空项目。
      </p>
    </el-dialog>

    <!-- 全局名词解释（词条单一数据源：constants/glossary.js） -->
    <GlossaryDrawer v-model="showGlossary" />
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

.layout__context {
  display: flex;
  align-items: center;
  gap: var(--pwc-space-4);
  padding-left: var(--pwc-space-6);
  border-left: 1px solid var(--pwc-border-subtle);
}

.layout__bell {
  display: inline-flex;
  align-items: center;
  cursor: pointer;
  color: var(--pwc-text-primary);
}

.layout__help {
  display: inline-flex;
  align-items: center;
  cursor: pointer;
  color: var(--pwc-text-primary);
}

.layout__help:hover {
  color: var(--pwc-primary);
}

.notif__head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-weight: 700;
  margin-bottom: var(--pwc-space-2);
}

.notif__item {
  padding: var(--pwc-space-2) 0;
  border-top: 1px solid var(--pwc-border-subtle);
  cursor: pointer;
}

.notif__item.is-unread .notif__title {
  font-weight: 700;
}

.notif__item.is-unread::before {
  content: "";
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--pwc-bg-brand);
  margin-right: var(--pwc-space-2);
  vertical-align: middle;
}

.notif__title {
  font-size: var(--pwc-font-body-s);
}

.notif__body {
  font-size: var(--pwc-font-body-s);
  color: var(--pwc-text-secondary);
  margin: 2px 0;
}

.notif__time {
  font-size: var(--pwc-font-body-s);
  color: var(--pwc-text-secondary);
}

.layout__project {
  width: 200px;
}

.layout__project-label {
  display: inline-flex;
  align-items: center;
  gap: var(--pwc-space-2);
}

.layout__project-icon {
  color: var(--pwc-bg-brand);
}

.layout__user {
  display: flex;
  align-items: center;
  gap: var(--pwc-space-3);
}

.layout__user-trigger {
  display: inline-flex;
  align-items: center;
  gap: var(--pwc-space-2);
  padding: var(--pwc-space-2) var(--pwc-space-3);
  border-radius: var(--pwc-radius-sm, 4px);
  cursor: pointer;
  outline: none;
  transition: background-color 0.15s;
}

.layout__user-trigger:hover {
  background: var(--pwc-bg-layer-2, rgba(0, 0, 0, 0.04));
}

.layout__user-avatar {
  font-size: 18px;
  color: var(--pwc-bg-brand);
}

.layout__user-name {
  font-size: var(--pwc-font-body-m);
  font-weight: 500;
}

.layout__user-caret {
  font-size: 12px;
  color: var(--pwc-text-secondary);
}

.proj-manage__create {
  display: flex;
  gap: var(--pwc-space-3);
  margin-bottom: var(--pwc-space-4);
}

.proj-manage__hint {
  margin-top: var(--pwc-space-3);
  font-size: var(--pwc-font-body-s);
  color: var(--pwc-text-secondary);
}

.layout__main {
  flex: 1;
}
</style>
