<!-- pwc-regime: product-ui -->
<script setup>
/**
 * 指标管理（analyst/admin）：CRUD（保存即编译校验）+ 从模板导入向导 +
 * 受限可见性配置（role/department 负向登记，整组替换）。
 */
import { computed, onMounted, reactive, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";

import {
  batchDeleteMetrics,
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
import { useProjectStore } from "@/stores/project";
import { useTemplateStore } from "@/stores/template";

const auth = useAuthStore();
const metricStore = useMetricStore();
const datasetStore = useDatasetStore();
const projectStore = useProjectStore();
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
  flat: {
    table: "", column: "", aggregation: "sum", filter: "", time_field: "", time_none: false, time_confirmed: false,
    // 结构化相对时间条件（2026-09-16）：以计算当天为锚点，生成 today 文法并入 filter
    timeCond: { on: false, column: "", kind: "up_to_today", n: 7, range: null },
  },
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

/* ---------- 相对时间条件：文法片段生成 + filter 文本回填解析 ---------- */

const TIME_KINDS = [
  { value: "before_today", label: "今天之前", frag: (c) => `${c} < today` },
  { value: "up_to_today", label: "今天及之前", frag: (c) => `${c} <= today` },
  { value: "after_today", label: "今天之后", frag: (c) => `${c} > today` },
  { value: "from_today", label: "今天及之后", frag: (c) => `${c} >= today` },
  { value: "next_n_days", label: "未来 N 天内", frag: (c, n) => `${c} >= today AND ${c} <= today+${n}d` },
  { value: "last_n_days", label: "过去 N 天内", frag: (c, n) => `${c} >= today-${n}d AND ${c} <= today` },
  { value: "custom_range", label: "自定义日期区间", frag: (c, r) => `${c} >= '${r[0]}' AND ${c} <= '${r[1]}'` },
];
const TIME_KIND_NEEDS_N = new Set(["next_n_days", "last_n_days"]);

function quoteFilterCol(name) {
  return /^[\w\u4e00-\u9fff.-]+$/.test(name) ? name : `"${name.replace(/"/g, '""')}"`;
}

function timeCondFragment(tc) {
  if (!tc.on) return { frag: "", error: "" };
  if (!tc.column) return { frag: "", error: "已启用相对时间条件：请选择日期列" };
  if (TIME_KIND_NEEDS_N.has(tc.kind) && (tc.n === null || tc.n === undefined || tc.n < 0))
    return { frag: "", error: "请填写相对时间的天数 N" };
  if (tc.kind === "custom_range" && (!Array.isArray(tc.range) || !tc.range[0] || !tc.range[1]))
    return { frag: "", error: "请选择自定义日期区间" };
  const kind = TIME_KINDS.find((k) => k.value === tc.kind);
  return { frag: kind.frag(quoteFilterCol(tc.column), tc.kind === "custom_range" ? tc.range : tc.n), error: "" };
}

// 按 AND 分割条件段（跳过单引号字符串内的 AND）
function splitFilterAnd(text) {
  const parts = [];
  let buf = "";
  let inStr = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (ch === "'") {
      inStr = !inStr;
      buf += ch;
      continue;
    }
    if (!inStr) {
      const m = text.slice(i).match(/^(\s+AND\s+)/i);
      if (m) {
        parts.push(buf.trim());
        buf = "";
        i += m[1].length - 1;
        continue;
      }
    }
    buf += ch;
  }
  if (buf.trim()) parts.push(buf.trim());
  return parts;
}

const RE_COND_SIMPLE = /^("[^"]+"|[^\s]+?)\s*(<=|>=|<|>)\s*today$/i;
const RE_COND_OFFSET = /^("[^"]+"|[^\s]+?)\s*(<=|>=|<|>)\s*today\s*([+-])\s*(\d+)\s*d$/i;

function unquoteFilterCol(c) {
  return c.startsWith('"') && c.endsWith('"') ? c.slice(1, -1).replace(/""/g, '"') : c;
}

/** 从 filter 文本中抽取可回填的相对时间条件（首组命中）。
 *  单条件（today 无偏移）四种方向 + 连续双段窗口（未来/过去 N 天）可回填；
 *  带偏移的单条件、自定义区间等留在文法框（合法可编辑）。 */
function extractTimeCond(filterText) {
  const empty = { timeCond: null, rest: (filterText ?? "").trim() };
  if (!filterText || !filterText.trim()) return empty;
  const segs = splitFilterAnd(filterText);
  const tc = { on: true, column: "", kind: "", n: 7, range: null };
  const colOf = (m) => unquoteFilterCol(m[1]);

  for (let i = 0; i < segs.length; i++) {
    const winA = segs[i].match(RE_COND_SIMPLE);
    const winB = i + 1 < segs.length ? segs[i + 1].match(RE_COND_OFFSET) : null;
    // 未来 N 天：C >= today AND C <= today+Nd
    if (winA && winB && colOf(winA) === colOf(winB) && winA[2] === ">=" && winA[3] === undefined
        && winB[2] === "<=" && winB[3] === "+") {
      Object.assign(tc, { column: colOf(winA), kind: "next_n_days", n: Number(winB[4]) });
      return { timeCond: tc, rest: [...segs.slice(0, i), ...segs.slice(i + 2)].join(" AND ") };
    }
    // 过去 N 天：C >= today-Nd AND C <= today
    const winA2 = segs[i].match(RE_COND_OFFSET);
    const winB2 = i + 1 < segs.length ? segs[i + 1].match(RE_COND_SIMPLE) : null;
    if (winA2 && winB2 && colOf(winA2) === colOf(winB2) && winA2[2] === ">=" && winA2[3] === "-"
        && winB2[2] === "<=") {
      Object.assign(tc, { column: colOf(winA2), kind: "last_n_days", n: Number(winA2[4]) });
      return { timeCond: tc, rest: [...segs.slice(0, i), ...segs.slice(i + 2)].join(" AND ") };
    }
    // 单条件：today（无偏移）
    const m = segs[i].match(RE_COND_SIMPLE);
    if (m) {
      const kindByOp = { "<": "before_today", "<=": "up_to_today", ">": "after_today", ">=": "from_today" };
      Object.assign(tc, { column: colOf(m), kind: kindByOp[m[2]] });
      return { timeCond: tc, rest: [...segs.slice(0, i), ...segs.slice(i + 1)].join(" AND ") };
    }
  }
  return empty;
}

function resetTimeCond() {
  Object.assign(builder.flat.timeCond, { on: false, column: "", kind: "up_to_today", n: 7, range: null });
}

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
      time_none: false,
      time_confirmed: false,
    });
    resetTimeCond();
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
    // 先抽出可回填的相对时间条件，剩余条件留在文法输入框
    const { timeCond, rest } = extractTimeCond(rule.source?.filter);
    Object.assign(builder.flat, {
      table: rule.source?.table ?? "",
      column: rule.source?.column ?? "",
      aggregation: rule.base_aggregation ?? "sum",
      filter: rest,
      time_field: rule.time_field ?? "",
      // 规则里 time_field 显式为 null = 已声明的全期常数指标，编辑时不得被自动补填，
      // 也不再次弹确认框（time_confirmed）
      time_none: rule.time_field === null,
      time_confirmed: rule.time_field === null,
    });
    if (timeCond) {
      Object.assign(builder.flat.timeCond, timeCond);
    } else {
      resetTimeCond();
    }
    // 旧规则未声明 time_field（键缺失）：列加载后自动补填，避免保存时被当成显式常数
    if (rule.time_field === undefined) autoPickTimeField(builder.flat.table);
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
    // 结构化相对时间条件 + 文法框条件合并（时间条件在前）
    const { frag, error } = timeCondFragment(f.timeCond);
    if (error) throw new Error(error);
    const merged = [frag, f.filter.trim()].filter(Boolean).join(" AND ");
    if (merged) rule.source.filter = merged;
    if (f.time_field.trim()) {
      rule.time_field = f.time_field.trim();
    } else if (f.time_none || Array.isArray(datasetColumns[f.table])) {
      // 用户显式清空（time_none）或数据集无日期列：显式声明全期常数（不按时间过滤）。
      // 列信息尚未加载时不写 time_field 键，保持后端自动解析语义，避免误改口径。
      rule.time_field = null;
    }
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
  } catch (e) {
    ElMessage.error(e instanceof SyntaxError ? "计算规则不是合法 JSON" : (e?.message ?? "计算规则构建失败"));
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
        type,
        isDate: type.includes("date") || /date|日期|_at$|_dt$/i.test(name),
      };
    });
    autoPickTimeField(tableName);
  } catch {
    datasetColumns[tableName] = [];
  }
}

