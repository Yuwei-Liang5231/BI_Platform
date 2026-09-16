import { computed, ref } from "vue";
import { defineStore } from "pinia";

import {
  createProject as createApi,
  deleteProject as deleteApi,
  listProjects as listApi,
  updateProject as updateApi,
} from "@/api/projects";

const STORAGE_KEY = "bi_current_project_id";

/**
 * 项目工作区（B9.3）：项目列表 + 当前项目上下文（localStorage 持久化）。
 *
 * currentId = null 表示「全部项目」浏览视图（仅目录/看板；问数等锁定项目的
 * 页面在 currentId 为空时应回落默认项目）。切换项目后由各页面 watch 重新拉数。
 */
export const useProjectStore = defineStore("project", () => {
  const projects = ref([]);
  const loaded = ref(false);
  const saved = Number(localStorage.getItem(STORAGE_KEY));
  const currentId = ref(Number.isFinite(saved) && saved > 0 ? saved : null);

  const current = computed(
    () => projects.value.find((p) => p.id === currentId.value) ?? null,
  );
  const defaultId = computed(() => {
    const def = projects.value.find((p) => p.name === "默认项目");
    return def?.id ?? projects.value[0]?.id ?? null;
  });
  /** 供锁定项目页面使用：当前项目 id，全部项目视图回落默认项目。 */
  const lockedId = computed(() => currentId.value ?? defaultId.value);

  async function fetchProjects() {
    projects.value = await listApi();
    loaded.value = true;
    // 持久化的项目可能已被删除：回落默认项目
    if (currentId.value && !projects.value.some((p) => p.id === currentId.value)) {
      setCurrent(null);
    }
    return projects.value;
  }

  function setCurrent(id) {
    currentId.value = id;
    if (id === null || id === undefined) localStorage.removeItem(STORAGE_KEY);
    else localStorage.setItem(STORAGE_KEY, String(id));
  }

  function create(payload) {
    return createApi(payload).then((p) => fetchProjects().then(() => p));
  }

  function update(id, payload) {
    return updateApi(id, payload).then((p) => fetchProjects().then(() => p));
  }

  function remove(id) {
    return deleteApi(id).then((r) => {
      if (currentId.value === id) setCurrent(null);
      return fetchProjects().then(() => r);
    });
  }

  return {
    projects,
    loaded,
    currentId,
    current,
    defaultId,
    lockedId,
    fetchProjects,
    setCurrent,
    create,
    update,
    remove,
  };
});
