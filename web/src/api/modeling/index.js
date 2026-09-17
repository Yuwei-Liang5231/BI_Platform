import request from "@/config/request";

/**
 * 自动建模建议（B13）：只读建议接口，零副作用。
 * 入库一律由确认流向导调用既有 relations / metrics 写接口完成。
 */
export const fetchModelingSuggestions = (projectId) =>
  request.post("/modeling/suggestions", { project_id: projectId ?? null });
