<!-- pwc-regime: product-ui -->
<script setup>
/**
 * AI 问数（B9 / B9.2-2）：先摊开理解、再给答案。
 * - 理解卡：指标/时间/对比/拆解维度/筛选/排序/TopN，全部可改，改完重算
 * - LLM 只产意图（锚定真实指标与维度候选），数值由指标中心统一计算（口径同源）
 * - 拆解结果表格（组值/基期/变化，点击表头切换排序）；单值结果卡
 * - 逃生舱（mode=help）：意图解析不出可执行结构时纯对话引导，绝不含数字
 */
import { computed, onMounted, reactive, ref } from "vue";
import { ElMessage } from "element-plus";

import {
  ask as askApi,
  askExecute as askExecuteApi,
  askDimensionValues,
  askSuggestions,
  metricValue,
} from "@/api/query";
import { useMetricStore } from "@/stores/metric";
import { useAuthStore } from "@/stores/auth";
import AskResultCard from "@/components/business/AskResultCard.vue";

const auth = useAuthStore();
const metricStore = useMetricStore();

const question = ref("");
const asking = ref(false);
const card = ref(null);
// B9.2-3：结果支持多指标并列——[{ label, data }]，data 与 ask/execute 返回同构
const results = ref([]);
const executing = ref(false);
// 问句同时命中的其他指标勾选区（B9.2-3 多指标并列）
const multiSelected = ref([]);
// 空态推荐问题（B9.2-3）：后端按可见指标生成；加载中不渲染避免先静态后推荐的跳动
const suggestions = ref([]);
const suggestionsLoading = ref(true);
// 推荐为空/接口失败时回落静态示例
const shownSuggestions = computed(() =>
  suggestions.value.length ? suggestions.value : EXAMPLES,
);
// B9.2-4 多轮会话：session_id 后端生成每轮携带；messages 记录历史轮（问句+只读结果）
const sessionId = ref(null);
const messages = ref([]); // {role:'user', text} | {role:'assistant', question, card, results}

function resetChat() {
  sessionId.value = null;
  card.value = null;
  results.value = [];
  multiSelected.value = [];
  messages.value = [];
  Object.assign(cardEdit, {
    metricCode: "", range: [], compare: "none", dimension: "",
    filters: [], order_by: "value", order: "desc", top_n: null,
  });
}

function syncResultsToLastMessage() {
  const last = messages.value[messages.value.length - 1];
  if (last?.role === "assistant") last.results = results.value;
}

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

// 维度候选标签：高基数列（>200 值，与后端 LOW_CARDINALITY_THRESHOLD 对齐）标注拆解较慢，由业务自行取舍
const dimLabel = (d) =>
  d.distinct_count > 200
    ? `${d.column}（${d.dataset} · ${d.distinct_count} 值 · 拆解较慢）`
    : `${d.column}（${d.dataset} · ${d.distinct_count} 值）`;

async function fetchSuggestions() {
  try {
    const res = await askSuggestions();
    suggestions.value = Array.isArray(res) ? res : [];
  } catch {
    suggestions.value = [];
  } finally {
    suggestionsLoading.value = false;
  }
}

async function submit(questionOverride) {
  const q = (questionOverride ?? question.value).trim();
  if (!q) {
    ElMessage.warning("请输入问题");
    return;
  }
  asking.value = true;
  results.value = [];
  messages.value.push({ role: "user", text: q });
  try {
    const res = await askApi(q, sessionId.value);
    card.value = res;
    // 多指标并列默认全选（问句本来就在问它们），用户可取消
    multiSelected.value = (res.multi_metrics ?? []).map((m) => m.code);
    applyCard(res);
    // B9.2-4：session_id 由后端生成，每轮携带实现多轮追问
    if (res.session_id) sessionId.value = res.session_id;
    messages.value.push({ role: "assistant", question: q, card: res, results: [] });
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
    // 主指标走完整理解卡参数（含拆解）；并列指标只共享时间/对比方式，
    // 逐指标独立调用同一 execute 出口——数值仍全部由后端单点计算
    const jobs = [{ payload: buildPayload(), label: metricName.value }];
    for (const m of card.value?.multi_metrics ?? []) {
      if (!multiSelected.value.includes(m.code)) continue;
      jobs.push({
        payload: {
          metric: m.code,
          start: cardEdit.range[0],
          end: cardEdit.range[1],
          compare: cardEdit.compare,
        },
        label: m.name,
      });
    }
    const datas = await Promise.all(jobs.map((j) => askExecuteApi(j.payload)));
    results.value = jobs.map((j, i) => ({ label: j.label, data: datas[i] }));
    syncResultsToLastMessage();
  } finally {
    executing.value = false;
  }
}

