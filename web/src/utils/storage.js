/**
 * 本地存储统一入口（页面禁止直接操作 localStorage）。
 * token 持久化在 localStorage，用户信息持久化在 sessionStorage。
 */
const TOKEN_KEY = "bi_token";
const USER_KEY = "bi_user";

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) ?? "";
}

export function setToken(token) {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

export function getUser() {
  const raw = sessionStorage.getItem(USER_KEY);
  return raw ? JSON.parse(raw) : null;
}

export function setUser(user) {
  sessionStorage.setItem(USER_KEY, JSON.stringify(user));
}

export function clearUser() {
  sessionStorage.removeItem(USER_KEY);
}
