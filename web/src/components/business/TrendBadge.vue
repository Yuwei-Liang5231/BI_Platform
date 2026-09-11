<!-- pwc-regime: product-ui -->
<script setup>
/**
 * D19 涨跌语义徽章：状态色（绿=向好 / 红=恶化）+ 箭头 + 文字，不单靠颜色。
 * change 为小数变化率（0.05 = +5%）；null 显示 "—"。
 */
import { computed } from "vue";

import { trendDirection } from "@/utils/format";

const props = defineProps({
  change: { type: Number, default: null },
  goodWhenUp: { type: Boolean, default: true },
});

const direction = computed(() => trendDirection(props.change, props.goodWhenUp));
const hasChange = computed(() => props.change !== null && props.change !== undefined);
const text = computed(() => {
  if (!hasChange.value) return "—";
  const pct = `${Math.abs(props.change * 100).toFixed(1)}%`;
  if (direction.value === "flat") return `持平 ${pct}`;
  return `${props.change > 0 ? "▲" : "▼"} ${pct}`;
});
</script>

<template>
  <span class="trend-badge" :class="`trend-badge--${direction}`">
    <span v-if="hasChange" aria-hidden="true">{{ direction === "good" ? "↑" : direction === "bad" ? "↓" : "=" }}</span>
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

.trend-badge--good { color: #059669; }
.trend-badge--bad { color: #DC2626; }
.trend-badge--flat, .trend-badge--none { color: var(--pwc-text-secondary); }
</style>
