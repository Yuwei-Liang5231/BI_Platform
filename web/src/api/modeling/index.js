import request from "@/config/request";

/**
 * 自动建模建议（B13）：只读建议接口，零副作用。
 * 入库一律由确认流向导调用既有 relations / metrics 写接口完成。
 *
 * 大数据集下后端需逐文本列 DISTINCT 扫描（48 万行 × 多列）+ 可选 LLM 语义复审
 * （自带 60s 超时），全局 30s 必超——单独放宽到 5 分钟。
 */
export const fetchModelingSuggestions = (projectId) =>
  request.post("/modeling/suggestions", { project_id: projectId ?? null }, { timeout: 300000 });
