<!-- pwc-regime: product-ui -->
<script setup>
/**
 * 指标详情：口径说明（为什么这样定/易错点）、当前值与日序列趋势（export CSV 解析）、
 * 变更历史、折叠区展示存档 SQL。
 */
import { computed, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import * as echarts from "echarts";

import { exportCsvBlob } from "@/api/query";
import TrendBadge from "@/components/business/TrendBadge.vue";
import { formatMetricValue } from "@/utils/format";
import { usePeriodRange } from "@/composables/usePeriodRange";
import { useMetricStore } from "@/stores/metric";
import { useQueryStore } from "@/stores/query";

const route = useRoute();
const router = useRouter();
const metricStore = useMetricStore();
const queryStore = useQueryStore();

const metricId = computed(() => route.params.id);
const metric = computed(() => metricStore.detail);
const current = ref(null);
const trendRows = ref([]);
// 全期常数指标（未绑定时间字段）：无逐日序列，趋势区显示说明而非折线
const isConstant = computed(() => current.value?.constant === true);
const sqlExpanded = ref(false);
const sqlText = ref("");
// 统计周期：默认上一自然月，可自定义
const { dateRange, range } = usePeriodRange();

// 口径分歧选项归一化：兼容 {name, description} / {label, value} / 字符串
const disambiguationOptions = computed(() => {
  const dis = metric.value?.disambiguation;
  if (!dis) return [];
  const def = dis.default;
  return (dis.options ?? []).map((raw) => {
    const opt = typeof raw === "string" ? { name: raw } : raw;
    const value = opt.value ?? opt.name ?? opt.label ?? "";
    return {
      name: opt.name ?? opt.label ?? String(raw),
      description: opt.description ?? (typeof raw === "object" && opt.label && opt.value && opt.value !== opt.label ? `口径取值：${opt.value}` : ""),
      isDefault: def !== undefined && value === def,
    };
  });
});

// 区间变更时重取当前值与日序列（首次加载由 loadAll 负责）
watch(dateRange, () => {
  if (!metricId.value) return;
  loadCurrent();
  loadTrend();
});

/* ── 变更历史：后端存 before/after 快照，前端生成可读摘要 ──
   后端只对口径变更强制填写 reason；别名/说明等基础信息编辑不强制，
   原因列显示占位符，变更内容由 before/after 字段级 diff 自动生成。 */
const CHANGE_FIELD_LABELS = {
  name: "名称",
  aliases: "别名",
  definition: "口径说明",
  calc_rule: "计算规则（口径）",
  topic: "主题",
  parent_id: "父级指标",
  disambiguation: "维度消歧",
  owner_department: "归属部门",
  status: "状态",
};

function fmtChangeVal(v) {
  if (v === undefined) return "（无）";
  if (v === null || v === "") return "（空）";
  const s = typeof v === "string" ? v : JSON.stringify(v);
  return s.length > 26 ? `${s.slice(0, 26)}…` : s;
}

function changeSummary(row) {
  const before = row?.before ?? {};
  const after = row?.after ?? {};
  const keys = Object.keys(CHANGE_FIELD_LABELS).filter(
    (k) => JSON.stringify(after[k] ?? null) !== JSON.stringify(before[k] ?? null),
  );
  if (!keys.length) return "（元数据变更，无业务字段差异）";
  return keys
    .map((k) => {
      const label = CHANGE_FIELD_LABELS[k];
      if (k === "calc_rule") {
        return `计算规则（口径）变更${after.ver ? `（版本 → v${after.ver}）` : ""}`;
      }
      return `${label}：${fmtChangeVal(before[k])} → ${fmtChangeVal(after[k])}`;
    })
    .join("；");
}

async function loadAll() {
  await metricStore.fetchDetail(metricId.value);
  await Promise.all([loadCurrent(), loadTrend(), loadSql(), metricStore.fetchChanges(metricId.value)]);
}

// 请求序号守卫：快速改区间时旧响应后到会覆盖新状态，过期响应一律丢弃。
// 当前值与趋势各自独立计数——两者并发发出，不能互相作废
let currentSeq = 0;
let trendSeq = 0;

async function loadCurrent() {
  const seq = ++currentSeq;
  try {
    const res = await queryStore.fetchValue({
      metric: metricId.value,
      start: range.value.start,
      end: range.value.end,
      compare: "mom",
    });
    if (seq !== currentSeq) return;
    // change_pct（百分数）→ TrendBadge 需要的小数变化率
    current.value = {
      ...res,
      change:
        res.compare?.change_pct !== null && res.compare?.change_pct !== undefined
          ? res.compare.change_pct / 100
          : null,
    };
  } catch {
    if (seq !== currentSeq) return;
    current.value = null;
  }
}

async function loadTrend() {
  const seq = ++trendSeq;
  let rows = [];
  try {
    const response = await exportCsvBlob({
      metric: metricId.value,
      start: range.value.start,
      end: range.value.end,
    });
    const text = await response.data.text();
    const lines = text.replace(/^\uFEFF/, "").trim().split(/\r?\n/);
    for (const line of lines.slice(1)) {
      const [date, value] = line.split(",");
      // 只判 date：无值日保留 null 占位，保证 x 轴连续（丢行会让日期轴断裂、
      // 「数据截至」截止线错位，且与看板页解析口径不一致）
      if (date) rows.push({ date, value: value === "" || value === undefined ? null : Number(value) });
    }
  } catch {
    rows = [];
  }
  if (seq !== trendSeq) return;
  trendRows.value = rows;
  rerender();
}

async function loadSql() {
  try {
    const archive = await metricStore.fetchSql(metricId.value);
    sqlText.value = typeof archive === "string" ? archive : (archive?.sql ?? JSON.stringify(archive, null, 2));
  } catch {
    sqlText.value = "";
  }
}

// ECharts 趋势（详情页大图）
const chartEl = ref(null);
let chart = null;

function renderChart() {
  if (!chartEl.value || !trendRows.value.length) return;
  // 容器是 v-if 按需挂载：DOM 被销毁重建后旧实例会失联——检测到即重建
  if (chart && chart.getDom() !== chartEl.value) {
    chart.dispose();
    chart = null;
  }
  if (!chart) chart = echarts.init(chartEl.value);
  const dates = trendRows.value.map((r) => r.date);
  const values = trendRows.value.map((r) => r.value);
  // 尾部无数据的日期段画「数据截至」截止线——与当前值卡的「区间无数据」互相印证
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
    grid: { left: 64, right: 24, top: 24, bottom: 32 },
    tooltip: { trigger: "axis" },
    xAxis: {
      type: "category",
      data: dates,
      axisLine: { lineStyle: { color: "#DFE3E6" } },
    },
    yAxis: {
      type: "value",
      splitLine: { lineStyle: { color: "#F5F7F8" } },
    },
    series: [
      {
        type: "line",
        data: values,
        symbol: "circle",
        symbolSize: 5,
        itemStyle: { color: "#FD5108" },
        lineStyle: { color: "#FE7C39", width: 2 },
        connectNulls: false,
        // setOption 是合并模式：无截止线时必须显式清空，否则上一个指标的标记会残留
        markLine: cutoffMark ?? { silent: true, symbol: "none", data: [] },
      },
    ],
  });
}

