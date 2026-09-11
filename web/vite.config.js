import { fileURLToPath, URL } from "node:url";

import vue from "@vitejs/plugin-vue";
import AutoImport from "unplugin-auto-import/vite";
import Components from "unplugin-vue-components/vite";
import { ElementPlusResolver } from "unplugin-vue-components/resolvers";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [
    vue(),
    AutoImport({
      imports: ["vue", "vue-router", "pinia"],
      resolvers: [ElementPlusResolver()],
    }),
    Components({
      resolvers: [ElementPlusResolver()],
    }),
  ],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    port: 5173,
    // dev 环境经 Vite 代理转发 /api 到本地 FastAPI，规避跨域
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8100",
        changeOrigin: true,
      },
    },
  },
  preview: {
    // 构建产物本地预览（联调/走查用）：同样代理 /api；
    // 目标后端可用环境变量 BI_BACKEND_ORIGIN 覆盖，默认 8100
    port: 5180,
    proxy: {
      "/api": {
        target: process.env.BI_BACKEND_ORIGIN || "http://127.0.0.1:8100",
        changeOrigin: true,
      },
    },
  },
});
