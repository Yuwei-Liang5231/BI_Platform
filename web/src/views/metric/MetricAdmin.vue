<!-- pwc-regime: product-ui -->
<script setup>
/**
 * 指标管理（analyst/admin）：CRUD（保存即编译校验）+ 从模板导入向导 +
 * 受限可见性配置（role/department 负向登记，整组替换）。
 */
import { computed, onMounted, reactive, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";

import {
  createMetric as createApi,
  deleteMetric as deleteApi,
  updateMetric as updateApi,
  tryCompile as tryCompileApi,
} from "@/api/metrics";
import { getDataset } from "@/api/datasets";
import { getRestrictions, putRestrictions } from "@/api/auth";
import { useAuthStore } from "@/stores/auth";
import { useDatasetStore } from "@/stores/dataset";
import { useMetricStore } from "@/stores/metric";
import { useTemplateStore } from "@/stores/template";

const auth = useAuthStore();
const metricStore = useMetricStore();
const datasetStore = useDatasetStore();
const templateStore = useTemplateStore();

const activeTab = ref("manage");
const search = ref("");
// 管理页需能看到 pending（模板导入未绑数据集）与 disabled，默认查全部
const statusFilter = ref("all");
const dialogVisible = ref(false);
const saving = ref(false);
const editingId = ref(null);
const formRef = ref(null);

const form = reactive({
  code: "",
  name: "",
  aliases: "",
  definition: "",
  topic: "general",
  parent_id: null,
});

/* ---------- 口径分歧（disambiguation）编辑 ---------- */
const dis = reactive({ question: "", defIndex: 0, options: [] });

function resetDisambiguation(d) {
  dis.question = d?.question ?? "";
  dis.options = (d?.options ?? []).map((raw) => {
    const o = typeof raw === "string" ? { name: raw } : raw;
    return { name: o.name ?? o.label ?? "", description: o.description ?? "" };
  });
  const idx = dis.options.findIndex((o) => o.name && o.name === d?.default);
  dis.defIndex = Math.max(0, idx);
}

function buildDisambiguation() {
  const options = dis.options
    .map((o) => ({ name: o.name.trim(), description: o.description.trim() }))
    .filter((o) => o.name);
  const question = dis.question.trim();
  if (!question && !options.length) return { value: null };
  if (question && !options.length)
    return { error: "已填分歧问题但没有候选口径：请添加候选口径，或清空问题" };
  if (!question && options.length) return { error: "已填候选口径，请补充分歧问题" };
  const defName = options[dis.defIndex]?.name ?? options[0].name;
  return { value: { question, options, default: defName } };
}

function addDisOption() {
  dis.options.push({ name: "", description: "" });
  if (dis.defIndex < 0 || dis.defIndex >= dis.options.length) dis.defIndex = 0;
}

function removeDisOption(i) {
  dis.options.splice(i, 1);
  if (dis.defIndex >= dis.options.length) dis.defIndex = Math.max(0, dis.options.length - 1);
}

/* ---------- 编辑态基线（判断 calc_rule 是否真变更，避免无谓的 reason 要求） ---------- */
const originalRule = ref(null);

function stable(value) {
  if (Array.isArray(value)) return value.map(stable);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value)
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([k, v]) => [k, stable(v)]),
    );
  }
  return value;
}

/* ---------- 计算规则三模式构建器：简单聚合 / 比率组合 / JSON 高级 ---------- */
const AGG_OPTIONS = [
  { value: "sum", label: "求和 sum" },
  { value: "count", label: "计数 count" },
  { value: "count_distinct", label: "去重计数 count_distinct" },
  { value: "avg", label: "平均 avg" },
  { value: "max", label: "最大 max" },
  { value: "min", label: "最小 min" },
];

const builder = reactive({
  mode: "flat", // flat | expr | json
  flat: { table: "", column: "", aggregation: "sum", filter: "", time_field: "" },
  expr: {
    expression: "A / B",
    operands: [
      { key: "A", table: "", column: "", aggregation: "sum", filter: "" },
      { key: "B", table: "", column: "", aggregation: "sum", filter: "" },
    ],
  },
});
const jsonRule = ref("");
const datasetColumns = reactive({}); // table name -> [{ name, isDate }]

function ruleMode(rule) {
  if (!rule || typeof rule !== "object") return "json";
  if (rule.base_aggregation || rule.source) return "flat";
  if (rule.expression || rule.operands) return "expr";
  return "json";
}