/** 理解卡可编辑：修改后立即重算（与契约"改完立即重算"一致，此处手动触发执行） */
async function recomputeWithDashboard() {
  // 校验当前卡与看板同口径（直接调用看板同一接口，数值应一致）
  if (!cardEdit.metricCode || cardEdit.dimension) return; // 拆解模式下与单值口径不同，不提供此按钮
  const targets = [
    { code: cardEdit.metricCode, name: metricName.value },
    ...(card.value?.multi_metrics ?? []).filter((m) => multiSelected.value.includes(m.code)),
  ];
  const datas = await Promise.all(
    targets.map((t) =>
      metricValue({
        metric: t.code,
        start: cardEdit.range[0],
        end: cardEdit.range[1],
        compare: cardEdit.compare,
      }),
    ),
  );
  results.value = targets.map((t, i) => ({ label: t.name, data: datas[i] }));
  syncResultsToLastMessage();
  ElMessage.success("已与看板同口径重算");
}

const metricName = computed(() => {
  const code = cardEdit.metricCode;
  return metricOptions.value.find((m) => m.code === code)?.name ?? code;
});

onMounted(fetchSuggestions);
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
        />
        <el-button type="danger" size="large" :loading="asking" @click="submit()">
          理解问题
        </el-button>
        <el-button v-if="messages.length" size="large" :disabled="asking" @click="resetChat">
          新话题
        </el-button>
      </div>
    </section>

    <!-- 空态推荐问题（B9.2-3）：后端按可见指标生成；加载中不渲染（避免静态示例→推荐的跳动），空/失败回落静态示例；点击即问 -->
    <section v-if="!card && !suggestionsLoading" class="pwc-card ask__card">
      <h4>试试这样问</h4>
      <div class="ask__examples">
        <el-tag
          v-for="s in shownSuggestions"
          :key="s"
          class="ask__example"
          effect="plain"
          @click="question = s; submit(s)"
        >
          {{ s }}
        </el-tag>
      </div>
    </section>

    <!-- B9.2-4 会话消息流：历史轮问句气泡 + 只读结果；当前轮在下方完整可交互 -->
    <template v-for="(msg, mi) in messages" :key="mi">
      <div v-if="msg.role === 'user'" class="ask__bubble">{{ msg.text }}</div>
      <template v-else-if="mi < messages.length - 1">
        <AskResultCard
          v-for="(r, ri) in msg.results"
          :key="ri"
          :data="r.data"
          :label="r.label"
          class="ask__history-result"
        />
        <el-alert
          v-if="msg.card?.mode === 'help'"
          type="info"
          :closable="false"
          show-icon
          class="ask__history-help"
          :title="msg.card.help_reply"
        />
        <div
          v-else-if="msg.card?.mode === 'analysis' && !msg.results.length"
          class="ask__hint ask__uncomputed"
        >
          该轮已理解（{{ msg.card.metric?.name ?? "—" }}），未计算
        </div>
      </template>
    </template>

    <!-- 逃生舱：意图解析不出可执行结构 → 纯对话引导（无数字） -->
    <section v-if="card && card.mode === 'help'" class="pwc-card ask__card">
      <div class="ask__card-head">
        <h4>没能理解您的问题</h4>
        <el-tag :type="card.llm_configured ? 'warning' : 'info'" effect="light">
          {{ card.llm_configured ? (card.source === "llm" ? "AI 语义解析" : "规则解析（AI 降级）") : "规则解析（未配置 AI）" }}
        </el-tag>
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
        <el-tag
          :type="card.source === 'llm' ? 'success' : (card.llm_configured ? 'warning' : 'info')"
          effect="light"
        >
          {{ card.source === "llm" ? "AI 语义解析" : (card.llm_configured ? "规则解析（AI 降级）" : "规则解析（未配置 AI）") }}
        </el-tag>
      </div>

      <!-- 解析来源提示：成功/降级/未配置都必须给用户明确反馈 -->
      <el-alert
        v-if="card.inherited"
        type="success"
        :closable="false"
        show-icon
        class="ask__amb"
        title="已继承上一轮的指标与口径，时间/维度等按新问题更新；如有偏差可在理解卡中修改"
      />
      <el-alert
        v-if="card.source === 'llm'"
        type="success"
        :closable="false"
        show-icon
        class="ask__amb"
        title="AI 语义解析成功，以下理解已自动填入；如有偏差可直接修改后计算"
      />
      <el-alert
        v-else-if="card.llm_configured"
        type="warning"
        :closable="false"
        show-icon
        class="ask__amb"
        :title="`AI 语义解析未成功，已用规则解析兜底${card.source_note ? '：' + card.source_note : ''}`"
      />
      <el-alert
        v-else
        type="info"
        :closable="false"
        show-icon
        class="ask__amb"
        title="当前使用规则解析（未配置 AI 模型）；配置后可理解更口语化的问法"
      />

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
              <template v-else-if="amb.field === 'disambiguation'">
                <!-- 口径分歧说明（指标责任人登记）：分歧问题 + 候选口径（默认口径标注），仅提示不阻断 -->
                <p v-if="amb.question" class="ask__dis-q">{{ amb.question }}</p>
                <div class="ask__amb-opts">
                  <el-tag
                    v-for="opt in amb.options"
                    :key="opt.name"
                    :type="opt.is_default ? 'warning' : 'info'"
                    class="ask__amb-opt ask__amb-opt--static"
                  >
                    {{ opt.name }}<template v-if="opt.is_default">（默认口径）</template>
                    <template v-if="opt.description">：{{ opt.description }}</template>
                  </el-tag>
                </div>
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

          <!-- B9.2-3 多指标并列：勾选后与主指标一并计算 -->
          <el-form-item v-if="card.multi_metrics?.length" label="同时计算">
            <div class="ask__multi">
              <el-checkbox-group v-model="multiSelected">
                <el-checkbox v-for="m in card.multi_metrics" :key="m.code" :value="m.code">
                  {{ m.name }}（{{ m.code }}）
                </el-checkbox>
              </el-checkbox-group>
              <span class="ask__hint">问句同时命中多个指标，勾选后一并出结果（各指标独立计算，口径同源）</span>
            </div>
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
            <span v-if="card.time_inherited" class="ask__hint">
              时间继承上一轮问题，可直接修改
            </span>
            <span v-else-if="!card.time_is_explicit" class="ask__hint">
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
                :label="dimLabel(d)"
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

    <!-- 结果：多指标并列渲染（B9.2-3），AskResultCard 与历史轮共用（B9.2-4 抽取） -->
    <AskResultCard
      v-for="(r, ri) in results"
      :key="ri"
      :data="r.data"
      :label="r.label"
    />
  </div>
</template>

<style scoped>
.ask__input-row {
  display: flex;
  align-items: center;
  gap: var(--pwc-space-3);
  margin-top: var(--pwc-space-3);
}

.ask__input-row .el-input {
  flex: 1;
}

/* B9.2-4 会话消息流 */
.ask__bubble {
  max-width: 72%;
  margin: var(--pwc-space-4) 0 var(--pwc-space-2) auto;
  padding: var(--pwc-space-2) var(--pwc-space-3);
  background: var(--pwc-brand, #FD5108);
  color: #fff;
  border-radius: 12px 12px 2px 12px;
  font-size: 14px;
  line-height: 1.5;
  width: fit-content;
}

.ask__history-result {
  margin-top: var(--pwc-space-3);
}

.ask__history-help {
  margin-top: var(--pwc-space-3);
}

.ask__uncomputed {
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

.ask__amb-opt--static {
  cursor: default;
}

.ask__dis-q {
  margin: 0 0 var(--pwc-space-1);
  font-weight: 600;
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

.ask__multi {
  width: 100%;
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
  color: var(--pwc-up, #D62222);
}

.ask__result-compare .down,
.ask__table .down {
  color: var(--pwc-down, #059669);
}
</style>