/* 时间字段自动补填：有日期列时选一个最可能的（写时间类词 > datetime 类型 > 靠前），
   选择在下拉框中可见可更改，既保证开箱即用，又不掩盖口径选择。
   留空（用户显式清空）或数据集无日期列 → buildRule 写 time_field: null，
   指标为「全期常数」：不按时间过滤，任意区间返回同一全期汇总值。 */
const TIME_FIELD_PRIORITY = /(write|create|update|occur|event|record|_time|time$)/i;

function autoPickTimeField(tableName) {
  if (builder.mode !== "flat" || builder.flat.table !== tableName) return;
  if (builder.flat.time_field || builder.flat.time_none) return;
  const cols = (datasetColumns[tableName] ?? []).filter((c) => c.isDate);
  if (!cols.length) return;
  const best = cols.length === 1 ? cols[0] : [...cols].sort((a, b) => score(b) - score(a))[0];
  builder.flat.time_field = best.name;
  builder.flat.time_none = false;
  builder.flat.time_confirmed = false;
  if (cols.length > 1) {
    ElMessage.info(`数据集「${tableName}」有 ${cols.length} 个日期列，已自动选择时间字段「${best.name}」，可手动更改`);
  }
  function score(c) {
    let s = 0;
    if (TIME_FIELD_PRIORITY.test(c.name)) s += 2;
    if (c.type === "datetime") s += 1;
    return s;
  }
}

