import request from "@/config/request";

export const createMetric = (data, config) => request.post("/metrics", data, config);

/** params: { search?, topic?, status? } */
export const listMetrics = (params) => request.get("/metrics", { params });

export const getMetric = (metricId) => request.get(`/metrics/${metricId}`);

export const updateMetric = (metricId, data) =>
  request.patch(`/metrics/${metricId}`, data);

export const deleteMetric = (metricId) => request.delete(`/metrics/${metricId}`);

export const getMetricSql = (metricId) => request.get(`/metrics/${metricId}/sql`);

export const getMetricChanges = (metricId) =>
  request.get(`/metrics/${metricId}/changes`);

export const tryCompile = (data) => request.post("/metrics/compile", data);
