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
  { key: "share", label: "占比" },
  { key: "change_abs", label: "绝对变化" },
  { key: "change_pct", label: "变化率 %" },
];

const AGG_CN = { sum: "求和", avg: "平均", count: "计数", max: "最大", min: "最小", count_distinct: "去重计数" };

/** 口径摘要行（B9.2-5 ⑥）：flat / expression 两形态 → 业务可读短句列表 */
const profileLines = computed(() => {
  const p = props.data?.metric_profile;
  if (!p) return [];
  const lines = [];
  if (p.definition) lines.push(`口径说明：${p.definition}`);
  if (p.kind === "flat") {
    lines.push(`算法：对 ${p.source_table}.${p.source_column} ${AGG_CN[p.aggregation] ?? p.aggregation}`);
    if (p.filter) lines.push(`过滤条件：${p.filter}`);
  } else {
    lines.push(`算法：${p.expression}`);
    for (const o of p.operands ?? []) {
      lines.push(`操作数 ${o.name} = ${o.table}.${o.column} ${AGG_CN[o.aggregation] ?? o.aggregation}${o.filter ? `（过滤：${o.filter}）` : ""}`);
    }
  }
  return lines;
});

const changePct = computed(() => {
  const c = props.data?.compare;
  if (!c || c.change_pct === null || c.change_pct === undefined) return null;
  return c.change_pct;
});

const displayName = computed(() => props.data?.name || props.label || "");

// ---------------------------------------------------------------- 导出 CSV（B9.2-6）
// 数据已在手上（后端单点出口算好），纯前端生成 CSV——口径与展示完全一致。

