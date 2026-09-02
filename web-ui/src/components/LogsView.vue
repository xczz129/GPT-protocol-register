<script setup>
import { computed, nextTick, ref, watch } from "vue";
const props = defineProps({ logs: { type: Array, default: () => [] }, connected: Boolean });
defineEmits(["refresh", "clear", "download"]);
const box = ref(null);
const query = ref("");
const level = ref("all");
const filtered = computed(() => props.logs.filter((line) => {
  const text = String(line || "").toLowerCase();
  if (query.value.trim() && !text.includes(query.value.trim().toLowerCase())) return false;
  if (level.value === "error" && !["失败", "错误", "error", "timeout", "http 4", "http 5"].some((item) => text.includes(item))) return false;
  if (level.value === "success" && !["成功", "完成", "http 200"].some((item) => text.includes(item))) return false;
  return true;
}));
watch(() => props.logs.length, async () => { await nextTick(); if (box.value) box.value.scrollTop = box.value.scrollHeight; });
function lineClass(line) {
  const text = String(line || "").toLowerCase();
  return text.includes("失败") || text.includes("error") ? "line-err" : text.includes("成功") || text.includes("完成") ? "line-ok" : "";
}
</script>

<template>
  <section class="card">
    <div class="card-h"><div><h2>实时日志</h2><p class="subtle">任务运行时通过 SSE 持续追加。</p></div><div class="row-btns"><span :class="['pill', connected ? 'success' : 'failed']">{{ connected ? "实时" : "重连中" }}</span><button class="btn secondary" @click="$emit('refresh')">刷新</button><button class="btn secondary" @click="$emit('download')">下载</button><button class="btn danger" @click="$emit('clear')">清空显示</button></div></div>
    <div class="account-filters"><label class="search-field"><span>⌕</span><input v-model="query" type="search" placeholder="搜索任务编号、邮箱或错误" /></label><select v-model="level"><option value="all">全部日志</option><option value="error">仅错误</option><option value="success">仅成功</option></select><span class="filter-result">{{ filtered.length }} / {{ logs.length }}</span></div>
    <div ref="box" class="log"><div v-if="!filtered.length" class="muted">暂无匹配日志</div><div v-for="(line, index) in filtered" :key="index" :class="lineClass(line)">{{ line }}</div></div>
  </section>
</template>
