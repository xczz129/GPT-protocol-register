import { defineConfig, loadEnv } from "vite";
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
