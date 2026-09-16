import { ref } from "vue";
import { defineStore } from "pinia";

import {
  addRelation as addRelationApi,
  deleteDataset as deleteDatasetApi,
  deleteRelation as deleteRelationApi,
  getDataset as getApi,
  getColumnAnomalies as getAnomaliesApi,
  getPreview as getPreviewApi,
  listDatasets as listApi,
  listRelations as listRelationsApi,
  renameDataset as renameApi,
  uploadDataset as uploadApi,
} from "@/api/datasets";

export const useDatasetStore = defineStore("dataset", () => {
  const list = ref([]);
  const detail = ref(null);
  const loading = ref(false);
  const preview = ref(null);
  const relations = ref([]);
  const anomalies = ref(null);

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

  async function fetchDetail(datasetId) {
    detail.value = await getApi(datasetId);
    return detail.value;
  }

  async function fetchPreview(datasetId) {
    preview.value = await getPreviewApi(datasetId);
    return preview.value;
  }

  async function fetchRelations(datasetId) {
    relations.value = await listRelationsApi(datasetId);
    return relations.value;
  }

  async function fetchAnomalies(datasetId, columnName) {
    anomalies.value = await getAnomaliesApi(datasetId, columnName);
    return anomalies.value;
  }

  function upload(file, name, onProgress, projectId) {
    const formData = new FormData();
    formData.append("file", file);
    formData.append("name", name);
    if (projectId) formData.append("project_id", String(projectId)); // B9.3 项目归属
    return uploadApi(formData, onProgress);
  }

  function rename(datasetId, name) {
    return renameApi(datasetId, { name });
  }

  function remove(datasetId) {
    return deleteDatasetApi(datasetId);
  }

  function addRelation(datasetId, payload) {
    return addRelationApi(datasetId, payload);
  }

  function removeRelation(datasetId, relationId) {
    return deleteRelationApi(datasetId, relationId);
  }

  return {
    list,
    detail,
    loading,
    preview,
    relations,
    anomalies,
    fetchList,
    fetchDetail,
    fetchPreview,
    fetchRelations,
    fetchAnomalies,
    upload,
    rename,
    remove,
    addRelation,
    removeRelation,
  };
});
