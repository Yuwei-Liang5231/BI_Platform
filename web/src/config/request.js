import axios from "axios";
import { ElMessage } from "element-plus";

import { clearToken, getToken } from "@/utils/storage";

/**
 * Axios 统一封装。
 *
 * 后端契约（架构 D11 / core/response.py）：
 * - HTTP 200 + body code=0        → 业务成功，拦截器解包返回 data
 * - HTTP 401 + body code=40100    → 未登录/凭证失效，清 token 跳 /login
 * - HTTP 403 + body code=40300    → 无权限，仅提示，不跳转
 * - HTTP 400 / 404 / 409 / 500    → 提示 message
 * - 导出接口（/query/export）返回 CSV 字节流，调用方用 responseType: "blob" 接收，
 *   本拦截器对 blob 响应直接放行。
 */

const instance = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL,
  timeout: 30000,
});

instance.interceptors.request.use((config) => {
  const token = getToken();
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

function gotoLogin() {
  clearToken();
  const current = window.location.hash.slice(1);
  if (current !== "/login") {
    window.location.hash = `#/login?redirect=${encodeURIComponent(current)}`;
  }
}

instance.interceptors.response.use(
  (response) => {
    // 文件流（导出 CSV）直接返回
    if (response.config.responseType === "blob") {
      return response;
    }
    const body = response.data;
    if (body.code === 0) {
      return body.data;
    }
    if (!response.config.skipErrorToast) {
      ElMessage.error(body.message || "请求失败");
    }
    return Promise.reject(new Error(body.message || "请求失败"));
  },
  (error) => {
    const status = error.response?.status;
    const body = error.response?.data;
    if (status === 401 || body?.code === 40100) {
      ElMessage.error(body?.message || "登录已过期，请重新登录");
      gotoLogin();
      return Promise.reject(error);
    }
    // 静默请求（后台辅助数据，如看板黄条/通知计数）：失败不弹全局提示
    if (error.config?.skipErrorToast) {
      return Promise.reject(error);
    }
    // 请求超时：后端可能仍在处理并成功入库（尤其大文件上传解析），提示用户核实而非断言失败
    if (error.code === "ECONNABORTED" || /timeout/i.test(error.message || "")) {
      ElMessage({
        type: "warning",
        message: "请求超时：服务器可能仍在处理，请稍后刷新核实结果",
        duration: 6000,
      });
      return Promise.reject(error);
    }
    // 500：后端契约在 data 携带 {exception, detail}（B1 约定），透出定位信息
    if (status === 500 && body?.data && typeof body.data === "object") {
      const hint = body.data.detail || body.data.exception || "";
      // eslint-disable-next-line no-console
      console.error("[API 500]", body.data);
      ElMessage.error(
        `${body.message || "服务异常"}：${String(hint).slice(0, 160) || "详见浏览器控制台"}`,
        { duration: 8000 },
      );
      return Promise.reject(error);
    }
    // eslint-disable-next-line no-console
    console.error("[API ERR]", error && error.stack ? error.stack : String(error));
    ElMessage.error(body?.message || "服务异常，请稍后重试");
    return Promise.reject(error);
  },
);

export default instance;
