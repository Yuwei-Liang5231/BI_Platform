<!-- pwc-regime: product-ui -->
<script setup>
/**
 * 经营总览（B10-3）：看板不等人来找问题。
 * 异动卡片（当前值/正常水平/变化/异动度）限量 ≤5 条宁缺毋滥；
 * 空态明确「本期无异动」；点击卡片进指标详情；
 * 卡片展开调归因接口显示主要来源（Top3 + 其他）。
 */
import { computed, nextTick, onMounted, reactive, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";

import {
  anomalyConfigsList,
  anomalyConfigsSave,
  anomalyEnableAll,
  anomalyScan,
  attributeDelta,
  breakdownDimensions,
} from "@/api/query";
import { listNotifications, markRead } from "@/api/notifications";
import { glossaryTerm } from "@/constants/glossary";
import { formatMetricValue } from "@/utils/format";
import { useProjectStore } from "@/stores/project";

const router = useRouter();
const projectStore = useProjectStore();

const loading = ref(true);
const scan = ref(null); // { counts, anomalies }
const attribution = reactive({}); // { [metricId]: { loading, data, error } }

async function fetchScan() {
  loading.value = true;
  try {
    scan.value = await anomalyScan(projectStore.lockedId);
  } finally {
    loading.value = false;
  }
}

// 切换项目重新扫描（含清空已加载的归因状态，避免跨项目残留）
watch(
  () => projectStore.lockedId,
  () => {
    Object.keys(attribution).forEach((k) => delete attribution[k]);
    fetchScan();
  },
);

async function loadAttribution(item) {
  const state = attribution[item.metric_id];
  if (state?.data || state?.loading) return;
  attribution[item.metric_id] = { loading: true, data: null, error: "" };
  try {
    // 归因需要维度列：取该指标第一个可拆解维度（拆解出口同一候选逻辑）
    const dims = await breakdownDimensions({ metric: item.metric_id });
    const column = dims?.dimensions?.[0]?.column;
    if (!column) {
      attribution[item.metric_id] = { loading: false, data: null, error: "该指标无可归因的维度列" };
      return;
    }
    // 归因区间 = 异动日所在的自然周（周一~周日）
    const d = new Date(item.date);
    const day = (d.getDay() + 6) % 7;
    const monday = new Date(d);
    monday.setDate(d.getDate() - day);
    const sunday = new Date(monday);
    sunday.setDate(monday.getDate() + 6);
    const iso = (x) =>
      `${x.getFullYear()}-${String(x.getMonth() + 1).padStart(2, "0")}-${String(x.getDate()).padStart(2, "0")}`;
    const data = await attributeDelta({
      metric: item.metric_id,
      start: iso(monday),
      end: iso(sunday),
      dimension: column,
      compare: "mom",
      top_n: 3,
    });
    attribution[item.metric_id] = { loading: false, data };
  } catch {
    attribution[item.metric_id] = {
      loading: false,
      data: null,
      error: "该指标暂无法归因（比率类或归因区间无数据）",
    };
  }
}

async function toggleDetail(item) {
  // 从总览进入详情时把对应通知标已读（小红点同步）——失败静默
  try {
    const unread = await listNotifications({ project_id: projectStore.lockedId, unread_only: true });
    const hit = (unread || []).find((n) => n.metric_id === item.metric_id);
    if (hit) await markRead(hit.id);
  } catch {
    /* 静默 */
  }
  router.push(`/metrics/${item.metric_id}`);
}

const directionText = (d) => (d === "up" ? "↑ 高于" : "↓ 低于");

const enabling = ref(false);

async function enableAll() {
  enabling.value = true;
  try {
    const res = await anomalyEnableAll(projectStore.lockedId);
    ElMessage.success(`已为 ${res.total} 个指标开启异动检测（本次新增 ${res.enabled_now} 个）`);
    await fetchScan();
  } finally {
    enabling.value = false;
  }
}

/* ---------- 检测指标管理（B10-3）：勾选开启/停用 ---------- */
const manageVisible = ref(false);
const manageLoading = ref(false);
const manageSaving = ref(false);
const metricItems = ref([]); // [{metric_id, code, name, configured, enabled}]
const checkedIds = ref([]);
const manageTableRef = ref(null);

async function openManage() {
  manageVisible.value = true;
  manageLoading.value = true;
  try {
    const res = await anomalyConfigsList(projectStore.lockedId);
    metricItems.value = res?.items ?? [];
    await nextTick();
    // 已开启的行预勾选（el-table 勾选需编程式设置）
    for (const row of metricItems.value) {
      if (row.enabled) manageTableRef.value?.toggleRowSelection(row, true);
    }
  } finally {
    manageLoading.value = false;
  }
}

async function saveManage() {
  manageSaving.value = true;
  try {
    await anomalyConfigsSave(checkedIds.value, projectStore.lockedId);
    ElMessage.success("检测指标已更新");
    manageVisible.value = false;
    await fetchScan();
  } finally {
    manageSaving.value = false;
  }
}

onMounted(fetchScan);
</script>

<template>
  <div class="page-container" v-loading="loading" element-loading-text="正在逐指标计算基准分布（同星期几基准 + z-score），指标越多耗时越长…">
    <div class="page-header">
      <div>
        <h1 class="page-header__title">经营总览</h1>
        <p class="page-header__subtitle">看板不等人来找问题 · 异动限量呈现，宁缺毋滥</p>
      </div>
      <div class="page-header__actions">
        <el-button @click="openManage" :disabled="loading">管理检测指标</el-button>
        <el-button @click="fetchScan" :loading="loading">重新扫描</el-button>
      </div>
    </div>

    <!-- 检测口径说明（常驻，文案引用统一术语表，避免多处维护） -->
    <el-alert type="info" :closable="false" class="overview__method">
      <template #title>
        检测口径：{{ glossaryTerm("anomaly")?.detail }}{{ glossaryTerm("materiality")?.brief }}逐指标阈值可在指标管理中调整。
      </template>
    </el-alert>

    <template v-if="scan">
      <p class="overview__summary">
        扫描 {{ scan.counts.configured }} 个开启检测的指标：
        <span class="overview__num overview__num--alert">{{ scan.counts.abnormal }} 个反常</span>、
        {{ scan.counts.normal }} 个正常、
        {{ scan.counts.insufficient_baseline }} 个基准不足、
        {{ scan.counts.no_data }} 个无数据
      </p>

      <el-empty
        v-if="!scan.anomalies.length"
        :description="scan.counts.configured === 0
          ? '尚未开启异动检测——开启后每次进入总览自动扫描，异动限量呈现、宁缺毋滥'
          : '本期无异动——所有开启检测的指标都在正常范围内'"
      >
        <el-button
          v-if="scan.counts.configured === 0"
          type="primary"
          :loading="enabling"
          @click="enableAll"
        >
          一键开启当前项目全部指标的异动检测
        </el-button>
      </el-empty>

      <div v-for="item in scan.anomalies" :key="item.metric_id" class="pwc-card overview__card">
        <div class="overview__head">
          <span class="overview__title" role="link" @click="toggleDetail(item)">
            {{ item.name }}（{{ item.metric_code }}）
          </span>
          <span class="overview__badge" :class="item.direction === 'up' ? 'is-up' : 'is-down'">
            {{ directionText(item.direction) }}正常水平
          </span>
        </div>
        <div class="overview__metrics">
          <div class="overview__metric">
            <span class="overview__metric-label">{{ item.date }} 值</span>
            <span class="overview__metric-value">{{ formatMetricValue(item.current) }}</span>
          </div>
          <div class="overview__metric">
            <span class="overview__metric-label">正常水平</span>
            <span class="overview__metric-value">
              {{ formatMetricValue(item.baseline?.mean) }}
              <span class="overview__metric-sub">±{{ formatMetricValue(item.baseline?.stdev) }}</span>
            </span>
          </div>
          <div class="overview__metric">
            <span class="overview__metric-label">变化</span>
            <span class="overview__metric-value">
              {{ item.delta > 0 ? "+" : "" }}{{ formatMetricValue(item.delta) }}
              <span class="overview__metric-sub" v-if="item.delta_pct !== null">
                （{{ item.delta_pct > 0 ? "+" : "" }}{{ item.delta_pct }}%）
              </span>
            </span>
          </div>
          <div class="overview__metric">
            <span class="overview__metric-label">异动度</span>
            <span class="overview__metric-value">{{ item.abnormality }}σ</span>
          </div>
        </div>
        <p class="overview__reason">{{ item.reason }}</p>
        <div class="overview__attr">
          <el-button text type="primary" size="small" @click="loadAttribution(item)">
            查看主要来源
          </el-button>
          <template v-if="attribution[item.metric_id]">
            <span v-if="attribution[item.metric_id].loading" class="overview__attr-loading">
              归因计算中…
            </span>
            <span v-else-if="attribution[item.metric_id].error" class="overview__attr-error">
              {{ attribution[item.metric_id].error }}
            </span>
            <div v-else-if="attribution[item.metric_id].data" class="overview__attr-body">
              <div
                v-for="c in attribution[item.metric_id].data.top_dimensions"
                :key="c.value"
                class="overview__attr-row"
              >
                <span>{{ c.value }}</span>
                <span>
                  {{ c.contribution > 0 ? "+" : "" }}{{ formatMetricValue(c.contribution) }}
                  <span v-if="c.contribution_pct !== null" class="overview__metric-sub">
                    （{{ c.contribution_pct > 0 ? "+" : "" }}{{ c.contribution_pct }}%）
                  </span>
                </span>
              </div>
            </div>
          </template>
        </div>
      </div>
    </template>

    <!-- 检测指标管理（B10-3）：勾选开启/停用 -->
    <el-dialog v-model="manageVisible" title="管理检测指标" width="560px">
      <p class="overview__method">
        勾选要监控的指标（建议只挑核心指标）：保存后每次进入总览自动扫描；
        取消勾选即停用（配置保留，可随时再勾回）。扫描阈值默认 3σ 保守档。
      </p>
      <el-table
        ref="manageTableRef"
        v-loading="manageLoading"
        :data="metricItems"
        row-key="metric_id"
        size="small"
        max-height="380"
        @selection-change="(rows) => (checkedIds = rows.map((r) => r.metric_id))"
      >
        <el-table-column type="selection" width="44" reserve-selection />
        <el-table-column prop="name" label="指标" min-width="140" />
        <el-table-column prop="code" label="编码" min-width="140" />
        <el-table-column label="检测状态" width="100">
          <template #default="{ row }">
            <span v-if="row.enabled" class="overview__num--alert">检测中</span>
            <span v-else style="color: var(--pwc-text-secondary)">未开启</span>
          </template>
        </el-table-column>
      </el-table>
      <template #footer>
        <el-button @click="manageVisible = false">取消</el-button>
        <el-button type="primary" :loading="manageSaving" @click="saveManage">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.overview__method {
  margin-bottom: var(--pwc-space-4);
}

.overview__summary {
  margin-bottom: var(--pwc-space-4);
  font-size: var(--pwc-font-body-s);
  color: var(--pwc-text-secondary);
}

.overview__num--alert {
  color: var(--pwc-up);
  font-weight: 700;
}

.overview__card {
  margin-bottom: var(--pwc-space-4);
  padding: var(--pwc-space-4);
}

.overview__head {
  display: flex;
  align-items: center;
  gap: var(--pwc-space-3);
  margin-bottom: var(--pwc-space-3);
}

.overview__title {
  font-weight: 700;
  cursor: pointer;
}

.overview__title:hover {
  color: var(--pwc-bg-brand);
}

.overview__badge {
  font-size: var(--pwc-font-body-s);
  padding: 2px 8px;
  border-radius: 999px;
}

.overview__badge.is-up {
  background: color-mix(in srgb, var(--pwc-up) 10%, transparent);
  color: var(--pwc-up);
}

.overview__badge.is-down {
  background: color-mix(in srgb, var(--pwc-down) 10%, transparent);
  color: var(--pwc-down);
}

.overview__metrics {
  display: flex;
  gap: var(--pwc-space-8);
  flex-wrap: wrap;
  margin-bottom: var(--pwc-space-2);
}

.overview__metric {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.overview__metric-label {
  font-size: var(--pwc-font-body-s);
  color: var(--pwc-text-secondary);
}

.overview__metric-value {
  font-size: var(--pwc-font-heading-m);
  font-weight: 700;
}

.overview__metric-sub {
  font-size: var(--pwc-font-body-s);
  font-weight: 400;
  color: var(--pwc-text-secondary);
}

.overview__reason {
  font-size: var(--pwc-font-body-s);
  color: var(--pwc-text-secondary);
  margin-bottom: var(--pwc-space-2);
}

.overview__attr-body {
  margin-top: var(--pwc-space-2);
  display: flex;
  flex-direction: column;
  gap: var(--pwc-space-1);
}

.overview__attr-row {
  display: flex;
  justify-content: space-between;
  max-width: 420px;
}

.overview__attr-error {
  font-size: var(--pwc-font-body-s);
  color: var(--pwc-text-secondary);
}
</style>