function fillBuilderFromRule(rule) {
  if (rule === null || rule === undefined) {
    // 新建：默认「简单聚合」空表单
    builder.mode = "flat";
    Object.assign(builder.flat, {
      table: "",
      column: "",
      aggregation: "sum",
      filter: "",
      time_field: "",
    });
    builder.expr.expression = "A / B";
    builder.expr.operands = [
      { key: "A", table: "", column: "", aggregation: "sum", filter: "" },
      { key: "B", table: "", column: "", aggregation: "sum", filter: "" },
    ];
    jsonRule.value = "";
    return;
  }
  builder.mode = ruleMode(rule);
  if (builder.mode === "flat") {
    Object.assign(builder.flat, {
      table: rule.source?.table ?? "",
      column: rule.source?.column ?? "",
      aggregation: rule.base_aggregation ?? "sum",
      filter: rule.source?.filter ?? "",
      time_field: rule.time_field ?? "",
    });
  } else if (builder.mode === "expr") {
    builder.expr.expression = rule.expression ?? "A / B";
    const names = Object.keys(rule.operands ?? {});
    builder.expr.operands = names.length
      ? names.map((k) => ({
          key: k,
          table: rule.operands[k]?.table ?? "",
          column: rule.operands[k]?.column ?? "",
          aggregation: rule.operands[k]?.aggregation ?? "sum",
          filter: rule.operands[k]?.filter ?? "",
        }))
      : [
          { key: "A", table: "", column: "", aggregation: "sum", filter: "" },
          { key: "B", table: "", column: "", aggregation: "sum", filter: "" },
        ];
  } else {
    jsonRule.value = rule ? JSON.stringify(rule, null, 2) : "";
  }
  // 预取已选表的字段供下拉
  const tables = new Set();
  if (builder.mode === "flat" && builder.flat.table) tables.add(builder.flat.table);
  if (builder.mode === "expr")
    builder.expr.operands.forEach((o) => o.table && tables.add(o.table));
  tables.forEach((t) => loadColumns(t));
}

function buildRule() {
  if (builder.mode === "flat") {
    const f = builder.flat;
    const rule = { base_aggregation: f.aggregation, source: { table: f.table, column: f.column } };
    if (f.filter.trim()) rule.source.filter = f.filter.trim();
    if (f.time_field.trim()) rule.time_field = f.time_field.trim();
    return rule;
  }
  if (builder.mode === "expr") {
    const operands = {};
    for (const o of builder.expr.operands) {
      const key = o.key.trim();
      const def = { table: o.table, column: o.column, aggregation: o.aggregation };
      if (o.filter.trim()) def.filter = o.filter.trim();
      operands[key] = def;
    }
    return { expression: builder.expr.expression.trim(), operands };
  }
  return JSON.parse(jsonRule.value); // json 模式可能抛错，由 parseRule 捕获
}

function parseRule() {
  try {
    return { ok: true, rule: buildRule() };
  } catch {
    ElMessage.error("计算规则不是合法 JSON");
    return { ok: false };
  }
}

async function switchMode(mode) {
  if (mode === builder.mode) return;
  // 离开 json 前先校验可解析；从表单切到 json 时同步快照
  if (builder.mode === "json" && mode !== "json") {
    try {
      const rule = JSON.parse(jsonRule.value);
      builder.mode = mode;
      fillBuilderFromRule(rule);
    } catch {
      ElMessage.error("当前 JSON 不合法，无法转为表单模式");
      return;
    }
    return;
  }
  if (builder.mode !== "json") {
    try {
      jsonRule.value = JSON.stringify(buildRule(), null, 2);
    } catch {
      jsonRule.value = "";
    }
  }
  builder.mode = mode;
}

async function loadColumns(tableName) {
  if (!tableName || datasetColumns[tableName]) return;
  if (!datasetStore.list.length) await datasetStore.fetchList();
  const id = datasetStore.list.find((d) => d.name === tableName)?.id;
  if (id === undefined) {
    datasetColumns[tableName] = [];
    return;
  }
  try {
    const detail = await getDataset(id);
    const raw = detail?.columns ?? [];
    datasetColumns[tableName] = raw.map((c) => {
      const name = typeof c === "string" ? c : (c.name ?? "");
      const type = (typeof c === "string" ? "" : String(c.type ?? c.dtype ?? "")).toLowerCase();
      return {
        name,
        isDate: type.includes("date") || /date|日期|_at$|_dt$/i.test(name),
      };
    });
  } catch {
    datasetColumns[tableName] = [];
  }
}

