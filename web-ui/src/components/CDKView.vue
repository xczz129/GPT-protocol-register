<script setup>
import { computed, onBeforeUnmount, ref, watch } from "vue";
import AppPager from "./AppPager.vue";

const props = defineProps({
  groups: { type: Array, default: () => [] },
  cdks: { type: Array, default: () => [] },
  summary: { type: Object, default: () => ({}) },
  eligibility: { type: Object, default: () => ({}) },
  generated: { type: Array, default: () => [] },
  busy: Boolean,
});
const emit = defineEmits(["group-change", "generate", "status", "refresh", "clear-generated"]);

const groupId = ref("");
const count = ref(1);
const expiresDays = ref(0);
const searchQuery = ref("");
const statusFilter = ref("all");
const pageSize = ref(20);
const page = ref(1);
const selected = ref([]);
const copiedId = ref("");
const copiedSelection = ref(false);
let copiedTimer = null;
let copiedSelectionTimer = null;
onBeforeUnmount(() => {
  clearTimeout(copiedTimer);
  clearTimeout(copiedSelectionTimer);
});

const preferredGroup = computed(() => props.groups.find((item) => item.name === "出售") || props.groups[0]);
const filtered = computed(() => {
  const needle = searchQuery.value.trim().toLowerCase();
  return props.cdks.filter((item) => {
    if (groupId.value && item.group_id !== groupId.value) return false;
    if (statusFilter.value === "expired") return isExpired(item);
    if (statusFilter.value !== "all" && item.status !== statusFilter.value) return false;
    if (!needle) return true;
    return [item.email, item.account_id, item.code, item.code_hint, item.provider_type, item.group_name, item.status]
      .some((value) => String(value || "").toLowerCase().includes(needle));
  });
});
const pages = computed(() => Math.max(1, Math.ceil(filtered.value.length / pageSize.value)));
const pageItems = computed(() => filtered.value.slice((page.value - 1) * pageSize.value, page.value * pageSize.value));
const pageIds = computed(() => pageItems.value.map((item) => item.id).filter(Boolean));
const selectedItems = computed(() => {
  const wanted = new Set(selected.value);
  return props.cdks.filter((item) => wanted.has(item.id));
});
const allPageSelected = computed({
  get: () => pageIds.value.length > 0 && pageIds.value.every((id) => selected.value.includes(id)),
  set: (checked) => {
    const set = new Set(selected.value);
    pageIds.value.forEach((id) => checked ? set.add(id) : set.delete(id));
    selected.value = [...set];
  },
});
const eligibleCount = computed(() => Number(props.eligibility.eligible || 0));

watch(() => props.groups, () => {
  if (!props.groups.some((item) => item.id === groupId.value)) {
    groupId.value = preferredGroup.value?.id || "";
  }
}, { immediate: true, deep: true });
watch(groupId, (value) => {
  page.value = 1;
  selected.value = [];
  if (value) emit("group-change", value);
});
watch([searchQuery, statusFilter, pageSize], () => {
  page.value = 1;
  selected.value = [];
});
watch(() => props.cdks, () => {
  const valid = new Set(props.cdks.map((item) => item.id));
  selected.value = selected.value.filter((id) => valid.has(id));
  page.value = Math.min(page.value, pages.value);
}, { deep: true });

