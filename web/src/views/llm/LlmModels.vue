<!-- pwc-regime: product-ui -->
<script setup>
/**
 * LLM 模型管理（admin）：多模型登记（OpenAI 兼容接口）/ 切换启用 /
 * 连通性测试 / 删除。启用中的模型优先于 env 兜底配置，供 AI 问数使用。
 */
import { onMounted, reactive, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";

import {
  activateLlmModel,
  createLlmModel,
  deleteLlmModel,
  listLlmModels,
  testLlmModel,
  updateLlmModel,
} from "@/api/llm";

const loading = ref(false);
const models = ref([]);
const effective = ref(null); // 当前生效配置（脱敏）：{ name?/base_url/model/source, ... }

const dialogVisible = ref(false);
const saving = ref(false);
const editingId = ref(null);
const testing = ref(false);

const formRef = ref(null);
const form = reactive({
  name: "",
  base_url: "",
  api_key: "",
  model: "",
  remark: "",
  activate: false,
});

const rules = {
  name: [{ required: true, message: "请输入展示名称", trigger: "blur" }],
  base_url: [
    { required: true, message: "请输入接口地址", trigger: "blur" },
    {
      pattern: /^https?:\/\//,
      message: "必须以 http:// 或 https:// 开头",
      trigger: "blur",
    },
  ],
  api_key: [{ required: true, message: "请输入 API Key", trigger: "blur" }],
  model: [{ required: true, message: "请输入模型标识", trigger: "blur" }],
};

async function load() {
  loading.value = true;
  try {
    const data = await listLlmModels();
    models.value = data?.models ?? [];
    effective.value = data?.effective ?? null;
  } finally {
    loading.value = false;
  }
}

function openCreate() {
  editingId.value = null;
  Object.assign(form, {
    name: "",
    base_url: "",
    api_key: "",
    model: "",
    remark: "",
    activate: models.value.length === 0, // 首个模型默认建议启用
  });
  dialogVisible.value = true;
}

function openEdit(row) {
  editingId.value = row.id;
  Object.assign(form, {
    name: row.name,
    base_url: row.base_url,
    api_key: "", // 留空 = 保持原凭据
    model: row.model,
    remark: row.remark ?? "",
    activate: false,
  });
  dialogVisible.value = true;
}

async function save() {
  try {
    await formRef.value.validate();
  } catch {
    return;
  }
  saving.value = true;
  try {
    if (editingId.value === null) {
      await createLlmModel({ ...form });
      ElMessage.success(form.activate ? "已添加并启用" : "已添加");
    } else {
      const payload = {
        name: form.name,
        base_url: form.base_url,
        model: form.model,
        remark: form.remark,
      };
      if (form.api_key.trim()) payload.api_key = form.api_key.trim();
      await updateLlmModel(editingId.value, payload);
      ElMessage.success("已保存");
    }
    dialogVisible.value = false;
    await load();
  } finally {
    saving.value = false;
  }
}

async function activate(row) {
  await activateLlmModel(row.id);
  ElMessage.success(`已切换启用「${row.name}」`);
  await load();
}

async function remove(row) {
  const tip = row.is_active
    ? `「${row.name}」当前处于启用状态，删除后 AI 问数将回退到系统默认配置。确定删除？`
    : `确定删除「${row.name}」？`;
  await ElMessageBox.confirm(tip, "删除确认", { type: "warning" });
  await deleteLlmModel(row.id);
  ElMessage.success("已删除");
  await load();
}

async function testRow(row) {
  testing.value = true;
  try {
    const data = await testLlmModel({ model_id: row.id });
    if (data?.ok) ElMessage.success(data.message || "连接成功");
    else ElMessage.error(data?.message || "连接失败");
  } finally {
    testing.value = false;
  }
}

async function testForm() {
  try {
    await formRef.value.validate();
  } catch {
    return;
  }
  testing.value = true;
  try {
    let apiKey = form.api_key.trim();
    if (editingId.value !== null && !apiKey) {
      // 编辑态留空 = 沿用已存凭据：借已存记录测试
      const data = await testLlmModel({ model_id: editingId.value });
      data?.ok
        ? ElMessage.success(data.message || "连接成功")
        : ElMessage.error(data?.message || "连接失败");
      return;
    }
    const data = await testLlmModel({
      base_url: form.base_url.trim(),
      api_key: apiKey,
      model: form.model.trim(),
    });
    data?.ok
      ? ElMessage.success(data.message || "连接成功")
      : ElMessage.error(data?.message || "连接失败");
  } finally {
    testing.value = false;
  }
}

onMounted(load);
</script>

<template>
  <div class="page-container">
    <div class="page-header">
      <div>
        <h1 class="page-header__title">模型管理</h1>
        <p class="page-header__subtitle">AI 问数使用的 LLM 配置 · 支持多模型登记与一键切换</p>
      </div>
      <div class="page-header__actions">
        <el-button type="primary" @click="openCreate">添加模型</el-button>
      </div>
    </div>

    <el-alert
      v-if="effective"
      :title="`当前生效：${effective.source === 'db' ? '启用中的模型' : '系统默认配置（env）'} · ${effective.model} · ${effective.base_url}`"
      :type="effective.source === 'db' ? 'success' : 'info'"
      show-icon
      :closable="false"
      class="llm-effective"
    />
    <el-alert
      v-else
      title="尚未配置任何 LLM：AI 问数将只使用关键词解析（不调用大模型）"
      type="warning"
      show-icon
      :closable="false"
      class="llm-effective"
    />

    <!-- P3：LLM 配置引导（内网 Ollama 等 OpenAI 兼容端点） -->
    <el-collapse class="llm-guide pwc-card">
      <el-collapse-item name="guide">
        <template #title>
          <span class="llm-guide__title">如何配置模型？（OpenAI 兼容端点，含内网 Ollama 示例）</span>
        </template>
        <ul class="llm-guide__list">
          <li>
            平台所有 AI 能力（问数理解、看板速览、异动假设、口径助手、语义标注、
            归因解读、报告叙述）共用这一处配置，走 <b>OpenAI 兼容</b>接口——
            接口地址需以 <code>/v1</code> 结尾。
          </li>
          <li>
            <b>内网 Ollama 示例</b>：接口地址 <code>http://&lt;内网主机&gt;:11434/v1</code>，
            模型标识如 <code>qwen2.5:14b</code>，API Key 填任意占位值（如 <code>ollama</code>）。
          </li>
          <li>
            vLLM、one-api、公司自建网关等 OpenAI 兼容服务同理：
            地址 + 模型标识 + Key 三项即可。
          </li>
          <li>
            保存后点该行「<b>测试</b>」验证连通，再点「启用」切换生效；
            公司网络可能屏蔽公网大模型，测试失败时优先确认网络可达性（推荐内网部署）。
          </li>
          <li>
            未配置或调用失败时，各 AI 功能都会<b>静默降级</b>（关键词解析/规则句/隐藏入口），
            不影响平台其余功能使用。
          </li>
        </ul>
      </el-collapse-item>
    </el-collapse>

    <el-table :data="models" v-loading="loading" class="pwc-card">
      <el-table-column label="名称" min-width="140">
        <template #default="{ row }">
          <span class="llm-name">{{ row.name }}</span>
          <el-tag v-if="row.is_active" type="success" size="small" effect="light">启用中</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="model" label="模型标识" min-width="160" />
      <el-table-column prop="base_url" label="接口地址" min-width="220" show-overflow-tooltip />
      <el-table-column prop="api_key_masked" label="API Key" min-width="150" />
      <el-table-column prop="remark" label="备注" min-width="120" show-overflow-tooltip />
      <el-table-column label="操作" width="260" fixed="right">
        <template #default="{ row }">
          <el-button
            v-if="!row.is_active"
            size="small"
            type="primary"
            @click="activate(row)"
          >启用</el-button>
          <el-button size="small" :disabled="testing" @click="testRow(row)">测试</el-button>
          <el-button size="small" @click="openEdit(row)">编辑</el-button>
          <el-button size="small" type="danger" plain @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty description="还没有登记模型，点击右上角「添加模型」" />
      </template>
    </el-table>

    <el-dialog
      v-model="dialogVisible"
      :title="editingId === null ? '添加模型' : '编辑模型'"
      width="560px"
    >
      <el-form ref="formRef" :model="form" :rules="rules" label-width="100px">
        <el-form-item label="名称" prop="name">
          <el-input v-model="form.name" placeholder="如：DeepSeek 官方" maxlength="100" />
        </el-form-item>
        <el-form-item label="接口地址" prop="base_url">
          <el-input
            v-model="form.base_url"
            placeholder="OpenAI 兼容根地址，如 https://api.deepseek.com"
          />
        </el-form-item>
        <el-form-item label="API Key" prop="api_key">
          <el-input
            v-model="form.api_key"
            :placeholder="editingId === null ? 'sk-...' : '留空表示保持原有 Key 不变'"
            show-password
          />
        </el-form-item>
        <el-form-item label="模型标识" prop="model">
          <el-input v-model="form.model" placeholder="如 deepseek-v4-flash" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="form.remark" maxlength="300" />
        </el-form-item>
        <el-form-item v-if="editingId === null" label="立即启用">
          <el-switch v-model="form.activate" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button :disabled="testing" :loading="testing" @click="testForm">测试连通</el-button>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.llm-effective {
  margin-bottom: var(--pwc-space-5, 16px);
}

/* P3：配置指引卡 */
.llm-guide {
  margin-bottom: var(--pwc-space-5, 16px);
  padding: 0 var(--pwc-space-4, 16px);
}
.llm-guide__title {
  font-size: var(--pwc-font-body-s, 13px);
  color: var(--pwc-text-secondary, #535353);
}
.llm-guide__list {
  margin: 0;
  padding-left: 18px;
  display: grid;
  gap: 6px;
  font-size: var(--pwc-font-body-s, 13px);
  color: var(--pwc-text-primary);
  line-height: 1.7;
}
.llm-guide__list code {
  background: var(--pwc-bg-muted, #f5f5f5);
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 12px;
}

.llm-name {
  font-weight: 600;
  margin-right: 8px;
}
</style>