function onOperandTableChange(operand) {
  operand.column = "";
  loadColumns(operand.table);
}

function addOperand() {
  const used = new Set(builder.expr.operands.map((o) => o.key));
  let i = builder.expr.operands.length;
  let key = `OP${i}`;
  while (used.has(key)) key = `OP${++i}`;
  builder.expr.operands.push({ key, table: "", column: "", aggregation: "sum", filter: "" });
}

function fillFlatSample() {
  const t = datasetStore.list[0]?.name ?? "orders";
  builder.mode = "flat";
  Object.assign(builder.flat, {
    table: t,
    column: "",
    aggregation: "sum",
    filter: "",
    time_field: "",
  });
  loadColumns(t);
  ElMessage.info(`已填入示例骨架：选数据集「${t}」后，再选字段并按需加过滤条件`);
}

function fillExprSample() {
  const t = datasetStore.list[0]?.name ?? "orders";
  builder.mode = "expr";
  builder.expr.expression = "A / B";
  builder.expr.operands = [
    { key: "A", table: t, column: "", aggregation: "sum", filter: "" },
    { key: "B", table: t, column: "", aggregation: "count", filter: "" },
  ];
  loadColumns(t);
  ElMessage.info(
    "示例：A 为金额类求和、B 为单据计数，表达式 A / B 即「平均单值」；字段请自行选择",
  );
}

const rules = {
  code: [
    { required: true, message: "请输入编码", trigger: "blur" },
    { pattern: /^[a-z][a-z0-9_]{1,98}$/, message: "小写字母开头，仅小写字母/数字/下划线", trigger: "blur" },
  ],
  name: [{ required: true, message: "请输入名称", trigger: "blur" }],
};

async function fetchData() {
  const params = { status: statusFilter.value };
  if (search.value) params.search = search.value;
  await metricStore.fetchList(params);
}

function openCreate() {
  editingId.value = null;
  Object.assign(form, {
    code: "",
    name: "",
    aliases: "",
    definition: "",
    topic: "general",
    parent_id: null,
  });
  fillBuilderFromRule(null);
  resetDisambiguation(null);
  originalRule.value = null;
  if (!datasetStore.list.length) datasetStore.fetchList();
  dialogVisible.value = true;
}

function openEdit(row) {
  editingId.value = row.id;
  Object.assign(form, {
    code: row.code,
    name: row.name,
    aliases: (row.aliases ?? []).join(", "),
    definition: row.definition ?? "",
    topic: row.topic ?? "general",
    parent_id: row.parent_id ?? null,
  });
  fillBuilderFromRule(row.calc_rule);
  resetDisambiguation(row.disambiguation);
  originalRule.value = row.calc_rule ?? null;
  if (!datasetStore.list.length) datasetStore.fetchList();
  dialogVisible.value = true;
}

async function handleTryCompile() {
  const parsed = parseRule();
  if (!parsed.ok) return;
  try {
    await tryCompileApi({ calc_rule: parsed.rule });
    ElMessage.success("编译通过");
  } catch {
    /* 拦截器已提示 */
  }
}

async function handleSave() {
  await formRef.value.validate();
  const parsed = parseRule();
  if (!parsed.ok) return;
  const disResult = buildDisambiguation();
  if (disResult.error) {
    ElMessage.error(disResult.error);
    return;
  }
  // 口径（calc_rule）真变更时，后端强制留痕 reason
  let reason = "";
  if (
    editingId.value &&
    JSON.stringify(stable(parsed.rule)) !== JSON.stringify(stable(originalRule.value ?? {}))
  ) {
    try {
      const { value } = await ElMessageBox.prompt(
        "计算规则（口径）发生了变更，必须填写变更原因（将记入变更历史）:",
        "口径变更确认",
        {
          inputValidator: (v) => (v && v.trim().length >= 2 ? true : "原因至少 2 个字"),
        },
      );
      reason = value.trim();
    } catch {
      return; // 用户取消
    }
  }
  saving.value = true;
  try {
    const payload = {
      code: form.code.trim(),
      name: form.name.trim(),
      aliases: form.aliases
        .split(/[,，]/)
        .map((s) => s.trim())
        .filter(Boolean),
      definition: form.definition,
      topic: form.topic,
      parent_id: form.parent_id,
      calc_rule: parsed.rule,
    };
    if (editingId.value) {
      // PATCH：口径未变不带 calc_rule（避免后端强制 reason）；变了才带规则 + reason。
      // disambiguation 传 null 不清空（后端 exclude_none），空对象可清空。
      const { code: _ignored, calc_rule: _rule, ...rest } = payload;
      const updatable = { ...rest, disambiguation: disResult.value ?? { question: "", options: [] } };
      if (reason) {
        updatable.calc_rule = parsed.rule;
        updatable.reason = reason;
      }
      await updateApi(editingId.value, updatable);
      ElMessage.success("已更新");
    } else {
      payload.disambiguation = disResult.value ?? null;
      await createApi(payload);
      ElMessage.success("已创建");
    }
    dialogVisible.value = false;
    await fetchData();
  } finally {
    saving.value = false;
  }
}

