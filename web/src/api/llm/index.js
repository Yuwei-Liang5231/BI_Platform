import request from "@/config/request";

// LLM 模型管理（admin）：多模型登记 / 切换启用 / 连通性测试

export const listLlmModels = () => request.get("/llm/models");

export const createLlmModel = (data) => request.post("/llm/models", data);

export const updateLlmModel = (modelId, data) =>
  request.put(`/llm/models/${modelId}`, data);

export const deleteLlmModel = (modelId) =>
  request.delete(`/llm/models/${modelId}`);

export const activateLlmModel = (modelId) =>
  request.post(`/llm/models/${modelId}/activate`);

export const testLlmModel = (data) => request.post("/llm/models/test", data);
