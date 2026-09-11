<!-- pwc-regime: product-ui -->
<script setup>
import { reactive, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage } from "element-plus";

import PwcLogo from "@/components/base/PwcLogo.vue";
import { useAuthStore } from "@/stores/auth";

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();

const formRef = ref(null);
const loading = ref(false);
const form = reactive({ username: "", password: "" });
const rules = {
  username: [{ required: true, message: "请输入用户名", trigger: "blur" }],
  password: [{ required: true, message: "请输入密码", trigger: "blur" }],
};

async function handleLogin() {
  await formRef.value.validate();
  loading.value = true;
  try {
    await auth.login(form.username.trim(), form.password);
    ElMessage.success("登录成功");
    router.push(route.query.redirect || "/");
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <div class="login">
    <!-- 品牌区（本页为项目唯一 primary surface） -->
    <section class="login__hero pwc-hero">
      <h1 class="login__hero-title">让每个口径<br />只定义一次</h1>
      <p class="login__hero-sub">
        指标中台 · 统一计算 · 主动预警——把"敢说算不了"变成纪律。
      </p>
    </section>

    <!-- 表单区 -->
    <section class="login__panel">
      <div class="login__panel-inner">
        <PwcLogo :height="48" />
        <h2 class="login__title">登录</h2>
        <p class="login__subtitle">使用平台账号进入工作台</p>
        <el-form
          ref="formRef"
          :model="form"
          :rules="rules"
          label-position="top"
          size="large"
          @keyup.enter="handleLogin"
        >
          <el-form-item label="用户名" prop="username">
            <el-input v-model="form.username" placeholder="用户名" autocomplete="username" />
          </el-form-item>
          <el-form-item label="密码" prop="password">
            <el-input
              v-model="form.password"
              type="password"
              show-password
              placeholder="密码"
              autocomplete="current-password"
            />
          </el-form-item>
          <el-button
            class="login__submit"
            type="primary"
            size="large"
            :loading="loading"
            @click="handleLogin"
          >
            登 录
          </el-button>
        </el-form>
      </div>
    </section>
  </div>
</template>

<style scoped>
.login {
  display: grid;
  grid-template-columns: 3fr 2fr;
  min-height: 100vh;
}

.login__hero {
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
  gap: var(--pwc-space-5);
}

.login__hero-title {
  font-size: var(--pwc-font-display-l);
  color: var(--pwc-text-on-brand);
}

.login__hero-sub {
  font-size: var(--pwc-font-body-l);
  max-width: 32em;
}

.login__panel {
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--pwc-bg-base);
  padding: var(--pwc-space-8);
}

.login__panel-inner {
  width: 100%;
  max-width: 360px;
  display: flex;
  flex-direction: column;
  gap: var(--pwc-space-4);
}

.login__title {
  font-size: var(--pwc-font-heading-l);
  margin-top: var(--pwc-space-4);
}

.login__subtitle {
  color: var(--pwc-text-secondary);
}

.login__submit {
  width: 100%;
  margin-top: var(--pwc-space-4);
}

@media (max-width: 1023px) {
  .login {
    grid-template-columns: 1fr;
  }

  .login__hero {
    min-height: 220px;
    padding: var(--pwc-space-7);
  }
}
</style>
