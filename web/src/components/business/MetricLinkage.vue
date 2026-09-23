<!-- pwc-regime: product-ui -->
<script setup>
/**
 * 多指标联动归因（A4）：目标指标异动时，自动找同项目内同期联动的指标
 * （Pearson |r| ≥ 0.6，对齐日值；受限候选剔除）+ AI 传播假设。
 *
 * - 自动加载（onMounted + watch props）；空态默认不渲染，show-empty 时给一句话；
 * - r 是统计量非指标值：按 z-score 同款例外保留 2 位小数；变化幅度 1 位小数；
 * - 红涨绿跌契约：▲ 红 / ▼ 绿，只保留三角，配文字；
 * - AI 解释零阻塞：失败/未配置 → 只展示候选清单与规则句。
 */
import { onMounted, ref, watch } from "vue";
import { useRouter } from "vue-router";

import { metricLinkage } from "@/api/ai";
import AiFeedback from "@/components/business/AiFeedback.vue";
import { useProjectStore } from "@/stores/project";

const props = defineProps({
  metricId: { type: [Number, String], required: true },
  start: { type: String, required: true },
  end: { type: String, required: true },
  showEmpty: { type: Boolean, default: false },
  projectId: { type: [Number, null], default: null },
});

const router = useRouter();
const projectStore = useProjectStore();

const loading = ref(false);
const loaded = ref(false);
const failed = ref(false);
const data = ref(null);

async function load() {
  loading.value = true;
  try {
    data.value = await metricLinkage({
      metricId: props.metricId,
      start: props.start,
      end: props.end,
    });
    failed.value = false;
  } catch {
    data.value = null; // 零阻塞：联动分析失败不影响宿主页面
    failed.value = true;
  } finally {
    loaded.value = true;
    loading.value = false;
  }
}

watch(
  () => [props.metricId, props.start, props.end],
  () => {
    loaded.value = false;
    data.value = null;
    failed.value = false;
    load();
  },
);

const gotoMetric = (m) => router.push(`/metrics/${m.metric_id}`);

const linkageText = (c) => (c.linkage === "inverse" ? "反向联动" : "同向联动");
const dirWord = (d) => (d === "up" ? "上升" : d === "down" ? "下降" : "持平");

onMounted(load);
</script>

<template>
  <div v-if="loaded && !failed && (data?.candidates?.length || showEmpty)" class="linkage">
    <!-- 窗口归一提示：所选区间过短/过长时后端已自动调整（否则单日区间算不出相关） -->
    <p v-if="data.window?.expanded" class="linkage__rule">
      所选区间不足以直接计算相关性，已按 {{ data.window.start }} ~ {{ data.window.end }} 分析。
    </p>
    <p v-if="data.interpretation" class="linkage__interp">
      {{ data.interpretation }}
      <el-tag type="success" effect="light" size="small">AI 假设</el-tag>
      <AiFeedback
        kind="metric_linkage"
        :target="`metric:${metricId}:${start}~${end}`"
        :project-id="projectId ?? projectStore.lockedId"
      />
    </p>
    <p v-else class="linkage__rule">{{ data.rule_text }}</p>

    <ul class="linkage__list">
      <li v-for="c in data.candidates" :key="c.metric_id" class="linkage__item">
        <span class="linkage__arrow" :class="c.direction === 'up' ? 'is-up' : c.direction === 'down' ? 'is-down' : ''">
          {{ c.direction === "up" ? "▲" : c.direction === "down" ? "▼" : "—" }}
          {{ dirWord(c.direction) }}
        </span>
        <span class="linkage__name" role="link" @click="gotoMetric(c)">{{ c.metric_name }}</span>
        <span v-if="c.change_pct !== null && c.change_pct !== undefined" class="linkage__pct">
          本期{{ c.change_pct > 0 ? "+" : "" }}{{ c.change_pct }}%
        </span>
        <el-tag
          :type="c.linkage === 'inverse' ? 'warning' : 'primary'"
          effect="light"
          size="small"
        >
          {{ linkageText(c) }}
        </el-tag>
        <span class="linkage__r">r = {{ c.correlation.toFixed(2) }}</span>
      </li>
    </ul>

    <p v-if="!data.candidates?.length" class="linkage__rule">
      同期未发现明显联动的指标（|r| ≥ 0.6 才纳入，宁缺毋滥）。
    </p>
  </div>
  <p v-else-if="loaded && failed && showEmpty" class="linkage__rule">
    联动分析暂不可用（请求失败或超时），可稍后重试；不影响本页其余功能。
  </p>
  <p v-else-if="loaded && showEmpty && !loading" class="linkage__rule">
    同期未发现明显联动的指标（|r| ≥ 0.6 才纳入，宁缺毋滥）。
  </p>
  <p v-else-if="loading" class="linkage__rule">正在扫描同期联动指标…</p>
</template>

<style scoped>
.linkage {
  margin-top: var(--pwc-space-3, 12px);
}

.linkage__interp {
  margin: 0 0 var(--pwc-space-2);
  line-height: 1.7;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--pwc-space-2);
}

.linkage__rule {
  margin: 0 0 var(--pwc-space-2);
  font-size: var(--pwc-font-body-s);
  color: var(--pwc-text-secondary);
}

.linkage__list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: grid;
  gap: 6px;
}

.linkage__item {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--pwc-space-2);
  font-size: var(--pwc-font-body-s);
}

/* 红涨绿跌契约：涨=红、跌=绿，只保留三角 */
.linkage__arrow.is-up {
  color: var(--pwc-up);
  font-weight: 700;
}

.linkage__arrow.is-down {
  color: var(--pwc-down);
  font-weight: 700;
}

.linkage__name {
  font-weight: 700;
  cursor: pointer;
}

.linkage__name:hover {
  color: var(--pwc-bg-brand);
}

.linkage__pct {
  color: var(--pwc-text-secondary);
}

.linkage__r {
  margin-left: auto;
  color: var(--pwc-text-secondary);
  font-variant-numeric: tabular-nums;
}
</style>
