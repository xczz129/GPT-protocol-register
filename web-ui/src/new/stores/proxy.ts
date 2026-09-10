/* --- 代理设置 store：表单从 /api/config + /api/status 填充（对齐旧版 fillForms），
   保存后回读国家池；代理检测结果独立存放。 */
import { reactive, ref } from "vue";
import { defineStore } from "pinia";
import { fetchConfig } from "../api/config";
import { fetchPanelStatus } from "../api/status";
import { saveProxy, testProxies, type CountryPool, type ProxyDiagnostics } from "../api/proxy";

export const useProxyStore = defineStore("proxy", () => {
  const form = reactive({
    enabled: false,
    rotate: true,
    mode: "single_proxy",
    url: "",
    resourceUrl: "",
    clearanceMode: "none",
    flareUrl: "",
    flareProxy: "",
    poolFile: "data/proxy_pool.txt",
    poolText: "",
    poolTextTouched: false,
    poolTextComplete: false,
    poolCount: 0,
    poolUniqueCount: 0,
    selectedCountry: "",
    countries: [] as CountryPool[],
    countryPoolPath: "data/registration_proxy_pools.json",
  });

  const loading = ref(false);
  const loadError = ref("");

  /** 填充表单：连接方式来自 config.proxy，池文本/国家池来自 status.proxy */
  async function load() {
    loading.value = true;
    loadError.value = "";
    try {
      const [{ config }, status] = await Promise.all([fetchConfig(), fetchPanelStatus()]);
      const proxy = (config.proxy || {}) as Record<string, unknown>;
      const proxyState = (status.proxy || {}) as Record<string, unknown>;
      const clearance = (proxy.clearance || {}) as Record<string, unknown>;
      const countryPools = (proxyState.country_pools || {}) as { items?: CountryPool[]; path?: string };

      form.enabled = !!proxy.enabled;
      form.rotate = proxy.rotate_per_task !== false;
      form.mode = String(proxy.mode || "single_proxy");
      form.url = String(proxy.url || "");
      form.resourceUrl = String(proxy.resource_url || "");
      form.clearanceMode = String(clearance.mode || "none");
      form.flareUrl = String(clearance.flaresolverr_url || "");
      form.flareProxy = String(clearance.proxy_url || "");
      form.poolFile = String(proxy.pool_file || "data/proxy_pool.txt");
      form.poolText = typeof proxyState.pool_text === "string" ? String(proxyState.pool_text) : "";
      form.poolTextTouched = false;
      form.poolTextComplete = typeof proxyState.pool_text === "string";
      form.poolCount = Number(proxyState.pool_count || 0);
      form.poolUniqueCount = Number(proxyState.pool_unique_count || proxyState.pool_count || 0);
      form.selectedCountry = String(proxy.selected_country || proxyState.selected_country || "").toUpperCase();
      form.countries = (countryPools.items || []).map((item) => ({ ...item }));
      form.countryPoolPath = countryPools.path || "data/registration_proxy_pools.json";
    } catch (error) {
      loadError.value = error instanceof Error ? error.message : "加载失败";
    } finally {
      loading.value = false;
    }
  }

  async function save() {
    const result = await saveProxy({
      enabled: form.enabled,
      mode: form.mode,
      url: form.url,
      resource_url: form.resourceUrl,
      pool_file: form.poolFile,
      pool_text: form.poolTextTouched ? form.poolText : null,
      selected_country: form.selectedCountry,
      country_pools: form.countries,
      rotate_per_task: form.rotate,
      clearance: {
        enabled: form.clearanceMode !== "none",
        mode: form.clearanceMode,
        flaresolverr_url: form.flareUrl,
        proxy_url: form.flareProxy,
        timeout_sec: 60,
        refresh_interval: 3600,
      },
    });
    if (result.proxy) {
      const pools = result.proxy.country_pools || {};
      form.countries = (pools.items || []).map((item) => ({ ...item }));
      form.selectedCountry = result.proxy.selected_country || "";
      form.countryPoolPath = pools.path || form.countryPoolPath;
      form.poolTextTouched = false;
    }
    return result;
  }

  /* --- 代理检测 --- */
  const diagnostics = ref<ProxyDiagnostics>({ items: [] });
  const testing = ref(false);

  async function test() {
    testing.value = true;
    try {
      diagnostics.value = await testProxies();
      return diagnostics.value;
    } finally {
      testing.value = false;
    }
  }

  return { form, loading, loadError, load, save, diagnostics, testing, test };
});
