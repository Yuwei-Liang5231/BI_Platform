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

/** AI 输出反馈（P4-2）：有用/无用 + 人工修正。
 *  body: { kind, target, rating:'up'|'down', correction?, projectId? } */
export const submitAiFeedback = (data) =>
  request.post("/ai/feedback", {
    kind: data.kind,
    target: data.target ?? "",
    rating: data.rating,
    correction: data.correction ?? "",
    project_id: data.projectId ?? null,
  });

/** AI 质量概览（admin）：各功能赞踩 + 差评率 + 最近 bad case（按项目过滤，
 *  缺省 → 默认项目；跟随顶部项目切换器）。 */
export const aiFeedbackSummary = (params) =>
  request.get("/ai/feedback/summary", {
    params: { project_id: params?.projectId ?? undefined },
  });

/** AI 调用观测（admin，B2）：近 N 天（默认 30）各功能 LLM 调用成功/降级/审计剔除、
 *  耗时与 Token 用量聚合 + 最近异常明细（按项目过滤，跟随顶部项目切换器）。 */
export const aiObservability = (days = 30, projectId) =>
  request.get("/ai/observability", {
    params: {
      days,
      project_id: projectId ?? undefined,
    },
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

/** 常驻洞察条（A2）：最近 N 天的每日洞察，按指标去重取最新（权限同源，受限指标隐藏）。
 *  query: { projectId?, limit?, days? } → { items, generated_today, insight_date } */
export const listAiInsights = (params) =>
  request.get("/ai/insights", {
    params: {
      project_id: params.projectId ?? undefined,
      limit: params.limit ?? undefined,
      days: params.days ?? undefined,
    },
  });

/** 手动触发一次每日洞察（admin，幂等）：无 LLM 秒回；有 LLM 时逐条生成
 *  最坏 TOP N(3) × 60s，timeout 200s 必须覆盖它（同归因解读教训） */
export const runDailyInsight = () =>
  request.post("/ai/insight/run", null, { timeout: 200000 });

/** 多指标联动归因（A4）：同期与本指标相关性最高的 TOP N + AI 传播假设。
 *  query: { metricId, start, end, topN? } → { candidates, interpretation, rule_text, source, reason }
 *  timeout 70s：后端 LLM 调用上限 60s（同归因解读教训） */
export const metricLinkage = (params) =>
  request.get("/ai/metric-linkage", {
    params: {
      metric_id: params.metricId,
      start: params.start,
      end: params.end,
      top_n: params.topN ?? undefined,
    },
    timeout: 70000,
  });
