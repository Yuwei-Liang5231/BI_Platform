<!-- pwc-regime: product-ui -->
<script setup>
/**
 * AI 问数（B9 / B9.2-2）：先摊开理解、再给答案。
 * - 理解卡：指标/时间/对比/拆解维度/筛选/排序/TopN，全部可改，改完重算
 * - LLM 只产意图（锚定真实指标与维度候选），数值由指标中心统一计算（口径同源）
 * - 拆解结果表格（组值/基期/变化，点击表头切换排序）；单值结果卡
 * - 逃生舱（mode=help）：意图解析不出可执行结构时纯对话引导，绝不含数字
 */
import { computed, reactive, ref } from "vue";
import { ElMessage } from "element-plus";

import {
  ask as askApi,
  askExecute as askExecuteApi,
  askDimensionValues,
  metricValue,
} from "@/api/query";
import { formatMetricValue } from "@/utils/format";
import { useMetricStore } from "@/stores/metric";
import { useAuthStore } from "@/stores/auth";

const auth = useAuthStore();
const metricStore = useMetricStore();

const question = ref("");
const asking = ref(false);
const card = ref(null);
const result = ref(null);
const executing = ref(false);

// 可编辑理解卡的本地状态（B9.2-2：拆解维度/筛选/排序/TopN 同样可改）
const cardEdit = reactive({
  metricCode: "",
  range: [],
  compare: "none",
  dimension: "",
  filters: [],
  order_by: "value",
  order: "desc",
  top_n: null,
});

const EXAMPLES = [
  "2026年1月销售额是多少",
  "上个月营业额环比如何",
  "最近30天 GMV 同比",
  "按地区拆解上个月销售额的环比",
  "上个月销售额各渠道环比，跌幅最厉害的前5",
];

const metricOptions = computed(() =>
  metricStore.list.map((m) => ({ code: m.code, name: m.name })),
);

const compareOptions = [
  { value: "none", label: "不对比" },
  { value: "mom", label: "环比（上一等长周期）" },
  { value: "yoy", label: "同比（去年同期）" },
];

const dimensionOptions = computed(() => card.value?.dimension_options ?? []);

const orderByOptions = [
  { value: "value", label: "当期值" },
  { value: "change_abs", label: "绝对变化" },
  { value: "change_pct", label: "变化率" },
];

// 筛选值候选（按列懒加载：value 下拉用真实取值，杜绝随手编值）
const dimValueOptions = reactive({});   // { column: [values] }
const dimValueLoading = reactive({});   // { column: bool }

const changePct = computed(() => {
  const c = result.value?.compare;
  if (!c || c.change_pct === null || c.change_pct === undefined) return null;
  return c.change_pct;
});

async function submit(questionOverride) {
  const q = (questionOverride ?? question.value).trim();
  if (!q) {
    ElMessage.warning("请输入问题");
    return;
  }
  asking.value = true;
  result.value = null;
  try {
    const res = await askApi(q);
    card.value = res;
    applyCard(res);
  } finally {
    asking.value = false;
  }
}

function applyCard(res) {
  cardEdit.metricCode = res.metric?.code ?? "";
  cardEdit.range = [res.start, res.end];
  cardEdit.compare = res.compare;
  cardEdit.dimension = res.dimension ?? "";
  cardEdit.filters = (res.filters ?? []).map((f) => ({ ...f }));
  cardEdit.order_by = res.order_by ?? "value";
  cardEdit.order = res.order ?? "desc";
  cardEdit.top_n = res.top_n ?? null;
  if (res.metric && !metricStore.list.length) metricStore.fetchList(); // 指标下拉数据（懒加载）
  // 预载已解析筛选列的取值候选
  for (const f of cardEdit.filters) loadDimValues(f.column);
}

async function loadDimValues(column) {
  if (!cardEdit.metricCode || !column || dimValueOptions[column]) return;
  dimValueLoading[column] = true;
  try {
    const res = await askDimensionValues({ metric: cardEdit.metricCode, column });
    dimValueOptions[column] = res?.values ?? [];
  } catch {
    dimValueOptions[column] = [];
  } finally {
    dimValueLoading[column] = false;
  }
}

function onEditMetric(code) {
  if (code !== cardEdit.metricCode) {
    // 指标换了：维度候选/筛选值全部失效，清空拆解意图
    cardEdit.dimension = "";
    cardEdit.filters = [];
    Object.keys(dimValueOptions).forEach((k) => delete dimValueOptions[k]);
  }
  cardEdit.metricCode = code;
}

function onDimensionChange(column) {
  cardEdit.dimension = column;
  if (column) loadDimValues(column);
}

