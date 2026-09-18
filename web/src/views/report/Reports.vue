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

    <!-- 左右两栏（方案 A）：左 = 模板 + 历史存档（常驻可见），右 = 报告正文 -->
    <div class="reports__layout">
      <aside class="reports__side">
        <h3 class="reports__side-title">报告模板</h3>
        <div class="reports__side-list">
          <div
            v-for="t in templates"
            :key="t.id"
            class="reports__side-item"
            :class="{ 'is-active': t.id === selectedTemplateId }"
            role="button"
            @click="selectTemplate(t)"
          >
            <div class="reports__side-name">{{ t.name }}</div>
            <div class="reports__side-meta">
              {{ periodLabel(t.period_type) }} · {{ t.metric_ids.length }} 个指标
            </div>
            <div class="reports__side-actions">
              <el-button text size="small" @click.stop="openTemplateDialog(t)">编辑</el-button>
              <el-button v-if="auth.isAdmin" text size="small" type="danger" @click.stop="doDelete(t)">
                删除
              </el-button>
            </div>
          </div>
          <p v-if="!templates.length" class="reports__side-empty">
            暂无模板——点右上角「新建报告模板」
          </p>
        </div>

        <template v-if="selectedTemplateId">
          <h3 class="reports__side-title">
            历史存档
            <el-tooltip content="快照不受口径变更影响；点击查看，行内可重新生成新版本">
              <el-icon class="reports__side-help"><QuestionFilled /></el-icon>
            </el-tooltip>
          </h3>
          <div class="reports__side-list reports__side-list--scroll">
            <div
              v-for="i in instances"
              :key="i.id"
              class="reports__side-item reports__side-item--inst"
              :class="{ 'is-active': i.id === viewingInstanceId }"
              role="button"
              @click="viewInstance(i)"
            >
              <div class="reports__side-name">
                <el-tag size="small" type="info" class="reports__ver-tag">v{{ i.version }}</el-tag>
                <span>{{ i.period_start }} ~ {{ i.period_end }}</span>
              </div>
              <div class="reports__side-meta">
                {{ i.narrative_source === "llm" ? "AI 叙述" : "规则叙述" }} ·
                {{ i.created_by }} · {{ shortTime(i.created_at) }}
              </div>
              <el-button
                text
                size="small"
                class="reports__regen"
                :loading="regeneratingId === i.id"
                @click.stop="doRegenerate(i)"
              >
                重新生成
              </el-button>
            </div>
            <p v-if="!instances.length" class="reports__side-empty">
              尚无存档——点右上角「生成并存档」
            </p>
          </div>
        </template>
      </aside>

      <section class="reports__main">
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

      <!-- ① 执行摘要（总览叙述置顶） -->
      <section
        v-for="sec in overviewNarrative"
        :key="sec.section"
        class="reports__section"
      >
        <h3 class="reports__sec-title">{{ sectionTitle(sec.section) }}</h3>
        <p v-for="(s, i) in sec.sentences" :key="i" class="reports__sentence">{{ s }}</p>
      </section>

      <!-- ② 指标卡（大数字 + 涨跌）+ 结论表 + 趋势图 -->
      <section class="reports__section">
        <h3 class="reports__sec-title">指标结论</h3>
        <div class="reports__cards">
          <div v-for="c in report.conclusions" :key="c.ref" class="reports__card">
            <div class="reports__card-name">
              {{ c.name }}
              <span class="reports__card-code">{{ c.code }}</span>
            </div>
            <div class="reports__card-value">{{ formatMetricValue(c.value) }}</div>
            <div class="reports__card-trends">
              <span
                v-if="report.sections.mom && c.mom_pct != null"
                class="reports__card-delta"
                :class="trendClass(c.mom_pct)"
              >
                环比 {{ c.mom_pct > 0 ? "▲" : c.mom_pct < 0 ? "▼" : "" }} {{ formatPercent(c.mom_pct) }}
              </span>
              <span
                v-if="report.sections.yoy && c.yoy_pct != null"
                class="reports__card-delta reports__card-delta--sub"
                :class="trendClass(c.yoy_pct)"
              >
                同比 {{ c.yoy_pct > 0 ? "▲" : c.yoy_pct < 0 ? "▼" : "" }} {{ formatPercent(c.yoy_pct) }}
              </span>
              <span v-if="c.constant" class="reports__card-note">全期值</span>
              <span
                v-else-if="c.period_complete === false && c.data_through"
                class="reports__card-note"
              >数据截至 {{ c.data_through }}</span>
            </div>
          </div>
        </div>
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
        <!-- 趋势图（本期 vs 环比基期双线，异动日红圈标注；日报无趋势） -->
        <template v-if="trendList.length">
          <div class="reports__charts-note">趋势走势（本期 vs 环比基期，红圈为异动日）</div>
          <div class="reports__charts">
            <div v-for="t in trendList" :key="t.metric_id" class="reports__chart-card">
              <div class="reports__chart-title">
                {{ t.name }}（{{ t.code }}）
                <span v-if="trendRangeLabel(t)" class="reports__chart-range">{{ trendRangeLabel(t) }}</span>
              </div>
              <div :ref="(el) => setTrendEl(el, t.metric_id)" class="reports__chart-body"></div>
            </div>
          </div>
        </template>
      </section>

      <!-- ③ 异动 / 指标结论 / 归因 叙述章节 -->
      <section
        v-for="sec in midNarrative"
        :key="sec.section"
        class="reports__section"
      >
        <h3 class="reports__sec-title">
          {{ sectionTitle(sec.section) }}
          <TermTip v-if="sec.section === 'anomaly'" term="anomaly" />
        </h3>
        <p v-for="(s, i) in sec.sentences" :key="i" class="reports__sentence">{{ s }}</p>
      </section>

      <!-- ④ 归因贡献图（正贡献红 / 负贡献绿；旧快照无 attributions 时自动跳过） -->
      <section v-if="attrList.length || attrEmptyNote" class="reports__section">
        <h3 class="reports__sec-title">
          归因贡献图
          <TermTip term="attribution" />
        </h3>
        <div v-if="attrList.length" class="reports__charts">
          <div v-for="att in attrList" :key="att.metric_id + att.dimension" class="reports__chart-card">
            <div class="reports__chart-title">
              {{ att.name }} · 按「{{ att.dimension }}」拆解的贡献
            </div>
            <div
              :ref="(el) => setAttrEl(el, att.metric_id + att.dimension)"
              class="reports__chart-body reports__chart-body--bar"
            ></div>
          </div>
        </div>
        <p v-else class="reports__muted">{{ attrEmptyNote }}</p>
      </section>

      <!-- ⑤ 洞察与建议（收尾） -->
      <section
        v-for="sec in insightNarrative"
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

        <!-- 生成中：主区明确反馈（用户反馈"点击后像没反应"） -->
        <div
          v-else-if="previewing || archiving"
          v-loading="true"
          element-loading-text="正在生成报告：计算指标 → 生成叙述 → 数字审计，约需十几秒…"
          class="reports__loading"
        >
          <p class="reports__loading-hint">
            正在为模板「{{ templates.find((t) => t.id === selectedTemplateId)?.name }}」生成报告，
            完成后正文将显示在此处。
          </p>
        </div>

        <el-empty
          v-else
          description="选择左侧模板生成报告，或点击历史存档查看快照；每个数字与指标计算接口对账一致"
        />
      </section>
    </div>

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
          <el-checkbox v-model="form.sections.overview">执行摘要</el-checkbox>
          <el-checkbox v-model="form.sections.mom">环比</el-checkbox>
          <el-checkbox v-model="form.sections.yoy">同比</el-checkbox>
          <el-checkbox v-model="form.sections.anomaly">异动检测</el-checkbox>
          <el-checkbox v-model="form.sections.attribution">归因来源</el-checkbox>
          <el-checkbox v-model="form.sections.insight">洞察与建议</el-checkbox>
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
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { QuestionFilled } from "@element-plus/icons-vue";
import * as echarts from "echarts";
import TermTip from "@/components/glossary/TermTip.vue";

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
  sections: { overview: true, mom: true, yoy: true, anomaly: true, attribution: true, insight: true },
});