function onFlatTableChange(tableName) {
  builder.flat.column = "";
  builder.flat.time_field = "";
  builder.flat.time_none = false;
  builder.flat.time_confirmed = false;
  resetTimeCond(); // 换数据集：相对时间条件的日期列随表失效
  loadColumns(tableName); // 未缓存时内部会再调 autoPickTimeField
  autoPickTimeField(tableName); // 已缓存时 loadColumns 提前返回，这里补一次
}

const flatDateCols = computed(() =>
  (datasetColumns[builder.flat.table] ?? []).filter((c) => c.isDate),
);

const flatTimePlaceholder = computed(() => {
  const n = flatDateCols.value.length;
  if (n > 1) return `时间字段（该数据集有 ${n} 个日期列；留空则全期汇总，不按时间过滤）`;
  if (n === 1) return "时间字段（留空则不按时间过滤，返回全期汇总值）";
  return Array.isArray(datasetColumns[builder.flat.table])
    ? "时间字段（该数据集无日期列，指标为全期常数）"
    : "时间字段（可选；留空则不按时间过滤）";
});

/* 试编译/保存前的本地预检：留空时间字段是合法选择（全期常数指标），
   但属于易被忽视的口径差异——弹确认框让用户知情，而不是静默生效或一刀切拦截。 */
async function confirmTimeFieldChoice() {
  const colsLoaded = Array.isArray(datasetColumns[builder.flat.table]);
  if (
    builder.mode === "flat" &&
    builder.flat.table &&
    colsLoaded &&
    !builder.flat.time_confirmed && // 已确认过（或编辑既有常数指标）不重复弹窗
    !builder.flat.time_field.trim() &&
    flatDateCols.value.length >= 1
  ) {
    try {
      await ElMessageBox.confirm(
        `数据集「${builder.flat.table}」有日期列（${flatDateCols.value
          .map((c) => c.name)
          .join("、")}）但未选择时间字段：指标将作为「全期常数」——不按时间过滤，` +
          "任意统计区间返回同一全期汇总值，看板卡片无环比、无逐日折线。是否继续？",
        "未选择时间字段",
        { confirmButtonText: "创建全期常数指标", cancelButtonText: "返回选择", type: "warning" },
      );
      builder.flat.time_confirmed = true; // 本次会话内确认过，不再重复弹窗
    } catch {
      return false; // 用户返回选择时间字段
    }
  }
  return true;
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
    time_none: false,
    time_confirmed: false,
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
  const pid = projectStore.currentId;
  const params = { status: statusFilter.value, ...(pid ? { project_id: pid } : {}) };
  if (search.value) params.search = search.value;
  await metricStore.fetchList(params);
}

