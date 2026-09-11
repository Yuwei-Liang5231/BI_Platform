import request from "@/config/request";

export const listIndustries = () => request.get("/templates/industries");

export const getIndustry = (industry) =>
  request.get(`/templates/${industry}`);

/**
 * 幂等导入。body: { industries?, codes?, revalidate? }
 * 返回 { created, skipped, failed, upgraded, status_counts, notes, ... }（以 B5 契约为准）。
 */
export const importTemplates = (data) => request.post("/templates/import", data);