const PERIOD_LABELS = { daily: "日报", weekly: "周报", monthly: "月报" };
const SECTION_TITLES = {
  overview: "执行摘要",
  metrics: "指标结论叙述",
  anomaly: "异动检测",
  attribution: "变化来源归因",
  insight: "洞察与建议",
};
const periodLabel = (t) => PERIOD_LABELS[t] ?? t;
const sectionTitle = (k) => SECTION_TITLES[k] ?? k;
// 红涨绿跌（2026-09-15 契约）：涨红跌绿，与 good/bad 业务语义解耦
const trendClass = (pct) =>
  pct > 0 ? "is-up" : pct < 0 ? "is-down" : "";

/* ---------- 报告图表（趋势折线 + 归因条形；ECharts SVG 渲染，打印友好） ---------- */
const trendList = computed(() => report.value?.trends ?? []);
const attrList = computed(
  () => (report.value?.attributions ?? []).filter((a) => (a.top_dimensions ?? []).length),
);
/* 版式按设计稿分组：执行摘要最先 → 指标卡/结论表+趋势 → 异动/归因叙述 → 归因图 → 洞察与建议 */
const overviewNarrative = computed(() =>
  (report.value?.narrative ?? []).filter((s) => s.section === "overview"),
);
const midNarrative = computed(() =>
  (report.value?.narrative ?? []).filter(
    (s) => s.section !== "overview" && s.section !== "insight",
  ),
);
const insightNarrative = computed(() =>
  (report.value?.narrative ?? []).filter((s) => s.section === "insight"),
);
/* 归因图空置说明：归因仅对「检测到异动且可拆解」的指标生成（B10-2 宁缺毋滥） */
const attrEmptyNote = computed(() => {
  if (!report.value?.sections?.attribution || attrList.value.length) return "";
  if ((report.value?.anomalies ?? []).length)
    return "本期异动指标均为比率类或无可用拆解维度，未生成归因贡献图。";
  return "本期未检测到异动指标——归因贡献图仅对发生显著异动的指标拆解生成，无异动则无归因。";
});
const chartInstances = new Map(); // key → echarts 实例（重渲染前 dispose）

