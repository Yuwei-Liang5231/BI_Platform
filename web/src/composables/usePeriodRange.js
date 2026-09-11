import { computed, ref } from "vue";
import { isoDate } from "@/utils/format";

/**
 * 统计周期选择（看板/目录/详情共用）。
 * 默认上一个完整自然月（保证 period_complete，环比口径稳定），支持自定义区间。
 */
export function usePeriodRange() {
  const now = new Date();
  const _default = [
    isoDate(new Date(now.getFullYear(), now.getMonth() - 1, 1)),
    isoDate(new Date(now.getFullYear(), now.getMonth(), 0)),
  ];
  // [start, end]，YYYY-MM-DD 字符串（与 el-date-picker value-format 对齐）
  const dateRange = ref([..._default]);
  const range = computed(() => ({
    start: dateRange.value?.[0],
    end: dateRange.value?.[1],
  }));
  function resetToDefault() {
    dateRange.value = [..._default];
  }
  return { dateRange, range, resetToDefault };
}
