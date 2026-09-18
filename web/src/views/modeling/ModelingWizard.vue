<!-- pwc-regime: product-ui -->
<script setup>
/**
 * 建模建议向导（B13-2）：自动建模建议 → 人工确认 → 复用既有写接口入库。
 *
 * 铁律：建议零副作用（POST /modeling/suggestions 只读）；入库只走
 * POST /datasets/{id}/relations 与 POST /metrics（零新增写路径）。
 * 关系入库需 admin（后端 AdminUser），指标入库需写权限（WriterUser）。
 */
import { computed, nextTick, onMounted, ref } from "vue";
import { ElMessage } from "element-plus";
import { useRouter } from "vue-router";

import { addRelation, listDatasets } from "@/api/datasets";
import { createMetric } from "@/api/metrics";
import { fetchModelingSuggestions } from "@/api/modeling";
import { useAuthStore } from "@/stores/auth";
import { useProjectStore } from "@/stores/project";

const router = useRouter();
const auth = useAuthStore();
const projectStore = useProjectStore();

const step = ref(0);
const loading = ref(false);
const datasets = ref([]);
const suggestions = ref(null);

const RELATION_TYPES = [
  { value: "one_to_one", label: "1:1" },
  { value: "one_to_many", label: "1:n" },
  { value: "many_to_one", label: "n:1" },
  { value: "many_to_many", label: "n:n" },
];

const relationRows = ref([]);
const metricRows = ref([]);
const skippedDatasets = ref([]); // 未参与关系建议的表及原因（明示"为什么没有建议"）
const relTableRef = ref(null);
const metricTableRef = ref(null);

const importing = ref(false);
const results = ref([]);
const finished = ref(false);

const relKey = (r) => `${r.from_dataset}|${r.from_column}|${r.to_dataset}|${r.to_column}`;

const relSelection = computed(
  () => relTableRef.value?.getSelectionRows?.() ?? [],
);
const metricSelection = computed(
  () => metricTableRef.value?.getSelectionRows?.() ?? [],
);

// 跨步骤快照：表格随 v-if 卸载后 getSelectionRows 失效，进入下一步前先落袋
const selectedRelations = ref([]);
const selectedMetrics = ref([]);

function toMetricsStep() {
  if (!relSelection.value.length) {
    ElMessage.warning("请至少勾选一条关系建议");
    return;
  }
  selectedRelations.value = [...relSelection.value];
  step.value = 2;
}

function toConfirmStep() {
  selectedMetrics.value = [...metricSelection.value];
  step.value = 3;
}