function disposeCharts() {
  chartInstances.forEach((inst) => inst.dispose());
  chartInstances.clear();
}

function _compact(v) {
  // 图表轴紧凑数字：同样保留 1 位小数（2026-09-18 修订：指标数字统一 1 位小数）
  const abs = Math.abs(v ?? 0);
  if (abs >= 1e8) return `${(v / 1e8).toFixed(1)}亿`;
  if (abs >= 1e4) return `${(v / 1e4).toFixed(1)}万`;
  return `${Number(v ?? 0).toFixed(1)}`;
}

function trendRangeLabel(t) {
  if (!t.current?.length) return "";
  const a = t.current[0].date;
  const b = t.current[t.current.length - 1].date;
  return a === b ? a : `${a} ~ ${b}`;
}

function _trendOption(t) {
  const dates = t.current.map((p) => p.date);
  // 基期与本期等长（周期完整性契约：环比基期 = 有效窗口整体前移一期），按下标对齐
  const prevVals = (t.previous ?? []).map((p) => p.value);
  const anomalyIdx = new Map(
    (t.anomaly_dates ?? [])
      .map((d) => [dates.indexOf(d), d])
      .filter(([idx]) => idx >= 0),
  );
  return {
    animation: false,
    grid: { left: 64, right: 16, top: 32, bottom: 28 },
    legend: { top: 0, left: 0, itemWidth: 14, data: ["本期", "上期"] },
    tooltip: { trigger: "axis" },
    xAxis: { type: "category", data: dates, axisLabel: { fontSize: 10 } },
    yAxis: { type: "value", scale: true, axisLabel: { formatter: _compact, fontSize: 10 } },
    series: [
      {
        name: "本期",
        type: "line",
        data: t.current.map((p) => p.value),
        showSymbol: dates.length <= 40,
        symbolSize: 4,
        lineStyle: { width: 2, color: "#534AB7" },
        itemStyle: { color: "#534AB7" },
        connectNulls: true,
        markPoint: {
          silent: true,
          label: { show: false },
          data: [...anomalyIdx.keys()].map((idx) => ({
            coord: [idx, t.current[idx].value],
            symbol: "circle",
            symbolSize: 11,
            itemStyle: { color: "rgba(0,0,0,0)", borderColor: "#D62222", borderWidth: 2 },
          })),
        },
      },
      {
        name: "上期",
        type: "line",
        data: dates.map((_, i) => prevVals[i] ?? null),
        showSymbol: false,
        lineStyle: { width: 1.5, color: "#B4B2A9", type: "dashed" },
        itemStyle: { color: "#B4B2A9" },
        connectNulls: true,
      },
    ],
  };
}

