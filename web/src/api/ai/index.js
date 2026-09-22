import request from "@/config/request";

/**
 * AI 能力接口（P1 批：看板速览 / 异动假设 / 口径助手）。
 * 全部走算写分离 + 降级：LLM 未配置/失败时后端返回规则兜底或空字段，前端按需展示。
 */

/** 看板速览：周期内项目/指定指标的智能摘要。
 *  query: { projectId?, start, end, metricIds? } */
export const dashboardSummary = (params) =>
  request.get("/ai/dashboard-summary", {
    params: {
      project_id: params.projectId ?? undefined,
      start: params.start,
      end: params.end,
      metric_ids: params.metricIds ?? undefined,
    },
  });

/** 异动 AI 假设解释：基于检测为异动的指标，按维度归因后生成中性假设。
 *  query: { metricId, start, end, compare?, dimensions? } */
export const anomalyHypothesis = (params) =>
  request.get("/ai/anomaly-hypothesis", {
    params: {
      metric_id: params.metricId,
      start: params.start,
      end: params.end,
      compare: params.compare ?? "mom",
      dimensions: params.dimensions ?? undefined,
    },
  });

/** 指标口径 AI 助手：基于字段+聚合+样本值生成名称/别名/结构化口径说明。
 *  body: { datasetId, column, aggregation, alias?, sampleValues? } */
export const suggestCalcNotes = (data) =>
  request.post("/ai/metric-calc-notes", {
    dataset_id: data.datasetId,
    column: data.column,
    aggregation: data.aggregation,
    alias: data.alias ?? null,
    sample_values: data.sampleValues ?? null,
  });
