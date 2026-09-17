<template>
  <div class="page-container">
    <div class="page-header">
      <div>
        <h1 class="page-header__title">报告中心</h1>
        <p class="page-header__subtitle">算写分离：数字由平台计算，叙述可读可对账</p>
      </div>
      <div class="page-header__actions">
        <el-button @click="openTemplateDialog()">新建报告模板</el-button>
        <el-button type="primary" :disabled="!selectedTemplateId" :loading="previewing" @click="doPreview">
          生成报告
        </el-button>
        <el-button
          type="success"
          :disabled="!selectedTemplateId"
          :loading="archiving"
          @click="doGenerate"
        >
          生成并存档
        </el-button>
      </div>
    </div>

    <!-- 模板区 -->
    <div class="reports__templates">
      <div
        v-for="t in templates"
        :key="t.id"
        class="reports__tpl"
        :class="{ 'is-active': t.id === selectedTemplateId }"
        role="button"
        @click="selectedTemplateId = t.id"
      >
        <div class="reports__tpl-name">{{ t.name }}</div>
        <div class="reports__tpl-meta">
          {{ periodLabel(t.period_type) }} · {{ t.metric_ids.length }} 个指标
        </div>
        <div class="reports__tpl-actions">
          <el-button text size="small" @click.stop="openTemplateDialog(t)">编辑</el-button>
          <el-button v-if="auth.isAdmin" text size="small" type="danger" @click.stop="doDelete(t)">
            删除
          </el-button>
        </div>
      </div>
      <el-empty v-if="!templates.length" description="暂无报告模板——先新建一个（选择周期类型与指标集）" />
    </div>

    <!-- 报告预览 -->
    <div v-if="report" ref="reportRef" class="reports__doc">
      <header class="reports__doc-head">
        <h2 class="reports__doc-title">
          {{ report.title }}
          <el-tag v-if="viewingInstanceId" size="small" type="warning">历史快照</el-tag>
          <el-tag
            :type="report.narrative_source === 'llm' ? 'success' : 'info'"
            size="small"
            class="reports__src-tag"
          >
            {{ report.narrative_source === "llm" ? "AI 叙述（数字平台回填）" : "规则叙述" }}
          </el-tag>
          <el-tooltip
            v-if="report.llm_degraded?.length"
            :content="`以下章节 LLM 输出未通过数字审计，已降级为规则句：${report.llm_degraded.join('、')}`"
          >
            <el-tag type="warning" size="small">部分降级</el-tag>
          </el-tooltip>
        </h2>
        <p class="reports__doc-sub">
          生成时间 {{ generatedAt }} · 所有数字均由指标计算出口产生，LLM 只组织文字、引用占位符回填，可逐项对账
        </p>
      </header>

      <section class="reports__section">
        <h3 class="reports__sec-title">指标结论</h3>
        <table class="reports__table">
          <thead>
            <tr>
              <th>指标</th>
              <th class="num">本期值</th>
              <th v-if="report.sections.mom" class="num">环比</th>
              <th v-if="report.sections.yoy" class="num">同比</th>
              <th>口径备注</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="c in report.conclusions" :key="c.ref">
              <td>{{ c.name }}（{{ c.code }}）</td>
              <td class="num">{{ formatMetricValue(c.value) }}</td>
              <td v-if="report.sections.mom" class="num" :class="trendClass(c.mom_pct)">
                {{ formatPercent(c.mom_pct) }}
              </td>
              <td v-if="report.sections.yoy" class="num" :class="trendClass(c.yoy_pct)">
                {{ formatPercent(c.yoy_pct) }}
              </td>
              <td class="reports__note">
                <template v-if="c.constant">全期常数指标</template>
                <template v-else-if="c.period_complete === false && c.data_through">
                  数据截至 {{ c.data_through }}
                </template>
              </td>
            </tr>
          </tbody>
        </table>
      </section>

      <section
        v-for="sec in report.narrative"
        :key="sec.section"
        class="reports__section"
      >
        <h3 class="reports__sec-title">{{ sectionTitle(sec.section) }}</h3>
        <p v-for="(s, i) in sec.sentences" :key="i" class="reports__sentence">{{ s }}</p>
      </section>

      <footer class="reports__doc-foot">
        <el-button @click="copyReport">复制文本</el-button>
        <el-button type="primary" @click="printReport">打印 / 导出 PDF</el-button>
      </footer>
    </div>

    <!-- 历史存档（B12-3） -->
    <section v-if="selectedTemplateId" class="reports__history">
      <h3 class="reports__sec-title">历史存档（最近 100 份，快照不受口径变更影响）</h3>
      <el-table :data="instances" size="small" max-height="280" @row-click="(row) => viewInstance(row)">
        <el-table-column label="周期" min-width="180">
          <template #default="{ row }">{{ row.period_start }} ~ {{ row.period_end }}</template>
        </el-table-column>
        <el-table-column label="版本" width="72">
          <template #default="{ row }">
            <el-tag size="small" type="info">v{{ row.version }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="叙述" width="110">
          <template #default="{ row }">
            {{ row.narrative_source === "llm" ? "AI 叙述" : "规则叙述" }}
          </template>
        </el-table-column>
        <el-table-column prop="created_by" label="生成人" width="100" />
        <el-table-column label="生成时间" min-width="160">
          <template #default="{ row }">{{ new Date(row.created_at).toLocaleString() }}</template>
        </el-table-column>
        <el-table-column label="操作" width="150">
          <template #default="{ row }">
            <el-button text size="small" @click.stop="viewInstance(row)">查看</el-button>
            <el-button text size="small" @click.stop="doRegenerate(row)">重新生成</el-button>
          </template>
        </el-table-column>
      </el-table>
    </section>

    <el-empty
      v-else
      description="选择左侧模板并点击「生成报告」；每个数字与指标计算接口对账一致"
    />

    <!-- 新建/编辑模板 -->
    <el-dialog v-model="dialogVisible" :title="editingId ? '编辑报告模板' : '新建报告模板'" width="560">
      <el-form label-width="88px">
        <el-form-item label="模板名称">
          <el-input v-model="form.name" maxlength="100" placeholder="如：每周经营速览" />
        </el-form-item>
        <el-form-item label="周期类型">
          <el-select v-model="form.period_type" style="width: 100%">
            <el-option label="日报（当天）" value="daily" />
            <el-option label="周报（上一个完整自然周）" value="weekly" />
            <el-option label="月报（上一个完整自然月）" value="monthly" />
          </el-select>
        </el-form-item>
        <el-form-item label="指标集">
          <el-select v-model="form.metric_ids" multiple filterable style="width: 100%" placeholder="选择要纳入报告的指标">
            <el-option v-for="m in metricStore.list" :key="m.id" :label="`${m.name}（${m.code}）`" :value="m.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="章节">
          <el-checkbox v-model="form.sections.overview">总览</el-checkbox>
          <el-checkbox v-model="form.sections.mom">环比</el-checkbox>
          <el-checkbox v-model="form.sections.yoy">同比</el-checkbox>
          <el-checkbox v-model="form.sections.anomaly">异动检测</el-checkbox>
          <el-checkbox v-model="form.sections.attribution">归因来源</el-checkbox>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveTemplate">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<!-- pwc-regime: product-ui -->
<script setup>
import { onMounted, reactive, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";

import {
  createReportTemplate,
  deleteReportTemplate,
  generateReport,
  getReportInstance,
  listReportInstances,
  listReportTemplates,
  previewReport,
  regenerateReport,
  updateReportTemplate,
} from "@/api/reports";
import { useAuthStore } from "@/stores/auth";
import { useProjectStore } from "@/stores/project";
import { useMetricStore } from "@/stores/metric";
import { formatMetricValue, formatPercent } from "@/utils/format";

const auth = useAuthStore();
const projectStore = useProjectStore();
const metricStore = useMetricStore();

const templates = ref([]);
const selectedTemplateId = ref(null);
const report = ref(null);
const previewing = ref(false);
const generatedAt = ref("");

const dialogVisible = ref(false);
const saving = ref(false);
const editingId = ref(null);
const form = reactive({
  name: "",
  period_type: "weekly",
  metric_ids: [],
  sections: { overview: true, mom: true, yoy: true, anomaly: true, attribution: true },
});

const PERIOD_LABELS = { daily: "日报", weekly: "周报", monthly: "月报" };
const SECTION_TITLES = {
  overview: "总览",
  metrics: "指标结论叙述",
  anomaly: "异动检测",
  attribution: "变化来源归因",
};
const periodLabel = (t) => PERIOD_LABELS[t] ?? t;
const sectionTitle = (k) => SECTION_TITLES[k] ?? k;
// 红涨绿跌（2026-09-15 契约）：涨红跌绿，与 good/bad 业务语义解耦
const trendClass = (pct) =>
  pct > 0 ? "is-up" : pct < 0 ? "is-down" : "";

async function fetchTemplates() {
  templates.value = await listReportTemplates(
    projectStore.lockedId ? { project_id: projectStore.lockedId } : undefined,
  );
}

async function doPreview() {
  if (!selectedTemplateId.value) return;
  previewing.value = true;
  try {
    report.value = await previewReport({ template_id: selectedTemplateId.value });
    generatedAt.value = new Date().toLocaleString();
    viewingInstanceId.value = null;
  } finally {
    previewing.value = false;
  }
}

/* ---------- 存档与历史（B12-3） ---------- */
const archiving = ref(false);
const instances = ref([]);
const viewingInstanceId = ref(null);

async function fetchInstances() {
  if (!selectedTemplateId.value) {
    instances.value = [];
    return;
  }
  instances.value = await listReportInstances({ template_id: selectedTemplateId.value });
}

async function doGenerate() {
  if (!selectedTemplateId.value) return;
  archiving.value = true;
  try {
    const res = await generateReport({ template_id: selectedTemplateId.value });
    report.value = res.content;
    generatedAt.value = new Date().toLocaleString();
    viewingInstanceId.value = res.id;
    ElMessage.success(`已存档为版本 v${res.version}`);
    await fetchInstances();
  } finally {
    archiving.value = false;
  }
}

async function viewInstance(row) {
  const detail = await getReportInstance(row.id);
  report.value = detail.content;
  generatedAt.value = new Date(detail.created_at).toLocaleString();
  viewingInstanceId.value = detail.id;
}

async function doRegenerate(row) {
  const res = await regenerateReport(row.id);
  report.value = res.content;
  generatedAt.value = new Date().toLocaleString();
  viewingInstanceId.value = res.id;
  ElMessage.success(`已重新生成（版本 v${res.version}），历史快照保留`);
  await fetchInstances();
}

watch(selectedTemplateId, fetchInstances);

function openTemplateDialog(tpl = null) {
  editingId.value = tpl?.id ?? null;
  form.name = tpl?.name ?? "";
  form.period_type = tpl?.period_type ?? "weekly";
  form.metric_ids = [...(tpl?.metric_ids ?? [])];
  Object.assign(form.sections, tpl?.sections ?? {});
  if (!metricStore.list.length) metricStore.fetchList();
  dialogVisible.value = true;
}

async function saveTemplate() {
  if (!form.name.trim()) return ElMessage.warning("请填写模板名称");
  if (!form.metric_ids.length) return ElMessage.warning("请至少选择一个指标");
  saving.value = true;
  try {
    const payload = {
      name: form.name.trim(),
      period_type: form.period_type,
      metric_ids: form.metric_ids,
      sections: { ...form.sections },
    };
    if (editingId.value) {
      await updateReportTemplate(editingId.value, payload);
      ElMessage.success("模板已更新");
    } else {
      payload.project_id = projectStore.lockedId || undefined;
      const created = await createReportTemplate(payload);
      selectedTemplateId.value = created.id;
      ElMessage.success("模板已创建");
    }
    dialogVisible.value = false;
    await fetchTemplates();
  } finally {
    saving.value = false;
  }
}

async function doDelete(tpl) {
  await ElMessageBox.confirm(`确定删除模板「${tpl.name}」？`, "删除确认", { type: "warning" });
  await deleteReportTemplate(tpl.id);
  if (selectedTemplateId.value === tpl.id) {
    selectedTemplateId.value = null;
    report.value = null;
  }
  await fetchTemplates();
  ElMessage.success("模板已删除");
}

function reportToText() {
  const lines = [report.value.title, ""];
  report.value.narrative.forEach((sec) => {
    lines.push(`【${sectionTitle(sec.section)}】`);
    sec.sentences.forEach((s) => lines.push(s));
    lines.push("");
  });
  lines.push("—— 本报告数字由平台指标计算出口产生（算写分离），可逐项对账。");
  return lines.join("\n");
}

async function copyReport() {
  try {
    await navigator.clipboard.writeText(reportToText());
    ElMessage.success("报告文本已复制");
  } catch {
    ElMessage.error("复制失败（浏览器剪贴板权限）");
  }
}

function printReport() {
  window.print();
}

onMounted(async () => {
  await projectStore.fetchProjects();
  await fetchTemplates();
});
</script>

<style scoped>
.reports__templates {
  display: flex;
  gap: var(--pwc-space-3);
  flex-wrap: wrap;
  margin-bottom: var(--pwc-space-5);
  min-height: 72px;
}

.reports__tpl {
  position: relative;
  width: 220px;
  padding: var(--pwc-space-3) var(--pwc-space-4);
  border: 1px solid var(--pwc-border-subtle);
  border-radius: var(--pwc-radius-m);
  background: var(--pwc-surface);
  cursor: pointer;
}

.reports__tpl.is-active {
  border-color: var(--pwc-primary);
  box-shadow: 0 0 0 1px var(--pwc-primary);
}

.reports__tpl-name {
  font-weight: 600;
  margin-bottom: var(--pwc-space-1);
}

.reports__tpl-meta {
  font-size: var(--pwc-font-body-s);
  color: var(--pwc-text-secondary);
}

.reports__tpl-actions {
  margin-top: var(--pwc-space-2);
  display: flex;
  gap: var(--pwc-space-1);
}

.reports__history {
  margin-top: var(--pwc-space-5);
}

.reports__doc {
  border: 1px solid var(--pwc-border-subtle);
  border-radius: var(--pwc-radius-m);
  background: var(--pwc-surface);
  padding: var(--pwc-space-6);
}

.reports__doc-title {
  font-size: var(--pwc-font-title-m);
  margin-bottom: var(--pwc-space-1);
  display: flex;
  align-items: center;
  gap: var(--pwc-space-2);
}

.reports__src-tag {
  font-weight: 400;
}

.reports__doc-sub {
  font-size: var(--pwc-font-body-s);
  color: var(--pwc-text-secondary);
  margin-bottom: var(--pwc-space-5);
}

.reports__section {
  margin-bottom: var(--pwc-space-5);
}

.reports__sec-title {
  font-size: var(--pwc-font-body-l);
  font-weight: 600;
  border-left: 3px solid var(--pwc-primary);
  padding-left: var(--pwc-space-2);
  margin-bottom: var(--pwc-space-3);
}

.reports__table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--pwc-font-body-s);
}

.reports__table th,
.reports__table td {
  border-bottom: 1px solid var(--pwc-border-subtle);
  padding: var(--pwc-space-2) var(--pwc-space-3);
  text-align: left;
}

.reports__table th.num,
.reports__table td.num {
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.reports__note {
  color: var(--pwc-text-secondary);
}

/* 红涨绿跌 */
.reports__table td.is-up {
  color: var(--pwc-up, #d62222);
}

.reports__table td.is-down {
  color: var(--pwc-down, #059669);
}

.reports__sentence {
  line-height: 1.9;
  margin-bottom: var(--pwc-space-1);
}

.reports__doc-foot {
  display: flex;
  justify-content: flex-end;
  gap: var(--pwc-space-3);
  margin-top: var(--pwc-space-5);
}
</style>
