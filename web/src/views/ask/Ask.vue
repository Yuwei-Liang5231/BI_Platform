<!-- pwc-regime: product-ui -->
<script setup>
/**
 * AI 问数（B9 / B9.2-2）：先摊开理解、再给答案。
 * - 理解卡：指标/时间/对比/拆解维度/筛选/排序/TopN，全部可改，改完重算
 * - LLM 只产意图（锚定真实指标与维度候选），数值由指标中心统一计算（口径同源）
 * - 拆解结果表格（组值/基期/变化，点击表头切换排序）；单值结果卡
 * - 逃生舱（mode=help）：意图解析不出可执行结构时纯对话引导，绝不含数字
 */
import { computed, nextTick, onMounted, reactive, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";

import {
  ask as askApi,
  askExecute as askExecuteApi,
  askDimensionValues,
  askSuggestions,
  askConversations,
  askConversationMessages,
  askConversationDelete,
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
// B9.2-6 会话持久化：activeConvId 标识当前对话（首问 null，后端自动建）；
// messages 记录消息流（问句+理解卡/结果）；conversations 为左侧历史列表
const activeConvId = ref(null);
const messages = ref([]); // {role:'user', text} | {role:'assistant', question, card, results, readonly?}
const conversations = ref([]);
const sidebarCollapsed = ref(false);
const scrollRef = ref(null);
// 理解卡折叠：确认计算后自动收起（结果上移可见），点「展开调整」可再改——新问题自动展开
const cardCollapsed = ref(false);

const cardSummary = computed(() => {
  if (!card.value?.metric) return "";
  const parts = [
    `${card.value.metric.name}（${card.value.metric.code}）`,
    `${cardEdit.range?.[0] ?? card.value.start} ~ ${cardEdit.range?.[1] ?? card.value.end}`,
  ];
  const cmp = compareOptions.find((o) => o.value === cardEdit.compare);
  if (cmp && cmp.value !== "none") parts.push(cmp.label.split("（")[0]);
  if (cardEdit.dimension) parts.push(`按「${cardEdit.dimension}」拆解`);
  if (cardEdit.top_n) parts.push(`前 ${cardEdit.top_n} 组`);
  return parts.join(" · ");
});

function newConversation() {
  activeConvId.value = null;
  card.value = null;
  results.value = [];
  multiSelected.value = [];
  messages.value = [];
  cardCollapsed.value = false;
  question.value = ""; // 新对话同时清空输入框
  Object.assign(cardEdit, {
    metricCode: "", range: [], compare: "none", dimension: "",
    filters: [], order_by: "value", order: "desc", top_n: null,
  });
}

async function fetchConversations() {
  try {
    const res = await askConversations();
    conversations.value = Array.isArray(res) ? res : [];
  } catch {
    conversations.value = [];
  }
}

function shortTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  const now = new Date();
  if (d.toDateString() === now.toDateString()) return hm;
  return `${d.getMonth() + 1}-${d.getDate()} ${hm}`;
}

async function openConversation(id) {
  if (asking.value || executing.value) return;
  if (id === activeConvId.value && messages.value.length) return;
  try {
    const list = await askConversationMessages(id);
    newConversation();
    activeConvId.value = id;
    messages.value = Array.isArray(list) ? list : [];
    await nextTick();
    scrollToBottom(true);
  } catch {
    /* 错误由请求拦截器统一提示（如会话已被删除） */
    fetchConversations();
  }
}

async function removeConversation(id) {
  try {
    await ElMessageBox.confirm("删除后该对话的历史记录不可恢复，确定删除？", "删除对话", {
      type: "warning",
      confirmButtonText: "删除",
      cancelButtonText: "取消",
    });
  } catch {
    return;
  }
  await askConversationDelete(id);
  if (activeConvId.value === id) newConversation();
  ElMessage.success("已删除");
  fetchConversations();
}

function scrollToBottom(instant = false) {
  const el = scrollRef.value;
  if (!el) return;
  el.scrollTo({ top: el.scrollHeight, behavior: instant ? "auto" : "smooth" });
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
  question.value = ""; // 问题发出即清空输入框（问句已入会话气泡）
  results.value = [];
  messages.value.push({ role: "user", text: q });
  try {
    const res = await askApi(q, activeConvId.value);
    card.value = res;
    // 多指标并列默认全选（问句本来就在问它们），用户可取消
    multiSelected.value = (res.multi_metrics ?? []).map((m) => m.code);
    applyCard(res);
    // B9.2-6：首问由后端建会话，理解卡返回 conversation_id 供后续轮携带
    if (res.conversation_id) activeConvId.value = res.conversation_id;
    messages.value.push({ role: "assistant", question: q, card: res, results: [] });
    cardCollapsed.value = false; // 新一轮理解卡默认展开
    await nextTick();
    scrollToBottom();
    fetchConversations(); // 首问生成标题 / 追问刷新最近使用排序
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
    // 逐指标独立调用同一 execute 出口——数值仍全部由后端单点计算；
    // conversation_id 让后端把结果快照回写当前会话（历史恢复可看当时真值）
    const convId = activeConvId.value ?? null;
    const jobs = [{ payload: { ...buildPayload(), conversation_id: convId }, label: metricName.value }];
    for (const m of card.value?.multi_metrics ?? []) {
      if (!multiSelected.value.includes(m.code)) continue;
      jobs.push({
        payload: {
          metric: m.code,
          start: cardEdit.range[0],
          end: cardEdit.range[1],
          compare: cardEdit.compare,
          conversation_id: convId,
        },
        label: m.name,
      });
    }
    const datas = await Promise.all(jobs.map((j) => askExecuteApi(j.payload)));
    results.value = jobs.map((j, i) => ({ label: j.label, data: datas[i] }));
    syncResultsToLastMessage();
    // 计算完成：理解卡自动收起（可展开再调），滚动到最新结果；结果快照已由后端回写会话
    cardCollapsed.value = true;
    await nextTick();
    scrollToBottom();
    fetchConversations();
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

onMounted(() => {
  fetchSuggestions();
  fetchConversations();
});
</script>

<template>
  <div class="ask">
    <!-- 左侧：历史会话列表（B9.2-6 持久化，按最近使用排序，可折叠） -->
    <aside v-show="!sidebarCollapsed" class="ask__sidebar">
      <div class="ask__sidebar-head">
        <el-button type="primary" class="ask__new-btn" @click="newConversation">＋ 新对话</el-button>
        <el-button class="ask__collapse-btn" title="收起列表" @click="sidebarCollapsed = true">
          ‹ 收起
        </el-button>
      </div>
      <div class="ask__conv-list">
        <div
          v-for="c in conversations"
          :key="c.id"
          class="ask__conv-item"
          :class="{ 'is-active': c.id === activeConvId }"
          @click="openConversation(c.id)"
        >
          <span class="ask__conv-title" :title="c.title">{{ c.title }}</span>
          <span class="ask__conv-meta">
            <span>{{ shortTime(c.updated_at) }}</span>
            <span class="ask__conv-del" title="删除对话" @click.stop="removeConversation(c.id)">✕</span>
          </span>
        </div>
        <p v-if="!conversations.length" class="ask__hint ask__conv-empty">
          这里将展示你的历史对话
        </p>
      </div>
    </aside>
    <el-button
      v-if="sidebarCollapsed"
      class="ask__sidebar-expand"
      title="展开历史列表"
      @click="sidebarCollapsed = false"
    >» 历史</el-button>

    <!-- 右侧：消息流（滚动）+ 底部固定输入 -->
    <main class="ask__main">
      <header class="ask__pagehead">
        <h1>AI 问数</h1>
        <p>先摊开理解、再给答案 · 数值由指标中心统一计算，与看板口径完全一致</p>
      </header>

      <div ref="scrollRef" class="ask__scroll">
        <!-- 空态欢迎 + 推荐问题（B9.2-3）：后端按可见指标生成；点击即问 -->
        <div v-if="!card && !messages.length && !suggestionsLoading" class="ask__welcome">
          <h2>问我任何指标问题</h2>
          <p class="ask__hint">支持数值查询、按维度拆解、环比/同比对比；可连续追问（如「那上个月呢」）</p>
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
        </div>

        <!-- 会话消息流：历史轮问句气泡 + 只读结果；当前轮在下方完整可交互 -->
        <template v-for="(msg, mi) in messages" :key="mi">
          <div v-if="msg.role === 'user'" class="ask__bubble">{{ msg.text }}</div>
          <template v-else-if="msg.readonly || mi < messages.length - 1">
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

        <!-- 理解卡：计算后自动收起，仅保留摘要行；点「展开调整」恢复完整表单 -->
        <section v-if="card && card.mode === 'analysis'" class="pwc-card ask__card">
      <div class="ask__card-head">
        <h4>理解卡</h4>
        <div class="ask__card-head-right">
          <el-tag
            :type="card.source === 'llm' ? 'success' : (card.llm_configured ? 'warning' : 'info')"
            effect="light"
          >
            {{ card.source === "llm" ? "AI 语义解析" : (card.llm_configured ? "规则解析（AI 降级）" : "规则解析（未配置 AI）") }}
          </el-tag>
          <el-button v-if="results.length" text type="primary" @click="cardCollapsed = !cardCollapsed">
            {{ cardCollapsed ? "展开调整" : "收起" }}
          </el-button>
        </div>
      </div>
      <div v-if="cardCollapsed" class="ask__card-summary" @click="cardCollapsed = false">
        <span class="ask__card-summary-arrow">▾</span>
        <span>{{ cardSummary }}</span>
        <span class="ask__card-summary-hint">已按以上口径计算，点击展开调整</span>
      </div>

      <template v-if="!cardCollapsed">
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

      <!-- 底部固定输入条：会话滚动到任何位置都能直接提问 -->
      <div class="ask__composer">
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
          <el-button v-if="messages.length" size="large" :disabled="asking" @click="newConversation">
            新对话
          </el-button>
        </div>
      </div>
    </main>
  </div>
</template>

<style scoped>
/* 三栏会话布局（B9.2-6）：左侧历史列表 + 右侧消息流 + 底部固定输入（72px = 顶部导航栏高度） */
.ask {
  display: flex;
  gap: var(--pwc-space-3);
  height: calc(100vh - 72px);
  padding: var(--pwc-space-4);
  box-sizing: border-box;
  overflow: hidden;
}

/* 左侧历史会话列表 */
.ask__sidebar {
  width: 236px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  background: var(--pwc-surface, #fff);
  border: 1px solid var(--pwc-border, rgba(0, 0, 0, 0.08));
  border-radius: 12px;
  overflow: hidden;
}

.ask__sidebar-head {
  display: flex;
  align-items: center;
  gap: var(--pwc-space-1);
  padding: var(--pwc-space-3);
  border-bottom: 1px solid var(--pwc-border, rgba(0, 0, 0, 0.06));
}

.ask__new-btn {
  flex: 1;
}

/* 收起/展开列表按钮：明显可点（用户反馈小到像圆点） */
.ask__collapse-btn {
  padding: 8px 10px;
  border: 1px solid var(--pwc-border, rgba(0, 0, 0, 0.15));
  border-radius: 8px;
  color: var(--pwc-text-secondary);
}

.ask__sidebar-expand {
  align-self: flex-start;
  margin: var(--pwc-space-2) 0 0;
  padding: 10px 12px;
  border: 1px solid var(--pwc-border, rgba(0, 0, 0, 0.15));
  border-radius: 8px;
  color: var(--pwc-text-secondary);
}

.ask__conv-list {
  flex: 1;
  overflow-y: auto;
  padding: var(--pwc-space-2);
}

.ask__conv-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: var(--pwc-space-2) var(--pwc-space-2);
  border-radius: 8px;
  cursor: pointer;
  margin-bottom: 2px;
}

.ask__conv-item:hover {
  background: var(--pwc-state-container-hover, rgba(0, 0, 0, 0.05));
}

.ask__conv-item.is-active {
  background: rgba(253, 81, 8, 0.1);
}

.ask__conv-title {
  font-size: 13px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.ask__conv-meta {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 11px;
  color: var(--pwc-text-secondary);
}

.ask__conv-del {
  visibility: hidden;
  cursor: pointer;
  padding: 0 2px;
}

.ask__conv-del:hover {
  color: var(--pwc-danger, #D62222);
}

.ask__conv-item:hover .ask__conv-del {
  visibility: visible;
}

.ask__conv-empty {
  padding: var(--pwc-space-3);
}

/* 右侧主区：页头 + 滚动消息流 + 底部输入条 */
.ask__main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.ask__pagehead {
  padding: 0 var(--pwc-space-2) var(--pwc-space-2);
}

.ask__pagehead h1 {
  margin: 0;
  font-size: 22px;
  font-weight: 700;
}

.ask__pagehead p {
  margin: 4px 0 0;
  color: var(--pwc-text-secondary);
  font-size: 13px;
}

.ask__scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: var(--pwc-space-2) var(--pwc-space-2) var(--pwc-space-3);
}

/* 空态欢迎屏 */
.ask__welcome {
  text-align: center;
  padding: 12vh var(--pwc-space-4) 0;
}

.ask__welcome h2 {
  margin: 0 0 var(--pwc-space-2);
  font-size: 26px;
  font-weight: 700;
}

/* 空态推荐问题：两列网格卡片，完整展示问句（用户反馈挤成一行不合理） */
.ask__welcome .ask__examples {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--pwc-space-2);
  max-width: 680px;
  margin: var(--pwc-space-4) auto 0;
}

.ask__welcome .ask__example {
  height: auto;
  white-space: normal;
  text-align: left;
  padding: 10px 14px;
  border-radius: 10px;
  line-height: 1.5;
  font-size: 13px;
}

.ask__welcome .ask__example::before {
  content: "↗ ";
  opacity: 0.6;
}

/* 底部固定输入条 */
.ask__composer {
  margin-top: var(--pwc-space-3);
  padding: var(--pwc-space-3);
  background: var(--pwc-surface, #fff);
  border: 1px solid var(--pwc-border, rgba(0, 0, 0, 0.06));
  border-radius: 12px;
  box-shadow: 0 -2px 12px rgba(0, 0, 0, 0.05);
}

.ask__input-row {
  display: flex;
  align-items: center;
  gap: var(--pwc-space-3);
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

.ask__card-head-right {
  display: flex;
  align-items: center;
  gap: var(--pwc-space-2);
}

/* 折叠态摘要行：计算口径一目了然，点击展开 */
.ask__card-summary {
  display: flex;
  align-items: center;
  gap: var(--pwc-space-2);
  margin-top: var(--pwc-space-3);
  padding: var(--pwc-space-2) var(--pwc-space-3);
  background: var(--pwc-state-container-hover, rgba(0, 0, 0, 0.04));
  border-radius: 8px;
  font-size: 13px;
  cursor: pointer;
  flex-wrap: wrap;
}

.ask__card-summary:hover {
  background: var(--pwc-state-container-pressed, rgba(0, 0, 0, 0.08));
}

.ask__card-summary-arrow {
  color: var(--pwc-text-secondary);
}

.ask__card-summary-hint {
  margin-left: auto;
  color: var(--pwc-text-secondary);
  font-size: 12px;
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
