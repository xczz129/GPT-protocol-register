<script setup lang="ts">
/* --- 代理设置：出站方式（单代理/普通池/国家池）+ 资源代理 + 清障链路 + 代理检测 ---
   交互对齐旧版 ProxyView：国家池标签页编辑、重复行提示、检测结果表。 */
import { computed, onMounted, ref, watch } from "vue";
import { Network, Plus, Trash2 } from "@lucide/vue";
import AppButton from "../../components/ui/AppButton.vue";
import AppEmpty from "../../components/ui/AppEmpty.vue";
import AppTag from "../../components/ui/AppTag.vue";
import { useProxyStore } from "../../stores/proxy";
import { useUiStore } from "../../stores/ui";
import { useConfirm } from "../../composables/useConfirm";

const proxy = useProxyStore();
const ui = useUiStore();
const confirm = useConfirm();

const busy = ref(false);
const activeCountry = ref("");
const newCountryCode = ref("");
const newCountryLabel = ref("");

const pools = computed(() => proxy.form.countries);
const currentPool = computed(
  () => pools.value.find((item) => item.country === activeCountry.value) || pools.value[0] || null,
);

watch(
  pools,
  (items) => {
    if (!items.some((item) => item.country === activeCountry.value)) {
      activeCountry.value = items.some((item) => item.country === proxy.form.selectedCountry)
        ? proxy.form.selectedCountry
        : items[0]?.country || "";
    }
  },
  { immediate: true, deep: true },
);

/* --- 代理行统计（对齐旧版：输入行数 / 去重后可调度数） --- */
function proxyLines(text: string) {
  return String(text || "").split(/\r?\n/).map((item) => item.trim()).filter((item) => item && !item.startsWith("#"));
}
function proxyLineCount(text: string) {
  return proxyLines(text).length;
}
function proxyCount(text: string) {
  return new Set(proxyLines(text)).size;
}
function duplicateCount(text: string) {
  return proxyLineCount(text) - proxyCount(text);
}
const totalLines = computed(() => pools.value.reduce((total, item) => total + proxyLineCount(item.text), 0));
const totalUnique = computed(() => pools.value.reduce((total, item) => total + proxyCount(item.text), 0));

function addCountry() {
  const country = newCountryCode.value.toUpperCase().replace(/[^A-Z]/g, "").slice(0, 2);
  if (country.length !== 2 || pools.value.some((item) => item.country === country)) return;
  proxy.form.countries.push({ country, label: newCountryLabel.value.trim() || country, text: "", count: 0 });
  activeCountry.value = country;
  if (!proxy.form.selectedCountry) proxy.form.selectedCountry = country;
  newCountryCode.value = "";
  newCountryLabel.value = "";
}

async function removeCountry() {
  if (!currentPool.value) return;
  const removed = currentPool.value.country;
  if (!(await confirm(`删除国家池 ${removed}？其中的代理文本将一并移除（保存后生效）。`, { title: "删除国家", confirmText: "删除", danger: true }))) return;
  proxy.form.countries = pools.value.filter((item) => item.country !== removed);
  if (proxy.form.selectedCountry === removed) proxy.form.selectedCountry = proxy.form.countries[0]?.country || "";
  activeCountry.value = proxy.form.selectedCountry || proxy.form.countries[0]?.country || "";
}

function importLegacyPool() {
  if (!currentPool.value || !proxy.form.poolText.trim()) return;
  currentPool.value.text = [currentPool.value.text, proxy.form.poolText]
    .filter((value) => String(value || "").trim())
    .join("\n");
}

async function save() {
  if (busy.value) return;
  busy.value = true;
  try {
    await proxy.save();
    ui.notify("代理设置已保存", "success");
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "保存失败", "error");
  } finally {
    busy.value = false;
  }
}

async function test() {
  try {
    const result = await proxy.test();
    ui.notify(`代理检测完成：可用 ${result.alive || 0}/${result.count || 0}`, (result.alive ?? 0) > 0 ? "success" : "warning");
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "代理检测失败", "error");
  }
}

onMounted(() => proxy.load());
</script>