async function handleDelete(row) {
  await ElMessageBox.confirm(`确定删除指标「${row.name}」？`, "删除确认", { type: "warning" });
  await deleteApi(row.id);
  ElMessage.success("已删除");
  await fetchData();
}

/* ---------- 受限可见性 ---------- */
const restrictionVisible = ref(false);
const restrictionTarget = ref(null);
const restrictionItems = ref([]);

async function openRestrictions(row) {
  restrictionTarget.value = row;
  restrictionVisible.value = true;
  try {
    const res = await getRestrictions(row.id);
    restrictionItems.value = res?.items ?? (Array.isArray(res) ? res : []);
  } catch {
    restrictionItems.value = [];
  }
}

async function saveRestrictions() {
  const items = restrictionItems.value
    .filter((it) => it.subject_value)
    .map((it) => ({ subject_type: it.subject_type, subject_value: it.subject_value }));
  await putRestrictions(restrictionTarget.value.id, items);
  ElMessage.success("可见性已更新");
  restrictionVisible.value = false;
}

/* ---------- 模板导入向导 ---------- */
const wizardVisible = ref(false);
const wizardStep = ref(0);
const chosenIndustry = ref("");
const packMetrics = ref([]);
const checkedCodes = ref([]);
const importResult = ref(null);

async function openWizard() {
  wizardVisible.value = true;
  wizardStep.value = 0;
  importResult.value = null;
  chosenIndustry.value = "";
  packMetrics.value = [];
  await templateStore.fetchIndustries();
}

async function chooseIndustry(industry) {
  chosenIndustry.value = industry;
  const pack = await templateStore.fetchPack(industry);
  packMetrics.value = pack?.metrics ?? (Array.isArray(pack) ? pack : []);
  checkedCodes.value = packMetrics.value.filter((m) => !m.imported).map((m) => m.code);
  wizardStep.value = 1;
}

async function doImport() {
  const res = await templateStore.importTemplates({
    industries: [chosenIndustry.value],
    codes: checkedCodes.value,
  });
  importResult.value = res;
  wizardStep.value = 2;
  await fetchData();
}

const canManage = computed(() => auth.canWrite);

onMounted(async () => {
  if (!canManage.value) {
    ElMessage.warning("当前角色无权进入指标管理");
    return;
  }
  await fetchData();
});
</script>