function genCode(row) {
  const raw = `${row.dataset}_${row.column}_${row.aggregation}`
    .toLowerCase()
    .replace(/[^a-z0-9_\u4e00-\u9fff]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return raw.slice(0, 90) || "metric";
}

async function generate() {
  loading.value = true;
  finished.value = false;
  results.value = [];
  try {
    const pid = projectStore.currentId;
    const [list, sug] = await Promise.all([
      listDatasets(pid ? { project_id: pid } : undefined),
      fetchModelingSuggestions(pid),
    ]);
    datasets.value = Array.isArray(list) ? list : (list?.items ?? []);
    suggestions.value = sug;

    relationRows.value = (sug.relations ?? []).map((r) => ({
      ...r,
      key: relKey(r),
      // 默认勾选：分数较高、未登记、LLM 复审未判 unlikely
      autoSelect:
        !r.existing &&
        r.score >= 0.72 &&
        r.llm_review?.verdict !== "unlikely",
    }));
    skippedDatasets.value = (sug.datasets ?? []).filter((d) => d.relation_note);
    metricRows.value = (sug.metrics ?? []).map((m) => ({
      ...m,
      code: genCode(m),
      time_field: m.time_field ?? "", // 多日期列数据集必须显式指定，否则保存即编译拒绝
      autoSelect: true,
    }));
    step.value = 1;
    await nextTick();
    relationRows.value.forEach((r) => {
      if (r.autoSelect) relTableRef.value?.toggleRowSelection(r, true);
    });
    metricRows.value.forEach((r) => {
      if (r.autoSelect) metricTableRef.value?.toggleRowSelection(r, true);
    });
  } finally {
    loading.value = false;
  }
}

const idByName = computed(() =>
  Object.fromEntries(datasets.value.map((d) => [d.name, d.id])),
);

// 后端契约：HTTP 400 等业务错误经拦截器 reject 的是 axios error，
// 真实 message 在 response.data.message（skipErrorToast 静默时须自行提取）
const errMsg = (e) =>
  e?.response?.data?.message || e?.message || "失败";

async function runImport() {
  if (!selectedRelations.value.length && !selectedMetrics.value.length) {
    ElMessage.warning("请至少勾选一条建议");
    return;
  }
  importing.value = true;
  results.value = [];
  const map = idByName.value;
  for (const r of selectedRelations.value) {
    const label = `${r.from_dataset}.${r.from_column} → ${r.to_dataset}.${r.to_column}`;
    try {
      await addRelation(map[r.from_dataset], {
        from_column: r.from_column,
        target_dataset_id: map[r.to_dataset],
        target_column: r.to_column,
        relation_type: r.relation_type,
      }, { skipErrorToast: true });
      results.value.push({ type: "表关系", label, ok: true });
    } catch (e) {
      results.value.push({ type: "表关系", label, ok: false, message: errMsg(e) });
    }
  }
  for (const m of selectedMetrics.value) {
    const label = `${m.name}（${m.dataset}.${m.column} ${m.aggregation}）`;
    try {
      const rule = { base_aggregation: m.aggregation, source: { table: m.dataset, column: m.column } };
      if (m.time_field) rule.time_field = m.time_field;
      await createMetric({
        code: m.code,
        name: m.name,
        calc_rule: rule,
        definition: m.reason || "建模建议向导入库",
        project_id: projectStore.currentId ?? null,
      }, { skipErrorToast: true });
      results.value.push({ type: "指标", label, ok: true });
    } catch (e) {
      results.value.push({ type: "指标", label, ok: false, message: errMsg(e) });
    }
  }
  importing.value = false;
  finished.value = true;
  const okCount = results.value.filter((r) => r.ok).length;
  ElMessage.success(`入库完成：成功 ${okCount} / 共 ${results.value.length} 条`);
}

const scoreTag = (r) =>
  r.llm_review?.verdict === "unlikely"
    ? "danger"
    : r.score >= 0.8
      ? "success"
      : r.score >= 0.72
        ? "warning"
        : "info";

onMounted(async () => {
  const pid = projectStore.currentId;
  try {
    const list = await listDatasets(pid ? { project_id: pid } : undefined);
    datasets.value = Array.isArray(list) ? list : (list?.items ?? []);
  } catch {
    datasets.value = [];
  }
});
</script>

<template>
  <div class="page-container">
    <div class="page-header">
      <div>
        <h1 class="page-header__title">建模建议向导</h1>
        <p class="page-header__subtitle">
          自动建议 · 人工确认才入库 · 全部走既有关系/指标写接口（B13 铁律）
        </p>
      </div>
      <div class="page-header__actions">
        <el-button :disabled="loading" @click="router.push('/datasets')">返回数据集</el-button>
        <el-button type="primary" :loading="loading" @click="generate">生成建模建议</el-button>
      </div>
    </div>

    <el-steps :active="step" align-center class="mw__steps">
      <el-step title="生成建议" description="引擎扫描项目内数据集" />
      <el-step title="确认表关系" description="勾选可信关系" />
      <el-step title="确认指标候选" description="核对名称与口径" />
      <el-step title="执行入库" description="写关系与指标" />
    </el-steps>

    <!-- 步骤 2：表关系建议 -->
    <section v-if="step === 1" class="pwc-card">
      <div class="pwc-card__header">
        <h4>表关系建议（{{ relationRows.length }}）</h4>
        <span class="pwc-badge pwc-badge--grey">已登记关系标注「已存在」，默认不勾选</span>
      </div>
      <el-alert
        v-if="!relationRows.length && !skippedDatasets.length"
        type="info"
        :closable="false"
        title="未发现可信关系候选：需至少两个数据集且存在值域重叠/命名相似的文本列"
      />
      <el-alert
        v-for="d in skippedDatasets"
        :key="d.name"
        type="info"
        :closable="false"
        style="margin-bottom: 6px"
        :title="`「${d.name}」未参与关系建议：${d.relation_note}（文本列 ${d.text_columns} 个 / 日期列 ${d.date_columns} 个）`"
      />
      <el-table
        v-if="relationRows.length"
        ref="relTableRef"
        :data="relationRows"
        row-key="key"
        size="small"
        max-height="480"
      >
        <el-table-column type="selection" width="42" />
        <el-table-column label="关系" min-width="240">
          <template #default="{ row }">
            {{ row.from_dataset }}.{{ row.from_column }} → {{ row.to_dataset }}.{{ row.to_column }}
          </template>
        </el-table-column>
        <el-table-column label="类型" width="100">
          <template #default="{ row }">
            <el-select v-model="row.relation_type" size="small">
              <el-option v-for="t in RELATION_TYPES" :key="t.value" :label="t.label" :value="t.value" />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column label="综合分" width="90">
          <template #default="{ row }">
            <el-tag :type="scoreTag(row)" size="small">{{ Math.round(row.score * 100) }}%</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="证据" min-width="220">
          <template #default="{ row }">
            <span class="mw__evidence">
              值域重叠 {{ row.evidence.overlap_ratio }} · 命名 {{ row.evidence.name_similarity }}
              · 基数 {{ row.evidence.cardinality_from }}/{{ row.evidence.cardinality_to }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="标注" min-width="200">
          <template #default="{ row }">
            <el-tag v-if="row.existing" size="small" type="info" class="mw__tag">已存在</el-tag>
            <el-tag v-if="row.evidence.sampled" size="small" type="warning" class="mw__tag">
              截断采样（高基数估算）
            </el-tag>
            <el-tag v-if="row.evidence.isolated_pair" size="small" type="warning" class="mw__tag">
              孤立巧合（无其他关联佐证）
            </el-tag>
            <el-tooltip v-if="row.llm_review" :content="row.llm_review.reason">
              <el-tag
                size="small"
                :type="row.llm_review.verdict === 'likely' ? 'success' : row.llm_review.verdict === 'unlikely' ? 'danger' : 'info'"
                class="mw__tag"
              >
                LLM：{{ row.llm_review.verdict }}
              </el-tag>
            </el-tooltip>
          </template>
        </el-table-column>
      </el-table>
      <div class="mw__nav">
        <el-button @click="step = 0">上一步</el-button>
        <el-button type="primary" :disabled="!relSelection.length" @click="toMetricsStep">
          下一步（已选 {{ relSelection.length }} 条关系）
        </el-button>
      </div>
    </section>

    <!-- 步骤 3：指标候选 -->
    <section v-if="step === 2" class="pwc-card">
      <div class="pwc-card__header">
        <h4>指标候选（{{ metricRows.length }}）</h4>
        <span class="pwc-badge pwc-badge--grey">布尔/枚举数值列已过滤，不产 sum 候选</span>
      </div>
      <el-alert
        v-if="!metricRows.length"
        type="info"
        :closable="false"
        title="未发现指标候选：需数据集含日期列与数值/高基数文本列"
      />
      <el-table v-else ref="metricTableRef" :data="metricRows" row-key="code" size="small" max-height="480">
        <el-table-column type="selection" width="42" />
        <el-table-column prop="dataset" label="数据集" min-width="120" />
        <el-table-column prop="column" label="列" min-width="110" />
        <el-table-column prop="aggregation" label="聚合" width="110" />
        <el-table-column label="指标名" min-width="160">
          <template #default="{ row }">
            <el-input v-model="row.name" size="small" maxlength="200" />
          </template>
        </el-table-column>
        <el-table-column label="code" min-width="160">
          <template #default="{ row }">
            <el-input v-model="row.code" size="small" maxlength="99" />
          </template>
        </el-table-column>
        <el-table-column label="时间列" min-width="140">
          <template #default="{ row }">
            <el-select
              v-if="(row.date_columns?.length ?? 0) > 1"
              v-model="row.time_field"
              size="small"
              placeholder="选择时间列"
            >
              <el-option v-for="c in row.date_columns" :key="c" :label="c" :value="c" />
            </el-select>
            <span v-else class="mw__evidence">{{ row.time_field || "全期常数" }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="reason" label="依据" min-width="200" show-overflow-tooltip />
        <el-table-column label="来源" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="row.source === 'llm' ? 'success' : 'info'">
              {{ row.source === "llm" ? "LLM" : "启发" }}
            </el-tag>
          </template>
        </el-table-column>
      </el-table>
      <div class="mw__nav">
        <el-button @click="step = 1">上一步</el-button>
        <el-button type="primary" @click="toConfirmStep">
          下一步（已选 {{ metricSelection.length }} 个指标，可不选仅入库关系）
        </el-button>
      </div>
    </section>

    <!-- 步骤 4：确认入库 -->
    <section v-if="step === 3" class="pwc-card">
      <div class="pwc-card__header">
        <h4>执行入库</h4>
        <span class="pwc-badge pwc-badge--grey">
          表关系 {{ selectedRelations.length }} 条 · 指标 {{ selectedMetrics.length }} 个
        </span>
      </div>
      <p class="mw__hint">
        入库即调用既有接口：表关系 → 数据集关系登记（需 admin）；指标 → 指标管理（需写权限）。
        同名/重复项会被后端拒绝并在下方逐条标注，不影响其余条目。
      </p>
      <el-button type="primary" :loading="importing" :disabled="finished" @click="runImport">
        {{ importing ? "入库中…" : finished ? "已完成" : "确认入库" }}
      </el-button>

      <el-table v-if="results.length" :data="results" size="small" class="mw__results" max-height="360">
        <el-table-column prop="type" label="类型" width="90" />
        <el-table-column prop="label" label="条目" min-width="260" />
        <el-table-column label="结果" width="90">
          <template #default="{ row }">
            <el-tag :type="row.ok ? 'success' : 'danger'" size="small">
              {{ row.ok ? "成功" : "失败" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="message" label="失败原因" min-width="220" show-overflow-tooltip />
      </el-table>
    </section>

    <!-- 步骤 1：说明 -->
    <section v-if="step === 0" class="pwc-card">
      <div class="pwc-card__header"><h4>开始</h4></div>
      <p class="mw__hint">
        引擎将扫描当前项目内全部数据集，自动建议<b>表关系</b>（值域重叠 + 命名相似，含证据分）
        与<b>指标候选</b>（数值列合计、高基数列计数）。所有建议仅供参考，
        <b>未经你确认不会写入任何配置</b>；确认入库走既有写接口，可逐条勾选与改名。
      </p>
      <p class="mw__hint">
        当前项目数据集：{{ datasets.length }} 个（建议需 ≥ 2 个数据集才有关系候选）。
        高基数列（如用户 ID）按截断采样估算，结果标注置信降级；孤立巧合建议会降权并警示。
      </p>
    </section>
  </div>
</template>

<style scoped>
.mw__steps {
  margin: var(--pwc-space-5) 0;
}

.mw__evidence {
  color: var(--pwc-text-secondary);
  font-size: 12px;
}

.mw__tag {
  margin-right: var(--pwc-space-2);
}

.mw__nav {
  margin-top: var(--pwc-space-4);
  display: flex;
  gap: var(--pwc-space-3);
  justify-content: flex-end;
}

.mw__hint {
  margin: 0 0 var(--pwc-space-4);
  color: var(--pwc-text-secondary);
  font-size: 13px;
  line-height: 1.8;
}

.mw__results {
  margin-top: var(--pwc-space-4);
}
</style>
