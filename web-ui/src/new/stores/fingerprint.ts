/* --- 指纹环境 store：浏览器运行环境检测 + 指纹/注册配置保存 ---
   保存时只更新 fingerprint 与 registration 的局部字段，其余配置原样保留。 */
import { reactive, ref } from "vue";
import { defineStore } from "pinia";
import { fetchBrowserStatus, fetchConfig, saveConfig, type AppConfig, type BrowserCapability } from "../api/config";

export const useFingerprintStore = defineStore("fingerprint", () => {
  const capabilities = ref<BrowserCapability[]>([]);
  const loading = ref(false);
  const loadError = ref("");

  const form = reactive({
    browser: "local",
    timeout: 480,
    maxTransientRetries: 2,
    postRegisterHealthCheck: false,
    camoufoxOs: "windows",
    camoufoxLocale: "",
    camoufoxHumanize: 0.5,
    camoufoxHeadless: false,
    camoufoxGeoip: true,
  });

  /* 保存时需要回写完整 config，本地保留一份原样副本 */
  let rawConfig: AppConfig = {};

  async function load() {
    loading.value = true;
    loadError.value = "";
    try {
      const [{ config }, status] = await Promise.all([fetchConfig(), fetchBrowserStatus()]);
      rawConfig = config;
      capabilities.value = status.items || [];

      const fp = (config.fingerprint || {}) as Record<string, unknown>;
      const reg = (config.registration || {}) as Record<string, unknown>;
      const camoufox = (fp.camoufox || {}) as Record<string, unknown>;

      form.browser = String(fp.browser || "local");
      form.timeout = Number(reg.timeout || 480);
      form.maxTransientRetries = Number(reg.max_transient_retries ?? 2);
      form.postRegisterHealthCheck = !!reg.post_register_health_check;
      form.camoufoxOs = String(camoufox.os || "windows");
      form.camoufoxLocale = String(camoufox.locale || "");
      form.camoufoxHumanize = Number(camoufox.humanize ?? 0.5);
      form.camoufoxHeadless = !!camoufox.headless;
      form.camoufoxGeoip = camoufox.geoip !== false;
    } catch (error) {
      loadError.value = error instanceof Error ? error.message : "加载失败";
    } finally {
      loading.value = false;
    }
  }

  async function save() {
    const next: AppConfig = {
      ...rawConfig,
      fingerprint: {
        ...((rawConfig.fingerprint || {}) as Record<string, unknown>),
        browser: form.browser,
        camoufox: {
          ...(((rawConfig.fingerprint || {}) as Record<string, unknown>).camoufox || {}) as Record<string, unknown>,
          os: form.camoufoxOs,
          locale: form.camoufoxLocale,
          humanize: form.camoufoxHumanize,
          headless: form.camoufoxHeadless,
          geoip: form.camoufoxGeoip,
        },
      },
      registration: {
        ...((rawConfig.registration || {}) as Record<string, unknown>),
        timeout: form.timeout,
        max_transient_retries: form.maxTransientRetries,
        post_register_health_check: form.postRegisterHealthCheck,
      },
    };
    await saveConfig(next);
    await load();
  }

  return { capabilities, form, loading, loadError, load, save };
});