<template>
  <div class="page-container">
    <div class="page-header">
      <div>
        <h1 class="page-header__title">指标管理</h1>
        <p class="page-header__subtitle">口径只定义一次 · 保存即校验可编译</p>
      </div>
      <div class="page-header__actions">
        <el-button @click="openWizard">从模板导入</el-button>
        <el-button type="primary" @click="openCreate">新建指标</el-button>
      </div>
    </div>

    <el-tabs v-model="activeTab">
      <el-tab-pane label="指标列表" name="manage">
        <div class="admin__toolbar">
          <el-input
            v-model="search"
            placeholder="搜索名称 / 别名"
            clearable
            style="width: 260px"
            @keyup.enter="fetchData"
            @clear="fetchData"
          />
          <el-select
            v-model="statusFilter"
            style="width: 140px"
            @change="fetchData"
          >
            <el-option label="全部状态" value="all" />
            <el-option label="启用中" value="active" />
            <el-option label="待绑定数据" value="pending" />
            <el-option label="已停用" value="disabled" />
          </el-select>
          <el-button type="primary" @click="fetchData">搜索</el-button>
        </div>
        <el-table v-loading="metricStore.loading" :data="metricStore.list">
          <el-table-column prop="code" label="编码" min-width="160" />
          <el-table-column prop="name" label="名称" min-width="160" />
          <el-table-column prop="topic" label="主题" width="120" />
          <el-table-column prop="status" label="状态" width="110">
            <template #default="{ row }">
              <span class="pwc-badge pwc-badge--grey">{{ row.status }}</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="280" fixed="right">
            <template #default="{ row }">
              <el-button text type="primary" @click="openEdit(row)">编辑</el-button>
              <el-button text @click="openRestrictions(row)">可见性</el-button>
              <el-button text type="danger" @click="handleDelete(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
    </el-tabs>

    <!-- 新建 / 编辑 -->
    <el-dialog
      v-model="dialogVisible"
      :title="editingId ? '编辑指标' : '新建指标'"
      width="640px"
    >
      <el-form ref="formRef" :model="form" :rules="rules" label-width="90px">
        <el-form-item label="编码" prop="code">
          <el-input v-model="form.code" :disabled="Boolean(editingId)" placeholder="如 ecom_gmv_paid" />
        </el-form-item>
        <el-form-item label="名称" prop="name">
          <el-input v-model="form.name" />
        </el-form-item>
        <el-form-item label="别名">
          <el-input v-model="form.aliases" placeholder="逗号分隔，如 GMV, 成交金额" />
        </el-form-item>
        <el-form-item label="主题">
          <el-input v-model="form.topic" placeholder="general" />
        </el-form-item>
        <el-form-item label="口径说明">
          <el-input v-model="form.definition" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="口径分歧">
          <div class="rule-builder">
            <el-input
              v-model="dis.question"
              placeholder="分歧问题（可选），如：退款金额是否包含部分退款？"
            />
            <div v-for="(o, i) in dis.options" :key="i" class="admin__restriction-row">
              <el-radio v-model="dis.defIndex" :value="i">默认</el-radio>
              <el-input v-model="o.name" placeholder="口径名称，如：全口径" style="width: 170px" />
              <el-input
                v-model="o.description"
                placeholder="口径描述，如：refund_amount 列直接求和"
                style="flex: 1"
              />
              <el-button text type="danger" @click="removeDisOption(i)">移除</el-button>
            </div>
            <div>
              <el-button text type="primary" @click="addDisOption">+ 添加候选口径</el-button>
              <span class="admin__hint">说明「同名指标的不同算法」：填问题与候选口径，勾选其一为默认。</span>
            </div>
          </div>
        </el-form-item>
        <el-form-item label="计算规则">
          <div class="rule-builder">
            <div class="rule-builder__bar">
              <el-radio-group :model-value="builder.mode" size="small" @update:model-value="switchMode">
                <el-radio-button value="flat">简单聚合</el-radio-button>
                <el-radio-button value="expr">比率组合</el-radio-button>
                <el-radio-button value="json">JSON 高级</el-radio-button>
              </el-radio-group>
              <el-button text type="primary" size="small" @click="builder.mode === 'expr' ? fillExprSample() : fillFlatSample()">
                填充示例
              </el-button>
            </div>
            <p class="admin__hint">
              {{ builder.mode === "flat"
                ? "对一个数据集的某字段做聚合，可选加过滤条件。如：已支付订单金额求和 = 客单价的分子。"
                : builder.mode === "expr"
                  ? "先定义若干个「操作数」（各自的聚合口径），再用表达式组合。表达式只能用 + - * / 和操作数名。"
                  : "直接编辑 JSON（完整能力，含跨表 operand 级 time_field）。切换回表单模式前需为合法 JSON。" }}
            </p>

            <!-- 简单聚合 -->
            <template v-if="builder.mode === 'flat'">
              <div class="rule-builder__grid">
                <el-select
                  v-model="builder.flat.table"
                  placeholder="数据集"
                  style="width: 180px"
                  @change="(t) => { builder.flat.column = ''; loadColumns(t); }"
                >
                  <el-option v-for="d in datasetStore.list" :key="d.id" :label="d.name" :value="d.name" />
                </el-select>
                <el-select v-model="builder.flat.column" placeholder="字段（必选）" style="width: 180px" filterable>
                  <el-option v-for="c in datasetColumns[builder.flat.table] ?? []" :key="c.name" :label="c.name" :value="c.name" />
                </el-select>
                <el-select v-model="builder.flat.aggregation" style="width: 200px">
                  <el-option v-for="a in AGG_OPTIONS" :key="a.value" :label="a.label" :value="a.value" />
                </el-select>
              </div>
              <el-input
                v-model="builder.flat.filter"
                placeholder="过滤条件（可选），如：order_status = '已支付'"
                class="rule-builder__mono"
              />
              <el-select v-model="builder.flat.time_field" placeholder="时间字段（可选，默认自动识别日期列）" clearable filterable>
                <el-option
                  v-for="c in (datasetColumns[builder.flat.table] ?? []).filter((c) => c.isDate)"
                  :key="c.name"
                  :label="c.name"
                  :value="c.name"
                />
              </el-select>
            </template>

            <!-- 比率组合 -->
            <template v-else-if="builder.mode === 'expr'">
              <el-input
                v-model="builder.expr.expression"
                placeholder="表达式，如：A / B"
                class="rule-builder__mono"
              >
                <template #prepend>表达式</template>
              </el-input>
              <div v-for="(o, i) in builder.expr.operands" :key="i" class="rule-builder__operand">
                <el-input v-model="o.key" placeholder="名称" style="width: 90px" class="rule-builder__mono" />
                <el-select
                  v-model="o.table"
                  placeholder="数据集"
                  style="width: 160px"
                  @change="() => onOperandTableChange(o)"
                >
                  <el-option v-for="d in datasetStore.list" :key="d.id" :label="d.name" :value="d.name" />
                </el-select>
                <el-select v-model="o.column" placeholder="字段" style="width: 160px" filterable>
                  <el-option v-for="c in datasetColumns[o.table] ?? []" :key="c.name" :label="c.name" :value="c.name" />
                </el-select>
                <el-select v-model="o.aggregation" style="width: 170px">
                  <el-option v-for="a in AGG_OPTIONS" :key="a.value" :label="a.label" :value="a.value" />
                </el-select>
                <el-input
                  v-model="o.filter"
                  placeholder="过滤（可选）"
                  class="rule-builder__mono"
                  style="flex: 1"
                />
                <el-button
                  text
                  type="danger"
                  :disabled="builder.expr.operands.length <= 1"
                  @click="builder.expr.operands.splice(i, 1)"
                >
                  移除
                </el-button>
              </div>
              <el-button text type="primary" @click="addOperand">+ 添加操作数</el-button>
            </template>

            <!-- JSON 高级 -->
            <el-input
              v-else
              v-model="jsonRule"
              type="textarea"
              :rows="8"
              class="rule-builder__mono"
              spellcheck="false"
            />

            <p class="admin__hint rule-builder__filter-help">
              过滤条件文法：仅支持「字段 比较符 值」，多个条件用 AND 连接；比较符 = != > >= < <=，文本值加单引号。不支持 OR / NOT / 括号 / 函数。
            </p>
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="handleTryCompile">试编译</el-button>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="handleSave">保存</el-button>
      </template>
    </el-dialog>

    <!-- 受限可见性 -->
    <el-dialog v-model="restrictionVisible" title="受限可见性配置" width="560px">
      <p class="admin__hint">
        负向登记：被登记的 role/department 将看不到「{{ restrictionTarget?.name }}」。
        保存为整组替换，全部清空即恢复全员可见。
      </p>
      <div
        v-for="(item, i) in restrictionItems"
        :key="i"
        class="admin__restriction-row"
      >
        <el-select v-model="item.subject_type" style="width: 140px">
          <el-option label="角色 role" value="role" />
          <el-option label="部门 department" value="department" />
        </el-select>
        <el-input
          v-model="item.subject_value"
          placeholder="如 viewer / sales"
          style="flex: 1"
        />
        <el-button text type="danger" @click="restrictionItems.splice(i, 1)">移除</el-button>
      </div>
      <el-button
        text
        type="primary"
        @click="restrictionItems.push({ subject_type: 'role', subject_value: '' })"
      >
        + 添加登记
      </el-button>
      <template #footer>
        <el-button @click="restrictionVisible = false">取消</el-button>
        <el-button type="primary" @click="saveRestrictions">保存</el-button>
      </template>
    </el-dialog>

    <!-- 模板导入向导 -->
    <el-dialog v-model="wizardVisible" title="从模板导入指标" width="720px">
      <el-steps :active="wizardStep" align-center finish-status="success">
        <el-step title="选行业" />
        <el-step title="预览勾选" />
        <el-step title="导入结果" />
      </el-steps>

      <div v-if="wizardStep === 0" class="admin__wizard-body">
        <div
          v-for="pack in templateStore.industries"
          :key="pack.industry"
          class="pwc-card admin__industry"
          role="button"
          tabindex="0"
          @click="chooseIndustry(pack.industry)"
          @keyup.enter="chooseIndustry(pack.industry)"
        >
          <h4>{{ pack.name }}</h4>
          <p class="admin__hint">{{ pack.description }} · {{ pack.metric_count }} 条</p>
        </div>
      </div>

      <div v-else-if="wizardStep === 1" class="admin__wizard-body">
        <el-checkbox-group v-model="checkedCodes">
          <el-checkbox
            v-for="m in packMetrics"
            :key="m.code"
            :value="m.code"
            :disabled="m.imported"
            class="admin__metric-check"
          >
            {{ m.name }}（{{ m.code }}）<span v-if="m.imported"> · 已导入</span>
          </el-checkbox>
        </el-checkbox-group>
      </div>

      <div v-else-if="wizardStep === 2" class="admin__wizard-body">
        <template v-if="importResult">
          <p>
            新建 {{ importResult.totals?.created ?? 0 }} · 跳过
            {{ importResult.totals?.skipped ?? 0 }} · 升级
            {{ importResult.totals?.upgraded ?? 0 }}
          </p>
          <p class="admin__hint">
            当前状态分布：启用 {{ importResult.totals?.active ?? 0 }} · 待绑定数据
            {{ importResult.totals?.pending ?? 0 }} · 已停用
            {{ importResult.totals?.disabled ?? 0 }}
          </p>
          <p
            v-if="importResult.totals?.pending > 0"
            class="admin__hint"
          >
            「待绑定数据」指标暂不在指标目录出现；上传对应数据集后重新执行本向导导入即可自动启用。
          </p>
          <p
            v-for="(r, i) in importResult.results ?? []"
            :key="i"
            class="admin__hint"
          >
            {{ r.name }}：新建 {{ r.created }} · 跳过 {{ r.skipped }} · 升级 {{ r.upgraded }}
          </p>
          <p
            v-for="(note, i) in importResult.results?.flatMap((r) => r.notes ?? []) ?? []"
            :key="'n' + i"
            class="admin__note"
          >
            {{ note }}
          </p>
        </template>
      </div>

      <template #footer>
        <template v-if="wizardStep === 1">
          <el-button @click="wizardStep = 0">上一步</el-button>
          <el-button
            type="primary"
            :loading="templateStore.importing"
            :disabled="!checkedCodes.length"
            @click="doImport"
          >
            导入 {{ checkedCodes.length }} 条
          </el-button>
        </template>
        <el-button v-else @click="wizardVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.admin__toolbar {
  display: flex;
  gap: var(--pwc-space-3);
  margin-bottom: var(--pwc-space-4);
}

