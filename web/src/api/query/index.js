import request from "@/config/request";

/**
 * 单值计算。body: { metric: id|code, start, end, compare: none|mom|yoy }
 * 返回 { value, compare, change, period_complete, data_through, coverage, ... }。
 * 契约 v2（2026-09-11）：覆盖与区间相交即返回真实值；周期未完整时
 * period_complete=false + data_through=数据截止日（前端标注"数据截至"）；
 * 仅区间与覆盖无交集或聚合为空时 value=null → 前端显示"区间无数据"。
 */
export const metricValue = (data) => request.post("/query/metric-value", data);

/** 问数（B9）：问句 → 理解卡（只解析意图，不产数值）。
 *  B9.2-3：命中多个指标时返回 multi_metrics 并列清单。
 *  B9.2-6：conversationId 每轮携带实现多轮追问与持久化（首问 null，后端自动建会话）。
 *  B9.3：projectId 锁定当前项目（候选/新会话归属该项目；null=不过滤，兼容存量调用）。 */
export const ask = (question, conversationId, projectId) =>
  request.post("/query/ask", {
    question,
    conversation_id: conversationId ?? null,
    project_id: projectId ?? null,
  });

/** 空态推荐问题（B9.2-3）：可见指标自动生成的示例问法（≤5 条）。 */
export const askSuggestions = (projectId) =>
  request.get("/query/ask/suggestions", {
    params: projectId ? { project_id: projectId } : undefined,
  });

/** 历史会话列表（B9.2-6）：最近 50 次对话，按最近使用排序。 */
export const askConversations = (projectId) =>
  request.get("/query/ask/conversations", {
    params: projectId ? { project_id: projectId } : undefined,
  });

/** 恢复会话消息（B9.2-6）：问句 + 理解卡/结果快照按序返回（只读历史）。 */
export const askConversationMessages = (conversationId) =>
  request.get(`/query/ask/conversations/${conversationId}/messages`);

/** 删除会话及其消息（B9.2-6）。 */
export const askConversationDelete = (conversationId) =>
  request.delete(`/query/ask/conversations/${conversationId}`);

/** 重命名会话（B9.2-6）。 */
export const askConversationRename = (conversationId, title) =>
  request.patch(`/query/ask/conversations/${conversationId}`, { title });

/** 理解卡确认后执行（与看板同一计算出口，口径同源）。
 *  B9.2-2：带 dimension 时走拆解出口（filters/order_by/order/top_n 生效）。 */
export const askExecute = (data) => request.post("/query/ask/execute", data);

/** 候选拆解维度列（B9.2-1）：主数据集与一跳可达维度表的文本列 + 现算基数。 */
export const breakdownDimensions = (data) =>
  request.post("/query/breakdown-dimensions", data);

/** 单指标反常性检测（B10-1）。body: { metric, date? } */
export const anomalyDetect = (data) => request.post("/query/anomaly", data);

/** 项目批量异动扫描（B10-1，总览页/看板黄条数据源）。
 *  逐指标序列计算，配置指标多时较慢——放宽超时至 3 分钟。 */
export const anomalyScan = (projectId) =>
  request.get("/query/anomalies", {
    params: projectId ? { project_id: projectId } : undefined,
    timeout: 180000,
  });

/** 单层归因（B10-2）：变化量按维度拆贡献（加性指标）。 */
export const attributeDelta = (data) => request.post("/query/attribute", data);

/** 理解卡筛选值候选（B9.2-2）：{ metric, column } → 维度列真实取值清单（≤50）。 */
export const askDimensionValues = (data) => request.post("/query/ask/dimension-values", data);

/** 拿 CSV blob（UTF-8 BOM），不触发浏览器下载——画序列图等内部用途走这个。 */
export const exportCsvBlob = (data) =>
  request.post("/query/export", data, { responseType: "blob" });

/** 导出 CSV 并触发浏览器下载（仅用户点「导出」按钮时调用）。 */
export const exportMetric = async (data) => {
  const response = await exportCsvBlob(data);
  const { headers, data: blob } = response;
  const disposition = headers["content-disposition"] || "";
  const match = disposition.match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i);
  const filename = match ? decodeURIComponent(match[1]) : "export.csv";
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
};
