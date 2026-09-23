<!-- pwc-regime: product-ui -->
<script setup>
/**
 * 常驻 AI 洞察条（A2）：每日定时洞察（P5/A1）在页面上的常驻呈现——
 * 打开看板/总览即自动看到「最近最值得关注的几件事」，无需点铃铛、无需点按钮。
 *
 * 数据 = GET /ai/insights（最近 3 天、按指标去重取最新、权限同源：受限指标隐藏）。
 * 空态默认不渲染（宁缺毋滥）；总览页传 show-empty 展示弱提示，
 * admin 可一键生成今日洞察（有 LLM 时逐条生成可能需要一两分钟）。
 * 红涨绿跌契约：▲ 红 / ▼ 绿，只保留三角，配文字。
 */
import { onMounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";

import { listAiInsights, runDailyInsight } from "@/api/ai";
import AiFeedback from "@/components/business/AiFeedback.vue";
import { formatMetricValue } from "@/utils/format";
import { useAuthStore } from "@/stores/auth";

const props = defineProps({
  projectId: { type: [Number, null], default: null },
  showEmpty: { type: Boolean, default: false },
  // 紧凑模式（看板页用）：默认收起为一行摘要 chips，与异动黄条同高，
  // 点「展开」才显示 AI 解释/正文/反馈——避免洞察太多把关键指标卡顶下去
  compact: { type: Boolean, default: false },
});

const router = useRouter();
const auth = useAuthStore();

const loaded = ref(false);
const running = ref(false);
const expanded = ref(false);
const items = ref([]);
const generatedToday = ref(false);

async function load() {
  try {
    const res = await listAiInsights({ projectId: props.projectId });
    items.value = res?.items ?? [];
    generatedToday.value = !!res?.generated_today;
  } catch {
    items.value = []; // 拉取失败不阻塞页面，只是不展示
  } finally {
    loaded.value = true;
  }
}

// 切项目重新拉取（洞察按项目隔离）
watch(
  () => props.projectId,
  () => {
    loaded.value = false;
    load();
  },
);

async function handleRun() {
  running.value = true;
  try {
    const res = await runDailyInsight();
    const created = res?.created ?? 0;
    ElMessage.success(
      created > 0 ? `已生成 ${created} 条今日洞察` : "今日洞察已是最新（重复执行幂等）",
    );
    await load();
  } catch {
    /* 拦截器已提示 */
  } finally {
    running.value = false;
  }
}

const gotoMetric = (item) => router.push(`/metrics/${item.metric_id}`);

// 标题形如「每日洞察：「指标名」上升（2026-09-22）」——条内去前缀保持紧凑
const shortTitle = (t) => (t || "").replace(/^每日洞察：/, "");

onMounted(load);
</script>

<template>
  <section
    v-if="loaded && (items.length || showEmpty)"
    class="pwc-card insight-bar"
    :class="{ 'is-compact': compact && !expanded }"
  >
    <div class="pwc-card__header insight-bar__head">
      <h4 class="insight-bar__title">AI 每日洞察</h4>

      <!-- 紧凑收起态：一行摘要 chips（点击直达指标详情），不把关键信息顶下去 -->
      <template v-if="compact && !expanded && items.length">
        <span
          v-for="item in items"
          :key="item.id"
          class="insight-bar__chip"
          role="link"
          :title="`展开查看 AI 解释（${item.abnormality ?? '—'}σ）`"
          @click="gotoMetric(item)"
        >
          <span class="insight-bar__chip-arrow" :class="item.direction === 'up' ? 'is-up' : 'is-down'">
            {{ item.direction === "up" ? "▲" : "▼" }}
          </span>
          {{ item.metric_name }}
          <span v-if="item.current !== null && item.current !== undefined" class="insight-bar__chip-num">
            {{ formatMetricValue(item.current) }}
          </span>
        </span>
      </template>
      <span v-else class="insight-bar__sub">后台每日自动扫描 · 限量呈现最值得关注的事</span>

      <el-button
        v-if="compact && items.length"
        link
        type="primary"
        size="small"
        class="insight-bar__toggle"
        @click="expanded = !expanded"
      >
        {{ expanded ? "收起" : `展开 ${items.length} 条` }}
      </el-button>
    </div>

    <template v-if="!compact || expanded">
      <div v-for="item in items" :key="item.id" class="insight-bar__item">
        <div class="insight-bar__row">
          <span class="insight-bar__arrow" :class="item.direction === 'up' ? 'is-up' : 'is-down'">
            {{ item.direction === "up" ? "▲" : "▼" }}
            {{ item.direction === "up" ? "上升" : "下降" }}
          </span>
          <span class="insight-bar__metric" role="link" @click="gotoMetric(item)">
            {{ item.metric_name }}
          </span>
          <span v-if="item.current !== null && item.current !== undefined" class="insight-bar__num">
            {{ formatMetricValue(item.current) }}
          </span>
          <span v-if="item.abnormality !== null && item.abnormality !== undefined" class="insight-bar__sigma">
            {{ item.abnormality }}σ
          </span>
          <el-tag
            :type="item.source === 'llm' ? 'success' : 'info'"
            effect="light"
            size="small"
          >
            {{ item.source === "llm" ? "AI 解释" : "规则文案" }}
          </el-tag>
          <span class="insight-bar__date">{{ item.insight_date.slice(5) }}</span>
        </div>
        <p class="insight-bar__body">{{ item.body }}</p>
        <AiFeedback
          kind="daily_insight"
          :target="`insight:${item.id}`"
          :project-id="projectId"
          class="insight-bar__fb"
        />
      </div>

      <!-- 空态（仅 showEmpty 页面展示）：admin 可手动补跑，viewer 只读提示 -->
      <div v-if="!items.length" class="insight-bar__empty">
        <template v-if="auth.isAdmin">
          <span>{{ generatedToday ? "今日洞察已生成，但暂无你可见的内容" : "今日暂无 AI 洞察" }}</span>
          <el-button
            v-if="!generatedToday"
            size="small"
            type="primary"
            :loading="running"
            @click="handleRun"
          >
            立即生成今日洞察
          </el-button>
          <span v-if="!generatedToday" class="insight-bar__empty-hint">（配置了 AI 模型时约需一两分钟）</span>
        </template>
        <template v-else>
          <span>{{ generatedToday ? "今日洞察已生成，但暂无你可见的内容" : "今日暂无 AI 洞察（后台每日自动生成，也可留意站内通知）" }}</span>
        </template>
      </div>
    </template>
  </section>
</template>

<style scoped>
.insight-bar {
  margin-bottom: var(--pwc-space-4);
}

.insight-bar__head {
  display: flex;
  align-items: baseline;
  gap: var(--pwc-space-3);
}

.insight-bar__title {
  display: flex;
  align-items: center;
}

.insight-bar__sub {
  font-size: var(--pwc-font-body-s);
  font-weight: 400;
  color: var(--pwc-text-secondary);
}

.insight-bar__item {
  padding: var(--pwc-space-2) 0;
  border-bottom: 1px solid var(--pwc-border-color, #eef0f2);
}

.insight-bar__item:last-of-type {
  border-bottom: none;
}

.insight-bar__row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--pwc-space-2);
}