function addFilter() {
  const col = cardEdit.dimension || dimensionOptions.value[0]?.column || "";
  cardEdit.filters.push({ column: col, op: "=", value: "" });
  if (col) loadDimValues(col);
}

function removeFilter(i) {
  cardEdit.filters.splice(i, 1);
}

function buildPayload() {
  const base = {
    metric: cardEdit.metricCode,
    start: cardEdit.range[0],
    end: cardEdit.range[1],
    compare: cardEdit.compare,
  };
  if (cardEdit.dimension) {
    return {
      ...base,
      dimension: cardEdit.dimension,
      filters: cardEdit.filters.filter((f) => f.column && f.value !== ""),
      order_by: cardEdit.order_by,
      order: cardEdit.order,
      top_n: cardEdit.top_n ? Number(cardEdit.top_n) : null,
    };
  }
  return base;
}

async function execute() {
  if (!cardEdit.metricCode || !cardEdit.range?.[0] || !cardEdit.range?.[1]) {
    ElMessage.warning("请先在理解卡中选择指标与时间区间");
    return;
  }
  executing.value = true;
  try {
    result.value = await askExecuteApi(buildPayload());
  } finally {
    executing.value = false;
  }
}

/** 理解卡可编辑：修改后立即重算（与契约"改完立即重算"一致，此处手动触发执行） */
async function recomputeWithDashboard() {
  // 校验当前卡与看板同口径（直接调用看板同一接口，数值应一致）
  if (!cardEdit.metricCode || cardEdit.dimension) return; // 拆解模式下与单值口径不同，不提供此按钮
  const viaDash = await metricValue({
    metric: cardEdit.metricCode,
    start: cardEdit.range[0],
    end: cardEdit.range[1],
    compare: cardEdit.compare,
  });
  result.value = viaDash;
  ElMessage.success("已与看板同口径重算");
}

/** 拆解表格列（后端已按理解卡排序返回；表头可客户端再排） */
const breakdownCols = [
  { key: "value", label: "当期值" },
  { key: "change_abs", label: "绝对变化" },
  { key: "change_pct", label: "变化率 %" },
];

const metricName = computed(() => {
  const code = cardEdit.metricCode;
  return metricOptions.value.find((m) => m.code === code)?.name ?? code;
});

const isBreakdown = computed(() => result.value?.kind === "breakdown");
</script>

