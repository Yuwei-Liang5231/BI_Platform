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

/** 字段语义标注建议：按列名+类型+样本值生成每列业务含义备注（P2）。
 *  body: { datasetId } → { annotations: {列名: 备注}, llm_configured } */
export const suggestSemanticAnnotations = (datasetId) =>
  request.post("/ai/dataset-semantic-annotations", { dataset_id: datasetId });

/** 字段语义标注落库：整组替换（空对象清空；管理员）。
 *  body: { annotations: {列名: 备注} } */
export const saveSemanticAnnotations = (datasetId, annotations) =>
  request.put(`/datasets/${datasetId}/semantic-annotations`, { annotations });

/** 归因下钻 AI 解读：根节点第一层 TopN 贡献的一句话解读（P2）。
 *  body: { metricId, start, end, dimensions, compare? } → { interpretation, source, llm_configured, reason }
 *  timeout 70s：后端 LLM 调用上限 60s，不能小于它（否则前端先掐断、永久静默失败） */
export const attributeInterpretation = (data) =>
  request.post(
    "/ai/attribute-interpretation",
    {
      metric_id: data.metricId,
      start: data.start,
      end: data.end,
      dimensions: data.dimensions,
      compare: data.compare ?? "mom",
    },
    { timeout: 70000 }
  );
