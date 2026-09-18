import { ref } from "vue";
import { defineStore } from "pinia";

import {
  addRelation as addRelationApi,
  batchDeleteDatasets as batchRemoveApi,
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
import { useProjectStore } from "@/stores/project";

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
      // B9.3：默认按当前项目过滤（全部项目视图 currentId=null → 不过滤看全部）；
      // 调用方显式传 project_id 时以调用方为准
      const projectStore = useProjectStore();
      const merged = {
        ...(projectStore.currentId ? { project_id: projectStore.currentId } : {}),
        ...params,
      };
      const res = await listApi(merged);
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

  /** 批量删除（0a7b61a 补漏）：返回 { deleted:[], failed:[{id,name,reason}] } */
  function batchRemove(ids) {
    return batchRemoveApi(ids);
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
    batchRemove,
    addRelation,
    removeRelation,
  };
});