<template>
  <div class="page-container ask">
    <div class="page-header">
      <div>
        <h1 class="page-header__title">AI 问数</h1>
        <p class="page-header__subtitle">
          先摊开理解、再给答案 · 数值由指标中心统一计算，与看板口径完全一致
        </p>
      </div>
    </div>

    <section class="pwc-card">
      <div class="ask__input-row">
        <el-input
          v-model="question"
          size="large"
          placeholder="试着问：上个月按地区拆解销售额的环比，跌幅最厉害的前5"
          clearable
          :disabled="asking"
          @keyup.enter="submit()"
        >
          <template #append>
            <el-button type="primary" :loading="asking" @click="submit()">理解问题</el-button>
          </template>
        </el-input>
      </div>
      <div class="ask__examples">
        <el-tag
          v-for="ex in EXAMPLES"
          :key="ex"
          class="ask__example"
          type="info"
          effect="plain"
          @click="question = ex; submit(ex)"
        >
          {{ ex }}
        </el-tag>
      </div>
    </section>

    <!-- 逃生舱：意图解析不出可执行结构 → 纯对话引导（无数字） -->
    <section v-if="card && card.mode === 'help'" class="pwc-card ask__card">
      <div class="ask__card-head">
        <h4>没能理解您的问题</h4>
        <span class="pwc-badge pwc-badge--grey">
          {{ card.llm_configured ? `意图解析：${card.source === "llm" ? "LLM" : "规则兜底"}` : "未配置 LLM · 规则解析" }}
        </span>
      </div>
      <el-alert type="info" :closable="false" show-icon class="ask__amb">
        <template #title>{{ card.help_reply }}</template>
      </el-alert>
      <p class="ask__hint">
        平台当前支持：已有指标的数值查询、按维度拆解、时间环比/同比对比。点击上方示例可快速体验。
      </p>
    </section>

    <!-- 理解卡 -->
    <section v-if="card && card.mode === 'analysis'" class="pwc-card ask__card">
      <div class="ask__card-head">
        <h4>理解卡</h4>
        <span class="pwc-badge pwc-badge--grey">
          {{ card.llm_configured ? `意图解析：${card.source === "llm" ? "LLM" : "规则兜底"}` : "未配置 LLM · 规则解析" }}
        </span>
      </div>

      <template v-if="card.can_compute">
        <!-- 歧义黄提示 -->
        <el-alert
          v-for="(amb, i) in card.ambiguous"
          :key="i"
          :title="amb.reason"
          type="warning"
          :closable="false"
          show-icon
          class="ask__amb"
        >
          <template v-if="amb.options?.length" #default>
            <div class="ask__amb-opts">
              <template v-if="amb.field === 'metric'">
                <el-tag
                  v-for="opt in amb.options"
                  :key="opt.code"
                  :type="opt.code === amb.default ? 'warning' : 'info'"
                  class="ask__amb-opt"
                  @click="onEditMetric(opt.code)"
                >
                  {{ opt.name }}（{{ opt.code }}）
                </el-tag>
              </template>
              <template v-else-if="amb.field === 'dimension'">
                <el-tag
                  v-for="opt in amb.options"
                  :key="opt.column"
                  :type="opt.column === amb.default ? 'warning' : 'info'"
                  class="ask__amb-opt"
                  @click="onDimensionChange(opt.column)"
                >
                  {{ opt.column }}
                </el-tag>
              </template>
              <template v-else>
                <span v-for="opt in amb.options" :key="opt.scenario" class="ask__amb-opt">
                  「{{ opt.scenario }}」默认按 {{ opt.default }}
                </span>
              </template>
            </div>
          </template>
        </el-alert>

        <el-form label-width="90px" class="ask__form">
          <el-form-item label="指标">
            <el-select
              :model-value="cardEdit.metricCode"
              filterable
              style="width: 100%"
              @update:model-value="onEditMetric"
            >
              <el-option
                v-for="m in metricOptions"
                :key="m.code"
                :label="`${m.name}（${m.code}）`"
                :value="m.code"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="时间区间">
            <el-date-picker
              v-model="cardEdit.range"
              type="daterange"
              value-format="YYYY-MM-DD"
              range-separator="至"
              start-placeholder="开始"
              end-placeholder="结束"
              style="width: 100%"
            />
            <span v-if="!card.time_is_explicit" class="ask__hint">
              问题未指明时间，默认取上一完整自然月
            </span>
          </el-form-item>
          <el-form-item label="对比方式">
            <el-radio-group v-model="cardEdit.compare">
              <el-radio v-for="opt in compareOptions" :key="opt.value" :value="opt.value">
                {{ opt.label }}
              </el-radio>
            </el-radio-group>
          </el-form-item>

          <!-- B9.2-2 拆解维度 -->
          <el-form-item label="拆解维度">
            <el-select
              :model-value="cardEdit.dimension"
              clearable
              placeholder="不拆解（可选）"
              style="width: 100%"
              @update:model-value="onDimensionChange"
            >
              <el-option
                v-for="d in dimensionOptions"
                :key="d.column"
                :label="`${d.column}（${d.dataset} · ${d.distinct_count} 值）`"
                :value="d.column"
              />
            </el-select>
            <span v-if="!dimensionOptions.length" class="ask__hint">
              该指标暂无低基数文本列可拆解
            </span>
          </el-form-item>

          <!-- B9.2-2 维度筛选 -->
          <el-form-item v-if="cardEdit.dimension" label="维度筛选">
            <div class="ask__filters">
              <div v-for="(f, i) in cardEdit.filters" :key="i" class="ask__filter-row">
                <el-select
                  v-model="f.column"
                  placeholder="维度列"
                  style="width: 140px"
                  @change="loadDimValues(f.column)"
                >
                  <el-option
                    v-for="d in dimensionOptions"
                    :key="d.column"
                    :label="d.column"
                    :value="d.column"
                  />
                </el-select>
                <el-select v-model="f.op" style="width: 80px">
                  <el-option label="等于" value="=" />
                  <el-option label="不等于" value="!=" />
                </el-select>
                <el-select
                  v-model="f.value"
                  filterable
                  allow-create
                  default-first-option
                  :loading="dimValueLoading[f.column]"
                  placeholder="选值或输入"
                  style="width: 180px"
                >
                  <el-option v-for="v in dimValueOptions[f.column] ?? []" :key="v" :label="v" :value="v" />
                </el-select>
                <el-button text type="danger" @click="removeFilter(i)">删除</el-button>
              </div>
              <el-button text type="primary" @click="addFilter">+ 添加筛选</el-button>
            </div>
          </el-form-item>

          <!-- B9.2-2 排序 / TopN -->
          <template v-if="cardEdit.dimension">
            <el-form-item label="排序依据">
              <el-select v-model="cardEdit.order_by" style="width: 160px">
                <el-option v-for="o in orderByOptions" :key="o.value" :label="o.label" :value="o.value" />
              </el-select>
              <el-radio-group v-model="cardEdit.order" class="ask__order">
                <el-radio value="desc">降序</el-radio>
                <el-radio value="asc">升序</el-radio>
              </el-radio-group>
            </el-form-item>
            <el-form-item label="显示条数">
              <el-input-number
                v-model="cardEdit.top_n"
                :min="1"
                :max="50"
                placeholder="默认 10"
                style="width: 160px"
              />
              <span class="ask__hint">留空显示前 10 组</span>
            </el-form-item>
          </template>
        </el-form>

        <div class="ask__actions">
          <el-button type="primary" :loading="executing" @click="execute">
            {{ executing ? "计算中…" : "确认计算" }}
          </el-button>
          <el-button v-if="!cardEdit.dimension" :disabled="executing" @click="recomputeWithDashboard">
            与看板同口径重算
          </el-button>
        </div>
      </template>
      <el-alert v-else :title="card.no_metric_reason" type="warning" :closable="false" show-icon />
    </section>

    <!-- 结果：拆解表格 -->
    <section v-if="result && isBreakdown" class="pwc-card ask__result">
      <div class="ask__result-head">
        <span class="ask__result-metric">{{ result.name }} · 按「{{ result.dimension }}」拆解</span>
        <span class="pwc-badge pwc-badge--grey">
          {{ result.start }} ~ {{ result.end }}
          <template v-if="!result.period_complete && result.data_through">
            · 数据截至 {{ result.data_through }}
          </template>
          · 共 {{ result.total_groups }} 组，显示前 {{ result.rows.length }} 组
        </span>
      </div>
      <el-table :data="result.rows" size="default" class="ask__table">
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
                {{ row.change_pct >= 0 ? "↑" : "↓" }} {{ Math.abs(row.change_pct).toFixed(2) }}%
              </span>
            </template>
          </template>
        </el-table-column>
      </el-table>
      <div v-if="result.compare" class="ask__hint">
        基期：{{ result.compare.start }} ~ {{ result.compare.end }}
        <template v-if="!result.compare.period_complete">（数据截至 {{ result.compare.data_through }}）</template>
      </div>
      <p v-else class="ask__hint">未启用对比或全期常数指标（无基期概念）</p>
    </section>

    <!-- 结果：单值卡 -->
    <section v-if="result && !isBreakdown" class="pwc-card ask__result">
      <div class="ask__result-head">
        <span class="ask__result-metric">{{ metricName }}</span>
        <span class="pwc-badge pwc-badge--grey">
          {{ result.start }} ~ {{ result.end }}
          <template v-if="!result.period_complete && result.data_through">
            · 数据截至 {{ result.data_through }}
          </template>
        </span>
      </div>
      <div class="ask__result-value">{{ formatMetricValue(result.value) }}</div>
      <div v-if="changePct !== null" class="ask__result-compare">
        {{ result.compare === "yoy" ? "同比" : "环比" }}
        <span :class="changePct >= 0 ? 'up' : 'down'">
          {{ changePct >= 0 ? "↑" : "↓" }} {{ Math.abs(changePct).toFixed(2) }}%
        </span>
      </div>
      <p v-else class="ask__hint">无对比基期数据</p>
    </section>
  </div>
