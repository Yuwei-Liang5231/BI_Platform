import { fileURLToPath, URL } from "node:url";

import vue from "@vitejs/plugin-vue";
import AutoImport from "unplugin-auto-import/vite";
import Components from "unplugin-vue-components/vite";
import { ElementPlusResolver } from "unplugin-vue-components/resolvers";
import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  // 后端代理目标优先级：终端环境变量 > web/.env.local > 默认 8100
  // （loadEnv 同时合并 process.env 与 .env* 文件，process.env 优先）
  const env = loadEnv(mode, fileURLToPath(new URL(".", import.meta.url)), "");
  const backendOrigin = env.BI_BACKEND_ORIGIN || "http://127.0.0.1:8100";

  return {
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
      // dev 环境经 Vite 代理转发 /api 到本地 FastAPI，规避跨域；
      // 后端换端口时设 BI_BACKEND_ORIGIN（终端环境变量或 web/.env.local）
      proxy: {
        "/api": {
          target: backendOrigin,
          changeOrigin: true,
        },
      },
    },
    preview: {
      // 构建产物本地预览（联调/走查用）：同样代理 /api
      port: 5180,
      proxy: {
        "/api": {
          target: backendOrigin,
          changeOrigin: true,
        },
      },
    },
  };
});
