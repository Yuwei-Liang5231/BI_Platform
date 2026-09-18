<!-- pwc-regime: product-ui -->
<script setup>
/**
 * D19 涨跌徽章（2026-09-15 约定：红涨绿跌）：三角 + 文字，不单靠颜色。
 * change 为小数变化率（0.05 = +5%）；null 显示 "—"。
 * 颜色编码变化方向：涨=红（▲）/ 跌=绿（▼）；goodWhenUp 保留参数兼容，不再影响配色。
 */
import { computed } from "vue";

import { trendDirection } from "@/utils/format";

const props = defineProps({
  change: { type: Number, default: null },
  goodWhenUp: { type: Boolean, default: true },
});

const direction = computed(() => trendDirection(props.change, props.goodWhenUp));
const hasChange = computed(() => props.change !== null && props.change !== undefined);
const kind = computed(() => {
  if (!hasChange.value || direction.value === "flat") return "flat";
  return props.change > 0 ? "up" : "down";
});
const text = computed(() => {
  if (!hasChange.value) return "—";
  const pct = `${Math.abs(props.change * 100).toFixed(1)}%`;
  if (direction.value === "flat") return `持平 ${pct}`;
  return `${props.change > 0 ? "▲" : "▼"} ${pct}`;
});
</script>

<template>
  <span class="trend-badge" :class="`trend-badge--${kind}`">
    <span class="trend-badge__text">{{ text }}</span>
  </span>
</template>

<style scoped>
.trend-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: var(--pwc-font-body-s);
  font-weight: 500;
}

.trend-badge__text {
  white-space: nowrap;
}

.trend-badge--up { color: var(--pwc-up, #D62222); }
.trend-badge--down { color: var(--pwc-down, #059669); }
.trend-badge--flat { color: var(--pwc-text-secondary); }
</style>
