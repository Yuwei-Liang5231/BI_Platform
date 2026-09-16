<!-- pwc-regime: product-ui -->
<script setup>
/**
 * 指标目录：主题分组 + 层级展开卡片 + 中文名/别名搜索（后端已过可见性过滤）。
 * 当前值/环比走 metric-value(compare=mom)；周期不完整显示 "—"。
 */
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRouter } from "vue-router";

import { metricValue } from "@/api/query";
import MetricTrend from "@/components/business/MetricTrend.vue";
import TrendBadge from "@/components/business/TrendBadge.vue";
import { formatMetricValue } from "@/utils/format";
import { usePeriodRange } from "@/composables/usePeriodRange";
import { useMetricStore } from "@/stores/metric";
import { useProjectStore } from "@/stores/project";

const router = useRouter();
const metricStore = useMetricStore();
const projectStore = useProjectStore();

const search = ref("");
const activeTopic = ref("all");
// valueMap[metricId] = { value, change, period_complete, loading }
const valueMap = reactive({});

// 统计周期：默认上一自然月（数据完整的常规选择），可自定义
const { dateRange, range } = usePeriodRange();

const metrics = computed(() => metricStore.list);

const topics = computed(() => {
  const set = new Set(metrics.value.map((m) => m.topic || "general"));
  return ["all", ...set];
});

// 主题分组 + 层级（parent_id 挂父卡片下）
const grouped = computed(() => {
  const filtered =
    activeTopic.value === "all"
      ? metrics.value
      : metrics.value.filter((m) => (m.topic || "general") === activeTopic.value);
  const byId = new Map(filtered.map((m) => [m.id, { ...m, children: [] }]));
  const roots = [];
  for (const item of byId.values()) {
    const parent = item.parent_id ? byId.get(item.parent_id) : null;
    if (parent) parent.children.push(item);
    else roots.push(item);
  }
  return roots;
});

async function loadValues(items) {
  const { start, end } = range.value;
  await Promise.all(
    items.map(async (m) => {
      if (valueMap[m.id]) return;
      valueMap[m.id] = { loading: true };
      try {
        const res = await metricValue({ metric: m.code, start, end, compare: "mom" });
        valueMap[m.id] = { ...res, loading: false };
      } catch {
        valueMap[m.id] = { value: null, loading: false };
      }
    }),
  );
}

async function fetchData() {
  const pid = projectStore.currentId;
  const params = { ...(search.value ? { search: search.value } : {}), ...(pid ? { project_id: pid } : {}) };
  const list = await metricStore.fetchList(params);
  loadValues(list);
}

function openDetail(id) {
  router.push(`/metrics/${id}`);
}

// 区间变更：清空缓存值并按新区间重取
watch(dateRange, () => {
  Object.keys(valueMap).forEach((k) => delete valueMap[k]);
  loadValues(metricStore.list);
});

// B9.3：切换项目重新拉取
watch(() => projectStore.currentId, fetchData);

onMounted(fetchData);
</script>

<template>
  <div class="page-container">
    <div class="page-header">
      <div>
        <h1 class="page-header__title">指标目录</h1>
        <p class="page-header__subtitle">统一口径 · 一次定义 · 处处一致</p>
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
        <el-input
          v-model="search"
          placeholder="搜索中文名 / 别名 / 编码"
          clearable
          style="width: 260px"
          @keyup.enter="fetchData"
          @clear="fetchData"
        />
        <el-button type="primary" @click="fetchData">搜索</el-button>
      </div>
    </div>

    <div class="catalog__topics">
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

    <div v-loading="metricStore.loading" class="catalog__grid">
      <article
        v-for="m in grouped"
        :key="m.id"
        class="pwc-card catalog-card"
        role="button"
        tabindex="0"
        @click="openDetail(m.id)"
        @keyup.enter="openDetail(m.id)"
      >
        <header class="catalog-card__head">
          <div>
            <h3 class="catalog-card__name">{{ m.name }}</h3>
            <p v-if="m.aliases?.length" class="catalog-card__aliases">
              别名：{{ m.aliases.join(" / ") }}
            </p>
          </div>
          <span class="pwc-badge pwc-badge--grey">{{ m.status }}</span>
        </header>
        <div class="catalog-card__value">
          <span
            v-if="valueMap[m.id]?.loading"
            v-loading="true"
            class="catalog-card__value-loading"
          />
          <template v-else>
            <strong class="catalog-card__num">{{
              formatMetricValue(valueMap[m.id]?.value ?? null)
            }}</strong>
            <TrendBadge
              v-if="m.status === 'active'"
              :change="valueMap[m.id]?.change ?? null"
            />
          </template>
        </div>
        <div class="catalog-card__trend">
          <MetricTrend :data="[]" />
        </div>
        <ul v-if="m.children?.length" class="catalog-card__children">
          <li
            v-for="child in m.children"
            :key="child.id"
            class="catalog-card__child"
            @click.stop="openDetail(child.id)"
          >
            <span>{{ child.name }}</span>
            <strong>{{ formatMetricValue(valueMap[child.id]?.value ?? null) }}</strong>
          </li>
        </ul>
      </article>

      <el-empty v-if="!metricStore.loading && !grouped.length" description="没有匹配的指标" />
    </div>
  </div>
</template>

<style scoped>
.catalog__topics {
  display: flex;
  flex-wrap: wrap;
  gap: var(--pwc-space-3);
  margin-bottom: var(--pwc-space-6);
}

.catalog__grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: var(--pwc-space-5);
  min-height: 200px;
}

.catalog-card {
  cursor: pointer;
  transition: border-color 0.2s;
}

.catalog-card:hover,
.catalog-card:focus-visible {
  border-color: var(--pwc-bg-brand);
}

.catalog-card__head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: var(--pwc-space-3);
  margin-bottom: var(--pwc-space-4);
}

.catalog-card__name {
  font-family: var(--pwc-font-sans);
  font-weight: 600;
  font-size: var(--pwc-font-heading-s);
}

.catalog-card__aliases {
  color: var(--pwc-text-secondary);
  font-size: var(--pwc-font-body-s);
  margin-top: var(--pwc-space-1);
}

.catalog-card__value {
  display: flex;
  align-items: baseline;
  gap: var(--pwc-space-3);
  min-height: 40px;
}

.catalog-card__num {
  font-size: var(--pwc-font-data-l);
  font-weight: 700;
}

.catalog-card__value-loading {
  display: inline-block;
  width: 120px;
  height: 32px;
}

.catalog-card__trend {
  margin: var(--pwc-space-3) 0;
}

.catalog-card__children {
  list-style: none;
  margin: 0;
  padding: 0;
  border-top: 1px solid var(--pwc-border-subtle);
}

.catalog-card__child {
  display: flex;
  justify-content: space-between;
  padding: var(--pwc-space-2) 0;
  cursor: pointer;
  color: var(--pwc-text-secondary);
}

.catalog-card__child:hover {
  color: var(--pwc-bg-brand);
}
</style>