// B9.3：切换项目重新拉取
watch(() => projectStore.currentId, fetchData);

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
  if (!(await confirmTimeFieldChoice())) return;
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
      // B9.3：归入当前项目；「全部项目」视图下新建 → 默认项目（后端缺省语义）
      ...(projectStore.currentId ? { project_id: projectStore.currentId } : {}),
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

/* ---------- 批量删除（多选/全选） ---------- */
const selectedMetrics = ref([]);

async function handleBatchDelete() {
  const rows = selectedMetrics.value;
  if (!rows.length) return;
  await ElMessageBox.confirm(
    `确定删除选中的 ${rows.length} 个指标？删除后目录与搜索不可见（软删除，留痕保留）。`,
    "批量删除确认",
    { type: "warning", confirmButtonText: "全部删除", cancelButtonText: "取消" },
  );
  const res = await batchDeleteMetrics({ ids: rows.map((r) => r.id) });
  const { deleted = [], failed = [] } = res ?? {};
  if (failed.length) {
    ElMessage.warning(`已删除 ${deleted.length} 个，${failed.length} 个失败：${failed.map((f) => f.id).join("、")}`);
  } else {
    ElMessage.success(`已删除 ${deleted.length} 个指标`);
  }
  selectedMetrics.value = [];
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
  // B9.3：导入状态按当前项目判定（全部项目视图 → 默认项目）
  const pid = projectStore.currentId ?? projectStore.defaultId;
  const pack = await templateStore.fetchPack(industry, pid ? { project_id: pid } : {});
  packMetrics.value = pack?.metrics ?? (Array.isArray(pack) ? pack : []);
  // 未导入 + 已导入但 pending（待绑定数据）的指标可勾选：pending 重新导入可升级启用
  checkedCodes.value = packMetrics.value
    .filter((m) => !m.imported || m.imported_status === "pending")
    .map((m) => m.code);
  wizardStep.value = 1;
}

