<script setup>
import { computed, ref } from "vue";
import { Globe, RotateCcw, Search, X } from "@lucide/vue";

const props = defineProps({
  open: Boolean,
  provider: { type: String, default: "smsbower" },
  items: { type: Array, default: () => [] },
  selected: { type: Array, default: () => [] },
  prices: { type: Object, default: () => ({}) },
  providers: { type: Object, default: () => ({}) },
  loading: Boolean,
  error: { type: String, default: "" },
});
const emit = defineEmits(["close", "toggle", "set-provider"]);

const search = ref("");
const minPrice = ref("");
const maxPrice = ref("");
const expanded = ref(null);

const filtered = computed(() => {
  const needle = String(search.value || "").trim().toLowerCase();
  const min = Number(minPrice.value) || 0;
  const max = Number(maxPrice.value) || 0;
  return props.items.filter((item) => {
    if (needle) {
      const name = String(item.name || "").toLowerCase();
      const calling = String(item.calling || "").toLowerCase();
      const id = String(item.id || "");
      if (!name.includes(needle) && !calling.includes(needle) && id !== needle) return false;
    }
    const price = Number(item.price);
    if (minPrice.value && (!item.price || price < min)) return false;
    if (maxPrice.value && (!item.price || price > max)) return false;
    return true;
  });
});

const selectedCount = computed(() => props.selected.length);

function resetFilters() {
  search.value = "";
  minPrice.value = "";
  maxPrice.value = "";
}

function roundPrice(value) {
  return String(Math.round(value * 1000) / 1000);
}

function fmtCount(value) {
  const count = Number(value) || 0;
  return count >= 10000 ? `${Math.round(count / 1000)}k` : String(count);
}

function toggleExpand(item) {
  expanded.value = expanded.value === item.id ? null : item.id;
}

function providerList(id) {
  const raw = String(props.providers[id] || "").trim();
  // "|" is the tier separator inside one token; "," stays accepted for
  // configs written by older builds.
  return raw ? raw.split(/[|,]/).map((pid) => pid.trim()).filter(Boolean) : [];
}

function providerRank(id, pid) {
  const list = providerList(id);
  const index = list.findIndex((item) => String(item) === String(pid));
  return index >= 0 ? index + 1 : 0;
}

function toggleTier(id, pid) {
  const list = providerList(id);
  const index = list.findIndex((item) => String(item) === String(pid));
  if (index >= 0) {
    list.splice(index, 1);
  } else {
    list.push(String(pid));
  }
  emit("set-provider", { id, providerId: list.join("|") });
}

function clearTiers(id) {
  emit("set-provider", { id, providerId: "" });
}
</script>

<template>
  <Teleport to="body">
    <Transition name="dialog-fade">
      <div v-if="open" class="app-dialog-mask" role="presentation" @click.self="emit('close')">
        <section class="app-dialog sms-country-dialog" role="dialog" aria-modal="true" aria-label="选择国家" @keyup.esc="emit('close')">
          <div class="app-dialog-head">
            <div>
              <span class="app-dialog-kicker"><Globe :size="12" aria-hidden="true" />选择国家</span>
              <h2>接码国家顺序</h2>
              <small>{{ provider === "firefox" ? "火狐狸" : "SMSBower" }} · 点行选中国家 · 点「价位」按顺序多选价格档位</small>
            </div>
            <button class="app-dialog-close" type="button" title="关闭" aria-label="关闭" @click="emit('close')"><X :size="16" aria-hidden="true" /></button>
          </div>

          <div class="sms-country-toolbar">
            <div class="sms-country-searchwrap">
              <Search :size="14" aria-hidden="true" />
              <input v-model="search" class="sms-country-search" type="text" placeholder="搜索国家 / 区号 / ID,如 +44 或 印尼" />
            </div>
            <template v-if="provider !== 'firefox'">
              <label class="sms-price-filter">$ ≥<input v-model="minPrice" class="sms-price-input" type="number" min="0" step="0.01" placeholder="0" /></label>
              <label class="sms-price-filter">$ ≤<input v-model="maxPrice" class="sms-price-input" type="number" min="0" step="0.01" placeholder="不限" /></label>
            </template>
            <button class="btn secondary sm" type="button" title="清空搜索与价格过滤" @click="resetFilters"><RotateCcw :size="13" aria-hidden="true" />重置</button>
          </div>

          <div class="sms-country-list">
            <div v-if="loading" class="sms-country-empty">正在获取国家列表…</div>
            <div v-else-if="error" class="sms-country-empty sms-country-error">{{ error }}</div>
            <div v-else-if="!filtered.length" class="sms-country-empty">没有匹配的国家{{ minPrice || maxPrice ? "(试试放宽价格区间)" : "" }}</div>
            <template v-for="item in filtered" :key="item.id">
              <div
                :class="['sms-country-row', selected.includes(item.id) ? 'on' : '']"
                role="button"
                tabindex="0"
                :aria-pressed="selected.includes(item.id)"
                @click="emit('toggle', item.id)"
                @keydown.enter.prevent="emit('toggle', item.id)"
                @keydown.space.prevent="emit('toggle', item.id)"
              >
                <span class="sms-country-order">{{ selected.includes(item.id) ? selected.indexOf(item.id) + 1 : "" }}</span>
                <span class="sms-country-name">{{ item.name }}</span>
                <span v-if="item.calling" class="sms-country-calling">{{ item.calling }}</span>
                <span v-if="item.price" class="sms-country-price">≈${{ item.price }}</span>
                <span v-if="item.count" class="sms-country-stock">{{ fmtCount(item.count) }} 件</span>
                <span
                  v-if="provider !== 'firefox' && (item.tiers || []).length"
                  :class="['sms-country-price-pick', providerList(item.id).length ? 'picked' : '']"
                  title="展开价格档位(可按顺序多选)"
                  @click.stop="toggleExpand(item)"
                >
                  {{ providerList(item.id).length ? providerList(item.id).map((pid) => `#${pid}`).slice(0, 2).join("·") + (providerList(item.id).length > 2 ? "…" : "") : "价位" }}
                </span>
              </div>
              <div v-if="expanded === item.id" class="sms-tier-panel">
                <div class="sms-tier-head">
                  <span v-if="providerList(item.id).length" class="sms-tier-seq">购买顺序:{{ providerList(item.id).map((pid) => `#${pid}`).join(" → ") }}</span>
                  <span v-else class="sms-tier-hint">点价格追加,先选的先试</span>
                  <button v-if="providerList(item.id).length" class="btn sm ghost" type="button" @click="clearTiers(item.id)">清空</button>
                </div>
                <button
                  v-for="tier in item.tiers"
                  :key="tier.id"
                  type="button"
                  :class="['sms-tier-row', providerRank(item.id, tier.id) ? 'cur' : '']"
                  @click="toggleTier(item.id, tier.id)"
                >
                  <span class="sms-tier-rank">{{ providerRank(item.id, tier.id) || "" }}</span>
                  <b>${{ roundPrice(tier.price) }}</b>
                  <small>#{{ tier.id }} · {{ fmtCount(tier.count) }} 件</small>
                </button>
              </div>
            </template>
          </div>

          <div class="app-dialog-actions">
            <span class="sms-country-count">已选 {{ selectedCount }} 个国家 · 共 {{ filtered.length }} 个</span>
            <button class="btn secondary" type="button" @click="emit('close')">完成</button>
          </div>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>
