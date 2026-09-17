import request from "@/config/request";

/** 报告模板列表（B12-1）。params: { project_id? } */
export const listReportTemplates = (params) =>
  request.get("/reports/templates", { params });

/** 新建报告模板。body: { name, period_type, metric_ids, sections?, project_id? } */
export const createReportTemplate = (data) => request.post("/reports/templates", data);

/** 更新报告模板。 */
export const updateReportTemplate = (id, data) =>
  request.put(`/reports/templates/${id}`, data);

/** 删除报告模板（admin）。 */
export const deleteReportTemplate = (id) => request.delete(`/reports/templates/${id}`);

/** 生成报告预览（无 LLM 完整可用版）。
 *  body: { template_id? | period_type+metric_ids+sections?, as_of?, project_id? }
 *  返回 { title, period, sections, conclusions, anomalies, attributions, narrative, refs } */
export const previewReport = (data) => request.post("/reports/preview", data, { timeout: 120000 });

/** 生成并存档（B12-3）：同模板×周期重复生成版本 +1。 */
export const generateReport = (data) => request.post("/reports/generate", data, { timeout: 120000 });

/** 历史存档列表（元信息）。params: { template_id?, project_id? } */
export const listReportInstances = (params) =>
  request.get("/reports/instances", { params });

/** 查看历史存档（快照正文）。 */
export const getReportInstance = (id) => request.get(`/reports/instances/${id}`);

/** 重新生成历史报告（同模板同周期锚点重算，版本 +1）。 */
export const regenerateReport = (id) =>
  request.post(`/reports/instances/${id}/regenerate`, null, { timeout: 120000 });
