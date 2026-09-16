import { computed, ref } from "vue";
import { defineStore } from "pinia";

import { getMe as getMeApi, login as loginApi } from "@/api/auth";
import { clearToken, clearUser, getToken, getUser, setToken, setUser } from "@/utils/storage";

export const useAuthStore = defineStore("auth", () => {
  const token = ref(getToken());
  const user = ref(getUser());

  const isLoggedIn = computed(() => Boolean(token.value));
  const role = computed(() => user.value?.role ?? "");
  const isAdmin = computed(() => role.value === "admin");
  // analyst/admin 可写（建指标、导入模板）；viewer 只读
  const canWrite = computed(() => ["admin", "analyst"].includes(role.value));

  async function login(username, password) {
    const data = await loginApi({ username, password });
    token.value = data.token;
    setToken(data.token);
    user.value = data.user ?? data;
    setUser(user.value);
    return user.value;
  }

  function logout() {
    clearToken();
    clearUser();
    token.value = "";
    user.value = null;
  }

  // token 持久化在 localStorage、user 在 sessionStorage（标签页关闭即失）——
  // 重开浏览器会出现「有 token 无 user」：角色为空、全站被当只读降权。
  // 用 /auth/me 恢复身份；token 失效则清除回登录页。
  let ensuring = null;
  function ensureUser() {
    if (!token.value || user.value) return Promise.resolve();
    ensuring =
      ensuring ??
      getMeApi()
        .then((u) => {
          user.value = u;
          setUser(u);
        })
        .catch(() => {
          logout();
        })
        .finally(() => {
          ensuring = null;
        });
    return ensuring;
  }

  return { token, user, isLoggedIn, role, isAdmin, canWrite, login, logout, ensureUser };
});