</template>

<style scoped>
.ask__input-row {
  margin-top: var(--pwc-space-3);
}

.ask__examples {
  display: flex;
  gap: var(--pwc-space-2);
  margin: var(--pwc-space-3) 0 var(--pwc-space-2);
  flex-wrap: wrap;
}

.ask__example {
  cursor: pointer;
}

.ask__card,
.ask__result {
  margin-top: var(--pwc-space-5);
}

.ask__card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.ask__amb {
  margin: var(--pwc-space-3) 0;
}

.ask__amb-opts {
  display: flex;
  gap: var(--pwc-space-2);
  flex-wrap: wrap;
}

.ask__amb-opt {
  cursor: pointer;
}

.ask__form {
  margin-top: var(--pwc-space-3);
}

.ask__hint {
  display: block;
  color: var(--pwc-text-secondary);
  font-size: 12px;
  margin-top: var(--pwc-space-1);
}

.ask__filters {
  width: 100%;
}

.ask__filter-row {
  display: flex;
  gap: var(--pwc-space-2);
  margin-bottom: var(--pwc-space-2);
  align-items: center;
}

.ask__order {
  margin-left: var(--pwc-space-3);
}

.ask__actions {
  display: flex;
  gap: var(--pwc-space-2);
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

.ask__result-compare .up,
.ask__table .up {
  color: var(--pwc-positive, #059669);
}

.ask__result-compare .down,
.ask__table .down {
  color: var(--pwc-danger, #dc2626);
}
</style>