function _csvCell(v) {
  const s = v === null || v === undefined ? "" : String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function _downloadCsv(filename, header, rows) {
  const lines = [header, ...rows].map((r) => r.map(_csvCell).join(","));
  const blob = new Blob(["\ufeff" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename.replace(/[\\/:*?"<>|]/g, "_") + ".csv";
  a.click();
  URL.revokeObjectURL(url);
}

function exportResult() {
  const d = props.data;
  if (isBreakdown.value) {
    // 导出列与表格渲染同源（breakdownCols 驱动）：后续新增列自动进 CSV
    const header = ["维度值", ...breakdownCols.map((c) => c.label), "查询区间", "基期区间"];
    const rows = d.rows.map((r) => {
      const cells = breakdownCols.map((col) => {
        const v = r[col.key];
        if (col.key === "share") return v === null || v === undefined ? "" : Math.round(v * 100);
        if (col.key === "change_pct") return v === null || v === undefined ? "" : Math.round(v);
        return v;
      });
      return [r.dimension, ...cells, `${d.start}~${d.end}`, d.compare ? `${d.compare.start}~${d.compare.end}` : ""];
    });
    _downloadCsv(`${displayName.value}-拆解-${d.start}~${d.end}`, header, rows);
    return;
  }
  _downloadCsv(
    `${displayName.value}-${d.start}~${d.end}`,
    ["指标", "查询区间", "值", "对比类型", "基期区间", "变化率%"],
    [[
      displayName.value,
      `${d.start}~${d.end}`,
      d.value,
      d.compare?.type === "yoy" ? "同比" : d.compare?.type === "mom" ? "环比" : "",
      d.compare ? `${d.compare.start}~${d.compare.end}` : "",
      changePct.value === null ? "" : changePct.value.toFixed(1),
    ]],
  );
}
</script>

<template>
  <!-- 拆解表格 -->
  <section v-if="isBreakdown" class="pwc-card ask__result">
    <div class="ask__result-head">
      <span class="ask__result-metric">{{ displayName }} · 按「{{ data.dimension }}」拆解</span>
      <el-tag v-if="data.compare?.type" size="small" type="warning" effect="plain">
        {{ data.compare.type === "yoy" ? "同比" : "环比" }}
      </el-tag>
      <span class="pwc-badge pwc-badge--grey">
        {{ data.start }} ~ {{ data.end }}
        <template v-if="!data.period_complete && data.data_through">
          · 数据截至 {{ data.data_through }}
        </template>
        · 共 {{ data.total_groups }} 组，显示前 {{ data.rows.length }} 组
      </span>
      <el-button text type="primary" class="ask__export" @click="exportResult">⬇ 导出 CSV</el-button>
    </div>
    <!-- B9.2-5 一句话结论：后端模板拼装（数字全部来自计算出口） -->
    <div v-if="data.conclusion" class="ask__conclusion">💡 {{ data.conclusion }}</div>
    <el-table :data="data.rows" size="default" class="ask__table">
      <el-table-column prop="dimension" label="维度值" min-width="140" />
      <el-table-column
        v-for="col in breakdownCols"
        :key="col.key"
        :prop="col.key"
        :label="col.label"
        min-width="120"
        sortable
      >
        <template #default="{ row }">
          <template v-if="col.key === 'value'">{{ formatMetricValue(row.value) }}</template>
          <template v-else-if="col.key === 'share'">
            <div v-if="row.share !== null && row.share !== undefined" class="ask__share">
              <div class="ask__share-track">
                <span class="ask__share-bar" :style="{ width: `${Math.max(row.share * 100, 3)}%` }" />
              </div>
              <span class="ask__share-num">{{ Math.round(row.share * 100) }}%</span>
            </div>
            <span v-else>—</span>
          </template>
          <template v-else-if="col.key === 'change_abs'">
            <span v-if="row.change_abs === null">—</span>
            <span v-else :class="row.change_abs >= 0 ? 'up' : 'down'">
              {{ row.change_abs >= 0 ? "+" : "" }}{{ formatMetricValue(row.change_abs) }}
            </span>
          </template>
          <template v-else>
            <span v-if="row.change_pct === null">—</span>
            <span v-else :class="row.change_pct >= 0 ? 'up' : 'down'">
              {{ row.change_pct >= 0 ? "▲" : "▼" }} {{ Math.abs(Math.round(row.change_pct)) }}%
            </span>
          </template>
        </template>
      </el-table-column>
    </el-table>
    <div v-if="data.compare" class="ask__hint">
      {{ data.compare.type === "yoy" ? "同比基期（去年同期）" : "环比基期（上一等长周期）" }}
      ：{{ data.compare.start }} ~ {{ data.compare.end }}
      <template v-if="!data.compare.period_complete">（数据截至 {{ data.compare.data_through }}）</template>
      —— 与上方查询区间对比得出变化
    </div>
    <p v-else class="ask__hint">未启用对比或全期常数指标（无基期概念）</p>
    <details v-if="profileLines.length" class="ask__profile">
      <summary>口径说明（{{ displayName }}）</summary>
      <p v-for="(l, i) in profileLines" :key="i">{{ l }}</p>
    </details>
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
      <el-button text type="primary" class="ask__export" @click="exportResult">⬇ 导出 CSV</el-button>
    </div>
    <div v-if="data.conclusion" class="ask__conclusion">💡 {{ data.conclusion }}</div>
    <div class="ask__result-value">{{ formatMetricValue(data.value) }}</div>
    <div v-if="changePct !== null" class="ask__result-compare">
      {{ data.compare?.type === "yoy" ? "同比" : "环比" }}
      <span :class="changePct >= 0 ? 'up' : 'down'">
        {{ changePct >= 0 ? "▲" : "▼" }} {{ Math.abs(changePct).toFixed(1) }}%
      </span>
    </div>
    <p v-else class="ask__hint">无对比基期数据</p>
    <details v-if="profileLines.length" class="ask__profile">
      <summary>口径说明（{{ displayName }}）</summary>
      <p v-for="(l, i) in profileLines" :key="i">{{ l }}</p>
    </details>
  </section>
</template>

<style scoped>
/* 涨跌配色（红涨绿跌，B9.2-4 抽组件后必须在本组件内声明——父组件 scoped 样式穿不进来） */
.up {
  color: var(--pwc-up, #D62222);
}

.down {
  color: var(--pwc-down, #059669);
}

.ask__result {
  margin-top: var(--pwc-space-5);
}

.ask__result-head {
  display: flex;
  align-items: center;
  gap: var(--pwc-space-3);
  flex-wrap: wrap;
}

.ask__result-metric {
  font-weight: 600;
}

.ask__export {
  margin-left: auto;
}

.ask__table {
  margin-top: var(--pwc-space-3);
}

.ask__result-value {
  font-size: 40px;
  font-weight: 700;
  margin: var(--pwc-space-3) 0;
  font-family: var(--pwc-font-sans);
}

.ask__result-compare {
  color: var(--pwc-text-secondary);
}

.ask__hint {
  display: block;
  color: var(--pwc-text-secondary);
  font-size: 12px;
  margin-top: var(--pwc-space-1);
}

/* B9.2-5 一句话结论条 */
.ask__conclusion {
  margin: var(--pwc-space-3) 0;
  padding: var(--pwc-space-2) var(--pwc-space-3);
  background: rgba(253, 81, 8, 0.06);
  border-left: 3px solid var(--pwc-brand, #FD5108);
  border-radius: 6px;
  font-size: 14px;
  line-height: 1.6;
}

/* 占比列：紧凑条形（固定 72px 上限，不再撑满整列）+ 固定宽数值 */
.ask__share {
  display: flex;
  align-items: center;
  gap: var(--pwc-space-2);
  width: 100%;
  min-width: 130px;
}

.ask__share-track {
  flex: 0 1 72px;
  min-width: 36px;
  height: 8px;
  border-radius: 4px;
  background: rgba(0, 0, 0, 0.06);
  overflow: hidden;
}

.ask__share-bar {
  display: block;
  height: 100%;
  border-radius: 4px;
  background: var(--pwc-brand, #FD5108);
  opacity: 0.55;
}

.ask__share-num {
  flex-shrink: 0;
  min-width: 46px;
  text-align: right;
  font-size: 12px;
  color: var(--pwc-text-secondary);
  white-space: nowrap;
}

/* 口径透明脚注（B9.2-5 ⑥） */
.ask__profile {
  margin-top: var(--pwc-space-3);
  font-size: 12px;
  color: var(--pwc-text-secondary);
}

.ask__profile summary {
  cursor: pointer;
  user-select: none;
}

.ask__profile p {
  margin: var(--pwc-space-1) 0 0 var(--pwc-space-4);
  line-height: 1.6;
}
</style>
