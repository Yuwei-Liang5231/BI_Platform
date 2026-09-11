<!-- pwc-regime: product-ui -->
<script setup>
/**
 * 数据集与表关系（V1.1）：上传接入、字段预览、覆盖区间、
 * 表关系可视化配置（左表·字段 → 右表·字段、关联类型）。
 * 上传/关系写操作仅 admin（后端 AdminUser 强制）。
 */
import { computed, onMounted, reactive, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";

import { useAuthStore } from "@/stores/auth";
import { useDatasetStore } from "@/stores/dataset";

const auth = useAuthStore();
const datasetStore = useDatasetStore();

const selectedId = ref(null);
const detail = ref(null);
const previewRows = ref([]);
const detailLoading = ref(false);

const uploadVisible = ref(false);
const uploadFile = ref(null);
const uploadFiles = ref([]); // 与 el-upload 内部列表双向绑定（单一事实来源）
const uploadName = ref("");
const uploadPercent = ref(0);
const uploadError = ref(false);
const uploading = ref(false); // 提交全程锁（含 100% 后服务器解析等待），防重复提交/误关对话框

const relationVisible = ref(false);
const relationForm = reactive({
  from_column: "",
  target_dataset_id: null,
  target_column: "",
  relation_type: "many_to_one",
});
// 与后端契约对齐（datasets.py VALID_RELATION_TYPES）：值为枚举、标签用业务符号
const RELATION_TYPES = [
  { value: "one_to_one", label: "1:1（一对一）" },
  { value: "one_to_many", label: "1:n（左表一条对右表多条）" },
  { value: "many_to_one", label: "n:1（左表多条对右表一条）" },
  { value: "many_to_many", label: "n:n（多对多）" },
];
const relationLabel = (v) => RELATION_TYPES.find((t) => t.value === v)?.label ?? v;

const selected = computed(() => datasetStore.list.find((d) => d.id === selectedId.value) ?? null);

// 详情接口带 columns / coverage；列表接口无
const columns = computed(() => {
  if (!detail.value) return [];
  const raw = detail.value.columns ?? [];
  return raw.map((c) => (typeof c === "string" ? { name: c } : c));
});

const coverages = computed(() => detail.value?.coverage ?? []);

const targetName = (id) => datasetStore.list.find((d) => d.id === id)?.name ?? `#${id}`;

// 关系登记：右表字段列表（懒加载所选右表的列）
const targetColumns = computed(() => {
  if (!relationForm.target_dataset_id) return [];
  if (detail.value && detail.value.id === relationForm.target_dataset_id) {
    return columns.value;
  }
  // 其余数据集需要拉详情；保持同步简单实现：点击时由 watcher 拉取
  return targetColumnsCache.value[relationForm.target_dataset_id] ?? [];
});

const targetColumnsCache = ref({});

watch(
  () => relationForm.target_dataset_id,
  async (id) => {
    if (!id) return;
    relationForm.target_column = "";
    if (targetColumnsCache.value[id]) return;
    try {
      const res = await datasetStore.fetchDetail(id);
      const raw = res?.columns ?? [];
      targetColumnsCache.value[id] = raw.map((c) => (typeof c === "string" ? { name: c } : c));
    } catch {
      targetColumnsCache.value[id] = [];
    }
  },
);

async function fetchData() {
  await datasetStore.fetchList();
  if (selectedId.value && !datasetStore.list.some((d) => d.id === selectedId.value)) {
    selectedId.value = null;
  }
  if (selectedId.value) await loadDetail(selectedId.value);
}

async function selectDataset(id) {
  selectedId.value = id;
  await loadDetail(id);
}

async function loadDetail(id) {
  detailLoading.value = true;
  try {
    const { getDataset } = await import("@/api/datasets");
    detail.value = await getDataset(id);
    const res = await datasetStore.fetchPreview(id);
    previewRows.value = res?.rows ?? [];
    await datasetStore.fetchRelations(id);
  } finally {
    detailLoading.value = false;
  }
}

function openUpload() {
  uploadFile.value = null;
  uploadFiles.value = []; // 同步清空 el-upload 内部文件列表，防「看似已选实则未选」
  uploadName.value = "";
  uploadPercent.value = 0;
  uploadVisible.value = true;
}

async function handleUpload() {
  if (uploading.value) return; // 防双击/重复提交竞态
  const raw = uploadFiles.value[0]?.raw;
  if (!raw || !uploadName.value.trim()) {
    ElMessage.warning("请填写数据集名称并选择文件");
    return;
  }
  uploadError.value = false;
  uploading.value = true;
  try {
    const created = await datasetStore.upload(
      raw,
      uploadName.value.trim(),
      (p) => (uploadPercent.value = p),
    );
    ElMessage.success(`已接入：${created.name ?? uploadName.value}`);
    uploadVisible.value = false;
    uploadFiles.value = [];
    uploadFile.value = null;
    uploadName.value = "";
    await fetchData();
  } catch {
    // 失败保持对话框打开便于重试；进度条复位，错误提示由 request 拦截器统一给出
    uploadError.value = true;
  } finally {
    uploading.value = false;
    uploadPercent.value = 0;
  }
}

async function handleDelete(row) {
  await ElMessageBox.confirm(`确定删除数据集「${row.name}」？`, "删除确认", { type: "warning" });
  await datasetStore.remove(row.id);
  ElMessage.success("已删除");
  await fetchData();
}

function openRelation() {
  relationForm.from_column = "";
  relationForm.target_dataset_id = null;
  relationForm.target_column = "";
  relationForm.relation_type = "many_to_one";
  relationVisible.value = true;
}

async function saveRelation() {
  if (!relationForm.from_column || !relationForm.target_dataset_id || !relationForm.target_column) {
    ElMessage.warning("请完整选择左列与右表·右列");
    return;
  }
  await datasetStore.addRelation(selectedId.value, {
    from_column: relationForm.from_column,
    target_dataset_id: relationForm.target_dataset_id,
    target_column: relationForm.target_column,
    relation_type: relationForm.relation_type,
  });
  ElMessage.success("关系已注册");
  relationVisible.value = false;
  await datasetStore.fetchRelations(selectedId.value);
}

async function removeRelation(rel) {
  await ElMessageBox.confirm("确定删除该表关系？", "删除确认", { type: "warning" });
  await datasetStore.removeRelation(selectedId.value, rel.id);
  ElMessage.success("已删除");
  await datasetStore.fetchRelations(selectedId.value);
}

onMounted(fetchData);
</script>

<template>
  <div class="page-container">
    <div class="page-header">
      <div>
        <h1 class="page-header__title">数据集与表关系</h1>
        <p class="page-header__subtitle">接入即注册 · 关系显式登记 · 覆盖区间驱动"留空"</p>
      </div>
      <div class="page-header__actions">
        <el-button type="primary" :disabled="!auth.isAdmin" @click="openUpload">上传接入</el-button>
        <el-button
          :disabled="!auth.isAdmin || !selectedId"
          @click="openRelation"
        >
          登记表关系
        </el-button>
      </div>
    </div>

    <div class="grid-12">
      <!-- 数据集列表 -->
      <section class="pwc-card col-span-4">
        <div class="pwc-card__header">
          <h4>数据集（{{ datasetStore.list.length }}）</h4>
        </div>
        <el-table
          v-loading="datasetStore.loading"
          :data="datasetStore.list"
          highlight-current-row
          @row-click="(row) => selectDataset(row.id)"
        >
          <el-table-column prop="name" label="名称" min-width="140" />
          <el-table-column prop="row_count" label="行数" width="90" />
          <el-table-column prop="encoding" label="编码" width="90" />
          <el-table-column label="操作" width="70" fixed="right">
            <template #default="{ row }">
              <el-button text type="danger" :disabled="!auth.isAdmin" @click.stop="handleDelete(row)">
                删
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </section>

      <!-- 详情 -->
      <section class="pwc-card col-span-8" v-loading="detailLoading">
        <div class="pwc-card__header">
          <h4>{{ selected?.name ?? "选择左侧数据集查看详情" }}</h4>
          <span v-if="detail" class="pwc-badge pwc-badge--grey">
            v{{ detail.dataset_ver }} · {{ detail.row_count }} 行 × {{ detail.column_count }} 列
          </span>
        </div>

        <template v-if="detail">
          <!-- 覆盖区间 -->
          <h5 class="ds__subhead">覆盖区间（留空机制依据）</h5>
          <el-table :data="coverages" size="small" style="width: 100%">
            <el-table-column prop="column" label="时间列" width="160" />
            <el-table-column prop="period_start" label="起" width="120" />
            <el-table-column prop="period_end" label="止" width="120" />
            <el-table-column prop="non_null_count" label="非空行数" width="110" />
          </el-table>

          <!-- 表关系 -->
          <h5 class="ds__subhead">已登记表关系</h5>
          <el-table :data="datasetStore.relations" size="small" style="width: 100%">
            <el-table-column label="左表.字段" min-width="180">
              <template #default>{{ selected?.name }}.{{ scopeUndefined }}</template>
            </el-table-column>
            <el-table-column label="右表.字段" min-width="180">
              <template #default="{ row }">
                {{ targetName(row.target_dataset_id) }}.{{ row.target_column }}
              </template>
            </el-table-column>
            <el-table-column prop="relation_type" label="类型" width="90">
              <template #default="{ row }">{{ relationLabel(row.relation_type).slice(0, 3) }}</template>
            </el-table-column>
            <el-table-column label="操作" width="80">
              <template #default="{ row }">
                <el-button text type="danger" :disabled="!auth.isAdmin" @click="removeRelation(row)">
                  删除
                </el-button>
              </template>
            </el-table-column>
          </el-table>

          <!-- 字段预览 -->
          <h5 class="ds__subhead">字段（{{ columns.length }}）与样例（{{ previewRows.length }} 行）</h5>
          <el-table :data="previewRows" size="small" style="width: 100%" max-height="320">
            <el-table-column
              v-for="col in columns"
              :key="col.name"
              :prop="col.name"
              :label="col.name"
              min-width="120"
            />
          </el-table>
        </template>
        <el-empty v-else description="点击左侧数据集查看字段与覆盖区间" />
      </section>
    </div>

    <!-- 上传 -->
    <el-dialog
      v-model="uploadVisible"
      title="上传接入"
      width="520px"
      :close-on-click-modal="!uploading"
      :close-on-press-escape="!uploading"
      :show-close="!uploading"
    >
      <el-form label-width="90px">
        <el-form-item label="数据集名">
          <el-input v-model="uploadName" placeholder="如 orders" :disabled="uploading" />
        </el-form-item>
        <el-form-item label="文件">
          <el-upload
            v-model:file-list="uploadFiles"
            :auto-upload="false"
            :limit="1"
            accept=".csv,.xlsx,.xls,.txt"
            :disabled="uploading"
            drag
          >
            <div class="ds__upload-hint">拖拽 CSV / Excel 到此，或点击选择</div>
          </el-upload>
        </el-form-item>
      </el-form>
      <el-progress v-if="uploadPercent > 0" :percentage="uploadPercent" />
      <p v-if="uploadPercent >= 100" class="ds__upload-parsing">
        文件已送达，服务器解析中（数十万行约需 1~2 分钟），请勿关闭…
      </p>
      <p v-if="uploadError" class="ds__upload-parsing ds__upload-parsing--error">
        上一次尝试未完成：若为超时，数据可能已实际入库，请先在列表中核实再重试（重名将自动加后缀）。
      </p>
      <template #footer>
        <el-button :disabled="uploading" @click="uploadVisible = false">取消</el-button>
        <el-button type="primary" :disabled="uploading" @click="handleUpload">
          {{ uploading ? "上传中…" : "上传" }}
        </el-button>
      </template>
    </el-dialog>

    <!-- 表关系登记 -->
    <el-dialog v-model="relationVisible" title="登记表关系" width="560px">
      <el-form label-width="110px">
        <el-form-item label="左表.字段">
          <el-select v-model="relationForm.from_column" style="width: 100%">
            <el-option v-for="c in columns" :key="c.name" :label="c.name" :value="c.name" />
          </el-select>
        </el-form-item>
        <el-form-item label="右表">
          <el-select v-model="relationForm.target_dataset_id" style="width: 100%">
            <el-option
              v-for="d in datasetStore.list.filter((x) => x.id !== selectedId)"
              :key="d.id"
              :label="d.name"
              :value="d.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="右表.字段">
          <el-select v-model="relationForm.target_column" style="width: 100%">
            <el-option
              v-for="c in targetColumns"
              :key="c.name"
              :label="c.name"
              :value="c.name"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="关联类型">
          <el-select v-model="relationForm.relation_type" style="width: 100%">
            <el-option v-for="t in RELATION_TYPES" :key="t.value" :label="t.label" :value="t.value" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="relationVisible = false">取消</el-button>
        <el-button type="primary" @click="saveRelation">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.ds__subhead {
  font-family: var(--pwc-font-sans);
  font-weight: 600;
  margin: var(--pwc-space-5) 0 var(--pwc-space-3);
}

.ds__upload-hint {
  padding: var(--pwc-space-5);
  color: var(--pwc-text-secondary);
}

.ds__upload-parsing {
  margin: var(--pwc-space-2) 0 0;
  font-size: 12px;
  color: var(--pwc-text-secondary);
}

.ds__upload-parsing--error {
  color: var(--pwc-danger, #d04a02);
}
</style>