function _attrOption(att) {
  const tops = att.top_dimensions.slice(0, 8);
  return {
    animation: false,
    grid: { left: 110, right: 56, top: 8, bottom: 24 },
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
    xAxis: { type: "value", axisLabel: { formatter: _compact, fontSize: 10 } },
    yAxis: {
      type: "category",
      inverse: true,
      data: tops.map((td) => String(td.value)),
      axisLabel: { fontSize: 11, width: 100, overflow: "truncate" },
    },
    series: [
      {
        type: "bar",
        barMaxWidth: 18,
        data: tops.map((td) => ({
          value: td.contribution,
          itemStyle: { color: (td.contribution ?? 0) >= 0 ? "#D62222" : "#059669" },
        })),
        label: {
          show: true,
          position: "right",
          fontSize: 10,
          formatter: ({ value }) => _compact(value),
        },
      },
    ],
  };
}

const trendEls = new Map();
const attrEls = new Map();
const setTrendEl = (el, id) => {
  if (el) trendEls.set(id, el);
  else trendEls.delete(id);
};
const setAttrEl = (el, key) => {
  if (el) attrEls.set(key, el);
  else attrEls.delete(key);
};

function renderCharts() {
  disposeCharts();
  if (!report.value) return;
  trendList.value.forEach((t) => {
    const el = trendEls.get(t.metric_id);
    if (!el) return;
    const inst = echarts.init(el, null, { renderer: "svg" });
    inst.setOption(_trendOption(t));
    chartInstances.set(`t${t.metric_id}`, inst);
  });
  attrList.value.forEach((att) => {
    const key = att.metric_id + att.dimension;
    const el = attrEls.get(key);
    if (!el) return;
    const inst = echarts.init(el, null, { renderer: "svg" });
    inst.setOption(_attrOption(att));
    chartInstances.set(`a${key}`, inst);
  });
}

watch(
  () => report.value,
  () => nextTick(renderCharts),
);

onBeforeUnmount(disposeCharts);

async function fetchTemplates() {
  templates.value = await listReportTemplates(
    projectStore.lockedId ? { project_id: projectStore.lockedId } : undefined,
  );
}

function selectTemplate(t) {
  selectedTemplateId.value = t.id;
}

function shortTime(ts) {
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString();
}

async function doPreview() {
  if (!selectedTemplateId.value) return;
  previewing.value = true;
  ElMessage.info("正在生成报告：计算指标 → 生成叙述 → 数字审计，约需十几秒，请稍候…");
  try {
    report.value = await previewReport({ template_id: selectedTemplateId.value });
    generatedAt.value = new Date().toLocaleString();
    viewingInstanceId.value = null;
    ElMessage.success("报告已生成");
  } finally {
    previewing.value = false;
  }
}