function isExpired(item) {
  if (!item.expires_at) return false;
  const time = new Date(item.expires_at).getTime();
  return Number.isFinite(time) && time <= Date.now();
}
function stateName(item) {
  if (isExpired(item)) return "已过期";
  return { available: "待兑换", redeemed: "已兑换", disabled: "已停用" }[item.status] || item.status || "未知";
}
function stateClass(item) {
  if (isExpired(item) || item.status === "disabled") return "bad";
  return item.status === "redeemed" ? "ok" : "warn";
}
function fmtTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}
function generate() {
  if (!groupId.value) return;
  emit("generate", { group_id: groupId.value, count: Math.max(1, Number(count.value || 1)), expires_days: Math.max(0, Number(expiresDays.value || 0)) });
}
function changeStatus(status) {
  if (!selected.value.length) return;
  emit("status", { ids: [...selected.value], status, group_id: groupId.value });
  selected.value = [];
}
function resetOne(item) {
  if (!item?.id) return;
  emit("status", { ids: [item.id], status: "reset", group_id: groupId.value });
}
function generatedText(separator = "\t") {
  const head = separator === "," ? "CDK,Email,Expires At" : "CDK\tEmail\tExpires At";
  const lines = props.generated.map((item) => [item.code, item.email, item.expires_at || "永久"].map((value) => {
    const text = String(value || "");
    return separator === "," ? `"${text.replaceAll('"', '""')}"` : text;
  }).join(separator));
  return [head, ...lines].join("\n");
}
function selectedText(separator = "\t", includeHeader = true) {
  const csv = separator === ",";
  const head = csv ? "CDK,Email" : "CDK\tEmail";
  const lines = selectedItems.value.map((item) => [
    item.code || item.code_hint || "",
    item.email || "",
  ].map((value) => {
    const text = String(value || "");
    return csv ? `"${text.replaceAll('"', '""')}"` : text;
  }).join(separator));
  return (includeHeader ? [head, ...lines] : lines).join("\n");
}
async function writeClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const helper = document.createElement("textarea");
    helper.value = text;
    helper.style.position = "fixed";
    helper.style.opacity = "0";
    document.body.appendChild(helper);
    helper.select();
    document.execCommand("copy");
    helper.remove();
  }
}
async function copyGenerated() {
  if (!props.generated.length) return;
  await writeClipboard(generatedText("\t"));
}
async function copyCode(item) {
  const code = String(item.code || "").trim();
  if (!code) return;
  await writeClipboard(code);
  copiedId.value = item.id;
  clearTimeout(copiedTimer);
  copiedTimer = setTimeout(() => { copiedId.value = ""; }, 1800);
}
async function copySelected() {
  if (!selectedItems.value.length) return;
  await writeClipboard(selectedText("\t", false));
  copiedSelection.value = true;
  clearTimeout(copiedSelectionTimer);
  copiedSelectionTimer = setTimeout(() => { copiedSelection.value = false; }, 1800);
}
function downloadSelected() {
  if (!selectedItems.value.length) return;
  const blob = new Blob(["\ufeff", selectedText(",")], { type: "text/csv;charset=utf-8" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  const group = props.groups.find((item) => item.id === groupId.value);
  const groupName = String(group?.name || "selected").replace(/[^a-zA-Z0-9\u4e00-\u9fff_-]+/g, "-");
  link.download = `gpt-cdks-${groupName}-${selectedItems.value.length}-${new Date().toISOString().slice(0, 10)}.csv`;
  link.click();
  URL.revokeObjectURL(link.href);
}
function downloadGenerated(format) {
  if (!props.generated.length) return;
  const csv = format === "csv";
  const blob = new Blob([csv ? "\ufeff" : "", generatedText(csv ? "," : "\t")], { type: csv ? "text/csv;charset=utf-8" : "text/plain;charset=utf-8" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `gpt-cdks-${new Date().toISOString().slice(0, 10)}.${csv ? "csv" : "txt"}`;
  link.click();
  URL.revokeObjectURL(link.href);
}
</script>

<template>
  <section class="card cdk-panel">
    <div class="card-h">
      <div><h2>CDK 管理</h2><p class="subtle">把指定分组中的账号绑定为邮箱兑换码，每个账号只生成一个 CDK。</p></div>
      <div class="row-btns"><a class="btn secondary" href="/redeem" target="_blank" rel="noopener">打开兑换页</a><button class="btn secondary" :disabled="busy" @click="emit('refresh', groupId)">刷新</button></div>
    </div>

    <div class="cdk-metrics">
      <div><b>{{ summary.total || 0 }}</b><span>全部 CDK</span></div>
      <div><b>{{ summary.available || 0 }}</b><span>待兑换</span></div>
      <div><b>{{ summary.redeemed || 0 }}</b><span>已兑换</span></div>
      <div><b>{{ summary.disabled || 0 }}</b><span>已停用</span></div>
      <div><b>{{ summary.expired || 0 }}</b><span>已过期</span></div>
      <div class="eligible"><b>{{ eligibleCount }}</b><span>当前可生成</span></div>
    </div>

    <div class="cdk-generator">
      <label><span>账号分组</span><select v-model="groupId"><option v-for="group in groups" :key="group.id" :value="group.id">{{ group.name }}（{{ group.account_count || 0 }}）</option></select></label>
      <label><span>生成数量</span><input v-model.number="count" type="number" min="1" max="500" /></label>
      <label><span>有效天数</span><input v-model.number="expiresDays" type="number" min="0" max="3650" /><small>0 表示永久</small></label>
      <button class="btn primary" :disabled="busy || !groupId" :title="!groupId ? '请先选择账号分组' : eligibleCount ? `当前可生成 ${eligibleCount} 个` : '点击后由服务端实时检查可生成账号'" @click="generate">{{ busy ? "生成中…" : "生成 CDK" }}</button>
      <div class="cdk-eligibility">
        <span>组内 {{ eligibility.group_accounts || 0 }}</span>
        <span>无邮箱凭证 {{ eligibility.without_mailbox || 0 }}</span>
        <span>失效 {{ eligibility.invalid || 0 }}</span>
        <span>已绑定 {{ eligibility.already_bound || 0 }}</span>
        <span>安全字段不完整 {{ eligibility.security_incomplete || 0 }}</span>
      </div>
    </div>

    <div v-if="generated.length" class="generated-cdks">
      <div class="generated-head"><div><strong>本次生成 {{ generated.length }} 个</strong><span>完整 CDK 已保存，可随时在下方列表点击复制。</span></div><div class="row-btns"><button class="btn secondary" @click="copyGenerated">复制全部</button><button class="btn secondary" @click="downloadGenerated('txt')">下载 TXT</button><button class="btn secondary" @click="downloadGenerated('csv')">下载 CSV</button><button class="btn secondary" @click="emit('clear-generated')">收起</button></div></div>
      <div class="generated-list"><div v-for="item in generated" :key="item.id"><code>{{ item.code }}</code><span>{{ item.email }}</span><small>{{ item.expires_at ? `到期 ${fmtTime(item.expires_at)}` : "永久有效" }}</small></div></div>
    </div>

    <div class="bulkbar cdk-toolbar">
      <label class="search-field"><span aria-hidden="true">⌕</span><input v-model="searchQuery" type="search" placeholder="搜索邮箱、账号 ID、CDK 或分组" /></label>
      <select v-model.number="pageSize" aria-label="每页数量"><option :value="10">10 / 页</option><option :value="20">20 / 页</option><option :value="50">50 / 页</option><option :value="100">100 / 页</option></select>
      <label class="checkline"><input v-model="allPageSelected" type="checkbox" />全选本页</label>
      <select v-model="statusFilter"><option value="all">全部状态</option><option value="available">待兑换</option><option value="redeemed">已兑换</option><option value="disabled">已停用</option><option value="expired">已过期</option></select>
      <span>显示 {{ filtered.length }} / {{ cdks.length }} · 已选 {{ selected.length }}</span>
      <button class="btn secondary" :disabled="!selectedItems.length" @click="copySelected">{{ copiedSelection ? "已复制" : `复制选中（${selectedItems.length}）` }}</button>
      <button class="btn secondary" :disabled="!selectedItems.length" @click="downloadSelected">导出 CSV</button>
      <button class="btn danger" :disabled="busy || !selected.length" @click="changeStatus('disabled')">停用选中</button>
      <button class="btn secondary" :disabled="busy || !selected.length" @click="changeStatus('available')">重新启用</button>
      <button class="btn secondary" :disabled="busy || !selected.length" @click="changeStatus('reset')">重置兑换状态</button>
    </div>

    <div class="table-wrap cdk-table-wrap">
      <table class="acct cdk-table">
        <thead><tr><th></th><th>CDK</th><th>绑定邮箱</th><th>分组</th><th>状态</th><th>创建时间</th><th>兑换时间</th><th>最近访问</th><th>到期时间</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-if="!pageItems.length"><td colspan="10" class="muted account-empty">当前筛选下没有 CDK</td></tr>
          <tr v-for="item in pageItems" :key="item.id">
            <td><input v-model="selected" type="checkbox" :value="item.id" /></td>
            <td><button class="cdk-code" type="button" :disabled="!item.code" title="点击复制 CDK" @click="copyCode(item)"><code>{{ item.code || item.code_hint || "-" }}</code><span v-if="copiedId === item.id">已复制</span></button></td>
            <td><strong class="cdk-email">{{ item.email || "-" }}</strong><small>{{ item.account_id || "无账号 ID" }} · {{ item.provider_type || "auto" }}<template v-if="item.replacement_count"> · 已自动替换 {{ item.replacement_count }} 次</template></small></td>
            <td><span class="tag group-tag">{{ item.group_name || "-" }}</span></td>
            <td><span :class="['tag', stateClass(item)]">{{ stateName(item) }}</span></td>
            <td>{{ fmtTime(item.created_at) }}</td><td>{{ fmtTime(item.redeemed_at) }}</td><td>{{ fmtTime(item.last_access_at) }}</td><td>{{ fmtTime(item.expires_at) }}</td>
            <td><button class="btn secondary cdk-reset" :disabled="busy || item.status !== 'redeemed'" @click="resetOne(item)">重置</button></td>
          </tr>
        </tbody>
      </table>
    </div>
    <AppPager v-model:page="page" :pages="pages" :total="filtered.length" />
  </section>
</template>
