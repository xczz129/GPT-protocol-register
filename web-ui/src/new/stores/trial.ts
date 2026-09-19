/* --- Plus 试用 store：Token 资格检测（单个/前端分块批量）+ 资格代理池 ---
   批量检测是前端分块循环（与旧版一致），stop 只是置标志位让循环在下一块前退出。 */
import { reactive, ref } from "vue";
import { defineStore } from "pinia";
import {
  checkTrialToken, checkTrialTokens, fetchTrialProxyPool, saveTrialProxyPool,
  type TrialCheckResult, type TrialProxyCountry,
} from "../api/trial";

export interface TrialBatchViewRow {
  tail: string;
  ok: boolean;
  eligible: boolean;
  plusActive: boolean;
  banned: boolean;
  status: string;
  plan: string;
  elapsed: number | null;
  reason: string;
  error: string;
}

export const useTrialStore = defineStore("trial", () => {
  /* --- 资格代理池 --- */
  const pool = reactive({ selected_country: "", countries: [] as TrialProxyCountry[] });
  const poolLoading = ref(false);
  const poolSaving = ref(false);

  async function loadPool() {
    poolLoading.value = true;
    try {
      const data = await fetchTrialProxyPool();
      pool.selected_country = data.selected_country;
      pool.countries = data.countries.map((item) => ({ ...item }));
    } finally {
      poolLoading.value = false;
    }
  }

  async function savePool() {
    poolSaving.value = true;
    try {
      await saveTrialProxyPool({ selected_country: pool.selected_country, countries: pool.countries });
      await loadPool();
    } finally {
      poolSaving.value = false;
    }
  }

  /* --- Token 资格检测 --- */
  const form = reactive({
    accessToken: "",
    batchTokens: "",
    useConfiguredProxy: false,
    channel: "v4" as "v4" | "nerver",
    workers: 20,
  });

  const single = reactive({ loading: false, error: "", result: null as TrialCheckResult | null });
  const batch = reactive({ running: false, total: 0, done: 0, results: [] as TrialBatchViewRow[], stopped: false });

  async function checkSingle() {
    const token = form.accessToken.trim();
    if (!token || single.loading) return;
    Object.assign(single, { loading: true, error: "", result: null });
    try {
      single.result = await checkTrialToken(token, form.useConfiguredProxy, form.channel);
    } catch (error) {
      single.error = error instanceof Error ? error.message : "Plus 资格检测失败";
      throw error;
    } finally {
      single.loading = false;
    }
  }

  function parseBatchTokens(): string[] {
    const seen = new Set<string>();
    const tokens: string[] = [];
    for (const raw of form.batchTokens.split(/[\s,;]+/)) {
      let token = raw.trim();
      if (!token) continue;
      if (token.toLowerCase().startsWith("bearer ")) token = token.slice(7).trim();
      if (token.length < 20 || token.length > 20000 || seen.has(token)) continue;
      seen.add(token);
      tokens.push(token);
    }
    return tokens;
  }

  async function startBatch() {
    if (batch.running) return 0;
    const tokens = parseBatchTokens();
    if (!tokens.length || tokens.length > 200) return tokens.length;
    if (!Number.isSafeInteger(form.workers) || form.workers < 1) throw new Error("并发数必须为正整数");
    Object.assign(batch, { running: true, total: tokens.length, done: 0, results: [], stopped: false });
    const workers = form.workers;
    try {
      for (let offset = 0; offset < tokens.length; offset += workers) {
        if (batch.stopped) break;
        const chunk = tokens.slice(offset, offset + workers);
        try {
          const data = await checkTrialTokens(chunk, form.useConfiguredProxy, form.channel, workers);
          for (const row of data?.results || []) {
            batch.results.push({
              tail: row.token_tail || "-",
              ok: Boolean(row.ok),
              eligible: Boolean(row.eligible),
              plusActive: Boolean(row.has_active_subscription),
              banned: Boolean(row.is_deactivated),
              status: row.status || "",
              plan: row.plan_type || "free",
              elapsed: row.elapsed_seconds ?? null,
              reason: row.reason || "",
              error: row.error || "",
            });
          }
        } catch (error) {
          const message = error instanceof Error ? error.message : "检测失败";
          for (const token of chunk) {
            batch.results.push({ tail: token.slice(-10), ok: false, eligible: false, plusActive: false, banned: false, status: "", plan: "", elapsed: null, reason: "", error: message });
          }
        }
        batch.done = Math.min(offset + workers, tokens.length);
      }
    } finally {
      batch.running = false;
    }
    return tokens.length;
  }

  function stopBatch() {
    batch.stopped = true;
  }

  function clearAll() {
    form.accessToken = "";
    form.batchTokens = "";
    Object.assign(single, { loading: false, error: "", result: null });
    Object.assign(batch, { running: false, total: 0, done: 0, results: [], stopped: false });
  }

  return { pool, poolLoading, poolSaving, loadPool, savePool, form, single, batch, checkSingle, parseBatchTokens, startBatch, stopBatch, clearAll };
});
