<script setup>
import { computed, ref, watch } from "vue";
const props = defineProps({ form: { type: Object, required: true }, busy: Boolean });
const emit = defineEmits(["save", "refresh"]);
const active = ref(""); const code = ref(""); const label = ref("");
const pools = computed(() => props.form.countries || []);
const current = computed(() => pools.value.find((item) => item.country === active.value) || pools.value[0] || null);
watch(pools, (rows) => { if (!rows.some((item) => item.country === active.value)) active.value = rows[0]?.country || ""; }, { immediate: true, deep: true });
function count(text) { return String(text || "").split(/\r?\n/).filter((x) => x.trim() && !x.trim().startsWith("#")).length; }
function add() { const country = code.value.toUpperCase().replace(/[^A-Z]/g, "").slice(0, 2); if (country.length !== 2 || pools.value.some((x) => x.country === country)) return; props.form.countries.push({ country, label: label.value.trim() || country, text: "" }); active.value = country; code.value = ""; label.value = ""; }
function remove() { if (!current.value) return; props.form.countries = pools.value.filter((x) => x.country !== current.value.country); if (props.form.selected_country === current.value.country) props.form.selected_country = ""; }
</script>

<template>
  <section class="card trial-proxy-card">
    <div class="card-h"><div><h2>资格代理</h2><p class="subtle">仅用于 0元 Plus 资格检测；注册任务继续使用“代理设置”。未选国家或该国家没有代理时，资格检测走本机直连。</p></div><div class="row-btns"><button class="btn secondary" type="button" :disabled="busy" @click="emit('refresh')">刷新</button><button class="btn primary" type="button" :disabled="busy" @click="emit('save')">保存资格代理</button></div></div>
    <div class="exit-picker">
      <span class="exit-label">资格检测出口</span>
      <div class="exit-options">
        <button type="button" :class="['exit-option', { active: !form.selected_country }]" @click="form.selected_country = ''">本机直连</button>
        <button v-for="item in pools" :key="item.country" type="button" :class="['exit-option', { active: form.selected_country === item.country }]" @click="form.selected_country = item.country"><b>{{ item.country }}</b>{{ item.label || item.country }}</button>
      </div>
    </div>
    <div class="country-pool-tabs"><button v-for="item in pools" :key="item.country" type="button" :class="['country-tab', { active: active === item.country }]" @click="active = item.country"><b>{{ item.country }}</b><span>{{ item.label || item.country }}</span><em>{{ count(item.text) }}</em></button></div>
    <div class="country-pool-add"><label class="country-add-field"><span>国家代码</span><input v-model="code" maxlength="2" placeholder="JP" @keyup.enter="add" /></label><label class="country-add-field"><span>显示名称</span><input v-model="label" maxlength="40" placeholder="日本" @keyup.enter="add" /></label><button class="btn" type="button" @click="add">添加国家</button></div>
    <template v-if="current"><div class="country-pool-editor-head"><div><strong>{{ current.country }} · {{ current.label || current.country }}</strong><span>可调度 {{ count(current.text) }} 条</span></div><label>显示名称<input v-model="current.label" maxlength="40" /></label><div class="row-btns"><button class="btn danger" type="button" @click="remove">删除国家</button></div></div><textarea v-model="current.text" rows="14" spellcheck="false" placeholder="每行一条：HOST:PORT@USERNAME:PASSWORD" /></template>
    <div v-else class="empty-state">添加国家后粘贴该国家的代理。</div>
  </section>
</template>
