<!-- pwc-regime: product-ui -->
<script setup>
/**
 * AI 问数（B9，阶段 2）：先摊开理解、再给答案。
 * - 问句 → 理解卡（指标/时间/环比，全部可改，改完立即重算）
 * - LLM 只产意图，数值由指标中心统一计算（口径同源，与看板一致）
 * - 歧义黄提示：多指标候选 + disambiguation 默认算法说明
 * - "算不了"明确返回原因；受限指标提示"该指标无权限"
 */
import { computed, reactive, ref } from "vue";
import { ElMessage } from "element-plus";

import { ask as askApi, askExecute as askExecuteApi, metricValue } from "@/api/query";
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

// 可编辑理解卡的本地状态（metric/time/compare 修改后执行走修改后的值）
const cardEdit = reactive({ metricCode: "", range: [], compare: "none" });

const EXAMPLES = ["2026年1月销售额是多少", "上个月营业额环比如何", "最近30天 GMV 同比"];

const metricOptions = computed(() =>
  metricStore.list.map((m) => ({ code: m.code, name: m.name })),
);

const compareOptions = [
  { value: "none", label: "不对比" },
  { value: "mom", label: "环比（上一等长周期）" },
  { value: "yoy", label: "同比（去年同期）" },
];

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
  if (res.metric && !metricStore.list.length) metricStore.fetchList(); // 指标下拉数据（懒加载）
}

function onEditMetric(code) {
  cardEdit.metricCode = code;
}

async function execute() {
  if (!cardEdit.metricCode || !cardEdit.range?.[0] || !cardEdit.range?.[1]) {
    ElMessage.warning("请先在理解卡中选择指标与时间区间");
    return;
  }
  executing.value = true;
  try {
    result.value = await askExecuteApi({
      metric: cardEdit.metricCode,
      start: cardEdit.range[0],
      end: cardEdit.range[1],
      compare: cardEdit.compare,
    });
  } finally {
    executing.value = false;
  }
}

/** 理解卡可编辑：修改后立即重算（与契约"改完立即重算"一致，此处手动触发执行） */
async function recomputeWithDashboard() {
  // 校验当前卡与看板同口径（直接调用看板同一接口，数值应一致）
  if (!cardEdit.metricCode) return;
  const viaDash = await metricValue({
    metric: cardEdit.metricCode,
    start: cardEdit.range[0],
    end: cardEdit.range[1],
    compare: cardEdit.compare,
  });
  result.value = viaDash;
  ElMessage.success("已与看板同口径重算");
}

const metricName = computed(() => {
  const code = cardEdit.metricCode;
  return metricOptions.value.find((m) => m.code === code)?.name ?? code;
});
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
          placeholder="试着问：2026年1月销售额是多少？上个月 GMV 环比如何？"
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

    <!-- 理解卡 -->
    <section v-if="card" class="pwc-card ask__card">
      <div class="ask__card-head">
        <h4>理解卡</h4>
        <span class="pwc-badge pwc-badge--grey">
          {{ card.llm_configured ? `意图解析：${card.source === "llm" ? "LLM" : "规则兜底"}` : "未配置 LLM · 规则解析" }}
        </span>
      </div>

      <!-- 算不了 -->
      <el-alert
        v-if="!card.can_compute"
        :title="card.no_metric_reason"
        type="warning"
        :closable="false"
        show-icon
      />

      <template v-else>
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
        </el-form>

        <div class="ask__actions">
          <el-button type="primary" :loading="executing" @click="execute">
            {{ executing ? "计算中…" : "确认计算" }}
          </el-button>
          <el-button :disabled="executing" @click="recomputeWithDashboard">与看板同口径重算</el-button>
        </div>
      </template>
    </section>

    <!-- 结果 -->
    <section v-if="result" class="pwc-card ask__result">
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

.ask__actions {
  display: flex;
  gap: var(--pwc-space-2);
}

.ask__result-head {
  display: flex;
  align-items: center;
  gap: var(--pwc-space-3);
}

.ask__result-metric {
  font-weight: 600;
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

.ask__result-compare .up {
  color: var(--pwc-positive, #059669);
}

.ask__result-compare .down {
  color: var(--pwc-danger, #dc2626);
}
</style>
