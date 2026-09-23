<!-- pwc-regime: product-ui -->
<script setup>
/**
 * 指标详情：口径说明（为什么这样定/易错点）、当前值与日序列趋势（export CSV 解析）、
 * 变更历史、折叠区展示存档 SQL。
 */
import { computed, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import * as echarts from "echarts";

import { exportCsvBlob, attributeTreeNode } from "@/api/query";
import { attributeInterpretation } from "@/api/ai";
import { anomalyHypothesis } from "@/api/ai";
import TrendBadge from "@/components/business/TrendBadge.vue";
import AiFeedback from "@/components/business/AiFeedback.vue";
import TermTip from "@/components/glossary/TermTip.vue";
import { ElMessage } from "element-plus";
import { formatMetricValue } from "@/utils/format";
import { usePeriodRange } from "@/composables/usePeriodRange";
import { useMetricStore } from "@/stores/metric";
import { useAuthStore } from "@/stores/auth";
import { useQueryStore } from "@/stores/query";

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();
const metricStore = useMetricStore();
const queryStore = useQueryStore();

// 可写角色（analyst/admin）才提供「编辑指标」深链（指标管理页仅可写角色可进）
const canManage = computed(() => auth.canWrite);

// 跳转指标管理页并自动弹出编辑对话框（?edit={id}，由管理页消费）
function goEditMetric() {
  router.push({ path: "/metrics/admin", query: { edit: metricId.value } });
}

const metricId = computed(() => route.params.id);
const metric = computed(() => metricStore.detail);
const current = ref(null);
const trendRows = ref([]);
// 全期常数指标（未绑定时间字段）：无逐日序列，趋势区显示说明而非折线
const isConstant = computed(() => current.value?.constant === true);
const sqlExpanded = ref(false);
const sqlText = ref("");
// 统计周期：默认上一自然月，可自定义
const { dateRange, range } = usePeriodRange();

// 口径分歧选项归一化：兼容 {name, description} / {label, value} / 字符串
const disambiguationOptions = computed(() => {
  const dis = metric.value?.disambiguation;
  if (!dis) return [];
  const def = dis.default;
  return (dis.options ?? []).map((raw) => {
    const opt = typeof raw === "string" ? { name: raw } : raw;
    const value = opt.value ?? opt.name ?? opt.label ?? "";
    return {
      name: opt.name ?? opt.label ?? String(raw),
      description: opt.description ?? (typeof raw === "object" && opt.label && opt.value && opt.value !== opt.label ? `口径取值：${opt.value}` : ""),
      isDefault: def !== undefined && value === def,
    };
  });
});

// 11.9 P2-1 口径结构化说明：三个字段任一非空即展示
const hasCalcNotes = computed(() => {
  const n = metric.value?.calc_notes;
  return !!(n && (n.rationale || n.alternatives || n.pitfalls));
});

// 区间变更时重取当前值与日序列（首次加载由 loadAll 负责）
watch(dateRange, () => {
  if (!metricId.value) return;
  loadCurrent();
  loadTrend();
  loadAiHypothesis();
  // 归因树与统计周期同源：区间变更后按当前层级重取（路径保留，节点值随区间变）
  if (treeDims.value.length >= 2) loadTreeNode();
});

/* ── 归因下钻（B14）：维度层级树逐层拆贡献 ──
   层级来自指标 dimensions 配置（可自选 2~4 层）；行点击逐层下钻，
   面包屑回退；贡献合计 ≈ 节点变化（守恒偏差如实展示）。
   P1 跨表维度：{dataset, column} 对象归一为 `表名.列名` 限定名传给编译器，
   展示时仅显示列名（来源表以 tooltip/括号标注）。 */
const dimCandidates = computed(() => {
  const raw = metric.value?.dimensions ?? [];
  const vals = raw
    .map((d) => (typeof d === "string" ? d : d?.dataset && d?.column ? `${d.dataset}.${d.column}` : (d?.column ?? "")))
    .filter(Boolean);
  return [...new Set(vals)];
});
const dimLabel = (d) => (d.includes(".") && !metric.value?.dimensions?.some((x) => typeof x === "string" && x === d)
  ? d.slice(d.indexOf(".") + 1)
  : d);
const treeDims = ref([]);
const treePath = ref([]); // [{dimension, value}]
const treeNode = ref(null);
const treeLoading = ref(false);

function addTreeDim(col) {
  if (!col || treeDims.value.includes(col) || treeDims.value.length >= 4) return;
  treeDims.value = [...treeDims.value, col];
  treeNode.value = null;
  treePath.value = [];
}

function removeTreeDim(idx) {
  treeDims.value = treeDims.value.filter((_, i) => i !== idx);
  treeNode.value = null;
  treePath.value = [];
}

async function loadTreeNode(path = treePath.value) {
  if (treeDims.value.length < 2) {
    ElMessage.warning("请先选择至少 2 个维度层级");
    return;
  }
  treeLoading.value = true;
  try {
    treeNode.value = await attributeTreeNode({
      metric: metricId.value,
      start: range.value.start,
      end: range.value.end,
      dimensions: treeDims.value,
      path,
      compare: "mom",
      // 拆解出口上限 50 组：全量展示（不再默认只看 Top10），超出部分并入「（其他）」
      top_n: 50,
    });
    treePath.value = path;
    // P2 #6：根节点生成一句 AI 解读（下钻子层不展示；失败静默——下钻结果照常）
    if (!path.length) {
      loadAttrInterpretation();
    } else {
      attrInterp.value = null;
      attrInterpHint.value = "";
    }
  } finally {
    treeLoading.value = false;
  }
}

const attrInterp = ref(null);
const attrInterpLoading = ref(false);
const attrInterpHint = ref(""); // 降级提示（成功时清空；no_groups 时保持空 → 整块隐藏）

async function loadAttrInterpretation() {
  attrInterpLoading.value = true;
  attrInterpHint.value = "";
  try {
    const res = await attributeInterpretation({
      metricId: metricId.value,
      start: range.value.start,
      end: range.value.end,
      dimensions: treeDims.value,
      compare: "mom",
    });
    attrInterp.value = res?.interpretation ?? null;
    if (!attrInterp.value && res?.reason !== "no_groups") {
      // 明确告知降级原因，不再无声消失
      attrInterpHint.value =
        res?.reason === "llm_not_configured"
          ? "未配置 AI 模型，暂无法生成解读（管理员可在「模型管理」页配置）"
          : "解读生成失败：模型暂不可达或输出未通过安全审计";
    }
  } catch {
    attrInterp.value = null; // 零阻塞：解读失败不影响归因树展示
    attrInterpHint.value = "解读生成失败：请求超时或服务异常";
  } finally {
    attrInterpLoading.value = false;
  }
}

function drillChild(row) {
  if (!treeNode.value?.has_next || row.value === "（其他）") return;
  loadTreeNode([...treePath.value, { dimension: treeNode.value.children_dimension, value: row.value }]);
}

function jumpTo(idx) {
  loadTreeNode(treePath.value.slice(0, idx));
}

function fmtSigned(v) {
  if (v == null) return "—";
  const n = Number(v);
  return `${n > 0 ? "▲ +" : n < 0 ? "▼ " : ""}${formatMetricValue(n)}`;
}

const attrNodeLabel = computed(() => {
  if (!treeNode.value) return "";
  const pathDesc = treeNode.value.path.map((s) => s.value).join(" · ");
  return `${dimLabel(treeNode.value.children_dimension)}（${pathDesc || "全部"}）`;
});

/* ── 变更历史：后端存 before/after 快照，前端生成可读摘要 ──
   后端只对口径变更强制填写 reason；别名/说明等基础信息编辑不强制，
   原因列显示占位符，变更内容由 before/after 字段级 diff 自动生成。 */
const CHANGE_FIELD_LABELS = {
  name: "名称",
  aliases: "别名",
  definition: "口径说明",
  calc_rule: "计算规则（口径）",
  topic: "主题",
  parent_id: "父级指标",
  disambiguation: "维度消歧",
  owner_department: "归属部门",
  status: "状态",
};

function fmtChangeVal(v) {
  if (v === undefined) return "（无）";
  if (v === null || v === "") return "（空）";
  const s = typeof v === "string" ? v : JSON.stringify(v);
  return s.length > 26 ? `${s.slice(0, 26)}…` : s;
}

function changeSummary(row) {
  const before = row?.before ?? {};
  const after = row?.after ?? {};
  const keys = Object.keys(CHANGE_FIELD_LABELS).filter(
    (k) => JSON.stringify(after[k] ?? null) !== JSON.stringify(before[k] ?? null),
  );
  if (!keys.length) return "（元数据变更，无业务字段差异）";
  return keys
    .map((k) => {
      const label = CHANGE_FIELD_LABELS[k];
      if (k === "calc_rule") {
        return `计算规则（口径）变更${after.ver ? `（版本 → v${after.ver}）` : ""}`;
      }
      return `${label}：${fmtChangeVal(before[k])} → ${fmtChangeVal(after[k])}`;
    })
    .join("；");
}

const notFound = ref(false);

// P1 功能2：AI 异动假设解释（检测为异动时展示；LLM 未配置/失败时降级为固定动作提示）
const aiHypothesis = reactive({
  loading: false,
  loaded: false,
  hasAnomaly: false,
  hypothesis: [],
  fallbackActionHint: "",
  source: "",
});

async function loadAiHypothesis() {
  if (!metricId.value) return;
  aiHypothesis.loading = true;
  try {
    const res = await anomalyHypothesis({
      metricId: metricId.value,
      start: range.value.start,
      end: range.value.end,
      compare: "mom",
    });
    aiHypothesis.loaded = true;
    aiHypothesis.hasAnomaly = res.has_anomaly;
    aiHypothesis.hypothesis = res.hypothesis ?? [];
    aiHypothesis.fallbackActionHint = res.fallback_action_hint ?? "";
    aiHypothesis.source = res.source;
  } catch {
    // 拦截器已提示；异动假设失败不阻塞详情页其余功能
  } finally {
    aiHypothesis.loading = false;
  }
}

async function loadAll() {
  notFound.value = false;
  try {
    await metricStore.fetchDetail(metricId.value);
  } catch {
    // 技术债清理（B7 登记）：指标已删除时清掉残留详情并给出明确空态，
    // 不再带着上一指标的详情继续渲染
    notFound.value = true;
    metricStore.detail = null;
    return;
  }
  await Promise.all([
    loadCurrent(),
    loadTrend(),
    loadSql(),
    metricStore.fetchChanges(metricId.value),
    loadAiHypothesis(),
  ]);
}

// 请求序号守卫：快速改区间时旧响应后到会覆盖新状态，过期响应一律丢弃。
// 当前值与趋势各自独立计数——两者并发发出，不能互相作废
let currentSeq = 0;
let trendSeq = 0;

async function loadCurrent() {
  const seq = ++currentSeq;
  try {
    const res = await queryStore.fetchValue({
      metric: metricId.value,
      start: range.value.start,
      end: range.value.end,
      compare: "mom",
    });
    if (seq !== currentSeq) return;
    // change_pct（百分数）→ TrendBadge 需要的小数变化率
    current.value = {
      ...res,
      change:
        res.compare?.change_pct !== null && res.compare?.change_pct !== undefined
          ? res.compare.change_pct / 100
          : null,
    };
  } catch {
    if (seq !== currentSeq) return;
    current.value = null;
  }
}

async function loadTrend() {
  const seq = ++trendSeq;
  let rows = [];
  try {
    const response = await exportCsvBlob({
      metric: metricId.value,
      start: range.value.start,
      end: range.value.end,
    });
    const text = await response.data.text();
    const lines = text.replace(/^\uFEFF/, "").trim().split(/\r?\n/);
    for (const line of lines.slice(1)) {
      const [date, value] = line.split(",");
      // 只判 date：无值日保留 null 占位，保证 x 轴连续（丢行会让日期轴断裂、
      // 「数据截至」截止线错位，且与看板页解析口径不一致）
      if (date) rows.push({ date, value: value === "" || value === undefined ? null : Number(value) });
    }
  } catch {
    rows = [];
  }
  if (seq !== trendSeq) return;
  trendRows.value = rows;
  rerender();
}

async function loadSql() {
  try {
    const archive = await metricStore.fetchSql(metricId.value);
    sqlText.value = typeof archive === "string" ? archive : (archive?.sql ?? JSON.stringify(archive, null, 2));
  } catch {
    sqlText.value = "";
  }
}

// ECharts 趋势（详情页大图）
const chartEl = ref(null);
let chart = null;

function renderChart() {
  if (!chartEl.value || !trendRows.value.length) return;
  // 容器是 v-if 按需挂载：DOM 被销毁重建后旧实例会失联——检测到即重建
  if (chart && chart.getDom() !== chartEl.value) {
    chart.dispose();
    chart = null;
  }
  if (!chart) chart = echarts.init(chartEl.value);
  const dates = trendRows.value.map((r) => r.date);
  const values = trendRows.value.map((r) => r.value);
  // 尾部无数据的日期段画「数据截至」截止线——与当前值卡的「区间无数据」互相印证
  const lastIdx = values.reduce((acc, v, i) => (v !== null && v !== undefined ? i : acc), -1);
  const cutoffMark =
    lastIdx >= 0 && lastIdx < dates.length - 1
      ? {
          silent: true,
          symbol: "none",
          data: [{ xAxis: dates[lastIdx] }],
          lineStyle: { color: "#B4BCC2", type: "dashed" },
          label: {
            formatter: `数据截至 ${dates[lastIdx].slice(5)}`,
            position: "end",
            color: "#7A838A",
            fontSize: 11,
          },
        }
      : undefined;
  chart.setOption({
    grid: { left: 64, right: 24, top: 24, bottom: 32 },
    tooltip: { trigger: "axis" },
    xAxis: {
      type: "category",
      data: dates,
      axisLine: { lineStyle: { color: "#DFE3E6" } },
    },
    yAxis: {
      type: "value",
      splitLine: { lineStyle: { color: "#F5F7F8" } },
    },
    series: [
      {
        type: "line",
        data: values,
        symbol: "circle",
        symbolSize: 5,
        itemStyle: { color: "#FD5108" },
        lineStyle: { color: "#FE7C39", width: 2 },
        connectNulls: false,
        // setOption 是合并模式：无截止线时必须显式清空，否则上一个指标的标记会残留
        markLine: cutoffMark ?? { silent: true, symbol: "none", data: [] },
      },
    ],
  });
}

function rerender() {
  requestAnimationFrame(renderChart);
}

onMounted(async () => {
  await loadAll();
  rerender();
});
</script>

<template>
  <div class="page-container">
    <div class="page-header">
      <div>
        <h1 class="page-header__title">{{ metric?.name ?? "指标详情" }}</h1>
        <p class="page-header__subtitle">
          <span class="pwc-badge pwc-badge--grey">{{ metric?.code }}</span>
          <span v-if="metric?.aliases?.length" class="detail__aliases">
            别名：{{ metric.aliases.join(" / ") }}
          </span>
        </p>
      </div>
      <div class="page-header__actions">
        <el-button @click="router.push('/metrics')">返回目录</el-button>
        <el-button type="primary" @click="router.push('/dashboard')">去看板</el-button>
      </div>
    </div>

    <el-empty v-if="notFound" description="该指标不存在或已被删除（可能已在指标目录中删除）">
      <el-button type="primary" @click="router.push('/metrics')">返回指标目录</el-button>
    </el-empty>

    <div v-else v-loading="metricStore.loading">
      <div class="grid-12">
        <!-- 当前值卡 -->
        <section class="pwc-card col-span-4">
          <div class="pwc-card__header">
            <h4>当前值（{{ range.start }} ~ {{ range.end }}）</h4>
          </div>
          <el-date-picker
            v-model="dateRange"
            type="daterange"
            value-format="YYYY-MM-DD"
            range-separator="~"
            start-placeholder="开始日期"
            end-placeholder="结束日期"
            :clearable="false"
            style="width: 100%; margin-bottom: 8px"
          />
          <p class="detail__value">
            {{ current?.value == null ? "—" : formatMetricValue(current.value) }}
          </p>
          <!-- 契约 v2：部分周期显示真实值 + 数据截至标注；区间零数据才提示无数据 -->
          <p v-if="current?.maturity_pending" class="metric-empty">
            观察期未满（需 {{ current.maturity_days }} 天）：该指标要等区间结束后 {{ current.maturity_days }} 天才能定型，暂不下结论
          </p>
          <p v-else-if="current && current.value == null" class="metric-empty">
            所选区间无数据<template
              v-if="current.coverage?.start && current.coverage?.end"
            >（数据覆盖：{{ current.coverage.start }} ~ {{ current.coverage.end }}，可调整上方统计周期）</template>
          </p>
          <p v-else-if="current && current.period_complete === false && current.data_through" class="metric-empty">
            统计周期未完整：仅统计 {{ current.data_through }} 之前的数据，环比已按同长度区间对齐
          </p>
          <TrendBadge v-if="current" :change="current.change ?? null" />
        </section>

        <!-- 口径说明 -->
        <section class="pwc-card col-span-8">
          <div class="pwc-card__header">
            <h4>口径说明</h4>
          </div>
          <p class="detail__definition">{{ metric?.definition || "（未填写口径说明）" }}</p>
          <template v-if="metric?.disambiguation">
            <h5 class="detail__subhead">为什么这样定 / 易错点</h5>
            <p>{{ metric.disambiguation.question }}</p>
            <ul class="detail__options">
              <li v-for="(opt, i) in disambiguationOptions" :key="i">
                <strong>{{ opt.name }}</strong>
                <span v-if="opt.description">：{{ opt.description }}</span>
                <span v-if="opt.isDefault" class="pwc-badge">当前默认口径</span>
              </li>
            </ul>
          </template>
          <!-- 11.9 P2-1 口径结构化说明 -->
          <template v-if="hasCalcNotes">
            <h5 class="detail__subhead">口径备注</h5>
            <ul class="detail__options">
              <li v-if="metric.calc_notes.rationale"><strong>为什么这样定：</strong>{{ metric.calc_notes.rationale }}</li>
              <li v-if="metric.calc_notes.alternatives"><strong>其他算法适用场景：</strong>{{ metric.calc_notes.alternatives }}</li>
              <li v-if="metric.calc_notes.pitfalls"><strong>最容易算错的地方：</strong>{{ metric.calc_notes.pitfalls }}</li>
            </ul>
          </template>
        </section>

        <!-- P1 功能2：AI 异动假设解释 -->
        <section class="pwc-card col-span-12">
          <div class="pwc-card__header">
            <h4>
              AI 异动假设
              <TermTip term="ai_hypothesis" />
              <el-tag v-if="aiHypothesis.source === 'llm'" type="success" effect="light" size="small">AI 生成</el-tag>
              <el-tag v-else-if="aiHypothesis.source === 'rule'" type="info" effect="light" size="small">规则提示</el-tag>
            </h4>
          </div>
          <template v-if="aiHypothesis.loaded">
            <template v-if="aiHypothesis.hasAnomaly">
              <p v-if="aiHypothesis.hypothesis.length" class="detail__ai-hyp">
                <span v-for="(s, j) in aiHypothesis.hypothesis" :key="j" class="detail__ai-line">{{ s }}</span>
                <AiFeedback
                  v-if="aiHypothesis.source === 'llm'"
                  kind="anomaly_hypothesis"
                  :target="`metric:${metricId}`"
                />
              </p>
              <p v-else class="detail__ai-rule">{{ aiHypothesis.fallbackActionHint }}</p>
            </template>
            <p v-else class="metric-empty">当前区间未检测到异动（指标正常），无需假设解释。</p>
          </template>
          <p v-else-if="aiHypothesis.loading" class="metric-empty">AI 正在分析异动…</p>
          <p v-else class="metric-empty">—</p>
        </section>

        <!-- 日序列趋势 -->
        <section class="pwc-card col-span-12">
          <div class="pwc-card__header">
            <h4>
              日序列趋势（{{ range.start }} ~ {{ range.end }}）
              <span v-if="isConstant" class="pwc-badge pwc-badge--grey"
                title="全期常数指标：未绑定时间字段，不按时间过滤，任意统计区间返回同一全期汇总值">全期值</span>
            </h4>
          </div>
          <!-- 全期常数指标：与统计区间无关，无逐日序列（与统一看板口径一致） -->
          <p v-if="isConstant" class="metric-empty">
            该指标为「全期常数」（未绑定时间字段）：不按时间过滤，任意统计区间返回同一全期汇总值，因此没有逐日折线。
          </p>
          <div v-else-if="trendRows.length" ref="chartEl" class="detail__chart"></div>
          <p v-else class="metric-empty">—</p>
        </section>

        <!-- 归因下钻（B14） -->
        <section class="pwc-card col-span-12">
          <div class="pwc-card__header">
            <h4>
              归因下钻
              <span v-if="treeNode" class="pwc-badge pwc-badge--grey">
                {{ treeNode.dimensions.map(dimLabel).join(" → ") }}
              </span>
            </h4>
          </div>
          <!-- 层级选择：指标 dimensions 配置里选 2~4 层（按顺序） -->
          <div class="attr__builder">
            <span class="attr__label">维度层级：</span>
            <el-tag
              v-for="(d, i) in treeDims"
              :key="d"
              closable
              @close="removeTreeDim(i)"
            >{{ i + 1 }}. {{ dimLabel(d) }}</el-tag>
            <el-dropdown
              v-if="dimCandidates.length > treeDims.length && treeDims.length < 4"
              trigger="click"
              @command="addTreeDim"
            >
              <el-button size="small" plain>+ 添加层级</el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item
                    v-for="c in dimCandidates.filter((c) => !treeDims.includes(c))"
                    :key="c"
                    :command="c"
                  >{{ dimLabel(c) }}</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
            <el-button
              v-if="dimCandidates.length"
              type="primary"
              size="small"
              :disabled="treeDims.length < 2"
              :loading="treeLoading"
              @click="loadTreeNode([])"
            >
              {{ treeNode ? "重新生成" : "生成归因" }}
            </el-button>
            <span v-if="!dimCandidates.length" class="attr__hint">
              该指标未配置常用维度（dimensions），<template v-if="canManage">点
              <el-link type="primary" class="attr__edit-link" @click="goEditMetric">「编辑指标」</el-link></template><template v-else>在「编辑指标」中</template>补充后使用归因下钻
            </span>
          </div>

          <template v-if="treeNode">
            <!-- AI 解读（P2 #6：仅根节点生成一句；降级给原因提示+重试，不再无声消失） -->
            <div
              v-if="treePath.length === 0 && (attrInterpLoading || attrInterp || attrInterpHint)"
              class="attr__interp"
            >
              <span class="attr__interp-badge">AI 解读</span>
              <span v-if="attrInterpLoading" class="attr__interp-text">生成中…</span>
              <span v-else-if="attrInterp" class="attr__interp-text">{{ attrInterp }}</span>
              <AiFeedback
                v-if="attrInterp"
                kind="attribute_interpretation"
                :target="`metric:${metricId}`"
              />
              <span v-else class="attr__interp-text attr__interp-hint">
                {{ attrInterpHint }}
                <el-link
                  type="primary"
                  :underline="false"
                  class="attr__interp-retry"
                  @click="loadAttrInterpretation"
                  >重试</el-link
                >
              </span>
            </div>
            <!-- 节点概要 + 面包屑（父级可点击回退到对应层级） -->
            <div class="attr__node">
              <el-breadcrumb separator="›">
                <el-breadcrumb-item>
                  <el-tooltip content="点击返回总计（最上层）" placement="top">
                    <el-link :underline="false" class="attr__crumb" @click="jumpTo(0)">总计</el-link>
                  </el-tooltip>
                </el-breadcrumb-item>
                <el-breadcrumb-item v-for="(s, i) in treeNode.path" :key="s.dimension">
                  <el-tooltip v-if="i < treeNode.path.length - 1" content="点击返回该层（收起其下钻层级）" placement="top">
                    <el-link :underline="false" class="attr__crumb" @click="jumpTo(i + 1)">
                      {{ dimLabel(s.dimension) }} = {{ s.value }}
                    </el-link>
                  </el-tooltip>
                  <span v-else>{{ dimLabel(s.dimension) }} = {{ s.value }}</span>
                </el-breadcrumb-item>
              </el-breadcrumb>
              <span class="attr__delta">
                变化：
                <b :class="treeNode.delta > 0 ? 'pwc-num--up' : treeNode.delta < 0 ? 'pwc-num--down' : ''">
                  {{ fmtSigned(treeNode.delta) }}
                </b>
                <span class="attr__totals">
                  （{{ treeNode.prev_total }} → {{ treeNode.current_total }}）
                </span>
              </span>
              <span
                v-if="treeNode.conservation_deviation_pct != null && Math.abs(treeNode.conservation_deviation_pct) > 0.5"
                class="attr__hint"
              >守恒偏差 {{ treeNode.conservation_deviation_pct }}%（组数超上限截断所致）</span>
            </div>
            <!-- 子层贡献表 -->
            <el-table
              :data="treeNode.children"
              v-loading="treeLoading"
              style="width: 100%"
              :row-class-name="({ row }) => (row.value === '（其他）' || !treeNode.has_next ? '' : 'attr__row--clickable')"
              @row-click="drillChild"
            >
              <el-table-column :label="attrNodeLabel" prop="value" min-width="160" />
              <el-table-column label="本期" width="120">
                <template #default="{ row }">{{ row.current == null ? "—" : formatMetricValue(row.current) }}</template>
              </el-table-column>
              <el-table-column label="基期" width="120">
                <template #default="{ row }">{{ row.prev == null ? "—" : formatMetricValue(row.prev) }}</template>
              </el-table-column>
              <el-table-column label="贡献" width="150">
                <template #default="{ row }">
                  <span :class="row.contribution > 0 ? 'pwc-num--up' : row.contribution < 0 ? 'pwc-num--down' : ''">
                    {{ fmtSigned(row.contribution) }}
                  </span>
                </template>
              </el-table-column>
              <el-table-column label="贡献占比" min-width="200">
                <template #default="{ row }">
                  <div v-if="row.contribution_pct != null" class="attr__bar-wrap">
                    <div
                      class="attr__bar"
                      :class="row.contribution >= 0 ? 'attr__bar--up' : 'attr__bar--down'"
                      :style="{ width: `${Math.min(Math.abs(row.contribution_pct), 100)}%` }"
                    />
                    <span class="attr__bar-label">
                      {{ row.contribution > 0 ? "▲" : row.contribution < 0 ? "▼" : "" }} {{ Math.abs(row.contribution_pct).toFixed(1) }}%
                    </span>
                  </div>
                  <span v-else>—</span>
                </template>
              </el-table-column>
              <el-table-column width="90">
                <template #default="{ row }">
                  <el-tag v-if="row.value !== '（其他）' && treeNode.has_next" size="small" type="info" effect="plain">
                    下钻 ›
                  </el-tag>
                </template>
              </el-table-column>
            </el-table>
            <p v-if="treeNode.has_next" class="attr__hint">
              点击行可继续按「{{ dimLabel(treeNode.next_dimension) }}」下钻；点击上方路径（总计 / 各层）可返回对应层级；
              共 {{ treeNode.children_total_count }} 组，逐层守恒（贡献合计 = 节点变化）。
              <template v-if="treeNode.children_total_count > 50">组数超 50，超出部分已并入「（其他）」。</template>
            </p>
            <p v-else class="attr__hint">
              已到叶子层（{{ dimLabel(treeNode.children_dimension) }} 为最后一层）；点击上方路径可返回对应层级。
            </p>
          </template>
        </section>

        <!-- 变更历史 -->
        <section class="pwc-card col-span-12">
          <div class="pwc-card__header">
            <h4>变更历史</h4>
          </div>
          <el-table :data="metricStore.changes" style="width: 100%">
            <el-table-column prop="created_at" label="时间" width="180" />
            <el-table-column label="操作人" width="140">
              <template #default="{ row }">{{ row.operator_id || "—" }}</template>
            </el-table-column>
            <el-table-column label="原因" min-width="200">
              <template #default="{ row }">{{ row.reason || "基础信息更新（未要求填写原因）" }}</template>
            </el-table-column>
            <el-table-column label="变更内容" min-width="320">
              <template #default="{ row }">{{ changeSummary(row) }}</template>
            </el-table-column>
          </el-table>
        </section>

        <!-- 存档 SQL（折叠区） -->
        <section class="pwc-card col-span-12">
          <el-collapse v-model="sqlExpanded">
            <el-collapse-item title="存档 SQL（编译产物）" name="sql">
              <pre class="detail__sql">{{ sqlText || "（无存档 SQL）" }}</pre>
            </el-collapse-item>
          </el-collapse>
        </section>
      </div>
    </div>
  </div>
</template>

<style scoped>
.detail__aliases {
  margin-left: var(--pwc-space-3);
}

.detail__value {
  font-size: var(--pwc-font-data-l);
  font-weight: 700;
  margin-bottom: var(--pwc-space-2);
}

.detail__definition {
  white-space: pre-wrap;
}

.detail__ai-hyp {
  line-height: 1.8;
}

.detail__ai-line {
  display: block;
}

.detail__ai-rule {
  color: var(--pwc-text-secondary);
  line-height: 1.8;
}

.detail__subhead {
  margin: var(--pwc-space-4) 0 var(--pwc-space-2);
  font-family: var(--pwc-font-sans);
  font-weight: 600;
}

.detail__options {
  margin: 0;
  padding-left: var(--pwc-space-5);
  color: var(--pwc-text-secondary);
}

.detail__options li {
  margin-bottom: var(--pwc-space-2);
}

.detail__options strong {
  color: var(--pwc-text-primary);
  font-weight: 600;
}

.detail__options .pwc-badge {
  margin-left: var(--pwc-space-2);
}

.detail__chart {
  width: 100%;
  height: 320px;
}

.detail__sql {
  background: var(--pwc-bg-layer-1);
  padding: var(--pwc-space-4);
  overflow: auto;
  font-family: Consolas, Monaco, monospace;
  font-size: 13px;
  white-space: pre-wrap;
}

/* ── 归因下钻（B14） ── */
/* AI 解读句（P2 #6）：浅底虚线框，弱化装饰、强调文字 */
.attr__interp {
  display: flex;
  align-items: baseline;
  gap: var(--pwc-space-2);
  margin-bottom: var(--pwc-space-3);
  padding: var(--pwc-space-2) var(--pwc-space-3);
  background: var(--pwc-surface-subtle, #f7f7f8);
  border-left: 3px solid var(--pwc-primary, #0058a0);
  border-radius: 4px;
}

.attr__interp-badge {
  flex: none;
  font-size: var(--pwc-font-body-s);
  color: var(--pwc-primary, #0058a0);
  font-weight: 600;
}

.attr__interp-text {
  font-size: var(--pwc-font-body-s);
  color: var(--pwc-text-primary);
}

/* 降级提示（未配置模型/生成失败）：弱化但可见，附重试入口 */
.attr__interp-hint {
  color: var(--pwc-text-secondary);
}
.attr__interp-retry {
  margin-left: var(--pwc-space-2);
  font-size: var(--pwc-font-body-s);
  vertical-align: baseline;
}

.attr__builder {
  display: flex;
  align-items: center;
  gap: var(--pwc-space-2);
  flex-wrap: wrap;
  margin-bottom: var(--pwc-space-3);
}

.attr__label {
  color: var(--pwc-text-secondary);
}

.attr__hint {
  color: var(--pwc-text-secondary);
  font-size: var(--pwc-font-body-s);
}
/* 行内「编辑指标」深链与提示文字基线对齐 */
.attr__edit-link {
  vertical-align: baseline;
  font-size: var(--pwc-font-body-s);
}

.attr__node {
  display: flex;
  align-items: center;
  gap: var(--pwc-space-4);
  flex-wrap: wrap;
  margin-bottom: var(--pwc-space-3);
}

/* 面包屑可点击回退：hover 显下划线强化「可点」暗示 */
.attr__crumb:hover .el-link__inner {
  text-decoration: underline;
}

.attr__delta b {
  font-size: var(--pwc-font-body-l);
}

.attr__totals {
  color: var(--pwc-text-secondary);
  font-size: var(--pwc-font-body-s);
}

:deep(.attr__row--clickable) {
  cursor: pointer;
}

.attr__bar-wrap {
  position: relative;
  display: flex;
  align-items: center;
  height: 18px;
  background: var(--pwc-bg-layer-2, rgba(0, 0, 0, 0.04));
  border-radius: 2px;
}

.attr__bar {
  height: 100%;
  border-radius: 2px;
  opacity: 0.75;
}

.attr__bar--up {
  background: var(--pwc-up, #d62222);
}

.attr__bar--down {
  background: var(--pwc-down, #059669);
}

.attr__bar-label {
  position: absolute;
  left: 6px;
  font-size: 12px;
  color: var(--pwc-text-primary);
}
</style>
