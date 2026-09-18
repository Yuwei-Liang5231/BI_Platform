import { computed, ref } from "vue";
import { isoDate } from "@/utils/format";

/**
 * 统计周期选择（看板/目录/详情共用）。
 * 默认当前一整月（1 号 → 当月月末）：配合后端周期完整性契约（相交即返回 +
 * 「数据截至」标注 + 环比基期等长前移），超出数据截止日的部分不会误导
 * （2026-09-18 用户要求由"月初至今"改为整月）；支持自定义区间。
 */
export function usePeriodRange() {
  const now = new Date();
  const _default = [
    isoDate(new Date(now.getFullYear(), now.getMonth(), 1)),
    isoDate(new Date(now.getFullYear(), now.getMonth() + 1, 0)),
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
