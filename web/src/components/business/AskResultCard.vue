<!-- pwc-regime: product-ui -->
<script setup>
/**
 * 问数结果卡（B9.2-4 抽取复用）：拆解表格 / 单值卡。
 * 纯展示组件——当前轮与历史轮消息流共用；数值已由后端单点出口算好。
 */
import { computed } from "vue";
import { formatMetricValue } from "@/utils/format";

const props = defineProps({
  data: { type: Object, required: true },   // ask/execute 返回同构（kind: value|breakdown）
  label: { type: String, default: "" },     // 并列指标名（主指标可能缺 name 时兜底）
});

const isBreakdown = computed(() => props.data?.kind === "breakdown");

const breakdownCols = [
  { key: "value", label: "当期值" },
  { key: "change_abs", label: "绝对变化" },
  { key: "change_pct", label: "变化率 %" },
];

const changePct = computed(() => {
  const c = props.data?.compare;
  if (!c || c.change_pct === null || c.change_pct === undefined) return null;
  return c.change_pct;
});

const displayName = computed(() => props.data?.name || props.label || "");
</script>

<template>
  <!-- 拆解表格 -->
  <section v-if="isBreakdown" class="pwc-card ask__result">
    <div class="ask__result-head">
      <span class="ask__result-metric">{{ displayName }} · 按「{{ data.dimension }}」拆解</span>
      <span class="pwc-badge pwc-badge--grey">
        {{ data.start }} ~ {{ data.end }}
        <template v-if="!data.period_complete && data.data_through">
          · 数据截至 {{ data.data_through }}
        </template>
        · 共 {{ data.total_groups }} 组，显示前 {{ data.rows.length }} 组
      </span>
    </div>
    <el-table :data="data.rows" size="default" class="ask__table">
      <el-table-column prop="dimension" label="维度值" min-width="140" />
      <el-table-column
        v-for="col in breakdownCols"
        :key="col.key"
        :prop="col.key"
        :label="col.label"
        min-width="130"
        sortable
      >
        <template #default="{ row }">
          <template v-if="col.key === 'value'">{{ formatMetricValue(row.value) }}</template>
          <template v-else-if="col.key === 'change_abs'">
            <span v-if="row.change_abs === null">—</span>
            <span v-else :class="row.change_abs >= 0 ? 'up' : 'down'">
              {{ row.change_abs >= 0 ? "+" : "" }}{{ formatMetricValue(row.change_abs) }}
            </span>
          </template>
          <template v-else>
            <span v-if="row.change_pct === null">—</span>
            <span v-else :class="row.change_pct >= 0 ? 'up' : 'down'">
              {{ row.change_pct >= 0 ? "▲" : "▼" }} {{ Math.abs(row.change_pct).toFixed(2) }}%
            </span>
          </template>
        </template>
      </el-table-column>
    </el-table>
    <div v-if="data.compare" class="ask__hint">
      基期：{{ data.compare.start }} ~ {{ data.compare.end }}
      <template v-if="!data.compare.period_complete">（数据截至 {{ data.compare.data_through }}）</template>
    </div>
    <p v-else class="ask__hint">未启用对比或全期常数指标（无基期概念）</p>
  </section>

  <!-- 单值卡 -->
  <section v-else class="pwc-card ask__result">
    <div class="ask__result-head">
      <span class="ask__result-metric">{{ displayName }}</span>
      <span class="pwc-badge pwc-badge--grey">
        {{ data.start }} ~ {{ data.end }}
        <template v-if="!data.period_complete && data.data_through">
          · 数据截至 {{ data.data_through }}
        </template>
      </span>
    </div>
    <div class="ask__result-value">{{ formatMetricValue(data.value) }}</div>
    <div v-if="changePct !== null" class="ask__result-compare">
      {{ data.compare?.type === "yoy" ? "同比" : "环比" }}
      <span :class="changePct >= 0 ? 'up' : 'down'">
        {{ changePct >= 0 ? "▲" : "▼" }} {{ Math.abs(changePct).toFixed(2) }}%
      </span>
    </div>
    <p v-else class="ask__hint">无对比基期数据</p>
  </section>
</template>