/* 红涨绿跌契约：涨=红、跌=绿，只保留三角 */
.insight-bar__arrow.is-up {
  color: var(--pwc-up);
  font-weight: 700;
}

.insight-bar__arrow.is-down {
  color: var(--pwc-down);
  font-weight: 700;
}

.insight-bar__metric {
  font-weight: 700;
  cursor: pointer;
}

.insight-bar__metric:hover {
  color: var(--pwc-bg-brand);
}

.insight-bar__num {
  font-weight: 700;
}

.insight-bar__sigma {
  font-size: var(--pwc-font-body-s);
  color: var(--pwc-text-secondary);
}

.insight-bar__date {
  margin-left: auto;
  font-size: var(--pwc-font-body-s);
  color: var(--pwc-text-secondary);
}

.insight-bar__body {
  margin: var(--pwc-space-1) 0 0;
  font-size: var(--pwc-font-body-s);
  color: var(--pwc-text-secondary);
  white-space: pre-line;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.insight-bar__fb {
  margin-left: 0;
  margin-top: 2px;
}

.insight-bar__empty {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--pwc-space-2);
  padding: var(--pwc-space-2) 0;
  font-size: var(--pwc-font-body-s);
  color: var(--pwc-text-secondary);
}

.insight-bar__empty-hint {
  color: var(--pwc-text-secondary);
  opacity: 0.75;
}

/* 紧凑收起态：一行摘要，压掉卡片内边距，与异动黄条同高量级 */
.insight-bar.is-compact {
  padding: var(--pwc-space-2) var(--pwc-space-4);
}

.insight-bar.is-compact .insight-bar__head {
  min-height: 28px;
}

.insight-bar__chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 10px;
  border-radius: 999px;
  background: var(--pwc-bg-muted, #f5f6f7);
  font-size: var(--pwc-font-body-s);
  font-weight: 600;
  cursor: pointer;
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.insight-bar__chip:hover {
  background: color-mix(in srgb, var(--pwc-bg-brand) 12%, transparent);
  color: var(--pwc-bg-brand);
}

.insight-bar__chip-arrow.is-up {
  color: var(--pwc-up);
}

.insight-bar__chip-arrow.is-down {
  color: var(--pwc-down);
}

.insight-bar__chip-num {
  font-weight: 400;
  color: var(--pwc-text-secondary);
}

.insight-bar__toggle {
  margin-left: auto;
}
</style>
