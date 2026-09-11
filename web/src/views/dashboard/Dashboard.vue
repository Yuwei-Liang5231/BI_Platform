<!-- pwc-regime: product-ui -->
<script setup>
/**
 * 统一看板：主题分组指标卡（当前值/环比）+ 选中指标日序列大图（ECharts）+ 导出 CSV。
 * 无权限指标显示占位（403 仅提示，不跳转——request.js 统一处理）。
 */
import { computed, onMounted, reactive, ref, watch } from "vue";
import * as echarts from "echarts";
import { ElMessage } from "element-plus";

import { metricValue, exportMetric, exportCsvBlob } from "@/api/query";
import TrendBadge from "@/components/business/TrendBadge.vue";
import { formatMetricValue } from "@/utils/format";
import { usePeriodRange } from "@/composables/usePeriodRange";
import { useMetricStore } from "@/stores/metric";
import { useAuthStore } from "@/stores/auth";

const metricStore = useMetricStore();
const auth = useAuthStore();

const search = ref("");
const activeTopic = ref("all");
const cards = reactive({}); // id -> { value, change, forbidden, loading }
const selectedId = ref(null);
const trendRows = ref([]);

const metrics = computed(() => metricStore.list.filter((m) => m.status === "active"));

const topics = computed(() => {
  const set = new Set(metrics.value.map((m) => m.topic || "general"));
  return ["all", ...set];
});

const visible = computed(() =>
  activeTopic.value === "all"
    ? metrics.value
    : metrics.value.filter((m) => (m.topic || "general") === activeTopic.value),
);

// 只在当前主题可见列表中选中——图表永远跟随所在主题，不会残留上一主题的指标
const selected = computed(() => visible.value.find((m) => m.id === selectedId.value));

// 统计周期：默认上一自然月，可自定义（看板所有取数共用）
const { dateRange, range } = usePeriodRange();

// 序列全为 null（区间内无一天有数据）视为无数据，用于空态提示
const hasTrendData = computed(() => trendRows.value.some((r) => r.value !== null));

// 所选区间无数据时，提示数据覆盖范围（来自 metric-value 响应的 coverage）
const rangeCoverageTip = computed(() => {
  const card = selected.value ? cards[selected.value.id] : null;
  if (!card || card.forbidden || hasTrendData.value) return "";
  const cov = card.coverage;
  if (!cov?.start || !cov?.end) return "所选区间无数据（该指标未登记时间覆盖范围）";
  return `所选区间无数据：数据覆盖为 ${cov.start} ~ ${cov.end}，可在右上角调整统计周期`;
});

function coverageTip(card) {
  const cov = card?.coverage;
  if (!cov?.start || !cov?.end) return "该指标未登记时间覆盖范围";
  return `数据覆盖：${cov.start} ~ ${cov.end}，所选区间不在覆盖范围内`;
}

async function reloadForRange() {
  Object.keys(cards).forEach((k) => delete cards[k]);
  await loadCards();
  await loadTrend();
}

watch(dateRange, reloadForRange);

// 切换主题时默认选中该主题的第一个指标（若主题为空则清空选中），
// 避免折线图停留在上一主题的指标上造成困惑
watch(activeTopic, () => {
  selectedId.value = visible.value[0]?.id ?? null;
});

async function loadCards() {
  await Promise.all(
    visible.value.map(async (m) => {
      if (cards[m.id]) return;
      cards[m.id] = { loading: true };
      try {
        const res = await metricValue({ metric: m.code, ...range.value, compare: "mom" });
        cards[m.id] = { ...res, loading: false };
      } catch {
        cards[m.id] = { forbidden: true, loading: false };
      }
    }),
  );
  if (!selectedId.value && visible.value.length) {
    selectedId.value = visible.value[0].id;
  }
}

async function loadTrend() {
  if (!selected.value) return;
  try {
    const response = await exportCsvBlob({ metric: selected.value.code, ...range.value });
    const text = await response.data.text();
    const lines = text.replace(/^\uFEFF/, "").trim().split(/\r?\n/);
    const rows = [];
    for (const line of lines.slice(1)) {
      const [date, value] = line.split(",");
      if (date) rows.push({ date, value: value === "" || value === undefined ? null : Number(value) });
    }
    trendRows.value = rows;
  } catch {
    trendRows.value = [];
  }
  rerender();
}

async function handleExport() {
  if (!selected.value) return;
  try {
    await exportMetric({ metric: selected.value.code, ...range.value });
    ElMessage.success("已导出 CSV");
  } catch {
    /* 拦截器已提示 */
  }
}

const chartEl = ref(null);
let chart = null;

