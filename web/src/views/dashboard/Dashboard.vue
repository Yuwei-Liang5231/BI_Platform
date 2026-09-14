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
  return `所选区间与数据覆盖（${cov.start} ~ ${cov.end}）无交集，可调整右上角统计周期`;
}

async function reloadForRange() {
  Object.keys(cards).forEach((k) => delete cards[k]);
  await loadCards();
  await loadTrend();
}

watch(dateRange, reloadForRange);

// 切换主题时默认选中该主题的第一个指标（若主题为空则清空选中），
// 避免折线图停留在上一主题的指标上造成困惑；
// 同时补载新主题卡片——改区间会清空全部卡片但只重载当前所在主题，
// 不补载的话切换后其他主题的卡片会停留在空值（显示"区间无数据"）
watch(activeTopic, () => {
  selectedId.value = visible.value[0]?.id ?? null;
  loadCards();
});

// 请求序号守卫：快速改区间/切主题/点卡片时，慢的旧响应后到会覆盖新状态
// （表现为"点开的卡片显示的是别的指标的折线/数值"），过期响应一律丢弃
let cardsSeq = 0;
let trendSeq = 0;

async function loadCards() {
  const seq = ++cardsSeq;
  await Promise.all(
    visible.value.map(async (m) => {
      if (cards[m.id]) return;
      // 注意：cards 是 reactive，读出的占位符是代理对象，与原始对象引用不相等，
      // 不能用 === 判定归属——用唯一 token 标记（属性读取穿透代理，恒等成立）
      const token = Symbol();
      cards[m.id] = { loading: true, __token: token };
      try {
        const res = await metricValue({ metric: m.code, ...range.value, compare: "mom" });
        if (seq !== cardsSeq) {
          // 过期响应：区间/主题已再次变化。仅当占位符还是自己设的才清除，
          // 避免误删新一轮请求的 loading 占位（否则该卡片会永久卡在 loading）
          if (cards[m.id]?.__token === token) delete cards[m.id];
          return;
        }
        // change_pct（百分数）→ TrendBadge 需要的小数变化率
        const change =
          res.compare?.change_pct !== null && res.compare?.change_pct !== undefined
            ? res.compare.change_pct / 100
            : null;
        cards[m.id] = { ...res, change, loading: false };
      } catch {
        if (seq !== cardsSeq) {
          if (cards[m.id]?.__token === token) delete cards[m.id];
          return;
        }
        cards[m.id] = { forbidden: true, loading: false };
      }
    }),
  );
  if (seq === cardsSeq && !selectedId.value && visible.value.length) {
    selectedId.value = visible.value[0].id;
  }
}

async function loadTrend() {
  const seq = ++trendSeq;
  if (!selected.value) {
    trendRows.value = [];
    return;
  }
  let rows = [];
  try {
    const response = await exportCsvBlob({ metric: selected.value.code, ...range.value });
    const text = await response.data.text();
    const lines = text.replace(/^\uFEFF/, "").trim().split(/\r?\n/);
    for (const line of lines.slice(1)) {
      const [date, value] = line.split(",");
      if (date) rows.push({ date, value: value === "" || value === undefined ? null : Number(value) });
    }
  } catch {
    rows = [];
  }
  if (seq !== trendSeq) return; // 过期响应：已选中其他指标，丢弃
  trendRows.value = rows;
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
  // 容器是 v-if 按需挂载：选中无数据指标时 DOM 被销毁重建，
  // 旧 ECharts 实例仍挂在已脱离的节点上导致图表"消失"——检测到失联即重建
  if (chart && chart.getDom() !== chartEl.value) {
    chart.dispose();
    chart = null;
  }
  if (!chart) chart = echarts.init(chartEl.value);
  const dates = trendRows.value.map((r) => r.date);
  const values = trendRows.value.map((r) => r.value);
  // 尾部无数据的日期段画「数据截至」截止线——与卡片「区间无数据」（周期未被
  // 数据完整覆盖 → 显示"—"）互相印证，消除"卡片没值折线却有"的困惑
  const lastIdx = values.reduce((acc, v, i) => (v !== null && v !== undefined ? i : acc), -1);
  const cutoffMark =
    lastIdx >= 0 && lastIdx < dates.length - 1
      ? {
          silent: true,
          symbol: "none",
          data: [{ xAxis: dates[lastIdx] }],
          lineStyle: { color: "#B4BCC2", type: "dashed" },
          label: {
            formatter: `数据截至 ${dates[lastIdx].slice(5)}`,
            position: "end",
            color: "#7A838A",
            fontSize: 11,
          },
        }
      : undefined;
  chart.setOption({
    grid: { left: 72, right: 24, top: 24, bottom: 36 },
    tooltip: { trigger: "axis" },
    xAxis: {
      type: "category",
      data: dates,
      axisLine: { lineStyle: { color: "#DFE3E6" } },
    },
    yAxis: { type: "value", splitLine: { lineStyle: { color: "#F5F7F8" } } },
    series: [
      {
        type: "line",
        data: values,
        connectNulls: false,
        symbol: "none",
        itemStyle: { color: "#FD5108" },
        lineStyle: { color: "#FE7C39", width: 2 },
        // setOption 是合并模式：无截止线时必须显式清空，否则上一个指标的标记会残留
        markLine: cutoffMark ?? { silent: true, symbol: "none", data: [] },
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
        <p class="page-header__subtitle">同一口径 · 同一数值 · 部分周期标注「数据截至」</p>
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
          <!-- 契约 v2：部分周期显示真实值 + 「数据截至」标注；区间零数据才显示「区间无数据」 -->
          <span
            v-if="(cards[m.id]?.value ?? null) === null"
            class="pwc-badge pwc-badge--grey"
            :title="coverageTip(cards[m.id])"
          >
            区间无数据
          </span>
          <span
            v-else-if="cards[m.id]?.period_complete === false && cards[m.id]?.data_through"
            class="pwc-badge pwc-badge--grey"
            :title="`统计周期未完整：仅统计 ${cards[m.id].data_through} 之前的数据，环比已按同长度区间对齐`"
          >
            数据截至 {{ cards[m.id].data_through.slice(5) }}
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
