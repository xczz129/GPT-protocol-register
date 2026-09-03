<script setup>
import { computed, ref, watch } from "vue";
import { Globe, X } from "@lucide/vue";

const props = defineProps({
  open: Boolean,
  provider: { type: String, default: "smsbower" },
  items: { type: Array, default: () => [] },
  selected: { type: Array, default: () => [] },
  loading: Boolean,
  error: { type: String, default: "" },
});
const emit = defineEmits(["close", "toggle"]);

const search = ref("");
const minPrice = ref("");
const maxPrice = ref("");

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
              <small>{{ provider === "firefox" ? "火狐狸" : "SMSBower" }} · 点击切换选中,按选择顺序取号 · 显示价格与库存</small>
            </div>
            <button class="app-dialog-close" type="button" title="关闭" aria-label="关闭" @click="emit('close')"><X :size="16" aria-hidden="true" /></button>
          </div>
          <div class="sms-country-toolbar">
            <input v-model="search" class="sms-country-search" type="text" placeholder="搜索国家 / 区号 / ID,如 +44 或 印尼" />
            <template v-if="provider !== 'firefox'">
              <label class="sms-price-filter">最低价<input v-model="minPrice" class="sms-price-input" type="number" min="0" step="0.01" placeholder="0" /></label>
              <label class="sms-price-filter">最高价<input v-model="maxPrice" class="sms-price-input" type="number" min="0" step="0.01" placeholder="不限" /></label>
            </template>
            <button class="btn secondary sm" type="button" title="清空搜索与价格过滤" @click="resetFilters">重置</button>
          </div>
          <div class="sms-country-list">
            <div v-if="loading" class="sms-country-empty">正在获取国家列表…</div>
            <div v-else-if="error" class="sms-country-empty sms-country-error">{{ error }}</div>
            <div v-else-if="!filtered.length" class="sms-country-empty">没有匹配的国家{{ minPrice || maxPrice ? "(试试放宽价格区间)" : "" }}</div>
            <button
              v-for="item in filtered"
              :key="item.id"
              :class="['sms-country-row', selected.includes(item.id) ? 'on' : '']"
              type="button"
              @click="emit('toggle', item.id)"
            >
              <span class="sms-country-order">{{ selected.includes(item.id) ? selected.indexOf(item.id) + 1 : "" }}</span>
              <span class="sms-country-name">{{ item.name }}</span>
              <span v-if="item.calling" class="sms-country-calling">{{ item.calling }}</span>
              <span v-if="item.price" class="sms-country-price">≈${{ item.price }}</span>
              <span v-if="item.count" class="sms-country-stock">库存 {{ item.count }}</span>
              <span v-else-if="!item.calling && !item.price" class="sms-country-id">#{{ item.id }}</span>
            </button>
          </div>
          <div class="app-dialog-actions">
            <span class="sms-country-count">已选 {{ selectedCount }} 个 · 共 {{ filtered.length }} 个国家</span>
            <button class="btn secondary" type="button" @click="emit('close')">完成</button>
          </div>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>