<!-- pwc-regime: product-ui -->
<script setup>
import { onMounted, reactive, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";

import PwcLogo from "@/components/base/PwcLogo.vue";
import { useAuthStore } from "@/stores/auth";
import { useProjectStore } from "@/stores/project";

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();
const projectStore = useProjectStore();

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

/* ---------- 项目工作区（B9.3）：全局切换器 + 项目管理 ---------- */
onMounted(() => {
  projectStore.fetchProjects();
});

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
      <div class="layout__user">
        <el-select
          :model-value="projectStore.currentId ?? ALL_PROJECTS"
          class="layout__project"
          @update:model-value="switchProject"
        >
          <el-option label="全部项目（浏览）" :value="ALL_PROJECTS" />
          <el-option v-for="p in projectStore.projects" :key="p.id" :label="p.name" :value="p.id" />
        </el-select>
        <el-button v-if="auth.isAdmin" text @click="openManage">管理项目</el-button>
        <span class="layout__user-name">{{ auth.user?.username }}</span>
        <span class="pwc-badge pwc-badge--grey">{{ auth.role }}</span>
        <el-button text @click="handleLogout">退出</el-button>
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

.layout__project {
  width: 170px;
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

.layout__user-name {
  font-size: var(--pwc-font-body-m);
  font-weight: 500;
}

.layout__main {
  flex: 1;
}
</style>
