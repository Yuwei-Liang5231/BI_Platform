import { ref } from "vue";
import { defineStore } from "pinia";

import { exportMetric as exportApi, metricValue as metricValueApi } from "@/api/query";

export const useQueryStore = defineStore("query", () => {
  const result = ref(null);
  const loading = ref(false);

  /**
   * 单值计算。metric 兼容 id 与 code。
   * 返回含 value / compare / change / period_complete 等（B3 契约）。
   */
  async function fetchValue(payload) {
    loading.value = true;
    try {
      result.value = await metricValueApi(payload);
      return result.value;
    } finally {
      loading.value = false;
    }
  }

  function exportCsv(payload) {
    return exportApi(payload);
  }

  return { result, loading, fetchValue, exportCsv };
});