function rerender() {
  requestAnimationFrame(renderChart);
}

onMounted(async () => {
  await loadAll();
  rerender();
});
</script>

<template>
  <div class="page-container">
    <div class="page-header">
      <div>
        <h1 class="page-header__title">{{ metric?.name ?? "指标详情" }}</h1>
        <p class="page-header__subtitle">
          <span class="pwc-badge pwc-badge--grey">{{ metric?.code }}</span>
          <span v-if="metric?.aliases?.length" class="detail__aliases">
            别名：{{ metric.aliases.join(" / ") }}
          </span>
        </p>
      </div>
      <div class="page-header__actions">
        <el-button @click="router.push('/metrics')">返回目录</el-button>
        <el-button type="primary" @click="router.push('/dashboard')">去看板</el-button>
      </div>
    </div>

    <div v-loading="metricStore.loading">
      <div class="grid-12">
        <!-- 当前值卡 -->
        <section class="pwc-card col-span-4">
          <div class="pwc-card__header">
            <h4>当前值（{{ range.start }} ~ {{ range.end }}）</h4>
          </div>
          <el-date-picker
            v-model="dateRange"
            type="daterange"
            value-format="YYYY-MM-DD"
            range-separator="~"
            start-placeholder="开始日期"
            end-placeholder="结束日期"
            :clearable="false"
            style="width: 100%; margin-bottom: 8px"
          />
          <p class="detail__value">
            {{ current?.value == null ? "—" : formatMetricValue(current.value) }}
          </p>
          <!-- 契约 v2：部分周期显示真实值 + 数据截至标注；区间零数据才提示无数据 -->
          <p v-if="current && current.value == null" class="metric-empty">
            所选区间无数据<template
              v-if="current.coverage?.start && current.coverage?.end"
            >（数据覆盖：{{ current.coverage.start }} ~ {{ current.coverage.end }}，可调整上方统计周期）</template>
          </p>
          <p v-else-if="current && current.period_complete === false && current.data_through" class="metric-empty">
            统计周期未完整：仅统计 {{ current.data_through }} 之前的数据，环比已按同长度区间对齐
          </p>
          <TrendBadge v-if="current" :change="current.change ?? null" />
        </section>

        <!-- 口径说明 -->
        <section class="pwc-card col-span-8">
          <div class="pwc-card__header">
            <h4>口径说明</h4>
          </div>
          <p class="detail__definition">{{ metric?.definition || "（未填写口径说明）" }}</p>
          <template v-if="metric?.disambiguation">
            <h5 class="detail__subhead">为什么这样定 / 易错点</h5>
            <p>{{ metric.disambiguation.question }}</p>
            <ul class="detail__options">
              <li v-for="(opt, i) in disambiguationOptions" :key="i">
                <strong>{{ opt.name }}</strong>
                <span v-if="opt.description">：{{ opt.description }}</span>
                <span v-if="opt.isDefault" class="pwc-badge">当前默认口径</span>
              </li>
            </ul>
          </template>
        </section>

        <!-- 日序列趋势 -->
        <section class="pwc-card col-span-12">
          <div class="pwc-card__header">
            <h4>
              日序列趋势（{{ range.start }} ~ {{ range.end }}）
              <span v-if="isConstant" class="pwc-badge pwc-badge--grey"
                title="全期常数指标：未绑定时间字段，不按时间过滤，任意统计区间返回同一全期汇总值">全期值</span>
            </h4>
          </div>
          <!-- 全期常数指标：与统计区间无关，无逐日序列（与统一看板口径一致） -->
          <p v-if="isConstant" class="metric-empty">
            该指标为「全期常数」（未绑定时间字段）：不按时间过滤，任意统计区间返回同一全期汇总值，因此没有逐日折线。
          </p>
          <div v-else-if="trendRows.length" ref="chartEl" class="detail__chart"></div>
          <p v-else class="metric-empty">—</p>
        </section>

        <!-- 变更历史 -->
        <section class="pwc-card col-span-12">
          <div class="pwc-card__header">
            <h4>变更历史</h4>
          </div>
          <el-table :data="metricStore.changes" style="width: 100%">
            <el-table-column prop="created_at" label="时间" width="180" />
            <el-table-column label="操作人" width="140">
              <template #default="{ row }">{{ row.operator_id || "—" }}</template>
            </el-table-column>
            <el-table-column label="原因" min-width="200">
              <template #default="{ row }">{{ row.reason || "基础信息更新（未要求填写原因）" }}</template>
            </el-table-column>
            <el-table-column label="变更内容" min-width="320">
              <template #default="{ row }">{{ changeSummary(row) }}</template>
            </el-table-column>
          </el-table>
        </section>

        <!-- 存档 SQL（折叠区） -->
        <section class="pwc-card col-span-12">
          <el-collapse v-model="sqlExpanded">
            <el-collapse-item title="存档 SQL（编译产物）" name="sql">
              <pre class="detail__sql">{{ sqlText || "（无存档 SQL）" }}</pre>
            </el-collapse-item>
          </el-collapse>
        </section>
      </div>
    </div>
  </div>
</template>

<style scoped>
.detail__aliases {
  margin-left: var(--pwc-space-3);
}

.detail__value {
  font-size: var(--pwc-font-data-l);
  font-weight: 700;
  margin-bottom: var(--pwc-space-2);
}

.detail__definition {
  white-space: pre-wrap;
}

.detail__subhead {
  margin: var(--pwc-space-4) 0 var(--pwc-space-2);
  font-family: var(--pwc-font-sans);
  font-weight: 600;
}

.detail__options {
  margin: 0;
  padding-left: var(--pwc-space-5);
  color: var(--pwc-text-secondary);
}

.detail__options li {
  margin-bottom: var(--pwc-space-2);
}

.detail__options strong {
  color: var(--pwc-text-primary);
  font-weight: 600;
}

.detail__options .pwc-badge {
  margin-left: var(--pwc-space-2);
}

.detail__chart {
  width: 100%;
  height: 320px;
}

.detail__sql {
  background: var(--pwc-bg-layer-1);
  padding: var(--pwc-space-4);
  overflow: auto;
  font-family: Consolas, Monaco, monospace;
  font-size: 13px;
  white-space: pre-wrap;
}
</style>