function renderChart() {
  if (!chartEl.value) return;
  if (!chart) chart = echarts.init(chartEl.value);
  chart.setOption({
    grid: { left: 72, right: 24, top: 24, bottom: 36 },
    tooltip: { trigger: "axis" },
    xAxis: {
      type: "category",
      data: trendRows.value.map((r) => r.date),
      axisLine: { lineStyle: { color: "#DFE3E6" } },
    },
    yAxis: { type: "value", splitLine: { lineStyle: { color: "#F5F7F8" } } },
    series: [
      {
        type: "line",
        data: trendRows.value.map((r) => r.value),
        connectNulls: false,
        symbol: "none",
        itemStyle: { color: "#FD5108" },
        lineStyle: { color: "#FE7C39", width: 2 },
      },
    ],
  });
  chart.resize();
}

function rerender() {
  requestAnimationFrame(renderChart);
}

watch(selectedId, loadTrend);

async function fetchData() {
  await metricStore.fetchList(search.value ? { search: search.value } : {});
  await loadCards();
}

onMounted(fetchData);
</script>

<template>
  <div class="page-container">
    <div class="page-header">
      <div>
        <h1 class="page-header__title">统一看板</h1>
        <p class="page-header__subtitle">同一口径 · 同一数值 · 不完整周期显示"—"</p>
      </div>
      <div class="page-header__actions">
        <el-date-picker
          v-model="dateRange"
          type="daterange"
          value-format="YYYY-MM-DD"
          range-separator="~"
          start-placeholder="开始日期"
          end-placeholder="结束日期"
          :clearable="false"
        />
        <el-button :disabled="!selected" type="primary" @click="handleExport">导出 CSV</el-button>
      </div>
    </div>

    <div class="dash__topics">
      <button
        v-for="t in topics"
        :key="t"
        class="tag-chip"
        :class="{ 'is-active': activeTopic === t }"
        @click="activeTopic = t"
      >
        {{ t === "all" ? "全部主题" : t }}
      </button>
    </div>

    <div class="grid-12">
      <div
        v-for="m in visible"
        :key="m.id"
        class="pwc-card stat-card col-span-3"
        role="button"
        tabindex="0"
        :class="{ 'is-selected': selectedId === m.id }"
        @click="selectedId = m.id"
        @keyup.enter="selectedId = m.id"
      >
        <span class="stat-card__label">{{ m.name }}</span>
        <span v-if="cards[m.id]?.loading" v-loading="true" class="stat-card__loading" />
        <template v-else-if="cards[m.id]?.forbidden">
          <span class="metric-empty">—</span>
          <span class="pwc-badge pwc-badge--grey">无权限</span>
        </template>
        <template v-else>
          <strong class="stat-card__value">{{ formatMetricValue(cards[m.id]?.value ?? null) }}</strong>
          <TrendBadge :change="cards[m.id]?.change ?? null" />
          <span
            v-if="cards[m.id]?.period_complete === false"
            class="pwc-badge pwc-badge--grey"
            :title="coverageTip(cards[m.id])"
          >
            区间无数据
          </span>
        </template>
      </div>
    </div>

    <section v-if="selected" class="pwc-card dash__chart-card">
      <div class="pwc-card__header">
        <h4>{{ selected.name }} · 日序列（{{ range.start }} ~ {{ range.end }}）</h4>
      </div>
      <div v-if="hasTrendData" ref="chartEl" class="dash__chart"></div>
      <template v-else>
        <p class="metric-empty">—</p>
        <p v-if="rangeCoverageTip" class="dash__cov-hint">{{ rangeCoverageTip }}</p>
      </template>
    </section>

    <p v-if="!auth.canWrite" class="dash__readonly">
      你当前为只读角色，仅可查看；如需建指标请联系管理员。
    </p>
  </div>
</template>

<style scoped>
.dash__topics {
  display: flex;
  flex-wrap: wrap;
  gap: var(--pwc-space-3);
  margin-bottom: var(--pwc-space-6);
}

.dash__cov-hint {
  margin-top: var(--pwc-space-2);
  color: var(--pwc-text-secondary);
  font-size: var(--pwc-font-body-s);
}

.stat-card {
  cursor: pointer;
}

.stat-card.is-selected {
  border-color: var(--pwc-bg-brand);
}

.stat-card__loading {
  display: block;
  width: 100%;
  height: 40px;
}

.dash__chart-card {
  margin-top: var(--pwc-space-5);
}

.dash__chart {
  width: 100%;
  height: 360px;
}

.dash__readonly {
  margin-top: var(--pwc-space-5);
  color: var(--pwc-text-secondary);
}
</style>
