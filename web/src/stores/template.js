import { ref } from "vue";
import { defineStore } from "pinia";

import {
  getIndustry as getIndustryApi,
  importTemplates as importApi,
  listIndustries as listApi,
} from "@/api/templates";

export const useTemplateStore = defineStore("template", () => {
  const industries = ref([]);
  const packDetail = ref(null);
  const importing = ref(false);

  async function fetchIndustries() {
    const res = await listApi();
    industries.value = Array.isArray(res) ? res : (res?.list ?? []);
    return industries.value;
  }

  async function fetchPack(industry, params) {
    packDetail.value = await getIndustryApi(industry, { params });
    return packDetail.value;
  }

  function importTemplates(payload) {
    importing.value = true;
    return importApi(payload).finally(() => {
      importing.value = false;
    });
  }

  return { industries, packDetail, importing, fetchIndustries, fetchPack, importTemplates };
});
