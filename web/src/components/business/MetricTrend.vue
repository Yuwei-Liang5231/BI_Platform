<!-- pwc-regime: product-ui -->
<script setup>
/**
 * 目录卡片小趋势图（ECharts 折线，PwC dataviz 序列橙色强调）。
 * data 为 [{ date, value }]；空/不完整周期显示 "—"，不画误导性 0。
 */
import { onBeforeUnmount, onMounted, ref, watch } from "vue";
import * as echarts from "echarts";

const props = defineProps({
  data: { type: Array, default: () => [] },
  height: { type: Number, default: 64 },
});

const el = ref(null);
let chart = null;

function render() {
  if (!el.value) return;
  if (!chart) {
    chart = echarts.init(el.value);
  }
  const hasData = props.data && props.data.length > 0;
  chart.setOption(
    {
      grid: { left: 4, right: 4, top: 6, bottom: 4 },
      xAxis: {
        type: "category",
        show: false,
        data: hasData ? props.data.map((d) => d.date) : [],
      },
      yAxis: { type: "value", show: false },
      series: [
        {
          type: "line",
          data: hasData ? props.data.map((d) => d.value) : [],
          symbol: "none",
          lineStyle: { color: "#FE7C39", width: 2 }, // Orange 400（dataviz 序列）
          areaStyle: { color: "rgba(255, 205, 168, 0.35)" }, // Orange 200
        },
      ],
    },
    true,
  );
  chart.resize();
}

onMounted(() => {
  render();
  window.addEventListener("resize", render);
});

onBeforeUnmount(() => {
  window.removeEventListener("resize", render);
  if (chart) {
    chart.dispose();
    chart = null;
  }
});

watch(() => props.data, render, { deep: true });
</script>

<template>
  <div v-if="data && data.length" ref="el" class="metric-trend" :style="{ height: `${height}px` }" />
  <span v-else class="metric-empty">—</span>
</template>

<style scoped>
.metric-trend {
  width: 100%;
}
</style>
