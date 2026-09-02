<script setup>
import { computed, ref, watch } from "vue";

const props = defineProps({ form: { type: Object, required: true }, diagnostics: { type: Object, default: () => ({ items: [] }) }, progress: { type: Object, default: () => ({ items: [] }) }, busy: Boolean });
const emit = defineEmits(["save", "test"]);
const activeCountry = ref("");
const newCountryCode = ref("");
const newCountryLabel = ref("");
const probe = computed(() => (Array.isArray(props.progress?.items) ? props.progress.items : []).find((item) => item.key === "proxy_test"));
const pools = computed(() => props.form.countries || []);
const currentPool = computed(() => pools.value.find((item) => item.country === activeCountry.value) || pools.value[0] || null);

watch(pools, (items) => {
  if (!items.some((item) => item.country === activeCountry.value)) {
    activeCountry.value = items.some((item) => item.country === props.form.selectedCountry)
      ? props.form.selectedCountry
      : items[0]?.country || "";
  }
}, { immediate: true, deep: true });

function proxyLines(text) {
  return String(text || "")
    .split(/\r?\n/)
    .map((item) => item.trim())
    .filter((item) => item && !item.startsWith("#"));
}

function proxyLineCount(text) {
  return proxyLines(text).length;
}

function proxyCount(text) {
  return new Set(proxyLines(text)).size;
}

function duplicateCount(text) {
  return proxyLineCount(text) - proxyCount(text);
}

function addCountry() {
  const country = newCountryCode.value.toUpperCase().replace(/[^A-Z]/g, "").slice(0, 2);
  if (country.length !== 2 || pools.value.some((item) => item.country === country)) return;
  props.form.countries.push({ country, label: newCountryLabel.value.trim() || country, text: "", count: 0 });
  activeCountry.value = country;
  if (!props.form.selectedCountry) props.form.selectedCountry = country;
  newCountryCode.value = "";
  newCountryLabel.value = "";
}

function removeCountry() {
  if (!currentPool.value) return;
  const removed = currentPool.value.country;
  props.form.countries = pools.value.filter((item) => item.country !== removed);
  if (props.form.selectedCountry === removed) props.form.selectedCountry = props.form.countries[0]?.country || "";
  activeCountry.value = props.form.selectedCountry || props.form.countries[0]?.country || "";
}

function importLegacyPool() {
  if (!currentPool.value || !String(props.form.poolText || "").trim()) return;
  currentPool.value.text = [currentPool.value.text, props.form.poolText]
    .filter((value) => String(value || "").trim())
    .join("\n");
}
</script>

