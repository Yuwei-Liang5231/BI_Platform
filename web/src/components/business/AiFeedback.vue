<!-- pwc-regime: product-ui -->
<script setup>
/**
 * AI 输出反馈入口（P4-2 反馈闭环）。
 *
 * 每条 AI 产出旁给「有用 / 无用」；点无用时展开可选的人工修正输入框。
 * 零阻塞：提交失败静默（AI 主流程不受影响），成功显示致谢态。
 */
import { ref } from "vue";
import { submitAiFeedback } from "@/api/ai";

const props = defineProps({
  kind: { type: String, required: true },
  target: { type: String, default: "" },
  projectId: { type: [Number, null], default: null },
});

const state = ref("idle"); // idle | correction | done
const correction = ref("");

async function send(rating) {
  if (rating === "down" && state.value === "idle") {
    state.value = "correction";
    return;
  }
  const payload = {
    kind: props.kind,
    target: props.target,
    rating,
    correction: rating === "down" ? correction.value : "",
    projectId: props.projectId,
  };
  state.value = "done"; // 乐观置位：反馈不阻塞阅读
  try {
    await submitAiFeedback(payload);
  } catch {
    /* 静默 */
  }
}
</script>

<template>
  <span class="ai-fb">
    <template v-if="state === 'idle'">
      <span class="ai-fb__label">这条 AI 结论</span>
      <el-button link type="primary" size="small" @click="send('up')">有用</el-button>
      <el-button link type="danger" size="small" @click="send('down')">无用</el-button>
    </template>
    <template v-else-if="state === 'correction'">
      <el-input
        v-model="correction"
        size="small"
        maxlength="200"
        placeholder="哪里不对？可填写正确说法（选填）"
        class="ai-fb__input"
      />
      <el-button link type="danger" size="small" @click="send('down')">提交</el-button>
      <el-button link size="small" @click="send('up')">算了，有用</el-button>
    </template>
    <span v-else class="ai-fb__done">感谢反馈</span>
  </span>
</template>

<style scoped>
.ai-fb {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  flex-wrap: wrap;
  margin-left: var(--pwc-space-2, 8px);
  vertical-align: middle;
}
.ai-fb__label {
  font-size: var(--pwc-font-body-s, 13px);
  color: var(--pwc-text-secondary, #535353);
  margin-right: 2px;
}
.ai-fb__input {
  width: 240px;
  margin-right: 4px;
}
.ai-fb__done {
  font-size: var(--pwc-font-body-s, 13px);
  color: var(--pwc-text-secondary, #535353);
}
</style>
