import { defineConfig, loadEnv } from "vite";
import { resolve } from "node:path";
import vue from "@vitejs/plugin-vue";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const proxyTarget = env.VITE_DEV_API_PROXY_TARGET || "http://127.0.0.1:8787";
  const proxy = {
    target: proxyTarget,
    changeOrigin: true,
  };
  return {
    plugins: [vue()],
    base: "/static/vue/",
    build: {
      outDir: "../web/static/vue",
      emptyOutDir: true,
      sourcemap: false,
      rollupOptions: {
        // 唯一入口：原新前端（src/new）已转正，旧 UI（src/main.js）已删除
        input: {
          main: resolve(__dirname, "index.html"),
        },
      },
    },
    server: {
      proxy: {
        "/api": proxy,
        "/static/style.css": proxy,
        "/login": proxy,
        "/redeem": proxy,
        "/cdk-query": proxy,
      },
    },
  };
});
