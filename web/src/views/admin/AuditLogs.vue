<!-- pwc-regime: product-ui -->
<script setup>
/**
 * 操作审计日志（C2，admin）：谁在什么时候对什么资源做了什么写操作。
 * 数据由后端中间件按写接口白名单自动记录（含登录成败）。
 */
import { onMounted, reactive, ref } from "vue";

import { auditMeta, listAuditLogs } from "@/api/audit";

const loading = ref(false);
const items = ref([]);
const total = ref(0);

const meta = ref({ users: [], actions: [], total: 0 });
const metaLoading = ref(false);

const filters = reactive({
  userId: null,
  action: null,
  start: "",
  end: "",
  page: 1,
  pageSize: 20,
});

const ACTION_LABELS = {
  user: "用户与权限",
  dataset: "数据集",
  relation: "表关系",
  metric: "指标",
  project: "项目",
  report: "报告",
  llm: "模型管理",
  insight: "AI 洞察",
};

const ACTION_TEXT = {
  user: { login: "登录", create: "新建用户", update: "更新用户", restrictions: "登记指标权限" },
  dataset: {
    upload: "上传数据集", rename: "数据集改名", annotations: "写入语义标注",
    import: "增量导入", delete: "删除数据集", batch_delete: "批量删除数据集",
    template_import: "模板导入数据集",
  },
  relation: { create: "登记表关系", delete: "删除表关系" },
  metric: {
    create: "新建指标", update: "更新指标", delete: "删除指标",
    batch_status: "批量启停指标", batch_delete: "批量删除指标", anomaly_config: "更新异动配置",
  },
  project: { create: "新建项目", update: "更新项目", delete: "删除项目" },
  report: {
    template_create: "新建报告模板", template_update: "更新报告模板",
    template_delete: "删除报告模板", generate: "生成报告", regenerate: "重新生成报告",
  },
  llm: { model_create: "登记模型", model_update: "更新模型", model_delete: "删除模型", model_activate: "切换启用模型" },
  insight: { run: "手动触发每日洞察" },
};

const actionText = (action) => {
  if (!action) return "—";
  const [group, verb] = action.split(".");
  return ACTION_TEXT[group]?.[verb] ?? action;
};
const groupText = (action) => ACTION_LABELS[action?.split(".")[0]] ?? "—";

const fmtTime = (t) => (t || "").slice(0, 19).replace("T", " ");

async function load() {
  loading.value = true;
  try {
    const data = await listAuditLogs({
      userId: filters.userId,
      action: filters.action,
      start: filters.start || undefined,
      end: filters.end || undefined,
      page: filters.page,
      pageSize: filters.pageSize,
    });
    items.value = data?.items ?? [];
    total.value = data?.total ?? 0;
  } finally {
    loading.value = false;
  }
}

async function loadMeta() {
  metaLoading.value = true;
  try {
    meta.value = (await auditMeta()) ?? { users: [], actions: [], total: 0 };
  } catch {
    /* meta 失败不阻塞列表 */
  } finally {
    metaLoading.value = false;
  }
}

function resetFilters() {
  filters.userId = null;
  filters.action = null;
  filters.start = "";
  filters.end = "";
  filters.page = 1;
  load();
}

function onSearch() {
  filters.page = 1;
  load();
}

onMounted(() => {
  load();
  loadMeta();
});
</script>

<template>
  <div class="page-container">
    <div class="page-header">
      <div>
        <h1 class="page-header__title">审计日志</h1>
        <p class="page-header__subtitle">
          操作审计（合规）：关键写操作与登录行为自动留痕，仅记录成功操作与登录成败
        </p>
      </div>
      <div class="page-header__actions">
        <el-button :loading="loading" @click="load">刷新</el-button>
      </div>
    </div>

    <section class="pwc-card">
      <div class="audit-filters">
        <el-select
          v-model="filters.userId"
          placeholder="全部用户"
          clearable filterable
          style="width: 160px"
          @change="onSearch"
        >
          <el-option
            v-for="u in meta.users"
            :key="u.id"
            :label="u.username"
            :value="u.id"
          />
        </el-select>
        <el-select
          v-model="filters.action"
          placeholder="全部动作"
          clearable filterable
          style="width: 200px"
          @change="onSearch"
        >
          <el-option v-for="a in meta.actions" :key="a" :label="actionText(a)" :value="a" />
        </el-select>
        <el-date-picker
          v-model="filters.start"
          type="date"
          placeholder="开始日期"
          value-format="YYYY-MM-DD"
          style="width: 150px"
          @change="onSearch"
        />
        <el-date-picker
          v-model="filters.end"
          type="date"
          placeholder="结束日期"
          value-format="YYYY-MM-DD"
          style="width: 150px"
          @change="onSearch"
        />
        <el-button @click="resetFilters">重置</el-button>
      </div>

      <el-table v-loading="loading" :data="items" size="small">
        <el-table-column label="时间" width="160">
          <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="用户" width="120">
          <template #default="{ row }">{{ row.username || `#${row.user_id}` }}</template>
        </el-table-column>
        <el-table-column label="动作" min-width="150">
          <template #default="{ row }">{{ actionText(row.action) }}</template>
        </el-table-column>
        <el-table-column label="分类" width="110">
          <template #default="{ row }">{{ groupText(row.action) }}</template>
        </el-table-column>
        <el-table-column prop="resource_id" label="资源 ID" width="90" />
        <el-table-column prop="method" label="方法" width="80" />
        <el-table-column label="状态" width="80">
          <template #default="{ row }">
            <el-tag size="small" :type="row.status_code < 300 ? 'success' : 'warning'" effect="light">
              {{ row.status_code }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="明细" min-width="240">
          <template #default="{ row }">
            <span class="audit-detail">{{ JSON.stringify(row.detail) }}</span>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!loading && !items.length" :image-size="60" description="暂无审计记录" />

      <div class="audit-pager">
        <el-pagination
          v-model:current-page="filters.page"
          v-model:page-size="filters.pageSize"
          :total="total"
          :page-sizes="[20, 50, 100]"
          layout="total, sizes, prev, pager, next"
          background
          @current-change="load"
          @size-change="onSearch"
        />
      </div>
    </section>
  </div>
</template>

<style scoped>
.audit-filters {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}
.audit-detail {
  font-size: 12px;
  color: var(--pwc-text-secondary, #606266);
  word-break: break-all;
}
.audit-pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}
</style>