/* ---------- 存档与历史（B12-3） ---------- */
const archiving = ref(false);
const instances = ref([]);
const viewingInstanceId = ref(null);
const regeneratingId = ref(null);

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
  ElMessage.info("正在生成并存档：计算指标 → 生成叙述 → 数字审计，约需十几秒，请稍候…");
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
  regeneratingId.value = row.id;
  try {
    const res = await regenerateReport(row.id);
    report.value = res.content;
    generatedAt.value = new Date().toLocaleString();
    viewingInstanceId.value = res.id;
    ElMessage.success(`已重新生成（版本 v${res.version}），历史快照保留`);
    await fetchInstances();
  } finally {
    regeneratingId.value = null;
  }
}

// 切模板：历史列表重取 + 清空正文区（避免停留在上一份报告）
watch(selectedTemplateId, () => {
  report.value = null;
  viewingInstanceId.value = null;
  fetchInstances();
});

function openTemplateDialog(tpl = null) {
  editingId.value = tpl?.id ?? null;
  form.name = tpl?.name ?? "";
  form.period_type = tpl?.period_type ?? "weekly";
  form.metric_ids = [...(tpl?.metric_ids ?? [])];
  // 先归位默认值再合并模板配置（旧模板无 insight 键时取默认开）
  Object.assign(
    form.sections,
    { overview: true, mom: true, yoy: true, anomaly: true, attribution: true, insight: true },
    tpl?.sections ?? {},
  );
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
  // 图表降级为数据行（复制文本不含图形，数字保持可对账）
  (report.value.conclusions ?? []).forEach((c) => {
    const parts = [`本期 ${formatMetricValue(c.value)}`];
    if (report.value.sections?.mom) parts.push(`环比 ${formatPercent(c.mom_pct)}`);
    if (report.value.sections?.yoy) parts.push(`同比 ${formatPercent(c.yoy_pct)}`);
    lines.push(`${c.name}（${c.code}）：${parts.join("，")}`);
  });
  lines.push("");
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
/* ---------- 左右两栏（方案 A）：左 = 模板 + 历史，右 = 正文 ---------- */
.reports__layout {
  display: flex;
  gap: var(--pwc-space-5);
  align-items: flex-start;
}

.reports__side {
  width: 280px;
  flex-shrink: 0;
}

.reports__side-title {
  display: flex;
  align-items: center;
  gap: var(--pwc-space-1);
  font-size: var(--pwc-font-body-m);
  font-weight: 600;
  margin-bottom: var(--pwc-space-2);
}

.reports__side-help {
  color: var(--pwc-text-secondary);
  cursor: help;
}

.reports__side-list {
  display: flex;
  flex-direction: column;
  gap: var(--pwc-space-2);
  margin-bottom: var(--pwc-space-5);
}

.reports__side-list--scroll {
  max-height: 420px;
  overflow-y: auto;
  padding-right: 2px;
}

.reports__side-item {
  border: 1px solid var(--pwc-border-subtle);
  border-radius: var(--pwc-radius-m);
  background: var(--pwc-surface);
  padding: var(--pwc-space-2) var(--pwc-space-3);
  cursor: pointer;
}

.reports__side-item:hover {
  border-color: var(--pwc-primary);
}

.reports__side-item.is-active {
  border-color: var(--pwc-primary);
  box-shadow: 0 0 0 1px var(--pwc-primary);
}

.reports__side-name {
  display: flex;
  align-items: center;
  gap: var(--pwc-space-1);
  font-weight: 600;
  font-size: var(--pwc-font-body-s);
}

.reports__ver-tag {
  flex-shrink: 0;
}

.reports__side-meta {
  font-size: var(--pwc-font-body-s);
  color: var(--pwc-text-secondary);
  margin-top: 2px;
}

.reports__side-actions {
  display: flex;
  gap: var(--pwc-space-1);
  margin-top: 2px;
}

.reports__regen {
  margin-top: 2px;
  padding: 0;
  height: auto;
}

.reports__side-empty {
  font-size: var(--pwc-font-body-s);
  color: var(--pwc-text-secondary);
  padding: var(--pwc-space-2) 0;
}

.reports__main {
  flex: 1;
  min-width: 0;
}

/* 生成中面板：给"点了没反应"一个明确的视觉状态 */
.reports__loading {
  min-height: 320px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px dashed var(--pwc-border-subtle);
  border-radius: var(--pwc-radius-m);
  background: var(--pwc-surface);
}
.reports__loading-hint {
  margin: 0 24px;
  color: var(--pwc-text-secondary);
  font-size: var(--pwc-font-body-s, 13px);
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

/* 指标卡（大数字 + 涨跌三角） */
.reports__cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: var(--pwc-space-3);
  margin-bottom: var(--pwc-space-4);
}

.reports__card {
  border: 1px solid var(--pwc-border, #e5e7eb);
  border-radius: 8px;
  padding: var(--pwc-space-3) var(--pwc-space-4);
  background: var(--pwc-surface, #fff);
}

.reports__card-name {
  font-size: var(--pwc-font-body-s);
  color: var(--pwc-text-secondary, #535353);
  margin-bottom: var(--pwc-space-1);
}

.reports__card-code {
  color: var(--pwc-text-tertiary, #8c8c8c);
  font-size: var(--pwc-font-body-xs);
  margin-left: var(--pwc-space-1);
}

.reports__card-value {
  font-size: 26px;
  font-weight: 700;
  line-height: 1.3;
  margin-bottom: var(--pwc-space-1);
}

.reports__card-trends {
  display: flex;
  flex-wrap: wrap;
  gap: var(--pwc-space-2);
  align-items: baseline;
}

.reports__card-delta {
  font-size: var(--pwc-font-body-s);
  font-weight: 600;
}

.reports__card-delta--sub {
  font-weight: 400;
  color: var(--pwc-text-secondary, #535353);
}

.reports__card-delta.is-up {
  color: var(--pwc-up, #d62222);
}

.reports__card-delta.is-down {
  color: var(--pwc-down, #059669);
}

.reports__card-note {
  font-size: var(--pwc-font-body-xs);
  color: var(--pwc-text-tertiary, #8c8c8c);
}

.reports__muted {
  color: var(--pwc-text-tertiary, #8c8c8c);
  font-size: var(--pwc-font-body-s);
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

/* 图表区：趋势/归因卡片栅格 */
.reports__charts-note {
  font-size: var(--pwc-font-body-s);
  color: var(--pwc-text-secondary);
  margin: var(--pwc-space-3) 0 var(--pwc-space-2);
}

.reports__charts {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
  gap: var(--pwc-space-3);
}

.reports__chart-card {
  border: 1px solid var(--pwc-border-subtle);
  border-radius: var(--pwc-radius-m);
  padding: var(--pwc-space-2) var(--pwc-space-3);
  min-width: 0;
}

.reports__chart-title {
  font-size: var(--pwc-font-body-s);
  font-weight: 600;
  margin-bottom: var(--pwc-space-1);
  display: flex;
  align-items: baseline;
  gap: var(--pwc-space-2);
  min-width: 0;
}

.reports__chart-range {
  font-weight: 400;
  color: var(--pwc-text-secondary);
}

.reports__chart-body {
  height: 220px;
  width: 100%;
}

.reports__chart-body--bar {
  height: 200px;
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

/* 打印/导出 PDF：只保留正文文档（含顶部导航与操作按钮一并隐藏） */
@media print {
  .reports__side,
  .page-header,
  .reports__doc-foot {
    display: none;
  }

  .reports__layout {
    display: block;
  }

  .reports__doc {
    border: none;
    padding: 0;
  }
}
</style>

<style>
/* 非 scoped：打印时隐藏全局顶栏（scoped 选择器够不到 DefaultLayout） */
@media print {
  .layout__header {
    display: none !important;
  }

  .layout__main {
    padding: 0 !important;
  }
}
</style>
