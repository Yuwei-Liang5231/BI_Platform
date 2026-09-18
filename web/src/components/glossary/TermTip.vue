<!-- 上下文名词提示：小问号图标，hover 显示词条定义。term 传 constants/glossary.js 中的词条 key -->
<script setup>
import { computed } from "vue";
import { QuestionFilled } from "@element-plus/icons-vue";
import { glossaryTerm } from "@/constants/glossary";

const props = defineProps({
  term: { type: String, required: true },
  placement: { type: String, default: "top" },
});

const info = computed(() => glossaryTerm(props.term));
</script>

<template>
  <el-tooltip
    v-if="info"
    placement="top"
    effect="light"
    :show-after="100"
  >
    <template #content>
      <div class="term-tip">
        <div class="term-tip__title">{{ info.title }}</div>
        <div class="term-tip__brief">{{ info.brief }}</div>
        <div class="term-tip__detail">{{ info.detail }}</div>
        <div class="term-tip__more">完整说明见右上角「名词解释」</div>
      </div>
    </template>
    <el-icon
      class="term-tip__icon"
      :size="14"
      aria-label="名词解释"
      role="button"
      tabindex="0"
    >
      <QuestionFilled />
    </el-icon>
  </el-tooltip>
</template>

<style scoped>
.term-tip__icon {
  color: var(--pwc-text-tertiary, #8c8c8c);
  cursor: help;
  vertical-align: -2px;
  margin-left: 4px;
}

.term-tip__icon:hover {
  color: var(--pwc-primary, #d04a02);
}

.term-tip {
  max-width: 320px;
  line-height: 1.6;
}

.term-tip__title {
  font-weight: 600;
  margin-bottom: 2px;
}

.term-tip__brief {
  margin-bottom: 4px;
}

.term-tip__detail {
  color: var(--pwc-text-secondary, #535353);
  font-size: 12px;
}

.term-tip__more {
  margin-top: 4px;
  font-size: 12px;
  color: var(--pwc-primary, #d04a02);
}
</style>