async function doImport() {
  try {
    const res = await templateStore.importTemplates({
      industries: [chosenIndustry.value],
      codes: checkedCodes.value,
      revalidate: true, // 已存在且 pending 的指标：数据集就绪后可编译则升级为 active
      // B9.3：导入指标挂当前项目（全部项目视图 → 默认项目）
      ...(projectStore.currentId ? { project_id: projectStore.currentId } : {}),
    });
    importResult.value = res;
    wizardStep.value = 2;
    await fetchData();
  } catch {
    /* 失败原因由全局拦截器 toast；向导留在本步允许重试，避免未处理异常 */
  }
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
          <el-button
            type="danger"
            plain
            :disabled="!selectedMetrics.length"
            @click="handleBatchDelete"
          >
            批量删除{{ selectedMetrics.length ? `（${selectedMetrics.length}）` : "" }}
          </el-button>
        </div>
        <el-table
          v-loading="metricStore.loading"
          :data="metricStore.list"
          row-key="id"
          @selection-change="selectedMetrics = $event"
        >
          <el-table-column type="selection" width="42" reserve-selection />
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
                  : "直接编辑 JSON（完整能力，含跨表 operand 级 time_field；规则级 time_field 置 null 即「全期常数」不按时间过滤）。切换回表单模式前需为合法 JSON。" }}
            </p>

            <!-- 简单聚合 -->
            <template v-if="builder.mode === 'flat'">
              <div class="rule-builder__grid">
                <el-select
                  v-model="builder.flat.table"
                  placeholder="数据集"
                  style="width: 180px"
                  @change="onFlatTableChange"
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
              <!-- 相对时间条件（today 锚点）：结构化点选，生成文法并入过滤条件 -->
              <div class="rule-builder__time-cond">
                <el-checkbox v-model="builder.flat.timeCond.on">相对时间条件</el-checkbox>
                <template v-if="builder.flat.timeCond.on">
                  <el-select
                    v-model="builder.flat.timeCond.column"
                    placeholder="日期列"
                    style="width: 150px"
                    filterable
                  >
                    <el-option v-for="c in flatDateCols" :key="c.name" :label="c.name" :value="c.name" />
                  </el-select>
                  <el-select v-model="builder.flat.timeCond.kind" style="width: 160px">
                    <el-option v-for="k in TIME_KINDS" :key="k.value" :label="k.label" :value="k.value" />
                  </el-select>
                  <el-input-number
                    v-if="TIME_KIND_NEEDS_N.has(builder.flat.timeCond.kind)"
                    v-model="builder.flat.timeCond.n"
                    :min="0"
                    :max="3650"
                    style="width: 120px"
                  />
                  <el-date-picker
                    v-if="builder.flat.timeCond.kind === 'custom_range'"
                    v-model="builder.flat.timeCond.range"
                    type="daterange"
                    value-format="YYYY-MM-DD"
                    start-placeholder="开始"
                    end-placeholder="结束"
                    style="width: 240px"
                  />
                </template>
              </div>
              <p v-if="builder.flat.timeCond.on" class="admin__hint">
                以计算当天为锚点，指标值随日期自动滚动（时点性指标）；环比/同比基期沿用同一锚点。建议在名称或口径说明中体现滚动性质（如「截至今天」）。
              </p>
              <el-input
                v-model="builder.flat.filter"
                placeholder="过滤条件（可选），如：order_status = '已支付'"
                class="rule-builder__mono"
              />
              <el-select
                :model-value="builder.flat.time_field ?? ''"
                @update:model-value="(v) => { builder.flat.time_field = v ?? ''; builder.flat.time_none = !v; if (v) builder.flat.time_confirmed = false; }"
                :placeholder="flatTimePlaceholder"
                clearable filterable
              >
                <el-option
                  v-for="c in flatDateCols"
                  :key="c.name"
                  :label="c.name"
                  :value="c.name"
                />
              </el-select>
              <p v-if="!builder.flat.time_field && flatDateCols.length >= 1" class="admin__hint admin__hint--warn">
                未选择时间字段：指标将为「全期常数」——不按时间过滤，任意统计区间返回同一全期汇总值，看板卡片无环比、无逐日折线。
              </p>
              <p v-else-if="flatDateCols.length === 0 && Array.isArray(datasetColumns[builder.flat.table])" class="admin__hint admin__hint--warn">
                该数据集没有日期列：指标将为「全期常数」——不按时间过滤，任意统计区间返回同一全期汇总值。
              </p>
              <p v-if="flatDateCols.length > 1" class="admin__hint admin__hint--warn">
                该数据集有 {{ flatDateCols.length }} 个日期列（{{ flatDateCols.map((c) => c.name).join("、") }}），
                不同日期列的时间范围可能差异很大，请确认所选时间字段符合业务口径。
              </p>
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
                  placeholder="过滤（可选，支持 today±N d）"
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
              过滤条件文法：仅支持「字段 比较符 值」，多个条件用 AND 连接；比较符 = != > >= < <=，文本值加单引号，列名含空格时用双引号包裹（如 "Project Name" = 'X'）。支持相对日期 today、today±N d（如 due_date &lt;= today+15d，锚定计算当天，值随日期滚动）。不支持 OR / NOT / 括号 / 函数。
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
            :disabled="m.imported && m.imported_status !== 'pending'"
            class="admin__metric-check"
          >
            {{ m.name }}（{{ m.code }}）
            <span v-if="m.imported && m.imported_status === 'pending'">
              · 待绑定数据，勾选后重新导入即可启用
            </span>
            <span v-else-if="m.imported"> · 已导入</span>
          </el-checkbox>
        </el-checkbox-group>
        <p v-if="!checkedCodes.length" class="admin__hint">
          该行业模板指标均已导入且启用，无需再次导入。
        </p>
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
            「待绑定数据」指标暂不在指标目录出现；上传对应数据集后重新执行本向导，勾选待启用指标导入即可自动启用。
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

.admin__hint--warn {
  color: var(--el-color-warning);
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

.rule-builder__time-cond {
  display: flex;
  gap: var(--pwc-space-3);
  align-items: center;
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