<template>
  <div class="proxy-page">
    <section class="panel glass-panel">
      <h2 class="panel-title">代理设置</h2>
      <p class="panel-sub">配置注册出站、资源出站和清障链路</p>

      <AppEmpty v-if="proxy.loadError" title="代理配置加载失败" :hint="proxy.loadError">
        <template #icon><Network :size="48" :stroke-width="1" /></template>
      </AppEmpty>

      <template v-else>
        <div class="form-grid">
          <label class="check"><input v-model="proxy.form.enabled" type="checkbox" /><span>启用代理</span></label>
          <label class="check"><input v-model="proxy.form.rotate" type="checkbox" /><span>每任务轮换</span></label>

          <label class="field">
            <span>出站方式</span>
            <select v-model="proxy.form.mode" class="input">
              <option value="single_proxy">单代理</option>
              <option value="pool">普通代理池</option>
              <option value="country_pool">按国家代理池</option>
            </select>
          </label>

          <label v-if="proxy.form.mode === 'country_pool'" class="field">
            <span>注册出口国家</span>
            <select v-model="proxy.form.selectedCountry" class="input">
              <option value="">请选择国家</option>
              <option v-for="item in pools" :key="item.country" :value="item.country">
                {{ item.country }} · {{ item.label || item.country }}（{{ proxyCount(item.text) }} 条可调度）
              </option>
            </select>
          </label>

          <label v-if="proxy.form.mode === 'single_proxy'" class="field">
            <span>代理地址</span>
            <input v-model="proxy.form.url" class="input" type="text" placeholder="socks5://127.0.0.1:1080" />
          </label>

          <label class="field">
            <span>资源代理地址</span>
            <input v-model="proxy.form.resourceUrl" class="input" type="text" placeholder="留空则复用注册代理" />
          </label>

          <label class="field">
            <span>清障方式</span>
            <select v-model="proxy.form.clearanceMode" class="input">
              <option value="none">关闭</option>
              <option value="flaresolverr">FlareSolverr</option>
            </select>
          </label>

          <label v-if="proxy.form.clearanceMode === 'flaresolverr'" class="field">
            <span>FlareSolverr 地址</span>
            <input v-model="proxy.form.flareUrl" class="input" type="text" placeholder="http://127.0.0.1:8191" />
          </label>

          <label v-if="proxy.form.clearanceMode === 'flaresolverr'" class="field">
            <span>FlareSolverr 出站代理</span>
            <input v-model="proxy.form.flareProxy" class="input" type="text" placeholder="socks5://warp:1080" />
          </label>

          <template v-if="proxy.form.mode === 'pool'">
            <label class="field">
              <span>代理池文件</span>
              <input v-model="proxy.form.poolFile" class="input" type="text" />
            </label>
            <label class="field full">
              <span>
                代理池文本
                <small class="help">
                  已加载 {{ proxy.form.poolCount }} 条 · 去重 {{ proxy.form.poolUniqueCount }} 条
                  <template v-if="!proxy.form.poolTextComplete"> · 当前批次结束后加载完整文本</template>
                </small>
              </span>
              <textarea
                v-model="proxy.form.poolText" class="input area" rows="8" spellcheck="false"
                placeholder="每行一条代理"
                @input="proxy.form.poolTextTouched = true; proxy.form.poolTextComplete = true"
              ></textarea>
            </label>
          </template>
        </div>

        <!-- 国家代理池 -->
        <div v-if="proxy.form.mode === 'country_pool'" class="country-pools">
          <div class="country-head">
            <div>
              <h3>国家代理池</h3>
              <span class="help">{{ pools.length }} 个国家 · 输入 {{ totalLines }} 行 · 可调度 {{ totalUnique }} 条</span>
            </div>
            <code>{{ proxy.form.countryPoolPath }}</code>
          </div>

          <div v-if="pools.length" class="country-tabs">
            <button
              v-for="item in pools" :key="item.country" type="button"
              :class="['country-tab', { active: activeCountry === item.country }]"
              @click="activeCountry = item.country"
            >
              <b>{{ item.country }}</b>
              <span>{{ item.label || item.country }}</span>
              <em>{{ proxyLineCount(item.text) }} / {{ proxyCount(item.text) }}</em>
            </button>
          </div>

          <div class="country-add">
            <label class="field">
              <span>国家代码</span>
              <input v-model="newCountryCode" class="input" maxlength="2" placeholder="JP" @keyup.enter="addCountry" />
            </label>
            <label class="field">
              <span>显示名称</span>
              <input v-model="newCountryLabel" class="input" maxlength="40" placeholder="日本" @keyup.enter="addCountry" />
            </label>
            <AppButton @click="addCountry"><Plus :size="14" /><span>添加国家</span></AppButton>
          </div>

          <template v-if="currentPool">
            <div class="pool-editor-head">
              <div>
                <strong>{{ currentPool.country }} · {{ currentPool.label || currentPool.country }}</strong>
                <span class="help">输入 {{ proxyLineCount(currentPool.text) }} 行 · 可调度 {{ proxyCount(currentPool.text) }} 条</span>
              </div>
              <label class="field label-inline">
                <span>显示名称</span>
                <input v-model="currentPool.label" class="input" maxlength="40" />
              </label>
              <div class="row-btns">
                <AppButton size="sm" :disabled="!proxy.form.poolText.trim()" title="追加普通代理池文本" @click="importLegacyPool">导入普通池</AppButton>
                <AppButton size="sm" variant="danger" @click="removeCountry"><Trash2 :size="13" /><span>删除国家</span></AppButton>
              </div>
            </div>
            <textarea v-model="currentPool.text" class="input area country-text" rows="12" spellcheck="false" placeholder="每行一条代理"></textarea>
            <p v-if="duplicateCount(currentPool.text)" class="dup-note">
              检测到 {{ duplicateCount(currentPool.text) }} 行重复代理；保存会保留全部输入，注册调度使用 {{ proxyCount(currentPool.text) }} 条唯一代理
            </p>
          </template>
          <AppEmpty v-else title="暂无国家" hint="在上方添加国家代码与代理文本" />
        </div>

        <div class="form-actions">
          <AppButton variant="primary" :loading="busy" @click="save"><span>保存代理设置</span></AppButton>
          <AppButton :loading="proxy.testing" :disabled="proxy.form.mode === 'country_pool' && !proxy.form.selectedCountry" @click="test">
            <span>{{ proxy.form.mode === "country_pool" ? "测试当前国家" : "测试全部代理" }}</span>
          </AppButton>
        </div>
      </template>
    </section>

    <!-- 检测结果 -->
    <section v-if="proxy.diagnostics.items?.length" class="panel glass-panel">
      <h2 class="panel-title">代理检测结果</h2>
      <p class="panel-sub">可用 {{ proxy.diagnostics.alive || 0 }} / {{ proxy.diagnostics.count || 0 }}</p>
      <div class="table-wrap">
        <table class="diag-table">
          <thead>
            <tr><th>代理</th><th>状态</th><th>出口 IP</th><th>地区</th><th>延迟</th><th>错误</th></tr>
          </thead>
          <tbody>
            <tr v-for="item in proxy.diagnostics.items" :key="item.proxy">
              <td><code>{{ item.proxy }}</code></td>
              <td><AppTag :kind="item.ok ? 'success' : 'error'">{{ item.ok ? "可用" : "失败" }}</AppTag></td>
              <td>{{ item.ip || "-" }}</td>
              <td>{{ item.country || "-" }}</td>
              <td>{{ item.latency_ms }}ms</td>
              <td class="error-cell">{{ item.error || "-" }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>

<style scoped>
.proxy-page {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.panel {
  padding: 18px 20px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.panel-title {
  font-size: 0.95rem;
  font-weight: 600;
  color: var(--text-primary);
}

.panel-sub {
  font-size: 0.76rem;
  color: var(--text-muted);
  margin-top: -10px;
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 12px 16px;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.field.full {
  grid-column: 1 / -1;
}

.field > span {
  font-size: 0.74rem;
  color: var(--text-secondary);
  font-weight: 500;
}

.help {
  font-size: 0.68rem;
  color: var(--text-muted);
  font-weight: 400;
}

.check {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 0.8rem;
  color: var(--text-secondary);
  cursor: pointer;
}

.check input {
  width: 15px;
  height: 15px;
  accent-color: var(--accent);
}

.area {
  resize: vertical;
}

/* --- 国家代理池 --- */
.country-pools {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 14px;
  border-radius: var(--radius-md);
  background: var(--glass-bg);
  border: 1px solid var(--glass-border);
}

.country-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}

.country-head h3 {
  font-size: 0.86rem;
  font-weight: 600;
  color: var(--text-primary);
}

.country-head code {
  font-size: 0.68rem;
  color: var(--text-muted);
}

.country-tabs {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}

.country-tab {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  border-radius: 9px;
  border: 1px solid var(--field-border);
  background: var(--field-bg);
  color: var(--text-secondary);
  font-size: 0.75rem;
  cursor: pointer;
  transition: var(--transition-fast);
}

.country-tab b {
  color: var(--text-primary);
}

.country-tab em {
  font-style: normal;
  font-size: 0.66rem;
  color: var(--text-muted);
  font-variant-numeric: tabular-nums;
}

.country-tab.active {
  border-color: var(--accent-border);
  background: var(--accent-bg);
  color: var(--accent);
}

.country-tab.active b {
  color: var(--accent);
}

.country-add {
  display: flex;
  gap: 10px;
  align-items: flex-end;
  flex-wrap: wrap;
}

.country-add .field {
  width: 150px;
}

.pool-editor-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
}

.pool-editor-head strong {
  display: block;
  color: var(--text-primary);
  font-size: 0.84rem;
}

.label-inline {
  min-width: 160px;
}

.row-btns {
  display: flex;
  gap: 8px;
}

.country-text {
  font-family: var(--font-mono);
  font-size: 0.75rem;
}

.dup-note {
  margin: 0;
  padding: 8px 12px;
  border-radius: var(--radius-sm);
  background: var(--warning-bg);
  color: var(--warning);
  font-size: 0.72rem;
}

.form-actions {
  display: flex;
  gap: 10px;
  border-top: 1px solid var(--line-subtle);
  padding-top: 14px;
}

/* --- 检测结果 --- */
.table-wrap {
  overflow-x: auto;
}

.diag-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.78rem;
}

.diag-table th {
  text-align: left;
  font-size: 0.7rem;
  font-weight: 600;
  color: var(--text-muted);
  padding: 8px 10px;
  border-bottom: 1px solid var(--line-subtle);
  white-space: nowrap;
}

.diag-table td {
  padding: 8px 10px;
  border-bottom: 1px solid var(--line-subtle);
  color: var(--text-secondary);
}

.diag-table tr:last-child td {
  border-bottom: none;
}

.diag-table code {
  font-size: 0.72rem;
  color: var(--text-primary);
  word-break: break-all;
}

.error-cell {
  color: var(--error);
  font-size: 0.72rem;
}

@media (max-width: 640px) {
  .form-grid {
    grid-template-columns: 1fr;
  }
}
</style>