.admin__hint {
  color: var(--pwc-text-secondary);
  font-size: var(--pwc-font-body-s);
}

.admin__rule :deep(textarea),
.rule-builder__mono :deep(textarea),
.rule-builder__mono :deep(input) {
  font-family: Consolas, Monaco, monospace;
}

.rule-builder {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: var(--pwc-space-3);
}

.rule-builder__bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.rule-builder__grid {
  display: flex;
  gap: var(--pwc-space-3);
  flex-wrap: wrap;
}

.rule-builder__operand {
  display: flex;
  gap: var(--pwc-space-2);
  align-items: center;
  flex-wrap: wrap;
  padding: var(--pwc-space-2) 0;
  border-top: 1px dashed var(--pwc-border, #dfe3e6);
}

.rule-builder__filter-help {
  margin-top: 0;
}

.admin__restriction-row {
  display: flex;
  gap: var(--pwc-space-3);
  margin-bottom: var(--pwc-space-3);
  align-items: center;
}

.admin__wizard-body {
  min-height: 200px;
  margin-top: var(--pwc-space-5);
}

.admin__industry {
  cursor: pointer;
  margin-bottom: var(--pwc-space-4);
}

.admin__industry:hover {
  border-color: var(--pwc-bg-brand);
}

.admin__metric-check {
  display: block;
  margin: 0;
  padding: var(--pwc-space-2) 0;
}

.admin__note {
  color: var(--pwc-text-secondary);
  font-size: var(--pwc-font-body-s);
}
</style>
