import request from "@/config/request";

// 上传需服务端流式解析入库（数十万行可超 30s），单独放宽超时（B14.1 单遍解析
// 改造后 200MB 级约 2~5 分钟，留足余量）；其余接口沿用全局 30s。
export const uploadDataset = (formData, onProgress) =>
  request.post("/datasets/upload", formData, {
    headers: { "Content-Type": "multipart/form-data" },
    timeout: 900000,
    onUploadProgress: (e) => {
      if (onProgress && e.total) onProgress(Math.round((e.loaded / e.total) * 100));
    },
  });

export const listDatasets = (params) => request.get("/datasets", { params });

export const getDataset = (datasetId) => request.get(`/datasets/${datasetId}`);

export const renameDataset = (datasetId, name) =>
  request.patch(`/datasets/${datasetId}/name`, { name });

export const getPreview = (datasetId) =>
  request.get(`/datasets/${datasetId}/preview`);

export const getDatasetQuality = (datasetId) =>
  request.get(`/datasets/${datasetId}/quality`);

// 增量导入（append）与全量覆盖（replace）同走此接口；大文件放宽超时
export const importDatasetData = (datasetId, formData, onProgress) =>
  request.post(`/datasets/${datasetId}/data`, formData, {
    headers: { "Content-Type": "multipart/form-data" },
    timeout: 900000,
    onUploadProgress: (e) => {
      if (onProgress && e.total) onProgress(Math.round((e.loaded / e.total) * 100));
    },
  });

export const getColumnAnomalies = (datasetId, columnName) =>
  request.get(`/datasets/${datasetId}/columns/${columnName}/anomalies`);

export const deleteDataset = (datasetId) =>
  request.delete(`/datasets/${datasetId}`);

export const addRelation = (datasetId, data, config) =>
  request.post(`/datasets/${datasetId}/relations`, data, config);

export const listRelations = (datasetId) =>
  request.get(`/datasets/${datasetId}/relations`);

export const deleteRelation = (datasetId, relationId) =>
  request.delete(`/datasets/${datasetId}/relations/${relationId}`);
