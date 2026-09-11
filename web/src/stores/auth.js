import { computed, ref } from "vue";
import { defineStore } from "pinia";

import { login as loginApi } from "@/api/auth";
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

  return { token, user, isLoggedIn, role, isAdmin, canWrite, login, logout };
});
