import { ref } from "vue";
import { defineStore } from "pinia";

import {
  createMetric as createApi,
  deleteMetric as deleteApi,
  getMetric as getApi,
  getMetricChanges as getChangesApi,
  getMetricSql as getSqlApi,
  listMetrics as listApi,
  tryCompile as tryCompileApi,
  updateMetric as updateApi,
} from "@/api/metrics";

export const useMetricStore = defineStore("metric", () => {
  const list = ref([]);
  const loading = ref(false);
  const detail = ref(null);
  const sqlArchive = ref(null);
  const changes = ref([]);

  async function fetchList(params) {
    loading.value = true;
    try {
      const res = await listApi(params);
      list.value = Array.isArray(res) ? res : (res?.list ?? []);
      return list.value;
    } finally {
      loading.value = false;
    }
  }

  async function fetchDetail(metricId) {
    loading.value = true;
    try {
      detail.value = await getApi(metricId);
      return detail.value;
    } finally {
      loading.value = false;
    }
  }

  async function fetchSql(metricId) {
    sqlArchive.value = await getSqlApi(metricId);
    return sqlArchive.value;
  }

  async function fetchChanges(metricId) {
    changes.value = await getChangesApi(metricId);
    return changes.value;
  }

  function create(payload) {
    return createApi(payload);
  }

  function update(metricId, payload) {
    return updateApi(metricId, payload);
  }

  function remove(metricId) {
    return deleteApi(metricId);
  }

  function tryCompile(payload) {
    return tryCompileApi(payload);
  }

  return {
    list,
    loading,
    detail,
    sqlArchive,
    changes,
    fetchList,
    fetchDetail,
    fetchSql,
    fetchChanges,
    create,
    update,
    remove,
    tryCompile,
  };
});