<template>
  <section class="card proxy-settings-card">
    <div class="card-h"><div><h2>代理设置</h2><p class="subtle">配置注册出站、资源出站和清障链路。</p></div></div>
    <div class="form two">
      <label class="check"><input v-model="form.enabled" type="checkbox" />启用代理</label>
      <label class="check"><input v-model="form.rotate" type="checkbox" />每任务轮换</label>
      <label>出站方式<select v-model="form.mode"><option value="single_proxy">单代理</option><option value="pool">普通代理池</option><option value="country_pool">按国家代理池</option></select></label>
      <label v-if="form.mode === 'country_pool'">注册出口国家<select v-model="form.selectedCountry"><option value="">请选择国家</option><option v-for="item in pools" :key="item.country" :value="item.country">{{ item.country }} · {{ item.label || item.country }}（{{ proxyCount(item.text) }} 条可调度）</option></select></label>
      <label v-if="form.mode === 'single_proxy'">代理地址<input v-model="form.url" type="text" placeholder="socks5://127.0.0.1:1080" /></label>
      <label>资源代理地址<input v-model="form.resourceUrl" type="text" placeholder="留空则复用注册代理" /></label>
      <label>清障方式<select v-model="form.clearanceMode"><option value="none">关闭</option><option value="flaresolverr">FlareSolverr</option></select></label>
      <label v-if="form.clearanceMode === 'flaresolverr'">FlareSolverr 地址<input v-model="form.flareUrl" type="text" placeholder="http://127.0.0.1:8191" /></label>
      <label v-if="form.clearanceMode === 'flaresolverr'">FlareSolverr 出站代理<input v-model="form.flareProxy" type="text" placeholder="socks5://warp:1080" /></label>
      <template v-if="form.mode === 'pool'">
        <label>代理池文件<input v-model="form.poolFile" type="text" /></label>
        <label class="full">代理池文本 <small>已加载 {{ form.poolCount || 0 }} 条 · 去重 {{ form.poolUniqueCount || 0 }} 条<span v-if="!form.poolTextComplete"> · 当前批次结束后加载完整文本</span></small><textarea v-model="form.poolText" rows="8" spellcheck="false" placeholder="每行一条代理" @input="form.poolTextTouched = true; form.poolTextComplete = true"></textarea></label>
      </template>
    </div>

    <div v-if="form.mode === 'country_pool'" class="registration-country-pools">
      <div class="proxy-country-heading"><div><h3>国家代理池</h3><span>{{ pools.length }} 个国家 · 输入 {{ pools.reduce((total, item) => total + proxyLineCount(item.text), 0) }} 行 · 可调度 {{ pools.reduce((total, item) => total + proxyCount(item.text), 0) }} 条</span></div><code>{{ form.countryPoolPath || "data/registration_proxy_pools.json" }}</code></div>
      <div class="country-pool-tabs"><button v-for="item in pools" :key="item.country" type="button" :class="{ active: activeCountry === item.country }" @click="activeCountry = item.country"><b>{{ item.country }}</b><span>{{ item.label || item.country }}</span><em>{{ proxyLineCount(item.text) }} / {{ proxyCount(item.text) }}</em></button></div>
      <div class="country-pool-add"><label><span>国家代码</span><input v-model="newCountryCode" maxlength="2" placeholder="JP" @keyup.enter="addCountry" /></label><label><span>显示名称</span><input v-model="newCountryLabel" maxlength="40" placeholder="日本" @keyup.enter="addCountry" /></label><button class="btn secondary" type="button" @click="addCountry">添加国家</button></div>
      <template v-if="currentPool">
        <div class="country-pool-editor-head"><div><strong>{{ currentPool.country }} · {{ currentPool.label || currentPool.country }}</strong><span>输入 {{ proxyLineCount(currentPool.text) }} 行 · 可调度 {{ proxyCount(currentPool.text) }} 条</span></div><label>显示名称<input v-model="currentPool.label" maxlength="40" /></label><div class="row-btns"><button class="btn secondary" type="button" :disabled="!String(form.poolText || '').trim()" title="追加普通代理池文本" @click="importLegacyPool">导入普通池</button><button class="btn danger" type="button" @click="removeCountry">删除国家</button></div></div>
        <textarea v-model="currentPool.text" class="country-proxy-text" rows="14" spellcheck="false" placeholder="每行一条代理" />
        <div v-if="duplicateCount(currentPool.text)" class="proxy-duplicate-note">检测到 {{ duplicateCount(currentPool.text) }} 行重复代理；保存会保留全部输入，注册调度使用 {{ proxyCount(currentPool.text) }} 条唯一代理。</div>
      </template>
      <div v-else class="empty-state">暂无国家</div>
    </div>

    <div class="actions"><button class="primary" :disabled="busy" @click="emit('save')">保存代理设置</button><button class="btn secondary" :disabled="busy || (form.mode === 'country_pool' && !form.selectedCountry)" @click="emit('test')">{{ form.mode === "country_pool" ? "测试当前国家" : "测试全部代理" }}</button></div>
    <div v-if="probe" class="inline-progress"><div><strong>{{ probe.stage }}</strong><span>{{ probe.done || 0 }} / {{ probe.total || 0 }} · {{ Math.round(probe.percent || 0) }}%</span></div><div class="inline-progress-track"><i :style="{ width: `${probe.percent || 0}%` }"></i></div><small>{{ probe.message || `已运行 ${Math.round(probe.elapsed_sec || 0)}s` }}</small></div>
  </section>
  <section v-if="diagnostics.items?.length" class="card">
    <div class="card-h"><div><h2>代理检测结果</h2><p class="subtle">可用 {{ diagnostics.alive || 0 }} / {{ diagnostics.count || 0 }}</p></div></div>
    <div class="table-wrap proxy-results"><table class="acct"><thead><tr><th>代理</th><th>状态</th><th>出口 IP</th><th>地区</th><th>延迟</th><th>错误</th></tr></thead><tbody><tr v-for="item in diagnostics.items" :key="item.proxy"><td data-label="代理"><code>{{ item.proxy }}</code></td><td data-label="状态"><span :class="['pill', item.ok ? 'success' : 'failed']">{{ item.ok ? '可用' : '失败' }}</span></td><td data-label="出口 IP">{{ item.ip || '-' }}</td><td data-label="地区">{{ item.country || '-' }}</td><td data-label="延迟">{{ item.latency_ms }}ms</td><td data-label="错误" class="health-error">{{ item.error || '-' }}</td></tr></tbody></table></div>
  </section>
</template>
