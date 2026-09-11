import request from "@/config/request";

/** 登录，返回 { token, user }（以 /auth/login 实际返回为准） */
export const login = (data) => request.post("/auth/login", data);

export const getMe = () => request.get("/auth/me");

export const listUsers = () => request.get("/auth/users");

export const createUser = (data) => request.post("/auth/users", data);

export const updateUser = (userId, data) =>
  request.patch(`/auth/users/${userId}`, data);

export const getRestrictions = (metricId) =>
  request.get(`/auth/metrics/${metricId}/restrictions`);

/** items=[] 清空（整组替换语义） */
export const putRestrictions = (metricId, items) =>
  request.put(`/auth/metrics/${metricId}/restrictions`, { items });
