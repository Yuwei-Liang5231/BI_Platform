<!-- 全局「名词解释」抽屉：按分类展示全部词条，支持搜索。词条数据源见 constants/glossary.js -->
<script setup>
import { computed, ref, watch } from "vue";
import { Search } from "@element-plus/icons-vue";
import {
  GLOSSARY_CATEGORIES,
  GLOSSARY_TERMS,
  glossaryCategoryLabel,
} from "@/constants/glossary";

const visible = defineModel({ type: Boolean, default: false });
const keyword = ref("");

watch(visible, (v) => {
  if (!v) keyword.value = "";
});

const groups = computed(() => {
  const kw = keyword.value.trim().toLowerCase();
  const matched = kw
    ? GLOSSARY_TERMS.filter(
        (t) =>
          t.title.toLowerCase().includes(kw) ||
          t.brief.toLowerCase().includes(kw) ||
          t.detail.toLowerCase().includes(kw),
      )
    : GLOSSARY_TERMS;
  return GLOSSARY_CATEGORIES.map((c) => ({
    ...c,
    terms: matched.filter((t) => t.category === c.key),
  })).filter((g) => g.terms.length);
});
</script>

<template>
  <el-drawer
    v-model="visible"
    title="名词解释"
    direction="rtl"
    size="480px"
    class="glossary-drawer"
  >
    <el-input
      v-model="keyword"
      placeholder="搜索名词或说明…"
      :prefix-icon="Search"
      clearable
      class="glossary__search"
    />
    <el-empty
      v-if="!groups.length"
      description="没有匹配的名词"
      :image-size="80"
    />
    <div v-for="g in groups" :key="g.key" class="glossary__group">
      <h4 class="glossary__cat">{{ g.label }}</h4>
      <div v-for="t in g.terms" :key="t.key" class="glossary__term">
        <div class="glossary__term-title">{{ t.title }}</div>
        <p class="glossary__term-brief">{{ t.brief }}</p>
        <p class="glossary__term-detail">{{ t.detail }}</p>
      </div>
    </div>
  </el-drawer>
</template>

<style scoped>
.glossary__search {
  margin-bottom: var(--pwc-space-4, 16px);
}

.glossary__group {
  margin-bottom: var(--pwc-space-4, 20px);
}

.glossary__cat {
  font-size: var(--pwc-font-body-m, 14px);
  font-weight: 600;
  color: var(--pwc-primary, #d04a02);
  border-left: 3px solid var(--pwc-primary, #d04a02);
  padding-left: 8px;
  margin: 0 0 var(--pwc-space-2, 8px);
}

.glossary__term {
  padding: var(--pwc-space-2, 8px) 0 var(--pwc-space-3, 12px);
  border-bottom: 1px solid var(--pwc-border, #e5e7eb);
}

.glossary__term:last-child {
  border-bottom: none;
}

.glossary__term-title {
  font-weight: 600;
  margin-bottom: 4px;
}

.glossary__term-brief {
  margin: 0 0 4px;
  font-size: var(--pwc-font-body-s, 13px);
  color: var(--pwc-text-primary, #262626);
}

.glossary__term-detail {
  margin: 0;
  font-size: var(--pwc-font-body-s, 13px);
  color: var(--pwc-text-secondary, #535353);
  line-height: 1.6;
}
</style>
